//! The proposals as the browser reads and writes them (plan 17, D48-D53): the `/v1/proposals` shapes, the
//! labels and states they map to, and the writes. Every struct carries `#[serde(default)]`, and only the
//! fields the page reads are mirrored: serde ignores the rest. An unknown state, kind or preview word reads as
//! `Unknown`, which the page shows as a failure, never as a success. Writes go through `api::request`, which
//! sends `Content-Type: application/json` with every body; the server refuses any other type (D53).

use serde::Deserialize;
use serde_json::{json, Value};

use super::api::{self, Reply};
use super::work::Tone;

pub const PROPOSALS_PATH: &str = "/v1/proposals";
/// The waiting proposals, newest first. The Dashboard polls this one.
pub const AWAITING_PATH: &str = "/v1/proposals?state=awaiting";
/// The decided proposals (denied, launched, launch_failed), newest first.
pub const DECIDED_PATH: &str = "/v1/proposals?state=decided";
/// How many decided proposals the Dashboard lists.
pub const RECENT_DECIDED: usize = 20;
/// The sentence a 403 shows. Proposals are for platform admins (D25).
pub const ADMIN_ONLY: &str = "Platform admins only.";
/// The sentence a failed write shows when the server sent none.
pub const GENERIC_FAILURE: &str = "The request did not complete. Try again.";
/// The sentence a stale approval or denial shows: the proposal was edited after this page loaded it (D53).
pub const STALE: &str = "This proposal changed since you loaded it. Review it again.";

/// The action a proposal takes. `assess` runs Snapshot Service Assessment; `verify` runs Verify Service Health.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Kind {
    Assess,
    Verify,
    #[default]
    #[serde(other)]
    Unknown,
}

impl Kind {
    /// The word the create route takes.
    pub fn wire(self) -> &'static str {
        match self {
            Kind::Assess => "assess",
            Kind::Verify => "verify",
            Kind::Unknown => "unknown",
        }
    }
}

/// Where a proposal stands. `Awaiting` is the only state a decision can change. `Launching` is a launch in flight: the
/// gate has not answered, so nothing can be decided on it.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum State {
    Awaiting,
    Launching,
    Denied,
    Launched,
    LaunchFailed,
    #[default]
    #[serde(other)]
    Unknown,
}

/// The gate's answer to "would this run?", as the preview last ran it.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum PreviewState {
    Allowed,
    Denied,
    Refused,
    Unavailable,
    #[default]
    #[serde(other)]
    Unknown,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum DecisionAction {
    Approved,
    Denied,
    #[default]
    #[serde(other)]
    Unknown,
}

/// A person the console names: an Authentik subject and a display name.
#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct Person {
    pub sub: String,
    pub name: String,
}

/// The OPA preview, as the gate last computed it.
#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct Preview {
    pub state: PreviewState,
    pub reason: Option<String>,
    pub detail: Option<String>,
}

/// Who decided a proposal, and how.
#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct Decision {
    pub action: DecisionAction,
    pub by: Person,
    pub at: String,
    pub note: Option<String>,
    pub self_approved: bool,
}

/// The gate's refusal at launch. `detail` is a sentence; `reason` is OPA's when OPA said no.
#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct LaunchError {
    pub detail: String,
    pub reason: Option<String>,
}

/// The launch record. Either the task the gate created, or the error it answered with.
#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct Launch {
    pub task_id: Option<i64>,
    pub error: Option<LaunchError>,
}

#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct Comment {
    pub by: Person,
    pub at: String,
    pub text: String,
}

/// One proposal, as `GET /v1/proposals` and every write return it.
#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct Proposal {
    pub id: String,
    /// The revision the page reviewed. An approval or denial sends it back, and the server refuses a stale one (D53).
    pub revision: u64,
    pub kind: Kind,
    pub service: String,
    pub template: String,
    pub agent: String,
    pub step: String,
    pub check_mode: bool,
    pub note: Option<String>,
    pub state: State,
    pub proposer: Person,
    pub created_at: String,
    pub preview: Option<Preview>,
    pub decision: Option<Decision>,
    pub launch: Option<Launch>,
    pub comments: Vec<Comment>,
}

