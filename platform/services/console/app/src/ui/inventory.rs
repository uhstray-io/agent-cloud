//! The inventory the signed-in shell holds for the selected tenant, the 30 s poll
//! that refreshes it, and the admin-only reads the Topology tabs make.
//!
//! Pages read the snapshot; they do not call the API. The poll and the one-off
//! refresh on a tenant switch are the only writers. A late answer for a tenant
//! that is no longer selected is dropped, so one tenant's data never shows under
//! another tenant's name.
//!
//! Selection, rollups and unit text are plain functions with tests. The
//! components only draw what those functions return.

use dioxus::prelude::*;
use serde::de::DeserializeOwned;

use super::api;
use super::inventory_api::{self, Inventory, Meta, Read, ServicesBody};
use super::shell::{Ctx, Loaded};
use super::status::{worst, Notice, Rag};

/// How often the tenant answers and the admin answers are re-read while the tab is visible.
pub const POLL_MS: u32 = 30_000;

/// What the shell keeps for the selected tenant. A failed refresh keeps the last
/// good body for that tenant and sets `reason`.
#[derive(Debug, Clone, PartialEq)]
pub struct Snapshot {
    pub tenant: String,
    pub inventory: Option<Inventory>,
    pub services: Option<ServicesBody>,
    /// Why the newest refresh failed, when it did.
    pub reason: Option<String>,
}

/// The fixed sentence a failed read reports, or `None` for a read that worked.
pub fn failure_text<T>(read: &Read<T>) -> Option<String> {
    match read {
        Read::Ok(_) => None,
        Read::SignedOut => Some("your session ended".to_string()),
        Read::Forbidden => Some("the console refused this read".to_string()),
        Read::Missing => Some("not found in the selected tenant".to_string()),
        Read::Failed(text) => Some(text.clone()),
    }
}

/// The snapshot after one refresh. A body that failed to read is kept from `prev`
/// only when `prev` is for the same tenant. Pure.
pub fn merge(
    prev: Option<&Snapshot>,
    tenant: &str,
    inv: Read<Inventory>,
    svc: Read<ServicesBody>,
) -> Snapshot {
    let kept = prev.filter(|p| p.tenant == tenant);
    let mut reasons: Vec<String> = Vec::new();
    let inventory = match inv {
        Read::Ok(body) => Some(body),
        other => {
            reasons.extend(failure_text(&other));
            kept.and_then(|p| p.inventory.clone())
        }
    };
    let services = match svc {
        Read::Ok(body) => Some(body),
        other => {
            reasons.extend(failure_text(&other));
            kept.and_then(|p| p.services.clone())
        }
    };
    Snapshot {
        tenant: tenant.to_string(),
        inventory,
        services,
        reason: (!reasons.is_empty()).then(|| reasons.join("; ")),
    }
}

/// Reads both tenant answers and writes the merged snapshot. The write is skipped
/// when the selected tenant changed while the reads were in flight.
pub async fn refresh(ctx: Ctx, tenant: String) {
    let inv = inventory_api::read::<Inventory>(&inventory_api::inventory_path(&tenant)).await;
    let svc = inventory_api::read::<ServicesBody>(&inventory_api::services_path(&tenant)).await;
    if ctx.tenant.peek().as_deref() != Some(tenant.as_str()) {
        return;
    }
    let prev = match &*ctx.inventory.peek() {
        Loaded::Ready(snap) => Some(snap.clone()),
        _ => None,
    };
    let next = merge(prev.as_ref(), &tenant, inv, svc);
    let mut inventory = ctx.inventory;
    inventory.set(Loaded::Ready(next));
}

/// The poll the signed-in shell starts once. It waits for the tab to be visible,
/// refreshes the selected tenant if there is one, then sleeps `POLL_MS` or until
/// the tab is hidden.
pub async fn poll(ctx: Ctx) {
    loop {
        api::wait_visible().await;
        let tenant = (*ctx.tenant.peek()).clone();
        if let Some(tenant) = tenant {
            refresh(ctx, tenant).await;
        }
        api::sleep_unless_hidden(POLL_MS).await;
    }
}

/// What a tenant page shows: the snapshot, or why there is none to show.
#[derive(Debug, Clone, PartialEq)]
pub enum View {
    NoTenant,
    Loading,
    Failed(String),
    Ready(Box<Snapshot>),
}

