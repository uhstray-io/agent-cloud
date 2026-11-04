//! The agent role page (plan 17, D40): one role of the service-onboarding registry, the work it holds in the
//! Active Work list, and the gateway client of the same name. The steps come from `GET /v1/roles` (read once
//! by the shell), the work from the Active Work poll the shell already runs, the client from `GET /v1/gateway`.
//! Everything decided here is a plain function with a test; the components only draw what those return.

use dioxus::prelude::*;

use super::api::{self, Me};
use super::gateway_api::{
    budget_source_text, budget_text, last_hour_text, not_ok_message, usage_notice, usage_percent,
    GatewayBody, GatewayClient, Usage, APPROXIMATE, GATEWAY_PATH,
};
use super::icons::icon;
use super::inventory::{poll_admin, Admin};
use super::roles_api::{role_named, RoleList, ROLES_PATH};
use super::shell::{is_platform_admin, Ctx, Loaded};
use super::status::{Meter, Notice};
use super::work::{board_items, item_age, Lane, Source, WorkItem, WorkState};
use super::Route;

/// The Active Work strips that belong to `role`: the board's items, one per job as the board shows them,
/// whose role is `role`. Earlier runs of a job are not listed here, as they are not on the board.
pub fn role_work(items: &[WorkItem], role: &str) -> Vec<WorkItem> {
    board_items(items)
        .into_iter()
        .filter(|item| item.role.as_deref() == Some(role))
        .collect()
}

/// The gateway client named `role`, if the answer has one. Names match exactly.
pub fn client_named<'a>(clients: &'a [GatewayClient], role: &str) -> Option<&'a GatewayClient> {
    clients.iter().find(|client| client.name == role)
}

/// Reads the registry's roles once into the shell. Only platform admins call it, since the server refuses
/// every other caller.
pub async fn load_roles(mut roles: Signal<Loaded<RoleList>>) {
    let reply = api::request("GET", ROLES_PATH, None).await;
    match api::parse::<RoleList>(&reply) {
        Some(list) => roles.set(Loaded::Ready(list)),
        None => roles.set(Loaded::Failed),
    }
}

/// The role page. Platform admins only: the registry, the work and the gateway answer it shows are all
/// admin reads. The gateway poll starts only for an admin.
#[component]
pub fn RoleDetail(role: String) -> Element {
    let ctx = use_context::<Ctx>();
    let admin =
        matches!(&*ctx.me.read(), Me::SignedIn(account) if is_platform_admin(&account.claims));
    let gateway = use_signal(|| Admin::<GatewayBody>::Loading);
    use_hook(move || {
        if admin {
            spawn(poll_admin(GATEWAY_PATH.to_string(), gateway, ctx.me));
        }
    });
    rsx! {
        div { class: "view role",
            header { class: "view-h",
                h1 { {icon("bot")} "{role}" }
                span { class: "authority", "Source of truth: service-onboarding registry, Semaphore, agentgateway" }
                p { "The steps this role owns, the work it holds now, and its gateway client." }
            }
            if admin {
                StepsCard { role: role.clone() }
                WorkCard { role: role.clone() }
                GatewayCard { role, gateway }
            } else {
                Notice { message: "Agent roles are limited to platform administrators.".to_string() }
            }
        }
    }
}

/// The registry steps the role owns, in order. The shell's roles read supplies them.
#[component]
fn StepsCard(role: String) -> Element {
    let ctx = use_context::<Ctx>();
    let roles = ctx.roles.read().clone();
    let body = match roles {
        Loaded::Loading => rsx! { p { class: "empty", "Loading the registry\u{2026}" } },
        Loaded::Failed => rsx! { p { class: "empty", "The agent roles could not be loaded." } },
        Loaded::Ready(list) => match role_named(&list, &role) {
            Some(found) => rsx! {
                div { class: "tbl-wrap",
                    table { class: "tbl",
                        thead {
                            tr {
                                th { class: "num", "Order" }
                                th { "Step" }
                                th { "Id" }
                            }
                        }
                        tbody {
                            for step in found.steps.iter() {
                                tr { key: "{step.id}",
                                    td { class: "num", "{step.order}" }
                                    td { "{step.label}" }
                                    td { class: "mono", "{step.id}" }
                                }
                            }
                        }
                    }
                }
            },
            None => rsx! { p { class: "empty", "No agent role named {role}." } },
        },
    };
    rsx! {
        section { class: "card",
            div { class: "card-h", h2 { "Steps" } }
            {body}
        }
    }
}

