//! Proposals (plan 17 D48–D53): a named template, the role that runs it, literal survey values and a note, stored by
//! the console. The launch gate previews each one (`/v1/decide`, D51), and a platform admin's approval launches it
//! (`/v1/launch`, D52). The rows live in `prefs.db` beside the preferences (D50).
//!
//! Every state change is one guarded UPDATE. An approval moves its row from `awaiting` to `launching` only while the
//! preview is `allowed`, so only the request that changed the row launches (D53). Nothing holds the database lock
//! across a gate call.

use std::collections::BTreeMap;
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use dioxus::server::axum::extract::rejection::{JsonRejection, QueryRejection};
use dioxus::server::axum::extract::{Path, Query, State};
use dioxus::server::axum::http::StatusCode;
use dioxus::server::axum::response::{IntoResponse, Response};
use dioxus::server::axum::Json;
use rusqlite::{params, Connection, OptionalExtension, Row};
use serde::de::DeserializeOwned;
use serde::{Deserialize, Serialize};
use serde_json::json;
use tokio::sync::Mutex;
use tower_sessions::Session;

use super::api;
use super::auth::{self, CurrentUser};
use super::gate::{self, GateCall, GateVerdict, LaunchOutcome};
use super::work::{self, rfc3339_from_unix};
use super::{AppState, SharedState};

/// The agent that owns both first-slice registry steps (D48).
const AGENT: &str = "service-agent";
const NOTE_MAX: usize = 2000;
const COMMENT_MAX: usize = 2000;
/// The most rows one list answers.
const LIST_MAX: usize = 200;

const AWAITING: &str = "awaiting";
/// Stored while an approval's launch is in flight. It reads as `launching` inside `LAUNCH_WINDOW_SECS`, then as `launch_failed`.
const LAUNCHING: &str = "launching";
const DENIED: &str = "denied";
const LAUNCHED: &str = "launched";
const LAUNCH_FAILED: &str = "launch_failed";

const APPROVED: &str = "approved";
/// What a proposal's preview shows until its first decide answers. Never `allowed`, so nothing launches on it.
const PREVIEW_PENDING: &str = "the preview has not finished; edit the proposal to run it again";
/// What a launch shows once its row has stayed in `launching` past `LAUNCH_WINDOW_SECS`: the gate never answered.
const INTERRUPTED: &str = "The launch did not finish; check Active Work before proposing again.";
/// How long a row may stay `launching` and still read as a launch in flight. The gate's launch call is capped at
/// 120 s (`gate.rs`, `LAUNCH_TIMEOUT`); the window is that cap plus 30 s.
const LAUNCH_WINDOW_SECS: i64 = 150;

const CREATE_SHAPE: &str = r#"request body must be JSON {"kind", "service", "check_mode", "note"}"#;
const EDIT_SHAPE: &str = r#"request body must be JSON {"check_mode", "note"}"#;
const COMMENT_SHAPE: &str = r#"request body must be JSON {"text"}"#;
const DECISION_SHAPE: &str = r#"request body must be JSON {"note", "revision"}"#;
const KIND_RULE: &str = "kind must be assess or verify";
const SERVICE_RULE: &str =
    "service must be 1 to 63 characters: a-z, 0-9 and -, starting with a lowercase letter or digit";
const NOTE_RULE: &str = "note must be at most 2000 characters";

const SCHEMA: &str = "CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    service TEXT NOT NULL,
    template TEXT NOT NULL,
    agent TEXT NOT NULL,
    step TEXT NOT NULL,
    survey_json TEXT NOT NULL,
    check_mode INTEGER NOT NULL,
    note TEXT NOT NULL,
    state TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    proposer_sub TEXT NOT NULL,
    proposer_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    preview_state TEXT NOT NULL,
    preview_json TEXT NOT NULL,
    decision_json TEXT,
    launch_json TEXT
);
CREATE TABLE IF NOT EXISTS proposal_comments (
    id INTEGER PRIMARY KEY,
    proposal_id INTEGER NOT NULL,
    by_sub TEXT NOT NULL,
    by_name TEXT NOT NULL,
    at TEXT NOT NULL,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS proposal_comments_by_proposal ON proposal_comments (proposal_id, id);";

/// The columns `read_row` reads, in one place so every query selects the same shape.
const COLUMNS: &str =
    "id, kind, service, template, agent, step, survey_json, check_mode, note, state, revision, \
    proposer_sub, proposer_name, created_at, updated_at, preview_json, decision_json, launch_json";

/// Creates the proposal tables. Called from `Prefs::from_connection`, beside the sessions table.
pub fn init(conn: &Connection) -> rusqlite::Result<()> {
    conn.execute_batch(SCHEMA)
}

/// A person the console names: the subject it keys on and the name it shows.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Person {
    pub sub: String,
    pub name: String,
}

/// `CurrentUser` as a proposal names a person: the display name, else username, else email, else the subject.
pub fn person(user: &CurrentUser) -> Person {
    let name = [&user.name, &user.username, &user.email]
        .into_iter()
        .flatten()
        .find(|value| !value.trim().is_empty())
        .cloned()
        .unwrap_or_else(|| user.sub.clone());
    Person {
        sub: user.sub.clone(),
        name,
    }
}

/// The stored preview (D51): the gate's answer at the last decide, in one shape for all four answers.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Preview {
    pub state: String,
    pub reason: Option<String>,
    pub detail: Option<String>,
    pub at: String,
}

impl Preview {
    /// The gate's answer in the stored shape. A refusal keeps its code and detail; its status is not stored.
    pub fn from_verdict(verdict: GateVerdict, at: String) -> Self {
        let (state, reason, detail) = match verdict {
            GateVerdict::Allowed { reason } => ("allowed", Some(reason), None),
            GateVerdict::Denied { reason } => ("denied", Some(reason), None),
            GateVerdict::Refused { code, detail } => {
                ("refused", None, Some(format!("{code}: {detail}")))
            }
            GateVerdict::Unavailable { detail } => ("unavailable", None, Some(detail)),
        };
        Self {
            state: state.to_string(),
            reason,
            detail,
            at,
        }
    }

    /// The placeholder a new proposal stores until its first decide answers.
    fn pending(at: String) -> Self {
        Self {
            state: "unavailable".to_string(),
            reason: None,
            detail: Some(PREVIEW_PENDING.to_string()),
            at,
        }
    }
}

/// An approval or a denial. `self_approved` is set only for an approval by the person who proposed it (D52).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Decision {
    pub action: String,
    pub by: Person,
    pub at: String,
    pub note: Option<String>,
    pub self_approved: bool,
}

impl Decision {
    fn new(action: &str, by: Person, proposer: &Person, note: Option<String>, at: String) -> Self {
        let self_approved = action == APPROVED && by.sub == proposer.sub;
        Self {
            action: action.to_string(),
            by,
            at,
            note,
            self_approved,
        }
    }
}

/// What a launch left behind: the task the gate created, or the error that stopped it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum Launch {
    Launched {
        task_id: u64,
        message: String,
        reason: String,
    },
    Failed {
        error: LaunchError,
    },
}

/// A launch that did not create a task. `status` is 0 when the gate gave no HTTP answer.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct LaunchError {
    pub status: u16,
    pub code: String,
    pub detail: String,
    pub reason: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Comment {
    pub by: Person,
    pub at: String,
    pub text: String,
}

