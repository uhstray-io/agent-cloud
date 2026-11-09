//! The CI/CD page (plan 17, task 9.1, D42): required checks, the promotion path and open pull requests, read
//! from `GET /v1/delivery`, for platform admins. Every status shows its icon and its word. The one control
//! on the page, a re-run, is off until proposals land (Phase 4). Nothing in this file writes to the API.

use dioxus::prelude::*;

use super::api::{self, Me};
use super::cicd_api::{
    age_label, checks_summary, not_ok_message, promotion_sentence, read_label, required_shown,
    subtitle, DeliveryBody, Promotion, Pull, DELIVERY_PATH,
};
use super::empty::CICD;
use super::icons::icon;
use super::inventory::{admin_next, end_session, keeps_polling_admin, Admin, POLL_MS};
use super::inventory_api;
use super::shell::Ctx;
use super::status::{Notice, DASH};
use super::work::Tone;

/// Why the Re-run button is off: a re-run arrives with proposals, which is Phase 4.
const RERUN_TITLE: &str = "Re-runs arrive with proposals (Phase 4)";

/// The element that shows `RERUN_TITLE` as visible text above the pull request table; each Re-run button describes itself by it.
const RERUN_REASON_ID: &str = "cicd-action-reason";

/// The page's one poll, as `poll_admin` runs it. It also keeps the client clock each answer arrived at, so
/// the ages in the table are read against that clock (as the Policy page does).
async fn poll(
    mut state: Signal<Admin<DeliveryBody>>,
    mut now: Signal<Option<i64>>,
    me: Signal<Me>,
) {
    loop {
        api::wait_visible().await;
        let read = inventory_api::read::<DeliveryBody>(DELIVERY_PATH).await;
        now.set(api::now_ms().await);
        let next = admin_next(&state.peek(), read);
        let keep = keeps_polling_admin(&next);
        let signed_out = matches!(next, Admin::SignedOut);
        state.set(next);
        if signed_out {
            end_session(me);
        }
        if !keep {
            return;
        }
        api::sleep_unless_hidden(POLL_MS).await;
    }
}

/// The CI/CD page. The header is the same in every case; the body depends on the answer.
#[component]
pub fn CiCd() -> Element {
    let ctx = use_context::<Ctx>();
    let state = use_signal(|| Admin::<DeliveryBody>::Loading);
    let now = use_signal(|| None::<i64>);
    use_hook(move || {
        spawn(poll(state, now, ctx.me));
    });

    let shown = state.read().clone();
    let now_ms = *now.read();
    let lede = match &shown {
        Admin::Shown { body, .. } => match &body.git_ref {
            Some(git_ref) => subtitle(git_ref),
            None => CICD.purpose.to_string(),
        },
        _ => CICD.purpose.to_string(),
    };
    let read = match &shown {
        Admin::Shown { body, .. } => body
            .refreshed_at
            .as_deref()
            .map(|stamp| read_label(now_ms, stamp)),
        _ => None,
    };
    rsx! {
        div { class: "view",
            header { class: "view-h",
                h1 { {icon(CICD.icon)} "{CICD.title}" }
                span { class: "authority", "Source of truth: {CICD.authority}" }
                p { "{lede}" }
                if let Some(read) = read {
                    p { "{read}." }
                }
            }
            CiCdView { state: shown, now_ms }
        }
    }
}

/// What the page shows for each state of the poll.
#[component]
fn CiCdView(state: Admin<DeliveryBody>, now_ms: Option<i64>) -> Element {
    match state {
        Admin::Loading => rsx! { Notice { message: "Loading delivery state\u{2026}".to_string() } },
        Admin::Forbidden => rsx! { Notice { message: "Platform admins only.".to_string() } },
        Admin::Missing => rsx! {
            Notice { message: "The delivery answer was not found.".to_string() }
        },
        Admin::SignedOut => rsx! {
            Notice { message: "Your session ended. Sign in again to see delivery state.".to_string() }
        },
        Admin::Unavailable(reason) => rsx! {
            Notice { message: format!(
                "Delivery state could not be loaded: {reason}. It retries every {} seconds while this tab is visible.",
                POLL_MS / 1000
            ) }
        },
        Admin::Shown { body, reason } => rsx! {
            CiCdBodyView { body, reason: reason.unwrap_or_default(), now_ms }
        },
    }
}

/// The body of an answer the console read. Any state but `ok` is one sentence. An `ok` answer is the three
/// cards, under the banners that say what is stale or what the newest read reported.
#[component]
fn CiCdBodyView(body: DeliveryBody, reason: String, now_ms: Option<i64>) -> Element {
    if let Some(message) = not_ok_message(body.state, body.error.as_deref()) {
        return rsx! { Notice { message } };
    }
    rsx! {
        if !reason.is_empty() {
            p { class: "stale", role: "status", "The last read failed: {reason}." }
        }
        if let Some(line) = body.error.clone() {
            Notice { message: line }
        }
        PromotionCard { repo: body.repo.clone(), promotion: body.promotion.clone() }
        RequiredCard { checks: body.required_checks.clone(), ruleset_url: body.ruleset_url.clone() }
        PullsCard { pulls: body.pulls.clone(), now_ms }
    }
}