/// The role's Active Work strips, from the list the shell already polls.
#[component]
fn WorkCard(role: String) -> Element {
    let ctx = use_context::<Ctx>();
    let state = ctx.work.read().clone();
    let body = match &state {
        WorkState::Loading => rsx! { p { class: "empty", "Loading work\u{2026}" } },
        WorkState::Forbidden | WorkState::SignedOut => {
            rsx! { p { class: "empty", "Active Work is not available to this session." } }
        }
        WorkState::Unreachable => rsx! { p { class: "empty", "Active Work could not be loaded." } },
        WorkState::Shown { list, .. } if list.source == Source::Unconfigured => {
            rsx! { p { class: "empty", "Semaphore connection not configured." } }
        }
        WorkState::Shown { list, now_ms, .. } => {
            let items = role_work(&list.items, &role);
            let now_ms = *now_ms;
            rsx! { WorkTable { items, now_ms } }
        }
    };
    rsx! {
        section { class: "card",
            div { class: "card-h", h2 { "Active work" } }
            {body}
        }
    }
}

/// One row per strip: its key (a link to Active Work, where the strip is), title, status and age.
#[component]
fn WorkTable(items: Vec<WorkItem>, now_ms: Option<i64>) -> Element {
    if items.is_empty() {
        return rsx! { p { class: "empty", "No work for this role in the Active Work list." } };
    }
    rsx! {
        div { class: "tbl-wrap",
            table { class: "tbl",
                thead {
                    tr {
                        th { "Key" }
                        th { "Title" }
                        th { "Status" }
                        th { class: "num", "Age" }
                    }
                }
                tbody {
                    for item in items.iter() {
                        tr { key: "{item.key}",
                            td { class: "mono",
                                Link { to: Route::Home {}, "{item.key}" }
                            }
                            td { "{item.title}" }
                            td { LaneCell { lane: item.lane } }
                            td { class: "num faint",
                                {item_age(now_ms, item).unwrap_or_else(|| "\u{2014}".to_string())}
                            }
                        }
                    }
                }
            }
        }
    }
}

/// A lane as the board draws it: its shape, its tone and its word.
#[component]
fn LaneCell(lane: Lane) -> Element {
    let (shape, tone) = lane.look();
    let spin = if lane == Lane::Running { "spin" } else { "" };
    rsx! {
        span { class: "st {tone.class()}",
            span { class: "{spin}", {icon(shape)} }
            "{lane.label()}"
        }
    }
}

/// The role's gateway client, from the one poll of `GET /v1/gateway` the page starts.
#[component]
fn GatewayCard(role: String, gateway: Signal<Admin<GatewayBody>>) -> Element {
    let shown = gateway.read().clone();
    let body = match shown {
        Admin::Loading => rsx! { p { class: "empty", "Loading the gateway\u{2026}" } },
        Admin::Forbidden => rsx! { p { class: "empty", "Platform admins only." } },
        Admin::Missing => rsx! { p { class: "empty", "The gateway answer was not found." } },
        Admin::SignedOut => {
            rsx! { p { class: "empty", "Your session ended. Sign in again to see the gateway." } }
        }
        Admin::Unavailable(reason) => {
            rsx! { p { class: "empty", "The gateway could not be loaded: {reason}." } }
        }
        Admin::Shown { body, .. } => match not_ok_message(body.state) {
            Some(message) => rsx! { p { class: "empty", "{message}" } },
            None => match client_named(&body.clients, &role) {
                Some(client) => rsx! {
                    ClientFacts { client: client.clone(), usage: body.usage.clone() }
                },
                None => rsx! { p { class: "empty", "No gateway client named {role}." } },
            },
        },
    };
    rsx! {
        section { class: "card",
            div { class: "card-h", h2 { "Gateway client" } }
            {body}
        }
    }
}