/// One proposal as the API answers it. `state` is one of five: `awaiting`, `launching` (a launch in flight), `denied`,
/// `launched` and `launch_failed`. A `launching` row older than `LAUNCH_WINDOW_SECS` answers `launch_failed` with an
/// `interrupted` launch.
#[derive(Debug, Clone, Serialize)]
pub struct Proposal {
    pub id: String,
    /// Starts at 1 and goes up by one on every edit. An approval or denial names the revision it reviewed (D53).
    pub revision: i64,
    pub kind: String,
    pub service: String,
    pub template: String,
    pub agent: String,
    pub step: String,
    pub survey: BTreeMap<String, String>,
    pub check_mode: bool,
    pub note: String,
    pub state: String,
    pub proposer: Person,
    pub created_at: String,
    pub updated_at: String,
    pub preview: Preview,
    pub decision: Option<Decision>,
    pub launch: Option<Launch>,
    pub comments: Vec<Comment>,
}

/// The values a create writes, before its id and preview exist.
pub struct NewProposal {
    pub kind: String,
    pub service: String,
    pub template: String,
    pub agent: String,
    pub step: String,
    pub survey: BTreeMap<String, String>,
    pub check_mode: bool,
    pub note: String,
    pub proposer: Person,
}

/// Why a guarded change did not happen. Each maps to one answer in the handlers.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Refusal {
    Missing,
    NotAwaiting,
    /// The proposal was edited after the approver loaded it: its revision moved on.
    Stale,
    PreviewNotAllowed,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ListFilter {
    All,
    Awaiting,
    Decided,
}

impl ListFilter {
    /// The SQL for this filter. Fixed text, never user input.
    fn clause(self) -> &'static str {
        match self {
            Self::All => "",
            Self::Awaiting => "WHERE state = 'awaiting'",
            Self::Decided => "WHERE state != 'awaiting'",
        }
    }
}

/// The proposal store: the `proposals` and `proposal_comments` tables on the preferences connection.
pub struct ProposalStore {
    conn: Arc<Mutex<Connection>>,
}

impl ProposalStore {
    pub(super) fn new(conn: Arc<Mutex<Connection>>) -> Self {
        Self { conn }
    }

    /// Inserts an awaiting proposal with the placeholder preview and returns its row id.
    pub async fn insert(&self, new: &NewProposal, at: &str) -> rusqlite::Result<i64> {
        let conn = self.conn.lock().await;
        let preview = Preview::pending(at.to_string());
        conn.execute(
            "INSERT INTO proposals (kind, service, template, agent, step, survey_json, check_mode, note, state, revision,
                proposer_sub, proposer_name, created_at, updated_at, preview_state, preview_json)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, 'awaiting', 1, ?9, ?10, ?11, ?11, ?12, ?13)",
            params![
                new.kind,
                new.service,
                new.template,
                new.agent,
                new.step,
                to_json(&new.survey)?,
                new.check_mode,
                new.note,
                new.proposer.sub,
                new.proposer.name,
                at,
                preview.state,
                to_json(&preview)?,
            ],
        )?;
        Ok(conn.last_insert_rowid())
    }

    /// Stores the preview of `revision`, and only while the row is awaiting at that revision. False when a later edit
    /// has moved the row on: that preview is stale and is dropped, and the caller re-reads the row.
    pub async fn set_preview(
        &self,
        id: i64,
        revision: i64,
        preview: &Preview,
    ) -> rusqlite::Result<bool> {
        let conn = self.conn.lock().await;
        let changed = conn.execute(
            "UPDATE proposals SET preview_state = ?1, preview_json = ?2, updated_at = ?3
             WHERE id = ?4 AND state = 'awaiting' AND revision = ?5",
            params![preview.state, to_json(preview)?, preview.at, id, revision],
        )?;
        Ok(changed == 1)
    }

    pub async fn get(&self, id: i64) -> rusqlite::Result<Option<Proposal>> {
        self.get_at(id, now_unix()).await
    }

    /// `get` as of `now` (Unix seconds). Only a `launching` row depends on the time (`read_row`), so tests pass it.
    pub async fn get_at(&self, id: i64, now: i64) -> rusqlite::Result<Option<Proposal>> {
        let conn = self.conn.lock().await;
        load(&conn, id, &launch_cutoff(now))
    }

    /// Newest first, at most `LIST_MAX`.
    pub async fn list(&self, filter: ListFilter) -> rusqlite::Result<Vec<Proposal>> {
        let conn = self.conn.lock().await;
        load_list(&conn, filter, &launch_cutoff(now_unix()))
    }

    /// Starts an edit (D51): in one UPDATE while the row is awaiting, it writes the values, bumps the revision and resets
    /// the preview to the placeholder. The placeholder keeps every approval refused until the new preview is stored, so
    /// no approval pairs the new values with the old verdict. Returns the row at its new revision.
    pub async fn begin_edit(
        &self,
        id: i64,
        check_mode: bool,
        note: &str,
        at: &str,
    ) -> rusqlite::Result<Result<Proposal, Refusal>> {
        let conn = self.conn.lock().await;
        let placeholder = Preview::pending(at.to_string());
        let changed = conn.execute(
            "UPDATE proposals SET check_mode = ?1, note = ?2, revision = revision + 1,
                 preview_state = ?3, preview_json = ?4, updated_at = ?5
             WHERE id = ?6 AND state = 'awaiting'",
            params![
                check_mode,
                note,
                placeholder.state,
                to_json(&placeholder)?,
                at,
                id
            ],
        )?;
        if changed == 1 {
            return Ok(load(&conn, id, &launch_cutoff(now_unix()))?.ok_or(Refusal::Missing));
        }
        let refusal = if stored_row(&conn, id)?.is_some() {
            Refusal::NotAwaiting
        } else {
            Refusal::Missing
        };
        Ok(Err(refusal))
    }

    /// The approval's one transition (D53): `awaiting` to `launching`, with the decision recorded in the same UPDATE.
    /// It applies only while the preview is `allowed` and the row is still at `revision`, the one the approver
    /// reviewed. On success it returns the row, so the launch uses the values as they were at the claim. Otherwise it
    /// says why, from the same lock.
    pub async fn claim(
        &self,
        id: i64,
        revision: i64,
        decision: &Decision,
    ) -> rusqlite::Result<Result<Proposal, Refusal>> {
        let conn = self.conn.lock().await;
        let changed = conn.execute(
            "UPDATE proposals SET state = 'launching', decision_json = ?1, updated_at = ?2
             WHERE id = ?3 AND state = 'awaiting' AND preview_state = 'allowed' AND revision = ?4",
            params![to_json(decision)?, decision.at, id, revision],
        )?;
        if changed == 1 {
            return Ok(load(&conn, id, &launch_cutoff(now_unix()))?.ok_or(Refusal::Missing));
        }
        Ok(Err(refused(
            &conn,
            id,
            revision,
            Refusal::PreviewNotAllowed,
        )?))
    }

    /// The denial's transition: `awaiting` to `denied`, one guarded UPDATE at the revision the denier reviewed.
    pub async fn deny(
        &self,
        id: i64,
        revision: i64,
        decision: &Decision,
    ) -> rusqlite::Result<Result<Proposal, Refusal>> {
        let conn = self.conn.lock().await;
        let changed = conn.execute(
            "UPDATE proposals SET state = 'denied', decision_json = ?1, updated_at = ?2
             WHERE id = ?3 AND state = 'awaiting' AND revision = ?4",
            params![to_json(decision)?, decision.at, id, revision],
        )?;
        if changed == 1 {
            return Ok(load(&conn, id, &launch_cutoff(now_unix()))?.ok_or(Refusal::Missing));
        }
        // A denial has no preview gate, so an awaiting row at this revision cannot reach the last arm.
        Ok(Err(refused(&conn, id, revision, Refusal::NotAwaiting)?))
    }

    /// Records the gate's answer to a claimed row. False when the row is no longer `launching`.
    pub async fn record_launch(
        &self,
        id: i64,
        state: &str,
        launch: &Launch,
        at: &str,
    ) -> rusqlite::Result<bool> {
        let conn = self.conn.lock().await;
        let changed = conn.execute(
            "UPDATE proposals SET state = ?1, launch_json = ?2, updated_at = ?3
             WHERE id = ?4 AND state = 'launching'",
            params![state, to_json(launch)?, at, id],
        )?;
        Ok(changed == 1)
    }

    /// Appends a comment in any state. False when the proposal does not exist. A comment leaves `updated_at` alone.
    pub async fn add_comment(
        &self,
        id: i64,
        by: &Person,
        at: &str,
        text: &str,
    ) -> rusqlite::Result<bool> {
        let conn = self.conn.lock().await;
        if stored_row(&conn, id)?.is_none() {
            return Ok(false);
        }
        conn.execute(
            "INSERT INTO proposal_comments (proposal_id, by_sub, by_name, at, text) VALUES (?1, ?2, ?3, ?4, ?5)",
            params![id, by.sub, by.name, at, text],
        )?;
        Ok(true)
    }
}

