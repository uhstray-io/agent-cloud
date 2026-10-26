//! The details panel for one Active Work item: its fields, the receipt link and,
//! for a skynet run, a vertical stepper of the run's steps. Minimized, the panel
//! becomes a summary rail that lists the work per lane.
//!
//! The stepper comes from `GET /v1/work/{key}`. The panel fetches it when the
//! item is picked, and again when the item's lane, status or step changes.

use dioxus::prelude::*;
use serde::Deserialize;

use super::api;
use super::icons::icon;
use super::shell::Ctx;
use super::work::{age_text, find_item, item_stamp, Lane, Tone, WorkItem};

/// Body of `GET /v1/work/{key}`. `run` is null for a task no skynet run started.
/// `stream_history` is the job's newest runs, newest first, this one included.
#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct WorkDetail {
    pub item: WorkItem,
    pub run: Option<RunDetail>,
    pub stream_history: Vec<HistoryEntry>,
}

/// One run of the job, as "Recent runs" draws it. The server sends more fields; the
/// panel reads only these.
#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct HistoryEntry {
    pub key: String,
    pub lane: Lane,
    pub created: String,
    pub started: Option<String>,
    pub receipt_url: String,
}

/// One row of "Recent runs", decided here so the markup only draws it.
#[derive(Debug, Clone, PartialEq)]
pub struct RecentRow {
    pub key: String,
    pub shape: &'static str,
    pub tone: Tone,
    pub word: &'static str,
    pub age: Option<String>,
    pub receipt_url: String,
    /// True for the run this panel is open on.
    pub current: bool,
}

/// The rows of "Recent runs" in the order given, which is newest first. The age counts
/// from the start when there is one, else from creation, as the item's own age does.
pub fn recent_rows(
    history: &[HistoryEntry],
    current_key: &str,
    now_ms: Option<i64>,
) -> Vec<RecentRow> {
    history
        .iter()
        .map(|entry| {
            let (shape, tone) = entry.lane.look();
            RecentRow {
                key: entry.key.clone(),
                shape,
                tone,
                word: entry.lane.label(),
                age: age_text(
                    now_ms,
                    entry.started.as_deref().unwrap_or(entry.created.as_str()),
                ),
                receipt_url: entry.receipt_url.clone(),
                current: entry.key == current_key,
            }
        })
        .collect()
}

/// One skynet run: its id, its service, and every registry step in order.
#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct RunDetail {
    pub run_id: String,
    pub service: Option<String>,
    pub steps: Vec<StepView>,
}

/// One registry step. `status` is a lane value, or `not_started`.
#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct StepView {
    pub id: String,
    pub order: u32,
    pub label: String,
    pub owner: Option<String>,
    pub status: String,
    pub key: Option<String>,
}

/// One row of the stepper, already decided, so the markup only draws it.
#[derive(Debug, Clone, PartialEq)]
pub struct StepRow {
    pub number: u32,
    pub label: String,
    pub owner: String,
    pub shape: &'static str,
    pub tone: Tone,
    pub word: &'static str,
    pub current: bool,
    pub not_started: bool,
    pub spins: bool,
    pub task: Option<String>,
}

/// The stepper rows in `order`. A step whose status is not a lane (`not_started`,
/// or anything this console does not know) is greyed and reads "Not started".
/// `current` is the item's own step id, which is the one the stepper highlights.
pub fn step_rows(steps: &[StepView], current: Option<&str>) -> Vec<StepRow> {
    let mut ordered: Vec<&StepView> = steps.iter().collect();
    ordered.sort_by_key(|step| step.order);
    ordered
        .into_iter()
        .map(|step| {
            let lane = Lane::from_wire(&step.status);
            let (shape, tone, word, spins) = match lane {
                Some(lane) => {
                    let (shape, tone) = lane.look();
                    (shape, tone, lane.label(), lane == Lane::Running)
                }
                None => ("circle-dashed", Tone::Idle, "Not started", false),
            };
            StepRow {
                number: step.order,
                label: step.label.clone(),
                owner: step.owner.clone().unwrap_or_else(|| "\u{2014}".to_string()),
                shape,
                tone,
                word,
                current: current == Some(step.id.as_str()),
                not_started: lane.is_none(),
                spins,
                task: step.key.clone(),
            }
        })
        .collect()
}

