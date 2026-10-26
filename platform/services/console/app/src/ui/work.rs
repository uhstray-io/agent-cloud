//! Active Work, the signed-in home page. Every Semaphore task the console can
//! see is a strip in the lane its status maps to. The data is `GET /v1/work`,
//! re-read every `POLL_MS` while the tab is visible.
//!
//! The server answers only platform admins. Anyone else gets the Phase 3 note.
//!
//! Everything that decides something (lane grouping, filters, counts, ages, how
//! a poll reply changes the state) is a plain function with tests. The
//! components only draw what those functions return.

use std::collections::BTreeSet;

use dioxus::prelude::*;
use serde::Deserialize;

use super::api::{self, Reply};
use super::empty::ACTIVE_WORK;
use super::icons::icon;
use super::shell::Ctx;
use super::work_detail::{Details, Rail};

/// How often the page re-reads `GET /v1/work` while the tab is visible.
pub const POLL_MS: u32 = 10_000;

/// The role filter value for items with no skynet role, launched by a person.
const OPERATOR: &str = "operator";

/// The tone of a status. Color follows the tone; the shape and the word carry
/// the same meaning on their own.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Tone {
    Idle,
    Run,
    Warn,
    Crit,
    Ok,
}

impl Tone {
    /// The CSS class that carries this tone.
    pub fn class(self) -> &'static str {
        match self {
            Tone::Idle => "idle",
            Tone::Run => "run",
            Tone::Warn => "warn",
            Tone::Crit => "crit",
            Tone::Ok => "ok",
        }
    }
}

/// A lane of the board. The wire name is the lane value `/v1/work` sends.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Lane {
    Queued,
    Running,
    Awaiting,
    Blocked,
    Failed,
    Done,
}

impl Lane {
    /// Board order, left to right.
    pub const ALL: [Lane; 6] = [
        Lane::Queued,
        Lane::Running,
        Lane::Awaiting,
        Lane::Blocked,
        Lane::Failed,
        Lane::Done,
    ];

    pub fn wire(self) -> &'static str {
        match self {
            Lane::Queued => "queued",
            Lane::Running => "running",
            Lane::Awaiting => "awaiting",
            Lane::Blocked => "blocked",
            Lane::Failed => "failed",
            Lane::Done => "done",
        }
    }

    pub fn from_wire(text: &str) -> Option<Lane> {
        Lane::ALL.into_iter().find(|lane| lane.wire() == text)
    }

    /// The status word. It always sits beside the shape.
    pub fn label(self) -> &'static str {
        match self {
            Lane::Queued => "Queued",
            Lane::Running => "Running",
            Lane::Awaiting => "Awaiting approval",
            Lane::Blocked => "Blocked",
            Lane::Failed => "Failed",
            Lane::Done => "Done",
        }
    }

    /// The Lucide icon name and the tone. Blocked and failed share a shape;
    /// their words tell them apart.
    pub fn look(self) -> (&'static str, Tone) {
        match self {
            Lane::Queued => ("circle-dashed", Tone::Idle),
            Lane::Running => ("loader-circle", Tone::Run),
            Lane::Awaiting => ("triangle-alert", Tone::Warn),
            Lane::Blocked | Lane::Failed => ("octagon-x", Tone::Crit),
            Lane::Done => ("circle-check", Tone::Ok),
        }
    }
}

/// Why the list is what it is. `error` is set only with `error` as the source.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Source {
    Ok,
    Unconfigured,
    Error,
}

/// One Semaphore task as the console shows it. Fields follow the `/v1/work` contract.
#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct WorkItem {
    pub key: String,
    pub title: String,
    pub lane: Lane,
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
}

/// Body of `GET /v1/work`. Items are newest first.
#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct WorkList {
    pub source: Source,
    pub error: Option<String>,
    pub updated_at: Option<String>,
    pub items: Vec<WorkItem>,
}

/// What the page shows. `Shown` keeps the last list it had, so a failed poll
/// shows stale data rather than an empty board.
#[derive(Debug, Clone, PartialEq)]
pub enum WorkState {
    /// Before the first answer.
    Loading,
    /// 403: the caller is not a platform admin. Polling stops.
    Forbidden,
    /// 401: the session ended. Polling stops.
    SignedOut,
    /// No answer yet, and none before either.
    Unreachable,
    /// The last answer. `now_ms` is the client clock when it arrived. `stale`
    /// is the reason the newest poll failed, or the server's error text.
    Shown {
        list: WorkList,
        now_ms: Option<i64>,
        stale: Option<String>,
    },
}

/// The role filter. `Operator` means an item with no skynet role.
#[derive(Debug, Clone, Default, PartialEq)]
pub enum RoleFilter {
    #[default]
    Any,
    Operator,
    Role(String),
}

impl RoleFilter {
    /// The select's value for this filter.
    pub fn value(&self) -> String {
        match self {
            RoleFilter::Any => String::new(),
            RoleFilter::Operator => OPERATOR.to_string(),
            RoleFilter::Role(role) => role.clone(),
        }
    }

