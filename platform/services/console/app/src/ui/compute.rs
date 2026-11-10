//! Compute: the node and VM levels of the selected tenant, as two tables, narrowed
//! to the scope bar.
//!
//! Node usage comes from the live Proxmox fields. The API sends those to platform
//! admins only, so a tenant member sees a node's name and status, and a dash where
//! the usage would be. A usage bar is a level indicator, not a chart: no history,
//! no axis, and no threshold the console invents.
//!
//! The VM table carries NetBox's allocation (vCPUs, memory, disk). The inventory
//! answer has no live usage per VM, so none is shown.

use dioxus::prelude::*;

use super::empty::COMPUTE;
use super::icons::icon;
use super::inventory::{
    bytes_text, count_text, cpu_percent, loaded_tree, mb_text, mem_percent, meta_of, node_key,
    scope_gate, scoped_inventory, uptime_text, view, vm_key, ScopeGate, Snapshot, View, ViewNotice,
};
use super::inventory_api::{Inventory, Vm};
use super::shell::Ctx;
use super::status::{unconfigured_notice, Freshness, Meter, Notice, Rag, RagBadge, DASH};

/// One node row. Usage fields are `None` when the API did not send them.
#[derive(Debug, Clone, PartialEq)]
pub struct NodeRow {
    /// Unique across the tree, so two nodes of one name still key apart.
    pub key: String,
    pub site: String,
    pub cluster: String,
    pub name: String,
    pub status: String,
    pub cores: Option<f64>,
    pub cpu_pct: Option<f64>,
    pub mem_pct: Option<f64>,
    /// "used of total" in sizes, present with `mem_pct`.
    pub mem_text: Option<String>,
    pub uptime: Option<String>,
}

/// One VM row, with NetBox's allocation and the server's RAG.
#[derive(Debug, Clone, PartialEq)]
pub struct VmRow {
    /// Unique across the tree: two VMs of one name in one cluster key apart by position.
    pub key: String,
    pub site: String,
    pub cluster: String,
    pub name: String,
    pub vmid: Option<u64>,
    pub netbox_status: String,
    pub live: String,
    pub vcpus: Option<String>,
    pub memory: Option<String>,
    pub disk: Option<String>,
    pub rag: Rag,
    pub grafana_url: Option<String>,
}

/// Every node of every site, in site and cluster order.
pub fn node_rows(inv: &Inventory) -> Vec<NodeRow> {
    let mut rows = Vec::new();
    for site in &inv.sites {
        for cluster in &site.clusters {
            for (ordinal, node) in cluster.nodes.iter().enumerate() {
                let memory = match (node.mem, node.maxmem) {
                    (Some(mem), Some(maxmem)) => Some((mem, maxmem)),
                    _ => None,
                };
                rows.push(NodeRow {
                    key: node_key(&site.id, &cluster.name, &node.name, ordinal),
                    site: site.id.clone(),
                    cluster: cluster.name.clone(),
                    name: node.name.clone(),
                    status: node
                        .status
                        .clone()
                        .unwrap_or_else(|| "no status".to_string()),
                    cores: node.maxcpu,
                    cpu_pct: node.cpu.map(cpu_percent),
                    mem_pct: memory.and_then(|(mem, maxmem)| mem_percent(mem, maxmem)),
                    mem_text: memory.map(|(mem, maxmem)| {
                        format!("{} of {}", bytes_text(mem), bytes_text(maxmem))
                    }),
                    uptime: node.uptime.map(uptime_text),
                });
            }
        }
    }
    rows
}

/// Every VM of every site, in site and cluster order.
pub fn vm_rows(inv: &Inventory) -> Vec<VmRow> {
    let mut rows = Vec::new();
    for site in &inv.sites {
        for cluster in &site.clusters {
            for (ordinal, vm) in cluster.vms.iter().enumerate() {
                rows.push(vm_row(&site.id, &cluster.name, vm, ordinal));
            }
        }
    }
    rows
}

fn vm_row(site: &str, cluster: &str, vm: &Vm, ordinal: usize) -> VmRow {
    VmRow {
        key: vm_key(site, cluster, vm, ordinal),
        site: site.to_string(),
        cluster: cluster.to_string(),
        name: vm.name.clone(),
        vmid: vm.vmid,
        netbox_status: vm.status.clone(),
        live: vm
            .live_status
            .clone()
            .unwrap_or_else(|| "No live state".to_string()),
        vcpus: vm.vcpus.map(|n| format!("{n:.0}")),
        memory: vm.memory_mb.map(mb_text),
        disk: vm.disk_mb.map(mb_text),
        rag: Rag::from_wire(&vm.rag),
        grafana_url: vm.grafana_url.clone(),
    }
}

