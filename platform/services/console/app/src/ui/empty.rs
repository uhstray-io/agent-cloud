//! The metadata each page's header reads (title, icon, purpose and authority), and the page
//! an unknown path shows.

use dioxus::prelude::*;

use super::Route;

/// What a page says about itself. `id` is the nav id from the reference prototype.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct PageMeta {
    pub id: &'static str,
    pub title: &'static str,
    pub icon: &'static str,
    pub purpose: &'static str,
    pub authority: &'static str,
}

pub const ACTIVE_WORK: PageMeta = PageMeta {
    id: "home",
    title: "Active Work",
    icon: "list-todo",
    purpose: "Every request as a strip from approval to done, scoped to what you picked.",
    authority: "skynet, Semaphore",
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
    purpose: "Target health and Grafana alerts by environment, cluster and service. Per-asset charts stay in Grafana.",
    authority: "Prometheus, Grafana",
};

pub const CICD: PageMeta = PageMeta {
    id: "cicd",
    title: "CI/CD",
    icon: "git-pull-request-arrow",
    purpose: "Delivery context for each change: reviews, required checks and the promotion path.",
    authority: "GitHub",
};

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
