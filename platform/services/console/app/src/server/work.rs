//! Active Work: Semaphore tasks as work items, joined to the service-onboarding
//! registry. The decisions (lane map, skynet message, target service, joins,
//! timestamps) are pure functions with unit tests. The poller and the two GET
//! handlers only wrap them.
//!
//! Ordering is by Semaphore task id, not by `created`. Ids are auto-increment, and
//! Semaphore's RFC3339 strings do not compare safely as text: Go drops trailing
//! fractional zeros, so `…:00Z` would sort after `…:00.1Z`.

use std::collections::HashMap;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use dioxus::server::axum::extract::{Path, State};
use dioxus::server::axum::http::StatusCode;
use dioxus::server::axum::response::{IntoResponse, Response};
use dioxus::server::axum::Json;
use serde::{Deserialize, Serialize};
use tokio::time::MissedTickBehavior;
use tower_sessions::cookie::time::OffsetDateTime;
use tower_sessions::Session;

use super::api::{error, unauthenticated};
use super::auth;
use super::config::SemaphoreConfig;
use super::scope::is_platform_admin;
use super::semaphore::{self, Task};
use super::SharedState;

/// How often the poller asks Semaphore for the task list (plan 17 D18).
pub const POLL_INTERVAL: Duration = Duration::from_secs(10);

/// One step of the registry file (`CONSOLE_WORKFLOW_FILE`, rendered from `registry.yml`).
#[derive(Debug, Clone, Deserialize)]
pub struct Step {
    pub id: String,
    pub order: u32,
    pub owner: String,
    pub label: String,
}

/// One Semaphore task as the console shows it. Built only from the fields below,
/// so nothing else of a task (environment, arguments, output) can reach a client.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct WorkItem {
    /// Semaphore task id. Not serialized: `key` carries it for the client.
    #[serde(skip)]
    pub id: u64,
    pub key: String,
    pub title: String,
    pub lane: &'static str,
    pub status: String,
    pub role: Option<String>,
    pub launched_by: String,
    pub run_id: Option<String>,
    pub step: Option<String>,
    pub service: Option<String>,
    pub created: String,
    pub started: Option<String>,
    pub ended: Option<String>,
    pub receipt_url: String,
    /// The job this task repeats: template, plus service when known. See `stream_key`.
    pub stream: String,
    /// True on the newest task of its stream within the fetched window. Only these get a strip.
    pub latest_in_stream: bool,
    /// Tasks of the stream within the fetched window. The same value on each of them.
    pub stream_runs: u32,
}

/// One earlier run of a job, as the details panel lists it under "Recent runs".
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct HistoryEntry {
    pub key: String,
    pub lane: &'static str,
    pub status: String,
    pub launched_by: String,
    pub created: String,
    pub started: Option<String>,
    pub ended: Option<String>,
    pub receipt_url: String,
}

/// How many runs of one job `GET /v1/work/{key}` lists, the item's own run included.
pub const HISTORY_LIMIT: usize = 10;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Source {
    Ok,
    Unconfigured,
    Error,
}

/// What `/v1/work` serves: the list body itself. The poller writes it; the handlers only read it.
#[derive(Debug, Serialize)]
pub struct WorkCache {
    pub source: Source,
    pub error: Option<String>,
    pub updated_at: Option<String>,
    pub items: Vec<WorkItem>,
}

impl WorkCache {
    /// No `CONSOLE_SEMAPHORE_*` configuration: nothing is polled.
    pub fn unconfigured() -> Self {
        Self::with_source(Source::Unconfigured)
    }

    /// Configured, first poll not finished. Source `ok` with no items and
    /// `updated_at: null`. A null timestamp is how a client tells "no data yet" from "polled, empty".
    pub fn awaiting_first_poll() -> Self {
        Self::with_source(Source::Ok)
    }

    fn with_source(source: Source) -> Self {
        Self {
            source,
            error: None,
            updated_at: None,
            items: Vec::new(),
        }
    }