/// The promotion path: how far `dev` is ahead of `main`, the compare link, and the pull request that carries
/// it, when one is open.
#[component]
fn PromotionCard(repo: Option<String>, promotion: Option<Promotion>) -> Element {
    rsx! {
        section { class: "card",
            div { class: "card-h",
                h2 { "Promotion path" }
                if let Some(repo) = repo {
                    span { class: "muted", "{repo}" }
                }
            }
            if let Some(promotion) = promotion {
                p { "{promotion_sentence(&promotion)}" }
                p {
                    a {
                        href: "{promotion.compare_url}",
                        target: "_blank",
                        rel: "noopener noreferrer",
                        "Compare on GitHub"
                    }
                }
                if let Some(pull) = &promotion.pull {
                    div { class: "row",
                        a {
                            href: "{pull.url}",
                            target: "_blank",
                            rel: "noopener noreferrer",
                            "#{pull.number} {pull.title}"
                        }
                        {status_badge(pull.review.look())}
                        span { class: "muted", "{checks_summary(&pull.checks)}" }
                    }
                } else {
                    p { "No open pull request from {promotion.head} into main." }
                }
            } else {
                p { class: "empty", "No promotion comparison was read." }
            }
        }
    }
}

/// The checks the ruleset on `main` requires, by name, and the link to the ruleset file when one is named.
#[component]
fn RequiredCard(checks: Vec<String>, ruleset_url: Option<String>) -> Element {
    let empty = checks.is_empty();
    rsx! {
        section { class: "card",
            div { class: "card-h",
                h2 { "Required checks in the ruleset file" }
                if let Some(url) = ruleset_url {
                    a {
                        href: "{url}",
                        target: "_blank",
                        rel: "noopener noreferrer",
                        "Ruleset file"
                    }
                }
            }
            ul {
                for name in checks.iter() {
                    li { key: "{name}", "{name}" }
                }
            }
            if empty {
                p { class: "empty", "No required checks were read." }
            }
        }
    }
}

/// Every open pull request: its target, author, review, checks, the required checks on `main`, its age and
/// the one control, which is off.
#[component]
fn PullsCard(pulls: Vec<Pull>, now_ms: Option<i64>) -> Element {
    let empty = pulls.is_empty();
    rsx! {
        section { class: "card",
            div { class: "card-h",
                h2 { "Open pull requests" }
            }
            if !empty {
                div { class: "card-b",
                    p { id: RERUN_REASON_ID, class: "action-reason", "{RERUN_TITLE}" }
                }
            }
            div { class: "tbl-wrap",
                table { class: "tbl",
                    thead {
                        tr {
                            th { "PR" }
                            th { "Into" }
                            th { "Author" }
                            th { "Review" }
                            th { "Checks" }
                            th { "Required (ruleset file)" }
                            th { "Updated" }
                            th { "Action" }
                        }
                    }
                    tbody {
                        for pull in pulls.iter() {
                            tr { key: "{pull.number}",
                                td {
                                    a {
                                        href: "{pull.url}",
                                        target: "_blank",
                                        rel: "noopener noreferrer",
                                        "#{pull.number} {pull.title}"
                                    }
                                    span { class: "muted", " from {pull.head}" }
                                    if pull.draft {
                                        span { class: "tag", "Draft" }
                                    }
                                }
                                td { class: "mono", "{pull.base}" }
                                td { "{pull.author}" }
                                td { {status_badge(pull.review.look())} }
                                td { "{checks_summary(&pull.checks)}" }
                                td { {required_cell(pull)} }
                                td { title: "{pull.updated_at}", {age_label(now_ms, &pull.updated_at)} }
                                td {
                                    button {
                                        class: "btn",
                                        disabled: true,
                                        aria_describedby: RERUN_REASON_ID,
                                        title: "{RERUN_TITLE}",
                                        "Re-run"
                                    }
                                }
                            }
                        }
                    }
                }
            }
            if empty {
                p { class: "empty", "No open pull requests." }
            }
        }
    }
}

/// A status as its icon, its tone and its word. The shape and the word carry the state; the tone repeats it.
/// This is the same shape `RagBadge` draws.
fn status_badge(look: (&'static str, Tone, &'static str)) -> Element {
    let (shape, tone, label) = look;
    rsx! {
        span { class: "st {tone.class()}", {icon(shape)} span { "{label}" } }
    }
}

/// The Required cell: each required check on a pull into `main` with its status, or a dash. A pull into any
/// other base has no required checks in the answer, so it shows the dash too (`required_shown` decides).
fn required_cell(pull: &Pull) -> Element {
    let checks = required_shown(pull);
    if checks.is_empty() {
        return rsx! { {DASH} };
    }
    rsx! {
        ul {
            for check in checks.iter() {
                li { key: "{check.name}",
                    span { class: "mono", "{check.name}" }
                    {status_badge(check.state.look())}
                }
            }
        }
    }
}