/// `GET /v1/proposals?state=...`.
#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct ProposalsBody {
    pub proposals: Vec<Proposal>,
}

/// What an action reports: a sentence, whether it went well, and the launched task when there is one.
#[derive(Debug, Clone, PartialEq)]
pub struct Outcome {
    pub text: String,
    pub ok: bool,
    pub task_id: Option<i64>,
}

/// The icon, tone and word for a proposal's state. The shape and the word carry it; the tone repeats it.
pub fn state_look(state: State) -> (&'static str, Tone, &'static str) {
    match state {
        State::Awaiting => ("loader-circle", Tone::Run, "Awaiting approval"),
        State::Launching => ("loader-circle", Tone::Run, "Launching"),
        State::Denied => ("x", Tone::Idle, "Denied"),
        State::Launched => ("circle-check", Tone::Ok, "Launched"),
        State::LaunchFailed => ("octagon-x", Tone::Crit, "Launch failed"),
        State::Unknown => ("triangle-alert", Tone::Warn, "Unknown state"),
    }
}

/// The label of an action.
pub fn kind_label(kind: Kind) -> &'static str {
    match kind {
        Kind::Assess => "Assess resource",
        Kind::Verify => "Verify health",
        Kind::Unknown => "Unknown action",
    }
}

/// The icon, tone and word for the gate's preview. `None` means the gate has not answered yet.
pub fn preview_look(preview: Option<&Preview>) -> (&'static str, Tone, &'static str) {
    match preview.map(|p| p.state) {
        Some(PreviewState::Allowed) => ("circle-check", Tone::Ok, "OPA allowed"),
        Some(PreviewState::Denied) => ("octagon-x", Tone::Crit, "OPA denied"),
        Some(PreviewState::Refused) => ("octagon-x", Tone::Crit, "Gate refused"),
        Some(PreviewState::Unavailable) => ("triangle-alert", Tone::Warn, "Gate unavailable"),
        Some(PreviewState::Unknown) => ("triangle-alert", Tone::Warn, "Unknown policy result"),
        None => ("circle-dashed", Tone::Idle, "Not checked yet"),
    }
}

/// The gate's reason for its answer, or its detail when it gave no reason. `None` when it gave neither.
pub fn preview_reason(preview: Option<&Preview>) -> Option<String> {
    preview
        .and_then(|p| p.reason.clone().or_else(|| p.detail.clone()))
        .filter(|text| !text.is_empty())
}

/// Whether Approve is offered: the proposal waits and the gate's last preview allowed it.
pub fn can_approve(proposal: &Proposal) -> bool {
    proposal.state == State::Awaiting
        && proposal
            .preview
            .as_ref()
            .is_some_and(|p| p.state == PreviewState::Allowed)
}

/// Why Approve is off, as the sentence the page shows beside it. `None` when Approve is on.
pub fn approve_refusal(proposal: &Proposal) -> Option<String> {
    if can_approve(proposal) {
        return None;
    }
    if proposal.state != State::Awaiting {
        return Some("This proposal is no longer awaiting approval.".to_string());
    }
    let reason = preview_reason(proposal.preview.as_ref())
        .unwrap_or_else(|| "no reason was given".to_string());
    match proposal.preview.as_ref().map(|p| p.state) {
        Some(PreviewState::Denied) => Some(format!("OPA did not allow this proposal: {reason}")),
        Some(PreviewState::Refused) => {
            Some(format!("The launch gate refused this proposal: {reason}"))
        }
        _ => Some("The policy check has not answered.".to_string()),
    }
}

