//! The signed-in shell: sidebar, scope bar and the page outlet. Signed out, the
//! whole screen is the sign-in page.
//!
//! The first frame is the session check, which both the server and the browser
//! render as "Checking session", so hydration matches. Tenants, the scope tree
//! and the saved theme load after the page mounts.

use dioxus::prelude::*;

use super::api::{self, Account, Claims, Me, NamedRef, TenantList, Tree};
use super::icons::icon;
use super::scope::{self, Scope, ScopeBar};
use super::work::{self, WorkState};
use super::Route;

/// A value loaded from the API. `Loading` is also what the first render shows.
#[derive(Debug, Clone, PartialEq)]
pub enum Loaded<T> {
    Loading,
    Ready(T),
    Failed,
}

/// State the sidebar, the scope bar and every page share. `Shell` provides it.
#[derive(Clone, Copy)]
pub struct Ctx {
    pub me: Signal<Me>,
    pub tenants: Signal<Loaded<TenantList>>,
    /// The tenant the sidebar shows, by id.
    pub tenant: Signal<Option<String>>,
    /// The scope tree of `tenant`.
    pub tree: Signal<Loaded<Tree>>,
    pub scope: Signal<Scope>,
    /// The Active Work list. One poll writes it; the page and the sidebar badge read it.
    pub work: Signal<WorkState>,
}

#[component]
pub fn Shell() -> Element {
    let me = use_signal(|| Me::Loading);
    let tenants = use_signal(|| Loaded::Loading);
    let tenant = use_signal(|| None::<String>);
    let tree = use_signal(|| Loaded::Loading);
    let scope = use_signal(Scope::default);
    let work = use_signal(|| WorkState::Loading);
    let ctx = use_context_provider(|| Ctx {
        me,
        tenants,
        tenant,
        tree,
        scope,
        work,
    });

    use_effect(move || {
        spawn(bootstrap(ctx));
    });

    match me() {
        Me::Loading => rsx! {
            main { class: "boot", p { "Checking session\u{2026}" } }
        },
        Me::SignedOut => rsx! { SignIn {} },
        Me::Failed => rsx! {
            main { class: "boot", p { "Could not read session state. Reload the page to try again." } }
        },
        Me::SignedIn(account) => rsx! { Workspace { account } },
    }
}

/// Reads the session, applies the saved theme, then loads the tenants.
async fn bootstrap(mut ctx: Ctx) {
    let me = api::classify_me(&api::request("GET", "/v1/me", None).await);
    let saved = match &me {
        Me::SignedIn(account) => {
            api::apply_theme(&account.preferences.theme);
            account.preferences.default_scope.clone()
        }
        _ => None,
    };
    let signed_in = matches!(me, Me::SignedIn(_));
    ctx.me.set(me);
    if signed_in {
        load_tenants(ctx, saved).await;
    }
}

/// Loads the tenant list and opens its first tenant. `saved` is the stored
/// default scope, which applies only to this first open.
async fn load_tenants(mut ctx: Ctx, saved: Option<String>) {
    let reply = api::request("GET", "/v1/tenants", None).await;
    if reply.status == 401 {
        ctx.me.set(Me::SignedOut);
        return;
    }
    match api::parse::<TenantList>(&reply) {
        Some(list) => {
            let first = list.tenants.first().map(|t| t.id.clone());
            ctx.tenants.set(Loaded::Ready(list));
            if let Some(id) = first {
                select_tenant(ctx, id, saved).await;
            }
        }
        None => {
            ctx.tenants.set(Loaded::Failed);
            ctx.tree.set(Loaded::Failed);
        }
    }
}

/// Makes `tenant_id` the sidebar's tenant and loads its scope tree. The scope
/// resets to the tree's first org, or to the saved default when it resolves.
pub async fn select_tenant(mut ctx: Ctx, tenant_id: String, saved: Option<String>) {
    ctx.tenant.set(Some(tenant_id.clone()));
    ctx.tree.set(Loaded::Loading);
    let path = format!("/v1/tenants/{}/tree", api::segment(&tenant_id));
    let reply = api::request("GET", &path, None).await;
    // A later pick of another tenant wins, so this answer is dropped.
    if ctx.tenant.peek().as_deref() != Some(tenant_id.as_str()) {
        return;
    }
    match api::parse::<Tree>(&reply) {
        Some(tree) => {
            ctx.scope.set(scope::initial(&tree, saved.as_deref()));
            ctx.tree.set(Loaded::Ready(tree));
        }
        None => ctx.tree.set(Loaded::Failed),
    }
}

#[component]
fn SignIn() -> Element {
    rsx! {
        main { class: "signin",
            div { class: "signin-card card",
                div { class: "brand",
                    span { class: "mark", aria_hidden: "true", "ac" }
                    h1 { "Dispatch Console" }
                }
                a { class: "btn primary", href: "/auth/login",
                    {icon("log-in")}
                    "Sign in with Authentik"
                }
            }
        }
    }
}