/// The view for the selected tenant. A snapshot for another tenant reads as loading.
pub fn view(state: &Loaded<Snapshot>, tenant: Option<&str>) -> View {
    let Some(tenant) = tenant else {
        return View::NoTenant;
    };
    match state {
        Loaded::Ready(snap) if snap.tenant == tenant => {
            if snap.inventory.is_none() && snap.services.is_none() {
                View::Failed(
                    snap.reason
                        .clone()
                        .unwrap_or_else(|| inventory_api::NO_ANSWER.to_string()),
                )
            } else {
                View::Ready(Box::new(snap.clone()))
            }
        }
        Loaded::Failed => View::Failed(inventory_api::NO_ANSWER.to_string()),
        _ => View::Loading,
    }
}

/// The envelope a page shows its banners from: the inventory's, else the services'.
pub fn meta_of(snap: &Snapshot) -> Option<&Meta> {
    snap.inventory
        .as_ref()
        .map(|inv| &inv.meta)
        .or_else(|| snap.services.as_ref().map(|svc| &svc.meta))
}

/// What an admin-only tab shows. `Forbidden` is the 403 answer, and polling stops there.
#[derive(Debug, Clone, PartialEq)]
pub enum Admin<T> {
    Loading,
    Forbidden,
    Shown { body: T, reason: Option<String> },
    Unavailable(String),
}

/// The state after one read. A failed read keeps the last good body with its reason.
pub fn admin_next<T: Clone>(prev: &Admin<T>, read: Read<T>) -> Admin<T> {
    match read {
        Read::Ok(body) => Admin::Shown { body, reason: None },
        Read::Forbidden => Admin::Forbidden,
        other => {
            let reason =
                failure_text(&other).unwrap_or_else(|| inventory_api::NO_ANSWER.to_string());
            match prev {
                Admin::Shown { body, .. } => Admin::Shown {
                    body: body.clone(),
                    reason: Some(reason),
                },
                _ => Admin::Unavailable(reason),
            }
        }
    }
}

/// Polls one admin-only path into `state`. It stops on 403. Otherwise it reads
/// while the tab is visible, like the tenant poll.
pub async fn poll_admin<T>(path: String, mut state: Signal<Admin<T>>)
where
    T: DeserializeOwned + Clone + PartialEq + 'static,
{
    loop {
        api::wait_visible().await;
        let read = inventory_api::read::<T>(&path).await;
        let next = admin_next(&state.peek(), read);
        let keep = !matches!(next, Admin::Forbidden);
        state.set(next);
        if !keep {
            return;
        }
        api::sleep_unless_hidden(POLL_MS).await;
    }
}

/// The kind of a selectable row in the compute tree.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    Site,
    Cluster,
    Node,
    Vm,
}

/// One selected row. `cluster` is set for a cluster, node or VM; `name` is the
/// cluster, node or VM name, and empty for a site (which uses `site` alone).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Pick {
    pub kind: Kind,
    pub site: String,
    pub cluster: Option<String>,
    pub name: String,
}

/// The selection after one click. A plain click replaces it. A shift-click adds
/// the pick, or removes it when it is already selected.
pub fn after_click(picks: &[Pick], pick: Pick, shift: bool) -> Vec<Pick> {
    if !shift {
        return vec![pick];
    }
    if picks.contains(&pick) {
        picks.iter().filter(|p| **p != pick).cloned().collect()
    } else {
        let mut next = picks.to_vec();
        next.push(pick);
        next
    }
}

/// True when the pick still names a row of `inv`. A row the next poll removed, or a
/// row of another tenant that shares an id, does not resolve.
pub fn resolves(inv: &Inventory, pick: &Pick) -> bool {
    let Some(site) = inv.sites.iter().find(|s| s.id == pick.site) else {
        return false;
    };
    if pick.kind == Kind::Site {
        return true;
    }
    let Some(cluster) = site
        .clusters
        .iter()
        .find(|c| Some(c.name.as_str()) == pick.cluster.as_deref())
    else {
        return false;
    };
    match pick.kind {
        Kind::Site | Kind::Cluster => true,
        Kind::Node => cluster.nodes.iter().any(|n| n.name == pick.name),
        Kind::Vm => cluster.vms.iter().any(|v| v.name == pick.name),
    }
}

/// The picks that still resolve. A selection outlives the poll that removed its row, and
/// must not keep showing the removed row.
pub fn live_picks(inv: &Inventory, picks: &[Pick]) -> Vec<Pick> {
    picks
        .iter()
        .filter(|pick| resolves(inv, pick))
        .cloned()
        .collect()
}

