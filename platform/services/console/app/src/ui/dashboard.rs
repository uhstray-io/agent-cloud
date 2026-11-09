//! The Dashboard (plan 17, D48-D53): the proposals waiting for a platform admin, and the ones recently decided.
//! An admin comments on a waiting proposal, edits its check mode and note, then approves or denies it.
//! Approval is the only way anything runs: the launch gate decides again then, and its answer is the outcome
//! line. The page reads `/v1/proposals` and writes through `proposals_api`. The poll is the only writer of
//! the two lists, as on the other admin pages; an action re-reads them at once.

use dioxus::prelude::*;

use super::api::{self, Me, Reply};
use super::empty::PageMeta;
use super::icons::icon;
use super::inventory::{admin_next, end_session, keeps_polling_admin, Admin, POLL_MS};
use super::inventory_api;
use super::proposals_api::{self, DecisionAction, Outcome, Proposal, ProposalsBody};
use super::shell::Ctx;
use super::status::DASH;
use super::work::age_text;
use super::Route;

/// The page's nav entry and header text.
pub const DASHBOARD: PageMeta = PageMeta {
    id: "dashboard",
    title: "Dashboard",
    icon: "layout-dashboard",
    purpose: "Proposals waiting for your review: comment, edit, approve or deny. Nothing runs until you approve.",
    authority: "Dispatch Console proposals, launch gate, OPA",
};

/// The outcome line's element. It is focused after an action, so a keyboard user keeps their place.
const OUTCOME_ID: &str = "dash-outcome";

/// Where one action ended: a new outcome for the page, or the end of the session (401).
#[derive(Debug, Clone, PartialEq)]
pub enum Settled {
    Changed(Outcome),
    SignedOut,
}

/// The two lists the page keeps, and the client clock they were last read at.
#[derive(Clone, Copy)]
struct Queues {
    awaiting: Signal<Admin<ProposalsBody>>,
    decided: Signal<Admin<ProposalsBody>>,
    now: Signal<Option<i64>>,
}

/// One read of both lists into their signals. Returns `false` when the poll should stop: the session ended,
/// or the caller is not a platform admin.
async fn read_queues(q: Queues, me: Signal<Me>) -> bool {
    let waiting = inventory_api::read::<ProposalsBody>(proposals_api::AWAITING_PATH).await;
    let decided = inventory_api::read::<ProposalsBody>(proposals_api::DECIDED_PATH).await;
    let mut now = q.now;
    now.set(api::now_ms().await);
    let next_waiting = admin_next(&q.awaiting.peek(), waiting);
    let next_decided = admin_next(&q.decided.peek(), decided);
    let signed_out =
        matches!(next_waiting, Admin::SignedOut) || matches!(next_decided, Admin::SignedOut);
    let keep = keeps_polling_admin(&next_waiting) && keeps_polling_admin(&next_decided);
    let mut awaiting = q.awaiting;
    let mut decided_signal = q.decided;
    awaiting.set(next_waiting);
    decided_signal.set(next_decided);
    if signed_out {
        end_session(me);
    }
    keep
}

/// The poll the page runs while it is open. It reads both lists, then sleeps `POLL_MS` or until the tab is hidden.
async fn poll(q: Queues, me: Signal<Me>) {
    loop {
        api::wait_visible().await;
        if !read_queues(q, me).await {
            return;
        }
        api::sleep_unless_hidden(POLL_MS).await;
    }
}

/// What an action's answer means: a 2xx becomes its outcome through `on_ok`, a 401 ends the session, and
/// anything else is the server's sentence.
pub fn action_settled(reply: &Reply, on_ok: impl FnOnce(&Proposal) -> Outcome) -> Settled {
    if reply.status == 401 {
        return Settled::SignedOut;
    }
    if (200..=299).contains(&reply.status) {
        return match api::parse::<Proposal>(reply) {
            Some(proposal) => Settled::Changed(on_ok(&proposal)),
            None => Settled::Changed(Outcome {
                text: proposals_api::GENERIC_FAILURE.to_string(),
                ok: false,
                task_id: None,
            }),
        };
    }
    Settled::Changed(Outcome {
        text: proposals_api::error_text(reply),
        ok: false,
        task_id: None,
    })
}