#[component]
fn Workspace(account: Account) -> Element {
    let ctx = use_context::<Ctx>();
    // Only a signed-in shell polls Active Work, so a signed-out visitor never sends the request.
    use_effect(move || {
        spawn(work::poll(ctx.work));
    });
    let no_access = has_no_tenant_access(&ctx.tenants.read());
    // The outlet stays in both cases, so the profile still works without a tenant.
    rsx! {
        div { class: "app",
            Sidebar { account: account.clone() }
            div { class: "workspace",
                if no_access {
                    NoTenantAccess {}
                } else {
                    ScopeBar {}
                }
                main { class: "page", Outlet::<Route> {} }
            }
        }
    }
}

/// True when the caller is signed in and the server lists no tenant for them.
pub fn has_no_tenant_access(tenants: &Loaded<TenantList>) -> bool {
    matches!(tenants, Loaded::Ready(list) if list.tenants.is_empty())
}

/// Signed in, but no tenant names one of the caller's groups. It replaces the
/// scope bar, so no tenant-scoped control is offered.
#[component]
fn NoTenantAccess() -> Element {
    rsx! {
        header { class: "top",
            p { class: "muted", role: "status",
                "No tenant access \u{2014} ask a platform admin to add one of your Authentik groups to a tenant."
            }
        }
    }
}

#[component]
fn Sidebar(account: Account) -> Element {
    let name = display_name(&account.claims);
    let initials = initials(&name);
    let admin = is_platform_admin(&account.claims);
    let ctx = use_context::<Ctx>();
    let attention = work::attention(&ctx.work.read());
    rsx! {
        aside { class: "side",
            div { class: "brand",
                span { class: "mark", aria_hidden: "true", "ac" }
                span { "Dispatch Console" }
            }
            TenantSwitch {}
            nav { class: "nav", aria_label: "Main",
                NavItem { to: Route::Dashboard {}, icon_name: "layout-dashboard", label: "Dashboard" }
                NavItem { to: Route::Home {}, icon_name: "list-todo", label: "Active Work", attention }
                div { class: "grp", "Infrastructure" }
                NavItem { to: Route::Topology {}, icon_name: "waypoints", label: "Topology" }
                NavItem { to: Route::Services {}, icon_name: "boxes", label: "Services" }
                NavItem { to: Route::Compute {}, icon_name: "server", label: "Compute" }
                NavItem { to: Route::Telemetry {}, icon_name: "activity", label: "Telemetry" }
                div { class: "grp", "Governance" }
                NavItem { to: Route::Access {}, icon_name: "users", label: "Access" }
                NavItem { to: Route::Secrets {}, icon_name: "key-round", label: "Secrets" }
                NavItem { to: Route::Policy {}, icon_name: "scale", label: "Policy" }
                div { class: "grp", "Automation" }
                NavItem { to: Route::AiGateway {}, icon_name: "door-open", label: "AI Gateway" }
                NavItem { to: Route::LlmAccess {}, icon_name: "message-square-lock", label: "LLM access" }
                NavItem { to: Route::CiCd {}, icon_name: "git-pull-request-arrow", label: "CI/CD" }
                div { class: "grp", "Agent roles" }
                p { class: "side-note", "Arrives in Phase 5" }
            }
            if admin {
                nav { class: "nav", aria_label: "Settings",
                    div { class: "grp", "Settings" }
                    NavItem { to: Route::TenantSettings {}, icon_name: "building", label: "Tenant settings" }
                    NavItem { to: Route::OrgSettings {}, icon_name: "settings-2", label: "Organization settings" }
                }
            }
            div { class: "side-foot",
                Link {
                    to: Route::Profile {},
                    class: "me-link",
                    active_class: "active",
                    title: "Your profile, signed in via Authentik",
                    span { class: "avatar", aria_hidden: "true", "{initials}" }
                    span { class: "me-text",
                        b { "{name}" }
                        small { "via Authentik" }
                    }
                }
                form { method: "post", action: "/auth/logout",
                    button { r#type: "submit", class: "side-link",
                        {icon("log-out")}
                        "Sign out"
                    }
                }
            }
        }
    }
}

#[component]
fn NavItem(
    to: Route,
    icon_name: &'static str,
    label: &'static str,
    /// Items that need a person. Shown as a red count only when above zero.
    #[props(default)]
    attention: usize,
) -> Element {
    rsx! {
        Link { to, class: "nav-link", active_class: "active",
            {icon(icon_name)}
            span { "{label}" }
            if attention > 0 {
                span {
                    class: "nav-badge",
                    role: "img",
                    aria_label: "{attention} need attention",
                    "{attention}"
                }
            }
        }
    }
}