fn to_json<T: Serialize>(value: &T) -> rusqlite::Result<String> {
    serde_json::to_string(value)
        .map_err(|err| rusqlite::Error::ToSqlConversionFailure(Box::new(err)))
}

fn decode_error(err: serde_json::Error) -> rusqlite::Error {
    rusqlite::Error::FromSqlConversionFailure(0, rusqlite::types::Type::Text, Box::new(err))
}

fn json_column<T: DeserializeOwned>(row: &Row<'_>, name: &str) -> rusqlite::Result<T> {
    let text: String = row.get(name)?;
    serde_json::from_str(&text).map_err(decode_error)
}

fn json_opt<T: DeserializeOwned>(row: &Row<'_>, name: &str) -> rusqlite::Result<Option<T>> {
    let text: Option<String> = row.get(name)?;
    text.map(|text| serde_json::from_str(&text).map_err(decode_error))
        .transpose()
}

/// The state and revision of a row, or `None` when there is no such proposal.
fn stored_row(conn: &Connection, id: i64) -> rusqlite::Result<Option<(String, i64)>> {
    conn.query_row(
        "SELECT state, revision FROM proposals WHERE id = ?1",
        [id],
        |row| Ok((row.get(0)?, row.get(1)?)),
    )
    .optional()
}

/// Why a guarded UPDATE changed no row. The row is missing, no longer awaiting, or at a newer revision than the one
/// the caller reviewed. Once none of those holds, the caller's own guard is what refused it, and `otherwise` names it.
fn refused(
    conn: &Connection,
    id: i64,
    revision: i64,
    otherwise: Refusal,
) -> rusqlite::Result<Refusal> {
    Ok(match stored_row(conn, id)? {
        None => Refusal::Missing,
        Some((state, _)) if state != AWAITING => Refusal::NotAwaiting,
        Some((_, current)) if current != revision => Refusal::Stale,
        Some(_) => otherwise,
    })
}

/// One row as the API shapes it, with the numeric id kept beside it for the comments query. A `launching` row reads as
/// in flight while its `updated_at` is at or after `cutoff` (`launch_cutoff`), and as interrupted before it. Both
/// stamps are text from `rfc3339_from_unix`, fixed-width UTC, so comparing them as text compares their times.
fn read_row(row: &Row<'_>, cutoff: &str) -> rusqlite::Result<(i64, Proposal)> {
    let id: i64 = row.get("id")?;
    let stored: String = row.get("state")?;
    let (state, launch) = if stored == LAUNCHING {
        let updated: String = row.get("updated_at")?;
        if updated.as_str() >= cutoff {
            (LAUNCHING.to_string(), None)
        } else {
            let error = LaunchError {
                status: 0,
                code: "interrupted".to_string(),
                detail: INTERRUPTED.to_string(),
                reason: None,
            };
            (LAUNCH_FAILED.to_string(), Some(Launch::Failed { error }))
        }
    } else {
        (stored, json_opt(row, "launch_json")?)
    };
    let proposal = Proposal {
        id: format!("p{id}"),
        revision: row.get("revision")?,
        kind: row.get("kind")?,
        service: row.get("service")?,
        template: row.get("template")?,
        agent: row.get("agent")?,
        step: row.get("step")?,
        survey: json_column(row, "survey_json")?,
        check_mode: row.get("check_mode")?,
        note: row.get("note")?,
        state,
        proposer: Person {
            sub: row.get("proposer_sub")?,
            name: row.get("proposer_name")?,
        },
        created_at: row.get("created_at")?,
        updated_at: row.get("updated_at")?,
        preview: json_column(row, "preview_json")?,
        decision: json_opt(row, "decision_json")?,
        launch,
        comments: Vec::new(),
    };
    Ok((id, proposal))
}

fn load_comments(conn: &Connection, id: i64) -> rusqlite::Result<Vec<Comment>> {
    let mut stmt = conn.prepare(
        "SELECT by_sub, by_name, at, text FROM proposal_comments WHERE proposal_id = ?1 ORDER BY id",
    )?;
    let comments = stmt
        .query_map([id], |row| {
            Ok(Comment {
                by: Person {
                    sub: row.get(0)?,
                    name: row.get(1)?,
                },
                at: row.get(2)?,
                text: row.get(3)?,
            })
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    Ok(comments)
}

fn load(conn: &Connection, id: i64, cutoff: &str) -> rusqlite::Result<Option<Proposal>> {
    let found = conn
        .query_row(
            &format!("SELECT {COLUMNS} FROM proposals WHERE id = ?1"),
            [id],
            |row| read_row(row, cutoff),
        )
        .optional()?;
    let Some((num, mut proposal)) = found else {
        return Ok(None);
    };
    proposal.comments = load_comments(conn, num)?;
    Ok(Some(proposal))
}

fn load_list(
    conn: &Connection,
    filter: ListFilter,
    cutoff: &str,
) -> rusqlite::Result<Vec<Proposal>> {
    let sql = format!(
        "SELECT {COLUMNS} FROM proposals {} ORDER BY id DESC LIMIT {LIST_MAX}",
        filter.clause()
    );
    let mut stmt = conn.prepare(&sql)?;
    let rows = stmt
        .query_map([], |row| read_row(row, cutoff))?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    let mut proposals = Vec::with_capacity(rows.len());
    for (num, mut proposal) in rows {
        proposal.comments = load_comments(conn, num)?;
        proposals.push(proposal);
    }
    Ok(proposals)
}

/// `p` then one or more digits, with no sign, space or leading `p` repeated. Anything else is not a proposal.
fn parse_id(text: &str) -> Option<i64> {
    let digits = text.strip_prefix('p')?;
    if digits.is_empty() || !digits.bytes().all(|byte| byte.is_ascii_digit()) {
        return None;
    }
    digits.parse::<i64>().ok().filter(|num| *num > 0)
}

/// The two first-slice kinds (D48): the base template each launches and its registry step. `None` for anything else.
fn kind_spec(kind: &str) -> Option<(&'static str, &'static str)> {
    match kind {
        "assess" => Some(("Snapshot Service Assessment", "service-assess")),
        "verify" => Some(("Verify Service Health", "service-validate")),
        _ => None,
    }
}

/// A service name as inventory writes a VM: `[a-z0-9][a-z0-9-]{0,62}`.
fn valid_service(name: &str) -> bool {
    let mut chars = name.chars();
    matches!(chars.next(), Some(c) if c.is_ascii_lowercase() || c.is_ascii_digit())
        && name.len() <= 63
        && chars.all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-')
}

/// A note as the console stores it: trimmed, and within the bound. The error is the 400 detail. The helpers return it
/// rather than a response, because a response is too large for a `Result` error (clippy `result_large_err`).
fn note_text(raw: &str) -> Result<String, &'static str> {
    let note = raw.trim();
    if note.chars().count() > NOTE_MAX {
        return Err(NOTE_RULE);
    }
    Ok(note.to_string())
}

/// An approval's or denial's body: the note, trimmed (`None` when empty) and within the bound, and the revision the
/// decider reviewed. The error is the 400 detail.
fn decision_body(body: Result<Json<DecisionBody>, JsonRejection>) -> Result<Decided, &'static str> {
    let Json(body) = body.map_err(|_| DECISION_SHAPE)?;
    let note = body
        .note
        .map(|note| note.trim().to_string())
        .filter(|note| !note.is_empty());
    if note
        .as_ref()
        .is_some_and(|note| note.chars().count() > NOTE_MAX)
    {
        return Err(NOTE_RULE);
    }
    Ok(Decided {
        note,
        revision: body.revision,
    })
}