/// Applies one action's result. The lists are re-read at once, so the card that acted moves to its new state.
fn settle(settled: Settled, q: Queues, me: Signal<Me>, mut outcome: Signal<Option<Outcome>>) {
    match settled {
        Settled::SignedOut => end_session(me),
        Settled::Changed(done) => {
            outcome.set(Some(done));
            spawn(async move {
                read_queues(q, me).await;
            });
        }
    }
}

/// The sentence a list shows when it has no list to show. `None` for a list the caller draws.
pub fn queue_notice<T>(state: &Admin<T>) -> Option<String> {
    match state {
        Admin::Shown { .. } => None,
        Admin::Loading => Some("Loading proposals\u{2026}".to_string()),
        Admin::Forbidden => Some(proposals_api::ADMIN_ONLY.to_string()),
        Admin::Missing => Some("The proposals answer was not found.".to_string()),
        Admin::SignedOut => Some("Your session ended. Sign in again to see proposals.".to_string()),
        Admin::Unavailable(reason) => Some(format!(
            "Proposals could not be loaded: {reason}. It retries every {} seconds while this tab is visible.",
            POLL_MS / 1000
        )),
    }
}

/// When something happened, as the page says it: the age on the client clock, or the stamp when the clock
/// or the stamp cannot be read.
fn when(now_ms: Option<i64>, stamp: &str) -> String {
    age_text(now_ms, stamp).unwrap_or_else(|| stamp.to_string())
}

/// The Dashboard page.
#[component]
pub fn Dashboard() -> Element {
    let ctx = use_context::<Ctx>();
    let me = ctx.me;
    let queues = Queues {
        awaiting: use_signal(|| Admin::<ProposalsBody>::Loading),
        decided: use_signal(|| Admin::<ProposalsBody>::Loading),
        now: use_signal(|| None::<i64>),
    };
    let outcome = use_signal(|| None::<Outcome>);
    let on_settled = use_callback(move |settled: Settled| settle(settled, queues, me, outcome));
    use_hook(move || {
        spawn(poll(queues, me));
    });
    use_effect(move || {
        if outcome.read().is_some() {
            api::focus(OUTCOME_ID);
        }
    });

    let me_sub = match me.read().clone() {
        Me::SignedIn(account) => Some(account.claims.sub),
        _ => None,
    };
    let now_ms = *queues.now.read();
    let waiting = queues.awaiting.read().clone();
    let decided = queues.decided.read().clone();
    let current = outcome.read().clone();
    let count = match &waiting {
        Admin::Shown { body, .. } => body.proposals.len().to_string(),
        _ => DASH.to_string(),
    };

    rsx! {
        div { class: "view dashboard",
            header { class: "view-h",
                h1 { {icon(DASHBOARD.icon)} "{DASHBOARD.title}" }
                span { class: "authority", "Source of truth: {DASHBOARD.authority}" }
                p { "{DASHBOARD.purpose}" }
            }
            OutcomeLine { outcome: current }
            section { class: "card", aria_labelledby: "dash-waiting-h",
                div { class: "card-h",
                    h2 { id: "dash-waiting-h", "Proposals awaiting approval" }
                    span { class: "faint num", "{count}" }
                }
                div { class: "card-b stack",
                    WaitingList { state: waiting, now_ms, me_sub, on_settled }
                }
            }
            section { class: "card", aria_labelledby: "dash-decided-h",
                div { class: "card-h",
                    h2 { id: "dash-decided-h", "Recently decided" }
                }
                div { class: "card-b stack",
                    DecidedList { state: decided, now_ms }
                }
            }
        }
    }
}

/// What the last action said. Empty until an action runs; then it is focused, so the keyboard stays with it.
#[component]
fn OutcomeLine(outcome: Option<Outcome>) -> Element {
    let (text, ok, task_id) = match outcome {
        Some(done) => (done.text, done.ok, done.task_id),
        None => (String::new(), true, None),
    };
    let (shape, tone) = if ok {
        ("circle-check", "ok")
    } else {
        ("octagon-x", "crit")
    };
    rsx! {
        p { id: OUTCOME_ID, class: "outcome", tabindex: "-1", role: "status",
            if !text.is_empty() {
                span { class: "st {tone}", {icon(shape)} span { "{text}" } }
            }
            if task_id.is_some() {
                " "
                Link { to: Route::Home {}, "Open Active Work" }
            }
        }
    }
}

