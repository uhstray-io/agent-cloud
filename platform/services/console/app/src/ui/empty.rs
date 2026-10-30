//! Placeholder pages. Each nav page the shell links to renders the same empty
//! state until its phase lands: what the page is for, the authority behind it,
//! and the phase it arrives in.

use dioxus::prelude::*;

use super::icons::icon;
use super::Route;

/// What a placeholder page says about itself. `id` is the nav id from the
/// reference prototype, and `phase_for` keys on it.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct PageMeta {
    pub id: &'static str,
    pub title: &'static str,
    pub icon: &'static str,
    pub purpose: &'static str,
    pub authority: &'static str,
}

/// The build phase a nav page arrives in, from plan/development/17-dispatch-console.md.
/// `None` means no phase is recorded for the page, and the page says "Planned".
pub fn phase_for(id: &str) -> Option<u8> {
    match id {
        "home" => Some(2),
        "topology" | "services" | "compute" => Some(3),
        "dashboard" => Some(4),
        "access" | "secrets" | "policy" | "agents" | "llm" | "tenantsettings" | "orgsettings" => {
            Some(5)
        }
        "telemetry" | "cicd" => Some(6),
        _ => None,
    }
}

pub const ACTIVE_WORK: PageMeta = PageMeta {
    id: "home",
    title: "Active Work",
    icon: "list-todo",
    purpose: "Every request as a strip from approval to done, scoped to what you picked.",
    authority: "skynet, Semaphore",
};

pub const DASHBOARD: PageMeta = PageMeta {
    id: "dashboard",
    title: "Dashboard",
    icon: "layout-dashboard",
    purpose: "Proposals waiting for your review: comment, edit the plan, approve or deny.",
    authority: "skynet, OPA",
};

pub const TOPOLOGY: PageMeta = PageMeta {
    id: "topology",
    title: "Topology",
    icon: "waypoints",
    purpose: "Compute, network and data flows for the scope, drawn from the inventory.",
    authority: "NetBox, Proxmox",
};

pub const SERVICES: PageMeta = PageMeta {
    id: "services",
    title: "Services",
    icon: "boxes",
    purpose: "Services in scope with their placement, integrations and health.",
    authority: "NetBox, Proxmox",
};

pub const COMPUTE: PageMeta = PageMeta {
    id: "compute",
    title: "Compute",
    icon: "server",
    purpose: "Capacity by site, cluster, node and VM, with a resource panel for each.",
    authority: "NetBox, Proxmox",
};

pub const TELEMETRY: PageMeta = PageMeta {
    id: "telemetry",
    title: "Telemetry",
    icon: "activity",
    purpose: "Rollups by tenant, org, site and cluster. Per-asset charts stay in Grafana.",
    authority: "Prometheus, Loki, Grafana",
};

pub const ACCESS: PageMeta = PageMeta {
    id: "access",
    title: "Access",
    icon: "users",
    purpose: "People, groups and scoped roles, with additions proposed as blueprints.",
    authority: "Authentik",
};

pub const SECRETS: PageMeta = PageMeta {
    id: "secrets",
    title: "Secrets",
    icon: "key-round",
    purpose: "Secret paths, field counts and rotation age. Values never appear here.",
    authority: "OpenBao",
};

pub const POLICY: PageMeta = PageMeta {
    id: "policy",
    title: "Policy",
    icon: "scale",
    purpose: "Policy packages and decisions. A change is proposed as a plan, then a pull request.",
    authority: "OPA (Rego in Git)",
};

pub const AI_GATEWAY: PageMeta = PageMeta {
    id: "agents",
    title: "AI Gateway",
    icon: "door-open",
    purpose: "Gateway groups for production and development, with their clients and budgets.",
    authority: "agentgateway",
};

pub const LLM_ACCESS: PageMeta = PageMeta {
    id: "llm",
    title: "LLM access",
    icon: "message-square-lock",
    purpose: "Request a model key for a scoped set of agents. The key is handed off, not shown.",
    authority: "agentgateway",
};

pub const CICD: PageMeta = PageMeta {
    id: "cicd",
    title: "CI/CD",
    icon: "git-pull-request-arrow",
    purpose: "Delivery context for each change: reviews, required checks and the promotion path.",
    authority: "GitHub",
};

