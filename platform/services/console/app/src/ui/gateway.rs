//! The AI Gateway and LLM access pages (plan 17, task 8.3a): which models the inference gateway
//! serves and which clients may call each, and the hourly token budget each client is held to.
//! Both read `GET /v1/gateway`, for platform admins. Neither shows or changes a key: rotation and
//! revocation arrive with proposals (Phase 4).

use dioxus::prelude::*;

use super::api::{self, Me};
use super::empty::PageMeta;
use super::gateway_api::{
    budget_source_text, budget_text, clients_by_model, last_hour_text, not_ok_message,
    usage_notice, usage_percent, usage_read_text, GatewayBody, GatewayClient, APPROXIMATE,
    GATEWAY_PATH,
};
use super::icons::icon;
use super::inventory::{admin_next, end_session, keeps_polling_admin, Admin, POLL_MS};
use super::inventory_api;
use super::shell::Ctx;
use super::status::{Meter, Notice};

/// The AI Gateway page's nav entry and header text.
pub const AI_GATEWAY: PageMeta = PageMeta {
    id: "agents",
    title: "AI Gateway",
    icon: "door-open",
    purpose: "The models the inference gateway serves and which clients may use each.",
    authority: "agentgateway",
};

/// The LLM access page's nav entry and header text.
pub const LLM_ACCESS: PageMeta = PageMeta {
    id: "llm",
    title: "LLM access",
    icon: "message-square-lock",
    purpose: "The gateway's clients, the models each may call, and the hourly token budget each is held to. Keys are never shown.",
    authority: "agentgateway",
};

/// Why the Rotate and Revoke buttons are off: key changes arrive with proposals (Phase 4).
const KEY_TITLE: &str = "Key changes arrive with proposals (Phase 4)";

/// Shown beneath both tables: the reason some platform admins are absent from them.
const ADMIN_NOTE: &str = "Platform admins with a personal gateway identity that is not also a listed client are not shown; the gateway enrols them at deploy with the admin tier.";

/// Which of the two tables a page shows.
#[derive(Debug, Clone, Copy, PartialEq)]
enum Panel {
    Models,
    Access,
}

/// The AI Gateway page: the served models, each with the clients allowed to call it.
#[component]
pub fn AiGateway() -> Element {
    rsx! { GatewayPage { page: AI_GATEWAY, panel: Panel::Models } }
}

/// The LLM access page: each client with its tier, hourly budget and models.
#[component]
pub fn LlmAccess() -> Element {
    rsx! { GatewayPage { page: LLM_ACCESS, panel: Panel::Access } }
}