/// What an approve or deny answer says. A launch error is shown with its detail and reason. A launch is shown
/// with its task. A denial launched nothing.
pub fn outcome_for(proposal: &Proposal) -> Outcome {
    if let Some(error) = proposal.launch.as_ref().and_then(|l| l.error.as_ref()) {
        let detail = if error.detail.is_empty() {
            "Launch failed.".to_string()
        } else {
            format!("Launch failed: {}", error.detail)
        };
        let text = match error.reason.as_deref().filter(|r| !r.is_empty()) {
            Some(reason) => format!("{detail} Reason: {reason}"),
            None => detail,
        };
        return Outcome {
            text,
            ok: false,
            task_id: None,
        };
    }
    if let Some(task_id) = proposal.launch.as_ref().and_then(|l| l.task_id) {
        return Outcome {
            text: format!("Launched as task {task_id}."),
            ok: true,
            task_id: Some(task_id),
        };
    }
    match proposal.state {
        State::Denied => Outcome {
            text: "Denied. Nothing ran.".to_string(),
            ok: true,
            task_id: None,
        },
        _ => Outcome {
            text: "No launch is recorded for this proposal.".to_string(),
            ok: false,
            task_id: None,
        },
    }
}

/// What a plan edit says: the save worked, and the gate ran its check again.
pub fn saved_outcome(proposal: &Proposal) -> Outcome {
    let (_, _, word) = preview_look(proposal.preview.as_ref());
    Outcome {
        text: format!(
            "{} saved. The policy check ran again: {}.",
            proposal.id, word
        ),
        ok: true,
        task_id: None,
    }
}

/// What a comment says: it was added.
pub fn comment_outcome(proposal: &Proposal) -> Outcome {
    Outcome {
        text: format!("Comment added to {}.", proposal.id),
        ok: true,
        task_id: None,
    }
}

/// The decided proposals the Dashboard lists: the first `RECENT_DECIDED`, in the server's order.
pub fn recent_decided(proposals: &[Proposal]) -> Vec<Proposal> {
    proposals.iter().take(RECENT_DECIDED).cloned().collect()
}

/// A comment's text with its surrounding space removed. `None` when nothing is left to send.
pub fn comment_text(raw: &str) -> Option<String> {
    let text = raw.trim();
    (!text.is_empty()).then(|| text.to_string())
}

/// The sentence a failed write shows. A 403 is the admin-only sentence, and a stale 409 is `STALE`. Otherwise it is the
/// server's `detail`, or the generic one when the answer carries none.
pub fn error_text(reply: &Reply) -> String {
    if reply.status == 403 {
        return ADMIN_ONLY.to_string();
    }
    if reply.status == 409 && reply.body["error"].as_str() == Some("stale") {
        return STALE.to_string();
    }
    reply.body["detail"]
        .as_str()
        .filter(|detail| !detail.is_empty())
        .map_or_else(|| GENERIC_FAILURE.to_string(), str::to_string)
}

/// One proposal's path. The id is percent-encoded as one segment.
pub fn proposal_path(id: &str) -> String {
    format!("{PROPOSALS_PATH}/{}", api::segment(id))
}

pub fn comments_path(id: &str) -> String {
    format!("{}/comments", proposal_path(id))
}

pub fn approve_path(id: &str) -> String {
    format!("{}/approve", proposal_path(id))
}

pub fn deny_path(id: &str) -> String {
    format!("{}/deny", proposal_path(id))
}

/// The create body. `note` goes as sent, trimmed; an empty note is an empty string.
pub fn create_body(kind: Kind, service: &str, check_mode: bool, note: &str) -> Value {
    json!({
        "kind": kind.wire(),
        "service": service,
        "check_mode": check_mode,
        "note": note.trim(),
    })
}

/// The edit body: the check mode and the note.
pub fn edit_body(check_mode: bool, note: &str) -> Value {
    json!({ "check_mode": check_mode, "note": note.trim() })
}

/// The comment body. The caller has already run `comment_text`.
pub fn comment_body(text: &str) -> Value {
    json!({ "text": text })
}

/// Approve sends no note and the revision the page shows. A null note is still a body, so the content type is always JSON.
pub fn approve_body(revision: u64) -> Value {
    json!({ "note": null, "revision": revision })
}

/// The denial body. A blank note is sent as null, which the server reads as no note. It names the revision too.
pub fn deny_body(note: &str, revision: u64) -> Value {
    let note = note.trim();
    json!({ "note": (!note.is_empty()).then_some(note), "revision": revision })
}

