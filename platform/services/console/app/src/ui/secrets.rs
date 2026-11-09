//! The Secrets page (plan 17 D35): what OpenBao metadata holds, for platform admins. Paths,
//! versions and rotation age. The console never shows a value, and rotation waits for Phase 4.

use dioxus::prelude::*;

use super::api::{self, Me};
use super::empty::PageMeta;
use super::icons::icon;
use super::inventory::{admin_next, end_session, keeps_polling_admin, Admin, POLL_MS};
use super::inventory_api;
use super::secrets_api::{age_label, not_ok_message, SecretsBody, SECRETS_PATH};
use super::shell::Ctx;
use super::status::Notice;

/// The page's nav entry and header text.
pub const SECRETS: PageMeta = PageMeta {
    id: "secrets",
    title: "Secrets",
    icon: "key-round",
    purpose: "Paths, versions and rotation age. The console never displays a secret value; rotation runs as a Semaphore template.",
    authority: "OpenBao",
};

/// Why the Rotate button is off: rotation arrives with proposals (D28, Phase 4).
const ROTATE_TITLE: &str = "Rotation arrives with proposals (Phase 4)";

/// The element that shows `ROTATE_TITLE` as visible text above the table; each Rotate button describes itself by it.
const ROTATE_REASON_ID: &str = "secrets-action-reason";

/// Shown above the table when a bound stopped the walk.
const TRUNCATED_TEXT: &str =
    "The list is truncated: a walk bound stopped it, so it is not the whole of OpenBao.";

/// The page's one poll, as `poll_admin` runs it. It also keeps the client clock each answer
/// arrived at, so the ages in the table are read against that clock.
async fn poll(mut state: Signal<Admin<SecretsBody>>, mut now: Signal<Option<i64>>, me: Signal<Me>) {
    loop {
        api::wait_visible().await;
        let read = inventory_api::read::<SecretsBody>(SECRETS_PATH).await;
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

/// The Secrets page. The header is the same in every case; the body depends on the answer.
#[component]
pub fn Secrets() -> Element {
    let ctx = use_context::<Ctx>();
    let state = use_signal(|| Admin::<SecretsBody>::Loading);
    let now = use_signal(|| None::<i64>);
    use_hook(move || {
        spawn(poll(state, now, ctx.me));
    });

    let shown = state.read().clone();
    let now_ms = *now.read();
    rsx! {
        div { class: "view",
            header { class: "view-h",
                h1 { {icon(SECRETS.icon)} "{SECRETS.title}" }
                span { class: "authority", "Source of truth: {SECRETS.authority}" }
                p { "{SECRETS.purpose}" }
            }
            SecretsView { state: shown, now_ms }
        }
    }
}

/// What the page shows for each state of the poll.
#[component]
fn SecretsView(state: Admin<SecretsBody>, now_ms: Option<i64>) -> Element {
    match state {
        Admin::Loading => rsx! { Notice { message: "Loading secrets\u{2026}".to_string() } },
        Admin::Forbidden => rsx! { Notice { message: "Platform admins only.".to_string() } },
        Admin::Missing => rsx! {
            Notice { message: "The secrets answer was not found.".to_string() }
        },
        Admin::SignedOut => rsx! {
            Notice { message: "Your session ended. Sign in again to see secrets.".to_string() }
        },
        Admin::Unavailable(reason) => rsx! {
            Notice { message: format!(
                "Secrets could not be loaded: {reason}. It retries every {} seconds while this tab is visible.",
                POLL_MS / 1000
            ) }
        },
        Admin::Shown { body, reason } => rsx! {
            SecretsBodyView { body, reason: reason.unwrap_or_default(), now_ms }
        },
    }
}

/// The body of an answer the console read. Any state but `ok` is one sentence. An `ok` answer
/// is the table, under the banners that say what is stale or cut short.
#[component]
fn SecretsBodyView(body: SecretsBody, reason: String, now_ms: Option<i64>) -> Element {
    if let Some(message) = not_ok_message(body.state, body.error.as_deref()) {
        return rsx! { Notice { message } };
    }
    let stale = body.error.as_ref().map(|error| {
        let when = body.refreshed_at.as_deref().map_or_else(
            || "an earlier walk".to_string(),
            |stamp| age_label(now_ms, stamp),
        );
        format!("The newest walk failed: {error}. The paths below are from {when}.")
    });
    let truncated = body.truncated;
    let empty = body.paths.is_empty();
    rsx! {
        if !reason.is_empty() {
            p { class: "stale", role: "status", "The last read failed: {reason}." }
        }
        if let Some(line) = stale {
            p { class: "stale", role: "status", "{line}" }
        }
        if truncated {
            p { class: "stale", role: "status", "{TRUNCATED_TEXT}" }
        }
        section { class: "card",
            if !empty {
                div { class: "card-b",
                    p { id: ROTATE_REASON_ID, class: "action-reason", "{ROTATE_TITLE}" }
                }
            }
            div { class: "tbl-wrap",
                table { class: "tbl",
                    thead {
                        tr {
                            th { "Path" }
                            th { class: "num", "Version" }
                            th { class: "num", "Versions kept" }
                            th { "Created" }
                            th { "Last changed" }
                            th { "Rotation" }
                        }
                    }
                    tbody {
                        for row in body.paths.iter() {
                            tr { key: "{row.path}",
                                td { class: "mono", "{row.path}" }
                                td { class: "num", "{row.current_version}" }
                                td { class: "num", "{row.versions}" }
                                td { title: "{row.created_time}", {age_label(now_ms, &row.created_time)} }
                                td { title: "{row.updated_time}", {age_label(now_ms, &row.updated_time)} }
                                td {
                                    button {
                                        class: "btn",
                                        disabled: true,
                                        aria_describedby: ROTATE_REASON_ID,
                                        title: "{ROTATE_TITLE}",
                                        "Rotate"
                                    }
                                }
                            }
                        }
                    }
                }
            }
            if empty {
                p { class: "empty", "OpenBao holds no secret paths." }
            }
        }
    }
}
