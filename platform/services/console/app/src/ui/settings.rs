//! The two settings pages: Tenant settings and Organization settings.
//!
//! Both read the tenant's tree (`settings_api`, `GET /v1/tenants/{t}/tree`). The Organization page
//! also counts from the inventory snapshot the shell already holds. Neither page writes.
//!
//! Every change control is disabled and says why: settings changes arrive with proposals (Phase 4).
//! Business domains are NetBox tags, which are global, so the Organization page lists them as
//! platform-wide labels and counts only the services of its own sites. The Tenant page shows the
//! structure and no domains.

use dioxus::prelude::*;

use super::icons::icon;
use super::inventory::{meta_of, poll_admin, view, Admin, Snapshot, View, POLL_MS};
use super::inventory_api::{self, Domain, ServiceRow};
use super::services::DomainSwatch;
use super::settings_api::{tree_path, TenantTree, TreeOrg, TreeSite};
use super::shell::Ctx;
use super::status::{Notice, DASH};

/// Why every change control on these pages is disabled.
pub const CHANGE_REASON: &str = "Settings changes arrive with proposals (Phase 4)";

/// Which settings page a tree read shows.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SettingsPage {
    Tenant,
    Org,
}

/// One row of the Organization page's domain table. `services` is `None` while the service read
/// has not answered. The page shows that as a dash, never as a zero.
#[derive(Debug, Clone, PartialEq)]
pub struct DomainRow {
    pub domain: Domain,
    pub services: Option<usize>,
}

/// The org with this id in the tree, if the tree holds it.
pub fn find_org<'a>(tree: &'a TenantTree, org_id: &str) -> Option<&'a TreeOrg> {
    tree.orgs.iter().find(|org| org.id == org_id)
}

/// The NetBox site a site maps to, or "not mapped". The console never guesses a NetBox site from
/// the site's name, so an absent mapping is shown as absent.
pub fn mapped_text(netbox_site: Option<&str>) -> String {
    netbox_site.map_or_else(|| "not mapped".to_string(), str::to_string)
}

/// The sites that map to a NetBox site, out of all of them, as `mapped/total`.
pub fn mapped_tally(sites: &[TreeSite]) -> String {
    let mapped = sites
        .iter()
        .filter(|site| site.netbox_site.is_some())
        .count();
    format!("{mapped}/{}", sites.len())
}

/// The environments as text, or a dash when the site declares none.
pub fn envs_text(envs: &[String]) -> String {
    if envs.is_empty() {
        DASH.to_string()
    } else {
        envs.join(", ")
    }
}

/// Services in the org's sites that carry this domain slug. The site test is the point: a service
/// of another org never counts here, even when it carries the same domain.
pub fn count_in_org(rows: &[ServiceRow], org_site_ids: &[&str], slug: &str) -> usize {
    rows.iter()
        .filter(|row| {
            org_site_ids.contains(&row.site.as_str()) && row.domains.iter().any(|d| d == slug)
        })
        .count()
}

/// The Organization page's domain table, from the snapshot. A snapshot with no inventory has no
/// domains to list. One with no service read leaves every count unknown.
pub fn org_domain_rows(snap: &Snapshot, org_site_ids: &[&str]) -> Vec<DomainRow> {
    let domains = snap
        .inventory
        .as_ref()
        .map(|inv| inv.domains.clone())
        .unwrap_or_default();
    let services = snap.services.as_ref().map(|svc| svc.services.as_slice());
    domains
        .into_iter()
        .map(|domain| DomainRow {
            services: services.map(|rows| count_in_org(rows, org_site_ids, &domain.slug)),
            domain,
        })
        .collect()
}

/// Tenant settings: the tenant's name, and its organizations with their NetBox mapping.
#[component]
pub fn TenantSettings() -> Element {
    let ctx = use_context::<Ctx>();
    let tenant = ctx.tenant.read().clone();
    let body = match tenant {
        Some(tenant) => rsx! {
            TreeRead { key: "{tenant}", tenant, page: SettingsPage::Tenant }
        },
        None => rsx! {
            Notice { message: "Pick a tenant in the sidebar to see its settings.".to_string() }
        },
    };
    rsx! {
        div { class: "view settings",
            header { class: "view-h",
                h1 { {icon("building")} "Tenant settings" }
                span { class: "authority", "Source of truth: site-config inventory" }
                p { "The tenant's structure: its organizations and each site's NetBox mapping. A change is proposed, never written directly." }
            }
            {body}
        }
    }
}

