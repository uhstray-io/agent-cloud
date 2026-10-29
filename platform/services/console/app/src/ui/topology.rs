//! Topology: the compute tree (sites, clusters, nodes, VMs), the resource panel
//! for what is selected, and two admin-only tabs (network prefixes, data flows).
//!
//! Click selects one row. Shift-click adds or removes a row, and the panel then
//! shows the count and the worst RAG of the selection. Every row is a NetBox or
//! Proxmox fact the inventory answer carries; nothing is drawn from console-local
//! data.
//!
//! The API carries no VM-to-node placement, so a cluster lists its nodes and its
//! VMs as two lists, not a VM under its node.

use dioxus::prelude::*;

use super::empty::TOPOLOGY;
use super::icons::icon;
use super::inventory::{
    after_click, bytes_text, count_text, cpu_percent, live_picks, mb_text, mem_percent, meta_of,
    pick_rags, poll_admin, selection_rag, uptime_text, view, Admin, Kind, Pick, Snapshot, View,
    ViewNotice,
};
use super::inventory_api::{
    self, Cluster, Device, Flow, FlowsBody, Inventory, NetworkBody, Node, Site, SourceStatus, Vm,
    Workflow,
};
use super::shell::Ctx;
use super::status::{
    step_rows, unconfigured_hint, unconfigured_notice, worst, Freshness, Meter, Notice, Rag,
    RagBadge, StepList,
};
use super::Route;

/// The three views inside Topology.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TopoTab {
    Compute,
    Network,
    Flows,
}

impl TopoTab {
    pub fn label(self) -> &'static str {
        match self {
            TopoTab::Compute => "Compute",
            TopoTab::Network => "Network",
            TopoTab::Flows => "Data flows",
        }
    }
}

/// Everything the resource panel shows for one pick, computed from the inventory.
#[derive(Debug, Clone, PartialEq)]
pub struct Detail {
    pub kind_label: &'static str,
    pub title: String,
    pub rag: Option<Rag>,
    pub rows: Vec<(&'static str, String)>,
    /// Usage levels, drawn as bars. Only values the API sent.
    pub bars: Vec<(&'static str, f64)>,
    pub workflow: Option<Workflow>,
    pub grafana_url: Option<String>,
}

/// The cluster a pick names, within its site.
fn cluster_of<'a>(site: &'a Site, pick: &Pick) -> Option<&'a Cluster> {
    let name = pick.cluster.as_deref()?;
    site.clusters.iter().find(|c| c.name == name)
}

