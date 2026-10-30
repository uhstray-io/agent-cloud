//! The inventory the signed-in shell holds for the selected tenant, the 30 s poll
//! that refreshes it, the admin-only reads the Topology tabs make, and the scope
//! bar's narrowing of what the tenant pages show.
//!
//! Pages read the snapshot; they do not call the API. The poll and the one-off
//! refresh on a tenant switch are the only writers. A late answer for a tenant
//! that is no longer selected is dropped, so one tenant's data never shows under
//! another tenant's name. A 401 ends the session the way the shell's tenant
//! list does: the shell shows sign-in.
//!
//! Selection, rollups, the scope filter and unit text are plain functions with
//! tests. The components only draw what those functions return.

use dioxus::prelude::*;
use serde::de::DeserializeOwned;

use super::api::{self, Me, Tree};
use super::inventory_api::{self, Cluster, Inventory, Meta, Read, ServiceRow, ServicesBody, Vm};
use super::scope::Scope;
use super::shell::{Ctx, Loaded};
use super::status::{worst, Notice, Rag};

/// How often the tenant answers and the admin answers are re-read while the tab is visible.
pub const POLL_MS: u32 = 30_000;

/// Separates the parts of a row key. A unit separator cannot appear in a name the API sends.
pub const SEP: char = '\u{1f}';

/// What the shell keeps for the selected tenant. A failed refresh keeps the last good
/// body for that tenant and sets `reason`.
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

/// The session ended. The shell's tenant list does the same on a 401: it sets the
/// session to signed out, and the shell then shows sign-in.
pub fn end_session(me: Signal<Me>) {
    let mut me = me;
    me.set(Me::SignedOut);
}

/// Reads both tenant answers and writes the merged snapshot. The write is skipped
/// when the selected tenant changed while the reads were in flight. Returns `false`
/// when the session ended, which tells the poll to stop.
pub async fn refresh(ctx: Ctx, tenant: String) -> bool {
    let inv = inventory_api::read::<Inventory>(&inventory_api::inventory_path(&tenant)).await;
    let svc = inventory_api::read::<ServicesBody>(&inventory_api::services_path(&tenant)).await;
    if matches!(inv, Read::SignedOut) || matches!(svc, Read::SignedOut) {
        end_session(ctx.me);
        return false;
    }
    if ctx.tenant.peek().as_deref() != Some(tenant.as_str()) {
        return true;
    }
    let prev = match &*ctx.inventory.peek() {
        Loaded::Ready(snap) => Some(snap.clone()),
        _ => None,
    };
    let next = merge(prev.as_ref(), &tenant, inv, svc);
    let mut inventory = ctx.inventory;
    inventory.set(Loaded::Ready(next));
    true
}