/// The tenant switcher. It is the only place the tenant is shown or changed.
#[component]
fn TenantSwitch() -> Element {
    let ctx = use_context::<Ctx>();
    let mut open = use_signal(|| false);
    let tenants = ctx.tenants.read().clone();
    let current = ctx.tenant.read().clone();
    let list: Vec<NamedRef> = match &tenants {
        Loaded::Ready(list) => list.tenants.clone(),
        _ => Vec::new(),
    };
    let label = match &tenants {
        Loaded::Loading => "Loading\u{2026}".to_string(),
        Loaded::Failed => "Unavailable".to_string(),
        Loaded::Ready(_) => current
            .as_ref()
            .and_then(|id| list.iter().find(|t| &t.id == id))
            .map(|t| t.name.clone())
            .unwrap_or_else(|| "No tenant".to_string()),
    };
    let rows: Vec<(NamedRef, bool)> = list
        .iter()
        .map(|t| (t.clone(), current.as_deref() == Some(t.id.as_str())))
        .collect();
    let ready = !rows.is_empty();

    rsx! {
        div { class: "tenant",
            button {
                r#type: "button",
                class: "tenant-switch",
                aria_haspopup: "listbox",
                aria_expanded: "{open()}",
                disabled: !ready,
                onclick: move |_| open.set(!open()),
                span { class: "tw",
                    small { "Tenant" }
                    b { "{label}" }
                }
                {icon("chevrons-up-down")}
            }
            if open() {
                ul {
                    class: "tenant-menu",
                    role: "listbox",
                    aria_label: "Tenants",
                    onkeydown: move |evt: KeyboardEvent| {
                        if evt.key() == Key::Escape {
                            open.set(false);
                        }
                    },
                    for (tenant, selected) in rows.iter().cloned() {
                        li {
                            button {
                                r#type: "button",
                                role: "option",
                                class: "tenant-option",
                                aria_selected: "{selected}",
                                onclick: move |_| {
                                    open.set(false);
                                    if selected {
                                        return;
                                    }
                                    spawn(select_tenant(ctx, tenant.id.clone(), None));
                                },
                                "{tenant.name}"
                            }
                        }
                    }
                }
            }
        }
    }
}

/// The name shown in the account footer: the first non-empty of name, username
/// and email, then the subject.
pub fn display_name(claims: &Claims) -> String {
    [
        claims.name.as_deref(),
        claims.username.as_deref(),
        claims.email.as_deref(),
    ]
    .into_iter()
    .flatten()
    .find(|text| !text.trim().is_empty())
    .unwrap_or(claims.sub.as_str())
    .to_string()
}

/// Two capitals for the avatar: the first letters of the first and last word,
/// or the first two letters of a single word.
pub fn initials(name: &str) -> String {
    let words: Vec<&str> = name.split_whitespace().collect();
    let letters: String = match words.as_slice() {
        [] => return "?".to_string(),
        [only] => only.chars().take(2).collect(),
        [first, .., last] => first.chars().take(1).chain(last.chars().take(1)).collect(),
    };
    letters.to_uppercase()
}

/// Settings entries show only for members of `platform-admins`. The UI hides
/// them as a convenience; the API and OPA are the controls.
pub fn is_platform_admin(claims: &Claims) -> bool {
    claims.groups.iter().any(|group| group == "platform-admins")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn claims(
        sub: &str,
        name: Option<&str>,
        username: Option<&str>,
        email: Option<&str>,
        groups: &[&str],
    ) -> Claims {
        Claims {
            sub: sub.into(),
            name: name.map(str::to_string),
            username: username.map(str::to_string),
            email: email.map(str::to_string),
            groups: groups.iter().map(|g| g.to_string()).collect(),
        }
    }

    #[test]
    fn display_name_prefers_name_then_username_then_email_then_sub() {
        assert_eq!(
            display_name(&claims("s", Some("Ada"), Some("ada"), None, &[])),
            "Ada"
        );
        assert_eq!(
            display_name(&claims("s", None, Some("ada"), Some("a@x.test"), &[])),
            "ada"
        );
        assert_eq!(
            display_name(&claims("s", Some("  "), None, Some("a@x.test"), &[])),
            "a@x.test"
        );
        assert_eq!(
            display_name(&claims("subject-1", None, None, None, &[])),
            "subject-1"
        );
    }

    #[test]
    fn initials_take_first_and_last_word() {
        assert_eq!(initials("Ada Lovelace"), "AL");
        assert_eq!(initials("  Ada   Lovelace   Byron "), "AB");
        assert_eq!(initials("ada"), "AD");
        assert_eq!(initials("   "), "?");
    }

    #[test]
    fn no_tenant_access_means_signed_in_with_an_empty_list() {
        let empty = TenantList {
            example: false,
            tenants: vec![],
        };
        let one = TenantList {
            example: false,
            tenants: vec![NamedRef {
                id: "acme".into(),
                name: "Acme".into(),
            }],
        };
        assert!(has_no_tenant_access(&Loaded::Ready(empty)));
        assert!(!has_no_tenant_access(&Loaded::Ready(one)));
        // Not yet loaded, or failed: that is not the same as "no access".
        assert!(!has_no_tenant_access(&Loaded::Loading));
        assert!(!has_no_tenant_access(&Loaded::Failed));
    }

    #[test]
    fn only_platform_admins_see_settings() {
        assert!(is_platform_admin(&claims(
            "s",
            None,
            None,
            None,
            &["users", "platform-admins"]
        )));
        assert!(!is_platform_admin(&claims(
            "s",
            None,
            None,
            None,
            &["platform-admins-old"]
        )));
        assert!(!is_platform_admin(&claims("s", None, None, None, &[])));
    }
}