/// Creates a proposal.
pub async fn create(kind: Kind, service: &str, check_mode: bool, note: &str) -> Reply {
    api::request(
        "POST",
        PROPOSALS_PATH,
        Some(create_body(kind, service, check_mode, note)),
    )
    .await
}

/// Saves the check mode and note of a waiting proposal. The gate checks it again.
pub async fn save(id: &str, check_mode: bool, note: &str) -> Reply {
    api::request("PUT", &proposal_path(id), Some(edit_body(check_mode, note))).await
}

/// Adds a comment. The caller has already run `comment_text`.
pub async fn comment(id: &str, text: &str) -> Reply {
    api::request("POST", &comments_path(id), Some(comment_body(text))).await
}

/// Approves the revision the page shows. The gate decides again, and a launch follows if it allows it.
pub async fn approve(id: &str, revision: u64) -> Reply {
    api::request("POST", &approve_path(id), Some(approve_body(revision))).await
}

/// Denies the revision the page shows. Nothing runs.
pub async fn deny(id: &str, note: &str, revision: u64) -> Reply {
    api::request("POST", &deny_path(id), Some(deny_body(note, revision))).await
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn reply(status: u16, body: Value) -> Reply {
        Reply { status, body }
    }

    /// A waiting proposal whose preview is `state`, with `reason` when given.
    fn waiting(state: PreviewState, reason: Option<&str>) -> Proposal {
        Proposal {
            id: "p42".into(),
            kind: Kind::Assess,
            state: State::Awaiting,
            preview: Some(Preview {
                state,
                reason: reason.map(str::to_string),
                detail: None,
            }),
            ..Proposal::default()
        }
    }

    /// The contract example from the console brief, with the fields the page does not read included.
    fn contract_example() -> Value {
        json!({
            "id": "p42",
            "revision": 3,
            "kind": "verify",
            "service": "honcho",
            "template": "Verify Service Health (Dev)",
            "agent": "service-agent",
            "step": "service-validate",
            "survey": {"target_service": "honcho_svc"},
            "check_mode": false,
            "note": "Before the honcho upgrade",
            "state": "awaiting",
            "proposer": {"sub": "abc", "name": "Ada"},
            "created_at": "2026-11-08T10:00:00Z",
            "updated_at": "2026-11-08T10:05:00Z",
            "preview": {"state": "allowed", "reason": null, "detail": null, "at": "2026-11-08T10:05:00Z"},
            "decision": null,
            "launch": null,
            "comments": [{"by": {"sub": "def", "name": "Grace"}, "at": "2026-11-08T10:06:00Z", "text": "Looks right."}]
        })
    }

    #[test]
    fn a_contract_example_reads_every_field_the_page_uses() {
        let p: Proposal = serde_json::from_value(contract_example()).expect("proposal");
        assert_eq!(p.id, "p42");
        assert_eq!(p.revision, 3);
        assert_eq!(p.kind, Kind::Verify);
        assert_eq!(p.service, "honcho");
        assert_eq!(p.template, "Verify Service Health (Dev)");
        assert_eq!(p.agent, "service-agent");
        assert_eq!(p.step, "service-validate");
        assert_eq!(p.note.as_deref(), Some("Before the honcho upgrade"));
        assert_eq!(p.state, State::Awaiting);
        assert_eq!(p.proposer.name, "Ada");
        assert_eq!(
            p.preview.as_ref().map(|x| x.state),
            Some(PreviewState::Allowed)
        );
        assert_eq!(p.decision, None);
        assert_eq!(p.launch, None);
        assert_eq!(p.comments[0].text, "Looks right.");
    }

    #[test]
    fn each_state_word_reads_as_its_state_and_an_unknown_word_is_unknown() {
        let read = |word: &str| {
            let value = json!({"id": "x", "state": word});
            serde_json::from_value::<Proposal>(value)
                .expect("proposal")
                .state
        };
        assert_eq!(read("awaiting"), State::Awaiting);
        assert_eq!(read("launching"), State::Launching);
        assert_eq!(read("denied"), State::Denied);
        assert_eq!(read("launched"), State::Launched);
        assert_eq!(read("launch_failed"), State::LaunchFailed);
        assert_eq!(read("cancelled"), State::Unknown);
    }

    #[test]
    fn each_preview_word_reads_as_its_state_and_an_unknown_word_is_unknown() {
        let read = |word: &str| {
            let value = json!({"preview": {"state": word}});
            serde_json::from_value::<Proposal>(value)
                .expect("proposal")
                .preview
                .map(|p| p.state)
        };
        assert_eq!(read("allowed"), Some(PreviewState::Allowed));
        assert_eq!(read("denied"), Some(PreviewState::Denied));
        assert_eq!(read("refused"), Some(PreviewState::Refused));
        assert_eq!(read("unavailable"), Some(PreviewState::Unavailable));
        assert_eq!(read("maybe"), Some(PreviewState::Unknown));
    }

    #[test]
    fn a_launch_error_and_a_launch_record_both_read() {
        let failed = json!({"launch": {"error": {"status": 409, "code": "preview_not_allowed", "detail": "OPA said no.", "reason": "tag not allowed"}}});
        let p: Proposal = serde_json::from_value(failed).expect("proposal");
        let error = p.launch.and_then(|l| l.error).expect("error");
        assert_eq!(error.detail, "OPA said no.");
        assert_eq!(error.reason.as_deref(), Some("tag not allowed"));

        let launched = json!({"launch": {"task_id": 7, "message": "started", "reason": "ok"}});
        let p: Proposal = serde_json::from_value(launched).expect("proposal");
        assert_eq!(p.launch.and_then(|l| l.task_id), Some(7));
    }

    #[test]
    fn a_self_approved_decision_reads_its_flag_and_action() {
        let value = json!({"state": "launched", "decision": {"action": "approved", "by": {"sub": "a", "name": "Ada"}, "at": "t", "note": null, "self_approved": true}});
        let p: Proposal = serde_json::from_value(value).expect("proposal");
        let decision = p.decision.expect("decision");
        assert!(decision.self_approved);
        assert_eq!(decision.action, DecisionAction::Approved);
        assert_eq!(decision.by.name, "Ada");
    }

    #[test]
    fn kind_and_state_words_have_labels_and_icons() {
        assert_eq!(kind_label(Kind::Assess), "Assess resource");
        assert_eq!(kind_label(Kind::Verify), "Verify health");
        assert_eq!(state_look(State::Awaiting).2, "Awaiting approval");
        assert_eq!(state_look(State::Denied).2, "Denied");
        assert_eq!(state_look(State::Launched).2, "Launched");
        assert_eq!(state_look(State::LaunchFailed).2, "Launch failed");
        assert_eq!(state_look(State::Launched).0, "circle-check");
    }

    #[test]
    fn preview_words_are_the_four_gate_answers_and_a_missing_one() {
        let word = |state: PreviewState| {
            preview_look(Some(&Preview {
                state,
                ..Preview::default()
            }))
            .2
        };
        assert_eq!(word(PreviewState::Allowed), "OPA allowed");
        assert_eq!(word(PreviewState::Denied), "OPA denied");
        assert_eq!(word(PreviewState::Refused), "Gate refused");
        assert_eq!(word(PreviewState::Unavailable), "Gate unavailable");
        assert_eq!(preview_look(None).2, "Not checked yet");
    }

    #[test]
    fn a_preview_reason_is_the_reason_then_the_detail() {
        let both = Preview {
            state: PreviewState::Denied,
            reason: Some("tag not allowed".into()),
            detail: Some("ignored".into()),
        };
        assert_eq!(
            preview_reason(Some(&both)).as_deref(),
            Some("tag not allowed")
        );
        let detail_only = Preview {
            state: PreviewState::Refused,
            reason: None,
            detail: Some("not on the allowlist".into()),
        };
        assert_eq!(
            preview_reason(Some(&detail_only)).as_deref(),
            Some("not on the allowlist")
        );
        assert_eq!(preview_reason(None), None);
    }

    #[test]
    fn only_a_waiting_proposal_with_an_allowed_preview_can_be_approved() {
        assert!(can_approve(&waiting(PreviewState::Allowed, None)));
        assert!(!can_approve(&waiting(PreviewState::Denied, Some("x"))));
        assert!(!can_approve(&waiting(PreviewState::Refused, None)));
        assert!(!can_approve(&waiting(PreviewState::Unavailable, None)));
        assert!(!can_approve(&Proposal {
            state: State::Awaiting,
            preview: None,
            ..Proposal::default()
        }));
        let decided = Proposal {
            state: State::Denied,
            ..waiting(PreviewState::Allowed, None)
        };
        assert!(!can_approve(&decided));
    }

    #[test]
    fn approve_is_refused_with_opas_own_reason_when_opa_denies() {
        let p = waiting(PreviewState::Denied, Some("tag not on the allowlist"));
        assert_eq!(
            approve_refusal(&p).as_deref(),
            Some("OPA did not allow this proposal: tag not on the allowlist")
        );
    }

    #[test]
    fn approve_is_refused_as_unanswered_when_there_is_no_preview() {
        let no_preview = Proposal {
            state: State::Awaiting,
            ..Proposal::default()
        };
        assert_eq!(
            approve_refusal(&no_preview).as_deref(),
            Some("The policy check has not answered.")
        );
        assert_eq!(
            approve_refusal(&waiting(PreviewState::Unavailable, None)).as_deref(),
            Some("The policy check has not answered.")
        );
    }

    #[test]
    fn approve_is_refused_as_the_gate_refusal_when_the_gate_refused() {
        let p = waiting(PreviewState::Refused, Some("service not on the allowlist"));
        assert_eq!(
            approve_refusal(&p).as_deref(),
            Some("The launch gate refused this proposal: service not on the allowlist")
        );
    }

    #[test]
    fn an_allowed_waiting_proposal_has_no_refusal() {
        assert_eq!(approve_refusal(&waiting(PreviewState::Allowed, None)), None);
    }

    #[test]
    fn a_decided_proposal_cannot_be_approved_and_says_so() {
        let decided = Proposal {
            state: State::Launched,
            ..waiting(PreviewState::Allowed, None)
        };
        assert_eq!(
            approve_refusal(&decided).as_deref(),
            Some("This proposal is no longer awaiting approval.")
        );
    }

    #[test]
    fn a_launch_error_is_shown_with_its_detail_and_reason() {
        let p = Proposal {
            state: State::LaunchFailed,
            launch: Some(Launch {
                task_id: None,
                error: Some(LaunchError {
                    detail: "The gate refused the launch.".into(),
                    reason: Some("branch not pinned".into()),
                }),
            }),
            ..Proposal::default()
        };
        let outcome = outcome_for(&p);
        assert!(!outcome.ok);
        assert_eq!(outcome.task_id, None);
        assert_eq!(
            outcome.text,
            "Launch failed: The gate refused the launch. Reason: branch not pinned"
        );
    }

    #[test]
    fn a_launched_proposal_names_its_task() {
        let p = Proposal {
            state: State::Launched,
            launch: Some(Launch {
                task_id: Some(42),
                error: None,
            }),
            ..Proposal::default()
        };
        let outcome = outcome_for(&p);
        assert!(outcome.ok);
        assert_eq!(outcome.task_id, Some(42));
        assert_eq!(outcome.text, "Launched as task 42.");
    }

    #[test]
    fn a_denial_says_nothing_ran() {
        let p = Proposal {
            state: State::Denied,
            ..Proposal::default()
        };
        let outcome = outcome_for(&p);
        assert!(outcome.ok);
        assert_eq!(outcome.text, "Denied. Nothing ran.");
    }

    #[test]
    fn a_saved_plan_says_the_gate_checked_it_again() {
        let p = waiting(PreviewState::Denied, Some("x"));
        assert_eq!(
            saved_outcome(&p).text,
            "p42 saved. The policy check ran again: OPA denied."
        );
    }

    #[test]
    fn a_comment_is_trimmed_and_an_empty_one_is_refused() {
        assert_eq!(
            comment_text("  Looks right.  ").as_deref(),
            Some("Looks right.")
        );
        assert_eq!(comment_text("   "), None);
        assert_eq!(comment_text(""), None);
    }

    #[test]
    fn the_decided_list_keeps_the_first_twenty_in_order() {
        let many: Vec<Proposal> = (0..25)
            .map(|i| Proposal {
                id: format!("p{i}"),
                ..Proposal::default()
            })
            .collect();
        let shown = recent_decided(&many);
        assert_eq!(shown.len(), RECENT_DECIDED);
        assert_eq!(shown[0].id, "p0");
        assert_eq!(shown[19].id, "p19");
    }

    #[test]
    fn a_forbidden_write_says_platform_admins_only() {
        assert_eq!(
            error_text(&reply(
                403,
                json!({"error": "forbidden", "detail": "not allowed"})
            )),
            ADMIN_ONLY
        );
    }

    #[test]
    fn a_failed_write_shows_the_servers_sentence_or_the_generic_one() {
        assert_eq!(
            error_text(&reply(
                409,
                json!({"error": "not_awaiting", "detail": "Already decided."})
            )),
            "Already decided."
        );
        assert_eq!(error_text(&reply(500, Value::Null)), GENERIC_FAILURE);
        assert_eq!(
            error_text(&reply(422, json!({"detail": ""}))),
            GENERIC_FAILURE
        );
    }

    #[test]
    fn a_proposal_id_is_one_percent_encoded_segment() {
        assert_eq!(proposal_path("p42"), "/v1/proposals/p42");
        assert_eq!(comments_path("p42"), "/v1/proposals/p42/comments");
        assert_eq!(approve_path("p42"), "/v1/proposals/p42/approve");
        assert_eq!(deny_path("p42"), "/v1/proposals/p42/deny");
        assert_eq!(proposal_path("a/b c"), "/v1/proposals/a%2Fb%20c");
    }

    #[test]
    fn every_write_body_is_an_object_so_the_content_type_is_always_json() {
        assert!(create_body(Kind::Assess, "honcho", true, "why").is_object());
        assert!(edit_body(false, "").is_object());
        assert!(comment_body("hi").is_object());
        assert!(approve_body(1).is_object());
        assert!(deny_body("", 1).is_object());
    }

    #[test]
    fn the_create_body_carries_the_kind_word_service_mode_and_trimmed_note() {
        let body = create_body(Kind::Verify, "honcho", true, "  before upgrade  ");
        assert_eq!(body["kind"], "verify");
        assert_eq!(body["service"], "honcho");
        assert_eq!(body["check_mode"], true);
        assert_eq!(body["note"], "before upgrade");
    }

    #[test]
    fn approve_sends_a_null_note_and_the_revision_and_a_blank_denial_note_is_null() {
        assert_eq!(approve_body(3), json!({"note": null, "revision": 3}));
        assert_eq!(deny_body("   ", 3), json!({"note": null, "revision": 3}));
        assert_eq!(
            deny_body(" not now ", 4),
            json!({"note": "not now", "revision": 4})
        );
    }

    #[test]
    fn a_stale_write_says_the_proposal_changed_since_it_was_loaded() {
        let stale = reply(
            409,
            json!({"error": "stale", "detail": "The proposal changed since you loaded it. Review it again."}),
        );
        assert_eq!(
            error_text(&stale),
            "This proposal changed since you loaded it. Review it again."
        );
        assert_eq!(
            error_text(&reply(
                409,
                json!({"error": "not_awaiting", "detail": "Already decided."})
            )),
            "Already decided.",
            "only a stale 409 takes the stale sentence"
        );
    }

    #[test]
    fn a_launch_in_flight_is_its_own_state_and_cannot_be_approved() {
        assert_eq!(state_look(State::Launching).2, "Launching");
        assert_eq!(state_look(State::Launching).1, Tone::Run);
        let launching = Proposal {
            state: State::Launching,
            ..waiting(PreviewState::Allowed, None)
        };
        assert!(!can_approve(&launching));
    }

    #[test]
    fn a_proposal_list_reads_with_no_proposals_key_as_empty() {
        let body: ProposalsBody = serde_json::from_value(json!({})).expect("body");
        assert!(body.proposals.is_empty());
    }
}