/// The fetch the panel has for one item: its key, and the stamp it was fetched at.
type Fetched = (String, String);

/// What the panel knows about the run.
#[derive(Debug, Clone, PartialEq)]
enum DetailState {
    Loading,
    Ready(Box<WorkDetail>),
    Missing,
    Failed,
}

/// Closes the panel: nothing is selected, and focus goes back to the strip.
fn close_details(mut selected: Signal<Option<String>>, key: &str) {
    selected.set(None);
    api::focus(&format!("strip-{key}"));
}

/// The "Recent runs" rows for a loaded panel. Empty unless the job ran more than once.
fn recent_for(state: &DetailState, current_key: &str, now_ms: Option<i64>) -> Vec<RecentRow> {
    match state {
        DetailState::Ready(found) if found.stream_history.len() > 1 => {
            recent_rows(&found.stream_history, current_key, now_ms)
        }
        _ => Vec::new(),
    }
}

/// Fetches one item's run. An answer for an item that is no longer the one
/// asked about (the selection moved, or its stamp changed) is dropped.
async fn load_detail(
    want: Fetched,
    fetched: Signal<Option<Fetched>>,
    mut detail: Signal<DetailState>,
) {
    let path = format!("/v1/work/{}", api::segment(&want.0));
    let reply = api::request("GET", &path, None).await;
    if fetched.peek().as_ref() != Some(&want) {
        return;
    }
    let next = match reply.status {
        200 => api::parse::<WorkDetail>(&reply).map_or(DetailState::Failed, |found| {
            DetailState::Ready(Box::new(found))
        }),
        404 => DetailState::Missing,
        _ => DetailState::Failed,
    };
    detail.set(next);
}