/// The waiting list, or the sentence that stands in for it.
#[component]
fn WaitingList(
    state: Admin<ProposalsBody>,
    now_ms: Option<i64>,
    me_sub: Option<String>,
    on_settled: EventHandler<Settled>,
) -> Element {
    match state {
        Admin::Shown { body, reason } => {
            let reason = reason.unwrap_or_default();
            rsx! {
                if !reason.is_empty() {
                    p { class: "stale", role: "status", "The last read failed: {reason}." }
                }
                if body.proposals.is_empty() {
                    div { class: "empty",
                        span { class: "empty-icon", aria_hidden: "true", {icon("info")} }
                        b { "Nothing waiting on approval" }
                        span { "Propose an assessment or a health check from a service's page." }
                    }
                } else {
                    ul { class: "proposals",
                        for proposal in body.proposals.iter() {
                            ProposalCard {
                                key: "{proposal.id}",
                                proposal: proposal.clone(),
                                now_ms,
                                me_sub: me_sub.clone(),
                                on_settled,
                            }
                        }
                    }
                }
            }
        }
        other => {
            let message = queue_notice(&other).unwrap_or_default();
            rsx! { p { class: "empty", role: "status", "{message}" } }
        }
    }
}

/// The recently decided list, newest first, or the sentence that stands in for it.
#[component]
fn DecidedList(state: Admin<ProposalsBody>, now_ms: Option<i64>) -> Element {
    match state {
        Admin::Shown { body, reason } => {
            let reason = reason.unwrap_or_default();
            let recent = proposals_api::recent_decided(&body.proposals);
            rsx! {
                if !reason.is_empty() {
                    p { class: "stale", role: "status", "The last read failed: {reason}." }
                }
                if recent.is_empty() {
                    p { class: "empty", "Nothing decided yet." }
                } else {
                    ul { class: "proposals",
                        for proposal in recent {
                            DecidedRow { key: "{proposal.id}", proposal, now_ms }
                        }
                    }
                }
            }
        }
        other => {
            let message = queue_notice(&other).unwrap_or_default();
            rsx! { p { class: "empty", role: "status", "{message}" } }
        }
    }
}

