//! The CI/CD page's answer (plan 17, task 9.1, D42): the required checks, the promotion path and the open pull
//! requests, read from GitHub by a background task into `DeliveryCache`. `GET /v1/delivery` only reads that cache.
//!
//! One refresh reads REST only, through the client `github.rs` builds for the Policy page, with the console's
//! read-only token: the ruleset `.github/rulesets/protect-main.json` on the configured ref (its
//! `required_status_checks` contexts), the comparison of the ref with `main`, the open pull requests into the ref
//! and into `main`, and for each of those its head commit's check runs and its reviews. A request that fails fails
//! the whole read, and the last good read stays. No token, URL or response body reaches an error or a log line.

use std::collections::BTreeMap;
use std::time::Duration;

use dioxus::server::axum::extract::State;
use dioxus::server::axum::response::{IntoResponse, Response};
use dioxus::server::axum::Json;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use tokio::time::MissedTickBehavior;
use tower_sessions::Session;

use super::config::{GithubConfig, Secret};
use super::github::{self, FetchError, Reader, GITHUB_API, GITHUB_WEB};
use super::openbao::Openbao;
use super::work::{self, rfc3339_now};
use super::{AppState, SharedState};

/// How often the read runs. Fifteen minutes, as the Policy read.
pub const REFRESH_INTERVAL: Duration = Duration::from_secs(900);
/// GitHub requests one refresh may send. The ruleset, the comparison and the two pull lists come first (four),
/// then two per pull request (its check runs and its reviews). With `PULLS_PER_LIST` in each list the most is
/// 4 + 2 * 60 = 124, so the bound equals that most and a full refresh always completes; it is a backstop that a
/// test lowers to prove it holds.
pub const MAX_REQUESTS: usize = 124;
/// Open pull requests asked for per list (into the ref, and into `main`).
const PULLS_PER_LIST: &str = "30";
/// The branch a promotion ends at.
const PROMOTION_BASE: &str = "main";
/// The committed ruleset, read on the ref.
const RULESET_PATH: &str = ".github/rulesets/protect-main.json";

/// The state one check run is in.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum CheckState {
    Passed,
    Failed,
    Pending,
    Skipped,
}

impl CheckState {
    fn as_str(self) -> &'static str {
        match self {
            CheckState::Passed => "passed",
            CheckState::Failed => "failed",
            CheckState::Pending => "pending",
            CheckState::Skipped => "skipped",
        }
    }
}

/// Counts of the newest run of each check name on a pull request's head commit.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize)]
pub struct Counts {
    pub passed: usize,
    pub failed: usize,
    pub pending: usize,
    pub skipped: usize,
}

/// One required check, named, as a pull request into `main` shows it: `passed`, `failed`, `pending` or `missing`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct RequiredState {
    pub name: String,
    pub state: &'static str,
}

/// One open pull request as `GET /v1/delivery` carries it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Pull {
    pub number: u64,
    pub title: String,
    pub author: String,
    pub base: String,
    pub head: String,
    pub draft: bool,
    pub url: String,
    pub updated_at: String,
    /// `approved`, `changes_requested` or `none`.
    pub review: &'static str,
    pub checks: Counts,
    /// Filled only for a pull request into `main`: one entry per required check, in ruleset order. Empty otherwise.
    pub required: Vec<RequiredState>,
}

/// The promotion path: how far the ref is from `main`, and its open pull request into `main`, if it has one.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Promotion {
    pub base: &'static str,
    pub head: String,
    pub ahead_by: u64,
    pub behind_by: u64,
    pub compare_url: String,
    pub pull: Option<Pull>,
}

/// One finished read of the delivery state.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Delivery {
    /// The `required_status_checks` contexts of the ruleset on the ref, in ruleset order.
    required: Vec<String>,
    ruleset_url: String,
    promotion: Promotion,
    pulls: Vec<Pull>,
}

/// The last read that finished.
struct Good {
    repo: String,
    git_ref: String,
    delivery: Delivery,
    refreshed_at: String,
}

/// What `GET /v1/delivery` reads. `good` is the last read that finished. `failure` is why the newest read failed,
/// as its reason only. A failed read leaves `good` in place, and a read that finishes clears `failure`.
pub struct DeliveryCache {
    configured: bool,
    good: Option<Good>,
    failure: Option<String>,
}

/// `GET /v1/delivery`. `repo`, `ref`, `ruleset_url`, `promotion` and `refreshed_at` are `null` until a read has
/// finished, and `required_checks` and `pulls` are empty.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct DeliveryBody {
    pub state: &'static str,
    pub repo: Option<String>,
    #[serde(rename = "ref")]
    pub git_ref: Option<String>,
    pub required_checks: Vec<String>,
    pub ruleset_url: Option<String>,
    pub promotion: Option<Promotion>,
    pub pulls: Vec<Pull>,
    pub refreshed_at: Option<String>,
    pub error: Option<String>,
}

/// A failure reason without its final full stop. The reasons GitHub's refusals carry end in one, and the
/// sentence the page shows adds its own, so a reason is never shown with two.
fn reason_text(reason: &str) -> &str {
    reason.trim_end_matches('.')
}

impl DeliveryCache {
    /// `configured` is whether OpenBao and the GitHub source are both set. When they are not, no read runs and
    /// the page says so.
    pub fn new(configured: bool) -> Self {
        Self {
            configured,
            good: None,
            failure: None,
        }
    }

    /// Stores a finished read and clears the failure.
    pub fn record_ok(&mut self, source: &GithubConfig, delivery: Delivery, refreshed_at: String) {
        self.failure = None;
        self.good = Some(Good {
            repo: source.repo.clone(),
            git_ref: source.git_ref.clone(),
            delivery,
            refreshed_at,
        });
    }

    /// Keeps the last good read, if there is one, and stores the reason this read failed.
    pub fn record_error(&mut self, reason: String) {
        self.failure = Some(reason);
    }

    /// The sentence the page shows, or none. A failed read names its reason, and says whether the last good read
    /// is still shown.
    fn error_sentence(&self) -> Option<String> {
        match (&self.failure, &self.good) {
            (Some(reason), Some(_)) => Some(format!(
                "The newest delivery read failed: {}. Showing the last good read.",
                reason_text(reason)
            )),
            (Some(reason), None) => Some(format!(
                "The delivery read failed: {}.",
                reason_text(reason)
            )),
            (None, _) => None,
        }
    }

    /// The body `GET /v1/delivery` serves. `state` is `unconfigured` without the source, `loading` before the first
    /// read finishes, `error` when no read has finished and the newest failed, and `ok` otherwise.
    pub fn body(&self) -> DeliveryBody {
        let state = match (self.configured, &self.good, &self.failure) {
            (false, _, _) => "unconfigured",
            (true, Some(_), _) => "ok",
            (true, None, Some(_)) => "error",
            (true, None, None) => "loading",
        };
        let error = self.error_sentence();
        match &self.good {
            Some(good) => DeliveryBody {
                state,
                repo: Some(good.repo.clone()),
                git_ref: Some(good.git_ref.clone()),
                required_checks: good.delivery.required.clone(),
                ruleset_url: Some(good.delivery.ruleset_url.clone()),
                promotion: Some(good.delivery.promotion.clone()),
                pulls: good.delivery.pulls.clone(),
                refreshed_at: Some(good.refreshed_at.clone()),
                error,
            },
            None => DeliveryBody {
                state,
                repo: None,
                git_ref: None,
                required_checks: Vec::new(),
                ruleset_url: None,
                promotion: None,
                pulls: Vec::new(),
                refreshed_at: None,
                error,
            },
        }
    }
}