    pub fn from_value(text: &str) -> RoleFilter {
        match text {
            "" => RoleFilter::Any,
            OPERATOR => RoleFilter::Operator,
            role => RoleFilter::Role(role.to_string()),
        }
    }
}

/// The three filters on the board. Held in signals; nothing is stored in the URL.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Filters {
    pub role: RoleFilter,
    pub lane: Option<Lane>,
    pub text: String,
}

/// True when `item` passes every filter. Text matches the title or the service,
/// case-insensitively, and an empty text matches everything.
pub fn in_filter(item: &WorkItem, filters: &Filters) -> bool {
    let role_ok = match &filters.role {
        RoleFilter::Any => true,
        RoleFilter::Operator => item.role.is_none(),
        RoleFilter::Role(role) => item.role.as_deref() == Some(role.as_str()),
    };
    let lane_ok = filters.lane.is_none_or(|lane| lane == item.lane);
    let needle = filters.text.trim().to_lowercase();
    let text_ok = needle.is_empty()
        || item.title.to_lowercase().contains(&needle)
        || item
            .service
            .as_deref()
            .is_some_and(|service| service.to_lowercase().contains(&needle));
    role_ok && lane_ok && text_ok
}

/// The items of each lane, in board order. Every lane appears, even when empty.
pub fn group_by_lane(items: &[WorkItem]) -> Vec<(Lane, Vec<WorkItem>)> {
    Lane::ALL
        .into_iter()
        .map(|lane| {
            let members = items
                .iter()
                .filter(|item| item.lane == lane)
                .cloned()
                .collect();
            (lane, members)
        })
        .collect()
}

/// How many items sit in `lane`.
pub fn lane_count(items: &[WorkItem], lane: Lane) -> usize {
    items.iter().filter(|item| item.lane == lane).count()
}

/// The count for the rail: every lane with its size, in board order.
pub fn lane_counts(items: &[WorkItem]) -> Vec<(Lane, usize)> {
    Lane::ALL
        .into_iter()
        .map(|lane| (lane, lane_count(items, lane)))
        .collect()
}

/// Items that need a person: blocked or failed. The sidebar badge shows this.
pub fn attention_count(items: &[WorkItem]) -> usize {
    lane_count(items, Lane::Blocked) + lane_count(items, Lane::Failed)
}

/// The sidebar badge's count for a state. Zero until a list has arrived.
pub fn attention(state: &WorkState) -> usize {
    match state {
        WorkState::Shown { list, .. } => attention_count(&list.items),
        _ => 0,
    }
}

/// The distinct roles on the board, sorted, for the role select.
pub fn roles_present(items: &[WorkItem]) -> Vec<String> {
    let roles: BTreeSet<&str> = items
        .iter()
        .filter_map(|item| item.role.as_deref())
        .collect();
    roles.into_iter().map(str::to_string).collect()
}

/// The item with `key` in the last list, if the state holds one.
pub fn find_item<'a>(state: &'a WorkState, key: &str) -> Option<&'a WorkItem> {
    match state {
        WorkState::Shown { list, .. } => list.items.iter().find(|item| item.key == key),
        _ => None,
    }
}

/// What the details panel compares to decide whether to fetch the run again:
/// the lane, the status and the step. A change in any of them can change the
/// stepper.
pub fn item_stamp(item: &WorkItem) -> String {
    format!(
        "{}|{}|{}",
        item.lane.wire(),
        item.status,
        item.step.as_deref().unwrap_or("")
    )
}

/// The age text, such as "12m ago", for a time `delta_ms` in the past.
/// A time in the future (client clock behind the server) reads as "0s ago".
pub fn format_age(delta_ms: i64) -> String {
    let secs = (delta_ms / 1000).max(0);
    if secs < 60 {
        format!("{secs}s ago")
    } else if secs < 3_600 {
        format!("{}m ago", secs / 60)
    } else if secs < 86_400 {
        format!("{}h ago", secs / 3_600)
    } else {
        format!("{}d ago", secs / 86_400)
    }
}

/// The age of `since` against the client clock, or `None` when either is unknown.
pub fn age_text(now_ms: Option<i64>, since: &str) -> Option<String> {
    let then = parse_rfc3339_ms(since)?;
    Some(format_age(now_ms? - then))
}

/// The strip's age: from the start, or from creation before the task started.
pub fn item_age(now_ms: Option<i64>, item: &WorkItem) -> Option<String> {
    age_text(now_ms, item.started.as_deref().unwrap_or(&item.created))
}

/// The banner shown when the newest poll failed or the server reports an error.
pub fn stale_text(now_ms: Option<i64>, updated_at: Option<&str>, reason: &str) -> String {
    match updated_at {
        Some(stamp) => match age_text(now_ms, stamp) {
            Some(age) => format!("Stale \u{2014} last update {age}: {reason}"),
            None => format!("Stale \u{2014} last update {stamp}: {reason}"),
        },
        None => format!("Stale \u{2014} no update yet: {reason}"),
    }
}