/// The poll the signed-in shell starts once. It waits for the tab to be visible,
/// refreshes the selected tenant if there is one, then sleeps `POLL_MS` or until
/// the tab is hidden. It stops when the session ends.
pub async fn poll(ctx: Ctx) {
    loop {
        api::wait_visible().await;
        let tenant = (*ctx.tenant.peek()).clone();
        if let Some(tenant) = tenant {
            if !refresh(ctx, tenant).await {
                return;
            }
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

/// What an admin-only tab, or one tenant service, shows.
#[derive(Debug, Clone, PartialEq)]
pub enum Admin<T> {
    Loading,
    /// 403: the caller is not a platform admin. Polling stops.
    Forbidden,
    /// 404: the tenant or the service is not the caller's, or does not exist. Polling stops,
    /// and a body loaded earlier is replaced by this answer.
    Missing,
    /// 401: the session ended. Polling stops, and the shell signs out.
    SignedOut,
    Shown {
        body: T,
        reason: Option<String>,
    },
    Unavailable(String),
}

/// The state after one read. A failed read keeps the last good body with its reason.
pub fn admin_next<T: Clone>(prev: &Admin<T>, read: Read<T>) -> Admin<T> {
    match read {
        Read::Ok(body) => Admin::Shown { body, reason: None },
        Read::Forbidden => Admin::Forbidden,
        Read::Missing => Admin::Missing,
        Read::SignedOut => Admin::SignedOut,
        Read::Failed(text) => match prev {
            Admin::Shown { body, .. } => Admin::Shown {
                body: body.clone(),
                reason: Some(text),
            },
            _ => Admin::Unavailable(text),
        },
    }
}

/// Whether the poll reads again. Forbidden, not found and a signed-out session end it.
pub fn keeps_polling_admin<T>(state: &Admin<T>) -> bool {
    !matches!(state, Admin::Forbidden | Admin::Missing | Admin::SignedOut)
}

/// Polls one path into `state` while the tab is visible. It stops on the answers
/// `keeps_polling_admin` names, and a 401 also ends the session.
pub async fn poll_admin<T>(path: String, mut state: Signal<Admin<T>>, me: Signal<Me>)
where
    T: DeserializeOwned + Clone + PartialEq + 'static,
{
    loop {
        api::wait_visible().await;
        let read = inventory_api::read::<T>(&path).await;
        let next = admin_next(&state.peek(), read);
        let keep = keeps_polling_admin(&next);
        let signed_out = matches!(next, Admin::SignedOut);
        state.set(next);
        if signed_out {
            end_session(me);
        }
        if !keep {
            return;
        }
        api::sleep_unless_hidden(POLL_MS).await;
    }
}

/// The tenant's scope tree when it has loaded, else `None`. The pages narrow by it.
pub fn loaded_tree(state: &Loaded<Tree>) -> Option<Tree> {
    match state {
        Loaded::Ready(tree) => Some(tree.clone()),
        _ => None,
    }
}

/// The scope bar's choice for a site: its org, and its environments from the tenant's
/// tree (the inventory answer carries neither). `None` for a part the answer did not give.
pub fn tree_site<'a>(tree: &'a Tree, site_id: &str) -> Option<(&'a str, &'a [String])> {
    tree.orgs.iter().find_map(|org| {
        org.sites
            .iter()
            .find(|site| site.id == site_id)
            .map(|site| (org.id.as_str(), site.envs.as_slice()))
    })
}

/// True when a site sits inside the scope bar's choice. An empty part means all:
/// no org, no site and no env. `org` and `envs` are the site's own. A part that needs
/// a value the answer did not give (an env with no tree) excludes the site, rather
/// than guess.
pub fn in_scope(scope: &Scope, site_id: &str, org: Option<&str>, envs: Option<&[String]>) -> bool {
    if !scope.org.is_empty() && org != Some(scope.org.as_str()) {
        return false;
    }
    if let Some(site) = &scope.site {
        if site != site_id {
            return false;
        }
    }
    match &scope.env {
        Some(env) => envs.is_some_and(|envs| envs.iter().any(|e| e == env)),
        None => true,
    }
}

/// The inventory narrowed to the scope bar. Sites outside it are removed whole.
pub fn scoped_inventory(inv: &Inventory, scope: &Scope, tree: Option<&Tree>) -> Inventory {
    let mut out = inv.clone();
    out.sites.retain(|site| {
        let envs = tree
            .and_then(|t| tree_site(t, &site.id))
            .map(|(_, envs)| envs);
        in_scope(scope, &site.id, Some(site.org.as_str()), envs)
    });
    out
}

/// The org a service's site belongs to. The inventory's own site is authoritative when it
/// has loaded. Otherwise the tenant tree gives it. `None` when neither names the site.
pub fn service_org<'a>(
    site_id: &str,
    inv: Option<&'a Inventory>,
    tree: Option<&'a Tree>,
) -> Option<&'a str> {
    inv.and_then(|inv| inv.sites.iter().find(|s| s.id == site_id))
        .map(|site| site.org.as_str())
        .or_else(|| tree.and_then(|t| tree_site(t, site_id)).map(|(org, _)| org))
}

/// The services narrowed to the scope bar. A service belongs to its VM's site. Its org
/// comes from the inventory when loaded, else the tree. Its envs come from the tree.
pub fn scoped_services(
    rows: &[ServiceRow],
    scope: &Scope,
    inv: Option<&Inventory>,
    tree: Option<&Tree>,
) -> Vec<ServiceRow> {
    rows.iter()
        .filter(|row| {
            let org = service_org(&row.site, inv, tree);
            let envs = tree
                .and_then(|t| tree_site(t, &row.site))
                .map(|(_, envs)| envs);
            in_scope(scope, &row.site, org, envs)
        })
        .cloned()
        .collect()
}