/// A change control that is off, with its reason as visible text tied to it (plan 17 D44). `DisabledAction`
/// takes a static reason; this one takes the policy's own sentence, which exists only at run time. The markup
/// is the same: a disabled button, `aria-describedby` to the reason, and the reason as a span.
#[component]
fn BlockedButton(label: String, reason: String, id: String) -> Element {
    rsx! {
        button { class: "btn", r#type: "button", disabled: true, aria_describedby: "{id}", title: "{reason}", "{label}" }
        span { id: "{id}", class: "action-reason", "{reason}" }
    }
}

/// One waiting proposal, with its gate preview, its comments, and the actions an admin takes on it.
#[component]
fn ProposalCard(
    proposal: Proposal,
    now_ms: Option<i64>,
    me_sub: Option<String>,
    on_settled: EventHandler<Settled>,
) -> Element {
    let mut editing = use_signal(|| false);
    let mut edit_check = use_signal(|| false);
    let mut edit_note = use_signal(String::new);
    let mut comment = use_signal(String::new);
    let mut deny_note = use_signal(String::new);
    let mut busy = use_signal(|| false);
    let mut notice = use_signal(|| None::<String>);

    let id = proposal.id.clone();
    let title = format!(
        "{} \u{00b7} {}",
        proposals_api::kind_label(proposal.kind),
        proposal.service
    );
    let (shape, tone, word) = proposals_api::preview_look(proposal.preview.as_ref());
    let reason = proposals_api::preview_reason(proposal.preview.as_ref());
    let refusal = proposals_api::approve_refusal(&proposal);
    let proposed = format!(
        "Proposed by {} \u{00b7} {}",
        proposal.proposer.name,
        when(now_ms, &proposal.created_at)
    );
    let own = me_sub.as_deref() == Some(proposal.proposer.sub.as_str());
    let note = proposal.note.clone().filter(|text| !text.is_empty());
    let runs = format!(
        "Runs {} for role {}, step {}.",
        proposal.template, proposal.agent, proposal.step
    );
    let approve_reason_id = format!("approve-reason-{id}");

    let on_approve = {
        let id = id.clone();
        move |_: MouseEvent| {
            if busy() {
                return;
            }
            busy.set(true);
            notice.set(None);
            let id = id.clone();
            spawn(async move {
                let reply = proposals_api::approve(&id).await;
                busy.set(false);
                on_settled.call(action_settled(&reply, proposals_api::outcome_for));
            });
        }
    };
    let on_deny = {
        let id = id.clone();
        move |_: MouseEvent| {
            if busy() {
                return;
            }
            busy.set(true);
            notice.set(None);
            let id = id.clone();
            let text = deny_note();
            spawn(async move {
                let reply = proposals_api::deny(&id, &text).await;
                busy.set(false);
                on_settled.call(action_settled(&reply, proposals_api::outcome_for));
            });
        }
    };
    let on_comment = {
        let id = id.clone();
        move |_: MouseEvent| {
            let Some(text) = proposals_api::comment_text(&comment()) else {
                notice.set(Some("Write a comment first.".to_string()));
                return;
            };
            if busy() {
                return;
            }
            busy.set(true);
            notice.set(None);
            let id = id.clone();
            spawn(async move {
                let reply = proposals_api::comment(&id, &text).await;
                busy.set(false);
                if (200..=299).contains(&reply.status) {
                    comment.set(String::new());
                }
                on_settled.call(action_settled(&reply, proposals_api::comment_outcome));
            });
        }
    };
    let on_edit = {
        let check = proposal.check_mode;
        let current_note = proposal.note.clone().unwrap_or_default();
        move |_: MouseEvent| {
            edit_check.set(check);
            edit_note.set(current_note.clone());
            notice.set(None);
            editing.set(true);
        }
    };
    let on_save = {
        let id = id.clone();
        move |_: MouseEvent| {
            if busy() {
                return;
            }
            busy.set(true);
            notice.set(None);
            let id = id.clone();
            let check = edit_check();
            let text = edit_note();
            spawn(async move {
                let reply = proposals_api::save(&id, check, &text).await;
                busy.set(false);
                if (200..=299).contains(&reply.status) {
                    editing.set(false);
                }
                on_settled.call(action_settled(&reply, proposals_api::saved_outcome));
            });
        }
    };

    rsx! {
        li { class: "proposal",
            div { class: "proposal-head",
                span { class: "mono faint", "{id}" }
                h3 { "{title}" }
                span { class: "st {tone.class()}", {icon(shape)} span { "{word}" } }
                if let Some(text) = reason {
                    span { class: "hint", "{text}" }
                }
            }
            p { class: "faint", "{proposed}" }
            if proposal.check_mode {
                span { class: "tag", "Check mode" }
            }
            p { class: "hint", "{runs}" }
            if let Some(text) = note {
                p { class: "muted", "Note: {text}" }
            }
            if own {
                p { class: "hint", "You proposed this. Approving it is recorded as a self-approval." }
            }
            if !proposal.comments.is_empty() {
                ul { class: "proposal-comments", aria_label: "Comments on {id}",
                    for entry in proposal.comments.iter() {
                        li {
                            "{entry.text}"
                            span { class: "faint", {format!(" \u{00b7} {} \u{00b7} {}", entry.by.name, when(now_ms, &entry.at))} }
                        }
                    }
                }
            }
            div { class: "proposal-actions",
                label { class: "field grow",
                    span { class: "label", "Comment" }
                    input {
                        class: "input",
                        r#type: "text",
                        maxlength: "2000",
                        placeholder: "Comment on this proposal",
                        value: "{comment}",
                        oninput: move |evt: FormEvent| comment.set(evt.value()),
                    }
                }
                button { class: "btn", r#type: "button", onclick: on_comment, "Add" }
            }
            if editing() {
                div { class: "stack",
                    label { class: "choice",
                        input {
                            r#type: "checkbox",
                            checked: edit_check(),
                            onchange: move |evt: FormEvent| edit_check.set(evt.checked()),
                        }
                        span { "Check mode (dry run)" }
                    }
                    label { class: "field",
                        span { class: "label", "Note" }
                        input {
                            class: "input",
                            r#type: "text",
                            maxlength: "2000",
                            value: "{edit_note}",
                            oninput: move |evt: FormEvent| edit_note.set(evt.value()),
                        }
                    }
                    div { class: "proposal-actions",
                        button { class: "btn primary", r#type: "button", onclick: on_save, {icon("check")} "Save" }
                        button {
                            class: "btn",
                            r#type: "button",
                            onclick: move |_: MouseEvent| editing.set(false),
                            "Cancel"
                        }
                    }
                }
            } else {
                div { class: "proposal-actions",
                    if let Some(text) = refusal {
                        BlockedButton { label: "Approve".to_string(), reason: text, id: approve_reason_id }
                    } else {
                        button { class: "btn primary", r#type: "button", onclick: on_approve, {icon("check")} "Approve" }
                    }
                    button { class: "btn", r#type: "button", onclick: on_edit, "Edit" }
                    label { class: "field",
                        span { class: "label", "Denial note (optional)" }
                        input {
                            class: "input",
                            r#type: "text",
                            maxlength: "2000",
                            value: "{deny_note}",
                            oninput: move |evt: FormEvent| deny_note.set(evt.value()),
                        }
                    }
                    button { class: "btn", r#type: "button", onclick: on_deny, {icon("x")} "Deny" }
                }
            }
            if busy() {
                p { class: "hint", role: "status", "Working\u{2026}" }
            }
            if let Some(message) = notice() {
                p { class: "hint", role: "status", "{message}" }
            }
        }
    }
}

/// One decided proposal: what it was, who decided it and when, whether it was a self-approval, and what
/// the decision led to.
#[component]
fn DecidedRow(proposal: Proposal, now_ms: Option<i64>) -> Element {
    let (shape, tone, word) = proposals_api::state_look(proposal.state);
    let title = format!(
        "{} \u{00b7} {}",
        proposals_api::kind_label(proposal.kind),
        proposal.service
    );
    let decided = proposal.decision.as_ref().map(|decision| {
        let verb = match decision.action {
            DecisionAction::Approved => "Approved",
            DecisionAction::Denied => "Denied",
            DecisionAction::Unknown => "Decided",
        };
        format!(
            "{verb} by {} \u{00b7} {}",
            decision.by.name,
            when(now_ms, &decision.at)
        )
    });
    let self_approved = proposal
        .decision
        .as_ref()
        .is_some_and(|decision| decision.self_approved);
    let decision_note = proposal
        .decision
        .as_ref()
        .and_then(|decision| decision.note.clone())
        .filter(|text| !text.is_empty());
    let outcome = proposals_api::outcome_for(&proposal);
    rsx! {
        li { class: "proposal",
            div { class: "proposal-head",
                span { class: "mono faint", "{proposal.id}" }
                h3 { "{title}" }
                span { class: "st {tone.class()}", {icon(shape)} span { "{word}" } }
                if self_approved {
                    span { class: "tag", "Self-approved" }
                }
            }
            if let Some(line) = decided {
                p { class: "faint", "{line}" }
            }
            if let Some(text) = decision_note {
                p { class: "muted", "Note: {text}" }
            }
            p { class: "hint", "{outcome.text}" }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_denied_queue_says_platform_admins_only() {
        let state: Admin<ProposalsBody> = Admin::Forbidden;
        assert_eq!(
            queue_notice(&state).as_deref(),
            Some("Platform admins only.")
        );
    }

    #[test]
    fn a_shown_list_has_no_notice_and_every_other_state_has_one() {
        let shown: Admin<ProposalsBody> = Admin::Shown {
            body: ProposalsBody::default(),
            reason: None,
        };
        assert_eq!(queue_notice(&shown), None);
        let signed_out: Admin<ProposalsBody> = Admin::SignedOut;
        assert!(queue_notice(&signed_out).is_some_and(|m| m.contains("Sign in again")));
        let missing: Admin<ProposalsBody> = Admin::Missing;
        assert!(queue_notice(&missing).is_some());
    }

    #[test]
    fn a_failed_load_says_when_it_retries_from_the_poll_interval() {
        let state: Admin<ProposalsBody> =
            Admin::Unavailable("GitHub answered HTTP 500".to_string());
        let message = queue_notice(&state).expect("message");
        assert!(message.contains("GitHub answered HTTP 500"));
        assert!(message.contains(&format!("every {} seconds", POLL_MS / 1000)));
    }

    #[test]
    fn a_forbidden_or_failed_write_maps_to_its_sentence() {
        let forbidden = Reply {
            status: 403,
            body: serde_json::json!({"error": "forbidden", "detail": "x"}),
        };
        match action_settled(&forbidden, proposals_api::outcome_for) {
            Settled::Changed(outcome) => {
                assert!(!outcome.ok);
                assert_eq!(outcome.text, "Platform admins only.");
            }
            other => panic!("expected a changed outcome, got {other:?}"),
        }
    }

    #[test]
    fn a_401_ends_the_session_and_a_conflict_keeps_the_servers_sentence() {
        let unauthenticated = Reply {
            status: 401,
            body: serde_json::Value::Null,
        };
        assert_eq!(
            action_settled(&unauthenticated, proposals_api::outcome_for),
            Settled::SignedOut
        );
        let conflict = Reply {
            status: 409,
            body: serde_json::json!({"error": "not_awaiting", "detail": "Already decided."}),
        };
        match action_settled(&conflict, proposals_api::outcome_for) {
            Settled::Changed(outcome) => assert_eq!(outcome.text, "Already decided."),
            other => panic!("expected a changed outcome, got {other:?}"),
        }
    }

    #[test]
    fn a_2xx_answer_becomes_the_outcome_the_action_names() {
        let launched = Reply {
            status: 200,
            body: serde_json::json!({
                "id": "p42",
                "state": "launched",
                "launch": {"task_id": 42, "message": "started", "reason": "ok"}
            }),
        };
        match action_settled(&launched, proposals_api::outcome_for) {
            Settled::Changed(outcome) => {
                assert!(outcome.ok);
                assert_eq!(outcome.task_id, Some(42));
                assert_eq!(outcome.text, "Launched as task 42.");
            }
            other => panic!("expected a changed outcome, got {other:?}"),
        }
        let unreadable = Reply {
            status: 200,
            body: serde_json::Value::Null,
        };
        match action_settled(&unreadable, proposals_api::outcome_for) {
            Settled::Changed(outcome) => assert!(!outcome.ok),
            other => panic!("expected a changed outcome, got {other:?}"),
        }
    }
}

#[cfg(all(test, feature = "server"))]
mod render_tests {
    use super::*;
    use crate::ui::proposals_api::{Decision, Person, Preview, PreviewState, State};

    fn render(element: Element) -> String {
        dioxus::ssr::render_element(element)
    }

    /// A card with its handler made inside a component. An event-handler prop needs the Dioxus runtime,
    /// which `render` builds only once it runs, so the test cannot make the handler outside it.
    #[component]
    fn CardUnderTest(proposal: Proposal) -> Element {
        rsx! {
            ProposalCard { proposal, now_ms: None, me_sub: None, on_settled: move |_: Settled| {} }
        }
    }

    /// The waiting list with its handler made inside a component, for the same reason as `CardUnderTest`.
    #[component]
    fn WaitingUnderTest(state: Admin<ProposalsBody>) -> Element {
        rsx! {
            WaitingList { state, now_ms: None, me_sub: None, on_settled: move |_: Settled| {} }
        }
    }

    /// The markup from the last `open` before `needle` through the first `close` at or after it.
    #[track_caller]
    fn element_around<'a>(html: &'a str, needle: &str, open: &str, close: &str) -> &'a str {
        let at = html
            .find(needle)
            .unwrap_or_else(|| panic!("no {needle:?} in: {html}"));
        let start = html[..at]
            .rfind(open)
            .unwrap_or_else(|| panic!("no {open:?} before {needle:?}"));
        let end = at + html[at..].find(close).expect("element end") + close.len();
        &html[start..end]
    }

    /// A waiting proposal whose gate preview is `state` with `reason`.
    fn waiting(state: PreviewState, reason: Option<&str>) -> Proposal {
        Proposal {
            id: "p42".into(),
            kind: proposals_api::Kind::Assess,
            service: "honcho".into(),
            template: "Snapshot Service Assessment (Dev)".into(),
            agent: "service-agent".into(),
            step: "service-assess".into(),
            state: State::Awaiting,
            proposer: Person {
                sub: "ada-sub".into(),
                name: "Ada".into(),
            },
            created_at: "2026-11-08T10:00:00Z".into(),
            preview: Some(Preview {
                state,
                reason: reason.map(str::to_string),
                detail: None,
            }),
            ..Proposal::default()
        }
    }

    #[test]
    fn a_card_draws_the_preview_icon_and_word_and_shows_the_reason() {
        let p = waiting(PreviewState::Denied, Some("tag not on the allowlist"));
        let html = render(rsx! { CardUnderTest { proposal: p } });
        let badge = element_around(&html, "OPA denied", "<span class=\"st", "</span></span>");
        assert!(
            badge.contains("<svg"),
            "no icon on the preview badge: {badge}"
        );
        let reason = element_around(&html, "tag not on the allowlist", "<span", "</span>");
        assert!(
            reason.contains("tag not on the allowlist"),
            "reason not visible: {reason}"
        );
    }

    #[test]
    fn approve_is_disabled_and_its_reason_is_visible_text_it_points_to() {
        let p = waiting(PreviewState::Denied, Some("tag not on the allowlist"));
        let html = render(rsx! { CardUnderTest { proposal: p } });
        let button = element_around(&html, ">Approve</button>", "<button", "</button>");
        assert!(
            button.contains("disabled"),
            "Approve is not disabled: {button}"
        );
        assert!(
            button.contains("aria-describedby=\"approve-reason-p42\""),
            "Approve does not point at its reason: {button}"
        );
        let reason = element_around(&html, "id=\"approve-reason-p42\"", "<span", "</span>");
        assert!(
            reason.contains("OPA did not allow this proposal: tag not on the allowlist"),
            "the reason is not visible text: {reason}"
        );
    }

    #[test]
    fn an_allowed_proposal_offers_an_enabled_approve() {
        let p = waiting(PreviewState::Allowed, None);
        let html = render(rsx! { CardUnderTest { proposal: p } });
        let button = element_around(&html, ">Approve</button>", "<button", "</button>");
        assert!(
            !button.contains("disabled"),
            "Approve is disabled: {button}"
        );
        assert!(
            !html.contains("approve-reason-p42"),
            "a reason shows with Approve on: {html}"
        );
    }

    #[test]
    fn a_self_approved_decision_is_marked_in_the_recent_list() {
        let decided = Proposal {
            id: "p7".into(),
            state: State::Launched,
            service: "tududi".into(),
            decision: Some(Decision {
                action: proposals_api::DecisionAction::Approved,
                by: Person {
                    sub: "ada-sub".into(),
                    name: "Ada".into(),
                },
                at: "2026-11-08T10:00:00Z".into(),
                note: None,
                self_approved: true,
            }),
            launch: Some(proposals_api::Launch {
                task_id: Some(9),
                error: None,
            }),
            ..Proposal::default()
        };
        let html = render(rsx! { DecidedRow { proposal: decided, now_ms: None } });
        assert!(
            html.contains("Self-approved"),
            "no self-approved marker: {html}"
        );
        assert!(
            html.contains("Launched as task 9."),
            "no launch outcome: {html}"
        );
    }

    #[test]
    fn a_decision_by_someone_else_is_not_marked_self_approved() {
        let decided = Proposal {
            id: "p8".into(),
            state: State::Denied,
            decision: Some(Decision {
                action: proposals_api::DecisionAction::Denied,
                by: Person {
                    sub: "grace-sub".into(),
                    name: "Grace".into(),
                },
                at: "2026-11-08T10:00:00Z".into(),
                note: None,
                self_approved: false,
            }),
            ..Proposal::default()
        };
        let html = render(rsx! { DecidedRow { proposal: decided, now_ms: None } });
        assert!(
            !html.contains("Self-approved"),
            "marked a peer decision: {html}"
        );
        assert!(html.contains("Denied by Grace"), "no decider: {html}");
    }

    #[test]
    fn an_empty_waiting_list_says_nothing_waiting_and_where_to_start() {
        let html = render(rsx! {
            WaitingUnderTest { state: Admin::Shown { body: ProposalsBody::default(), reason: None } }
        });
        assert!(
            html.contains("Nothing waiting on approval"),
            "no empty state: {html}"
        );
        assert!(
            html.contains("Propose an assessment or a health check from a service"),
            "no pointer to a service page: {html}"
        );
    }
}
