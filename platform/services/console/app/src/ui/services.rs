//! Services: the table of the tenant's services, the business-domain tiles, and the
//! detail page for one service.
//!
//! The rows are the tenant's VMs that carry workflow fields. The RAG is the
//! server's. The domain tiles count the rows by NetBox tag, which the inventory
//! answer carries as the tag name; nothing is computed from console-local data.

use dioxus::prelude::*;

use super::empty::SERVICES;
use super::icons::icon;
use super::inventory::{
    count_text, loaded_tree, mb_text, meta_of, poll_admin, scoped_services, view, Admin, Snapshot,
    View, ViewNotice, SEP,
};
use super::inventory_api::{self, ServiceBody, ServiceRow};
use super::shell::Ctx;
use super::status::{
    step_rows, unconfigured_hint, unconfigured_notice, Freshness, Notice, Rag, RagBadge, StepList,
    DASH,
};
use super::Route;

/// The five business domains an organization's services are tagged with in NetBox.
pub const DOMAIN_TAGS: [&str; 5] = [
    "Products",
    "Professional Services",
    "Automation",
    "Administration",
    "IT",
];

/// A tag in slug form: lower case, runs of whitespace as one hyphen. NetBox
/// slugs and tag names compare equal under this form.
pub fn slug(text: &str) -> String {
    text.split_whitespace()
        .map(str::to_lowercase)
        .collect::<Vec<_>>()
        .join("-")
}

/// True when a NetBox tag names this domain, matched case-insensitively by name or slug.
pub fn tag_matches(tag: &str, domain: &str) -> bool {
    slug(tag) == slug(domain)
}

/// Services per domain, in `DOMAIN_TAGS` order, and the count of services with no
/// domain tag at all. A service with two domain tags counts in both.
pub fn domain_counts(rows: &[ServiceRow]) -> (Vec<(&'static str, usize)>, usize) {
    let counts = DOMAIN_TAGS
        .iter()
        .map(|domain| {
            let n = rows
                .iter()
                .filter(|row| row.tags.iter().any(|tag| tag_matches(tag, domain)))
                .count();
            (*domain, n)
        })
        .collect();
    let untagged = rows
        .iter()
        .filter(|row| {
            !row.tags
                .iter()
                .any(|tag| DOMAIN_TAGS.iter().any(|domain| tag_matches(tag, domain)))
        })
        .count();
    (counts, untagged)
}

/// The table's two filters. An empty `text` matches every row, and `rag` of
/// `None` matches every status.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct ServiceFilter {
    pub text: String,
    pub rag: Option<Rag>,
}

/// The rows that pass the filter, in the server's order. The text matches the
/// name, site or cluster, ignoring case and surrounding spaces.
pub fn filter_services(rows: &[ServiceRow], filter: &ServiceFilter) -> Vec<ServiceRow> {
    let needle = filter.text.trim().to_lowercase();
    rows.iter()
        .filter(|row| filter.rag.is_none_or(|rag| Rag::from_wire(&row.rag) == rag))
        .filter(|row| {
            needle.is_empty()
                || row.name.to_lowercase().contains(&needle)
                || row.site.to_lowercase().contains(&needle)
                || row.cluster.to_lowercase().contains(&needle)
        })
        .cloned()
        .collect()
}

/// The failed-steps cell: the names, or a dash when none failed.
pub fn failed_text(row: &ServiceRow) -> String {
    if row.failed_steps.is_empty() {
        DASH.to_string()
    } else {
        row.failed_steps.join(", ")
    }
}

/// The Services page.
#[component]
pub fn Services() -> Element {
    let ctx = use_context::<Ctx>();
    let state = ctx.inventory.read().clone();
    let tenant = ctx.tenant.read().clone();
    let shown = match view(&state, tenant.as_deref()) {
        View::Ready(snap) => rsx! { ServicesContent { snap: *snap } },
        other => rsx! { ViewNotice { view: other } },
    };
    rsx! {
        div { class: "view services",
            header { class: "view-h",
                h1 { {icon(SERVICES.icon)} "{SERVICES.title}" }
                span { class: "authority", "Source of truth: {SERVICES.authority}" }
                p { "{SERVICES.purpose}" }
            }
            {shown}
        }
    }
}

#[component]
fn ServicesContent(snap: Snapshot) -> Element {
    let ctx = use_context::<Ctx>();
    let reason = snap.reason.clone().unwrap_or_default();
    let Some(body) = snap.services.clone() else {
        return rsx! {
            Freshness { meta: meta_of(&snap).cloned().unwrap_or_default(), reason }
            Notice { message: "The services could not be loaded.".to_string() }
        };
    };
    let scope = ctx.scope.read().clone();
    let tree = loaded_tree(&ctx.tree.read());
    let rows = scoped_services(&body.services, &scope, tree.as_ref());
    let meta = body.meta.clone();
    let unconfigured = unconfigured_notice(&meta);
    rsx! {
        Freshness { meta, reason }
        if let Some(message) = unconfigured {
            Notice { message }
        }
        ServicesPanel { rows }
    }
}