/// Adds a row only when the API sent the value. An absent value is not shown as zero.
fn push_some(rows: &mut Vec<(&'static str, String)>, label: &'static str, value: Option<String>) {
    if let Some(value) = value {
        rows.push((label, value));
    }
}

/// The pick for a cluster row.
fn cluster_pick(site_id: &str, cluster_name: &str) -> Pick {
    Pick {
        kind: Kind::Cluster,
        site: site_id.to_string(),
        cluster: Some(cluster_name.to_string()),
        name: cluster_name.to_string(),
    }
}

/// A cluster's rollup: the worst of its VMs. The same rule `pick_rags` applies to a cluster pick.
fn cluster_rag(cluster: &Cluster) -> Rag {
    worst(cluster.vms.iter().map(|vm| Rag::from_wire(&vm.rag)))
}

/// The pick for a site row.
fn site_pick(site_id: &str) -> Pick {
    Pick {
        kind: Kind::Site,
        site: site_id.to_string(),
        cluster: None,
        name: String::new(),
    }
}

/// The panel's content for one pick, or `None` when the pick is no longer in the inventory.
pub fn detail(inv: &Inventory, pick: &Pick) -> Option<Detail> {
    let site = inv.sites.iter().find(|s| s.id == pick.site)?;
    match pick.kind {
        Kind::Site => Some(site_detail(inv, site, pick)),
        Kind::Cluster => {
            let cluster = cluster_of(site, pick)?;
            Some(Detail {
                kind_label: "Cluster",
                title: cluster.name.clone(),
                rag: Some(worst(pick_rags(inv, pick))),
                rows: vec![
                    ("Site", site.name.clone()),
                    ("Nodes", count_text(cluster.nodes.len(), "node", "nodes")),
                    ("VMs", count_text(cluster.vms.len(), "VM", "VMs")),
                    ("Status basis", "Worst of its VMs".to_string()),
                ],
                bars: Vec::new(),
                workflow: None,
                grafana_url: None,
            })
        }
        Kind::Node => {
            let cluster = cluster_of(site, pick)?;
            let node = cluster.nodes.iter().find(|n| n.name == pick.name)?;
            Some(node_detail(node, &site.name, &cluster.name))
        }
        Kind::Vm => {
            let cluster = cluster_of(site, pick)?;
            let vm = cluster.vms.iter().find(|v| v.name == pick.name)?;
            Some(vm_detail(vm, &site.name, &cluster.name))
        }
    }
}

/// A site's panel. Its RAG is the worst of its VMs.
fn site_detail(inv: &Inventory, site: &Site, pick: &Pick) -> Detail {
    let vm_total: usize = site.clusters.iter().map(|c| c.vms.len()).sum();
    Detail {
        kind_label: "Site",
        title: site.name.clone(),
        rag: Some(worst(pick_rags(inv, pick))),
        rows: vec![
            ("Org", site.org.clone()),
            (
                "NetBox site",
                site.netbox_site
                    .clone()
                    .unwrap_or_else(|| "\u{2014}".to_string()),
            ),
            ("Mapped", if site.mapped { "Yes" } else { "No" }.to_string()),
            (
                "Clusters",
                count_text(site.clusters.len(), "cluster", "clusters"),
            ),
            ("VMs", count_text(vm_total, "VM", "VMs")),
            ("Status basis", "Worst of its VMs".to_string()),
            (
                "Devices",
                count_text(site.devices.len(), "device", "devices"),
            ),
        ],
        bars: Vec::new(),
        workflow: None,
        grafana_url: None,
    }
}

/// A node's panel. The API sends its usage only to platform admins, so each usage
/// row and each bar is added only when its value is present.
fn node_detail(node: &Node, site: &str, cluster: &str) -> Detail {
    let mut rows = vec![("Site", site.to_string()), ("Cluster", cluster.to_string())];
    push_some(&mut rows, "Status", node.status.clone());
    push_some(
        &mut rows,
        "Cores",
        node.maxcpu.map(|cores| format!("{cores:.0}")),
    );
    if let (Some(mem), Some(maxmem)) = (node.mem, node.maxmem) {
        rows.push((
            "Memory",
            format!("{} of {}", bytes_text(mem), bytes_text(maxmem)),
        ));
    }
    push_some(&mut rows, "Uptime", node.uptime.map(uptime_text));
    let mut bars = Vec::new();
    if let Some(cpu) = node.cpu {
        bars.push(("CPU", cpu_percent(cpu)));
    }
    if let (Some(mem), Some(maxmem)) = (node.mem, node.maxmem) {
        if let Some(pct) = mem_percent(mem, maxmem) {
            bars.push(("Memory", pct));
        }
    }
    Detail {
        kind_label: "Node",
        title: node.name.clone(),
        rag: None,
        rows,
        bars,
        workflow: None,
        grafana_url: None,
    }
}

/// A VM's panel. A null live status reads as "No live state", and its RAG is the
/// server's word whatever the live state is.
fn vm_detail(vm: &Vm, site: &str, cluster: &str) -> Detail {
    let mut rows = vec![("Site", site.to_string()), ("Cluster", cluster.to_string())];
    push_some(&mut rows, "VM ID", vm.vmid.map(|id| id.to_string()));
    rows.push(("NetBox status", vm.status.clone()));
    rows.push((
        "Live status",
        vm.live_status
            .clone()
            .unwrap_or_else(|| "No live state".to_string()),
    ));
    push_some(&mut rows, "vCPUs", vm.vcpus.map(|n| format!("{n:.0}")));
    push_some(&mut rows, "Memory", vm.memory_mb.map(mb_text));
    push_some(&mut rows, "Disk", vm.disk_gb.map(|gb| format!("{gb} GB")));
    push_some(&mut rows, "Primary IP", vm.primary_ip.clone());
    if !vm.tags.is_empty() {
        rows.push(("Tags", vm.tags.join(", ")));
    }
    Detail {
        kind_label: "VM",
        title: vm.name.clone(),
        rag: Some(Rag::from_wire(&vm.rag)),
        rows,
        bars: Vec::new(),
        workflow: vm.workflow.clone(),
        grafana_url: vm.grafana_url.clone(),
    }
}

/// One device's line in the tree: its name, role and status, those NetBox holds.
pub fn device_text(device: &Device) -> String {
    let mut parts = vec![device
        .name
        .clone()
        .unwrap_or_else(|| "Unnamed device".to_string())];
    parts.extend(device.role.clone());
    parts.extend(device.status.clone());
    parts.join(" \u{00b7} ")
}

/// The flows grouped by kind, in the order each kind first appears in the catalogue.
pub fn group_flows(flows: &[Flow]) -> Vec<(String, Vec<Flow>)> {
    let mut groups: Vec<(String, Vec<Flow>)> = Vec::new();
    for flow in flows {
        match groups.iter_mut().find(|(kind, _)| *kind == flow.kind) {
            Some((_, members)) => members.push(flow.clone()),
            None => groups.push((flow.kind.clone(), vec![flow.clone()])),
        }
    }
    groups
}

/// The name a pick shows as in a chip: a site's id, else the row's own name.
fn pick_name(pick: &Pick) -> String {
    match pick.kind {
        Kind::Site => pick.site.clone(),
        _ => pick.name.clone(),
    }
}

/// The Topology page. The tenant's snapshot feeds the compute tab. The network and
/// flows tabs read their own admin-only answers, and polling stops for a non-admin.
#[component]
pub fn Topology() -> Element {
    let ctx = use_context::<Ctx>();
    let tab = use_signal(|| TopoTab::Compute);
    let network = use_signal(|| Admin::<NetworkBody>::Loading);
    let flows = use_signal(|| Admin::<FlowsBody>::Loading);
    use_hook(move || {
        spawn(poll_admin(inventory_api::NETWORK_PATH.to_string(), network));
        spawn(poll_admin(inventory_api::FLOWS_PATH.to_string(), flows));
    });

    let state = ctx.inventory.read().clone();
    let tenant = ctx.tenant.read().clone();
    let shown = match view(&state, tenant.as_deref()) {
        View::Ready(snap) => rsx! {
            TopologyBody { snap: *snap, tab, network, flows }
        },
        other => rsx! { ViewNotice { view: other } },
    };

    rsx! {
        div { class: "view topo",
            header { class: "view-h",
                h1 { {icon(TOPOLOGY.icon)} "{TOPOLOGY.title}" }
                span { class: "authority", "Source of truth: {TOPOLOGY.authority}" }
                p { "{TOPOLOGY.purpose}" }
            }
            {shown}
        }
    }
}

/// The shown tenant's topology. Its selection lives here, so a tenant switch (which
/// unmounts this body while the new inventory loads) clears it.
#[component]
fn TopologyBody(
    snap: Snapshot,
    tab: Signal<TopoTab>,
    network: Signal<Admin<NetworkBody>>,
    flows: Signal<Admin<FlowsBody>>,
) -> Element {
    let picks = use_signal(Vec::<Pick>::new);
    let mut tab = tab;
    let current = tab();
    let meta = meta_of(&snap).cloned().unwrap_or_default();
    let reason = snap.reason.clone().unwrap_or_default();
    let body = match current {
        TopoTab::Compute => match snap.inventory.clone() {
            Some(inv) => rsx! {
                div { class: "topo-layout",
                    ComputeTree { inventory: inv.clone(), picks }
                    ResourcePanel { inventory: inv, picks }
                }
            },
            None => rsx! {
                Notice { message: "The inventory could not be loaded, so the compute tree is not shown.".to_string() }
            },
        },
        TopoTab::Network => rsx! { NetworkPanel { state: network } },
        TopoTab::Flows => rsx! { FlowsPanel { state: flows } },
    };
    let unconfigured = unconfigured_notice(&meta);
    rsx! {
        Freshness { meta, reason }
        if let Some(message) = unconfigured {
            Notice { message }
        }
        div { class: "seg", role: "group", aria_label: "Topology view",
            for option in [TopoTab::Compute, TopoTab::Network, TopoTab::Flows] {
                button {
                    r#type: "button",
                    aria_pressed: "{current == option}",
                    onclick: move |_| tab.set(option),
                    {option.label()}
                }
            }
        }
        {body}
    }
}

/// The compute tree: every site, its devices, and each cluster with its nodes and VMs.
#[component]
fn ComputeTree(inventory: Inventory, picks: Signal<Vec<Pick>>) -> Element {
    rsx! {
        section { class: "card tree", aria_label: "Compute tree",
            if inventory.sites.is_empty() {
                p { class: "faint", "This tenant has no sites in the inventory." }
            }
            for site in inventory.sites.iter() {
                SiteBranch { site: site.clone(), rag: worst(pick_rags(&inventory, &site_pick(&site.id))), picks }
            }
        }
    }
}

/// One site: its row, its devices, and its clusters. The RAG is the site's rollup.
#[component]
fn SiteBranch(site: Site, rag: Rag, picks: Signal<Vec<Pick>>) -> Element {
    let mapping = if site.mapped {
        "mapped to NetBox"
    } else {
        "not mapped"
    };
    let meta = format!("{} \u{00b7} {}", site.org, mapping);
    let devices: Vec<String> = site.devices.iter().map(device_text).collect();
    let site_id = site.id.clone();
    rsx! {
        div { class: "tree-site",
            TreeRow { pick: site_pick(&site_id), label: site.name.clone(), meta, rag: Some(rag), rollup: true, picks }
            if !site.mapped {
                p { class: "tree-note", "No NetBox site matches this site, or NetBox is not read, so it has no clusters or devices." }
            }
            if !devices.is_empty() {
                ul { class: "tree-leaves", aria_label: "Devices",
                    for (index, text) in devices.iter().enumerate() {
                        li { class: "tree-meta", key: "{index}", "{text}" }
                    }
                }
            }
            for cluster in site.clusters.iter() {
                ClusterBranch {
                    site_id: site_id.clone(),
                    cluster: cluster.clone(),
                    rag: cluster_rag(cluster),
                    picks,
                }
            }
        }
    }
}

/// One cluster, with its node list and its VM list. A VM is not placed under a
/// node, because the API does not say which node runs it.
#[component]
fn ClusterBranch(site_id: String, cluster: Cluster, rag: Rag, picks: Signal<Vec<Pick>>) -> Element {
    let meta = format!(
        "{} \u{00b7} {}",
        count_text(cluster.nodes.len(), "node", "nodes"),
        count_text(cluster.vms.len(), "VM", "VMs")
    );
    let pick = cluster_pick(&site_id, &cluster.name);
    rsx! {
        div { class: "tree-cluster",
            TreeRow { pick, label: cluster.name.clone(), meta, rag: Some(rag), rollup: true, picks }
            if !cluster.nodes.is_empty() {
                p { class: "tree-meta", "Nodes" }
                ul { class: "tree-leaves", aria_label: "Nodes of {cluster.name}",
                    for node in cluster.nodes.iter() {
                        li { key: "{node.name}",
                            TreeRow {
                                pick: Pick { kind: Kind::Node, site: site_id.clone(), cluster: Some(cluster.name.clone()), name: node.name.clone() },
                                label: node.name.clone(),
                                meta: node.status.clone().unwrap_or_else(|| "no status".to_string()),
                                rag: None,
                                picks,
                            }
                        }
                    }
                }
            }
            if !cluster.vms.is_empty() {
                p { class: "tree-meta", "VMs" }
                ul { class: "tree-leaves", aria_label: "VMs of {cluster.name}",
                    for vm in cluster.vms.iter() {
                        li { key: "{vm.name}",
                            TreeRow {
                                pick: Pick { kind: Kind::Vm, site: site_id.clone(), cluster: Some(cluster.name.clone()), name: vm.name.clone() },
                                label: vm.name.clone(),
                                meta: vm_meta(vm),
                                rag: Some(Rag::from_wire(&vm.rag)),
                                picks,
                            }
                        }
                    }
                }
            }
        }
    }
}

/// A VM's tree line: the NetBox status, then the live status (or that there is none).
fn vm_meta(vm: &Vm) -> String {
    let live = vm
        .live_status
        .clone()
        .unwrap_or_else(|| "no live state".to_string());
    format!("{} \u{00b7} {}", vm.status, live)
}

/// A selectable row. A plain click selects it alone. A shift-click toggles it in the selection.
#[component]
fn TreeRow(
    pick: Pick,
    label: String,
    meta: String,
    rag: Option<Rag>,
    /// True on a site or cluster: its RAG is the worst of its VMs, and the row says so.
    #[props(default)]
    rollup: bool,
    picks: Signal<Vec<Pick>>,
) -> Element {
    let mut picks = picks;
    let selected = picks.read().contains(&pick);
    let clicked = pick.clone();
    rsx! {
        button {
            r#type: "button",
            class: "tree-row",
            aria_pressed: "{selected}",
            onclick: move |evt: MouseEvent| {
                let next = after_click(&picks.read(), clicked.clone(), evt.modifiers().shift());
                picks.set(next);
            },
            span { class: "grow",
                b { "{label}" }
                span { class: "tree-meta", " {meta}" }
            }
            if rollup {
                span { class: "faint", "worst VM" }
            }
            if let Some(rag) = rag {
                RagBadge { rag }
            }
        }
    }
}

/// The resource panel. One selection shows its detail. Several show the count, the
/// worst RAG across them, and a clear button.
#[component]
fn ResourcePanel(inventory: Inventory, picks: Signal<Vec<Pick>>) -> Element {
    let mut picks = picks;
    let current = live_picks(&inventory, &picks.read());
    let body = match current.as_slice() {
        [] => rsx! {
            section { class: "card",
                div { class: "card-b faint",
                    "Select a site, cluster, node or VM to see its details. Shift-click to select several."
                }
            }
        },
        [only] => match detail(&inventory, only) {
            Some(d) => rsx! { DetailCard { detail: d } },
            None => rsx! {
                Notice { message: "That row is no longer in the inventory.".to_string() }
            },
        },
        many => {
            let rag = selection_rag(&inventory, many);
            let title = format!("{} selected", many.len());
            let names: Vec<String> = many.iter().map(pick_name).collect();
            rsx! {
                section { class: "card",
                    div { class: "card-h",
                        h2 { "{title}" }
                        RagBadge { rag }
                    }
                    div { class: "card-b stack",
                        div { class: "chips",
                            for (index, name) in names.iter().enumerate() {
                                span { class: "tag", key: "{index}", "{name}" }
                            }
                        }
                        p { class: "hint", "The status is the worst of the selected VMs. Nodes carry no status in the inventory, so they add nothing to it." }
                        button {
                            r#type: "button",
                            class: "btn",
                            onclick: move |_| picks.set(Vec::new()),
                            {icon("x")}
                            "Clear selection"
                        }
                    }
                }
            }
        }
    };
    rsx! {
        aside { class: "details", aria_label: "Resource panel",
            {body}
        }
    }
}

/// One pick's details, as a card.
#[component]
fn DetailCard(detail: Detail) -> Element {
    let kind = detail.kind_label;
    let workflow = detail.workflow.clone();
    let steps = workflow.as_ref().map(step_rows).unwrap_or_default();
    let failed = workflow
        .as_ref()
        .map(|wf| wf.failed.join(", "))
        .unwrap_or_default();
    let last_run = workflow
        .as_ref()
        .and_then(|wf| wf.last_run.clone())
        .unwrap_or_default();
    rsx! {
        section { class: "card",
            div { class: "card-h",
                span { class: "label", "{kind}" }
                h2 { "{detail.title}" }
                if let Some(rag) = detail.rag {
                    RagBadge { rag }
                }
            }
            div { class: "card-b stack",
                dl { class: "fields",
                    for (label, value) in detail.rows.iter() {
                        div { class: "field-row", key: "{label}",
                            dt { class: "label", "{label}" }
                            dd { "{value}" }
                        }
                    }
                }
                for (label, pct) in detail.bars.iter() {
                    Meter { label: label.to_string(), pct: *pct }
                }
                if workflow.is_some() {
                    div { class: "stack",
                        p { class: "label", "Workflow steps" }
                        StepList { steps }
                        if !failed.is_empty() {
                            p { class: "hint", "Failed: {failed}" }
                        }
                        if !last_run.is_empty() {
                            p { class: "hint", "Last run {last_run}" }
                        }
                        Link { to: Route::ServiceDetail { name: detail.title.clone() }, class: "btn", "Service detail" }
                    }
                }
                if let Some(url) = detail.grafana_url.clone() {
                    a { class: "btn", href: "{url}", target: "_blank", rel: "noopener", "Open in Grafana" }
                }
            }
        }
    }
}

/// The network tab. Prefixes are admin-only. A non-admin sees the forbidden state.
#[component]
fn NetworkPanel(state: Signal<Admin<NetworkBody>>) -> Element {
    let shown = state.read().clone();
    match shown {
        Admin::Loading => rsx! { Notice { message: "Loading network\u{2026}".to_string() } },
        Admin::Forbidden => rsx! {
            Notice { message: "Network is limited to platform administrators.".to_string() }
        },
        Admin::Unavailable(reason) => rsx! {
            Notice { message: format!("The network could not be loaded: {reason}. It retries every 30 seconds while this tab is visible.") }
        },
        Admin::Shown { body, reason } => {
            let reason = reason.unwrap_or_default();
            let rows = body.prefixes.clone();
            let empty = rows.is_empty();
            let unconfigured = unconfigured_notice(&body.meta);
            rsx! {
                Freshness { meta: body.meta.clone(), reason }
                if let Some(message) = unconfigured {
                    Notice { message }
                }
                if body.truncated.unwrap_or(false) {
                    p { class: "stale", role: "status", "The prefix list is truncated. It is not the whole of NetBox." }
                }
                section { class: "card",
                    div { class: "tbl-wrap",
                        table { class: "tbl",
                            thead {
                                tr {
                                    th { "Prefix" }
                                    th { "VLAN" }
                                    th { "Description" }
                                    th { "Scope" }
                                }
                            }
                            tbody {
                                for prefix in rows.iter() {
                                    tr { key: "{prefix.prefix}",
                                        td { class: "mono", "{prefix.prefix}" }
                                        td { class: "num", {prefix.vlan.map_or("\u{2014}".to_string(), |v| v.to_string())} }
                                        td { "{prefix.description}" }
                                        td { {prefix.site.clone().map_or("Global".to_string(), |s| format!("Site {s}"))} }
                                    }
                                }
                            }
                        }
                    }
                    if empty {
                        p { class: "empty", "NetBox holds no prefixes." }
                    }
                }
            }
        }
    }
}

/// The data-flows tab. Admin-only. Flows are grouped by kind, and each group is a list.
#[component]
fn FlowsPanel(state: Signal<Admin<FlowsBody>>) -> Element {
    let shown = state.read().clone();
    match shown {
        Admin::Loading => rsx! { Notice { message: "Loading data flows\u{2026}".to_string() } },
        Admin::Forbidden => rsx! {
            Notice { message: "Data flows are limited to platform administrators.".to_string() }
        },
        Admin::Unavailable(reason) => rsx! {
            Notice { message: format!("The data flows could not be loaded: {reason}. It retries every 30 seconds while this tab is visible.") }
        },
        Admin::Shown { body, reason } => {
            let reason = reason.unwrap_or_default();
            let groups: Vec<(String, String, Vec<Flow>)> = group_flows(&body.flows)
                .into_iter()
                .map(|(kind, members)| {
                    let counted = count_text(members.len(), "flow", "flows");
                    (kind, counted, members)
                })
                .collect();
            let notice: Option<String> = if body.source == SourceStatus::Unconfigured {
                Some(unconfigured_hint("flows").to_string())
            } else if groups.is_empty() {
                Some("No data flows are declared.".to_string())
            } else {
                None
            };
            rsx! {
                if !reason.is_empty() {
                    p { class: "stale", role: "status", "The last refresh failed: {reason}." }
                }
                if let Some(message) = notice {
                    Notice { message }
                }
                for (kind, counted, members) in groups.iter() {
                    section { class: "card", key: "{kind}",
                        div { class: "card-h",
                            h2 { class: "mono", "{kind}" }
                            span { class: "faint", "{counted}" }
                        }
                        div { class: "tbl-wrap",
                            table { class: "tbl",
                                thead { tr { th { "From" } th { "To" } th { "Label" } } }
                                tbody {
                                    for flow in members.iter() {
                                        tr { key: "{flow.from}-{flow.to}-{flow.label}",
                                            td { class: "mono", "{flow.from}" }
                                            td { class: "mono", "{flow.to}" }
                                            td { "{flow.label}" }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn vm(name: &str, rag: &str, live: Option<&str>) -> Vm {
        Vm {
            name: name.into(),
            status: "active".into(),
            live_status: live.map(str::to_string),
            rag: rag.into(),
            ..Vm::default()
        }
    }

    fn inventory() -> Inventory {
        let mut tududi = vm("tududi", "red", Some("running"));
        tududi.workflow = Some(Workflow {
            failed: vec!["step-b".into()],
            ..Workflow::default()
        });
        tududi.grafana_url = Some("https://grafana.example.test/d/service-overview".into());
        Inventory {
            sites: vec![
                Site {
                    id: "dc1".into(),
                    name: "DC 1".into(),
                    org: "ops".into(),
                    mapped: true,
                    devices: vec![Device {
                        name: Some("pve-01".into()),
                        role: Some("Hypervisor".into()),
                        status: Some("active".into()),
                    }],
                    clusters: vec![Cluster {
                        name: "pve-prod".into(),
                        nodes: vec![Node {
                            name: "pve-01".into(),
                            status: Some("online".into()),
                            cpu: Some(0.12),
                            maxcpu: Some(16.0),
                            mem: Some(8_589_934_592),
                            maxmem: Some(68_719_476_736),
                            uptime: Some(123_456),
                        }],
                        vms: vec![tududi, vm("honcho", "green", Some("running"))],
                    }],
                    ..Site::default()
                },
                Site {
                    id: "dc2".into(),
                    name: "DC 2".into(),
                    mapped: false,
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

    #[test]
    fn a_vm_detail_carries_its_rag_grafana_link_and_workflow() {
        let d = detail(
            &inventory(),
            &pick(Kind::Vm, "dc1", Some("pve-prod"), "tududi"),
        )
        .unwrap();
        assert_eq!(d.kind_label, "VM");
        assert_eq!(d.rag, Some(Rag::Red));
        assert!(d.workflow.is_some());
        assert_eq!(
            d.grafana_url.as_deref(),
            Some("https://grafana.example.test/d/service-overview")
        );
        assert!(d
            .rows
            .iter()
            .any(|(k, v)| *k == "Live status" && v == "running"));
    }

    #[test]
    fn a_vm_with_no_live_state_says_so_and_keeps_the_servers_rag() {
        let inv = Inventory {
            sites: vec![Site {
                id: "dc1".into(),
                clusters: vec![Cluster {
                    name: "pve-prod".into(),
                    vms: vec![vm("ambiguous", "amber", None)],
                    ..Cluster::default()
                }],
                ..Site::default()
            }],
            ..Inventory::default()
        };
        let d = detail(&inv, &pick(Kind::Vm, "dc1", Some("pve-prod"), "ambiguous")).unwrap();
        assert!(d
            .rows
            .iter()
            .any(|(k, v)| *k == "Live status" && v == "No live state"));
        assert_eq!(
            d.rag,
            Some(Rag::Amber),
            "the server's rag is not second-guessed"
        );
        assert_eq!(
            vm_meta(&inv.sites[0].clusters[0].vms[0]),
            "active \u{00b7} no live state"
        );
    }

    #[test]
    fn a_node_shows_usage_bars_only_when_the_api_sent_them() {
        let full = detail(
            &inventory(),
            &pick(Kind::Node, "dc1", Some("pve-prod"), "pve-01"),
        )
        .unwrap();
        assert_eq!(full.rag, None, "a node has no RAG");
        let cpu = full.bars.iter().find(|(l, _)| *l == "CPU").unwrap().1;
        assert!((cpu - 12.0).abs() < 1e-9);
        assert!(full.bars.iter().any(|(l, _)| *l == "Memory"));

        // A non-admin tenant answer carries only the name and the status.
        let mut inv = inventory();
        inv.sites[0].clusters[0].nodes[0] = Node {
            name: "pve-01".into(),
            status: Some("online".into()),
            ..Node::default()
        };
        let thin = detail(&inv, &pick(Kind::Node, "dc1", Some("pve-prod"), "pve-01")).unwrap();
        assert!(thin.bars.is_empty(), "absent usage draws no bar");
        assert!(
            !thin.rows.iter().any(|(k, _)| *k == "Uptime"),
            "absent usage is not shown as zero"
        );
        assert!(thin
            .rows
            .iter()
            .any(|(k, v)| *k == "Status" && v == "online"));
    }

    #[test]
    fn a_site_detail_counts_its_children_and_rolls_up_its_vms() {
        let d = detail(&inventory(), &pick(Kind::Site, "dc1", None, "")).unwrap();
        assert_eq!(d.rag, Some(Rag::Red), "the worst of its VMs");
        assert!(d.rows.iter().any(|(k, v)| *k == "VMs" && v == "2 VMs"));
        let unmapped = detail(&inventory(), &pick(Kind::Site, "dc2", None, "")).unwrap();
        assert_eq!(unmapped.rag, Some(Rag::Grey), "no VMs is no signal");
        assert!(unmapped
            .rows
            .iter()
            .any(|(k, v)| *k == "Mapped" && v == "No"));
    }

    #[test]
    fn a_pick_that_left_the_inventory_has_no_detail() {
        assert_eq!(
            detail(
                &inventory(),
                &pick(Kind::Vm, "dc1", Some("pve-prod"), "gone")
            ),
            None
        );
        assert_eq!(
            detail(&inventory(), &pick(Kind::Site, "dc9", None, "")),
            None
        );
    }

    #[test]
    fn a_device_line_names_what_netbox_holds_and_nothing_else() {
        let full = Device {
            name: Some("pve-01".into()),
            role: Some("Hypervisor".into()),
            status: Some("active".into()),
        };
        assert_eq!(
            device_text(&full),
            "pve-01 \u{00b7} Hypervisor \u{00b7} active"
        );
        assert_eq!(device_text(&Device::default()), "Unnamed device");
    }

    #[test]
    fn flows_group_by_kind_in_first_seen_order() {
        let flow = |kind: &str, label: &str| Flow {
            kind: kind.into(),
            from: "a".into(),
            to: "b".into(),
            label: label.into(),
        };
        let groups = group_flows(&[
            flow("oidc", "sign-in"),
            flow("edge", "https"),
            flow("oidc", "logout"),
            flow("metrics", "scrape"),
        ]);
        let kinds: Vec<&str> = groups.iter().map(|(k, _)| k.as_str()).collect();
        assert_eq!(kinds, ["oidc", "edge", "metrics"]);
        assert_eq!(groups[0].1.len(), 2);
        assert!(group_flows(&[]).is_empty());
    }

    #[test]
    fn the_tabs_have_the_brief_labels() {
        assert_eq!(TopoTab::Compute.label(), "Compute");
        assert_eq!(TopoTab::Network.label(), "Network");
        assert_eq!(TopoTab::Flows.label(), "Data flows");
    }

    #[test]
    fn a_site_chip_shows_the_site_and_other_rows_their_name() {
        assert_eq!(pick_name(&pick(Kind::Site, "dc1", None, "")), "dc1");
        assert_eq!(
            pick_name(&pick(Kind::Vm, "dc1", Some("pve-prod"), "tududi")),
            "tududi"
        );
    }
}