/// A gateway client's budget, tier, last-hour use and allowed models, with the usage notice when the read is
/// not current.
#[component]
fn ClientFacts(client: GatewayClient, usage: Usage) -> Element {
    let notice = usage_notice(&usage);
    rsx! {
        div { class: "card-b",
            if let Some(line) = notice {
                p { class: "stale", role: "status", "{line}" }
            }
            div { class: "field-row",
                dt { class: "label", "Tier" }
                dd {
                    {client.tier.as_deref().unwrap_or("\u{2014}")}
                }
            }
            div { class: "field-row",
                dt { class: "label", "Budget / hour" }
                dd { class: "num",
                    span { "{budget_text(client.tokens_per_hour)}" }
                    if let Some(source) = budget_source_text(client.budget_source) {
                        span { class: "hint", " {source}" }
                    }
                }
            }
            div { class: "field-row",
                dt { class: "label", "Last hour" }
                dd { class: "num", title: "{APPROXIMATE}",
                    span { "{last_hour_text(client.tokens_last_hour)}" }
                    if let Some(pct) = usage_percent(client.tokens_last_hour, client.tokens_per_hour) {
                        Meter { label: format!("{} last hour, share of its budget", client.name), pct }
                    }
                }
            }
            div { class: "field-row",
                dt { class: "label", "Models" }
                dd {
                    if client.allowed_models.is_empty() {
                        span { class: "faint", "none" }
                    }
                    for model in client.allowed_models.iter() {
                        span { class: "tag mono", key: "{model}", "{model}" }
                    }
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn work_item(key: &str, role: Option<&str>, latest: bool) -> WorkItem {
        WorkItem {
            key: key.to_string(),
            title: format!("Title {key}"),
            lane: Lane::Running,
            status: "running".to_string(),
            role: role.map(str::to_string),
            launched_by: "skynet".to_string(),
            run_id: None,
            step: None,
            service: None,
            created: "2026-10-26T12:00:00Z".to_string(),
            started: None,
            ended: None,
            receipt_url: "https://semaphore.example.test/project/1/history?t=1".to_string(),
            latest_in_stream: latest,
            stream_runs: 1,
        }
    }

    fn keys(items: &[WorkItem]) -> Vec<&str> {
        items.iter().map(|item| item.key.as_str()).collect()
    }

    #[test]
    fn a_role_page_lists_only_its_own_board_strips() {
        let items = vec![
            work_item("sem-1", Some("netclaw"), true),
            work_item("sem-2", Some("netclaw"), false),
            work_item("sem-3", Some("bridge"), true),
            work_item("sem-4", None, true),
        ];
        assert_eq!(keys(&role_work(&items, "netclaw")), ["sem-1"]);
        assert_eq!(keys(&role_work(&items, "bridge")), ["sem-3"]);
        assert!(role_work(&items, "ghost").is_empty());
        assert!(
            role_work(&items, "operator").is_empty(),
            "a launched-by-person item has no role to match"
        );
    }

    #[test]
    fn the_gateway_client_is_the_one_with_the_exact_name() {
        let clients = [
            GatewayClient {
                name: "alpha".into(),
                ..GatewayClient::default()
            },
            GatewayClient {
                name: "alpha-2".into(),
                ..GatewayClient::default()
            },
        ];
        assert_eq!(
            client_named(&clients, "alpha").map(|c| c.name.as_str()),
            Some("alpha")
        );
        assert!(client_named(&clients, "alp").is_none());
        assert!(client_named(&clients, "beta").is_none());
    }
}