/// The domain tiles, the filters and the table. The filters are held here, so
/// they survive a poll that replaces the rows.
#[component]
fn ServicesPanel(rows: Vec<ServiceRow>) -> Element {
    let filter = use_signal(ServiceFilter::default);
    let mut filter_w = filter;
    let current = filter.read().clone();
    let (counts, untagged) = domain_counts(&rows);
    let shown = filter_services(&rows, &current);
    let rag_value = current.rag.map_or("", Rag::wire).to_string();
    let none_match = shown.is_empty() && !rows.is_empty();
    let total = count_text(rows.len(), "service", "services");
    rsx! {
        section { class: "card",
            div { class: "card-h",
                h2 { "Business domains" }
                span { class: "faint", "{total}" }
            }
            div { class: "card-b stack",
                div { class: "tiles",
                    for (domain, n) in counts.iter() {
                        div { class: "tile", key: "{domain}",
                            b { "{n}" }
                            small { "{domain}" }
                        }
                    }
                    div { class: "tile",
                        b { "{untagged}" }
                        small { "Untagged" }
                    }
                }
                p { class: "hint", "A service with two domain tags counts in both." }
            }
        }
        div { class: "filters", role: "search", aria_label: "Filter services",
            label { class: "field-inline grow",
                span { class: "label", "Name, site or cluster" }
                input {
                    class: "input",
                    r#type: "search",
                    value: "{current.text}",
                    oninput: move |evt: FormEvent| filter_w.write().text = evt.value(),
                }
            }
            label { class: "field-inline",
                span { class: "label", "Status" }
                select {
                    class: "scope-select",
                    value: "{rag_value}",
                    onchange: move |evt: FormEvent| {
                        let value = evt.value();
                        filter_w.write().rag = [Rag::Green, Rag::Amber, Rag::Red, Rag::Grey]
                            .into_iter()
                            .find(|rag| rag.wire() == value);
                    },
                    option { value: "", "All statuses" }
                    for rag in [Rag::Green, Rag::Amber, Rag::Red, Rag::Grey] {
                        option { value: "{rag.wire()}", {rag.label()} }
                    }
                }
            }
        }
        if none_match {
            p { class: "faint", role: "status", "No service matches these filters." }
        }
        section { class: "card",
            div { class: "tbl-wrap",
                table { class: "tbl",
                    thead {
                        tr {
                            th { "Service" }
                            th { "Site" }
                            th { "Cluster" }
                            th { "Live" }
                            th { "Status" }
                            th { "Failed steps" }
                            th { "Last run" }
                            th { "Tags" }
                            th { "Grafana" }
                        }
                    }
                    tbody {
                        for (index, row) in shown.iter().enumerate() {
                            ServiceTableRow {
                                key: "{row.site}{SEP}{row.cluster}{SEP}{row.name}{SEP}{index}",
                                row: row.clone(),
                            }
                        }
                    }
                }
            }
            if rows.is_empty() {
                p { class: "empty", "No service is recorded for this tenant." }
            }
        }
    }
}

/// One row of the services table.
#[component]
fn ServiceTableRow(row: ServiceRow) -> Element {
    let live = row
        .live_status
        .clone()
        .unwrap_or_else(|| "No live state".to_string());
    let failed = failed_text(&row);
    let last_run = row.last_run.clone().unwrap_or_else(|| DASH.to_string());
    let tags = row.tags.join(", ");
    let rag = Rag::from_wire(&row.rag);
    rsx! {
        tr { key: "{row.name}",
            td {
                Link { to: Route::ServiceDetail { name: row.name.clone() }, "{row.name}" }
            }
            td { "{row.site}" }
            td { "{row.cluster}" }
            td { "{live}" }
            td { RagBadge { rag } }
            td { "{failed}" }
            td { class: "mono", "{last_run}" }
            td { "{tags}" }
            td {
                if let Some(url) = row.grafana_url.clone() {
                    a { href: "{url}", target: "_blank", rel: "noopener", "Grafana" }
                } else {
                    span { class: "faint", {DASH} }
                }
            }
        }
    }
}