/// `GET /v1/delivery` (platform admins only, as `/v1/policies`). Reads the cache, never OpenBao or GitHub.
pub async fn get_delivery(State(app): State<SharedState>, session: Session) -> Response {
    if let Err(response) = work::admin_only(&session).await {
        return response;
    }
    let cache = app.delivery.read().await;
    Json(cache.body()).into_response()
}

/// Reads the delivery state once and stores the result. Does nothing unless OpenBao and the GitHub source are both
/// configured. The lock is held only to store, never across the reads.
pub async fn refresh(app: &AppState) {
    refresh_from(app, GITHUB_API).await;
}

/// `refresh` against `api`. Production passes `GITHUB_API`; tests pass a local origin.
async fn refresh_from(app: &AppState, api: &str) {
    let (Some(bao), Some(source)) = (app.openbao.as_ref(), app.config.github.as_ref()) else {
        return;
    };
    let result = read(bao, &app.http, api, source, MAX_REQUESTS).await;
    let mut cache = app.delivery.write().await;
    match result {
        Ok(delivery) => cache.record_ok(source, delivery, rfc3339_now()),
        Err(err) => {
            let text = err.to_string();
            log_failure(cache.failure.as_deref(), &text);
            cache.record_error(text);
        }
    }
}

/// One read: the token from OpenBao now, then the delivery state from GitHub with it. `max_requests` bounds the
/// requests this read may send; `refresh_from` passes `MAX_REQUESTS`.
async fn read(
    bao: &Openbao,
    http: &reqwest::Client,
    api: &str,
    source: &GithubConfig,
    max_requests: usize,
) -> Result<Delivery, FetchError> {
    let token = token(bao, http).await?;
    let mut reader = Reader::new(http, &token, max_requests);
    let (owner, name) = source.repo.split_once('/').ok_or(FetchError::Decode)?;

    let mut ruleset_url = github::endpoint(
        api,
        &[
            "repos",
            owner,
            name,
            "contents",
            ".github",
            "rulesets",
            "protect-main.json",
        ],
    )?;
    ruleset_url
        .query_pairs_mut()
        .append_pair("ref", &source.git_ref);
    let ruleset = reader.raw(ruleset_url).await?;
    let required = required_check_names(&ruleset)?;

    // `main...<ref>`: the ref's slashes stay path separators, as GitHub's own compare URLs carry them.
    let basehead = format!("{PROMOTION_BASE}...{}", source.git_ref);
    let mut compare_segments = vec!["repos", owner, name, "compare"];
    compare_segments.extend(basehead.split('/'));
    let compare: CompareRow = reader
        .json(github::endpoint(api, &compare_segments)?)
        .await?;

    let mut bases = vec![source.git_ref.as_str()];
    if source.git_ref != PROMOTION_BASE {
        bases.push(PROMOTION_BASE);
    }
    let mut rows: Vec<PullRow> = Vec::new();
    for base in bases {
        let mut url = github::endpoint(api, &["repos", owner, name, "pulls"])?;
        url.query_pairs_mut()
            .append_pair("state", "open")
            .append_pair("base", base)
            .append_pair("sort", "updated")
            .append_pair("direction", "desc")
            .append_pair("per_page", PULLS_PER_LIST);
        let page: Vec<PullRow> = reader.json(url).await?;
        rows.extend(page);
    }

    let mut pulls = Vec::with_capacity(rows.len());
    let mut promotion_pull = None;
    for row in &rows {
        let pull = pull_state(&mut reader, api, source, row, &required).await?;
        let head_repo = row.head.repo.as_ref().map(|repo| repo.full_name.as_str());
        if promotion_pull.is_none()
            && row.base.git_ref == PROMOTION_BASE
            && is_promotion(&row.head.git_ref, head_repo, &source.repo, &source.git_ref)
        {
            promotion_pull = Some(pull.clone());
        }
        pulls.push(pull);
    }

    let mut compare_url_segments = vec![owner, name, "compare"];
    compare_url_segments.extend(basehead.split('/'));
    Ok(Delivery {
        required,
        ruleset_url: github::html_url(source, RULESET_PATH)?,
        promotion: Promotion {
            base: PROMOTION_BASE,
            head: source.git_ref.clone(),
            ahead_by: compare.ahead_by,
            behind_by: compare.behind_by,
            compare_url: github::endpoint(GITHUB_WEB, &compare_url_segments)?.to_string(),
            pull: promotion_pull,
        },
        pulls,
    })
}

/// One open pull request: its check runs on the head commit, its reviews, and for a pull into `main` each
/// required check's state.
async fn pull_state(
    reader: &mut Reader<'_>,
    api: &str,
    source: &GithubConfig,
    row: &PullRow,
    required: &[String],
) -> Result<Pull, FetchError> {
    let (owner, name) = source.repo.split_once('/').ok_or(FetchError::Decode)?;
    let mut runs_url = github::endpoint(
        api,
        &[
            "repos",
            owner,
            name,
            "commits",
            row.head.sha.as_str(),
            "check-runs",
        ],
    )?;
    runs_url.query_pairs_mut().append_pair("per_page", "100");
    let runs: CheckRuns = reader.json(runs_url).await?;

    let number = row.number.to_string();
    let mut reviews_url = github::endpoint(
        api,
        &["repos", owner, name, "pulls", number.as_str(), "reviews"],
    )?;
    reviews_url.query_pairs_mut().append_pair("per_page", "100");
    let reviews: Vec<ReviewRow> = reader.json(reviews_url).await?;

    let author = login(&row.user);
    let latest = latest_runs(&runs.check_runs);
    Ok(Pull {
        number: row.number,
        title: row.title.clone(),
        author: author.clone(),
        base: row.base.git_ref.clone(),
        head: row.head.git_ref.clone(),
        draft: row.draft,
        url: row.html_url.clone(),
        updated_at: row.updated_at.clone(),
        review: review_state(&author, &reviews),
        checks: counts(&latest),
        required: if row.base.git_ref == PROMOTION_BASE {
            required_states(required, &latest)
        } else {
            Vec::new()
        },
    })
}

/// The console's GitHub token: the `token` field at `secret/services/console/github`, read now. It is refreshed in
/// OpenBao by `refresh-console-github-token.yml`, so a value read earlier may be spent.
async fn token(bao: &Openbao, http: &reqwest::Client) -> Result<Secret, FetchError> {
    let data = bao
        .read_console_secret(http, "github")
        .await
        .map_err(FetchError::OpenBao)?;
    data.get("token")
        .and_then(Value::as_str)
        .filter(|token| !token.is_empty())
        .map(Secret::new)
        .ok_or(FetchError::TokenMissing)
}

