//! The Request a service page (plan 17, task 8.4, D38): the services the platform can deploy, from its deploy
//! templates, with a tag beside each one a host already runs. It reads `GET /v1/catalog`, for any signed-in member.
//! The Request buttons stay off until proposals arrive (Phase 4), so nothing on this page writes.

use dioxus::prelude::*;

use super::empty::PageMeta;
use super::icons::icon;
use super::inventory::{poll_admin, Admin, POLL_MS};
use super::request_api::{not_ok_message, CatalogBody, CatalogService, CATALOG_PATH};
use super::shell::Ctx;
use super::status::Notice;

/// The Request a service page's nav entry and header text.
pub const REQUEST_SERVICE: PageMeta = PageMeta {
    id: "request",
    title: "Request a service",
    icon: "user-round",
    purpose: "Services the platform can deploy, from its deploy templates. Requests arrive with proposals (Phase 4).",
    authority: "Semaphore deploy templates",
};

/// Why the Request buttons are off: requests arrive with proposals (Phase 4).
const REQUEST_TITLE: &str = "Service requests arrive with proposals (Phase 4)";

/// The element that shows `REQUEST_TITLE` as visible text; each Request button describes itself by it.
const REQUEST_REASON_ID: &str = "request-action-reason";

/// The Request a service page: the header, and the one poll of `GET /v1/catalog` behind the list.
#[component]
pub fn RequestService() -> Element {
    let ctx = use_context::<Ctx>();
    let state = use_signal(|| Admin::<CatalogBody>::Loading);
    use_hook(move || {
        spawn(poll_admin(CATALOG_PATH.to_string(), state, ctx.me));
    });
    let shown = state.read().clone();
    rsx! {
        div { class: "view",
            header { class: "view-h",
                h1 { {icon(REQUEST_SERVICE.icon)} "{REQUEST_SERVICE.title}" }
                span { class: "authority", "Source of truth: {REQUEST_SERVICE.authority}" }
                p { "{REQUEST_SERVICE.purpose}" }
            }
            CatalogView { state: shown }
        }
    }
}

/// What the page shows for each state of the poll. Every signed-in member may read the catalog, so no state is
/// an admin-only refusal.
#[component]
fn CatalogView(state: Admin<CatalogBody>) -> Element {
    match state {
        Admin::Loading => rsx! { Notice { message: "Loading the services\u{2026}".to_string() } },
        Admin::Forbidden => rsx! {
            Notice { message: "The service catalog is not available to this account.".to_string() }
        },
        Admin::Missing => rsx! {
            Notice { message: "The service catalog answer was not found.".to_string() }
        },
        Admin::SignedOut => rsx! {
            Notice { message: "Your session ended. Sign in again to see the services.".to_string() }
        },
        Admin::Unavailable(reason) => rsx! {
            Notice { message: format!(
                "The services could not be loaded: {reason}. It retries every {} seconds while this tab is visible.",
                POLL_MS / 1000
            ) }
        },
        Admin::Shown { body, reason } => rsx! {
            CatalogBodyView { body, reason: reason.unwrap_or_default() }
        },
    }
}

/// The body of an answer the console read. Any state but `ok` is one sentence. An `ok` answer is the list, under
/// the banner that says a later read failed.
#[component]
fn CatalogBodyView(body: CatalogBody, reason: String) -> Element {
    if let Some(message) = not_ok_message(body.state) {
        return rsx! { Notice { message } };
    }
    rsx! {
        if !reason.is_empty() {
            p { class: "stale", role: "status", "The last read failed: {reason}." }
        }
        ServiceTable { services: body.services }
        p { id: REQUEST_REASON_ID, class: "hint", "{REQUEST_TITLE}." }
    }
}

/// One row per deployable service: its name with a tag when a host runs it, its deploy template, and the request
/// control, which stays off until proposals land.
#[component]
fn ServiceTable(services: Vec<CatalogService>) -> Element {
    let empty = services.is_empty();
    rsx! {
        section { class: "card",
            div { class: "tbl-wrap",
                table { class: "tbl",
                    thead {
                        tr {
                            th { "Service" }
                            th { "Deploy template" }
                            th { "Request" }
                        }
                    }
                    tbody {
                        for service in services.iter() {
                            tr { key: "{service.service}",
                                td {
                                    span { "{service.name}" }
                                    if service.running {
                                        span { class: "tag", "running here" }
                                    }
                                }
                                td { class: "mono", "{service.template}" }
                                td {
                                    button {
                                        class: "btn",
                                        disabled: true,
                                        aria_describedby: REQUEST_REASON_ID,
                                        title: "{REQUEST_TITLE}",
                                        "Request"
                                    }
                                }
                            }
                        }
                    }
                }
            }
            if empty {
                p { class: "empty", "No service has a deploy template." }
            }
        }
    }
}