/// The detail page for one service. It is keyed on the tenant and the name, so a
/// tenant switch or another service starts a fresh read.
#[component]
pub fn ServiceDetail(name: String) -> Element {
    let ctx = use_context::<Ctx>();
    let tenant = ctx.tenant.read().clone();
    let body = match tenant {
        Some(tenant) => rsx! {
            ServiceView { key: "{tenant}/{name}", tenant, name: name.clone() }
        },
        None => rsx! {
            Notice { message: "Pick a tenant in the sidebar to see this service.".to_string() }
        },
    };
    rsx! {
        div { class: "view service-detail",
            header { class: "view-h",
                h1 { "{name}" }
                span { class: "authority", "Source of truth: {SERVICES.authority}" }
                p {
                    Link { to: Route::Services {}, "Back to Services" }
                }
            }
            {body}
        }
    }
}

/// The service's own read, polled while the tab is visible.
#[component]
fn ServiceView(tenant: String, name: String) -> Element {
    let ctx = use_context::<Ctx>();
    let state = use_signal(|| Admin::<ServiceBody>::Loading);
    let path = inventory_api::service_path(&tenant, &name);
    use_hook(move || {
        spawn(poll_admin(path, state, ctx.me));
    });
    let shown = state.read().clone();
    match shown {
        Admin::Loading => rsx! { Notice { message: "Loading the service\u{2026}".to_string() } },
        Admin::Forbidden => rsx! {
            Notice { message: "This service is not available to you.".to_string() }
        },
        Admin::Missing => rsx! {
            Notice { message: "Service not found in this tenant.".to_string() }
        },
        Admin::SignedOut => rsx! {
            Notice { message: "Your session ended. Sign in again to see this service.".to_string() }
        },
        Admin::Unavailable(reason) => rsx! {
            Notice { message: format!("The service could not be loaded: {reason}. It retries every 30 seconds while this tab is visible.") }
        },
        Admin::Shown { body, reason } => {
            let reason = reason.unwrap_or_default();
            rsx! { ServiceCard { body, reason } }
        }
    }
}