/// One line per distinct failure, not one per read while it lasts.
fn log_failure(previous: Option<&str>, text: &str) {
    if previous != Some(text) {
        eprintln!("console: GitHub delivery read failed: {text}");
    }
}

/// Starts the delivery read when OpenBao and the GitHub source are configured. The first read runs at once. A
/// missed tick is skipped, not replayed.
pub fn spawn_refresh(state: SharedState) {
    if state.openbao.is_none() || state.config.github.is_none() {
        return;
    }
    tokio::spawn(async move {
        let mut ticker = tokio::time::interval(REFRESH_INTERVAL);
        ticker.set_missed_tick_behavior(MissedTickBehavior::Delay);
        loop {
            ticker.tick().await;
            refresh(&state).await;
        }
    });
}

#[derive(Deserialize)]
struct Login {
    login: String,
}

#[derive(Deserialize)]
struct BranchRef {
    #[serde(rename = "ref")]
    git_ref: String,
}

#[derive(Deserialize)]
struct RepoName {
    full_name: String,
}

#[derive(Deserialize)]
struct HeadRef {
    #[serde(rename = "ref")]
    git_ref: String,
    sha: String,
    repo: Option<RepoName>,
}

/// One row of a pull request list, with the fields the page carries.
#[derive(Deserialize)]
struct PullRow {
    number: u64,
    title: String,
    user: Option<Login>,
    base: BranchRef,
    head: HeadRef,
    draft: bool,
    html_url: String,
    updated_at: String,
}

#[derive(Deserialize)]
struct CheckRuns {
    check_runs: Vec<CheckRow>,
}

/// One check run. GitHub numbers runs by `id`, so a re-run is a larger id of the same name.
#[derive(Deserialize)]
struct CheckRow {
    id: u64,
    name: String,
    status: String,
    conclusion: Option<String>,
}

#[derive(Deserialize)]
struct ReviewRow {
    user: Option<Login>,
    state: String,
}

#[derive(Deserialize)]
struct CompareRow {
    ahead_by: u64,
    behind_by: u64,
}

fn login(user: &Option<Login>) -> String {
    user.as_ref()
        .map(|user| user.login.clone())
        .unwrap_or_default()
}

/// The `context` names of every `required_status_checks` rule in `text`, in ruleset order, each name once. Refused
/// when the text is not JSON, has no `rules` array, or names no check: a page with no required check would show
/// every pull request as having nothing to pass.
fn required_check_names(text: &str) -> Result<Vec<String>, FetchError> {
    let ruleset: Value = serde_json::from_str(text).map_err(|_| FetchError::Decode)?;
    let rules = ruleset
        .get("rules")
        .and_then(Value::as_array)
        .ok_or(FetchError::Decode)?;
    let mut names: Vec<String> = Vec::new();
    for rule in rules {
        if rule.get("type").and_then(Value::as_str) != Some("required_status_checks") {
            continue;
        }
        let checks = rule
            .pointer("/parameters/required_status_checks")
            .and_then(Value::as_array)
            .into_iter()
            .flatten();
        for check in checks {
            let Some(context) = check.get("context").and_then(Value::as_str) else {
                continue;
            };
            if names.iter().all(|name| name != context) {
                names.push(context.to_string());
            }
        }
    }
    if names.is_empty() {
        return Err(FetchError::Decode);
    }
    Ok(names)
}

/// The state of one check run: not completed is pending; success passes; skipped and neutral are skipped; any
/// other conclusion, or none, fails.
fn run_state(status: &str, conclusion: Option<&str>) -> CheckState {
    if status != "completed" {
        return CheckState::Pending;
    }
    match conclusion {
        Some("success") => CheckState::Passed,
        Some("skipped" | "neutral") => CheckState::Skipped,
        _ => CheckState::Failed,
    }
}

/// The newest run of each check name. A re-run is a newer run of the same name, so the highest id wins whatever
/// order GitHub listed the runs in.
fn latest_runs(runs: &[CheckRow]) -> BTreeMap<&str, &CheckRow> {
    let mut latest: BTreeMap<&str, &CheckRow> = BTreeMap::new();
    for run in runs {
        latest
            .entry(run.name.as_str())
            .and_modify(|kept| {
                if run.id > kept.id {
                    *kept = run;
                }
            })
            .or_insert(run);
    }
    latest
}

fn counts(latest: &BTreeMap<&str, &CheckRow>) -> Counts {
    let mut counts = Counts::default();
    for run in latest.values() {
        match run_state(&run.status, run.conclusion.as_deref()) {
            CheckState::Passed => counts.passed += 1,
            CheckState::Failed => counts.failed += 1,
            CheckState::Pending => counts.pending += 1,
            CheckState::Skipped => counts.skipped += 1,
        }
    }
    counts
}

/// Each required check by name, in ruleset order. A name with no run on the head is `missing`.
fn required_states(required: &[String], latest: &BTreeMap<&str, &CheckRow>) -> Vec<RequiredState> {
    required
        .iter()
        .map(|name| RequiredState {
            name: name.clone(),
            state: match latest.get(name.as_str()) {
                Some(run) => run_state(&run.status, run.conclusion.as_deref()).as_str(),
                None => "missing",
            },
        })
        .collect()
}

/// The review state: the latest review of each reviewer other than the author. Comment-only, dismissed and
/// pending reviews do not count. A changes request from any reviewer wins, then an approval, else none.
fn review_state(author: &str, reviews: &[ReviewRow]) -> &'static str {
    let mut latest: BTreeMap<&str, &str> = BTreeMap::new();
    for review in reviews {
        let Some(user) = &review.user else {
            continue;
        };
        if user.login.eq_ignore_ascii_case(author) {
            continue;
        }
        if matches!(review.state.as_str(), "COMMENTED" | "DISMISSED" | "PENDING") {
            continue;
        }
        latest.insert(user.login.as_str(), review.state.as_str());
    }
    if latest.values().any(|state| *state == "CHANGES_REQUESTED") {
        "changes_requested"
    } else if latest.values().any(|state| *state == "APPROVED") {
        "approved"
    } else {
        "none"
    }
}

/// Whether a pull request into `main` is the promotion pull of the ref: its head branch is the ref, in the
/// configured repository and not in a fork. Repository names are case-insensitive on GitHub.
fn is_promotion(head_ref: &str, head_repo: Option<&str>, repo: &str, git_ref: &str) -> bool {
    head_ref == git_ref && head_repo.is_some_and(|full| full.eq_ignore_ascii_case(repo))
}

#[cfg(test)]
mod tests {

