//! The Policy page (plan 17, 8.4, D36): the OPA and OpenBao policy files on the configured GitHub ref, for
//! platform admins. Each file shows its name, its kind, when it last changed and a link to it. The console has
//! no source for decision counts, so none are shown. A change is proposed as a plan, then a pull request (Phase 4).

use dioxus::prelude::*;

use super::action::DisabledAction;
use super::api::{self, Me};
use super::empty::PageMeta;
use super::icons::icon;
use super::inventory::{admin_next, end_session, keeps_polling_admin, Admin, POLL_MS};
use super::inventory_api;
use super::policy_api::{
    kind_label, not_ok_message, opa_files, openbao_files, read_label, subtitle, PoliciesBody,
    PolicyFile, POLICY_PATH,
};
use super::shell::Ctx;
use super::status::Notice;
use super::work::age_text;

/// The page's nav entry and header text. The header line under the title names the ref once a read has
/// finished (see `policy_api::subtitle`); this is the line before that.
pub const POLICY: PageMeta = PageMeta {
    id: "policy",
    title: "Policy",
    icon: "scale",
    purpose: "Policy files as they are on the configured ref, read from GitHub. Decision counts are not shown: the console has no source for them.",
    authority: "GitHub (OPA and OpenBao policy files)",
};

/// Why the Propose button is off: a policy change arrives as a proposal, which is Phase 4.
const PROPOSE_TITLE: &str = "Policy changes arrive with proposals (Phase 4)";

/// The page's one poll, as `poll_admin` runs it. It also keeps the client clock each answer arrived at, so
/// the ages in the table are read against that clock (as the Secrets page does).
async fn poll(
    mut state: Signal<Admin<PoliciesBody>>,
    mut now: Signal<Option<i64>>,
    me: Signal<Me>,
) {
    loop {
        api::wait_visible().await;
        let read = inventory_api::read::<PoliciesBody>(POLICY_PATH).await;
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

/// The Policy page. The header is the same in every case; the body depends on the answer.
#[component]
pub fn Policy() -> Element {
    let ctx = use_context::<Ctx>();
    let state = use_signal(|| Admin::<PoliciesBody>::Loading);
    let now = use_signal(|| None::<i64>);
    use_hook(move || {
        spawn(poll(state, now, ctx.me));
    });

    let shown = state.read().clone();
    let now_ms = *now.read();
    let lede = match &shown {
        Admin::Shown { body, .. } => match &body.git_ref {
            Some(git_ref) => subtitle(git_ref),
            None => POLICY.purpose.to_string(),
        },
        _ => POLICY.purpose.to_string(),
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
                h1 { {icon(POLICY.icon)} "{POLICY.title}" }
                span { class: "authority", "Source of truth: {POLICY.authority}" }
                DisabledAction {
                    label: "Propose a change".to_string(),
                    reason: PROPOSE_TITLE,
                    id: "policy-propose-reason".to_string(),
                }
                p { "{lede}" }
                if let Some(read) = read {
                    p { "{read}." }
                }
            }
            PolicyView { state: shown, now_ms }
        }
    }
}

/// What the page shows for each state of the poll.
#[component]
fn PolicyView(state: Admin<PoliciesBody>, now_ms: Option<i64>) -> Element {
    match state {
        Admin::Loading => rsx! { Notice { message: "Loading policy files\u{2026}".to_string() } },
        Admin::Forbidden => rsx! { Notice { message: "Platform admins only.".to_string() } },
        Admin::Missing => rsx! {
            Notice { message: "The policy answer was not found.".to_string() }
        },
        Admin::SignedOut => rsx! {
            Notice { message: "Your session ended. Sign in again to see policy files.".to_string() }
        },
        Admin::Unavailable(reason) => rsx! {
            Notice { message: format!(
                "Policy files could not be loaded: {reason}. It retries every {} seconds while this tab is visible.",
                POLL_MS / 1000
            ) }
        },
        Admin::Shown { body, reason } => rsx! {
            PolicyBodyView { body, reason: reason.unwrap_or_default(), now_ms }
        },
    }
}

/// The body of an answer the console read. Any state but `ok` is one sentence. An `ok` answer is the two
/// cards, under the banners that say what is stale or cut short.
#[component]
fn PolicyBodyView(body: PoliciesBody, reason: String, now_ms: Option<i64>) -> Element {
    if let Some(message) = not_ok_message(body.state, body.error.as_deref()) {
        return rsx! { Notice { message } };
    }
    // The stale banner names the read the list is from, so an admin sees how old the shown list is.
    let banner = body.error.clone().map(|line| match &body.refreshed_at {
        Some(stamp) => format!("{line} {}.", read_label(now_ms, stamp)),
        None => line,
    });
    let opa = opa_files(&body.files);
    let openbao = openbao_files(&body.files);
    rsx! {
        if !reason.is_empty() {
            p { class: "stale", role: "status", "The last read failed: {reason}." }
        }
        if let Some(line) = banner {
            p { class: "stale", role: "status", "{line}" }
        }
        PolicyCard {
            title: "OPA policies",
            files: opa,
            now_ms,
            empty_text: "No OPA policy files on this ref.",
        }
        PolicyCard {
            title: "OpenBao policies",
            files: openbao,
            now_ms,
            empty_text: "No OpenBao policy files on this ref.",
        }
    }
}

/// One card: a table of files with their kind, last change and link.
#[component]
fn PolicyCard(
    title: &'static str,
    files: Vec<PolicyFile>,
    now_ms: Option<i64>,
    empty_text: &'static str,
) -> Element {
    let empty = files.is_empty();
    rsx! {
        section { class: "card",
            div { class: "card-h", h2 { "{title}" } }
            div { class: "tbl-wrap",
                table { class: "tbl",
                    thead {
                        tr {
                            th { "Name" }
                            th { "Kind" }
                            th { "Last change" }
                            th { "Source" }
                        }
                    }
                    tbody {
                        for file in files.iter() {
                            tr { key: "{file.path}",
                                td { class: "mono", "{file.name}" }
                                td { span { class: "tag", "{kind_label(&file.kind)}" } }
                                td { {last_change(now_ms, file)} }
                                td {
                                    a {
                                        href: "{file.url}",
                                        target: "_blank",
                                        rel: "noopener noreferrer",
                                        "View"
                                    }
                                }
                            }
                        }
                    }
                }
            }
            if empty {
                p { class: "empty", "{empty_text}" }
            }
        }
    }
}

/// The last-change cell: the age of the commit on the client clock (or its date when the clock cannot be
/// read), the short SHA and the subject. A file with no commit on the ref says so.
fn last_change(now_ms: Option<i64>, file: &PolicyFile) -> Element {
    match &file.last_commit {
        Some(commit) => {
            let when = age_text(now_ms, &commit.date).unwrap_or_else(|| commit.date.clone());
            rsx! {
                span { title: "{commit.date}", "{when}" }
                span { class: "mono", " {commit.sha}" }
                span { " {commit.subject}" }
            }
        }
        None => rsx! { span { class: "tag", "No commit on this ref" } },
    }
}