/// The Compute page.
#[component]
pub fn Compute() -> Element {
    let ctx = use_context::<Ctx>();
    let state = ctx.inventory.read().clone();
    let tenant = ctx.tenant.read().clone();
    let shown = match view(&state, tenant.as_deref()) {
        View::Ready(snap) => rsx! { ComputeBody { snap: *snap } },
        other => rsx! { ViewNotice { view: other } },
    };
    rsx! {
        div { class: "view compute",
            header { class: "view-h",
                h1 { {icon(COMPUTE.icon)} "{COMPUTE.title}" }
                span { class: "authority", "Source of truth: {COMPUTE.authority}" }
                p { "{COMPUTE.purpose}" }
            }
            {shown}
        }
    }
}

#[component]
fn ComputeBody(snap: Snapshot) -> Element {
    let ctx = use_context::<Ctx>();
    let meta = meta_of(&snap).cloned().unwrap_or_default();
    let reason = snap.reason.clone().unwrap_or_default();
    let Some(inv) = snap.inventory.clone() else {
        return rsx! {
            Freshness { meta, reason }
            Notice { message: "The inventory could not be loaded, so no capacity is shown.".to_string() }
        };
    };
    let scope = ctx.scope.read().clone();
    let tree_state = ctx.tree.read().clone();
    match scope_gate(&scope, Some(&inv), &tree_state) {
        ScopeGate::Loading => {
            return rsx! {
                Freshness { meta, reason }
                Notice { message: "Loading scope\u{2026}".to_string() }
            };
        }
        ScopeGate::Unresolved => {
            return rsx! {
                Freshness { meta, reason }
                Notice { message: "The scope could not be loaded for this tenant, so no capacity is shown.".to_string() }
            };
        }
        ScopeGate::Ready => {}
    }
    let tree = loaded_tree(&tree_state);
    let inv = scoped_inventory(&inv, &scope, tree.as_ref());
    let nodes = node_rows(&inv);
    let vms = vm_rows(&inv);
    let node_count = count_text(nodes.len(), "node", "nodes");
    let vm_count = count_text(vms.len(), "VM", "VMs");
    let unconfigured = unconfigured_notice(&meta);
    rsx! {
        Freshness { meta, reason }
        if let Some(message) = unconfigured {
            Notice { message }
        }
        section { class: "card",
            div { class: "card-h",
                h2 { "Nodes" }
                span { class: "faint", "{node_count}" }
            }
            div { class: "tbl-wrap",
                table { class: "tbl",
                    thead {
                        tr {
                            th { "Site" }
                            th { "Cluster" }
                            th { "Node" }
                            th { "Status" }
                            th { "Cores" }
                            th { "CPU" }
                            th { "Memory" }
                            th { "Uptime" }
                        }
                    }
                    tbody {
                        for row in nodes.iter() {
                            tr { key: "{row.key}",
                                td { "{row.site}" }
                                td { "{row.cluster}" }
                                td { class: "mono", "{row.name}" }
                                td { "{row.status}" }
                                td { class: "num", {row.cores.map_or(DASH.to_string(), |c| format!("{c:.0}"))} }
                                td {
                                    if let Some(pct) = row.cpu_pct {
                                        Meter { label: "CPU".to_string(), pct }
                                    } else {
                                        span { class: "faint", {DASH} }
                                    }
                                }
                                td {
                                    if let Some(pct) = row.mem_pct {
                                        Meter { label: "Memory".to_string(), pct }
                                    } else {
                                        span { class: "faint", {DASH} }
                                    }
                                    if let Some(text) = &row.mem_text {
                                        span { class: "faint", " {text}" }
                                    }
                                }
                                td { {row.uptime.clone().unwrap_or_else(|| DASH.to_string())} }
                            }
                        }
                    }
                }
            }
            if nodes.is_empty() {
                p { class: "empty", "No node is recorded for the sites in this scope." }
            }
            p { class: "hint", "Usage is shown to platform administrators only. Disk usage is not in the inventory answer." }
        }
        section { class: "card",
            div { class: "card-h",
                h2 { "VMs" }
                span { class: "faint", "{vm_count}" }
            }
            div { class: "tbl-wrap",
                table { class: "tbl",
                    thead {
                        tr {
                            th { "Site" }
                            th { "Cluster" }
                            th { "VM" }
                            th { "VM ID" }
                            th { "NetBox" }
                            th { "Live" }
                            th { "vCPUs" }
                            th { "Memory" }
                            th { "Disk" }
                            th { "Status" }
                            th { "Grafana" }
                        }
                    }
                    tbody {
                        for row in vms.iter() {
                            tr { key: "{row.key}",
                                td { "{row.site}" }
                                td { "{row.cluster}" }
                                td { class: "mono", "{row.name}" }
                                td { class: "num", {row.vmid.map_or(DASH.to_string(), |id| id.to_string())} }
                                td { "{row.netbox_status}" }
                                td { "{row.live}" }
                                td { class: "num", {row.vcpus.clone().unwrap_or_else(|| DASH.to_string())} }
                                td { class: "num", {row.memory.clone().unwrap_or_else(|| DASH.to_string())} }
                                td { class: "num", {row.disk.clone().unwrap_or_else(|| DASH.to_string())} }
                                td { RagBadge { rag: row.rag } }
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
                }
            }
            if vms.is_empty() {
                p { class: "empty", "No VM is recorded for the sites in this scope." }
            }
            p { class: "hint", "vCPUs, memory and disk are NetBox's allocation, not live usage." }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ui::inventory_api::{Cluster, Node, Site};

    fn inventory(node: Node) -> Inventory {
        Inventory {
            sites: vec![Site {
                id: "dc1".into(),
                mapped: true,
                clusters: vec![Cluster {
                    name: "pve-prod".into(),
                    nodes: vec![node],
                    vms: vec![Vm {
                        name: "tududi".into(),
                        status: "active".into(),
                        live_status: None,
                        vcpus: Some(2.0),
                        memory_mb: Some(4096),
                        disk_mb: Some(40_960),
                        rag: "amber".into(),
                        ..Vm::default()
                    }],
                }],
                ..Site::default()
            }],
            ..Inventory::default()
        }
    }

    #[test]
    fn an_admin_node_row_carries_its_usage_levels() {
        let rows = node_rows(&inventory(Node {
            name: "pve-01".into(),
            status: Some("online".into()),
            cpu: Some(0.12),
            maxcpu: Some(16.0),
            mem: Some(8_589_934_592),
            maxmem: Some(68_719_476_736),
            uptime: Some(123_456),
        }));
        let row = &rows[0];
        assert!((row.cpu_pct.unwrap() - 12.0).abs() < 1e-9);
        assert_eq!(row.mem_pct, Some(12.5));
        assert_eq!(row.mem_text.as_deref(), Some("8.0 GiB of 64.0 GiB"));
        assert_eq!(row.uptime.as_deref(), Some("1d 10h"));
    }

    #[test]
    fn a_tenant_node_row_has_no_levels_to_draw() {
        let rows = node_rows(&inventory(Node {
            name: "pve-01".into(),
            status: Some("online".into()),
            ..Node::default()
        }));
        let row = &rows[0];
        assert_eq!(row.status, "online", "the name and status are the tenant's");
        assert_eq!(row.cpu_pct, None, "no bar when the API sent no usage");
        assert_eq!(row.mem_pct, None);
        assert_eq!(row.mem_text, None);
        assert_eq!(row.uptime, None);
    }

    #[test]
    fn a_vm_with_no_live_state_reads_as_such_and_keeps_its_rag() {
        let rows = vm_rows(&inventory(Node::default()));
        let vm = &rows[0];
        assert_eq!(vm.live, "No live state");
        assert_eq!(vm.rag, Rag::Amber);
        assert_eq!(vm.vcpus.as_deref(), Some("2"));
        assert_eq!(vm.memory.as_deref(), Some("4.0 GiB"));
        assert_eq!(
            vm.disk.as_deref(),
            Some("40.0 GiB"),
            "40960 MB is 40 GiB, unit shown"
        );
        assert_eq!(vm.vmid, None);
    }

    #[test]
    fn two_vms_of_one_name_in_one_cluster_get_distinct_rows() {
        let first = Vm {
            name: "tududi".into(),
            vmid: Some(101),
            rag: "red".into(),
            ..Vm::default()
        };
        let second = Vm {
            vmid: Some(202),
            rag: "green".into(),
            ..first.clone()
        };
        let inv = Inventory {
            sites: vec![Site {
                id: "dc1".into(),
                clusters: vec![Cluster {
                    name: "pve-prod".into(),
                    vms: vec![first, second],
                    ..Cluster::default()
                }],
                ..Site::default()
            }],
            ..Inventory::default()
        };
        let rows = vm_rows(&inv);
        assert_eq!(rows.len(), 2);
        assert_ne!(rows[0].key, rows[1].key, "same name, distinct keys");
        assert_eq!(rows[0].rag, Rag::Red, "each row keeps its own RAG");
        assert_eq!(rows[1].rag, Rag::Green);
    }

    #[test]
    fn an_unmapped_site_contributes_no_rows() {
        let inv = Inventory {
            sites: vec![Site {
                id: "dc2".into(),
                mapped: false,
                ..Site::default()
            }],
            ..Inventory::default()
        };
        assert!(node_rows(&inv).is_empty());
        assert!(vm_rows(&inv).is_empty());
    }
}