    /// The CI/CD page parses exactly what this route serves: every field reaches the page's own type with its
    /// value, so a renamed key or state word on either side fails here rather than reading as a default.
    #[test]
    fn the_cicd_page_reads_every_field_the_route_serves() {
        use crate::ui::cicd_api as page;
        let pull = Pull {
            number: 7,
            title: "Promote".into(),
            author: "someone".into(),
            base: "main".into(),
            head: "dev".into(),
            draft: true,
            url: "https://github.com/o/r/pull/7".into(),
            updated_at: "2026-11-08T00:00:00Z".into(),
            review: "changes_requested",
            checks: Counts {
                passed: 1,
                failed: 2,
                pending: 3,
                skipped: 4,
            },
            required: vec![RequiredState {
                name: "Unit Tests".into(),
                state: "missing",
            }],
        };
        let mut cache = DeliveryCache::new(true);
        cache.record_ok(
            &GithubConfig {
                repo: "o/r".into(),
                git_ref: "dev".into(),
            },
            Delivery {
                required: vec!["Unit Tests".into()],
                ruleset_url: "https://github.com/o/r/blob/dev/x.json".into(),
                promotion: Promotion {
                    base: "main",
                    head: "dev".into(),
                    ahead_by: 5,
                    behind_by: 1,
                    compare_url: "https://github.com/o/r/compare/main...dev".into(),
                    pull: Some(pull.clone()),
                },
                pulls: vec![pull],
            },
            "2026-11-08T00:00:00Z".into(),
        );
        let wire = serde_json::to_string(&cache.body()).expect("serialize");
        let read: page::DeliveryBody =
            serde_json::from_str(&wire).expect("the page parses the route");
        assert_eq!(read.state, page::DeliveryState::Ok);
        assert_eq!(read.repo.as_deref(), Some("o/r"));
        assert_eq!(read.git_ref.as_deref(), Some("dev"));
        assert_eq!(read.required_checks, vec!["Unit Tests".to_string()]);
        assert!(read.ruleset_url.is_some());
        let promotion = read.promotion.expect("promotion");
        assert_eq!(
            (
                promotion.base.as_str(),
                promotion.ahead_by,
                promotion.behind_by
            ),
            ("main", 5, 1)
        );
        let shown = promotion.pull.expect("promotion pull");
        assert_eq!(shown.review, page::ReviewState::ChangesRequested);
        assert!(shown.draft);
        assert_eq!(
            (
                shown.checks.passed,
                shown.checks.failed,
                shown.checks.pending,
                shown.checks.skipped
            ),
            (1, 2, 3, 4)
        );
        assert_eq!(shown.required[0].state, page::RequiredState::Missing);
        assert_eq!(read.pulls.len(), 1);
        assert_eq!(read.refreshed_at.as_deref(), Some("2026-11-08T00:00:00Z"));
    }

    use super::*;
    use std::net::SocketAddr;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::{Arc, Mutex};

    use dioxus::server::axum::http::{HeaderMap, StatusCode, Uri};
    use dioxus::server::axum::routing::{get, post};
    use dioxus::server::axum::Router;
    use serde_json::json;

    use crate::server::auth;
    use crate::server::config::{Config, OpenbaoConfig};
    use crate::server::inventory::{FlowSet, InventoryCache};
    use crate::server::openbao::Openbao;
    use crate::server::prefs::Prefs;
    use crate::server::scope::Scope;
    use crate::server::{api_router, session_layer};

    const ROLE_ID: &str = "console-role-test";
    const SECRET_ID: &str = "console-secret-test";
    /// The token the fake OpenBao and the fake GitHub hold. Not shaped like a real GitHub token.
    const TOKEN: &str = "console-github-canary-token";
    const SCOPE: &str = r#"{"example": true, "tenants": []}"#;
    const REPO_PATH: &str = "/repos/uhstray-io/agent-cloud/";
    /// The committed ruleset, so the fake serves the names the real file names.
    const RULESET: &str = include_str!("../../../../../../.github/rulesets/protect-main.json");

    const SHA_915: &str = "1111111111111111111111111111111111111915";
    const SHA_916: &str = "1111111111111111111111111111111111111916";
    const SHA_900: &str = "1111111111111111111111111111111111111900";
    const SHA_901: &str = "1111111111111111111111111111111111111901";