/// The details panel for the selected item. It is a labelled region that takes
/// focus when it opens; Escape or Close deselects the item.
#[component]
pub fn Details(
    selected: Signal<Option<String>>,
    minimized: Signal<bool>,
    now_ms: Option<i64>,
) -> Element {
    let ctx = use_context::<Ctx>();
    let mut minimized = minimized;
    let mut fetched = use_signal(|| None::<Fetched>);
    let mut detail = use_signal(|| DetailState::Loading);

    use_effect(move || {
        let Some(key) = selected() else {
            return;
        };
        let stamp = find_item(&ctx.work.read(), &key)
            .map(item_stamp)
            .unwrap_or_default();
        let want = (key, stamp);
        if fetched.peek().as_ref() == Some(&want) {
            return;
        }
        fetched.set(Some(want.clone()));
        detail.set(DetailState::Loading);
        spawn(load_detail(want, fetched, detail));
    });

    let Some(key) = selected() else {
        return rsx! {};
    };
    let listed = find_item(&ctx.work.read(), &key).cloned();
    let state = detail();
    let item = listed.or_else(|| match &state {
        DetailState::Ready(found) => Some(found.item.clone()),
        _ => None,
    });
    let title = item
        .as_ref()
        .map_or_else(|| key.clone(), |item| item.title.clone());
    let receipt = item.as_ref().map(|item| item.receipt_url.clone());
    let current_step = item.as_ref().and_then(|item| item.step.clone());

    let steps = match &state {
        DetailState::Loading => rsx! {
            p { class: "faint", "Loading steps\u{2026}" }
        },
        DetailState::Failed => rsx! {
            p { class: "crit", role: "status", "Steps could not be loaded. The board above is still current." }
        },
        DetailState::Missing => rsx! {
            p { class: "faint", role: "status", "This item is no longer in the console's list." }
        },
        DetailState::Ready(found) => match &found.run {
            None => rsx! {
                p { class: "faint", "Not part of a skynet run, so there is no stepper." }
            },
            Some(run) => {
                let rows = step_rows(&run.steps, current_step.as_deref());
                let heading = match &run.service {
                    Some(service) => format!("{service} \u{00b7} run {}", run.run_id),
                    None => format!("run {}", run.run_id),
                };
                rsx! {
                    p { class: "faint mono", "{heading}" }
                    Stepper { rows }
                }
            }
        },
    };

    let recent = recent_for(&state, &key, now_ms);

    let close_key = key.clone();
    let escape_key = key.clone();
    rsx! {
        section {
            class: "card details",
            id: "work-details",
            tabindex: "-1",
            role: "region",
            aria_labelledby: "work-details-h",
            onmounted: move |evt: MountedEvent| {
                let node = evt.data();
                spawn(async move {
                    let _ = node.set_focus(true).await;
                });
            },
            onkeydown: move |evt: KeyboardEvent| {
                if evt.key() == Key::Escape {
                    close_details(selected, &escape_key);
                }
            },
            header { class: "card-h",
                div { class: "grow",
                    h2 { id: "work-details-h", "{title}" }
                    span { class: "mono faint", "{key}" }
                }
                button {
                    r#type: "button",
                    class: "btn",
                    onclick: move |_| minimized.set(true),
                    "Hide"
                }
                button {
                    r#type: "button",
                    class: "btn",
                    onclick: move |_| close_details(selected, &close_key),
                    "Close"
                }
            }
            div { class: "card-b stack",
                if let Some(item) = &item {
                    dl { class: "fields",
                        FieldRow { label: "Status", value: item.status.clone(), mono: true }
                        FieldRow { label: "Lane", value: item.lane.label().to_string() }
                        FieldRow { label: "Role", value: or_dash(item.role.as_deref()) }
                        FieldRow { label: "Launched by", value: item.launched_by.clone() }
                        FieldRow { label: "Run", value: or_dash(item.run_id.as_deref()), mono: true }
                        FieldRow { label: "Step", value: or_dash(item.step.as_deref()), mono: true }
                        FieldRow { label: "Service", value: or_dash(item.service.as_deref()), mono: true }
                        FieldRow { label: "Created", value: item.created.clone(), mono: true }
                        FieldRow { label: "Started", value: or_dash(item.started.as_deref()), mono: true }
                        FieldRow { label: "Ended", value: or_dash(item.ended.as_deref()), mono: true }
                    }
                }
                if let Some(url) = receipt {
                    a {
                        class: "btn primary",
                        href: "{url}",
                        target: "_blank",
                        rel: "noopener",
                        "Open receipt in Semaphore"
                    }
                }
                div { class: "stack",
                    span { class: "label", "Run steps" }
                    {steps}
                }
                if !recent.is_empty() {
                    RecentRuns { rows: recent }
                }
            }
        }
    }
}

/// "Recent runs": the job's newest runs, each with its lane, age and receipt link.
/// The row for the open run is marked current.
#[component]
fn RecentRuns(rows: Vec<RecentRow>) -> Element {
    rsx! {
        div { class: "stack",
            span { class: "label", "Recent runs" }
            ol { class: "recent-runs", aria_label: "Recent runs of this job",
                for row in rows {
                    li {
                        class: "recent-run",
                        aria_current: if row.current { "true" } else { "false" },
                        span { class: "st {row.tone.class()}", {icon(row.shape)} }
                        span { class: "mono", "{row.key}" }
                        span { class: "st {row.tone.class()}", "{row.word}" }
                        if let Some(age) = &row.age {
                            span { class: "faint", "{age}" }
                        }
                        a {
                            class: "btn",
                            href: "{row.receipt_url}",
                            target: "_blank",
                            rel: "noopener",
                            aria_label: "Open receipt for {row.key} in Semaphore",
                            "Receipt"
                        }
                    }
                }
            }
        }
    }
}