pub const TENANT_SETTINGS: PageMeta = PageMeta {
    id: "tenantsettings",
    title: "Tenant settings",
    icon: "building",
    purpose: "Tenant defaults. A change is proposed, never written directly.",
    authority: "site-config inventory, Authentik, NetBox tags",
};

pub const ORG_SETTINGS: PageMeta = PageMeta {
    id: "orgsettings",
    title: "Organization settings",
    icon: "settings-2",
    purpose: "Organizations, sites, approvers and business-domain tags. A change is proposed.",
    authority: "site-config inventory, Authentik, NetBox tags",
};

/// The empty state every placeholder page renders.
#[component]
pub fn EmptyPage(page: PageMeta) -> Element {
    let arrival = match phase_for(page.id) {
        Some(phase) => format!("Arrives in Phase {phase}"),
        None => "Planned".to_string(),
    };
    rsx! {
        div { class: "view",
            header { class: "view-h",
                h1 { {icon(page.icon)} "{page.title}" }
                span { class: "authority", "Source of truth: {page.authority}" }
                p { "{page.purpose}" }
            }
            section { class: "card",
                div { class: "empty",
                    span { class: "empty-icon", aria_hidden: "true", {icon(page.icon)} }
                    h2 { "Not built yet" }
                    span { class: "tag accent", "{arrival}" }
                }
            }
        }
    }
}

#[component]
pub fn Dashboard() -> Element {
    rsx! { EmptyPage { page: DASHBOARD } }
}

#[component]
pub fn Telemetry() -> Element {
    rsx! { EmptyPage { page: TELEMETRY } }
}

#[component]
pub fn Access() -> Element {
    rsx! { EmptyPage { page: ACCESS } }
}

#[component]
pub fn Secrets() -> Element {
    rsx! { EmptyPage { page: SECRETS } }
}

#[component]
pub fn Policy() -> Element {
    rsx! { EmptyPage { page: POLICY } }
}

#[component]
pub fn AiGateway() -> Element {
    rsx! { EmptyPage { page: AI_GATEWAY } }
}

#[component]
pub fn LlmAccess() -> Element {
    rsx! { EmptyPage { page: LLM_ACCESS } }
}

#[component]
pub fn CiCd() -> Element {
    rsx! { EmptyPage { page: CICD } }
}

#[component]
pub fn TenantSettings() -> Element {
    rsx! { EmptyPage { page: TENANT_SETTINGS } }
}

#[component]
pub fn OrgSettings() -> Element {
    rsx! { EmptyPage { page: ORG_SETTINGS } }
}

/// Any path the router does not know. It renders outside the shell, so it
/// shows no account or tenant data.
#[component]
pub fn NotFound(segments: Vec<String>) -> Element {
    let path = segments.join("/");
    rsx! {
        div { class: "view",
            header { class: "view-h",
                h1 { "Page not found" }
                p { "Nothing is served at /{path}." }
            }
            p {
                Link { to: Route::Home {}, "Back to Active Work" }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_nav_page_has_a_phase_from_plan_17() {
        assert_eq!(phase_for("home"), Some(2));
        assert_eq!(phase_for("topology"), Some(3));
        assert_eq!(phase_for("services"), Some(3));
        assert_eq!(phase_for("compute"), Some(3));
        assert_eq!(phase_for("dashboard"), Some(4));
        assert_eq!(phase_for("access"), Some(5));
        assert_eq!(phase_for("agents"), Some(5));
        assert_eq!(phase_for("llm"), Some(5));
        assert_eq!(phase_for("tenantsettings"), Some(5));
        assert_eq!(phase_for("cicd"), Some(6));
        assert_eq!(phase_for("telemetry"), Some(6));
    }

    #[test]
    fn unmapped_pages_are_planned_not_guessed() {
        assert_eq!(phase_for("profile"), None);
        assert_eq!(phase_for("network"), None);
    }

    #[test]
    fn every_placeholder_page_is_mapped() {
        let pages = [
            ACTIVE_WORK,
            DASHBOARD,
            TOPOLOGY,
            SERVICES,
            COMPUTE,
            TELEMETRY,
            ACCESS,
            SECRETS,
            POLICY,
            AI_GATEWAY,
            LLM_ACCESS,
            CICD,
            TENANT_SETTINGS,
            ORG_SETTINGS,
        ];
        for page in pages {
            assert!(phase_for(page.id).is_some(), "{} has no phase", page.id);
            assert!(!page.authority.is_empty(), "{} names no authority", page.id);
        }
    }
}