/// Days from 1970-01-01 to the given civil date (proleptic Gregorian).
fn days_from_civil(year: i64, month: i64, day: i64) -> i64 {
    let y = if month <= 2 { year - 1 } else { year };
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = y - era * 400;
    let mp = (month + 9) % 12;
    let doy = (153 * mp + 2) / 5 + day - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

/// The `len` ASCII digits at `at`, as a number.
fn digits(bytes: &[u8], at: usize, len: usize) -> Option<i64> {
    let part = bytes.get(at..at + len)?;
    if !part.iter().all(u8::is_ascii_digit) {
        return None;
    }
    Some(
        part.iter()
            .fold(0, |acc, digit| acc * 10 + i64::from(digit - b'0')),
    )
}

/// Reads an RFC 3339 time (`2026-10-26T12:00:00.250-04:00`) as Unix milliseconds.
/// Fractions past the third digit are cut, and a missing zone is refused.
pub fn parse_rfc3339_ms(text: &str) -> Option<i64> {
    let b = text.as_bytes();
    let sep = |at: usize, want: u8| (b.get(at) == Some(&want)).then_some(());

    let year = digits(b, 0, 4)?;
    sep(4, b'-')?;
    let month = digits(b, 5, 2)?;
    sep(7, b'-')?;
    let day = digits(b, 8, 2)?;
    if !matches!(b.get(10), Some(b'T' | b't' | b' ')) {
        return None;
    }
    let hour = digits(b, 11, 2)?;
    sep(13, b':')?;
    let minute = digits(b, 14, 2)?;
    sep(16, b':')?;
    let second = digits(b, 17, 2)?;
    if !(1..=12).contains(&month)
        || !(1..=31).contains(&day)
        || hour > 23
        || minute > 59
        || second > 60
    {
        return None;
    }

    let mut at = 19;
    let mut millis = 0;
    if b.get(at) == Some(&b'.') {
        at += 1;
        let start = at;
        while b.get(at).is_some_and(u8::is_ascii_digit) {
            at += 1;
        }
        if at == start {
            return None;
        }
        // The first three fraction digits, padded on the right with zeros.
        for offset in 0..3 {
            let digit = b
                .get(start + offset)
                .filter(|c| c.is_ascii_digit())
                .map_or(0, |c| i64::from(c - b'0'));
            millis = millis * 10 + digit;
        }
    }

    let offset_minutes = match b.get(at) {
        Some(b'Z' | b'z') if at + 1 == b.len() => 0,
        Some(sign @ (b'+' | b'-')) if at + 6 == b.len() => {
            sep(at + 3, b':')?;
            let total = digits(b, at + 1, 2)? * 60 + digits(b, at + 4, 2)?;
            if *sign == b'-' {
                -total
            } else {
                total
            }
        }
        _ => return None,
    };

    let days = days_from_civil(year, month, day);
    let seconds = days * 86_400 + hour * 3_600 + minute * 60 + second - offset_minutes * 60;
    Some(seconds * 1000 + millis)
}

/// Where the page is. Polling stops only on `Forbidden` and `SignedOut`.
/// `Unreachable` and a stale `Shown` keep trying.
pub fn keeps_polling(state: &WorkState) -> bool {
    !matches!(state, WorkState::Forbidden | WorkState::SignedOut)
}

/// What the page shows after one poll. `prev` is the state before the poll,
/// `now_ms` the client clock at the reply. A failed poll keeps the last list.
pub fn next_state(prev: &WorkState, reply: &Reply, now_ms: Option<i64>) -> WorkState {
    match reply.status {
        200 => match api::parse::<WorkList>(reply) {
            Some(list) => {
                let stale = match list.source {
                    Source::Error => Some(
                        list.error
                            .clone()
                            .unwrap_or_else(|| "unknown error".to_string()),
                    ),
                    Source::Ok | Source::Unconfigured => None,
                };
                WorkState::Shown {
                    list,
                    now_ms,
                    stale,
                }
            }
            None => keep_last(prev, "the console sent an answer it could not read", now_ms),
        },
        401 => WorkState::SignedOut,
        403 => WorkState::Forbidden,
        _ => keep_last(prev, "the console API did not answer", now_ms),
    }
}

/// A failed poll: show the last list with the reason, or nothing if there was none.
fn keep_last(prev: &WorkState, reason: &str, now_ms: Option<i64>) -> WorkState {
    match prev {
        WorkState::Shown { list, .. } => WorkState::Shown {
            list: list.clone(),
            now_ms,
            stale: Some(reason.to_string()),
        },
        _ => WorkState::Unreachable,
    }
}

/// The page's one poll. It runs for the life of the signed-in shell, and both
/// the sidebar badge and the page read the state it writes. It waits for the
/// tab to be visible, reads, then sleeps `POLL_MS` or until the tab is hidden.
pub async fn poll(mut state: Signal<WorkState>) {
    loop {
        api::wait_visible().await;
        let reply = api::request("GET", "/v1/work", None).await;
        let now = api::now_ms().await;
        let next = next_state(&state.peek(), &reply, now);
        let keep = keeps_polling(&next);
        state.set(next);
        if !keep {
            return;
        }
        api::sleep_unless_hidden(POLL_MS).await;
    }
}

/// The page. Its body depends on the state; the header is the same in every case.
#[component]
pub fn Home() -> Element {
    let ctx = use_context::<Ctx>();
    let state = ctx.work.read().clone();
    let filters = use_signal(Filters::default);
    let selected = use_signal(|| None::<String>);
    let minimized = use_signal(|| false);
    let done_open = use_signal(|| false);

    let body = match state {
        WorkState::Loading => rsx! {
            Notice { message: "Loading work\u{2026}" }
        },
        WorkState::Forbidden => rsx! {
            Notice { message: "Active Work is limited to platform administrators until work maps to tenants (Phase 3)" }
        },
        WorkState::SignedOut => rsx! {
            Notice { message: "Your session ended. Sign in again to see Active Work" }
        },
        WorkState::Unreachable => rsx! {
            Notice { message: "Active Work could not be loaded. It retries every 10 seconds while this tab is visible" }
        },
        WorkState::Shown { list, .. } if list.source == Source::Unconfigured => rsx! {
            Notice { message: "Semaphore connection not configured" }
        },
        WorkState::Shown {
            list,
            now_ms,
            stale,
        } => rsx! {
            Live {
                list,
                now_ms,
                stale,
                filters,
                selected,
                minimized,
                done_open,
            }
        },
    };

    rsx! {
        div { class: "view work",
            header { class: "view-h",
                h1 { {icon(ACTIVE_WORK.icon)} "{ACTIVE_WORK.title}" }
                span { class: "authority", "Source of truth: {ACTIVE_WORK.authority}" }
                p { "{ACTIVE_WORK.purpose}" }
            }
            {body}
        }
    }
}

/// One empty state: a message in the page's empty-state card.
#[component]
fn Notice(message: &'static str) -> Element {
    rsx! {
        section { class: "card",
            div { class: "empty",
                span { class: "empty-icon", aria_hidden: "true", {icon("info")} }
                p { role: "status", "{message}" }
            }
        }
    }
}

/// The board with its banners, filters, and the details panel or its rail.
#[component]
fn Live(
    list: WorkList,
    now_ms: Option<i64>,
    stale: Option<String>,
    filters: Signal<Filters>,
    selected: Signal<Option<String>>,
    minimized: Signal<bool>,
    done_open: Signal<bool>,
) -> Element {
    let running = lane_count(&list.items, Lane::Running);
    let roles = roles_present(&list.items);
    let current = filters.read().clone();
    let shown: Vec<WorkItem> = list
        .items
        .iter()
        .filter(|item| in_filter(item, &current))
        .cloned()
        .collect();
    let none_match = shown.is_empty() && !list.items.is_empty();
    let groups = group_by_lane(&shown);
    let counts = lane_counts(&list.items);
    let stale_line = stale
        .as_deref()
        .map(|reason| stale_text(now_ms, list.updated_at.as_deref(), reason));
    let can_open = selected.read().is_some();
    let panel_open = can_open && !minimized();
    let layout = if panel_open {
        "work-layout panel-open"
    } else {
        "work-layout"
    };

    rsx! {
        ScopeBanner { running }
        if let Some(line) = stale_line {
            p { class: "stale", role: "status", "{line}" }
        }
        FilterBar { roles, filters }
        if none_match {
            p { class: "faint", role: "status", "No work matches these filters." }
        }
        div { class: "{layout}",
            Board { groups, now_ms, selected, minimized, done_open }
            if panel_open {
                Details { selected, minimized }
            } else {
                Rail { counts, can_open, minimized }
            }
        }
    }
}

/// The scope banner. The count is tenant-wide in Phase 2, and the label says so.
/// Clicking it moves focus to the Running lane.
#[component]
fn ScopeBanner(running: usize) -> Element {
    let spin = if running > 0 { "spin" } else { "" };
    rsx! {
        button {
            r#type: "button",
            class: "scope-banner",
            onclick: move |_| api::focus("lane-running"),
            span { class: "st run",
                span { class: "{spin}", {icon("loader-circle")} }
            }
            span { class: "grow",
                b { "{running} running" }
                span { "platform-wide (Phase 2)" }
            }
        }
    }
}

/// The three filters: role, lane and text. Each change writes its signal at once.
#[component]
fn FilterBar(roles: Vec<String>, filters: Signal<Filters>) -> Element {
    let mut filters = filters;
    let current = filters.read().clone();
    let lane_value = current.lane.map_or("", Lane::wire);
    rsx! {
        div { class: "filters", role: "search", aria_label: "Filter work",
            label { class: "field-inline",
                span { class: "label", "Role" }
                select {
                    class: "scope-select",
                    value: "{current.role.value()}",
                    onchange: move |evt: FormEvent| {
                        filters.write().role = RoleFilter::from_value(&evt.value());
                    },
                    option { value: "", "All roles" }
                    option { value: OPERATOR, "operator" }
                    for role in roles {
                        option { value: "{role}", "{role}" }
                    }
                }
            }
            label { class: "field-inline",
                span { class: "label", "Lane" }
                select {
                    class: "scope-select",
                    value: "{lane_value}",
                    onchange: move |evt: FormEvent| {
                        filters.write().lane = Lane::from_wire(&evt.value());
                    },
                    option { value: "", "All lanes" }
                    for lane in Lane::ALL {
                        option { value: "{lane.wire()}", "{lane.label()}" }
                    }
                }
            }
            label { class: "field-inline grow",
                span { class: "label", "Title or service" }
                input {
                    class: "input",
                    r#type: "search",
                    value: "{current.text}",
                    oninput: move |evt: FormEvent| filters.write().text = evt.value(),
                }
            }
        }
    }
}

/// The board: one section per lane. Done folds to a narrow button until opened.
#[component]
fn Board(
    groups: Vec<(Lane, Vec<WorkItem>)>,
    now_ms: Option<i64>,
    selected: Signal<Option<String>>,
    minimized: Signal<bool>,
    done_open: Signal<bool>,
) -> Element {
    let class = if done_open() {
        "board done-open"
    } else {
        "board"
    };
    rsx! {
        div { class: "{class}",
            for group in groups {
                if group.0 == Lane::Done && !done_open() {
                    DoneToggle { count: group.1.len(), done_open }
                } else {
                    LaneSection {
                        lane: group.0,
                        items: group.1,
                        now_ms,
                        selected,
                        minimized,
                        toggle: if group.0 == Lane::Done { Some(done_open) } else { None },
                    }
                }
            }
        }
    }
}

/// The collapsed Done lane: its count, and a button to open it.
#[component]
fn DoneToggle(count: usize, done_open: Signal<bool>) -> Element {
    let mut done_open = done_open;
    rsx! {
        button {
            r#type: "button",
            class: "lane lane-collapsed",
            aria_expanded: "false",
            aria_label: "Show done, {count} items",
            onclick: move |_| done_open.set(true),
            span { class: "vert", "Done" }
            span { class: "num", "{count}" }
        }
    }
}

/// One lane: a labelled region holding a heading, its count and its strips.
#[component]
fn LaneSection(
    lane: Lane,
    items: Vec<WorkItem>,
    now_ms: Option<i64>,
    selected: Signal<Option<String>>,
    minimized: Signal<bool>,
    toggle: Option<Signal<bool>>,
) -> Element {
    let (shape, tone) = lane.look();
    let spin = if lane == Lane::Running { "spin" } else { "" };
    let heading = format!("lane-{}-h", lane.wire());
    let section_id = format!("lane-{}", lane.wire());
    let count = items.len();
    rsx! {
        section {
            class: "lane",
            id: "{section_id}",
            tabindex: "-1",
            aria_labelledby: "{heading}",
            header { class: "lane-h",
                span { class: "st {tone.class()}",
                    span { class: "{spin}", {icon(shape)} }
                }
                h2 { id: "{heading}", "{lane.label()}" }
                span { class: "num", "{count}" }
                if let Some(mut open) = toggle {
                    button {
                        r#type: "button",
                        class: "btn",
                        aria_expanded: "true",
                        aria_label: "Hide done items",
                        onclick: move |_| open.set(false),
                        "Hide"
                    }
                }
            }
            if items.is_empty() {
                p { class: "lane-empty", "Nothing here" }
            }
            for item in items {
                Strip { key: "{item.key}", item, now_ms, selected, minimized }
            }
        }
    }
}

/// One work item: a button that opens its details. The shape and the word say
/// the status; the bar's color repeats it.
#[component]
fn Strip(
    item: WorkItem,
    now_ms: Option<i64>,
    selected: Signal<Option<String>>,
    minimized: Signal<bool>,
) -> Element {
    let mut selected = selected;
    let mut minimized = minimized;
    let (shape, tone) = item.lane.look();
    let spin = if item.lane == Lane::Running {
        "spin"
    } else {
        ""
    };
    let label = item.lane.label();
    let is_selected = selected().as_deref() == Some(item.key.as_str());
    let age = item_age(now_ms, &item);
    let open_key = item.key.clone();
    rsx! {
        button {
            r#type: "button",
            class: "strip",
            id: "strip-{item.key}",
            aria_pressed: "{is_selected}",
            onclick: move |_| {
                selected.set(Some(open_key.clone()));
                minimized.set(false);
            },
            span { class: "strip-bar {tone.class()}" }
            span { class: "strip-b",
                span { class: "strip-top",
                    span { class: "strip-key mono", "{item.key}" }
                    span { class: "strip-title", "{item.title}" }
                }
                span { class: "strip-meta",
                    if let Some(role) = &item.role {
                        span { class: "tag", "{role}" }
                    } else {
                        span { class: "faint", "launched by {item.launched_by}" }
                    }
                    span { class: "st {tone.class()}",
                        span { class: "{spin}", {icon(shape)} }
                        "{label}"
                    }
                    if let Some(age) = age {
                        span { class: "faint", "{age}" }
                    }
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn item(key: &str, lane: Lane) -> WorkItem {
        WorkItem {
            key: key.to_string(),
            title: format!("Title {key}"),
            lane,
            status: "running".to_string(),
            role: None,
            launched_by: "stray".to_string(),
            run_id: None,
            step: None,
            service: None,
            created: "2026-10-26T12:00:00Z".to_string(),
            started: None,
            ended: None,
            receipt_url: "https://semaphore.example.test/project/1/history?t=1".to_string(),
        }
    }

    fn keys(items: &[WorkItem]) -> Vec<&str> {
        items.iter().map(|item| item.key.as_str()).collect()
    }

    fn reply(status: u16, body: serde_json::Value) -> Reply {
        Reply { status, body }
    }

    fn list_body(source: &str, items: serde_json::Value) -> serde_json::Value {
        json!({
            "source": source,
            "error": null,
            "updated_at": "2026-10-26T12:00:00Z",
            "items": items,
        })
    }

    #[test]
    fn lanes_are_in_board_order_and_wire_names_round_trip() {
        let wires: Vec<&str> = Lane::ALL.iter().map(|lane| lane.wire()).collect();
        assert_eq!(
            wires,
            ["queued", "running", "awaiting", "blocked", "failed", "done"]
        );
        for lane in Lane::ALL {
            assert_eq!(Lane::from_wire(lane.wire()), Some(lane));
        }
        assert_eq!(Lane::from_wire("proposed"), None);
    }

    #[test]
    fn group_by_lane_puts_each_item_in_its_lane_and_keeps_empty_lanes() {
        let items = vec![
            item("sem-1", Lane::Failed),
            item("sem-2", Lane::Queued),
            item("sem-3", Lane::Awaiting),
            item("sem-4", Lane::Failed),
            item("sem-5", Lane::Queued),
        ];
        let groups = group_by_lane(&items);
        let order: Vec<Lane> = groups.iter().map(|(lane, _)| *lane).collect();
        assert_eq!(order, Lane::ALL.to_vec(), "every lane, in board order");
        let by = |lane: Lane| -> Vec<&str> {
            let (_, members) = groups.iter().find(|(l, _)| *l == lane).unwrap();
            keys(members)
        };
        assert_eq!(
            by(Lane::Queued),
            ["sem-2", "sem-5"],
            "newest-first order kept"
        );
        assert_eq!(by(Lane::Awaiting), ["sem-3"]);
        assert_eq!(by(Lane::Failed), ["sem-1", "sem-4"]);
        assert!(by(Lane::Running).is_empty());
        assert!(by(Lane::Done).is_empty());
    }

    #[test]
    fn attention_counts_blocked_and_failed_only() {
        let items = vec![
            item("sem-1", Lane::Blocked),
            item("sem-2", Lane::Failed),
            item("sem-3", Lane::Failed),
            item("sem-4", Lane::Running),
            item("sem-5", Lane::Awaiting),
            item("sem-6", Lane::Done),
        ];
        assert_eq!(attention_count(&items), 3);
        assert_eq!(attention_count(&[]), 0);
        assert_eq!(attention(&WorkState::Loading), 0);
        assert_eq!(attention(&WorkState::Unreachable), 0);
        let shown = WorkState::Shown {
            list: WorkList {
                source: Source::Ok,
                error: None,
                updated_at: None,
                items,
            },
            now_ms: None,
            stale: None,
        };
        assert_eq!(attention(&shown), 3);
    }

    #[test]
    fn lane_counts_sum_to_the_whole_list() {
        let items = vec![
            item("a", Lane::Running),
            item("b", Lane::Running),
            item("c", Lane::Done),
        ];
        let counts = lane_counts(&items);
        assert_eq!(counts.len(), 6);
        assert_eq!(counts.iter().map(|(_, n)| n).sum::<usize>(), items.len());
        assert_eq!(lane_count(&items, Lane::Running), 2);
    }

    #[test]
    fn filters_combine_by_role_lane_and_text() {
        let mut skynet = item("sem-1", Lane::Queued);
        skynet.role = Some("netclaw".to_string());
        skynet.service = Some("netbox".to_string());
        let person = item("sem-2", Lane::Queued);
        let mut other_lane = item("sem-3", Lane::Running);
        other_lane.role = Some("netclaw".to_string());
        let items = [skynet, person, other_lane];

        let by_role = Filters {
            role: RoleFilter::Role("netclaw".to_string()),
            ..Filters::default()
        };
        let picked: Vec<&str> = items
            .iter()
            .filter(|i| in_filter(i, &by_role))
            .map(|i| i.key.as_str())
            .collect();
        assert_eq!(picked, ["sem-1", "sem-3"]);

        let operator = Filters {
            role: RoleFilter::Operator,
            ..Filters::default()
        };
        let picked: Vec<&str> = items
            .iter()
            .filter(|i| in_filter(i, &operator))
            .map(|i| i.key.as_str())
            .collect();
        assert_eq!(picked, ["sem-2"], "operator means no skynet role");

        let queued_only = Filters {
            lane: Some(Lane::Queued),
            ..Filters::default()
        };
        let picked: Vec<&str> = items
            .iter()
            .filter(|i| in_filter(i, &queued_only))
            .map(|i| i.key.as_str())
            .collect();
        assert_eq!(picked, ["sem-1", "sem-2"]);

        let by_service = Filters {
            text: "  NETBOX ".to_string(),
            ..Filters::default()
        };
        let picked: Vec<&str> = items
            .iter()
            .filter(|i| in_filter(i, &by_service))
            .map(|i| i.key.as_str())
            .collect();
        assert_eq!(
            picked,
            ["sem-1"],
            "text matches the service, ignoring case and spaces"
        );

        let by_title = Filters {
            text: "title sem-2".to_string(),
            ..Filters::default()
        };
        assert!(!in_filter(&items[0], &by_title));
        assert!(in_filter(
            &items[1],
            &Filters {
                text: "TITLE SEM-2".to_string(),
                ..Filters::default()
            }
        ));
    }

    #[test]
    fn role_filter_round_trips_through_its_select_value() {
        assert_eq!(RoleFilter::from_value(""), RoleFilter::Any);
        assert_eq!(RoleFilter::from_value("operator"), RoleFilter::Operator);
        assert_eq!(
            RoleFilter::from_value("netclaw"),
            RoleFilter::Role("netclaw".to_string())
        );
        assert_eq!(RoleFilter::Any.value(), "");
        assert_eq!(RoleFilter::Operator.value(), "operator");
        assert_eq!(RoleFilter::Role("netclaw".to_string()).value(), "netclaw");
    }

    #[test]
    fn roles_present_are_distinct_and_sorted() {
        let mut a = item("a", Lane::Queued);
        a.role = Some("netclaw".to_string());
        let mut b = item("b", Lane::Queued);
        b.role = Some("bridge".to_string());
        let mut c = item("c", Lane::Queued);
        c.role = Some("netclaw".to_string());
        let d = item("d", Lane::Queued);
        assert_eq!(roles_present(&[a, b, c, d]), ["bridge", "netclaw"]);
    }

    #[test]
    fn rfc3339_reads_offsets_fractions_and_refuses_bad_text() {
        // 1970-01-01T00:00:00Z is the epoch.
        assert_eq!(parse_rfc3339_ms("1970-01-01T00:00:00Z"), Some(0));
        // 2000-03-01 is 11017 days after the epoch (leap day in 2000 counted).
        assert_eq!(days_from_civil(2000, 3, 1), 11_017);
        assert_eq!(
            parse_rfc3339_ms("2000-03-01T00:00:00Z"),
            Some(11_017 * 86_400 * 1000)
        );
        let utc = parse_rfc3339_ms("2026-10-26T12:00:00Z").unwrap();
        assert_eq!(parse_rfc3339_ms("2026-10-26T08:00:00-04:00"), Some(utc));
        assert_eq!(parse_rfc3339_ms("2026-10-26T13:30:00+01:30"), Some(utc));
        assert_eq!(parse_rfc3339_ms("2026-10-26T12:00:00+00:00"), Some(utc));
        assert_eq!(
            parse_rfc3339_ms("2026-10-26T12:00:00.250Z"),
            Some(utc + 250)
        );
        assert_eq!(
            parse_rfc3339_ms("2026-10-26T12:00:00.1234567Z"),
            Some(utc + 123)
        );
        assert_eq!(parse_rfc3339_ms("2026-10-26T12:00:00.5Z"), Some(utc + 500));
        assert_eq!(parse_rfc3339_ms("2026-10-26 12:00:00Z"), Some(utc));
        assert_eq!(parse_rfc3339_ms("2026-10-26T12:00:00"), None, "no zone");
        assert_eq!(parse_rfc3339_ms("2026-13-01T00:00:00Z"), None);
        assert_eq!(parse_rfc3339_ms("2026-10-26T24:00:00Z"), None);
        assert_eq!(parse_rfc3339_ms("2026-10-26T12:00:00.Z"), None);
        assert_eq!(parse_rfc3339_ms("2026-10-26T12:00:00+0400"), None);
        assert_eq!(parse_rfc3339_ms("not a time"), None);
        assert_eq!(parse_rfc3339_ms(""), None);
    }

    #[test]
    fn age_text_is_relative_and_needs_both_clocks() {
        let since = "2026-10-26T12:00:00Z";
        let then = parse_rfc3339_ms(since).unwrap();
        assert_eq!(
            age_text(Some(then + 45_000), since).as_deref(),
            Some("45s ago")
        );
        assert_eq!(
            age_text(Some(then + 12 * 60_000), since).as_deref(),
            Some("12m ago")
        );
        assert_eq!(
            age_text(Some(then + 3 * 3_600_000), since).as_deref(),
            Some("3h ago")
        );
        assert_eq!(
            age_text(Some(then + 2 * 86_400_000), since).as_deref(),
            Some("2d ago")
        );
        assert_eq!(
            age_text(Some(then - 5_000), since).as_deref(),
            Some("0s ago")
        );
        assert_eq!(age_text(None, since), None);
        assert_eq!(age_text(Some(then), "garbage"), None);
    }

    #[test]
    fn item_age_starts_at_start_time_else_creation() {
        let mut task = item("sem-1", Lane::Running);
        task.created = "2026-10-26T12:00:00Z".to_string();
        let now = parse_rfc3339_ms("2026-10-26T12:10:00Z");
        assert_eq!(item_age(now, &task).as_deref(), Some("10m ago"));
        task.started = Some("2026-10-26T12:09:00Z".to_string());
        assert_eq!(item_age(now, &task).as_deref(), Some("1m ago"));
    }

    #[test]
    fn stale_text_names_the_last_update_or_says_there_was_none() {
        let now = parse_rfc3339_ms("2026-10-26T12:05:00Z");
        assert_eq!(
            stale_text(
                now,
                Some("2026-10-26T12:00:00Z"),
                "console API did not answer"
            ),
            "Stale \u{2014} last update 5m ago: console API did not answer"
        );
        assert_eq!(
            stale_text(None, Some("2026-10-26T12:00:00Z"), "boom"),
            "Stale \u{2014} last update 2026-10-26T12:00:00Z: boom"
        );
        assert_eq!(
            stale_text(now, None, "boom"),
            "Stale \u{2014} no update yet: boom"
        );
    }

    #[test]
    fn a_good_answer_is_shown_and_an_error_source_is_stale() {
        let ok = reply(
            200,
            list_body(
                "ok",
                json!([{
                    "key": "sem-9", "title": "Deploy", "lane": "running", "status": "running",
                    "role": "netclaw", "launched_by": "skynet", "run_id": "r1", "step": "apply",
                    "service": "netbox", "created": "2026-10-26T12:00:00Z", "started": null,
                    "ended": null, "receipt_url": "https://semaphore.example.test/project/1/history?t=9"
                }]),
            ),
        );
        match next_state(&WorkState::Loading, &ok, Some(1)) {
            WorkState::Shown {
                list,
                now_ms,
                stale,
            } => {
                assert_eq!(list.items.len(), 1);
                assert_eq!(list.items[0].lane, Lane::Running);
                assert_eq!(list.items[0].role.as_deref(), Some("netclaw"));
                assert_eq!(now_ms, Some(1));
                assert_eq!(stale, None);
            }
            other => panic!("expected Shown, got {other:?}"),
        }

        let error = reply(
            200,
            json!({"source": "error", "error": "semaphore returned 503", "updated_at": "2026-10-26T12:00:00Z", "items": []}),
        );
        match next_state(&WorkState::Loading, &error, Some(2)) {
            WorkState::Shown { stale, .. } => {
                assert_eq!(stale.as_deref(), Some("semaphore returned 503"))
            }
            other => panic!("expected Shown, got {other:?}"),
        }
    }

    #[test]
    fn a_failed_poll_keeps_the_last_list_and_marks_it_stale() {
        let ok = reply(200, list_body("ok", json!([])));
        let first = next_state(&WorkState::Loading, &ok, Some(1));
        let failed = next_state(&first, &reply(0, serde_json::Value::Null), Some(9));
        match &failed {
            WorkState::Shown { stale, now_ms, .. } => {
                assert_eq!(stale.as_deref(), Some("the console API did not answer"));
                assert_eq!(*now_ms, Some(9));
            }
            other => panic!("expected Shown with stale, got {other:?}"),
        }
        assert!(keeps_polling(&failed), "a stale board keeps polling");
        assert_eq!(
            next_state(
                &WorkState::Loading,
                &reply(500, serde_json::Value::Null),
                None
            ),
            WorkState::Unreachable
        );
        assert_eq!(
            next_state(&WorkState::Loading, &reply(200, json!({"nope": 1})), None),
            WorkState::Unreachable,
            "an unreadable 200 is a failed poll"
        );
    }

    #[test]
    fn forbidden_and_signed_out_stop_polling() {
        assert_eq!(
            next_state(
                &WorkState::Loading,
                &reply(403, json!({"error": "forbidden"})),
                None
            ),
            WorkState::Forbidden
        );
        assert_eq!(
            next_state(
                &WorkState::Loading,
                &reply(401, json!({"error": "unauthenticated"})),
                None
            ),
            WorkState::SignedOut
        );
        assert!(!keeps_polling(&WorkState::Forbidden));
        assert!(!keeps_polling(&WorkState::SignedOut));
        assert!(keeps_polling(&WorkState::Loading));
        assert!(keeps_polling(&WorkState::Unreachable));
    }

    #[test]
    fn stamp_changes_with_lane_status_or_step() {
        let mut task = item("sem-1", Lane::Queued);
        let before = item_stamp(&task);
        task.step = Some("apply".to_string());
        assert_ne!(item_stamp(&task), before);
        let after_step = item_stamp(&task);
        task.lane = Lane::Running;
        assert_ne!(item_stamp(&task), after_step);
    }
}