    /// A successful poll replaces the items and stamps the time.
    pub fn record_ok(&mut self, items: Vec<WorkItem>, polled_at: String) {
        self.source = Source::Ok;
        self.error = None;
        self.updated_at = Some(polled_at);
        self.items = items;
    }

    /// A failed poll keeps the last good items and their timestamp. They are stale,
    /// and `error` says so.
    pub fn record_error(&mut self, error: String) {
        self.source = Source::Error;
        self.error = Some(error);
    }
}

/// One run's steps, in registry order, each with its latest task's lane.
#[derive(Debug, PartialEq, Serialize)]
pub struct Run {
    pub run_id: String,
    pub service: Option<String>,
    pub steps: Vec<RunStep>,
}

#[derive(Debug, PartialEq, Serialize)]
pub struct RunStep {
    pub id: String,
    pub order: u32,
    pub label: String,
    pub owner: String,
    /// A lane, or `not_started` when no task of the run carries this step.
    pub status: &'static str,
    pub key: Option<String>,
}

/// Semaphore status to lane (contract, Semaphore read). An unknown status is
/// `blocked`, so a status Semaphore adds later is shown rather than hidden.
pub fn lane_for(status: &str) -> &'static str {
    match status {
        "waiting" | "starting" | "confirmed" => "queued",
        "running" | "stopping" => "running",
        "waiting_confirmation" => "awaiting",
        "rejected" => "blocked",
        "error" | "stopped" => "failed",
        "success" => "done",
        _ => "blocked",
    }
}

/// `skynet <run_id> <step_id>`, exactly: three tokens, single spaces, no other
/// whitespace. Anything else was not launched by skynet.
pub fn parse_skynet_message(message: &str) -> Option<(String, String)> {
    let parts: Vec<&str> = message.split(' ').collect();
    let (prefix, run, step) = match parts.as_slice() {
        [prefix, run, step] => (*prefix, *run, *step),
        _ => return None,
    };
    let plain = |token: &str| !token.is_empty() && !token.chars().any(char::is_whitespace);
    (prefix == "skynet" && plain(run) && plain(step)).then(|| (run.to_string(), step.to_string()))
}

/// `target_service` from a task's environment JSON, and nothing else. Only that key
/// is bound to a field, so serde discards every other key, secret-shaped or not.
pub fn target_service(environment: &str) -> Option<String> {
    #[derive(Deserialize)]
    struct OnlyTarget {
        target_service: Option<String>,
    }
    let parsed: OnlyTarget = serde_json::from_str(environment).ok()?;
    parsed
        .target_service
        .filter(|service| !service.trim().is_empty())
}

/// The stream a task belongs to: its template, and its service when one is known, so
/// one job's repeat runs group together. A task with no template id is a stream of its
/// own. Never keyed on the title: two templates can share one.
pub fn stream_key(template_id: Option<i64>, service: Option<&str>, task_id: u64) -> String {
    match (template_id, service) {
        (Some(template), Some(service)) => format!("tpl-{template}:{service}"),
        (Some(template), None) => format!("tpl-{template}"),
        (None, _) => format!("task-{task_id}"),
    }
}

/// Who launched a task, first match wins: a skynet message, a person (a non-empty user
/// name), a schedule, an integration. Anything else is `unknown`.
pub fn launcher(
    skynet: bool,
    user_name: Option<&str>,
    schedule_id: Option<i64>,
    integration_id: Option<i64>,
) -> String {
    if skynet {
        "skynet".to_string()
    } else if let Some(name) = user_name.filter(|name| !name.is_empty()) {
        name.to_string()
    } else if schedule_id.is_some() {
        "schedule".to_string()
    } else if integration_id.is_some() {
        "integration".to_string()
    } else {
        "unknown".to_string()
    }
}

/// Maps Semaphore tasks to items, newest first (highest id first), with each item's
/// stream marked within this list.
pub fn build_items(
    mut tasks: Vec<Task>,
    steps: &[Step],
    config: &SemaphoreConfig,
) -> Vec<WorkItem> {
    tasks.sort_by_key(|task| std::cmp::Reverse(task.id));
    let mut items: Vec<WorkItem> = tasks
        .iter()
        .map(|task| build_item(task, steps, &config.ui_url, config.project))
        .collect();
    mark_streams(&mut items);
    items
}