/// The panel minimized: a 44px-wide column with the expand control and the
/// count per lane. The expand control is disabled while nothing is selected.
#[component]
pub fn Rail(counts: Vec<(Lane, usize)>, can_open: bool, minimized: Signal<bool>) -> Element {
    let mut minimized = minimized;
    rsx! {
        aside { class: "work-rail", aria_label: "Details, minimized",
            button {
                r#type: "button",
                class: "btn",
                disabled: !can_open,
                aria_label: "Show details",
                title: "Show details",
                onclick: move |_| minimized.set(false),
                "Details"
            }
            ul { class: "rail-counts", aria_label: "Work per lane",
                for (lane, count) in counts {
                    li {
                        class: "rail-count",
                        title: "{lane.label()}: {count}",
                        aria_label: "{lane.label()}: {count}",
                        span { class: "st {lane.look().1.class()}",
                            {icon(lane.look().0)}
                        }
                        span { class: "num", "{count}" }
                    }
                }
            }
        }
    }
}

/// The run's steps, top to bottom. The item's own step is marked current.
#[component]
fn Stepper(rows: Vec<StepRow>) -> Element {
    let rows: Vec<(String, StepRow)> = rows
        .into_iter()
        .map(|row| {
            let class = format!(
                "vstep {}{}{}",
                row.tone.class(),
                if row.current { " current" } else { "" },
                if row.not_started { " todo" } else { "" },
            );
            (class, row)
        })
        .collect();
    rsx! {
        ol { class: "vsteps", aria_label: "Run steps",
            for (class, row) in rows {
                li {
                    class: "{class}",
                    aria_current: if row.current { "step" } else { "false" },
                    span { class: "vnum",
                        span { class: if row.spins { "spin" } else { "" },
                            {icon(row.shape)}
                        }
                    }
                    span { class: "grow",
                        b { "{row.number}. {row.label}" }
                        span { class: "faint", "{row.owner} \u{00b7} {row.word}" }
                        if let Some(task) = &row.task {
                            span { class: "mono faint", "{task}" }
                        }
                    }
                }
            }
        }
    }
}

/// One labelled field in the details list.
#[component]
fn FieldRow(label: &'static str, value: String, #[props(default)] mono: bool) -> Element {
    let class = if mono { "mono" } else { "" };
    rsx! {
        div { class: "field-row",
            dt { class: "label", "{label}" }
            dd { class: "{class}", "{value}" }
        }
    }
}