/// The VM RAGs one pick stands for: a VM its own, a cluster or a site all of its
/// VMs. A node stands for none, because the API carries no RAG for a node.
pub fn pick_rags(inv: &Inventory, pick: &Pick) -> Vec<Rag> {
    let mut out = Vec::new();
    for site in inv.sites.iter().filter(|s| s.id == pick.site) {
        for cluster in &site.clusters {
            let in_scope = match pick.kind {
                Kind::Site => true,
                Kind::Cluster | Kind::Node | Kind::Vm => {
                    pick.cluster.as_deref() == Some(cluster.name.as_str())
                }
            };
            if !in_scope {
                continue;
            }
            for vm in &cluster.vms {
                let counted = match pick.kind {
                    Kind::Vm => vm.name == pick.name,
                    Kind::Cluster | Kind::Site => true,
                    Kind::Node => false,
                };
                if counted {
                    out.push(Rag::from_wire(&vm.rag));
                }
            }
        }
    }
    out
}

/// The rollup for a whole selection: the most severe RAG among its VMs. Grey
/// when the selection holds no VM.
pub fn selection_rag(inv: &Inventory, picks: &[Pick]) -> Rag {
    worst(picks.iter().flat_map(|pick| pick_rags(inv, pick)))
}

/// CPU fraction (0 to 1, Proxmox's unit) as a percent, clamped to 0 to 100.
pub fn cpu_percent(fraction: f64) -> f64 {
    (fraction * 100.0).clamp(0.0, 100.0)
}

/// Memory as a percent of the node's total. `None` when the total is zero.
pub fn mem_percent(mem: u64, maxmem: u64) -> Option<f64> {
    (maxmem > 0).then(|| (mem as f64 / maxmem as f64 * 100.0).clamp(0.0, 100.0))
}

const GIB: f64 = 1024.0 * 1024.0 * 1024.0;
const MIB: f64 = 1024.0 * 1024.0;

/// Bytes as a size in GiB (one decimal) or MiB (whole number).
pub fn bytes_text(bytes: u64) -> String {
    let b = bytes as f64;
    if b >= GIB {
        format!("{:.1} GiB", b / GIB)
    } else {
        format!("{:.0} MiB", b / MIB)
    }
}

/// Megabytes (the NetBox memory field) as a size.
pub fn mb_text(megabytes: u64) -> String {
    bytes_text(megabytes.saturating_mul(1024 * 1024))
}

/// Seconds of uptime as `3d 4h`, `4h 12m` or `12m`.
pub fn uptime_text(seconds: u64) -> String {
    let days = seconds / 86_400;
    let hours = (seconds % 86_400) / 3_600;
    let minutes = (seconds % 3_600) / 60;
    if days > 0 {
        format!("{days}d {hours}h")
    } else if hours > 0 {
        format!("{hours}h {minutes}m")
    } else {
        format!("{minutes}m")
    }
}

/// A count with its noun, singular for one: `1 VM`, `2 VMs`.
pub fn count_text(count: usize, singular: &str, plural: &str) -> String {
    if count == 1 {
        format!("1 {singular}")
    } else {
        format!("{count} {plural}")
    }
}