/// The gateway's one poll, as `poll_admin` runs it. It also keeps the client clock each answer arrived at, so the
/// usage age is read against that clock, as the Secrets page reads its ages. The role page uses it too.
pub async fn poll_gateway(
    mut state: Signal<Admin<GatewayBody>>,
    mut now: Signal<Option<i64>>,
    me: Signal<Me>,
) {
    loop {
        api::wait_visible().await;
        let read = inventory_api::read::<GatewayBody>(GATEWAY_PATH).await;
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

/// The header both pages share, and the one poll of `GET /v1/gateway` behind the body.
#[component]
fn GatewayPage(page: PageMeta, panel: Panel) -> Element {
    let ctx = use_context::<Ctx>();
    let state = use_signal(|| Admin::<GatewayBody>::Loading);
    let now = use_signal(|| None::<i64>);
    use_hook(move || {
        spawn(poll_gateway(state, now, ctx.me));
    });
    let shown = state.read().clone();
    let now_ms = *now.read();
    rsx! {
        div { class: "view",
            header { class: "view-h",
                h1 { {icon(page.icon)} "{page.title}" }
                span { class: "authority", "Source of truth: {page.authority}" }
                p { "{page.purpose}" }
            }
            GatewayView { state: shown, panel, now_ms }
        }
    }
}

/// What the page shows for each state of the poll.
#[component]
fn GatewayView(state: Admin<GatewayBody>, panel: Panel, now_ms: Option<i64>) -> Element {
    match state {
        Admin::Loading => rsx! { Notice { message: "Loading the gateway\u{2026}".to_string() } },
        Admin::Forbidden => rsx! { Notice { message: "Platform admins only.".to_string() } },
        Admin::Missing => rsx! {
            Notice { message: "The gateway answer was not found.".to_string() }
        },
        Admin::SignedOut => rsx! {
            Notice { message: "Your session ended. Sign in again to see the gateway.".to_string() }
        },
        Admin::Unavailable(reason) => rsx! {
            Notice { message: format!(
                "The gateway could not be loaded: {reason}. It retries every {} seconds while this tab is visible.",
                POLL_MS / 1000
            ) }
        },
        Admin::Shown { body, reason } => rsx! {
            GatewayBodyView { body, panel, reason: reason.unwrap_or_default(), now_ms }
        },
    }
}

/// The body of an answer the console read. Any state but `ok` is one sentence. An `ok` answer
/// is the table, under the banner that says a later read failed.
#[component]
fn GatewayBodyView(
    body: GatewayBody,
    panel: Panel,
    reason: String,
    now_ms: Option<i64>,
) -> Element {
    if let Some(message) = not_ok_message(body.state) {
        return rsx! { Notice { message } };
    }
    let notice = usage_notice(&body.usage, now_ms);
    let usage_read = usage_read_text(now_ms, &body.usage);
    rsx! {
        if !reason.is_empty() {
            p { class: "stale", role: "status", "The last read failed: {reason}." }
        }
        match panel {
            Panel::Models => rsx! { ModelTable { clients: body.clients, models: body.models } },
            Panel::Access => rsx! {
                if let Some(line) = notice {
                    p { class: "stale", role: "status", "{line}" }
                }
                AccessTable { clients: body.clients, usage_read }
            },
        }
        p { class: "hint", "{ADMIN_NOTE}" }
    }
}

/// One row per served model, with the clients whose allow list names it.
#[component]
fn ModelTable(clients: Vec<GatewayClient>, models: Vec<String>) -> Element {
    let rows = clients_by_model(&clients, &models);
    let empty = rows.is_empty();
    rsx! {
        section { class: "card",
            div { class: "tbl-wrap",
                table { class: "tbl",
                    thead {
                        tr {
                            th { "Model" }
                            th { "Clients allowed" }
                        }
                    }
                    tbody {
                        for row in rows.iter() {
                            tr { key: "{row.model}",
                                td { class: "mono", "{row.model}" }
                                td {
                                    span { class: "num", "{row.clients.len()}" }
                                    for name in row.clients.iter() {
                                        span { class: "tag mono", key: "{name}", "{name}" }
                                    }
                                }
                            }
                        }
                    }
                }
            }
            if empty {
                p { class: "empty", "The gateway serves no models." }
            }
        }
    }
}

/// One row per client: its tier, its hourly budget with where the budget came from, its last-hour use with the age
/// of the read that gave it, its models, and the key actions, which stay off until proposals land.
#[component]
fn AccessTable(clients: Vec<GatewayClient>, usage_read: Option<String>) -> Element {
    let empty = clients.is_empty();
    rsx! {
        section { class: "card",
            div { class: "tbl-wrap",
                table { class: "tbl",
                    thead {
                        tr {
                            th { "Client" }
                            th { "Tier" }
                            th { class: "num", "Budget / hour" }
                            th { class: "num",
                                "Last hour"
                                if let Some(text) = usage_read.as_deref() {
                                    span { class: "hint", " {text}" }
                                }
                            }
                            th { "Models" }
                            th { "Actions" }
                        }
                    }
                    tbody {
                        for client in clients.iter() {
                            tr { key: "{client.name}",
                                td { class: "mono", "{client.name}" }
                                td {
                                    span {
                                        class: if client.tier.is_some() { "tag" } else { "faint" },
                                        {client.tier.as_deref().unwrap_or("\u{2014}")}
                                    }
                                }
                                td { class: "num",
                                    span { "{budget_text(client.tokens_per_hour)}" }
                                    if let Some(source) = budget_source_text(client.budget_source) {
                                        span { class: "hint", " {source}" }
                                    }
                                }
                                td { class: "num", title: "{APPROXIMATE}",
                                    span { "{last_hour_text(client.tokens_last_hour)}" }
                                    if let Some(pct) = usage_percent(client.tokens_last_hour, client.tokens_per_hour) {
                                        Meter { label: format!("{} last hour, share of its budget", client.name), pct }
                                    }
                                }
                                td {
                                    if client.allowed_models.is_empty() {
                                        span { class: "faint", "none" }
                                    }
                                    for model in client.allowed_models.iter() {
                                        span { class: "tag mono", key: "{model}", "{model}" }
                                    }
                                }
                                td {
                                    div { class: "row",
                                        button { class: "btn", disabled: true, title: "{KEY_TITLE}", "Rotate" }
                                        button { class: "btn", disabled: true, title: "{KEY_TITLE}", "Revoke" }
                                    }
                                }
                            }
                        }
                    }
                }
            }
            if empty {
                p { class: "empty", "No gateway client is configured." }
            }
        }
    }
}