/// The services in the scope bar's choice, or why they cannot be narrowed yet.
#[derive(Debug, Clone, PartialEq)]
pub enum ScopedServices {
    Rows(Vec<ServiceRow>),
    /// The scope needs a source that has not loaded yet. Shown as "Loading scope…".
    Loading,
    /// The scope needs a source that failed. Shown as a notice, never as an empty list.
    Unresolved,
}

/// The services for the page: narrowed when the scope can be checked, otherwise loading
/// or unresolved. A service list is never reported as empty while the scope is pending.
pub fn services_in_scope(
    rows: &[ServiceRow],
    scope: &Scope,
    inv: Option<&Inventory>,
    tree: &Loaded<Tree>,
) -> ScopedServices {
    match scope_gate(scope, inv, tree) {
        ScopeGate::Loading => ScopedServices::Loading,
        ScopeGate::Unresolved => ScopedServices::Unresolved,
        ScopeGate::Ready => ScopedServices::Rows(scoped_services(
            rows,
            scope,
            inv,
            loaded_tree(tree).as_ref(),
        )),
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

/// One selected row. `cluster` is set for a cluster, node or VM. `name` is the
/// cluster, node or VM name, and empty for a site. `ordinal` is the row's position
/// in its cluster's `nodes` or `vms`: it tells two VMs of one name apart. It is
/// zero for a site or a cluster.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Pick {
    pub kind: Kind,
    pub site: String,
    pub cluster: Option<String>,
    pub name: String,
    /// The position in the cluster's `nodes` or `vms`. Used when the row has no VM ID.
    pub ordinal: usize,
    /// A VM's Proxmox or NetBox ID. When present it names the VM at any position, so a
    /// reorder keeps the pick on the same VM.
    pub vmid: Option<u64>,
}

/// The key one VM row is listed under: its site, cluster, name, ID and position.
/// Unique across the tree, including two VMs of one name in one cluster.
pub fn vm_key(site: &str, cluster: &str, vm: &Vm, ordinal: usize) -> String {
    let vmid = vm.vmid.map_or_else(|| "-".to_string(), |id| id.to_string());
    format!(
        "{site}{SEP}{cluster}{SEP}{}{SEP}{vmid}{SEP}{ordinal}",
        vm.name
    )
}

/// The key one node row is listed under: its site, cluster, name and position.
pub fn node_key(site: &str, cluster: &str, name: &str, ordinal: usize) -> String {
    format!("{site}{SEP}{cluster}{SEP}{name}{SEP}{ordinal}")
}

/// The selection after one click. A plain click replaces it. A shift-click adds
/// the pick, or removes it when it is already selected.
pub fn after_click(picks: &[Pick], pick: Pick, shift: bool) -> Vec<Pick> {
    if !shift {
        return vec![pick];
    }
    if picks.iter().any(|p| same_row(p, &pick)) {
        picks
            .iter()
            .filter(|p| !same_row(p, &pick))
            .cloned()
            .collect()
    } else {
        let mut next = picks.to_vec();
        next.push(pick);
        next
    }
}

/// True when the pick still names a row of `inv`. A row the next poll removed, a
/// row of another tenant that shares an id, or a position whose name has changed
/// does not resolve.
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
        Kind::Node => cluster
            .nodes
            .get(pick.ordinal)
            .is_some_and(|n| n.name == pick.name),
        Kind::Vm => vm_of(cluster, pick).is_some(),
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

/// The VM a VM pick names in its cluster. A pick with a VM ID finds that VM at any
/// position, so a reorder keeps it. A pick without one falls back to its position and name.
pub fn vm_of<'a>(cluster: &'a Cluster, pick: &Pick) -> Option<&'a Vm> {
    match pick.vmid {
        Some(id) => cluster.vms.iter().find(|vm| vm.vmid == Some(id)),
        None => cluster
            .vms
            .get(pick.ordinal)
            .filter(|vm| vm.name == pick.name),
    }
}