/// Sets `stream_runs` and `latest_in_stream` on every item. Decided by id, not by the
/// order of `items`: the highest id of a stream is its latest run.
pub fn mark_streams(items: &mut [WorkItem]) {
    let mut runs: HashMap<String, u32> = HashMap::new();
    let mut newest: HashMap<String, u64> = HashMap::new();
    for item in items.iter() {
        *runs.entry(item.stream.clone()).or_insert(0) += 1;
        newest
            .entry(item.stream.clone())
            .and_modify(|id| *id = (*id).max(item.id))
            .or_insert(item.id);
    }
    for item in items.iter_mut() {
        item.stream_runs = runs[&item.stream];
        item.latest_in_stream = newest[&item.stream] == item.id;
    }
}

/// Up to `limit` runs of `stream`, newest first, the item's own run among them. Sorted
/// here by id, so the answer does not depend on the order `items` is in.
pub fn stream_history(items: &[WorkItem], stream: &str, limit: usize) -> Vec<HistoryEntry> {
    let mut members: Vec<&WorkItem> = items.iter().filter(|item| item.stream == stream).collect();
    members.sort_by_key(|item| std::cmp::Reverse(item.id));
    members
        .into_iter()
        .take(limit)
        .map(|item| HistoryEntry {
            key: item.key.clone(),
            lane: item.lane,
            status: item.status.clone(),
            launched_by: item.launched_by.clone(),
            created: item.created.clone(),
            started: item.started.clone(),
            ended: item.ended.clone(),
            receipt_url: item.receipt_url.clone(),
        })
        .collect()
}

/// One task as an item. A skynet-launched task gets its run, step and registry owner
/// as role. Title is the template name, plus the service when one is known. A lone
/// item is its own stream's only and latest run; `build_items` corrects the counts.
pub fn build_item(task: &Task, steps: &[Step], ui_url: &str, project: u32) -> WorkItem {
    let skynet = parse_skynet_message(task.message.as_deref().unwrap_or(""));
    let service = task.environment.as_deref().and_then(target_service);
    let template = task.tpl_alias.clone().unwrap_or_default();
    let title = match &service {
        Some(service) => format!("{template} — {service}"),
        None => template,
    };
    let stream = stream_key(task.template_id, service.as_deref(), task.id);
    let launched_by = launcher(
        skynet.is_some(),
        task.user_name.as_deref(),
        task.schedule_id,
        task.integration_id,
    );
    let (run_id, step) = match skynet {
        Some((run_id, step)) => (Some(run_id), Some(step)),
        None => (None, None),
    };
    let role = step
        .as_deref()
        .and_then(|id| steps.iter().find(|candidate| candidate.id == id))
        .map(|candidate| candidate.owner.clone());
    WorkItem {
        id: task.id,
        key: format!("sem-{}", task.id),
        title,
        lane: lane_for(&task.status),
        status: task.status.clone(),
        role,
        launched_by,
        run_id,
        step,
        service,
        created: task.created.clone(),
        started: task.start.clone(),
        ended: task.end.clone(),
        receipt_url: format!(
            "{}/project/{project}/history?t={}",
            ui_url.trim_end_matches('/'),
            task.id
        ),
        stream,
        latest_in_stream: true,
        stream_runs: 1,
    }
}