/// One service's placement, workflow and links.
#[component]
fn ServiceCard(body: ServiceBody, reason: String) -> Element {
    let vm = body.vm.clone();
    let row = body.service.clone();
    let rag = Rag::from_wire(&row.rag);
    let live = vm
        .live_status
        .clone()
        .unwrap_or_else(|| "No live state".to_string());
    let vmid = vm.vmid.map_or(DASH.to_string(), |id| id.to_string());
    let memory = vm.memory_mb.map_or(DASH.to_string(), mb_text);
    let disk = vm.disk_gb.map_or(DASH.to_string(), |gb| format!("{gb} GB"));
    let vcpus = vm.vcpus.map_or(DASH.to_string(), |n| format!("{n:.0}"));
    let ip = vm.primary_ip.clone().unwrap_or_else(|| DASH.to_string());
    let tags = if vm.tags.is_empty() {
        DASH.to_string()
    } else {
        vm.tags.join(", ")
    };
    let steps = vm.workflow.as_ref().map(step_rows).unwrap_or_default();
    let has_workflow = vm.workflow.is_some();
    let failed = vm
        .workflow
        .as_ref()
        .map(|wf| wf.failed.join(", "))
        .unwrap_or_default();
    let last_run = row.last_run.clone().unwrap_or_else(|| DASH.to_string());
    let grafana = vm.grafana_url.clone().or(row.grafana_url.clone());
    let conformance = body.meta.links.grafana_conformance.clone();
    let links_off = grafana.is_none() && conformance.is_none();
    let hint = unconfigured_hint("grafana").to_string();
    rsx! {
        Freshness { meta: body.meta.clone(), reason }
        div { class: "row wrap",
            RagBadge { rag }
            span { class: "faint", "{row.site} \u{00b7} {row.cluster}" }
        }
        section { class: "card",
            div { class: "card-h",
                h2 { "Placement" }
            }
            div { class: "card-b",
                dl { class: "fields",
                    div { class: "field-row", dt { class: "label", "Live status" } dd { "{live}" } }
                    div { class: "field-row", dt { class: "label", "NetBox status" } dd { "{vm.status}" } }
                    div { class: "field-row", dt { class: "label", "VM ID" } dd { "{vmid}" } }
                    div { class: "field-row", dt { class: "label", "vCPUs" } dd { "{vcpus}" } }
                    div { class: "field-row", dt { class: "label", "Memory" } dd { "{memory}" } }
                    div { class: "field-row", dt { class: "label", "Disk" } dd { "{disk}" } }
                    div { class: "field-row", dt { class: "label", "Primary IP" } dd { class: "mono", "{ip}" } }
                    div { class: "field-row", dt { class: "label", "Tags" } dd { "{tags}" } }
                }
            }
        }
        section { class: "card",
            div { class: "card-h",
                h2 { "Workflow" }
            }
            div { class: "card-b stack",
                if has_workflow {
                    StepList { steps }
                    if !failed.is_empty() {
                        p { class: "hint", "Failed: {failed}" }
                    }
                    p { class: "hint", "Last run {last_run}" }
                } else {
                    Notice { message: "This VM carries no workflow fields.".to_string() }
                }
            }
        }
        section { class: "card",
            div { class: "card-h",
                h2 { "Links" }
            }
            div { class: "card-b row wrap",
                if let Some(url) = grafana.clone() {
                    a { class: "btn", href: "{url}", target: "_blank", rel: "noopener", "Service overview in Grafana" }
                }
                if let Some(url) = conformance.clone() {
                    a { class: "btn", href: "{url}", target: "_blank", rel: "noopener", "Deployment conformance in Grafana" }
                }
                if links_off {
                    p { class: "hint", "{hint}" }
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn row(name: &str, rag: &str, tags: &[&str], failed: &[&str]) -> ServiceRow {
        ServiceRow {
            name: name.into(),
            site: "dc1".into(),
            cluster: "pve-prod".into(),
            rag: rag.into(),
            tags: tags.iter().map(|t| t.to_string()).collect(),
            failed_steps: failed.iter().map(|s| s.to_string()).collect(),
            ..ServiceRow::default()
        }
    }

    #[test]
    fn a_tag_matches_its_domain_by_name_or_slug_in_any_case() {
        assert!(tag_matches("Products", "Products"));
        assert!(tag_matches("products", "Products"));
        assert!(tag_matches(
            "professional-services",
            "Professional Services"
        ));
        assert!(tag_matches(
            "Professional Services",
            "Professional Services"
        ));
        assert!(tag_matches("it", "IT"));
        assert!(!tag_matches("Product", "Products"), "no prefix matches");
        assert!(!tag_matches("tududi-sync", "Automation"));
    }

    #[test]
    fn domain_tiles_count_each_service_once_per_domain_and_the_rest_as_untagged() {
        let rows = vec![
            row("a", "green", &["Products"], &[]),
            row("b", "red", &["Products", "IT"], &[]),
            row("c", "amber", &["tududi-sync"], &[]),
            row("d", "grey", &[], &[]),
        ];
        let (counts, untagged) = domain_counts(&rows);
        assert_eq!(counts.len(), 5);
        let get = |name: &str| counts.iter().find(|(d, _)| *d == name).unwrap().1;
        assert_eq!(get("Products"), 2);
        assert_eq!(get("IT"), 1);
        assert_eq!(get("Automation"), 0);
        assert_eq!(untagged, 2, "c and d have no domain tag");
    }

    #[test]
    fn the_filter_matches_text_and_status_together() {
        let rows = vec![
            row("tududi", "red", &[], &[]),
            row("honcho", "green", &[], &[]),
            row("postiz", "red", &[], &[]),
        ];
        let by_text = filter_services(
            &rows,
            &ServiceFilter {
                text: "  TUD ".into(),
                rag: None,
            },
        );
        assert_eq!(by_text.len(), 1);
        assert_eq!(by_text[0].name, "tududi");
        let red = filter_services(
            &rows,
            &ServiceFilter {
                text: String::new(),
                rag: Some(Rag::Red),
            },
        );
        let names: Vec<&str> = red.iter().map(|r| r.name.as_str()).collect();
        assert_eq!(names, ["tududi", "postiz"], "server order kept");
        let both = filter_services(
            &rows,
            &ServiceFilter {
                text: "honcho".into(),
                rag: Some(Rag::Red),
            },
        );
        assert!(both.is_empty(), "both filters must pass");
    }

    #[test]
    fn a_text_filter_reads_the_site_and_the_cluster_too() {
        let rows = vec![row("tududi", "green", &[], &[])];
        let by_site = filter_services(
            &rows,
            &ServiceFilter {
                text: "DC1".into(),
                rag: None,
            },
        );
        assert_eq!(by_site.len(), 1);
        let by_cluster = filter_services(
            &rows,
            &ServiceFilter {
                text: "pve-prod".into(),
                rag: None,
            },
        );
        assert_eq!(by_cluster.len(), 1);
    }

    #[test]
    fn failed_steps_read_as_names_or_a_dash() {
        assert_eq!(
            failed_text(&row("a", "red", &[], &["step-b", "step-c"])),
            "step-b, step-c"
        );
        assert_eq!(failed_text(&row("a", "green", &[], &[])), DASH);
    }

    #[test]
    fn the_filter_starts_with_every_row_and_every_status() {
        assert_eq!(ServiceFilter::default().rag, None);
        assert_eq!(ServiceFilter::default().text, "");
        let rows = vec![row("a", "red", &[], &[]), row("b", "grey", &[], &[])];
        assert_eq!(filter_services(&rows, &ServiceFilter::default()).len(), 2);
    }

    #[test]
    fn slug_joins_words_with_hyphens() {
        assert_eq!(slug("Professional   Services"), "professional-services");
        assert_eq!(slug(" IT "), "it");
    }
}