/// Whether the scope bar can narrow the data yet.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ScopeGate {
    /// Every part the scope names can be checked.
    Ready,
    /// A part needs the tenant tree, and the tree is still being read.
    Loading,
    /// A part needs a source that failed to load, so it cannot be checked honestly.
    Unresolved,
}

/// What the scope needs before it can narrow. An org comes from the inventory's own sites
/// or from the tree. An env comes only from the tree. A site needs neither. An empty scope
/// needs nothing.
pub fn scope_gate(scope: &Scope, inv: Option<&Inventory>, tree: &Loaded<Tree>) -> ScopeGate {
    let tree_ready = matches!(tree, Loaded::Ready(_));
    let org_known = inv.is_some() || tree_ready;
    let needs_org = !scope.org.is_empty();
    let needs_env = scope.env.is_some();
    if (needs_org && !org_known) || (needs_env && !tree_ready) {
        return if matches!(tree, Loaded::Loading) {
            ScopeGate::Loading
        } else {
            ScopeGate::Unresolved
        };
    }
    ScopeGate::Ready
}
/// True when two picks name the same row. A VM with a VM ID is the same row at any
/// position. Other rows, and VMs without an ID, are the same when their position and name agree.
pub fn same_row(a: &Pick, b: &Pick) -> bool {
    if a.kind != b.kind || a.site != b.site || a.cluster != b.cluster {
        return false;
    }
    match (a.kind, a.vmid, b.vmid) {
        (Kind::Vm, Some(x), Some(y)) => x == y,
        _ => a.name == b.name && a.ordinal == b.ordinal,
    }
}