    fn required_names() -> Vec<&'static str> {
        vec![
            "Static Analysis",
            "Security Scan",
            "Unit Tests",
            "Promotion source (dev -> main)",
        ]
    }

    /// A fake OpenBao: AppRole login and the console's GitHub secret. It counts every read of it.
    struct FakeBao {
        /// The `token` field it answers with. `None` omits the field.
        token: Option<String>,
        reads: AtomicUsize,
    }

    async fn bao_login() -> (StatusCode, Json<Value>) {
        (
            StatusCode::OK,
            Json(json!({"auth": {
                "client_token": "tok",
                "lease_duration": 1800,
                "num_uses": 0,
            }})),
        )
    }

    async fn bao_read(State(fake): State<Arc<FakeBao>>, uri: Uri) -> (StatusCode, Json<Value>) {
        if uri.path() != "/v1/secret/data/services/console/github" {
            return (StatusCode::NOT_FOUND, Json(json!({"errors": []})));
        }
        fake.reads.fetch_add(1, Ordering::SeqCst);
        let data = match &fake.token {
            Some(token) => json!({"token": token, "refreshed_at": "2026-11-04T00:00:00Z"}),
            None => json!({"refreshed_at": "2026-11-04T00:00:00Z"}),
        };
        (StatusCode::OK, Json(json!({"data": {"data": data}})))
    }

    async fn serve(app: Router) -> SocketAddr {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind");
        let addr = listener.local_addr().expect("addr");
        tokio::spawn(async move {
            dioxus::server::axum::serve(listener, app)
                .await
                .expect("serve");
        });
        addr
    }

    async fn fake_bao(token: Option<&str>) -> (Arc<FakeBao>, Openbao) {
        let fake = Arc::new(FakeBao {
            token: token.map(str::to_string),
            reads: AtomicUsize::new(0),
        });
        let app = Router::new()
            .route("/v1/auth/approle/login", post(bao_login))
            .route("/v1/{*rest}", get(bao_read))
            .with_state(fake.clone());
        let addr = serve(app).await;
        let client = Openbao::new(OpenbaoConfig {
            api_url: format!("http://{addr}"),
            role_id: Secret::new(ROLE_ID),
            secret_id: Secret::new(SECRET_ID),
        })
        .expect("client");
        (fake, client)
    }

    /// A fake GitHub origin for one repository. It answers only the bearer `TOKEN`, records every path and query
    /// it receives, and refuses every request with `fail_with` when that is non-zero.
    struct FakeGithub {
        fail_with: AtomicUsize,
        seen: Mutex<Vec<String>>,
    }

    fn json_ok(value: Value) -> Response {
        (StatusCode::OK, Json(value)).into_response()
    }

    fn check_run(id: u64, name: &str, status: &str, conclusion: Option<&str>) -> Value {
        json!({"id": id, "name": name, "status": status, "conclusion": conclusion})
    }

    fn check_runs(sha: &str) -> Value {
        let runs = match sha {
            SHA_915 => vec![
                check_run(1, "Static Analysis", "completed", Some("failure")),
                check_run(5, "Static Analysis", "completed", Some("success")),
                check_run(2, "Unit Tests", "in_progress", None),
                check_run(3, "lint", "completed", Some("skipped")),
                check_run(4, "Docs", "completed", Some("neutral")),
            ],
            SHA_900 => vec![
                check_run(10, "Static Analysis", "completed", Some("success")),
                check_run(11, "Security Scan", "completed", Some("failure")),
                check_run(12, "Unit Tests", "completed", Some("success")),
                check_run(13, "Promotion source (dev -> main)", "queued", None),
            ],
            SHA_901 => vec![check_run(
                20,
                "Static Analysis",
                "completed",
                Some("success"),
            )],
            _ => Vec::new(),
        };
        json!({"total_count": runs.len(), "check_runs": runs})
    }

    fn review(login: &str, state: &str) -> Value {
        json!({"user": {"login": login}, "state": state})
    }

    fn reviews(number: &str) -> Value {
        let rows = match number {
            "915" => vec![
                review("jestyr27", "APPROVED"),
                review("alice", "COMMENTED"),
                review("bob", "CHANGES_REQUESTED"),
                review("bob", "APPROVED"),
            ],
            "916" => vec![
                review("carol", "APPROVED"),
                review("dave", "CHANGES_REQUESTED"),
            ],
            "901" => vec![
                review("contributor", "APPROVED"),
                review("dave", "COMMENTED"),
            ],
            _ => Vec::new(),
        };
        Value::Array(rows)
    }

    /// One pull request as a list carries it. The titles are the fixture's own.
    fn pull(
        number: u64,
        author: &str,
        base: &str,
        head: &str,
        head_repo: &str,
        draft: bool,
        sha: &str,
    ) -> Value {
        let title = match number {
            915 => "docs: x",
            916 => "fix: y",
            901 => "fix: from a fork",
            900 => "release: dev to main",
            _ => "other",
        };
        json!({
            "number": number,
            "title": title,
            "user": {"login": author},
            "draft": draft,
            "html_url": format!("https://github.com/uhstray-io/agent-cloud/pull/{number}"),
            "updated_at": "2026-11-08T05:00:00Z",
            "base": {"ref": base},
            "head": {"ref": head, "sha": sha, "repo": {"full_name": head_repo}},
        })
    }

    async fn github_answer(
        State(fake): State<Arc<FakeGithub>>,
        headers: HeaderMap,
        uri: Uri,
    ) -> Response {
        fake.seen.lock().expect("seen").push(uri.to_string());
        let presented = headers
            .get("authorization")
            .and_then(|value| value.to_str().ok());
        if presented != Some(format!("Bearer {TOKEN}").as_str()) {
            return (StatusCode::UNAUTHORIZED, Json(json!({"message": "denied"}))).into_response();
        }
        let forced = fake.fail_with.load(Ordering::SeqCst);
        if forced != 0 {
            let status = StatusCode::from_u16(forced as u16).expect("status");
            return (status, Json(json!({"message": "denied"}))).into_response();
        }
        let Some(rest) = uri.path().strip_prefix(REPO_PATH) else {
            return (StatusCode::NOT_FOUND, Json(json!({"message": "not found"}))).into_response();
        };
        let query = uri.query().unwrap_or_default();
        if rest == "contents/.github/rulesets/protect-main.json" {
            return (StatusCode::OK, RULESET).into_response();
        }
        if rest == "compare/main...dev" {
            return json_ok(json!({"ahead_by": 12, "behind_by": 0}));
        }
        if rest == "pulls" && query.contains("base=dev") {
            return json_ok(json!([
                pull(
                    915,
                    "jestyr27",
                    "dev",
                    "docs/x",
                    "uhstray-io/agent-cloud",
                    false,
                    SHA_915
                ),
                pull(
                    916,
                    "jestyr27",
                    "dev",
                    "fix/y",
                    "uhstray-io/agent-cloud",
                    true,
                    SHA_916
                ),
            ]));
        }
        if rest == "pulls" && query.contains("base=main") {
            // The fork's pull comes first, so a promotion that ignored the repository would pick it.
            return json_ok(json!([
                pull(
                    901,
                    "contributor",
                    "main",
                    "dev",
                    "contributor/agent-cloud",
                    false,
                    SHA_901
                ),
                pull(
                    900,
                    "jestyr27",
                    "main",
                    "dev",
                    "uhstray-io/agent-cloud",
                    false,
                    SHA_900
                ),
            ]));
        }
        if let Some(sha) = rest
            .strip_prefix("commits/")
            .and_then(|tail| tail.strip_suffix("/check-runs"))
        {
            return json_ok(check_runs(sha));
        }
        if let Some(number) = rest
            .strip_prefix("pulls/")
            .and_then(|tail| tail.strip_suffix("/reviews"))
        {
            return json_ok(reviews(number));
        }
        (StatusCode::NOT_FOUND, Json(json!({"message": "not found"}))).into_response()
    }

    /// A fake GitHub origin for the repository, and its `api` base.
    async fn fake_github() -> (Arc<FakeGithub>, String) {
        let fake = Arc::new(FakeGithub {
            fail_with: AtomicUsize::new(0),
            seen: Mutex::new(Vec::new()),
        });
        let app = Router::new()
            .fallback(github_answer)
            .with_state(fake.clone());
        let addr = serve(app).await;
        (fake, format!("http://{addr}"))
    }

    fn source() -> GithubConfig {
        GithubConfig {
            repo: "uhstray-io/agent-cloud".into(),
            git_ref: "dev".into(),
        }
    }

    fn check(name: &str, status: &str, conclusion: Option<&str>, id: u64) -> CheckRow {
        CheckRow {
            id,
            name: name.into(),
            status: status.into(),
            conclusion: conclusion.map(str::to_string),
        }
    }

    fn reviewed(login: Option<&str>, state: &str) -> ReviewRow {
        ReviewRow {
            user: login.map(|login| Login {
                login: login.into(),
            }),
            state: state.into(),
        }
    }

    /// The application state. OpenBao and the GitHub source are both present when `openbao` is given and
    /// `github` is `Some`, so the cache is configured exactly when a read can run.
    fn state(openbao: Option<Openbao>, github: Option<GithubConfig>) -> SharedState {
        let configured = openbao.is_some() && github.is_some();
        Arc::new(AppState {
            config: Config {
                base_url: "https://console.test".into(),
                oidc_issuer: "https://auth.test/application/o/console/".into(),
                oidc_client_id: "console".into(),
                oidc_client_secret: "test-secret".into(),
                extra_ca_file: None,
                data_dir: std::env::temp_dir(),
                scope_file: std::env::temp_dir(),
                semaphore: None,
                workflow_file: None,
                netbox: None,
                proxmox: None,
                openbao: None,
                flows_file: None,
                gateway_file: None,
                catalog_file: None,
                grafana_url: None,
                grafana_token: None,
                authentik: None,
                role_map_file: None,
                github,
                launch_gate: None,
                launch_dev_variants: true,
            },
            http: reqwest::Client::new(),
            oidc: tokio::sync::OnceCell::new(),
            scope: Scope::parse(SCOPE).expect("scope"),
            prefs: Prefs::in_memory().expect("prefs"),
            work: Arc::new(tokio::sync::RwLock::new(work::WorkCache::unconfigured())),
            workflow: Vec::new(),
            inventory: tokio::sync::RwLock::new(InventoryCache::new(false, false)),
            proxmox_http: reqwest::Client::new(),
            flows: FlowSet::unconfigured(),
            gateway: crate::server::gateway::GatewaySet::unconfigured(),
            catalog: crate::server::catalog::CatalogSet::unconfigured(),
            usage: tokio::sync::RwLock::new(crate::server::grafana::UsageCache::new(false)),
            openbao,
            secrets: tokio::sync::RwLock::new(crate::server::secrets::SecretsCache::new(false)),
            access: tokio::sync::RwLock::new(crate::server::access::AccessCache::new(false)),
            role_map: crate::server::access::RoleMap::empty(),
            policies: tokio::sync::RwLock::new(crate::server::policies::PoliciesCache::new(false)),
            delivery: tokio::sync::RwLock::new(DeliveryCache::new(configured)),
            telemetry: tokio::sync::RwLock::new(crate::server::telemetry::TelemetryCache::new(
                false,
            )),
            gate: None,
        })
    }

    /// `GET /test/login`: signs a user in with `groups`.
    fn login_route(groups: &'static [&'static str]) -> Router {
        Router::new().route(
            "/test/login",
            get(move |session: Session| async move {
                let user = auth::CurrentUser {
                    sub: "user-1".into(),
                    email: None,
                    name: None,
                    username: None,
                    groups: groups.iter().map(|group| group.to_string()).collect(),
                };
                session
                    .insert(auth::KEY_USER, &user)
                    .await
                    .expect("insert user");
                "signed in"
            }),
        )
    }

    /// Serves the API over `state` and returns its address and a session cookie for a user in `groups`.
    async fn app_for(state: SharedState, groups: &'static [&'static str]) -> (SocketAddr, String) {
        let sessions = session_layer(Prefs::in_memory().expect("sessions").session_store());
        let app = api_router(state).merge(login_route(groups)).layer(sessions);
        let addr = serve(app).await;
        let reply = reqwest::get(format!("http://{addr}/test/login"))
            .await
            .expect("login");
        let cookie = reply
            .headers()
            .get("set-cookie")
            .expect("session cookie")
            .to_str()
            .expect("ascii cookie")
            .split(';')
            .next()
            .expect("cookie pair")
            .to_string();
        (addr, cookie)
    }

    /// One GET, with `cookie` when given. Returns the status and the body text.
    async fn fetch(addr: SocketAddr, path: &str, cookie: Option<&str>) -> (u16, String) {
        let client = reqwest::Client::new();
        let mut request = client.get(format!("http://{addr}{path}"));
        if let Some(cookie) = cookie {
            request = request.header("cookie", cookie);
        }
        let reply = request.send().await.expect("send");
        let status = reply.status().as_u16();
        (status, reply.text().await.expect("body"))
    }

    #[test]
    fn the_cache_is_unconfigured_without_a_source_and_loading_before_a_read() {
        let body = DeliveryCache::new(false).body();
        assert_eq!(body.state, "unconfigured");
        assert!(body.pulls.is_empty() && body.required_checks.is_empty());
        assert!(body.error.is_none() && body.promotion.is_none());
        assert_eq!(body.repo, None);
        assert_eq!(body.git_ref, None);
        assert_eq!(body.refreshed_at, None);

        let body = DeliveryCache::new(true).body();
        assert_eq!(body.state, "loading", "no read has finished yet");
    }

    #[test]
    fn a_failed_read_with_no_good_read_is_an_error_naming_its_reason() {
        let mut cache = DeliveryCache::new(true);
        cache.record_error("GitHub answered HTTP 401.".into());
        let body = cache.body();
        assert_eq!(body.state, "error");
        assert_eq!(
            body.error.as_deref(),
            Some("The delivery read failed: GitHub answered HTTP 401.")
        );
        assert!(body.pulls.is_empty() && body.refreshed_at.is_none());
    }

    #[test]
    fn a_failed_read_keeps_the_last_good_read_and_says_why_until_the_next_good_one() {
        let mut cache = DeliveryCache::new(true);
        cache.record_ok(
            &source(),
            Delivery {
                required: vec!["Unit Tests".into()],
                ruleset_url: "https://github.com/x".into(),
                promotion: Promotion {
                    base: PROMOTION_BASE,
                    head: "dev".into(),
                    ahead_by: 3,
                    behind_by: 0,
                    compare_url: "https://github.com/y".into(),
                    pull: None,
                },
                pulls: Vec::new(),
            },
            "2026-11-04T10:00:00Z".into(),
        );
        cache.record_error("cannot connect to GitHub".into());
        let body = cache.body();
        assert_eq!(body.state, "ok");
        assert_eq!(body.required_checks, vec!["Unit Tests".to_string()]);
        assert_eq!(body.refreshed_at.as_deref(), Some("2026-11-04T10:00:00Z"));
        assert_eq!(
            body.error.as_deref(),
            Some("The newest delivery read failed: cannot connect to GitHub. Showing the last good read.")
        );

        cache.record_ok(
            &source(),
            Delivery {
                required: Vec::new(),
                ruleset_url: String::new(),
                promotion: Promotion {
                    base: PROMOTION_BASE,
                    head: "dev".into(),
                    ahead_by: 0,
                    behind_by: 0,
                    compare_url: String::new(),
                    pull: None,
                },
                pulls: Vec::new(),
            },
            "2026-11-04T10:15:00Z".into(),
        );
        assert_eq!(
            cache.body().error,
            None,
            "the next good read clears the failure"
        );
    }

    #[test]
    fn the_ruleset_names_are_its_required_status_checks_in_order_each_once() {
        let text = r#"{"rules": [
            {"type": "deletion"},
            {"type": "required_status_checks", "parameters": {"required_status_checks": [
                {"context": "A", "integration_id": 1}, {"context": "B"}]}},
            {"type": "required_status_checks", "parameters": {"required_status_checks": [
                {"context": "C"}, {"context": "A"}]}}
        ]}"#;
        assert_eq!(
            required_check_names(text).expect("names"),
            vec!["A".to_string(), "B".to_string(), "C".to_string()]
        );
    }

    #[test]
    fn a_ruleset_with_no_rules_or_no_required_check_is_refused() {
        for text in [
            "not json",
            r#"{"name": "protect-main"}"#,
            r#"{"rules": [{"type": "deletion"}]}"#,
        ] {
            assert_eq!(
                required_check_names(text),
                Err(FetchError::Decode),
                "{text}"
            );
        }
    }

    #[test]
    fn required_checks_match_the_committed_ruleset() {
        let text = include_str!("../../../../../../.github/rulesets/protect-main.json");
        assert_eq!(required_check_names(text).expect("names"), required_names());
    }

    #[test]
    fn the_newest_run_of_a_check_name_is_the_one_counted() {
        // The newer run is listed first, so a reader that took the last listed run would count the failure.
        let runs = vec![
            check("Static Analysis", "completed", Some("success"), 5),
            check("Static Analysis", "completed", Some("failure"), 1),
            check("Unit Tests", "in_progress", None, 2),
        ];
        let latest = latest_runs(&runs);
        assert_eq!(latest.len(), 2);
        assert_eq!(
            counts(&latest),
            Counts {
                passed: 1,
                failed: 0,
                pending: 1,
                skipped: 0
            }
        );
    }

    #[test]
    fn a_run_is_pending_until_completed_then_passed_skipped_or_failed() {
        for (status, conclusion, want) in [
            ("queued", None, "pending"),
            ("in_progress", None, "pending"),
            ("completed", Some("success"), "passed"),
            ("completed", Some("skipped"), "skipped"),
            ("completed", Some("neutral"), "skipped"),
            ("completed", Some("failure"), "failed"),
            ("completed", Some("cancelled"), "failed"),
            ("completed", Some("timed_out"), "failed"),
            ("completed", None, "failed"),
        ] {
            assert_eq!(
                run_state(status, conclusion).as_str(),
                want,
                "{status} {conclusion:?}"
            );
        }
    }

    #[test]
    fn required_states_follow_the_ruleset_order_and_name_a_missing_check() {
        let runs = vec![
            check("Unit Tests", "completed", Some("success"), 3),
            check("Static Analysis", "completed", Some("failure"), 1),
            check("Promotion source (dev -> main)", "queued", None, 4),
        ];
        let latest = latest_runs(&runs);
        let required: Vec<String> = required_names()
            .iter()
            .map(|name| name.to_string())
            .collect();
        let states = required_states(&required, &latest);
        let got: Vec<(&str, &str)> = states
            .iter()
            .map(|state| (state.name.as_str(), state.state))
            .collect();
        assert_eq!(
            got,
            vec![
                ("Static Analysis", "failed"),
                ("Security Scan", "missing"),
                ("Unit Tests", "passed"),
                ("Promotion source (dev -> main)", "pending"),
            ]
        );
    }

    #[test]
    fn reviews_count_the_latest_review_of_each_other_reviewer() {
        let cases: Vec<(&str, Vec<ReviewRow>, &str)> = vec![
            ("a", vec![], "none"),
            // The author's own approval does not count.
            ("x", vec![reviewed(Some("x"), "APPROVED")], "none"),
            // A comment does not replace an approval from the same reviewer.
            (
                "a",
                vec![
                    reviewed(Some("x"), "APPROVED"),
                    reviewed(Some("x"), "COMMENTED"),
                ],
                "approved",
            ),
            // GitHub reports a dismissed review as the same review with state DISMISSED. It does not count.
            ("a", vec![reviewed(Some("x"), "DISMISSED")], "none"),
            // A pending review does not count.
            ("a", vec![reviewed(Some("x"), "PENDING")], "none"),
            // The latest review of a reviewer is the one that counts.
            (
                "a",
                vec![
                    reviewed(Some("x"), "CHANGES_REQUESTED"),
                    reviewed(Some("x"), "APPROVED"),
                ],
                "approved",
            ),
            // A review with no user is not attributed to anyone.
            ("a", vec![reviewed(None, "CHANGES_REQUESTED")], "none"),
        ];
        for (author, reviews, want) in cases {
            assert_eq!(review_state(author, &reviews), want, "author {author}");
        }
    }

    #[test]
    fn a_changes_request_from_any_reviewer_wins_over_an_approval() {
        let reviews = vec![
            reviewed(Some("carol"), "APPROVED"),
            reviewed(Some("dave"), "CHANGES_REQUESTED"),
        ];
        assert_eq!(review_state("jestyr27", &reviews), "changes_requested");
    }

    #[test]
    fn the_promotion_pull_is_a_branch_of_this_repository_named_as_the_ref() {
        let repo = "uhstray-io/agent-cloud";
        assert!(is_promotion("dev", Some(repo), repo, "dev"));
        assert!(is_promotion(
            "dev",
            Some("Uhstray-IO/Agent-Cloud"),
            repo,
            "dev"
        ));
        assert!(
            !is_promotion("dev", Some("contributor/agent-cloud"), repo, "dev"),
            "a fork"
        );
        assert!(!is_promotion("dev", None, repo, "dev"), "a deleted fork");
        assert!(
            !is_promotion("feature", Some(repo), repo, "dev"),
            "another branch"
        );
    }

    #[test]
    fn the_delivery_body_has_the_contract_keys_before_a_read() {
        let body = serde_json::to_value(DeliveryCache::new(true).body()).expect("json");
        let mut keys: Vec<&str> = body
            .as_object()
            .expect("object")
            .keys()
            .map(String::as_str)
            .collect();
        keys.sort_unstable();
        assert_eq!(
            keys,
            vec![
                "error",
                "promotion",
                "pulls",
                "ref",
                "refreshed_at",
                "repo",
                "required_checks",
                "ruleset_url",
                "state"
            ],
            "the keys the UI codes against, in name order"
        );
        assert_eq!(body["state"], "loading");
        assert!(body["promotion"].is_null() && body["ref"].is_null());
    }

    #[tokio::test]
    async fn no_openbao_or_no_github_source_means_no_read_and_an_unconfigured_page() {
        let (bao_fake, bao) = fake_bao(Some(TOKEN)).await;
        let (_gh, api) = fake_github().await;
        let app = state(None, Some(source()));
        refresh_from(&app, &api).await;
        assert_eq!(app.delivery.read().await.body().state, "unconfigured");

        let app = state(Some(bao), None);
        refresh_from(&app, &api).await;
        assert_eq!(app.delivery.read().await.body().state, "unconfigured");

        assert_eq!(
            bao_fake.reads.load(Ordering::SeqCst),
            0,
            "an unconfigured page reads no token"
        );
    }

    #[tokio::test]
    async fn a_read_past_its_request_bound_is_an_error_not_a_partial_read() {
        let (_bao_fake, bao) = fake_bao(Some(TOKEN)).await;
        let (_gh, api) = fake_github().await;
        let http = reqwest::Client::new();
        // Ruleset, comparison, two lists: four. Then two requests for each of the four pull requests: eight.
        assert_eq!(
            read(&bao, &http, &api, &source(), 11)
                .await
                .expect_err("one request short"),
            FetchError::Bound
        );
        assert!(read(&bao, &http, &api, &source(), 12).await.is_ok());
    }

    #[tokio::test]
    async fn a_refresh_reads_the_delivery_state_and_serves_it() {
        let (_bao_fake, bao) = fake_bao(Some(TOKEN)).await;
        let (gh, api) = fake_github().await;
        let app = state(Some(bao), Some(source()));
        refresh_from(&app, &api).await;

        let body = app.delivery.read().await.body();
        let mut value = serde_json::to_value(&body).expect("json");
        assert!(value["refreshed_at"].is_string(), "{value}");
        value
            .as_object_mut()
            .expect("object")
            .remove("refreshed_at");
        let promotion_pull = json!({
            "number": 900,
            "title": "release: dev to main",
            "author": "jestyr27",
            "base": "main",
            "head": "dev",
            "draft": false,
            "url": "https://github.com/uhstray-io/agent-cloud/pull/900",
            "updated_at": "2026-11-08T05:00:00Z",
            "review": "none",
            "checks": {"passed": 2, "failed": 1, "pending": 1, "skipped": 0},
            "required": [
                {"name": "Static Analysis", "state": "passed"},
                {"name": "Security Scan", "state": "failed"},
                {"name": "Unit Tests", "state": "passed"},
                {"name": "Promotion source (dev -> main)", "state": "pending"},
            ],
        });
        let expected = json!({
            "state": "ok",
            "repo": "uhstray-io/agent-cloud",
            "ref": "dev",
            "required_checks": required_names(),
            "ruleset_url": "https://github.com/uhstray-io/agent-cloud/blob/dev/.github/rulesets/protect-main.json",
            "promotion": {
                "base": "main",
                "head": "dev",
                "ahead_by": 12,
                "behind_by": 0,
                "compare_url": "https://github.com/uhstray-io/agent-cloud/compare/main...dev",
                "pull": promotion_pull,
            },
            "pulls": [
                {
                    "number": 915,
                    "title": "docs: x",
                    "author": "jestyr27",
                    "base": "dev",
                    "head": "docs/x",
                    "draft": false,
                    "url": "https://github.com/uhstray-io/agent-cloud/pull/915",
                    "updated_at": "2026-11-08T05:00:00Z",
                    "review": "approved",
                    "checks": {"passed": 1, "failed": 0, "pending": 1, "skipped": 2},
                    "required": [],
                },
                {
                    "number": 916,
                    "title": "fix: y",
                    "author": "jestyr27",
                    "base": "dev",
                    "head": "fix/y",
                    "draft": true,
                    "url": "https://github.com/uhstray-io/agent-cloud/pull/916",
                    "updated_at": "2026-11-08T05:00:00Z",
                    "review": "changes_requested",
                    "checks": {"passed": 0, "failed": 0, "pending": 0, "skipped": 0},
                    "required": [],
                },
                {
                    "number": 901,
                    "title": "fix: from a fork",
                    "author": "contributor",
                    "base": "main",
                    "head": "dev",
                    "draft": false,
                    "url": "https://github.com/uhstray-io/agent-cloud/pull/901",
                    "updated_at": "2026-11-08T05:00:00Z",
                    "review": "none",
                    "checks": {"passed": 1, "failed": 0, "pending": 0, "skipped": 0},
                    "required": [
                        {"name": "Static Analysis", "state": "passed"},
                        {"name": "Security Scan", "state": "missing"},
                        {"name": "Unit Tests", "state": "missing"},
                        {"name": "Promotion source (dev -> main)", "state": "missing"},
                    ],
                },
                promotion_pull,
            ],
            "error": null,
        });
        assert_eq!(value, expected);

        let seen = gh.seen.lock().expect("seen").clone();
        assert!(
            seen.contains(&format!(
                "{REPO_PATH}contents/.github/rulesets/protect-main.json?ref=dev"
            )),
            "{seen:?}"
        );
        assert!(
            seen.contains(&format!("{REPO_PATH}compare/main...dev")),
            "{seen:?}"
        );
        assert!(
            seen.iter().any(|path| path.contains("base=main")
                && path.contains("state=open")
                && path.contains("per_page=30")),
            "{seen:?}"
        );
    }

    #[tokio::test]
    async fn a_failed_github_read_keeps_the_last_good_read_and_says_why_then_heals() {
        let (_bao_fake, bao) = fake_bao(Some(TOKEN)).await;
        let (gh, api) = fake_github().await;
        let app = state(Some(bao), Some(source()));
        refresh_from(&app, &api).await;
        let first = app.delivery.read().await.body();
        assert_eq!(first.state, "ok");

        // GitHub now refuses every request. The page keeps the read and names the status.
        gh.fail_with.store(500, Ordering::SeqCst);
        refresh_from(&app, &api).await;
        let stale = app.delivery.read().await.body();
        assert_eq!(stale.state, "ok", "the last good read is still served");
        assert_eq!(stale.pulls, first.pulls, "the pulls are kept, not cleared");
        assert_eq!(
            stale.refreshed_at, first.refreshed_at,
            "the stamp is the older read's"
        );
        assert_eq!(
            stale.error.as_deref(),
            Some("The newest delivery read failed: GitHub answered HTTP 500. Showing the last good read.")
        );

        gh.fail_with.store(0, Ordering::SeqCst);
        refresh_from(&app, &api).await;
        assert_eq!(app.delivery.read().await.body().error, None);
    }

    #[tokio::test]
    async fn the_token_never_reaches_a_body_or_an_error() {
        let (_bao_fake, bao) = fake_bao(Some(TOKEN)).await;
        let app = state(Some(bao), Some(source()));
        // Nothing listens on port 9, so the read fails before any answer.
        refresh_from(&app, "http://127.0.0.1:9").await;
        let body = app.delivery.read().await.body();
        let text = serde_json::to_string(&body).expect("json");
        assert_eq!(body.state, "error");
        assert!(
            body.error
                .as_deref()
                .is_some_and(|error| error.starts_with("The delivery read failed: ")),
            "{text}"
        );
        assert!(!text.contains(TOKEN), "{text}");
    }

    #[tokio::test]
    async fn members_get_403_signed_out_gets_401_and_platform_admins_get_the_answer() {
        let (_bao_fake, bao) = fake_bao(Some(TOKEN)).await;
        let (_gh, api) = fake_github().await;
        let app = state(Some(bao), Some(source()));
        refresh_from(&app, &api).await;

        for groups in [&["ops"][..], &["platform-admins-old", "users"][..]] {
            let (addr, cookie) = app_for(app.clone(), groups).await;
            let (status, body) = fetch(addr, "/v1/delivery", Some(&cookie)).await;
            assert_eq!(status, 403, "{groups:?}");
            assert_eq!(body, r#"{"error":"forbidden"}"#, "{groups:?}");
        }

        let (addr, _) = app_for(app.clone(), &["platform-admins"]).await;
        let (status, body) = fetch(addr, "/v1/delivery", None).await;
        assert_eq!(status, 401);
        assert_eq!(body, r#"{"error":"unauthenticated"}"#);

        let (addr, cookie) = app_for(app.clone(), &["platform-admins"]).await;
        let (status, body) = fetch(addr, "/v1/delivery", Some(&cookie)).await;
        assert_eq!(status, 200);
        let value: Value = serde_json::from_str(&body).expect("json");
        assert_eq!(value["state"], "ok");
        assert_eq!(value["ref"], "dev");
        assert_eq!(value["pulls"].as_array().map(Vec::len), Some(4));
        assert!(!body.contains(TOKEN), "{body}");
    }
}