/// What an approval or denial carries: the note and the revision the decider reviewed.
struct Decided {
    note: Option<String>,
    revision: i64,
}

/// The request a proposal makes of the gate. `dry_run` is the proposal's check mode (D52).
pub fn call_of(proposal: &Proposal) -> GateCall {
    GateCall {
        template: proposal.template.clone(),
        agent: proposal.agent.clone(),
        workflow_id: proposal.id.clone(),
        step: proposal.step.clone(),
        survey: proposal.survey.clone(),
        dry_run: proposal.check_mode,
    }
}

/// What a launch answer records: the state it moves the row to and the launch it keeps (D52).
fn launch_record(outcome: LaunchOutcome) -> (&'static str, Launch) {
    let failed = |status, code: &str, detail: &str, reason| Launch::Failed {
        error: LaunchError {
            status,
            code: code.to_string(),
            detail: detail.to_string(),
            reason,
        },
    };
    match outcome {
        LaunchOutcome::Launched {
            task_id,
            message,
            reason,
        } => (
            LAUNCHED,
            Launch::Launched {
                task_id,
                message,
                reason,
            },
        ),
        // The gate's denial is 403 `opa_denied` with the detail below (`launch_gate.py`, `opa_denied`).
        LaunchOutcome::Denied { reason } => (
            LAUNCH_FAILED,
            failed(403, "opa_denied", "OPA denied the launch", Some(reason)),
        ),
        LaunchOutcome::Refused {
            status,
            code,
            detail,
        } => (LAUNCH_FAILED, failed(status, &code, &detail, None)),
        LaunchOutcome::Unavailable { detail } => {
            (LAUNCH_FAILED, failed(0, "gate_unavailable", &detail, None))
        }
    }
}

/// The current time in Unix seconds.
fn now_unix() -> i64 {
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |elapsed| elapsed.as_secs());
    i64::try_from(secs).unwrap_or(i64::MAX)
}

/// The oldest stamp a `launching` row may carry and still read as in flight at `now` (Unix seconds).
fn launch_cutoff(now: i64) -> String {
    rfc3339_from_unix(now - LAUNCH_WINDOW_SECS)
}

/// The current time as RFC 3339, the form every timestamp in the API takes.
fn now() -> String {
    rfc3339_from_unix(now_unix())
}

/// Whether the inventory lists a service of this name.
pub enum Known {
    Yes,
    No,
    NotRead,
}

/// The rule is `inventory.rs` `services_of`: a VM with workflow fields, in a scope site that maps to a NetBox site
/// the cache read. Every tenant counts, because proposals are platform-admin only. The rule is repeated here
/// because `services_of` is private to `inventory.rs`.
pub async fn known_service(app: &AppState, name: &str) -> Known {
    let cache = app.inventory.read().await;
    let Some(netbox) = cache.netbox.data.as_ref() else {
        return Known::NotRead;
    };
    let listed = app
        .scope
        .tenants
        .iter()
        .flat_map(|tenant| &tenant.orgs)
        .flat_map(|org| &org.sites)
        .filter_map(|site| {
            site.netbox_site
                .as_deref()
                .and_then(|slug| netbox.sites.get(slug))
        })
        .flat_map(|site| &site.clusters)
        .flat_map(|cluster| &cluster.vms)
        .any(|vm| vm.name == name && vm.workflow.is_some());
    if listed {
        Known::Yes
    } else {
        Known::No
    }
}

/// The platform admin making the request, or the 401/403 it gets (D25). Every proposal route runs this first.
async fn admin(session: &Session) -> Result<CurrentUser, Response> {
    work::admin_only(session).await?;
    auth::current_user(session)
        .await
        .ok_or_else(api::unauthenticated)
}

fn bad_request(detail: &str) -> Response {
    (
        StatusCode::BAD_REQUEST,
        Json(json!({ "error": "bad_request", "detail": detail })),
    )
        .into_response()
}

fn not_found() -> Response {
    api::error(StatusCode::NOT_FOUND, "not_found")
}

fn not_awaiting() -> Response {
    api::error(StatusCode::CONFLICT, "not_awaiting")
}

fn gate_unconfigured() -> Response {
    api::error(StatusCode::SERVICE_UNAVAILABLE, "gate_unconfigured")
}

fn unavailable() -> Response {
    api::error(StatusCode::INTERNAL_SERVER_ERROR, "proposals_unavailable")
}

/// 409 `stale`: the approver reviewed an older revision. Distinct from `not_awaiting`, which means the row has moved on
/// from awaiting altogether.
fn stale() -> Response {
    (
        StatusCode::CONFLICT,
        Json(json!({
            "error": "stale",
            "detail": "The proposal changed since you loaded it. Review it again.",
        })),
    )
        .into_response()
}

fn refusal_response(refusal: Refusal) -> Response {
    match refusal {
        Refusal::Missing => not_found(),
        Refusal::NotAwaiting => not_awaiting(),
        Refusal::Stale => stale(),
        Refusal::PreviewNotAllowed => api::error(StatusCode::CONFLICT, "preview_not_allowed"),
    }
}

/// The proposal as stored now, with `status`; 404 when the row is gone, 500 when the store fails.
async fn reply(store: &ProposalStore, id: i64, status: StatusCode) -> Response {
    match store.get(id).await {
        Ok(Some(proposal)) => (status, Json(proposal)).into_response(),
        Ok(None) => not_found(),
        Err(_) => unavailable(),
    }
}

#[derive(Deserialize)]
pub struct ListQuery {
    state: Option<String>,
}

#[derive(Serialize)]
struct ListBody {
    proposals: Vec<Proposal>,
}