/// The run's registry steps in `order`. Each takes the lane of the run's newest task
/// for that step (highest id). A step with no task is `not_started`.
pub fn run_detail(items: &[WorkItem], run_id: &str, steps: &[Step]) -> Run {
    let in_run: Vec<&WorkItem> = items
        .iter()
        .filter(|item| item.run_id.as_deref() == Some(run_id))
        .collect();
    let service = in_run.iter().find_map(|item| item.service.clone());
    let mut ordered: Vec<&Step> = steps.iter().collect();
    ordered.sort_by_key(|step| step.order);
    let run_steps: Vec<RunStep> = ordered
        .into_iter()
        .map(|step| {
            let latest = in_run
                .iter()
                .filter(|item| item.step.as_deref() == Some(step.id.as_str()))
                .max_by_key(|item| item.id);
            RunStep {
                id: step.id.clone(),
                order: step.order,
                label: step.label.clone(),
                owner: step.owner.clone(),
                status: latest.map_or("not_started", |item| item.lane),
                key: latest.map(|item| item.key.clone()),
            }
        })
        .collect();
    Run {
        run_id: run_id.to_string(),
        service,
        steps: run_steps,
    }
}

/// Reads the registry steps file. A configured file that cannot be read fails startup, as the scope file does.
pub fn load_workflow(path: &std::path::Path) -> Result<Vec<Step>, String> {
    let text = std::fs::read_to_string(path).map_err(|err| {
        format!(
            "CONSOLE_WORKFLOW_FILE: cannot read {}: {err}",
            path.display()
        )
    })?;
    serde_json::from_str(&text).map_err(|err| format!("CONSOLE_WORKFLOW_FILE: invalid JSON: {err}"))
}

/// The current time as RFC3339 UTC.
pub fn rfc3339_now() -> String {
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |elapsed| elapsed.as_secs() as i64);
    rfc3339_from_unix(secs)
}

/// `YYYY-MM-DDTHH:MM:SSZ` for a Unix time, formatted from the date parts.
pub fn rfc3339_from_unix(secs: i64) -> String {
    let at = OffsetDateTime::from_unix_timestamp(secs).unwrap_or(OffsetDateTime::UNIX_EPOCH);
    format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}Z",
        at.year(),
        at.month() as u8,
        at.day(),
        at.hour(),
        at.minute(),
        at.second()
    )
}

/// The platform-admin gate (plan 17 D19), checked after the session. A session
/// without the group gets the same 403 whether the key exists or not.
pub(super) async fn admin_only(session: &Session) -> Result<(), Response> {
    let Some(user) = auth::current_user(session).await else {
        return Err(unauthenticated());
    };
    if is_platform_admin(&user.groups) {
        Ok(())
    } else {
        Err(error(StatusCode::FORBIDDEN, "forbidden"))
    }
}

/// `GET /v1/work`: the cache, newest first.
pub async fn list_work(State(app): State<SharedState>, session: Session) -> Response {
    if let Err(response) = admin_only(&session).await {
        return response;
    }
    let cache = app.work.read().await;
    Json(&*cache).into_response()
}

#[derive(Serialize)]
struct ItemBody<'a> {
    item: &'a WorkItem,
    run: Option<Run>,
    stream_history: Vec<HistoryEntry>,
}

/// `GET /v1/work/{key}`: one item, its skynet run's registry steps when it has a run,
/// and the newest runs of its job.
pub async fn get_work_item(
    State(app): State<SharedState>,
    session: Session,
    Path(key): Path<String>,
) -> Response {
    if let Err(response) = admin_only(&session).await {
        return response;
    }
    let cache = app.work.read().await;
    let Some(item) = cache.items.iter().find(|item| item.key == key) else {
        return error(StatusCode::NOT_FOUND, "unknown_work");
    };
    let run = item
        .run_id
        .as_deref()
        .map(|run_id| run_detail(&cache.items, run_id, &app.workflow));
    let history = stream_history(&cache.items, &item.stream, HISTORY_LIMIT);
    Json(ItemBody {
        item,
        run,
        stream_history: history,
    })
    .into_response()
}