/// The value, or an em dash when the field is null.
fn or_dash(value: Option<&str>) -> String {
    value.unwrap_or("\u{2014}").to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn step(id: &str, order: u32, status: &str) -> StepView {
        StepView {
            id: id.to_string(),
            order,
            label: format!("Step {id}"),
            owner: Some("netclaw".to_string()),
            status: status.to_string(),
            key: None,
        }
    }

    #[test]
    fn steps_come_out_in_registry_order() {
        let steps = vec![
            step("c", 3, "queued"),
            step("a", 1, "done"),
            step("b", 2, "running"),
        ];
        let numbers: Vec<u32> = step_rows(&steps, None)
            .iter()
            .map(|row| row.number)
            .collect();
        assert_eq!(numbers, [1, 2, 3]);
    }

    #[test]
    fn current_step_is_the_items_own_step_only() {
        let steps = vec![
            step("a", 1, "done"),
            step("b", 2, "running"),
            step("c", 3, "not_started"),
        ];
        let rows = step_rows(&steps, Some("b"));
        let current: Vec<bool> = rows.iter().map(|row| row.current).collect();
        assert_eq!(current, [false, true, false]);
        assert!(step_rows(&steps, None).iter().all(|row| !row.current));
    }

    #[test]
    fn not_started_steps_are_grey_and_say_so() {
        let steps = vec![step("a", 1, "not_started"), step("b", 2, "something-new")];
        for row in step_rows(&steps, None) {
            assert!(row.not_started);
            assert_eq!(row.tone, Tone::Idle);
            assert_eq!(row.word, "Not started");
            assert!(!row.spins);
        }
    }

    #[test]
    fn lane_status_sets_the_marker_shape_tone_and_word() {
        let rows = step_rows(&[step("a", 1, "running")], None);
        let row = &rows[0];
        assert_eq!(row.shape, "loader-circle");
        assert_eq!(row.tone, Tone::Run);
        assert_eq!(row.word, "Running");
        assert!(row.spins, "only a running step spins");
        assert!(!row.not_started);

        let rows = step_rows(&[step("a", 1, "awaiting"), step("b", 2, "failed")], None);
        assert_eq!(
            (rows[0].shape, rows[0].tone, rows[0].word),
            ("triangle-alert", Tone::Warn, "Awaiting approval")
        );
        assert_eq!(
            (rows[1].shape, rows[1].tone, rows[1].word),
            ("octagon-x", Tone::Crit, "Failed")
        );
    }

    #[test]
    fn a_step_without_an_owner_reads_as_a_dash() {
        let mut only = step("a", 1, "done");
        only.owner = None;
        assert_eq!(step_rows(&[only], None)[0].owner, "\u{2014}");
    }

    #[test]
    fn dash_stands_in_for_a_null_field() {
        assert_eq!(or_dash(None), "\u{2014}");
        assert_eq!(or_dash(Some("sem-4")), "sem-4");
    }

    fn history(key: &str, lane: Lane, started: Option<&str>) -> HistoryEntry {
        HistoryEntry {
            key: key.to_string(),
            lane,
            created: "2026-10-26T12:00:00Z".to_string(),
            started: started.map(str::to_string),
            receipt_url: format!("https://semaphore.example.test/project/1/history?t={key}"),
        }
    }

    fn loaded(history: Vec<HistoryEntry>) -> DetailState {
        DetailState::Ready(Box::new(WorkDetail {
            item: WorkItem {
                key: "sem-21".to_string(),
                title: "Deploy tududi".to_string(),
                lane: Lane::Done,
                status: "success".to_string(),
                role: None,
                launched_by: "schedule".to_string(),
                run_id: None,
                step: None,
                service: Some("tududi".to_string()),
                created: "2026-10-26T12:00:00Z".to_string(),
                started: None,
                ended: None,
                receipt_url: "https://semaphore.example.test/project/1/history?t=21".to_string(),
                latest_in_stream: false,
                stream_runs: history.len() as u32,
            },
            run: None,
            stream_history: history,
        }))
    }

    #[test]
    fn recent_rows_keep_order_mark_the_open_run_and_age_from_start_or_creation() {
        use super::super::work::parse_rfc3339_ms;
        let entries = vec![
            history("sem-23", Lane::Failed, None),
            history("sem-21", Lane::Done, Some("2026-10-26T12:05:00Z")),
        ];
        let now = parse_rfc3339_ms("2026-10-26T12:10:00Z");
        let rows = recent_rows(&entries, "sem-21", now);
        let keys: Vec<&str> = rows.iter().map(|row| row.key.as_str()).collect();
        assert_eq!(keys, ["sem-23", "sem-21"]);
        assert_eq!(
            rows[0].age.as_deref(),
            Some("10m ago"),
            "no start: age from creation"
        );
        assert_eq!(
            rows[1].age.as_deref(),
            Some("5m ago"),
            "age from the start time"
        );
        let current: Vec<bool> = rows.iter().map(|row| row.current).collect();
        assert_eq!(current, [false, true]);
        assert_eq!((rows[0].word, rows[0].shape), ("Failed", "octagon-x"));
        assert!(rows[1].receipt_url.ends_with("t=sem-21"));
    }

    #[test]
    fn recent_runs_show_only_for_a_job_that_ran_more_than_once() {
        let once = loaded(vec![history("sem-21", Lane::Done, None)]);
        assert!(recent_for(&once, "sem-21", None).is_empty());
        let twice = loaded(vec![
            history("sem-23", Lane::Failed, None),
            history("sem-21", Lane::Done, None),
        ]);
        assert_eq!(recent_for(&twice, "sem-21", None).len(), 2);
        assert!(recent_for(&DetailState::Loading, "sem-21", None).is_empty());
    }
}