/// `GET /v1/proposals?state=awaiting|decided|all`: newest first, at most 200.
pub async fn list_proposals(
    State(app): State<SharedState>,
    session: Session,
    query: Result<Query<ListQuery>, QueryRejection>,
) -> Response {
    if let Err(response) = admin(&session).await {
        return response;
    }
    let filter = match query.map(|Query(query)| query.state) {
        Ok(None) => ListFilter::All,
        Ok(Some(state)) => match state.as_str() {
            "all" => ListFilter::All,
            "awaiting" => ListFilter::Awaiting,
            "decided" => ListFilter::Decided,
            _ => return bad_request("state must be awaiting, decided or all"),
        },
        Err(_) => return bad_request("state must be awaiting, decided or all"),
    };
    match app.prefs.proposal_store().list(filter).await {
        Ok(proposals) => Json(ListBody { proposals }).into_response(),
        Err(_) => unavailable(),
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CreateBody {
    kind: String,
    service: String,
    check_mode: bool,
    note: String,
}

/// `POST /v1/proposals`: stores the proposal, then previews it through the gate (D51). A refusal or an
/// unreachable gate is stored as the preview, and the proposal is still created.
pub async fn create_proposal(
    State(app): State<SharedState>,
    session: Session,
    body: Result<Json<CreateBody>, JsonRejection>,
) -> Response {
    let user = match admin(&session).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Ok(Json(body)) = body else {
        return bad_request(CREATE_SHAPE);
    };
    let Some((base, step)) = kind_spec(&body.kind) else {
        return bad_request(KIND_RULE);
    };
    if !valid_service(&body.service) {
        return bad_request(SERVICE_RULE);
    }
    let note = match note_text(&body.note) {
        Ok(note) => note,
        Err(detail) => return bad_request(detail),
    };
    let Some(gate_client) = app.gate.as_ref() else {
        return gate_unconfigured();
    };
    match known_service(&app, &body.service).await {
        Known::Yes => {}
        Known::No => return bad_request("service is not in the inventory"),
        Known::NotRead => {
            return bad_request("the inventory has not been read, so no service is known yet")
        }
    }
    let new = NewProposal {
        template: gate::template_name(base, app.config.launch_dev_variants),
        agent: AGENT.to_string(),
        step: step.to_string(),
        survey: BTreeMap::from([(
            "target_service".to_string(),
            format!("{}_svc", body.service),
        )]),
        kind: body.kind,
        service: body.service,
        check_mode: body.check_mode,
        note,
        proposer: person(&user),
    };
    let store = app.prefs.proposal_store();
    let at = now();
    let id = match store.insert(&new, &at).await {
        Ok(id) => id,
        Err(_) => return unavailable(),
    };
    let proposal = match store.get(id).await {
        Ok(Some(proposal)) => proposal,
        _ => return unavailable(),
    };
    let verdict = gate_client.decide(&call_of(&proposal)).await;
    if store
        .set_preview(
            id,
            proposal.revision,
            &Preview::from_verdict(verdict, now()),
        )
        .await
        .is_err()
    {
        return unavailable();
    }
    reply(&store, id, StatusCode::CREATED).await
}

/// `GET /v1/proposals/{id}`.
pub async fn get_proposal(
    State(app): State<SharedState>,
    session: Session,
    Path(id): Path<String>,
) -> Response {
    if let Err(response) = admin(&session).await {
        return response;
    }
    let Some(num) = parse_id(&id) else {
        return not_found();
    };
    reply(&app.prefs.proposal_store(), num, StatusCode::OK).await
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EditBody {
    check_mode: bool,
    note: String,
}

/// `PUT /v1/proposals/{id}`: edits check mode and note while awaiting, and runs the preview again.
pub async fn edit_proposal(
    State(app): State<SharedState>,
    session: Session,
    Path(id): Path<String>,
    body: Result<Json<EditBody>, JsonRejection>,
) -> Response {
    if let Err(response) = admin(&session).await {
        return response;
    }
    let Some(num) = parse_id(&id) else {
        return not_found();
    };
    let Ok(Json(body)) = body else {
        return bad_request(EDIT_SHAPE);
    };
    let note = match note_text(&body.note) {
        Ok(note) => note,
        Err(detail) => return bad_request(detail),
    };
    let store = app.prefs.proposal_store();
    let current = match store.get(num).await {
        Ok(Some(proposal)) => proposal,
        Ok(None) => return not_found(),
        Err(_) => return unavailable(),
    };
    if current.state != AWAITING {
        return not_awaiting();
    }
    let Some(gate_client) = app.gate.as_ref() else {
        return gate_unconfigured();
    };
    // The revision moves and the preview resets before the decide runs, so no approval can pair the new values with
    // the old verdict while the decide is in flight.
    let edited = match store.begin_edit(num, body.check_mode, &note, &now()).await {
        Ok(Ok(proposal)) => proposal,
        Ok(Err(refusal)) => return refusal_response(refusal),
        Err(_) => return unavailable(),
    };
    let call = GateCall {
        dry_run: body.check_mode,
        ..call_of(&edited)
    };
    let preview = Preview::from_verdict(gate_client.decide(&call).await, now());
    // A preview for an older revision is dropped by the guard in `set_preview`. The reply re-reads the row either way.
    match store.set_preview(num, edited.revision, &preview).await {
        Ok(_) => reply(&store, num, StatusCode::OK).await,
        Err(_) => unavailable(),
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CommentBody {
    text: String,
}

/// `POST /v1/proposals/{id}/comments`: allowed in any state.
pub async fn add_comment(
    State(app): State<SharedState>,
    session: Session,
    Path(id): Path<String>,
    body: Result<Json<CommentBody>, JsonRejection>,
) -> Response {
    let user = match admin(&session).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(num) = parse_id(&id) else {
        return not_found();
    };
    let Ok(Json(body)) = body else {
        return bad_request(COMMENT_SHAPE);
    };
    let text = body.text.trim();
    if text.is_empty() || text.chars().count() > COMMENT_MAX {
        return bad_request("text must be 1 to 2000 characters");
    }
    let store = app.prefs.proposal_store();
    match store.add_comment(num, &person(&user), &now(), text).await {
        Ok(true) => reply(&store, num, StatusCode::CREATED).await,
        Ok(false) => not_found(),
        Err(_) => unavailable(),
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DecisionBody {
    note: Option<String>,
    /// The revision the decider reviewed. Required: a decision names what it approves (D53).
    revision: i64,
}

/// `POST /v1/proposals/{id}/approve` (D52, D53): claims the row once, then launches through the gate. Only the
/// request that claims the row launches, and the gate's answer at launch is the one that counts.
pub async fn approve_proposal(
    State(app): State<SharedState>,
    session: Session,
    Path(id): Path<String>,
    body: Result<Json<DecisionBody>, JsonRejection>,
) -> Response {
    let user = match admin(&session).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(num) = parse_id(&id) else {
        return not_found();
    };
    let decided = match decision_body(body) {
        Ok(decided) => decided,
        Err(detail) => return bad_request(detail),
    };
    // Checked before the claim: a row claimed with no gate configured would stay `launching` with nowhere to go.
    let Some(gate_client) = app.gate.as_ref() else {
        return gate_unconfigured();
    };
    let store = app.prefs.proposal_store();
    let current = match store.get(num).await {
        Ok(Some(proposal)) => proposal,
        Ok(None) => return not_found(),
        Err(_) => return unavailable(),
    };
    let decision = Decision::new(
        APPROVED,
        person(&user),
        &current.proposer,
        decided.note,
        now(),
    );
    let claimed = match store.claim(num, decided.revision, &decision).await {
        Ok(Ok(proposal)) => proposal,
        Ok(Err(refusal)) => return refusal_response(refusal),
        Err(_) => return unavailable(),
    };
    let (state, launch) = launch_record(gate_client.launch(&call_of(&claimed)).await);
    match store.record_launch(num, state, &launch, &now()).await {
        Ok(true) => reply(&store, num, StatusCode::OK).await,
        _ => unavailable(),
    }
}

/// `POST /v1/proposals/{id}/deny` (D52). Needs no gate: a denial launches nothing.
pub async fn deny_proposal(
    State(app): State<SharedState>,
    session: Session,
    Path(id): Path<String>,
    body: Result<Json<DecisionBody>, JsonRejection>,
) -> Response {
    let user = match admin(&session).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(num) = parse_id(&id) else {
        return not_found();
    };
    let decided = match decision_body(body) {
        Ok(decided) => decided,
        Err(detail) => return bad_request(detail),
    };
    let store = app.prefs.proposal_store();
    let current = match store.get(num).await {
        Ok(Some(proposal)) => proposal,
        Ok(None) => return not_found(),
        Err(_) => return unavailable(),
    };
    let decision = Decision::new(
        DENIED,
        person(&user),
        &current.proposer,
        decided.note,
        now(),
    );
    match store.deny(num, decided.revision, &decision).await {
        Ok(Ok(proposal)) => (StatusCode::OK, Json(proposal)).into_response(),
        Ok(Err(refusal)) => refusal_response(refusal),
        Err(_) => unavailable(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::server::prefs::Prefs;
    use crate::ui::proposals_api as page;

    const T0: &str = "2026-11-09T10:00:00Z";
    const T1: &str = "2026-11-09T10:05:00Z";
    /// `T1` in Unix seconds. `the_test_clock_matches_the_stamps_the_tests_write` pins it to `T1`.
    const T1_UNIX: i64 = 1794218700;

    fn ada() -> Person {
        Person {
            sub: "user-1".into(),
            name: "Ada".into(),
        }
    }

    fn grace() -> Person {
        Person {
            sub: "user-2".into(),
            name: "Grace".into(),
        }
    }

    fn new_row(service: &str) -> NewProposal {
        NewProposal {
            kind: "verify".into(),
            service: service.into(),
            template: "Verify Service Health (Dev)".into(),
            agent: AGENT.into(),
            step: "service-validate".into(),
            survey: BTreeMap::from([("target_service".to_string(), format!("{service}_svc"))]),
            check_mode: false,
            note: "before the upgrade".into(),
            proposer: ada(),
        }
    }

    fn allowed(at: &str) -> Preview {
        Preview {
            state: "allowed".into(),
            reason: Some("read-only".into()),
            detail: None,
            at: at.into(),
        }
    }

    async fn store() -> ProposalStore {
        Prefs::in_memory().expect("prefs").proposal_store()
    }

    fn approval(by: Person) -> Decision {
        Decision::new(APPROVED, by, &ada(), None, T1.into())
    }

    #[tokio::test]
    async fn store_keeps_rows_newest_first_and_filters_by_state() {
        let store = store().await;
        let first = store.insert(&new_row("honcho"), T0).await.expect("insert");
        let second = store.insert(&new_row("tududi"), T0).await.expect("insert");
        let third = store.insert(&new_row("n8n"), T0).await.expect("insert");
        assert_eq!((first, second, third), (1, 2, 3));
        let denial = Decision::new(DENIED, grace(), &ada(), None, T1.into());
        store
            .deny(second, 1, &denial)
            .await
            .expect("db")
            .expect("denied");

        let ids = |list: Vec<Proposal>| list.into_iter().map(|p| p.id).collect::<Vec<_>>();
        assert_eq!(
            ids(store.list(ListFilter::All).await.expect("all")),
            ["p3", "p2", "p1"]
        );
        assert_eq!(
            ids(store.list(ListFilter::Awaiting).await.expect("awaiting")),
            ["p3", "p1"]
        );
        assert_eq!(
            ids(store.list(ListFilter::Decided).await.expect("decided")),
            ["p2"]
        );

        let stored = store.get(first).await.expect("get").expect("p1");
        assert_eq!(stored.state, AWAITING);
        assert_eq!(stored.survey["target_service"], "honcho_svc");
        assert_eq!(stored.preview.state, "unavailable");
        assert!(store.get(99).await.expect("get").is_none());
    }

    #[tokio::test]
    async fn a_claim_moves_a_row_once_and_only_from_awaiting_with_an_allowed_preview() {
        let store = store().await;
        let id = store.insert(&new_row("honcho"), T0).await.expect("insert");

        // The placeholder preview is not allowed, so the claim refuses and the row stays awaiting.
        let refused = store
            .claim(id, 1, &approval(grace()))
            .await
            .expect("db")
            .err();
        assert_eq!(refused, Some(Refusal::PreviewNotAllowed));
        assert_eq!(
            store.get(id).await.expect("get").expect("row").state,
            AWAITING
        );

        // An edit moves the row to revision 2 with its values written. The preview stays the placeholder until stored.
        let edited = store
            .begin_edit(id, false, "edited", T1)
            .await
            .expect("db")
            .expect("the edit applies");
        assert_eq!(edited.revision, 2);
        assert!(store.set_preview(id, 2, &allowed(T1)).await.expect("db"));
        let claimed = store
            .claim(id, 2, &approval(grace()))
            .await
            .expect("db")
            .expect("the first claim launches");
        assert_eq!(claimed.note, "edited");
        assert!(!claimed.check_mode);

        let again = store
            .claim(id, 2, &approval(grace()))
            .await
            .expect("db")
            .err();
        assert_eq!(
            again,
            Some(Refusal::NotAwaiting),
            "a second claim launches nothing, and is not stale"
        );
        let denial = Decision::new(DENIED, grace(), &ada(), None, T1.into());
        let late_denial = store.deny(id, 2, &denial).await.expect("db").err();
        assert_eq!(late_denial, Some(Refusal::NotAwaiting));
        assert!(
            store
                .begin_edit(id, true, "late", T1)
                .await
                .expect("db")
                .is_err(),
            "a launching row takes no edit"
        );
        let missing = store
            .claim(99, 1, &approval(grace()))
            .await
            .expect("db")
            .err();
        assert_eq!(missing, Some(Refusal::Missing));

        let launch = Launch::Launched {
            task_id: 4217,
            message: "console p1 service-validate".into(),
            reason: "read-only".into(),
        };
        assert!(store
            .record_launch(id, LAUNCHED, &launch, T1)
            .await
            .expect("db"));
        let done = store.get(id).await.expect("get").expect("row");
        assert_eq!(done.state, LAUNCHED);
        assert_eq!(done.launch, Some(launch));
        let decision = done.decision.expect("the decision is recorded");
        assert_eq!(decision.by.sub, "user-2");
        assert!(!decision.self_approved, "grace did not propose it");
        assert!(
            !store
                .record_launch(
                    id,
                    LAUNCH_FAILED,
                    &Launch::Failed {
                        error: LaunchError {
                            status: 0,
                            code: "x".into(),
                            detail: "x".into(),
                            reason: None
                        }
                    },
                    T1
                )
                .await
                .expect("db"),
            "a launched row takes no second answer"
        );
    }

    #[tokio::test]
    async fn a_row_left_launching_reads_as_launch_failed_and_is_never_claimed_again() {
        let store = store().await;
        let id = store.insert(&new_row("honcho"), T0).await.expect("insert");
        assert!(store.set_preview(id, 1, &allowed(T0)).await.expect("db"));
        store
            .claim(id, 1, &approval(grace()))
            .await
            .expect("db")
            .expect("claimed");
        // No record_launch: the process stopped while the gate was answering. Past the window it reads as interrupted.
        let shown = store
            .get_at(id, T1_UNIX + LAUNCH_WINDOW_SECS + 1)
            .await
            .expect("get")
            .expect("row");
        assert_eq!(shown.state, LAUNCH_FAILED);
        assert_eq!(
            shown.launch,
            Some(Launch::Failed {
                error: LaunchError {
                    status: 0,
                    code: "interrupted".into(),
                    detail: "The launch did not finish; check Active Work before proposing again."
                        .into(),
                    reason: None,
                }
            })
        );
        assert!(
            store
                .claim(id, 1, &approval(grace()))
                .await
                .expect("db")
                .is_err(),
            "a restart must not reopen the launch (D53)"
        );
    }

    /// The window is inclusive: 150 s after the claim the row still reads as in flight, and 151 s after it reads as
    /// interrupted. Mutating the comparison in `read_row` to strict `>` fails the 150 s check.
    #[tokio::test]
    async fn a_launch_reads_as_in_flight_for_150_seconds_after_its_claim() {
        let store = store().await;
        let id = store.insert(&new_row("honcho"), T0).await.expect("insert");
        assert!(store.set_preview(id, 1, &allowed(T0)).await.expect("db"));
        store
            .claim(id, 1, &approval(grace()))
            .await
            .expect("db")
            .expect("claimed");

        let at_claim = store.get_at(id, T1_UNIX).await.expect("get").expect("row");
        assert_eq!(at_claim.state, LAUNCHING);
        assert_eq!(at_claim.launch, None);

        let edge = store
            .get_at(id, T1_UNIX + LAUNCH_WINDOW_SECS)
            .await
            .expect("get")
            .expect("row");
        assert_eq!(
            edge.state, LAUNCHING,
            "150 s after the claim is still in flight"
        );
        assert_eq!(edge.launch, None);

        let late = store
            .get_at(id, T1_UNIX + LAUNCH_WINDOW_SECS + 1)
            .await
            .expect("get")
            .expect("row");
        assert_eq!(
            late.state, LAUNCH_FAILED,
            "151 s after the claim is interrupted"
        );
        assert_eq!(
            late.launch,
            Some(Launch::Failed {
                error: LaunchError {
                    status: 0,
                    code: "interrupted".into(),
                    detail: "The launch did not finish; check Active Work before proposing again."
                        .into(),
                    reason: None,
                }
            })
        );
    }

    #[test]
    fn the_test_clock_matches_the_stamps_the_tests_write() {
        assert_eq!(rfc3339_from_unix(T1_UNIX), T1);
    }

    /// The page reads the JSON the server writes. Every field it mirrors is set to a value that is not its default, so a
    /// field the server renames or drops fails here instead of reading back as the default (D48-D53).
    #[tokio::test]
    async fn the_page_reads_every_field_the_server_writes() {
        let store = store().await;
        let id = store.insert(&new_row("honcho"), T0).await.expect("insert");
        store
            .begin_edit(id, false, "second", T1)
            .await
            .expect("db")
            .expect("edit to revision 2");
        store
            .begin_edit(id, true, "before the honcho upgrade", T1)
            .await
            .expect("db")
            .expect("edit to revision 3");
        assert!(store.set_preview(id, 3, &allowed(T1)).await.expect("db"));
        store
            .claim(id, 3, &approval(ada()))
            .await
            .expect("db")
            .expect("claimed");
        let launch = Launch::Launched {
            task_id: 4217,
            message: "console p1 service-validate".into(),
            reason: "read-only".into(),
        };
        assert!(store
            .record_launch(id, LAUNCHED, &launch, T1)
            .await
            .expect("db"));
        assert!(store
            .add_comment(id, &grace(), T1, "Looks right.")
            .await
            .expect("db"));

        let stored = store.get(id).await.expect("get").expect("row");
        let json = serde_json::to_value(&stored).expect("json");
        let page: page::Proposal =
            serde_json::from_value(json).expect("the page reads the server's JSON");

        assert_eq!(page.id, "p1");
        assert_eq!(page.revision, 3);
        assert_eq!(page.kind, page::Kind::Verify);
        assert_eq!(page.service, "honcho");
        assert_eq!(page.template, "Verify Service Health (Dev)");
        assert_eq!(page.agent, "service-agent");
        assert_eq!(page.step, "service-validate");
        assert!(page.check_mode);
        assert_eq!(page.note.as_deref(), Some("before the honcho upgrade"));
        assert_eq!(page.state, page::State::Launched);
        assert_eq!(page.created_at, T0);
        assert_eq!(page.proposer.name, "Ada");
        let preview = page.preview.expect("preview");
        assert_eq!(preview.state, page::PreviewState::Allowed);
        assert_eq!(preview.reason.as_deref(), Some("read-only"));
        let decision = page.decision.expect("decision");
        assert_eq!(decision.action, page::DecisionAction::Approved);
        assert_eq!(decision.by.name, "Ada");
        assert_eq!(decision.at, T1);
        assert!(decision.self_approved);
        assert_eq!(page.launch.and_then(|launch| launch.task_id), Some(4217));
        assert_eq!(page.comments.len(), 1);
        assert_eq!(page.comments[0].text, "Looks right.");
        assert_eq!(page.comments[0].by.name, "Grace");
    }

    /// A launch in flight reads as `launching` on the page, with no launch object yet.
    #[tokio::test]
    async fn a_launch_in_flight_reads_as_launching_on_the_page() {
        let store = store().await;
        let id = store.insert(&new_row("honcho"), T0).await.expect("insert");
        assert!(store.set_preview(id, 1, &allowed(T0)).await.expect("db"));
        store
            .claim(id, 1, &approval(grace()))
            .await
            .expect("db")
            .expect("claimed");

        let stored = store
            .get_at(id, T1_UNIX + 10)
            .await
            .expect("get")
            .expect("row");
        let json = serde_json::to_value(&stored).expect("json");
        let page: page::Proposal =
            serde_json::from_value(json).expect("the page reads the server's JSON");
        assert_eq!(page.state, page::State::Launching);
        assert_eq!(page.revision, 1);
        assert!(page.launch.is_none());
        assert_eq!(
            page.decision.map(|decision| decision.by.name),
            Some("Grace".to_string())
        );
    }

    #[tokio::test]
    async fn an_edit_moves_the_revision_and_holds_approval_until_its_preview_is_stored() {
        let store = store().await;
        let id = store.insert(&new_row("honcho"), T0).await.expect("insert");
        assert!(store.set_preview(id, 1, &allowed(T0)).await.expect("db"));

        let edited = store
            .begin_edit(id, true, "second", T1)
            .await
            .expect("db")
            .expect("the edit applies");
        assert_eq!(edited.revision, 2);
        assert_eq!(
            edited.preview.state, "unavailable",
            "the old verdict goes with the old values"
        );
        // An approval during the decide is refused, not launched on the verdict of the values it replaced.
        assert_eq!(
            store
                .claim(id, 2, &approval(grace()))
                .await
                .expect("db")
                .err(),
            Some(Refusal::PreviewNotAllowed)
        );
        // A decide for revision 1 that finishes late is dropped. Revision 2's preview is stored.
        assert!(!store.set_preview(id, 1, &allowed(T1)).await.expect("db"));
        assert!(store.set_preview(id, 2, &allowed(T1)).await.expect("db"));
        assert_eq!(
            store
                .claim(id, 1, &approval(grace()))
                .await
                .expect("db")
                .err(),
            Some(Refusal::Stale),
            "an approver who reviewed revision 1 is refused"
        );
        assert_eq!(
            store.get(id).await.expect("get").expect("row").state,
            AWAITING,
            "a stale claim moves nothing"
        );
        assert!(store
            .claim(id, 2, &approval(grace()))
            .await
            .expect("db")
            .is_ok());
    }

    #[tokio::test]
    async fn a_denial_at_an_old_revision_is_stale_and_moves_nothing() {
        let store = store().await;
        let id = store.insert(&new_row("honcho"), T0).await.expect("insert");
        store
            .begin_edit(id, false, "again", T1)
            .await
            .expect("db")
            .expect("the edit applies");
        let denial = Decision::new(DENIED, grace(), &ada(), None, T1.into());
        assert_eq!(
            store.deny(id, 1, &denial).await.expect("db").err(),
            Some(Refusal::Stale)
        );
        assert_eq!(
            store.get(id).await.expect("get").expect("row").state,
            AWAITING
        );
        assert!(store.deny(id, 2, &denial).await.expect("db").is_ok());
        assert_eq!(
            store.get(id).await.expect("get").expect("row").state,
            DENIED
        );
    }

    #[tokio::test]
    async fn comments_keep_their_order_and_leave_the_state_alone() {
        let store = store().await;
        let id = store.insert(&new_row("honcho"), T0).await.expect("insert");
        assert!(store
            .add_comment(id, &grace(), T1, "Looks right.")
            .await
            .expect("db"));
        assert!(store
            .add_comment(id, &ada(), T1, "Thanks.")
            .await
            .expect("db"));
        assert!(!store
            .add_comment(99, &ada(), T1, "nobody")
            .await
            .expect("db"));

        let shown = store.get(id).await.expect("get").expect("row");
        let texts: Vec<&str> = shown.comments.iter().map(|c| c.text.as_str()).collect();
        assert_eq!(texts, ["Looks right.", "Thanks."]);
        assert_eq!(shown.comments[0].by.name, "Grace");
        assert_eq!(shown.state, AWAITING);
        assert_eq!(shown.updated_at, T0, "a comment does not move updated_at");
    }

    #[test]
    fn preview_keeps_each_gate_answer_in_its_stored_shape() {
        let shown = |verdict: GateVerdict| Preview::from_verdict(verdict, T1.to_string());
        assert_eq!(
            shown(GateVerdict::Allowed {
                reason: "ok".into()
            }),
            Preview {
                state: "allowed".into(),
                reason: Some("ok".into()),
                detail: None,
                at: T1.into(),
            }
        );
        assert_eq!(
            shown(GateVerdict::Denied {
                reason: "proposal required".into()
            }),
            Preview {
                state: "denied".into(),
                reason: Some("proposal required".into()),
                detail: None,
                at: T1.into(),
            }
        );
        assert_eq!(
            shown(GateVerdict::Refused {
                code: "bad_request".into(),
                detail: "dry_run must be true or false".into(),
            }),
            Preview {
                state: "refused".into(),
                reason: None,
                detail: Some("bad_request: dry_run must be true or false".into()),
                at: T1.into(),
            }
        );
        assert_eq!(
            shown(GateVerdict::Unavailable {
                detail: "the launch gate did not answer".into()
            }),
            Preview {
                state: "unavailable".into(),
                reason: None,
                detail: Some("the launch gate did not answer".into()),
                at: T1.into(),
            }
        );
    }

    #[test]
    fn each_launch_outcome_is_recorded_under_its_own_state() {
        let (state, launch) = launch_record(LaunchOutcome::Denied {
            reason: "proposal required".into(),
        });
        assert_eq!(state, LAUNCH_FAILED);
        assert_eq!(
            launch,
            Launch::Failed {
                error: LaunchError {
                    status: 403,
                    code: "opa_denied".into(),
                    detail: "OPA denied the launch".into(),
                    reason: Some("proposal required".into()),
                }
            }
        );

        let (state, launch) = launch_record(LaunchOutcome::Refused {
            status: 409,
            code: "already_running".into(),
            detail: "template X already has a running task: 9".into(),
        });
        assert_eq!(state, LAUNCH_FAILED);
        assert_eq!(
            launch,
            Launch::Failed {
                error: LaunchError {
                    status: 409,
                    code: "already_running".into(),
                    detail: "template X already has a running task: 9".into(),
                    reason: None,
                }
            }
        );

        let (state, launch) = launch_record(LaunchOutcome::Unavailable {
            detail: "no answer".into(),
        });
        assert_eq!(state, LAUNCH_FAILED);
        assert_eq!(
            launch,
            Launch::Failed {
                error: LaunchError {
                    status: 0,
                    code: "gate_unavailable".into(),
                    detail: "no answer".into(),
                    reason: None,
                }
            }
        );

        let (state, launch) = launch_record(LaunchOutcome::Launched {
            task_id: 4217,
            message: "console p1 service-validate".into(),
            reason: "read-only".into(),
        });
        assert_eq!(state, LAUNCHED);
        assert_eq!(
            launch,
            Launch::Launched {
                task_id: 4217,
                message: "console p1 service-validate".into(),
                reason: "read-only".into(),
            }
        );
    }

    #[test]
    fn service_names_follow_the_contract_pattern() {
        let long = "a".repeat(63);
        let too_long = "a".repeat(64);
        for ok in ["honcho", "n8n", "a", "0-x", long.as_str()] {
            assert!(valid_service(ok), "{ok}");
        }
        for bad in [
            "",
            "-a",
            "Honcho",
            "a_b",
            "a.b",
            "a b",
            "é",
            too_long.as_str(),
        ] {
            assert!(!valid_service(bad), "{bad}");
        }
    }

    #[test]
    fn the_two_first_kinds_name_their_template_and_registry_step() {
        assert_eq!(
            kind_spec("assess"),
            Some(("Snapshot Service Assessment", "service-assess"))
        );
        assert_eq!(
            kind_spec("verify"),
            Some(("Verify Service Health", "service-validate"))
        );
        assert_eq!(
            kind_spec("request"),
            None,
            "request is not in the first slice (D48)"
        );
    }

    #[test]
    fn a_person_is_named_by_display_name_then_username_then_email_then_subject() {
        let user = |name: Option<&str>, username: Option<&str>, email: Option<&str>| CurrentUser {
            sub: "s".into(),
            email: email.map(str::to_string),
            name: name.map(str::to_string),
            username: username.map(str::to_string),
            groups: Vec::new(),
        };
        assert_eq!(
            person(&user(Some("Ada"), Some("ada"), Some("a@x"))).name,
            "Ada"
        );
        assert_eq!(person(&user(None, Some("ada"), Some("a@x"))).name, "ada");
        assert_eq!(person(&user(Some("  "), None, Some("a@x"))).name, "a@x");
        assert_eq!(person(&user(None, None, None)).name, "s");
    }

    #[test]
    fn proposal_ids_are_p_then_digits_and_nothing_else() {
        assert_eq!(parse_id("p42"), Some(42));
        for bad in ["42", "p", "p0", "p-1", "p+5", "px", "P42", "p4 2", "pp4"] {
            assert_eq!(parse_id(bad), None, "{bad}");
        }
    }

    #[test]
    fn only_an_approval_by_the_proposer_is_self_approved() {
        assert!(Decision::new(APPROVED, ada(), &ada(), None, T1.into()).self_approved);
        assert!(!Decision::new(APPROVED, grace(), &ada(), None, T1.into()).self_approved);
        assert!(
            !Decision::new(DENIED, ada(), &ada(), None, T1.into()).self_approved,
            "a withdrawal is not an approval"
        );
    }
}