/// Starts the poll when Semaphore is configured. The first poll runs at once; a
/// missed tick is skipped, not replayed. The cache lock is held only to store a result.
pub fn spawn_poller(state: SharedState) {
    let Some(config) = state.config.semaphore.clone() else {
        return;
    };
    tokio::spawn(async move {
        let mut ticker = tokio::time::interval(POLL_INTERVAL);
        ticker.set_missed_tick_behavior(MissedTickBehavior::Delay);
        let mut last_error: Option<String> = None;
        loop {
            ticker.tick().await;
            let polled = semaphore::fetch_last(&state.http, &config)
                .await
                .map(|tasks| build_items(tasks, &state.workflow, &config));
            let mut cache = state.work.write().await;
            match polled {
                Ok(items) => {
                    cache.record_ok(items, rfc3339_now());
                    last_error = None;
                }
                Err(err) => {
                    let text = err.to_string();
                    // Log a failure once, not on every 10 s tick while it lasts.
                    if last_error.as_deref() != Some(text.as_str()) {
                        eprintln!("console: work poll failed: {text}");
                    }
                    cache.record_error(text.clone());
                    last_error = Some(text);
                }
            }
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    fn step(id: &str, order: u32, owner: &str, label: &str) -> Step {
        Step {
            id: id.into(),
            order,
            owner: owner.into(),
            label: label.into(),
        }
    }

    fn registry() -> Vec<Step> {
        vec![
            step("step-b", 2, "service-agent", "Build"),
            step("step-a", 1, "infra-agent", "Plan"),
            step("step-c", 3, "security-agent", "Verify"),
        ]
    }

    fn task(id: u64, status: &str, message: Option<&str>, env: Option<&str>) -> Task {
        serde_json::from_value(serde_json::json!({
            "id": id,
            "status": status,
            "created": "2026-10-26T12:00:00Z",
            "message": message,
            "tpl_alias": "Deploy tududi",
            "user_name": "svc-console",
            "environment": env,
        }))
        .expect("task fixture")
    }

    #[test]
    fn lane_map_covers_every_contract_status() {
        let cases = [
            ("waiting", "queued"),
            ("starting", "queued"),
            ("confirmed", "queued"),
            ("running", "running"),
            ("stopping", "running"),
            ("waiting_confirmation", "awaiting"),
            ("rejected", "blocked"),
            ("error", "failed"),
            ("stopped", "failed"),
            ("success", "done"),
        ];
        for (status, lane) in cases {
            assert_eq!(lane_for(status), lane, "{status}");
        }
    }

    #[test]
    fn unknown_status_is_blocked_not_hidden() {
        assert_eq!(lane_for("paused-by-moon"), "blocked");
        assert_eq!(lane_for(""), "blocked");
    }

    #[test]
    fn skynet_message_must_be_exactly_three_single_spaced_tokens() {
        assert_eq!(
            parse_skynet_message("skynet service_onboarding:0a1b step-2"),
            Some(("service_onboarding:0a1b".to_string(), "step-2".to_string()))
        );
        for bad in [
            "",
            "skynet",
            "skynet run",
            "skynet run step extra",
            "skynet  run step",
            "skynet run\tstep",
            "skynet run step\n",
            "Skynet run step",
            "skynet  ",
            "deploy skynet run step",
            "skynet run ",
            " skynet run step",
        ] {
            assert_eq!(parse_skynet_message(bad), None, "{bad:?}");
        }
    }

    #[test]
    fn target_service_reads_only_that_key() {
        let env = r#"{"SECRET_TOKEN":"hunter2","target_service":"tududi","password":"p"}"#;
        assert_eq!(target_service(env).as_deref(), Some("tududi"));
        assert_eq!(target_service(r#"{"secret_target_service":"x"}"#), None);
        assert_eq!(target_service(r#"{"target_service":""}"#), None);
        assert_eq!(target_service(r#"{"target_service":null}"#), None);
        assert_eq!(target_service(r#"{"target_service":7}"#), None);
        assert_eq!(target_service(""), None);
        assert_eq!(target_service("{}"), None);
    }

    #[test]
    fn skynet_task_gets_run_step_role_and_service_title() {
        let t = task(
            40,
            "running",
            Some("skynet service_onboarding:abc step-b"),
            Some(r#"{"target_service":"tududi"}"#),
        );
        let item = build_item(&t, &registry(), "https://semaphore.test:8443/", 7);
        assert_eq!(item.key, "sem-40");
        assert_eq!(item.title, "Deploy tududi — tududi");
        assert_eq!(item.lane, "running");
        assert_eq!(item.launched_by, "skynet");
        assert_eq!(item.run_id.as_deref(), Some("service_onboarding:abc"));
        assert_eq!(item.step.as_deref(), Some("step-b"));
        assert_eq!(item.role.as_deref(), Some("service-agent"));
        assert_eq!(item.service.as_deref(), Some("tududi"));
        assert_eq!(
            item.receipt_url,
            "https://semaphore.test:8443/project/7/history?t=40"
        );
    }

    #[test]
    fn human_task_has_no_run_role_or_service_and_shows_the_user() {
        let t = task(9, "success", None, None);
        let item = build_item(&t, &registry(), "https://ui.test", 1);
        assert_eq!(item.title, "Deploy tududi");
        assert_eq!(item.launched_by, "svc-console");
        assert_eq!(item.run_id, None);
        assert_eq!(item.step, None);
        assert_eq!(item.role, None);
        assert_eq!(item.service, None);
        assert_eq!(item.lane, "done");
    }

    #[test]
    fn missing_user_and_unknown_step_fall_back_without_inventing_a_role() {
        let mut t = task(10, "waiting", Some("skynet run ghost-step"), None);
        t.user_name = None;
        let item = build_item(&t, &registry(), "https://ui.test", 1);
        assert_eq!(item.role, None, "step not in the registry has no owner");
        assert_eq!(item.step.as_deref(), Some("ghost-step"));

        let mut human = task(11, "waiting", None, None);
        human.user_name = Some(String::new());
        assert_eq!(
            build_item(&human, &registry(), "https://ui.test", 1).launched_by,
            "unknown"
        );
    }

    #[test]
    fn build_items_is_newest_first_by_id() {
        let config = SemaphoreConfig {
            api_url: "https://api.test".into(),
            ui_url: "https://ui.test".into(),
            project: 1,
            token: crate::server::config::Secret::new("t"),
        };
        let tasks = vec![
            task(3, "success", None, None),
            task(11, "success", None, None),
            task(5, "success", None, None),
        ];
        let keys: Vec<String> = build_items(tasks, &registry(), &config)
            .into_iter()
            .map(|item| item.key)
            .collect();
        assert_eq!(keys, ["sem-11", "sem-5", "sem-3"]);
    }

    fn item_for(id: u64, status: &str, run: &str, step: &str) -> WorkItem {
        let message = format!("skynet {run} {step}");
        build_item(
            &task(
                id,
                status,
                Some(message.as_str()),
                Some(r#"{"target_service":"tududi"}"#),
            ),
            &registry(),
            "https://ui.test",
            1,
        )
    }

    #[test]
    fn run_detail_lists_every_step_in_registry_order_with_not_started() {
        let items = vec![
            item_for(20, "success", "run-1", "step-a"),
            item_for(21, "running", "run-1", "step-b"),
            item_for(30, "success", "run-2", "step-c"),
        ];
        let run = run_detail(&items, "run-1", &registry());
        assert_eq!(run.run_id, "run-1");
        assert_eq!(run.service.as_deref(), Some("tududi"));
        let ids: Vec<&str> = run.steps.iter().map(|s| s.id.as_str()).collect();
        assert_eq!(ids, ["step-a", "step-b", "step-c"]);
        assert_eq!(run.steps[0].status, "done");
        assert_eq!(run.steps[0].key.as_deref(), Some("sem-20"));
        assert_eq!(run.steps[1].status, "running");
        assert_eq!(
            run.steps[2].status, "not_started",
            "run-2 task is not in run-1"
        );
        assert_eq!(run.steps[2].key, None);
        assert_eq!(run.steps[2].owner, "security-agent");
    }

    #[test]
    fn run_detail_takes_the_newest_task_per_step_whatever_the_input_order() {
        // An earlier failed attempt (id 20) and a retry (id 25) on the same step.
        let items = vec![
            item_for(20, "error", "run-1", "step-b"),
            item_for(25, "success", "run-1", "step-b"),
        ];
        let run = run_detail(&items, "run-1", &registry());
        let build = run.steps.iter().find(|s| s.id == "step-b").expect("step");
        assert_eq!(build.status, "done");
        assert_eq!(build.key.as_deref(), Some("sem-25"));

        let reversed: Vec<WorkItem> = items.into_iter().rev().collect();
        let run = run_detail(&reversed, "run-1", &registry());
        let build = run.steps.iter().find(|s| s.id == "step-b").expect("step");
        assert_eq!(build.key.as_deref(), Some("sem-25"));
    }

    #[test]
    fn run_detail_without_registry_has_no_steps() {
        let items = vec![item_for(20, "success", "run-1", "step-a")];
        let run = run_detail(&items, "run-1", &[]);
        assert!(run.steps.is_empty());
        assert_eq!(run.service.as_deref(), Some("tududi"));
    }

    /// A failed task of one template, for the stream tests. No skynet message.
    fn templated(id: u64, template_id: i64, environment: Option<&str>) -> WorkItem {
        let task: Task = serde_json::from_value(serde_json::json!({
            "id": id,
            "status": "error",
            "created": "2026-10-26T12:00:00Z",
            "template_id": template_id,
            "tpl_alias": "Check o11y Liveness (Dev)",
            "environment": environment,
        }))
        .expect("task fixture");
        build_item(&task, &registry(), "https://ui.test", 1)
    }

    #[test]
    fn stream_key_is_template_and_service_and_a_lone_task_without_a_template() {
        assert_eq!(stream_key(Some(3), Some("tududi"), 9), "tpl-3:tududi");
        assert_eq!(stream_key(Some(3), None, 9), "tpl-3");
        assert_eq!(stream_key(None, Some("tududi"), 9), "task-9");
        assert_eq!(stream_key(None, None, 9), "task-9");
    }

    #[test]
    fn launcher_takes_the_first_source_that_applies() {
        assert_eq!(launcher(true, Some("stray"), Some(4), Some(2)), "skynet");
        assert_eq!(launcher(false, Some("stray"), Some(4), Some(2)), "stray");
        assert_eq!(
            launcher(false, Some(""), Some(4), Some(2)),
            "schedule",
            "an empty user name is no user"
        );
        assert_eq!(launcher(false, None, None, Some(2)), "integration");
        assert_eq!(launcher(false, None, None, None), "unknown");
    }

    #[test]
    fn scheduled_task_is_labelled_schedule_not_unknown() {
        let task: Task = serde_json::from_value(serde_json::json!({
            "id": 60,
            "status": "success",
            "template_id": 2,
            "schedule_id": 4,
            "user_name": null,
        }))
        .expect("task fixture");
        let item = build_item(&task, &registry(), "https://ui.test", 1);
        assert_eq!(item.launched_by, "schedule");
        assert_eq!(
            item.stream, "tpl-2",
            "no service: keyed by the template alone"
        );
    }

    #[test]
    fn mark_streams_flags_the_highest_id_whatever_the_order() {
        // Shuffled on purpose: the latest flag must follow the id, not the position.
        let mut items = vec![
            templated(12, 5, None),
            templated(40, 6, None),
            templated(7, 5, None),
            templated(31, 5, None),
            templated(22, 6, Some(r#"{"target_service":"tududi"}"#)),
        ];
        mark_streams(&mut items);
        let by = |key: &str| items.iter().find(|item| item.key == key).expect("item");
        for key in ["sem-12", "sem-7", "sem-31"] {
            assert_eq!(by(key).stream, "tpl-5");
            assert_eq!(by(key).stream_runs, 3, "{key}");
            assert_eq!(by(key).latest_in_stream, key == "sem-31", "{key}");
        }
        assert_eq!(by("sem-40").stream, "tpl-6");
        assert!(by("sem-40").latest_in_stream);
        assert_eq!(by("sem-40").stream_runs, 1);
        assert_eq!(
            by("sem-22").stream,
            "tpl-6:tududi",
            "a service splits one template's stream"
        );
        assert!(by("sem-22").latest_in_stream);
    }

    #[test]
    fn stream_history_is_newest_first_capped_and_one_stream_only() {
        // Twelve runs of one job, interleaved with a second job, in no particular order.
        let mut items: Vec<WorkItem> = Vec::new();
        for id in [4, 1, 9, 12, 2, 7, 11, 3, 6, 10, 5, 8] {
            items.push(templated(id, 5, None));
        }
        items.push(templated(50, 6, None));
        items.push(templated(51, 6, None));
        let history = stream_history(&items, "tpl-5", HISTORY_LIMIT);
        let keys: Vec<&str> = history.iter().map(|entry| entry.key.as_str()).collect();
        assert_eq!(
            keys,
            [
                "sem-12", "sem-11", "sem-10", "sem-9", "sem-8", "sem-7", "sem-6", "sem-5", "sem-4",
                "sem-3"
            ]
        );
        assert_eq!(history[0].lane, "failed");
        let other_job = stream_history(&items, "tpl-6", HISTORY_LIMIT);
        let other: Vec<&str> = other_job.iter().map(|entry| entry.key.as_str()).collect();
        assert_eq!(other, ["sem-51", "sem-50"]);
        assert!(stream_history(&items, "tpl-9", HISTORY_LIMIT).is_empty());
    }

    #[test]
    fn rfc3339_formats_the_epoch_and_a_leap_day() {
        assert_eq!(rfc3339_from_unix(0), "1970-01-01T00:00:00Z");
        // 2028-02-29T12:34:56Z, checked with `date -u -r 1835440496`.
        assert_eq!(rfc3339_from_unix(1_835_440_496), "2028-02-29T12:34:56Z");
    }

    #[test]
    fn rfc3339_now_has_the_expected_shape() {
        let now = rfc3339_now();
        assert_eq!(now.len(), 20, "{now}");
        assert!(now.ends_with('Z') && now.as_bytes()[10] == b'T', "{now}");
    }

    #[test]
    fn cache_keeps_stale_items_on_error() {
        let mut cache = WorkCache::awaiting_first_poll();
        assert_eq!(cache.source, Source::Ok);
        assert_eq!(cache.updated_at, None);
        cache.record_ok(vec![item_for(1, "success", "r", "step-a")], "t1".into());
        cache.record_error("Semaphore answered HTTP 500".into());
        assert_eq!(cache.source, Source::Error);
        assert_eq!(cache.error.as_deref(), Some("Semaphore answered HTTP 500"));
        assert_eq!(cache.updated_at.as_deref(), Some("t1"));
        assert_eq!(cache.items.len(), 1);
    }

    #[test]
    fn load_workflow_reads_the_registry_shape_and_names_the_setting_on_error() {
        let dir = std::env::temp_dir().join(format!("console-work-{}", std::process::id()));
        std::fs::create_dir_all(&dir).expect("dir");
        let good = dir.join("workflow.json");
        std::fs::write(
            &good,
            r#"[{"id":"step-a","order":1,"owner":"infra-agent","label":"Plan"}]"#,
        )
        .expect("write");
        let steps = load_workflow(&good).expect("valid");
        assert_eq!(steps[0].owner, "infra-agent");

        let bad = dir.join("broken.json");
        std::fs::write(&bad, "[{").expect("write");
        let err = load_workflow(&bad).expect_err("must fail");
        assert!(err.starts_with("CONSOLE_WORKFLOW_FILE:"), "{err}");
        let missing = load_workflow(&dir.join("absent.json")).expect_err("must fail");
        assert!(
            missing.starts_with("CONSOLE_WORKFLOW_FILE: cannot read"),
            "{missing}"
        );
        // One plain delete per file, then the directory: no recursive removal.
        let _ = std::fs::remove_file(&good);
        let _ = std::fs::remove_file(&bad);
        let _ = std::fs::remove_dir(&dir);
    }
}