/// Organization settings: the selected organization's sites, and the platform's business domains
/// with the services of this organization in each.
#[component]
pub fn OrgSettings() -> Element {
    let ctx = use_context::<Ctx>();
    let tenant = ctx.tenant.read().clone();
    let body = match tenant {
        Some(tenant) => rsx! {
            TreeRead { key: "{tenant}", tenant, page: SettingsPage::Org }
        },
        None => rsx! {
            Notice { message: "Pick a tenant in the sidebar to see its settings.".to_string() }
        },
    };
    rsx! {
        div { class: "view settings",
            header { class: "view-h",
                h1 { {icon("settings-2")} "Organization settings" }
                span { class: "authority", "Source of truth: site-config inventory, NetBox tags" }
                p { "The selected organization's sites, and the business domains NetBox holds. A change is proposed, never written directly." }
            }
            {body}
        }
    }
}

/// Reads the tenant tree while the tab is visible, then shows the page for it. The caller keys it
/// on the tenant, so a tenant switch starts a fresh read.
#[component]
fn TreeRead(tenant: String, page: SettingsPage) -> Element {
    let ctx = use_context::<Ctx>();
    let state = use_signal(|| Admin::<TenantTree>::Loading);
    let path = tree_path(&tenant);
    use_hook(move || {
        spawn(poll_admin(path, state, ctx.me));
    });
    let shown = state.read().clone();
    match shown {
        Admin::Loading => rsx! { Notice { message: "Loading the settings\u{2026}".to_string() } },
        Admin::Forbidden => rsx! {
            Notice { message: "These settings are not available to you.".to_string() }
        },
        Admin::Missing => rsx! {
            Notice { message: "This tenant is not available to you.".to_string() }
        },
        Admin::SignedOut => rsx! {
            Notice { message: "Your session ended. Sign in again to see these settings.".to_string() }
        },
        Admin::Unavailable(reason) => rsx! {
            Notice { message: format!("The settings could not be loaded: {reason}. They retry every {} seconds while this tab is visible.", POLL_MS / 1000) }
        },
        Admin::Shown { body, reason } => {
            let reason = reason.unwrap_or_default();
            match page {
                SettingsPage::Tenant => rsx! { TenantView { body, reason } },
                SettingsPage::Org => rsx! { OrgView { tenant, body, reason } },
            }
        }
    }
}

