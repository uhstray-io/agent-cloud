//! The Access page: the people and service accounts Authentik holds, their groups, and how each
//! access level maps into each system. Read-only. Access changes arrive with proposals (Phase 4).

use dioxus::prelude::*;

use super::access_api::{
    not_ok_message, role_rows, sorted_people, stale_line, status_label, AccessBody, ACCESS_PATH,
};
use super::empty::PageMeta;
use super::icons::icon;
use super::inventory::{poll_admin, Admin, POLL_MS};
use super::shell::Ctx;
use super::status::Notice;

/// The page's nav entry and header text.
pub const ACCESS: PageMeta = PageMeta {
    id: "access",
    title: "Access",
    icon: "users",
    purpose: "People and service accounts in Authentik, their groups, and how each access level maps into each system. Read-only: access changes arrive with proposals (Phase 4).",
    authority: "Authentik",
};

/// Why the Add user button is off: access changes arrive with proposals (Phase 4).
const ADD_USER_TITLE: &str = "Access changes arrive with proposals (Phase 4)";

/// Says where the role map comes from, and that a row marked target is not provisioned.
const ROLE_MAP_CAPTION: &str = "From plan/architecture/04-credentials-access.md; rows marked target are the design, not yet provisioned.";

/// The Access page. The header is the same in every case; the body depends on the answer.
#[component]
pub fn Access() -> Element {
    let ctx = use_context::<Ctx>();
    let state = use_signal(|| Admin::<AccessBody>::Loading);
    use_hook(move || {
        spawn(poll_admin(ACCESS_PATH.to_string(), state, ctx.me));
    });

    let shown = state.read().clone();
    rsx! {
        div { class: "view",
            header { class: "view-h",
                h1 { {icon(ACCESS.icon)} "{ACCESS.title}" }
                span { class: "authority", "Source of truth: {ACCESS.authority}" }
                p { "{ACCESS.purpose}" }
            }
            AccessView { state: shown }
        }
    }
}

/// What the page shows for each state of the poll.
#[component]
fn AccessView(state: Admin<AccessBody>) -> Element {
    match state {
        Admin::Loading => rsx! { Notice { message: "Loading access\u{2026}".to_string() } },
        Admin::Forbidden => rsx! { Notice { message: "Platform admins only.".to_string() } },
        Admin::Missing => rsx! {
            Notice { message: "The access answer was not found.".to_string() }
        },
        Admin::SignedOut => rsx! {
            Notice { message: "Your session ended. Sign in again to see access.".to_string() }
        },
        Admin::Unavailable(reason) => rsx! {
            Notice { message: format!(
                "Access could not be loaded: {reason}. It retries every {} seconds while this tab is visible.",
                POLL_MS / 1000
            ) }
        },
        Admin::Shown { body, reason } => rsx! {
            AccessBodyView { body, reason: reason.unwrap_or_default() }
        },
    }
}

/// The body of an answer the console read. The people and groups cards show the directory or the
/// sentence that says why there is none. The role map is always shown, because the server sends it
/// in every state.
#[component]
fn AccessBodyView(body: AccessBody, reason: String) -> Element {
    let error = body.error.as_deref();
    let stale = stale_line(body.state, error);
    let directory = not_ok_message(body.state, error);
    let people = sorted_people(&body.users);
    let roles = role_rows(&body.role_map);
    rsx! {
        if !reason.is_empty() {
            p { class: "stale", role: "status", "The last read failed: {reason}." }
        }
        if let Some(line) = stale {
            p { class: "stale", role: "status", "{line}" }
        }
        section { class: "card",
            div { class: "card-h",
                h2 { "People" }
                button { class: "btn", disabled: true, title: "{ADD_USER_TITLE}", "Add user" }
            }
            if let Some(message) = &directory {
                p { class: "empty", "{message}" }
            } else {
                div { class: "tbl-wrap",
                    table { class: "tbl",
                        thead {
                            tr {
                                th { "Person" }
                                th { "Email" }
                                th { "Groups" }
                            }
                        }
                        tbody {
                            for user in people.iter() {
                                tr { key: "{user.username}",
                                    td {
                                        div { class: "row wrap",
                                            span { "{user.name}" }
                                            if user.service_account {
                                                span { class: "tag", "service account" }
                                            }
                                            if !user.active {
                                                span { class: "tag", "inactive" }
                                            }
                                        }
                                        div { class: "mono faint", "{user.username}" }
                                    }
                                    td { {user.email.as_deref().unwrap_or("\u{2014}")} }
                                    td {
                                        div { class: "chips",
                                            for group in user.groups.iter() {
                                                span { key: "{group}", class: "tag mono", "{group}" }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
                if people.is_empty() {
                    p { class: "empty", "Authentik holds no users." }
                }
            }
        }
        section { class: "card",
            div { class: "card-h",
                h2 { "Groups" }
            }
            if let Some(message) = &directory {
                p { class: "empty", "{message}" }
            } else {
                div { class: "tbl-wrap",
                    table { class: "tbl",
                        thead {
                            tr {
                                th { "Group" }
                                th { class: "num", "Members" }
                            }
                        }
                        tbody {
                            for group in body.groups.iter() {
                                tr { key: "{group.name}",
                                    td {
                                        div { class: "row wrap",
                                            span { class: "mono", "{group.name}" }
                                            if group.superuser {
                                                span { class: "tag", "superuser" }
                                            }
                                        }
                                    }
                                    td { class: "num", "{group.members}" }
                                }
                            }
                        }
                    }
                }
                if body.groups.is_empty() {
                    p { class: "empty", "Authentik holds no groups." }
                }
            }
        }
        section { class: "card",
            div { class: "card-h",
                h2 { "Access level → per-system roles" }
            }
            div { class: "tbl-wrap",
                table { class: "tbl",
                    thead {
                        tr {
                            th { "Level" }
                            for system in body.role_map.systems.iter() {
                                th { key: "{system}", "{system}" }
                            }
                            th { "Status" }
                        }
                    }
                    tbody {
                        for row in roles.iter() {
                            tr { key: "{row.level}",
                                td { "{row.level}" }
                                for (index, cell) in row.cells.iter().enumerate() {
                                    td { key: "{index}", "{cell}" }
                                }
                                td {
                                    span { class: "tag", {status_label(row.status)} }
                                }
                            }
                        }
                    }
                }
            }
            if roles.is_empty() {
                p { class: "empty", "The role map has no levels." }
            }
            div { class: "card-b",
                p { class: "hint", "{ROLE_MAP_CAPTION}" }
            }
        }
    }
}
