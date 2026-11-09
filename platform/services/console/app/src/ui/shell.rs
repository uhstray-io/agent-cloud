//! The signed-in shell: sidebar, scope bar and the page outlet. Signed out, the
//! whole screen is the sign-in page.
//!
//! The first frame is the session check, which both the server and the browser
//! render as "Checking session", so hydration matches. Tenants, the scope tree
//! and the saved theme load after the page mounts.

use dioxus::prelude::*;

use super::api::{self, Account, Claims, Me, NamedRef, Reply, TenantList, Tree};
use super::icons::icon;
use super::inventory::{self, Snapshot};
use super::roles::load_roles;
use super::roles_api::RoleState;
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
    /// The inventory of `tenant`. The inventory poll and a tenant switch write it; the Phase 3 pages read it.
    pub inventory: Signal<Loaded<Snapshot>>,
    /// The agent roles of the registry (D40). Read once by platform admins; the sidebar and the role page read it.
    pub roles: Signal<RoleState>,
}

#[component]
pub fn Shell() -> Element {
    let me = use_signal(|| Me::Loading);
    let tenants = use_signal(|| Loaded::Loading);
    let tenant = use_signal(|| None::<String>);
    let tree = use_signal(|| Loaded::Loading);
    let scope = use_signal(Scope::default);
    let work = use_signal(|| WorkState::Loading);
    let inventory = use_signal(|| Loaded::Loading);
    let roles = use_signal(|| RoleState::Loading);
    let ctx = use_context_provider(|| Ctx {
        me,
        tenants,
        tenant,
        tree,
        scope,
        work,
        inventory,
        roles,
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
    // The old tenant's scope ids must not outlive the switch, so the scope is tenant-wide
    // before the new tree is read.
    ctx.scope.set(switch_scope());
    // The inventory of the old tenant must not show under the new name, so it waits for a fresh read.
    ctx.inventory.set(Loaded::Loading);
    let refreshed = tenant_id.clone();
    spawn(async move {
        inventory::refresh(ctx, refreshed).await;
    });
    let path = format!("/v1/tenants/{}/tree", api::segment(&tenant_id));
    let answer = classify_tree(&api::request("GET", &path, None).await);
    // A 401 ends the session, so it is not dropped as stale even when another tenant was picked since.
    if answer == TreeAnswer::SignedOut {
        inventory::end_session(ctx.me);
        return;
    }
    // A later pick of another tenant wins, so this answer is dropped.
    if ctx.tenant.peek().as_deref() != Some(tenant_id.as_str()) {
        return;
    }
    let parsed = match answer {
        TreeAnswer::Ready(tree) => Some(tree),
        TreeAnswer::SignedOut | TreeAnswer::Failed => None,
    };
    ctx.scope
        .set(scope_after_tree(parsed.as_ref(), saved.as_deref()));
    match parsed {
        Some(tree) => ctx.tree.set(Loaded::Ready(tree)),
        None => ctx.tree.set(Loaded::Failed),
    }
}

/// The scope a tenant switch starts from: tenant-wide (no org, site or env).
pub fn switch_scope() -> Scope {
    Scope::default()
}

/// The scope once the tenant's tree has answered: the saved default when it resolves in
/// this tree, else the first org. A tree that did not load leaves the tenant-wide scope the
/// switch set, never the previous tenant's ids.
pub fn scope_after_tree(tree: Option<&Tree>, saved: Option<&str>) -> Scope {
    match tree {
        Some(tree) => scope::initial(tree, saved),
        None => switch_scope(),
    }
}

/// What a tree read answered. A 401 is a session that ended, not a tree that failed to load.
#[derive(Debug, Clone, PartialEq)]
pub enum TreeAnswer {
    Ready(Tree),
    SignedOut,
    Failed,
}

/// Reads `GET /v1/tenants/{t}/tree`, as `api::classify_me` reads the session: 200 with a tree
/// is ready, 401 is signed out, and any other status or body is a failed read.
pub fn classify_tree(reply: &Reply) -> TreeAnswer {
    match reply.status {
        200 => api::parse::<Tree>(reply).map_or(TreeAnswer::Failed, TreeAnswer::Ready),
        401 => TreeAnswer::SignedOut,
        _ => TreeAnswer::Failed,
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
    let admin = is_platform_admin(&account.claims);
    // Only a signed-in shell polls Active Work, so a signed-out visitor never sends the request.
    // `use_hook` runs its closure once, on the first render of this instance. `use_effect`
    // would run it again whenever a signal read in its body changed, and each run would
    // start another poll loop. The poll's own waits are in the browser (`api::sleep_unless_hidden`).
    // The agent roles are read once, and only for a platform admin: the server refuses everyone else.
    use_hook(move || {
        spawn(work::poll(ctx.work));
        spawn(inventory::poll(ctx));
        if admin {
            spawn(load_roles(ctx.roles));
        }
    });
    let no_access = has_no_tenant_access(&ctx.tenants.read());
    // The outlet stays in both cases, so the profile still works without a tenant.
    rsx! {
        div { class: "app",
            // The first stop on every signed-in page. It moves focus past the sidebar to `main`.
            a {
                class: "skip-link",
                href: "#content",
                onclick: move |evt: MouseEvent| {
                    evt.prevent_default();
                    api::focus("content");
                },
                "Skip to content"
            }
            Sidebar { account: account.clone() }
            div { class: "workspace",
                if no_access {
                    NoTenantAccess {}
                } else {
                    ScopeBar {}
                }
                main { class: "page", id: "content", tabindex: "-1", Outlet::<Route> {} }
            }
        }
    }
}

/// True when the caller is signed in and the server lists no tenant for them.
pub fn has_no_tenant_access(tenants: &Loaded<TenantList>) -> bool {
    matches!(tenants, Loaded::Ready(list) if list.tenants.is_empty())
}

/// What a tenant page says when no tenant is selected and the list did not load. The shell
/// does not retry the list on its own: a page reload runs the read again, so the sentence says so.
pub const TENANT_LIST_FAILED: &str =
    "The tenant list could not be loaded, so no tenant is selected. Reload the page to try again.";

/// The sentence a tenant page shows with no tenant selected: `pick` while the list is loading or
/// loaded, and the failure when the list did not load, so the operator is not asked to pick from it.
pub fn no_tenant_message(tenants: &Loaded<TenantList>, pick: &str) -> String {
    match tenants {
        Loaded::Failed => TENANT_LIST_FAILED.to_string(),
        Loaded::Loading | Loaded::Ready(_) => pick.to_string(),
    }
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
                NavItem { to: Route::RequestService {}, icon_name: "user-round", label: "Request a service" }
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
                if admin {
                    div { class: "grp", "Agent roles" }
                    RoleLinks {}
                }
            }
            // Both settings pages show to every signed-in user. Each page reads only what the
            // caller may see (the tenant tree, the tenant's inventory), and says why a control is
            // disabled. Nothing in this group is admin-only.
            nav { class: "nav", aria_label: "Settings",
                div { class: "grp", "Settings" }
                NavItem { to: Route::TenantSettings {}, icon_name: "building", label: "Tenant settings" }
                NavItem { to: Route::OrgSettings {}, icon_name: "settings-2", label: "Organization settings" }
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

/// One link per agent role, from the roles the shell read. Only shown to platform admins.
#[component]
fn RoleLinks() -> Element {
    let ctx = use_context::<Ctx>();
    let roles = ctx.roles.read().clone();
    match roles {
        RoleState::Loading => rsx! { p { class: "side-note", "Loading roles\u{2026}" } },
        RoleState::Failed => rsx! { p { class: "side-note", "Roles unavailable" } },
        RoleState::Forbidden => {
            rsx! { p { class: "side-note", "Agent roles are limited to platform administrators." } }
        }
        RoleState::Ready(list) if list.roles.is_empty() => {
            rsx! { p { class: "side-note", "No roles in the registry" } }
        }
        RoleState::Ready(list) => rsx! {
            for role in list.roles.iter() {
                Link {
                    key: "{role.name}",
                    to: Route::RoleDetail { role: role.name.clone() },
                    class: "nav-link",
                    active_class: "active",
                    {icon("bot")}
                    span { "{role.name}" }
                }
            }
        },
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

/// The DOM id of the tenant switch button. Focus returns to it when the menu closes.
const TENANT_SWITCH_ID: &str = "tenant-switch";

/// A move through the tenant menu's options.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum MenuStep {
    Next,
    Previous,
    First,
    Last,
}

/// The move a key asks for in the tenant menu: the arrows move one option, Home and End jump
/// to the ends. Any other key is not a menu move.
pub fn menu_step(key: &Key) -> Option<MenuStep> {
    match key {
        Key::ArrowDown => Some(MenuStep::Next),
        Key::ArrowUp => Some(MenuStep::Previous),
        Key::Home => Some(MenuStep::First),
        Key::End => Some(MenuStep::Last),
        _ => None,
    }
}

/// The option that takes focus after `step` from option `current` of `count`. Next and Previous
/// wrap past the ends. `None` when the menu has no options.
pub fn option_after(current: usize, count: usize, step: MenuStep) -> Option<usize> {
    if count == 0 {
        return None;
    }
    Some(match step {
        MenuStep::Next => (current + 1) % count,
        MenuStep::Previous => (current + count - 1) % count,
        MenuStep::First => 0,
        MenuStep::Last => count - 1,
    })
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
    // The option that takes focus when the menu opens: the selected tenant, else the first.
    let focus_index = rows.iter().position(|(_, selected)| *selected).unwrap_or(0);
    let count = rows.len();

    rsx! {
        div { class: "tenant",
            button {
                id: TENANT_SWITCH_ID,
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
                            api::focus(TENANT_SWITCH_ID);
                        }
                    },
                    for (index, (tenant, selected)) in rows.iter().cloned().enumerate() {
                        li {
                            button {
                                id: "tenant-option-{index}",
                                r#type: "button",
                                role: "option",
                                class: "tenant-option",
                                aria_selected: "{selected}",
                                onmounted: move |evt: MountedEvent| {
                                    if index == focus_index {
                                        let node = evt.data();
                                        spawn(async move {
                                            let _ = node.set_focus(true).await;
                                        });
                                    }
                                },
                                onkeydown: move |evt: KeyboardEvent| {
                                    if let Some(step) = menu_step(&evt.key()) {
                                        evt.prevent_default();
                                        if let Some(next) = option_after(index, count, step) {
                                            api::focus(&format!("tenant-option-{next}"));
                                        }
                                    }
                                },
                                onclick: move |_| {
                                    open.set(false);
                                    api::focus(TENANT_SWITCH_ID);
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

/// The Agent roles nav and the role pages show only for members of `platform-admins` (D40). The UI
/// hides them as a convenience; the server's admin gate on `/v1/roles` and `/v1/work` is the control.
pub fn is_platform_admin(claims: &Claims) -> bool {
    claims.groups.iter().any(|group| group == "platform-admins")
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{json, Value};

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
    fn only_an_exact_platform_admins_member_sees_the_agent_roles() {
        assert!(is_platform_admin(&claims(
            "a",
            None,
            None,
            None,
            &["ops", "platform-admins"]
        )));
        assert!(!is_platform_admin(&claims(
            "b",
            None,
            None,
            None,
            &["platform-admins-old"]
        )));
        assert!(!is_platform_admin(&claims("c", None, None, None, &["ops"])));
        assert!(!is_platform_admin(&claims("d", None, None, None, &[])));
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

    fn two_org_tree() -> Tree {
        use crate::ui::api::{Org, Site};
        Tree {
            example: false,
            orgs: vec![
                Org {
                    id: "ops".into(),
                    name: "Ops".into(),
                    sites: vec![Site {
                        id: "dc1".into(),
                        name: "DC 1".into(),
                        envs: vec!["prod".into()],
                    }],
                },
                Org {
                    id: "labs".into(),
                    name: "Labs".into(),
                    sites: vec![Site {
                        id: "dc2".into(),
                        name: "DC 2".into(),
                        envs: vec!["dev".into()],
                    }],
                },
            ],
        }
    }

    #[test]
    fn a_switch_starts_tenant_wide() {
        assert_eq!(switch_scope(), Scope::default());
        assert!(switch_scope().org.is_empty());
    }

    #[test]
    fn a_failed_tree_read_leaves_the_scope_tenant_wide_not_the_previous_tenants() {
        let previous = Scope {
            org: "ops".into(),
            site: Some("dc1".into()),
            env: Some("prod".into()),
        };
        let after = scope_after_tree(None, Some("ops/dc1/prod"));
        assert_eq!(after, Scope::default(), "a failed tree read is tenant-wide");
        assert_ne!(after, previous, "the previous tenant's ids do not survive");
    }

    #[test]
    fn a_tree_read_applies_the_saved_default_else_the_first_org() {
        let tree = two_org_tree();
        assert_eq!(scope_after_tree(Some(&tree), Some("labs")).org, "labs");
        assert_eq!(
            scope_after_tree(Some(&tree), None).org,
            "ops",
            "the first org"
        );
        assert_eq!(
            scope_after_tree(Some(&tree), Some("gone/away")).org,
            "ops",
            "an unresolved saved default falls back to the first org"
        );
    }

    fn reply(status: u16, body: Value) -> Reply {
        Reply { status, body }
    }

    #[test]
    fn a_401_on_the_tree_ends_the_session_and_any_other_failure_is_a_failed_tree() {
        assert_eq!(
            classify_tree(&reply(401, Value::Null)),
            TreeAnswer::SignedOut
        );
        assert!(matches!(
            classify_tree(&reply(200, json!({"orgs": []}))),
            TreeAnswer::Ready(_)
        ));
        assert_eq!(classify_tree(&reply(500, Value::Null)), TreeAnswer::Failed);
        assert_eq!(classify_tree(&reply(0, Value::Null)), TreeAnswer::Failed);
        assert_eq!(
            classify_tree(&reply(200, json!({"example": true}))),
            TreeAnswer::Failed,
            "a 200 whose body is not a tree is a failed read"
        );
        assert_eq!(
            classify_tree(&reply(403, json!({"orgs": []}))),
            TreeAnswer::Failed,
            "only a 200 carries a tree, whatever the body"
        );
    }

    #[test]
    fn a_failed_tenant_list_says_so_on_a_tenant_page_and_loading_keeps_the_pick_sentence() {
        let pick = "Pick a tenant in the sidebar to see its settings.";
        assert_eq!(no_tenant_message(&Loaded::Failed, pick), TENANT_LIST_FAILED);
        assert!(TENANT_LIST_FAILED.contains("could not be loaded"));
        assert_eq!(no_tenant_message(&Loaded::Loading, pick), pick);
        let one = TenantList {
            example: false,
            tenants: vec![NamedRef {
                id: "acme".into(),
                name: "Acme".into(),
            }],
        };
        assert_eq!(no_tenant_message(&Loaded::Ready(one), pick), pick);
    }

    #[test]
    fn the_failure_sentence_names_no_literal_retry_time_and_no_pick() {
        assert!(
            !TENANT_LIST_FAILED.chars().any(|c| c.is_ascii_digit()),
            "a retry time comes from the poll interval, never a literal"
        );
        assert!(!TENANT_LIST_FAILED.contains("Pick"));
    }

    #[test]
    fn the_arrows_home_and_end_are_menu_moves_and_no_other_key_is_one() {
        assert_eq!(menu_step(&Key::ArrowDown), Some(MenuStep::Next));
        assert_eq!(menu_step(&Key::ArrowUp), Some(MenuStep::Previous));
        assert_eq!(menu_step(&Key::Home), Some(MenuStep::First));
        assert_eq!(menu_step(&Key::End), Some(MenuStep::Last));
        assert_eq!(
            menu_step(&Key::Escape),
            None,
            "Escape is handled by the menu"
        );
        assert_eq!(menu_step(&Key::Enter), None);
    }

    #[test]
    fn next_and_previous_wrap_and_home_and_end_jump() {
        assert_eq!(option_after(0, 3, MenuStep::Next), Some(1));
        assert_eq!(
            option_after(2, 3, MenuStep::Next),
            Some(0),
            "Next wraps from the last"
        );
        assert_eq!(
            option_after(0, 3, MenuStep::Previous),
            Some(2),
            "Previous wraps from the first"
        );
        assert_eq!(option_after(2, 3, MenuStep::Previous), Some(1));
        assert_eq!(option_after(1, 3, MenuStep::First), Some(0));
        assert_eq!(option_after(0, 3, MenuStep::Last), Some(2));
        assert_eq!(
            option_after(0, 1, MenuStep::Next),
            Some(0),
            "one option stays put"
        );
        assert_eq!(
            option_after(0, 0, MenuStep::Next),
            None,
            "no options, no move"
        );
    }
}