/// The Tenant settings body.
#[component]
fn TenantView(body: TenantTree, reason: String) -> Element {
    let name = body.tenant.name.clone();
    let id = body.tenant.id.clone();
    let orgs: Vec<(String, usize, String)> = body
        .orgs
        .iter()
        .map(|org| (org.name.clone(), org.sites.len(), mapped_tally(&org.sites)))
        .collect();
    rsx! {
        if !reason.is_empty() {
            p { class: "hint", role: "status", "The last refresh failed ({reason}); the figures are from the last good read." }
        }
        section { class: "card",
            div { class: "card-h",
                h2 { "Tenant" }
            }
            div { class: "card-b",
                dl { class: "fields",
                    div { class: "field-row", dt { class: "label", "Name" } dd { "{name}" } }
                    div { class: "field-row", dt { class: "label", "ID" } dd { class: "mono", "{id}" } }
                }
            }
        }
        section { class: "card",
            div { class: "card-h",
                h2 { "Organizations" }
            }
            div { class: "tbl-wrap",
                table { class: "tbl",
                    thead {
                        tr {
                            th { "Organization" }
                            th { "Sites" }
                            th { "NetBox sites mapped" }
                        }
                    }
                    tbody {
                        for (org, sites, mapped) in orgs.iter() {
                            tr {
                                td { "{org}" }
                                td { "{sites}" }
                                td { class: "mono", "{mapped}" }
                            }
                        }
                    }
                }
            }
            if orgs.is_empty() {
                p { class: "empty", "No organization is declared for this tenant." }
            }
        }
        section { class: "card",
            div { class: "card-h",
                h2 { "Change" }
            }
            div { class: "card-b stack",
                div { class: "row wrap",
                    button { class: "btn", r#type: "button", disabled: true, title: "{CHANGE_REASON}", "Edit tenant" }
                }
                p { class: "hint", "{CHANGE_REASON}" }
            }
        }
    }
}

/// The Organization settings body for the org the scope bar has chosen.
#[component]
fn OrgView(tenant: String, body: TenantTree, reason: String) -> Element {
    let ctx = use_context::<Ctx>();
    let scope = ctx.scope.read().clone();
    let chosen = if scope.org.is_empty() {
        None
    } else {
        find_org(&body, &scope.org)
    };
    let Some(org) = chosen else {
        return rsx! {
            Notice { message: "Pick an organization in the scope bar to see its settings.".to_string() }
        };
    };
    let snapshot = ctx.inventory.read().clone();
    let org_name = org.name.clone();
    let org_id = org.id.clone();
    let site_ids: Vec<&str> = org
        .sites
        .iter()
        .map(|site: &TreeSite| site.id.as_str())
        .collect();
    let sites: Vec<(String, String, String)> = org
        .sites
        .iter()
        .map(|site| {
            (
                site.name.clone(),
                envs_text(&site.envs),
                mapped_text(site.netbox_site.as_deref()),
            )
        })
        .collect();
    let (domain_rows, domains_loaded, domain_reason) = match view(&snapshot, Some(tenant.as_str()))
    {
        View::Ready(snap) => (
            org_domain_rows(&snap, &site_ids),
            snap.inventory.is_some(),
            meta_of(&snap).and_then(inventory_api::domains_error),
        ),
        _ => (Vec::new(), false, None),
    };
    let listed: Vec<Domain> = domain_rows.iter().map(|row| row.domain.clone()).collect();
    let empty_line = inventory_api::no_domains_line(&listed);
    let domain_cells: Vec<(Domain, String)> = domain_rows
        .into_iter()
        .map(|row| {
            let count = row.services.map_or(DASH.to_string(), |n| n.to_string());
            (row.domain, count)
        })
        .collect();
    rsx! {
        if !reason.is_empty() {
            p { class: "hint", role: "status", "The last refresh failed ({reason}); the figures are from the last good read." }
        }
        section { class: "card",
            div { class: "card-h",
                h2 { "Organization" }
            }
            div { class: "card-b",
                dl { class: "fields",
                    div { class: "field-row", dt { class: "label", "Name" } dd { "{org_name}" } }
                    div { class: "field-row", dt { class: "label", "ID" } dd { class: "mono", "{org_id}" } }
                }
            }
        }
        section { class: "card",
            div { class: "card-h",
                h2 { "Sites" }
            }
            div { class: "tbl-wrap",
                table { class: "tbl",
                    thead {
                        tr {
                            th { "Site" }
                            th { "Environments" }
                            th { "NetBox site" }
                        }
                    }
                    tbody {
                        for (name, envs, mapped) in sites.iter() {
                            tr {
                                td { "{name}" }
                                td { "{envs}" }
                                td { class: "mono", "{mapped}" }
                            }
                        }
                    }
                }
            }
            if sites.is_empty() {
                p { class: "empty", "This organization has no sites." }
            }
        }
        section { class: "card",
            div { class: "card-h",
                h2 { "Business domains" }
                span { class: "faint", "NetBox tags, platform-wide" }
            }
            div { class: "card-b stack",
                if let Some(reason) = domain_reason {
                    Notice { message: reason }
                }
                if domains_loaded {
                    div { class: "tbl-wrap",
                        table { class: "tbl",
                            thead {
                                tr {
                                    th { "Domain" }
                                    th { "Services in this org" }
                                    th { "Slug" }
                                }
                            }
                            tbody {
                                for (domain, count) in domain_cells.iter() {
                                    tr {
                                        td {
                                            DomainSwatch { domain: domain.clone() }
                                            "{domain.name}"
                                        }
                                        td { "{count}" }
                                        td { class: "mono", "{domain.slug}" }
                                    }
                                }
                            }
                        }
                    }
                    if let Some(line) = empty_line {
                        p { class: "hint", "{line}" }
                    }
                } else {
                    Notice { message: "The business domains are not loaded yet.".to_string() }
                }
                div { class: "row wrap",
                    button { class: "btn", r#type: "button", disabled: true, title: "{CHANGE_REASON}", "Edit domains" }
                }
                p { class: "hint", "{CHANGE_REASON}" }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::super::inventory_api::{Inventory, ServicesBody};
    use super::*;

    fn row(name: &str, site: &str, domains: &[&str]) -> ServiceRow {
        ServiceRow {
            name: name.into(),
            site: site.into(),
            domains: domains.iter().map(|d| d.to_string()).collect(),
            ..ServiceRow::default()
        }
    }

    fn domain(slug: &str, name: &str) -> Domain {
        Domain {
            name: name.into(),
            slug: slug.into(),
            color: "4caf50".into(),
        }
    }

    #[test]
    fn an_orgs_domain_count_is_its_own_sites_services_only() {
        let rows = vec![
            row("a", "dc1", &["domain-it"]),
            row("b", "dc2", &["domain-it"]),
            row("c", "dc1", &[]),
            row("d", "dc1", &["domain-it", "domain-products"]),
        ];
        assert_eq!(
            count_in_org(&rows, &["dc1"], "domain-it"),
            2,
            "b is another org's service, though it carries the domain"
        );
        assert_eq!(count_in_org(&rows, &["dc1"], "domain-products"), 1);
        assert_eq!(count_in_org(&rows, &["dc1", "dc2"], "domain-it"), 3);
        assert_eq!(
            count_in_org(&rows, &[], "domain-it"),
            0,
            "no sites, no services"
        );
    }

    #[test]
    fn domain_rows_count_from_the_snapshot_and_stay_unknown_until_services_load() {
        let snap = Snapshot {
            tenant: "acme".into(),
            inventory: Some(Inventory {
                domains: vec![domain("domain-it", "IT")],
                ..Inventory::default()
            }),
            services: None,
            reason: None,
        };
        let rows = org_domain_rows(&snap, &["dc1"]);
        assert_eq!(rows.len(), 1);
        assert_eq!(
            rows[0].services, None,
            "no service read yet is unknown, not zero"
        );

        let loaded = Snapshot {
            services: Some(ServicesBody {
                services: vec![row("a", "dc1", &["domain-it"])],
                ..ServicesBody::default()
            }),
            ..snap
        };
        assert_eq!(org_domain_rows(&loaded, &["dc1"])[0].services, Some(1));
    }

    #[test]
    fn no_inventory_means_no_domains_to_list() {
        let snap = Snapshot {
            tenant: "acme".into(),
            inventory: None,
            services: None,
            reason: None,
        };
        assert!(org_domain_rows(&snap, &["dc1"]).is_empty());
    }

    #[test]
    fn an_unmapped_site_says_so_and_a_mapped_one_names_its_slug() {
        assert_eq!(mapped_text(None), "not mapped");
        assert_eq!(mapped_text(Some("dc-1")), "dc-1");
    }

    #[test]
    fn the_mapping_tally_is_mapped_over_all_sites() {
        let sites = vec![
            TreeSite {
                id: "a".into(),
                netbox_site: Some("dc-1".into()),
                ..TreeSite::default()
            },
            TreeSite {
                id: "b".into(),
                ..TreeSite::default()
            },
        ];
        assert_eq!(mapped_tally(&sites), "1/2");
        assert_eq!(mapped_tally(&[]), "0/0");
    }

    #[test]
    fn envs_read_as_a_list_or_a_dash() {
        assert_eq!(
            envs_text(&["prod".to_string(), "dev".to_string()]),
            "prod, dev"
        );
        assert_eq!(envs_text(&[]), DASH);
    }

    #[test]
    fn an_org_is_found_by_id_and_an_unknown_one_is_none() {
        let tree = TenantTree {
            orgs: vec![TreeOrg {
                id: "ops".into(),
                name: "Ops".into(),
                sites: Vec::new(),
            }],
            ..TenantTree::default()
        };
        assert_eq!(
            find_org(&tree, "ops").map(|org| org.name.as_str()),
            Some("Ops")
        );
        assert!(find_org(&tree, "lab").is_none());
    }
}