/// The VM RAGs one pick stands for: a VM its own, a cluster or a site all of its
/// VMs. A node stands for none, because the API carries no RAG for a node.
pub fn pick_rags(inv: &Inventory, pick: &Pick) -> Vec<Rag> {
    let rag_of = |vm: &Vm| Rag::from_wire(&vm.rag);
    let mut out = Vec::new();
    for site in inv.sites.iter().filter(|s| s.id == pick.site) {
        for cluster in &site.clusters {
            let this_cluster = pick.cluster.as_deref() == Some(cluster.name.as_str());
            match pick.kind {
                Kind::Site => out.extend(cluster.vms.iter().map(rag_of)),
                Kind::Cluster if this_cluster => out.extend(cluster.vms.iter().map(rag_of)),
                Kind::Vm if this_cluster => out.extend(vm_of(cluster, pick).map(rag_of)),
                _ => {}
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
            Notice { message: "Pick a tenant in the sidebar to see its inventory.".to_string() }
        },
        View::Loading => rsx! {
            Notice { message: "Loading inventory\u{2026}".to_string() }
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
    use crate::ui::api::{Org, Site as TreeSite};
    use crate::ui::inventory_api::{Cluster, Site};
    use crate::ui::scope::Scope;

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
                    org: "ops".into(),
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
                    org: "labs".into(),
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

    /// A pick of a VM at `ordinal` in the cluster.
    fn vm_pick(site: &str, cluster: &str, name: &str, ordinal: usize) -> Pick {
        Pick {
            kind: Kind::Vm,
            site: site.into(),
            cluster: Some(cluster.into()),
            name: name.into(),
            ordinal,
            vmid: None,
        }
    }

    fn site_pick(site: &str) -> Pick {
        Pick {
            kind: Kind::Site,
            site: site.into(),
            cluster: None,
            name: String::new(),
            ordinal: 0,
            vmid: None,
        }
    }

    fn tree() -> Tree {
        Tree {
            example: false,
            orgs: vec![
                Org {
                    id: "ops".into(),
                    name: "Ops".into(),
                    sites: vec![TreeSite {
                        id: "dc1".into(),
                        name: "DC 1".into(),
                        envs: vec!["prod".into()],
                    }],
                },
                Org {
                    id: "labs".into(),
                    name: "Labs".into(),
                    sites: vec![TreeSite {
                        id: "dc2".into(),
                        name: "DC 2".into(),
                        envs: vec!["dev".into()],
                    }],
                },
            ],
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
        let a = vm_pick("dc1", "pve-prod", "tududi", 0);
        let b = vm_pick("dc1", "pve-prod", "honcho", 1);
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
    fn two_vms_of_one_name_in_one_cluster_are_distinct_rows() {
        // Two VMs named "tududi" in pve-prod: different IDs, different RAGs.
        let mut first = vm("tududi", "red");
        first.vmid = Some(101);
        let mut second = vm("tududi", "green");
        second.vmid = Some(202);
        let inv = Inventory {
            sites: vec![Site {
                id: "dc1".into(),
                clusters: vec![Cluster {
                    name: "pve-prod".into(),
                    vms: vec![first.clone(), second.clone()],
                    ..Cluster::default()
                }],
                ..Site::default()
            }],
            ..Inventory::default()
        };
        // Distinct keys, so the list renders both.
        assert_ne!(
            vm_key("dc1", "pve-prod", &first, 0),
            vm_key("dc1", "pve-prod", &second, 1)
        );
        // Two NetBox-only VMs have no VM ID, so only their position can tell them apart.
        let unlinked = vm("tududi", "red");
        assert_ne!(
            vm_key("dc1", "pve-prod", &unlinked, 0),
            vm_key("dc1", "pve-prod", &unlinked, 1),
            "same name and no VM ID: the position keys them apart"
        );
        // Distinct picks, so each selects on its own.
        let a = vm_pick("dc1", "pve-prod", "tududi", 0);
        let b = vm_pick("dc1", "pve-prod", "tududi", 1);
        assert_ne!(a, b);
        let picked = after_click(std::slice::from_ref(&a), b.clone(), true);
        assert_eq!(
            picked,
            vec![a.clone(), b.clone()],
            "shift adds the second one"
        );
        assert_eq!(
            pick_rags(&inv, &a),
            vec![Rag::Red],
            "the first VM's own RAG"
        );
        assert_eq!(
            pick_rags(&inv, &b),
            vec![Rag::Green],
            "the second VM's own RAG"
        );
        assert_eq!(selection_rag(&inv, &picked), Rag::Red);
        let without_first = after_click(&picked, a, true);
        assert_eq!(
            without_first,
            vec![b.clone()],
            "removing one keeps the other"
        );
        assert_eq!(live_picks(&inv, std::slice::from_ref(&b)), vec![b]);
    }

    #[test]
    fn a_selection_rolls_up_to_the_worst_vm() {
        let inv = inventory();
        let tududi = vm_pick("dc1", "pve-prod", "tududi", 0);
        let honcho = vm_pick("dc1", "pve-prod", "honcho", 1);
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
        let cluster = Pick {
            kind: Kind::Cluster,
            site: "dc1".into(),
            cluster: Some("pve-prod".into()),
            name: "pve-prod".into(),
            ordinal: 0,
            vmid: None,
        };
        assert_eq!(selection_rag(&inv, &[cluster]), Rag::Red);
        assert_eq!(selection_rag(&inv, &[site_pick("dc1")]), Rag::Red);
        assert_eq!(selection_rag(&inv, &[site_pick("dc2")]), Rag::Grey);
    }

    #[test]
    fn a_node_or_an_empty_selection_has_no_rag_to_roll_up() {
        let inv = inventory();
        let node = Pick {
            kind: Kind::Node,
            site: "dc1".into(),
            cluster: Some("pve-prod".into()),
            name: "pve-01".into(),
            ordinal: 0,
            vmid: None,
        };
        assert!(pick_rags(&inv, &node).is_empty());
        assert_eq!(selection_rag(&inv, &[node]), Rag::Grey);
        assert_eq!(selection_rag(&inv, &[]), Rag::Grey);
    }

    #[test]
    fn picks_of_different_kinds_are_different_rows() {
        let site = site_pick("dc1");
        let cluster = Pick {
            kind: Kind::Cluster,
            site: "dc1".into(),
            cluster: Some("pve-prod".into()),
            name: "pve-prod".into(),
            ordinal: 0,
            vmid: None,
        };
        assert_ne!(site, cluster);
    }

    #[test]
    fn a_pick_whose_row_left_the_inventory_is_dropped() {
        let inv = inventory();
        let kept = vm_pick("dc1", "pve-prod", "tududi", 0);
        let gone = vm_pick("dc1", "pve-prod", "removed", 5);
        let moved = vm_pick("dc1", "pve-prod", "honcho", 0);
        let live = live_picks(&inv, &[kept.clone(), gone, moved]);
        assert_eq!(
            live,
            vec![kept],
            "a removed row, and a position that changed name, are not counted"
        );
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
        let from_first = vm_pick("dc1", "pve-prod", "tududi", 0);
        assert!(!resolves(&other, &from_first));
        assert!(live_picks(&other, &[from_first]).is_empty());
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
        assert!(keeps_polling_admin(&failed), "a failed read is retried");
        assert_eq!(
            admin_next(&Admin::<u32>::Loading, Read::Failed("x".into())),
            Admin::Unavailable("x".into())
        );
        let forbidden = admin_next(&first, Read::<u32>::Forbidden);
        assert_eq!(
            forbidden,
            Admin::Forbidden,
            "a non-admin sees the forbidden state"
        );
        assert!(!keeps_polling_admin(&forbidden));
    }

    #[test]
    fn a_not_found_after_a_good_load_replaces_the_body_and_stops() {
        let good = Admin::Shown {
            body: 7u32,
            reason: None,
        };
        let gone = admin_next(&good, Read::<u32>::Missing);
        assert_eq!(gone, Admin::Missing, "the old body is not kept");
        assert!(!keeps_polling_admin(&gone), "a 404 is not retried");
    }

    #[test]
    fn a_signed_out_read_ends_the_poll_without_a_retry() {
        let next = admin_next(
            &Admin::Shown {
                body: 1u32,
                reason: None,
            },
            Read::<u32>::SignedOut,
        );
        assert_eq!(next, Admin::SignedOut);
        assert!(!keeps_polling_admin(&next));
    }

    #[test]
    fn the_scope_bar_narrows_by_org_and_site_and_env_and_empty_is_tenant_wide() {
        let inv = inventory();
        let t = tree();
        let everything = scoped_inventory(&inv, &Scope::default(), Some(&t));
        assert_eq!(everything.sites.len(), 2, "an empty scope is tenant-wide");

        let by_org = Scope {
            org: "ops".into(),
            ..Scope::default()
        };
        let ops = scoped_inventory(&inv, &by_org, Some(&t));
        let ids: Vec<&str> = ops.sites.iter().map(|s| s.id.as_str()).collect();
        assert_eq!(ids, ["dc1"], "the org filter keeps its own sites");

        let by_site = Scope {
            org: "labs".into(),
            site: Some("dc2".into()),
            env: None,
        };
        let lab = scoped_inventory(&inv, &by_site, Some(&t));
        assert_eq!(lab.sites.len(), 1);
        assert_eq!(lab.sites[0].id, "dc2");

        let wrong_site = Scope {
            org: "ops".into(),
            site: Some("dc2".into()),
            env: None,
        };
        assert!(
            scoped_inventory(&inv, &wrong_site, Some(&t))
                .sites
                .is_empty(),
            "a site outside the org is not shown"
        );
    }

    #[test]
    fn the_env_filter_reads_the_tree_and_excludes_when_there_is_none() {
        let inv = inventory();
        let t = tree();
        let prod = Scope {
            env: Some("prod".into()),
            ..Scope::default()
        };
        let ids: Vec<String> = scoped_inventory(&inv, &prod, Some(&t))
            .sites
            .into_iter()
            .map(|s| s.id)
            .collect();
        assert_eq!(ids, ["dc1"], "only the site that runs prod");
        assert!(
            scoped_inventory(&inv, &prod, None).sites.is_empty(),
            "no tree means no env to match, so the site is excluded"
        );
    }

    #[test]
    fn services_follow_their_site_and_the_tree_gives_their_org() {
        let rows = vec![
            ServiceRow {
                name: "tududi".into(),
                site: "dc1".into(),
                ..ServiceRow::default()
            },
            ServiceRow {
                name: "scratch".into(),
                site: "dc2".into(),
                ..ServiceRow::default()
            },
        ];
        let t = tree();
        let ops = Scope {
            org: "ops".into(),
            ..Scope::default()
        };
        let names: Vec<String> = scoped_services(&rows, &ops, None, Some(&t))
            .into_iter()
            .map(|r| r.name)
            .collect();
        assert_eq!(names, ["tududi"]);
        assert_eq!(
            scoped_services(&rows, &Scope::default(), None, Some(&t)).len(),
            2,
            "empty scope keeps every row"
        );
        assert!(
            scoped_services(&rows, &ops, None, None).is_empty(),
            "without the tree the org is unknown, so no row is claimed for it"
        );
    }

    #[test]
    fn a_tree_that_has_not_loaded_narrows_nothing_and_claims_no_org() {
        assert_eq!(loaded_tree(&Loaded::<Tree>::Loading), None);
        assert_eq!(loaded_tree(&Loaded::<Tree>::Failed), None);
        assert!(loaded_tree(&Loaded::Ready(tree())).is_some());
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
        assert_eq!(
            mb_text(40_960),
            "40.0 GiB",
            "a disk in megabytes shows its GiB"
        );
        assert_eq!(mb_text(1536), "1.5 GiB", "one decimal");
        assert_eq!(mb_text(512), "512 MiB", "below a GiB the unit is MiB");
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

    fn tree_loaded() -> Loaded<Tree> {
        Loaded::Ready(tree())
    }

    fn service(name: &str, site: &str) -> ServiceRow {
        ServiceRow {
            name: name.into(),
            site: site.into(),
            ..ServiceRow::default()
        }
    }

    #[test]
    fn a_vm_pick_with_a_vm_id_survives_a_reorder() {
        let a = Vm {
            name: "tududi".into(),
            vmid: Some(101),
            rag: "red".into(),
            ..Vm::default()
        };
        let b = Vm {
            name: "honcho".into(),
            vmid: Some(202),
            rag: "green".into(),
            ..Vm::default()
        };
        let site = |vms: Vec<Vm>| Inventory {
            sites: vec![Site {
                id: "dc1".into(),
                clusters: vec![Cluster {
                    name: "pve-prod".into(),
                    vms,
                    ..Cluster::default()
                }],
                ..Site::default()
            }],
            ..Inventory::default()
        };
        let before = site(vec![a.clone(), b.clone()]);
        let picked = Pick {
            kind: Kind::Vm,
            site: "dc1".into(),
            cluster: Some("pve-prod".into()),
            name: "tududi".into(),
            ordinal: 0,
            vmid: Some(101),
        };
        assert!(resolves(&before, &picked));

        // The list is reordered by the next poll: tududi moves to position 1.
        let after = site(vec![b, a]);
        assert!(resolves(&after, &picked), "found by VM ID, not by position");
        assert_eq!(
            live_picks(&after, std::slice::from_ref(&picked)),
            vec![picked.clone()]
        );
        assert_eq!(
            pick_rags(&after, &picked),
            vec![Rag::Red],
            "the same VM's own RAG, not the VM now at position 0"
        );
        let found = vm_of(&after.sites[0].clusters[0], &picked).expect("the VM");
        assert_eq!(found.vmid, Some(101));

        // The row now renders at position 1. It is still the same row, so a shift-click removes it.
        let moved = Pick {
            ordinal: 1,
            ..picked.clone()
        };
        assert!(same_row(&moved, &picked));
        assert!(after_click(std::slice::from_ref(&picked), moved, true).is_empty());
    }

    #[test]
    fn a_vm_pick_without_a_vm_id_falls_back_to_position_and_name() {
        let a = vm("tududi", "red");
        let b = vm("honcho", "green");
        let site = |vms: Vec<Vm>| Inventory {
            sites: vec![Site {
                id: "dc1".into(),
                clusters: vec![Cluster {
                    name: "pve-prod".into(),
                    vms,
                    ..Cluster::default()
                }],
                ..Site::default()
            }],
            ..Inventory::default()
        };
        let picked = vm_pick("dc1", "pve-prod", "tududi", 0);
        assert!(resolves(&site(vec![a.clone(), b.clone()]), &picked));
        assert!(
            !resolves(&site(vec![b, a]), &picked),
            "with no VM ID, a reorder moves the position to another name, so the pick does not resolve"
        );
    }

    #[test]
    fn services_with_no_tree_and_no_inventory_are_loading_not_empty() {
        let rows = vec![service("tududi", "dc1")];
        let scope = Scope {
            org: "ops".into(),
            ..Scope::default()
        };
        assert_eq!(
            services_in_scope(&rows, &scope, None, &Loaded::Loading),
            ScopedServices::Loading,
            "the scope cannot be checked yet, so the page says so"
        );
        assert_eq!(
            services_in_scope(&rows, &scope, None, &Loaded::Failed),
            ScopedServices::Unresolved,
            "a failed tree is a notice, never an empty list"
        );
    }

    #[test]
    fn an_empty_or_site_scope_does_not_wait_for_the_tree() {
        let rows = vec![service("tududi", "dc1"), service("scratch", "dc2")];
        assert_eq!(
            services_in_scope(&rows, &Scope::default(), None, &Loaded::Loading),
            ScopedServices::Rows(rows.clone()),
            "an empty scope narrows nothing"
        );
        let by_site = Scope {
            site: Some("dc1".into()),
            ..Scope::default()
        };
        assert_eq!(
            services_in_scope(&rows, &by_site, None, &Loaded::Loading),
            ScopedServices::Rows(vec![service("tududi", "dc1")]),
            "a site is compared by id, so it needs no tree"
        );
    }

    #[test]
    fn an_env_scope_waits_for_the_tree_because_only_the_tree_has_envs() {
        let rows = vec![service("tududi", "dc1")];
        let prod = Scope {
            env: Some("prod".into()),
            ..Scope::default()
        };
        assert_eq!(
            services_in_scope(&rows, &prod, Some(&inventory()), &Loaded::Loading),
            ScopedServices::Loading
        );
        assert_eq!(
            services_in_scope(&rows, &prod, Some(&inventory()), &tree_loaded()),
            ScopedServices::Rows(rows.clone())
        );
    }

    #[test]
    fn the_inventory_org_is_read_before_the_tree_org() {
        // The inventory says dc1 is in "labs"; the tree says "ops". The inventory is the
        // answer the services came from, so it wins.
        let mut inv = inventory();
        inv.sites[0].org = "labs".into();
        let rows = vec![service("tududi", "dc1")];
        let labs = Scope {
            org: "labs".into(),
            ..Scope::default()
        };
        assert_eq!(
            services_in_scope(&rows, &labs, Some(&inv), &tree_loaded()),
            ScopedServices::Rows(rows.clone()),
            "the inventory's org matches"
        );
        let ops = Scope {
            org: "ops".into(),
            ..Scope::default()
        };
        assert_eq!(
            services_in_scope(&rows, &ops, Some(&inv), &tree_loaded()),
            ScopedServices::Rows(vec![]),
            "the tree's org is not used once the inventory has loaded"
        );
    }

    #[test]
    fn the_inventory_alone_resolves_an_org_scope_while_the_tree_loads() {
        let rows = vec![service("tududi", "dc1")];
        let ops = Scope {
            org: "ops".into(),
            ..Scope::default()
        };
        assert_eq!(
            services_in_scope(&rows, &ops, Some(&inventory()), &Loaded::Loading),
            ScopedServices::Rows(rows.clone())
        );
    }

    #[test]
    fn service_org_prefers_the_inventory_site() {
        let t = tree();
        let mut inv = inventory();
        inv.sites[0].org = "labs".into();
        assert_eq!(service_org("dc1", Some(&inv), Some(&t)), Some("labs"));
        assert_eq!(
            service_org("dc1", None, Some(&t)),
            Some("ops"),
            "the tree when no inventory"
        );
        assert_eq!(
            service_org("dc9", Some(&inv), Some(&t)),
            None,
            "neither names it"
        );
    }
}