/// The notice for a tenant page with nothing to show yet, or a failure.
#[component]
pub fn ViewNotice(view: View) -> Element {
    match view {
        View::NoTenant => rsx! {
            Notice { message: "Pick a tenant in the sidebar to see its inventory." }
        },
        View::Loading => rsx! {
            Notice { message: "Loading inventory\u{2026}" }
        },
        View::Failed(reason) => rsx! {
            Notice {
                message: format!("The inventory could not be loaded: {reason}. It retries every 30 seconds while this tab is visible."),
            }
        },
        View::Ready(_) => rsx! {},
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ui::inventory_api::{Cluster, Site, Vm};

    fn vm(name: &str, rag: &str) -> Vm {
        Vm {
            name: name.into(),
            rag: rag.into(),
            ..Vm::default()
        }
    }

    fn inventory() -> Inventory {
        Inventory {
            sites: vec![
                Site {
                    id: "dc1".into(),
                    name: "DC 1".into(),
                    mapped: true,
                    clusters: vec![Cluster {
                        name: "pve-prod".into(),
                        vms: vec![vm("tududi", "red"), vm("honcho", "green")],
                        ..Cluster::default()
                    }],
                    ..Site::default()
                },
                Site {
                    id: "dc2".into(),
                    name: "DC 2".into(),
                    clusters: vec![Cluster {
                        name: "pve-lab".into(),
                        vms: vec![vm("scratch", "grey")],
                        ..Cluster::default()
                    }],
                    ..Site::default()
                },
            ],
            ..Inventory::default()
        }
    }

    fn pick(kind: Kind, site: &str, cluster: Option<&str>, name: &str) -> Pick {
        Pick {
            kind,
            site: site.into(),
            cluster: cluster.map(str::to_string),
            name: name.into(),
        }
    }

    fn snap(tenant: &str, inv: Option<Inventory>) -> Snapshot {
        Snapshot {
            tenant: tenant.into(),
            inventory: inv,
            services: None,
            reason: None,
        }
    }

    #[test]
    fn a_plain_click_replaces_and_a_shift_click_toggles() {
        let a = pick(Kind::Vm, "dc1", Some("pve-prod"), "tududi");
        let b = pick(Kind::Vm, "dc1", Some("pve-prod"), "honcho");
        let one = after_click(&[], a.clone(), false);
        assert_eq!(one, vec![a.clone()]);
        let replaced = after_click(&one, b.clone(), false);
        assert_eq!(replaced, vec![b.clone()], "a plain click does not add");
        let both = after_click(&replaced, a.clone(), true);
        assert_eq!(both, vec![b.clone(), a.clone()], "shift adds");
        let without_a = after_click(&both, a.clone(), true);
        assert_eq!(without_a, vec![b], "shift on a selected row removes it");
    }

    #[test]
    fn a_selection_rolls_up_to_the_worst_vm() {
        let inv = inventory();
        let tududi = pick(Kind::Vm, "dc1", Some("pve-prod"), "tududi");
        let honcho = pick(Kind::Vm, "dc1", Some("pve-prod"), "honcho");
        assert_eq!(selection_rag(&inv, std::slice::from_ref(&tududi)), Rag::Red);
        assert_eq!(
            selection_rag(&inv, std::slice::from_ref(&honcho)),
            Rag::Green
        );
        assert_eq!(selection_rag(&inv, &[honcho, tududi]), Rag::Red);
    }

    #[test]
    fn a_cluster_or_site_pick_rolls_up_all_its_vms() {
        let inv = inventory();
        let cluster = pick(Kind::Cluster, "dc1", Some("pve-prod"), "pve-prod");
        assert_eq!(selection_rag(&inv, &[cluster]), Rag::Red);
        let site = pick(Kind::Site, "dc1", None, "");
        assert_eq!(selection_rag(&inv, &[site]), Rag::Red);
        let lab = pick(Kind::Site, "dc2", None, "");
        assert_eq!(selection_rag(&inv, &[lab]), Rag::Grey);
    }

    #[test]
    fn a_pick_whose_row_left_the_inventory_is_dropped() {
        let inv = inventory();
        let kept = pick(Kind::Vm, "dc1", Some("pve-prod"), "tududi");
        let gone = pick(Kind::Vm, "dc1", Some("pve-prod"), "removed");
        let live = live_picks(&inv, &[kept.clone(), gone]);
        assert_eq!(live, vec![kept], "a removed row is not counted");
    }

    #[test]
    fn picks_from_another_tenant_do_not_resolve() {
        // The same site id in a different tenant, with other clusters: nothing resolves.
        let other = Inventory {
            sites: vec![Site {
                id: "dc1".into(),
                clusters: vec![Cluster {
                    name: "pve-other".into(),
                    vms: vec![vm("tududi", "green")],
                    ..Cluster::default()
                }],
                ..Site::default()
            }],
            ..Inventory::default()
        };
        let from_first = pick(Kind::Vm, "dc1", Some("pve-prod"), "tududi");
        assert!(!resolves(&other, &from_first));
        assert!(live_picks(&other, &[from_first]).is_empty());
        let node_of_other = pick(Kind::Node, "dc1", Some("pve-other"), "pve-01");
        assert!(!resolves(&other, &node_of_other), "no node of that name");
    }

    #[test]
    fn a_node_or_an_empty_selection_has_no_rag_to_roll_up() {
        let inv = inventory();
        let node = pick(Kind::Node, "dc1", Some("pve-prod"), "pve-01");
        assert!(pick_rags(&inv, &node).is_empty());
        assert_eq!(selection_rag(&inv, &[node]), Rag::Grey);
        assert_eq!(selection_rag(&inv, &[]), Rag::Grey);
    }

    #[test]
    fn picks_of_different_kinds_are_different_rows() {
        let site = pick(Kind::Site, "dc1", None, "");
        let cluster = pick(Kind::Cluster, "dc1", Some("pve-prod"), "pve-prod");
        let node = pick(Kind::Node, "dc1", Some("pve-prod"), "pve-prod");
        assert_ne!(site, cluster);
        assert_ne!(cluster, node);
    }

    #[test]
    fn a_failed_refresh_keeps_the_same_tenants_last_good_body() {
        let prev = snap("uhstray", Some(inventory()));
        let kept = merge(
            Some(&prev),
            "uhstray",
            Read::Failed(inventory_api::NO_ANSWER.into()),
            Read::Missing,
        );
        assert!(kept.inventory.is_some(), "last good inventory kept");
        assert_eq!(
            kept.reason.as_deref(),
            Some("the console API did not answer; not found in the selected tenant")
        );
    }

    #[test]
    fn a_different_tenants_snapshot_is_never_kept() {
        let prev = snap("acme", Some(inventory()));
        let next = merge(
            Some(&prev),
            "uhstray",
            Read::Failed(inventory_api::NO_ANSWER.into()),
            Read::Failed(inventory_api::NO_ANSWER.into()),
        );
        assert!(
            next.inventory.is_none(),
            "acme's data never shows as uhstray's"
        );
        assert_eq!(next.tenant, "uhstray");
    }

    #[test]
    fn a_snapshot_for_another_tenant_reads_as_loading() {
        let state = Loaded::Ready(snap("acme", Some(inventory())));
        assert_eq!(view(&state, None), View::NoTenant);
        assert_eq!(view(&state, Some("uhstray")), View::Loading);
        assert!(matches!(view(&state, Some("acme")), View::Ready(_)));
    }

    #[test]
    fn a_snapshot_with_no_body_and_a_reason_is_a_failure() {
        let state = Loaded::Ready(Snapshot {
            tenant: "uhstray".into(),
            inventory: None,
            services: None,
            reason: Some("the console sent an answer it could not read".into()),
        });
        assert_eq!(
            view(&state, Some("uhstray")),
            View::Failed("the console sent an answer it could not read".into())
        );
    }

    #[test]
    fn admin_polling_keeps_the_last_body_and_stops_on_forbidden() {
        let first = admin_next(&Admin::Loading, Read::Ok(7u32));
        assert_eq!(
            first,
            Admin::Shown {
                body: 7,
                reason: None
            }
        );
        let failed = admin_next(&first, Read::Failed(inventory_api::NO_ANSWER.into()));
        assert_eq!(
            failed,
            Admin::Shown {
                body: 7,
                reason: Some(inventory_api::NO_ANSWER.into())
            }
        );
        assert_eq!(
            admin_next(&Admin::<u32>::Loading, Read::Failed("x".into())),
            Admin::Unavailable("x".into())
        );
        assert_eq!(
            admin_next(&first, Read::<u32>::Forbidden),
            Admin::Forbidden,
            "a non-admin sees the forbidden state"
        );
    }

    #[test]
    fn cpu_and_memory_percents_are_clamped_and_guard_zero_total() {
        assert!((cpu_percent(0.12) - 12.0).abs() < 1e-9);
        assert_eq!(cpu_percent(1.7), 100.0);
        assert_eq!(mem_percent(8_589_934_592, 68_719_476_736), Some(12.5));
        assert_eq!(mem_percent(1, 0), None, "no total, no percent");
    }

    #[test]
    fn sizes_and_uptime_read_in_human_units() {
        assert_eq!(bytes_text(8_589_934_592), "8.0 GiB");
        assert_eq!(bytes_text(68_719_476_736), "64.0 GiB");
        assert_eq!(bytes_text(1_048_576), "1 MiB");
        assert_eq!(mb_text(4096), "4.0 GiB");
        assert_eq!(uptime_text(123_456), "1d 10h");
        assert_eq!(uptime_text(3 * 3_600 + 12 * 60), "3h 12m");
        assert_eq!(uptime_text(12 * 60), "12m");
    }

    #[test]
    fn counts_pluralise_on_one_only() {
        assert_eq!(count_text(1, "VM", "VMs"), "1 VM");
        assert_eq!(count_text(0, "VM", "VMs"), "0 VMs");
        assert_eq!(count_text(3, "VM", "VMs"), "3 VMs");
    }
}
