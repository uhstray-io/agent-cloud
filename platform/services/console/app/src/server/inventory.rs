//! Inventory: NetBox sites, clusters, VMs and devices, joined to Proxmox live
//! state by VM name, for the tenant the caller may see (plan 17 D22-D25, task 6.3).
//!
//! The refresh task reads both authorities every `REFRESH_INTERVAL` into `InventoryCache`.
//! Requests only read that cache, and the join is a pure function of the cache and the
//! scope, so one caller's view costs no upstream request. A failed refresh keeps the last
//! good data and marks the answer stale. Credentials never reach a response or a log line.
//!
//! `rag` is the one place a status colour is decided. Its rule is written there and tested
//! row by row.

use std::collections::BTreeSet;
use std::path::Path;
use std::time::Duration;

use dioxus::server::axum::extract::{Path as AxumPath, State};
use dioxus::server::axum::http::StatusCode;
use dioxus::server::axum::response::{IntoResponse, Response};
use dioxus::server::axum::Json;
use serde::{Deserialize, Serialize};
use tokio::time::MissedTickBehavior;
use tower_sessions::Session;

use super::api::{error, unauthenticated};
use super::auth;
use super::grafana;
use super::netbox::{self, NbCluster, NbDevice, NbPrefix, NbVm, NetboxData, Workflow};
use super::proxmox::{self, ProxmoxData, PveGuest, PveNode};
use super::scope::{Site, Tenant};
use super::work::{self, rfc3339_now, Source};
use super::{AppState, SharedState};

/// How often both authorities are read. Slower than Active Work's 10 s poll: the
/// inventory changes on a human timescale.
pub const REFRESH_INTERVAL: Duration = Duration::from_secs(60);

/// One authority's last read. `data` survives a failed refresh; `status` says whether
/// it is current.
pub struct SourceState<T> {
    pub status: Source,
    pub error: Option<String>,
    pub updated_at: Option<String>,
    pub data: Option<T>,
}

impl<T> SourceState<T> {
    /// No configuration for this authority.
    pub fn unconfigured() -> Self {
        Self::with_status(Source::Unconfigured)
    }

    /// Configured, first read not finished. Status `ok` with `updated_at: null`, as the
    /// work list answers before its first poll.
    pub fn awaiting_first_refresh() -> Self {
        Self::with_status(Source::Ok)
    }

    fn with_status(status: Source) -> Self {
        Self {
            status,
            error: None,
            updated_at: None,
            data: None,
        }
    }

    pub fn record_ok(&mut self, data: T, refreshed_at: String) {
        self.status = Source::Ok;
        self.error = None;
        self.updated_at = Some(refreshed_at);
        self.data = Some(data);
    }

    /// A failed read keeps the last good data. The status and `error` say it is stale.
    pub fn record_error(&mut self, error: String) {
        self.status = Source::Error;
        self.error = Some(error);
    }

    fn is_stale(&self) -> bool {
        self.status == Source::Error
    }
}

/// Both authorities as the refresh task leaves them.
pub struct InventoryCache {
    pub netbox: SourceState<NetboxData>,
    pub proxmox: SourceState<ProxmoxData>,
}

impl InventoryCache {
    pub fn new(netbox_configured: bool, proxmox_configured: bool) -> Self {
        Self {
            netbox: if netbox_configured {
                SourceState::awaiting_first_refresh()
            } else {
                SourceState::unconfigured()
            },
            proxmox: if proxmox_configured {
                SourceState::awaiting_first_refresh()
            } else {
                SourceState::unconfigured()
            },
        }
    }

    fn stale(&self) -> bool {
        self.netbox.is_stale() || self.proxmox.is_stale()
    }
}

/// Fields every inventory answer carries: each authority's state, its last error text,
/// its last successful read, and whether any answer is stale.
#[derive(Serialize)]
pub struct Meta {
    pub source: Sources,
    pub error: Errors,
    pub stale: bool,
    pub updated_at: Stamps,
    pub links: Links,
}

/// Deep links out to Grafana. Base-level, so it is safe in every envelope. Each link is
/// `null` when `CONSOLE_GRAFANA_URL` is unset.
#[derive(Serialize)]
pub struct Links {
    /// The Grafana origin the console was given.
    pub grafana: Option<String>,
    /// The service-deployment conformance dashboard under that origin.
    pub grafana_conformance: Option<String>,
}

fn links(grafana_base: Option<&str>) -> Links {
    Links {
        grafana: grafana_base.map(str::to_string),
        grafana_conformance: grafana_base.and_then(grafana::service_conformance),
    }
}

#[derive(Serialize)]
pub struct Sources {
    pub netbox: Source,
    pub proxmox: Source,
}

#[derive(Serialize)]
pub struct Errors {
    pub netbox: Option<String>,
    pub proxmox: Option<String>,
}

#[derive(Serialize)]
pub struct Stamps {
    pub netbox: Option<String>,
    pub proxmox: Option<String>,
}

fn meta(cache: &InventoryCache, grafana_base: Option<&str>) -> Meta {
    Meta {
        links: links(grafana_base),
        source: Sources {
            netbox: cache.netbox.status,
            proxmox: cache.proxmox.status,
        },
        error: Errors {
            netbox: cache.netbox.error.clone(),
            proxmox: cache.proxmox.error.clone(),
        },
        stale: cache.stale(),
        updated_at: Stamps {
            netbox: cache.netbox.updated_at.clone(),
            proxmox: cache.proxmox.updated_at.clone(),
        },
    }
}

/// `GET /v1/tenants/{t}/inventory`.
#[derive(Serialize)]
pub struct InventoryBody {
    #[serde(flatten)]
    pub meta: Meta,
    pub sites: Vec<SiteView>,
}

/// One scope site, joined. `mapped` is false when the site has no `netbox_site` or NetBox
/// holds no site with that slug; an unmapped site has no clusters or devices. Devices are
/// site-scoped. Prefixes are global, so they live only in `GET /v1/network`, admin-only.
#[derive(Serialize)]
pub struct SiteView {
    pub id: String,
    pub name: String,
    pub org: String,
    pub netbox_site: Option<String>,
    pub mapped: bool,
    pub clusters: Vec<ClusterView>,
    pub devices: Vec<NbDevice>,
}

#[derive(Serialize)]
pub struct ClusterView {
    pub name: String,
    /// The Proxmox nodes that host this cluster's matched VMs, by name. A node with no
    /// matched VM of the cluster is not listed.
    pub nodes: Vec<PveNode>,
    pub vms: Vec<VmView>,
}

#[derive(Clone, Serialize)]
pub struct VmView {
    pub name: String,
    pub vmid: Option<u64>,
    /// NetBox status.
    pub status: String,
    /// Proxmox status, or `null` when no unique guest of this name exists.
    pub live_status: Option<String>,
    pub vcpus: Option<f64>,
    pub memory_mb: Option<u64>,
    pub disk_gb: Option<u64>,
    /// The primary address NetBox holds, prefix length included.
    pub primary_ip: Option<String>,
    pub tags: Vec<String>,
    /// `null` when the VM carries no workflow fields.
    pub workflow: Option<Workflow>,
    pub rag: &'static str,
    /// The per-service Grafana dashboard for this VM's name, or `null` when
    /// `CONSOLE_GRAFANA_URL` is unset.
    pub grafana_url: Option<String>,
}

/// `GET /v1/tenants/{t}/services` row: a VM that carries workflow fields.
#[derive(Clone, Serialize)]
pub struct ServiceRow {
    pub name: String,
    /// Same link as the VM's `grafana_url`.
    pub grafana_url: Option<String>,
    /// The scope site id the VM sits under.
    pub site: String,
    pub cluster: String,
    pub live_status: Option<String>,
    pub rag: &'static str,
    /// Union of `ac_failed_steps` and every step whose status is `fail`, sorted.
    pub failed_steps: Vec<String>,
    pub last_run: Option<String>,
    pub tags: Vec<String>,
}

#[derive(Serialize)]
pub struct ServicesBody {
    #[serde(flatten)]
    pub meta: Meta,
    pub services: Vec<ServiceRow>,
}

/// `GET /v1/tenants/{t}/services/{name}`: one row and its VM as the inventory shows it.
#[derive(Serialize)]
pub struct ServiceBody {
    #[serde(flatten)]
    pub meta: Meta,
    pub service: ServiceRow,
    pub vm: VmView,
}

/// One data flow, rendered at deploy from inventory declarations (plan 17 D24). Names only:
/// `deny_unknown_fields` refuses an entry that carries an address or any other key.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Flow {
    pub kind: String,
    pub from: String,
    pub to: String,
    pub label: String,
}

/// The flow catalogue `GET /v1/flows` answers. `unconfigured` when no file is set.
pub struct FlowSet {
    pub source: Source,
    pub flows: Vec<Flow>,
}

impl FlowSet {
    pub fn unconfigured() -> Self {
        FlowSet {
            source: Source::Unconfigured,
            flows: Vec::new(),
        }
    }

    pub fn configured(flows: Vec<Flow>) -> Self {
        FlowSet {
            source: Source::Ok,
            flows,
        }
    }
}

#[derive(Serialize)]
pub struct FlowsBody {
    pub source: Source,
    pub flows: Vec<Flow>,
    pub links: Links,
}

/// Reads `CONSOLE_FLOWS_FILE`. A configured file that cannot be read or parsed fails startup.
pub fn load_flows(path: &Path) -> Result<Vec<Flow>, String> {
    let text = std::fs::read_to_string(path)
        .map_err(|err| format!("CONSOLE_FLOWS_FILE: cannot read {}: {err}", path.display()))?;
    serde_json::from_str(&text).map_err(|err| format!("CONSOLE_FLOWS_FILE: invalid JSON: {err}"))
}

/// What the RAG rule reads. `live_status` is `None` when no live state is known.
pub struct RagInput<'a> {
    pub netbox_status: &'a str,
    pub live_status: Option<&'a str>,
    pub failed: bool,
    pub pending: bool,
    pub stale: bool,
}

/// The RAG rule. The first matching colour wins:
///
/// - red: a workflow step failed, or Proxmox says `stopped` while NetBox says `active`.
/// - amber: the answer is stale, NetBox says `planned` or `staged`, a step is pending or
///   unknown, or Proxmox reports a state that is neither `running` nor (above) stopped.
/// - grey: no live state (Proxmox unconfigured, never read, or no unique name match).
/// - green: Proxmox says `running`, and nothing above applies.
pub fn rag(input: &RagInput) -> &'static str {
    if input.failed || (input.live_status == Some("stopped") && input.netbox_status == "active") {
        return "red";
    }
    if input.stale || matches!(input.netbox_status, "planned" | "staged") || input.pending {
        return "amber";
    }
    match input.live_status {
        None => "grey",
        Some("running") => "green",
        Some(_) => "amber",
    }
}

/// Step statuses that are settled. `skip` is a deliberate skip, so it is neither a
/// failure nor pending.
fn is_pending(status: &str) -> bool {
    !matches!(status, "pass" | "skip" | "fail")
}

/// Every failed step: the `ac_failed_steps` list and every step whose status is `fail`.
fn failed_steps(workflow: &Workflow) -> Vec<String> {
    let mut failed: BTreeSet<String> = workflow.failed.iter().cloned().collect();
    failed.extend(
        workflow
            .steps
            .iter()
            .filter(|(_, status)| status.as_str() == "fail")
            .map(|(step, _)| step.clone()),
    );
    failed.into_iter().collect()
}

/// The one guest with this name, or `None` when there is none or several. Two guests of one
/// name would make the join guess, and a wrong guess would show another host's state.
pub fn unique_guest<'a>(name: &str, guests: &'a [PveGuest]) -> Option<&'a PveGuest> {
    let mut matches = guests.iter().filter(|guest| guest.name == name);
    let first = matches.next()?;
    matches.next().is_none().then_some(first)
}

/// What one request's join reads from the cache.
struct Join<'a> {
    netbox: Option<&'a NetboxData>,
    proxmox: Option<&'a ProxmoxData>,
    stale: bool,
    grafana: Option<&'a str>,
}

impl<'a> Join<'a> {
    fn of(cache: &'a InventoryCache, grafana: Option<&'a str>) -> Self {
        Join {
            netbox: cache.netbox.data.as_ref(),
            proxmox: cache.proxmox.data.as_ref(),
            stale: cache.stale(),
            grafana,
        }
    }
}

/// The tenant's sites in scope-file order, each joined to the cached data.
fn site_views(tenant: &Tenant, join: &Join<'_>) -> Vec<SiteView> {
    tenant
        .orgs
        .iter()
        .flat_map(|org| {
            org.sites
                .iter()
                .map(move |site| site_view(&org.id, site, join))
        })
        .collect()
}

fn site_view(org: &str, site: &Site, join: &Join<'_>) -> SiteView {
    let mapped = site
        .netbox_site
        .as_deref()
        .and_then(|slug| join.netbox.and_then(|data| data.sites.get(slug)));
    let (clusters, devices): (Vec<ClusterView>, Vec<NbDevice>) = match mapped {
        Some(nb) => (
            nb.clusters
                .iter()
                .map(|cluster| cluster_view(cluster, join))
                .collect(),
            nb.devices.clone(),
        ),
        None => (Vec::new(), Vec::new()),
    };
    SiteView {
        id: site.id.clone(),
        name: site.name.clone(),
        org: org.to_string(),
        netbox_site: site.netbox_site.clone(),
        mapped: mapped.is_some(),
        clusters,
        devices,
    }
}

fn cluster_view(cluster: &NbCluster, join: &Join<'_>) -> ClusterView {
    let guests: &[PveGuest] = match join.proxmox {
        Some(data) => &data.guests,
        None => &[],
    };
    let mut placed: BTreeSet<&str> = BTreeSet::new();
    let vms: Vec<VmView> = cluster
        .vms
        .iter()
        .map(|vm| {
            let live = unique_guest(&vm.name, guests);
            if let Some(node) = live.and_then(|guest| guest.node.as_deref()) {
                placed.insert(node);
            }
            vm_view(vm, live, join)
        })
        .collect();
    let nodes: Vec<PveNode> = placed
        .into_iter()
        .filter_map(|name| {
            join.proxmox
                .and_then(|data| data.nodes.iter().find(|node| node.name == name))
        })
        .cloned()
        .collect();
    ClusterView {
        name: cluster.name.clone(),
        nodes,
        vms,
    }
}

fn vm_view(vm: &NbVm, live: Option<&PveGuest>, join: &Join<'_>) -> VmView {
    let live_status = live.and_then(|guest| guest.status.clone());
    let (failed, pending) = match &vm.workflow {
        Some(workflow) => (
            !failed_steps(workflow).is_empty(),
            workflow.steps.values().any(|status| is_pending(status)),
        ),
        None => (false, false),
    };
    VmView {
        name: vm.name.clone(),
        vmid: live.and_then(|guest| guest.vmid),
        status: vm.status.clone(),
        live_status: live_status.clone(),
        vcpus: vm.vcpus,
        memory_mb: vm.memory_mb,
        disk_gb: vm.disk_gb,
        primary_ip: vm.primary_ip.clone(),
        tags: vm.tags.clone(),
        workflow: vm.workflow.clone(),
        rag: rag(&RagInput {
            netbox_status: vm.status.as_str(),
            live_status: live_status.as_deref(),
            failed,
            pending,
            stale: join.stale,
        }),
        grafana_url: join
            .grafana
            .and_then(|base| grafana::service_overview(base, &vm.name)),
    }
}

/// Every VM with workflow fields, in tenant order, as a service row and its VM.
fn services_of(sites: &[SiteView]) -> Vec<(ServiceRow, VmView)> {
    let mut services = Vec::new();
    for site in sites.iter().filter(|site| site.mapped) {
        for cluster in &site.clusters {
            for vm in cluster.vms.iter().filter(|vm| vm.workflow.is_some()) {
                let row = ServiceRow {
                    name: vm.name.clone(),
                    grafana_url: vm.grafana_url.clone(),
                    site: site.id.clone(),
                    cluster: cluster.name.clone(),
                    live_status: vm.live_status.clone(),
                    rag: vm.rag,
                    failed_steps: vm.workflow.as_ref().map(failed_steps).unwrap_or_default(),
                    last_run: vm.workflow.as_ref().and_then(|w| w.last_run.clone()),
                    tags: vm.tags.clone(),
                };
                services.push((row, vm.clone()));
            }
        }
    }
    services
}

/// The tenant the caller may see. An unauthenticated caller gets 401; a tenant the caller
/// may not see answers exactly as an unknown one does.
async fn visible_tenant<'a>(
    app: &'a AppState,
    session: &Session,
    tenant_id: &str,
) -> Result<&'a Tenant, Response> {
    let Some(user) = auth::current_user(session).await else {
        return Err(unauthenticated());
    };
    app.scope
        .visible_tenant(tenant_id, &user.groups)
        .ok_or_else(|| error(StatusCode::NOT_FOUND, "unknown_tenant"))
}

/// `GET /v1/tenants/{t}/inventory`: the cache, joined for the tenant.
pub async fn get_inventory(
    State(app): State<SharedState>,
    session: Session,
    AxumPath(tenant_id): AxumPath<String>,
) -> Response {
    let tenant = match visible_tenant(&app, &session, &tenant_id).await {
        Ok(tenant) => tenant,
        Err(response) => return response,
    };
    let cache = app.inventory.read().await;
    let sites = site_views(tenant, &Join::of(&cache, app.config.grafana_url.as_deref()));
    Json(InventoryBody {
        meta: meta(&cache, app.config.grafana_url.as_deref()),
        sites,
    })
    .into_response()
}

/// `GET /v1/tenants/{t}/services`: the tenant's VMs that carry workflow fields.
pub async fn list_services(
    State(app): State<SharedState>,
    session: Session,
    AxumPath(tenant_id): AxumPath<String>,
) -> Response {
    let tenant = match visible_tenant(&app, &session, &tenant_id).await {
        Ok(tenant) => tenant,
        Err(response) => return response,
    };
    let cache = app.inventory.read().await;
    let sites = site_views(tenant, &Join::of(&cache, app.config.grafana_url.as_deref()));
    let services = services_of(&sites)
        .into_iter()
        .map(|(row, _)| row)
        .collect();
    Json(ServicesBody {
        meta: meta(&cache, app.config.grafana_url.as_deref()),
        services,
    })
    .into_response()
}

/// `GET /v1/tenants/{t}/services/{name}`: one service and its VM. Unknown name: 404.
pub async fn get_service(
    State(app): State<SharedState>,
    session: Session,
    AxumPath((tenant_id, name)): AxumPath<(String, String)>,
) -> Response {
    let tenant = match visible_tenant(&app, &session, &tenant_id).await {
        Ok(tenant) => tenant,
        Err(response) => return response,
    };
    let cache = app.inventory.read().await;
    let sites = site_views(tenant, &Join::of(&cache, app.config.grafana_url.as_deref()));
    let Some((service, vm)) = services_of(&sites)
        .into_iter()
        .find(|(row, _)| row.name == name)
    else {
        return error(StatusCode::NOT_FOUND, "unknown_service");
    };
    Json(ServiceBody {
        meta: meta(&cache, app.config.grafana_url.as_deref()),
        service,
        vm,
    })
    .into_response()
}

/// `GET /v1/network`: every prefix NetBox holds, global ones included. Admin-only: a prefix
/// without a site names no tenant, so it cannot sit in a tenant-scoped answer (D16, D25).
#[derive(Serialize)]
pub struct NetworkBody {
    #[serde(flatten)]
    pub meta: Meta,
    pub prefixes: Vec<NbPrefix>,
}

pub async fn get_network(State(app): State<SharedState>, session: Session) -> Response {
    if let Err(response) = work::admin_only(&session).await {
        return response;
    }
    let cache = app.inventory.read().await;
    let prefixes = cache
        .netbox
        .data
        .as_ref()
        .map(|data| data.prefixes.clone())
        .unwrap_or_default();
    Json(NetworkBody {
        meta: meta(&cache, app.config.grafana_url.as_deref()),
        prefixes,
    })
    .into_response()
}

/// `GET /v1/flows`. Platform-wide: the catalogue names no tenant (D16), so it is
/// admin-only, like `/v1/work` (D19, D25), and not tenant-scoped.
pub async fn get_flows(State(app): State<SharedState>, session: Session) -> Response {
    if let Err(response) = work::admin_only(&session).await {
        return response;
    }
    Json(FlowsBody {
        source: app.flows.source,
        flows: app.flows.flows.clone(),
        links: links(app.config.grafana_url.as_deref()),
    })
    .into_response()
}

/// Reads each configured authority once and stores the result. The lock is held only to
/// store, never across a request.
pub async fn refresh(app: &AppState) {
    if let Some(config) = &app.config.netbox {
        let result = netbox::fetch_all(&app.http, config, &app.scope.netbox_sites()).await;
        let mut cache = app.inventory.write().await;
        match result {
            Ok(data) => cache.netbox.record_ok(data, rfc3339_now()),
            Err(err) => {
                let text = err.to_string();
                log_failure("NetBox", cache.netbox.error.as_deref(), &text);
                cache.netbox.record_error(text);
            }
        }
    }
    if let Some(config) = &app.config.proxmox {
        let result = proxmox::fetch(&app.proxmox_http, config).await;
        let mut cache = app.inventory.write().await;
        match result {
            Ok(data) => cache.proxmox.record_ok(data, rfc3339_now()),
            Err(err) => {
                let text = err.to_string();
                log_failure("Proxmox", cache.proxmox.error.as_deref(), &text);
                cache.proxmox.record_error(text);
            }
        }
    }
}

/// One line per distinct failure, not one per refresh while it lasts.
fn log_failure(source: &str, previous: Option<&str>, text: &str) {
    if previous != Some(text) {
        eprintln!("console: {source} refresh failed: {text}");
    }
}

/// Starts the refresh when either authority is configured. The first read runs at once.
pub fn spawn_refresh(state: SharedState) {
    if state.config.netbox.is_none() && state.config.proxmox.is_none() {
        return;
    }
    tokio::spawn(async move {
        let mut ticker = tokio::time::interval(REFRESH_INTERVAL);
        ticker.set_missed_tick_behavior(MissedTickBehavior::Delay);
        loop {
            ticker.tick().await;
            refresh(&state).await;
        }
    });
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;

    use dioxus::server::axum::routing::get;
    use dioxus::server::axum::Router;
    use tower_sessions::Session;

    use super::*;
    use crate::server::config::{Config, NetboxConfig, ProxmoxConfig, Secret};
    use crate::server::netbox::fixtures as nb;
    use crate::server::prefs::Prefs;
    use crate::server::proxmox::fixtures as pve;
    use crate::server::scope::Scope;
    use crate::server::{api_router, session_layer};

    fn guest(name: &str, vmid: u64, node: &str, status: &str) -> PveGuest {
        PveGuest {
            name: name.into(),
            vmid: Some(vmid),
            node: Some(node.into()),
            status: Some(status.into()),
        }
    }

    fn input<'a>(
        netbox: &'a str,
        live: Option<&'a str>,
        failed: bool,
        pending: bool,
        stale: bool,
    ) -> RagInput<'a> {
        RagInput {
            netbox_status: netbox,
            live_status: live,
            failed,
            pending,
            stale,
        }
    }

    #[test]
    fn rag_red_when_a_step_failed_whatever_else_holds() {
        assert_eq!(
            rag(&input("active", Some("running"), true, false, false)),
            "red"
        );
        assert_eq!(
            rag(&input("planned", Some("running"), true, false, true)),
            "red"
        );
    }

    #[test]
    fn rag_red_when_stopped_while_netbox_says_active() {
        assert_eq!(
            rag(&input("active", Some("stopped"), false, false, false)),
            "red"
        );
    }

    #[test]
    fn rag_stopped_while_planned_is_amber_not_red() {
        assert_eq!(
            rag(&input("planned", Some("stopped"), false, false, false)),
            "amber"
        );
        assert_eq!(
            rag(&input("offline", Some("stopped"), false, false, false)),
            "amber"
        );
    }

    #[test]
    fn rag_amber_for_stale_planned_staged_pending_or_odd_live_state() {
        assert_eq!(
            rag(&input("active", Some("running"), false, false, true)),
            "amber"
        );
        assert_eq!(
            rag(&input("planned", Some("running"), false, false, false)),
            "amber"
        );
        assert_eq!(
            rag(&input("staged", Some("running"), false, false, false)),
            "amber"
        );
        assert_eq!(
            rag(&input("active", Some("running"), false, true, false)),
            "amber"
        );
        assert_eq!(
            rag(&input("active", Some("paused"), false, false, false)),
            "amber"
        );
    }

    #[test]
    fn rag_grey_without_live_state() {
        assert_eq!(rag(&input("active", None, false, false, false)), "grey");
    }

    #[test]
    fn rag_green_only_when_running_with_nothing_else_to_say() {
        assert_eq!(
            rag(&input("active", Some("running"), false, false, false)),
            "green"
        );
    }

    #[test]
    fn a_skipped_step_is_settled_not_pending() {
        let workflow = Workflow {
            steps: [
                ("a".to_string(), "pass".to_string()),
                ("b".to_string(), "skip".to_string()),
            ]
            .into_iter()
            .collect(),
            failed: Vec::new(),
            last_run: None,
        };
        assert!(!workflow.steps.values().any(|status| is_pending(status)));
        assert!(failed_steps(&workflow).is_empty());
    }

    #[test]
    fn an_unknown_or_pending_step_is_pending() {
        assert!(is_pending("pending"));
        assert!(is_pending("unknown"));
        assert!(!is_pending("fail"));
    }

    #[test]
    fn failed_steps_is_the_sorted_union_of_the_list_and_fail_statuses() {
        let workflow = Workflow {
            steps: [
                ("step-c".to_string(), "fail".to_string()),
                ("step-a".to_string(), "pass".to_string()),
                ("step-b".to_string(), "fail".to_string()),
            ]
            .into_iter()
            .collect(),
            failed: vec!["step-b".into(), "step-z".into()],
            last_run: None,
        };
        assert_eq!(failed_steps(&workflow), ["step-b", "step-c", "step-z"]);
    }

    #[test]
    fn a_name_with_two_guests_has_no_live_state() {
        let guests = vec![
            guest("twin", 300, "pve-02", "running"),
            guest("twin", 301, "pve-01", "stopped"),
        ];
        assert!(unique_guest("twin", &guests).is_none());
        assert!(unique_guest("absent", &guests).is_none());
        let one = vec![guest("solo", 9, "pve-01", "running")];
        assert_eq!(unique_guest("solo", &one).and_then(|g| g.vmid), Some(9));
    }

    #[test]
    fn no_log_line_names_a_credential() {
        for (file, text) in [
            ("inventory.rs", include_str!("inventory.rs")),
            ("netbox.rs", include_str!("netbox.rs")),
            ("proxmox.rs", include_str!("proxmox.rs")),
        ] {
            for line in text.lines().filter(|line| line.contains("println!")) {
                let lowered = line.to_ascii_lowercase();
                assert!(
                    !line.contains("expose()") && !lowered.contains("token"),
                    "{file}: {line}"
                );
            }
        }
    }

    fn flow_file(name: &str, text: &str) -> std::path::PathBuf {
        let path =
            std::env::temp_dir().join(format!("console-flows-{}-{name}.json", std::process::id()));
        std::fs::write(&path, text).expect("write");
        path
    }

    #[test]
    fn load_flows_reads_the_declared_shape_and_names_the_setting_on_error() {
        let good = flow_file(
            "good",
            r#"[{"kind":"oidc","from":"console","to":"authentik","label":"sign-in"}]"#,
        );
        let flows = load_flows(&good).expect("valid");
        assert_eq!(flows[0].kind, "oidc");
        let bad = flow_file("bad", "[{");
        let err = load_flows(&bad).expect_err("must fail");
        assert!(err.starts_with("CONSOLE_FLOWS_FILE: invalid JSON"), "{err}");
        let missing = load_flows(&std::env::temp_dir().join("console-flows-absent.json"))
            .expect_err("must fail");
        assert!(
            missing.starts_with("CONSOLE_FLOWS_FILE: cannot read"),
            "{missing}"
        );
        // Names only: an entry that carries an address is refused, not silently kept.
        let with_address = flow_file(
            "address",
            r#"[{"kind":"oidc","from":"console","to":"authentik","label":"sign-in","address":"198.51.100.5"}]"#,
        );
        let err = load_flows(&with_address).expect_err("an address is not a flow name");
        assert!(err.starts_with("CONSOLE_FLOWS_FILE: invalid JSON"), "{err}");
        assert!(!err.contains("198.51.100.5"), "{err}");
        // One plain delete per file: no recursive removal.
        let _ = std::fs::remove_file(&good);
        let _ = std::fs::remove_file(&bad);
        let _ = std::fs::remove_file(&with_address);
    }

    // ---- route-level tests against the loopback NetBox and Proxmox stand-ins ----

    const SCOPE: &str = r#"{"example": true, "tenants": [
        {"id": "acme", "name": "Acme", "groups": ["ops"], "orgs": [{"id": "ops", "name": "Ops", "sites": [
            {"id": "dc1", "name": "DC 1", "netbox_site": "dc-1"},
            {"id": "dc2", "name": "DC 2"},
            {"id": "ghost", "name": "Ghost", "netbox_site": "missing"}
        ]}]},
        {"id": "initech", "name": "Initech", "groups": ["finance"], "orgs": [{"id": "lab", "name": "Lab", "sites": [
            {"id": "hq", "name": "HQ", "netbox_site": "dc-1"}
        ]}]}
    ]}"#;

    fn config(netbox: Option<String>, proxmox: Option<String>) -> Config {
        Config {
            base_url: "https://console.test".into(),
            oidc_issuer: "https://auth.test/application/o/console/".into(),
            oidc_client_id: "console".into(),
            oidc_client_secret: "test-secret".into(),
            extra_ca_file: None,
            data_dir: std::env::temp_dir(),
            scope_file: std::env::temp_dir(),
            semaphore: None,
            workflow_file: None,
            netbox: netbox.map(|url| NetboxConfig {
                api_url: url,
                token: Secret::new(nb::TOKEN),
            }),
            proxmox: proxmox.map(|url| ProxmoxConfig {
                api_url: url,
                token_id: pve::TOKEN_ID.into(),
                token: Secret::new(pve::TOKEN),
                ca_file: None,
            }),
            flows_file: None,
            grafana_url: None,
        }
    }

    fn app_state_with(config: Config, cache: InventoryCache, flows: FlowSet) -> SharedState {
        Arc::new(AppState {
            config,
            http: reqwest::Client::new(),
            oidc: tokio::sync::OnceCell::new(),
            scope: Scope::parse(SCOPE).expect("scope"),
            prefs: Prefs::in_memory().expect("prefs"),
            work: Arc::new(tokio::sync::RwLock::new(
                crate::server::work::WorkCache::unconfigured(),
            )),
            workflow: Vec::new(),
            inventory: tokio::sync::RwLock::new(cache),
            proxmox_http: reqwest::Client::new(),
            flows,
        })
    }

    /// A configured cache, not yet refreshed.
    fn fresh_cache() -> InventoryCache {
        InventoryCache::new(true, true)
    }

    fn login_route(groups: &'static [&'static str]) -> Router {
        Router::new().route(
            "/test/login",
            get(move |session: Session| async move {
                let user = auth::CurrentUser {
                    sub: "user-1".into(),
                    email: None,
                    name: None,
                    username: Some("ops-user".into()),
                    groups: groups.iter().map(|group| group.to_string()).collect(),
                };
                session
                    .insert(auth::KEY_USER, &user)
                    .await
                    .expect("insert user");
                "signed in"
            }),
        )
    }

    /// Serves the API as `groups` and returns the base URL and that session's cookie.
    async fn signed_in(state: SharedState, groups: &'static [&'static str]) -> (String, String) {
        let sessions = session_layer(state.prefs.session_store());
        let app = api_router(state.clone())
            .merge(login_route(groups))
            .layer(sessions);
        let addr = nb::serve(app).await;
        let base = format!("http://{addr}");
        let login = reqwest::get(format!("{base}/test/login"))
            .await
            .expect("login");
        let cookie = login
            .headers()
            .get(reqwest::header::SET_COOKIE)
            .and_then(|value| value.to_str().ok())
            .and_then(|value| value.split(';').next())
            .expect("session cookie")
            .to_string();
        (base, cookie)
    }

    async fn get_json(base: &str, cookie: Option<&str>, path: &str) -> (u16, String) {
        let mut request = reqwest::Client::new().get(format!("{base}{path}"));
        if let Some(cookie) = cookie {
            request = request.header(reqwest::header::COOKIE, cookie);
        }
        let response = request.send().await.expect("request");
        let status = response.status().as_u16();
        (status, response.text().await.expect("body"))
    }

    fn both_sources(base_netbox: &str, base_proxmox: &str) -> Config {
        config(
            Some(base_netbox.to_string()),
            Some(base_proxmox.to_string()),
        )
    }

    /// Both stand-ins running, and one refresh done.
    async fn live_state() -> (SharedState, nb::Mock, pve::Mock) {
        live_state_with(None).await
    }

    /// Both stand-ins, one refresh, and `CONSOLE_GRAFANA_URL` as given.
    async fn live_state_with(grafana: Option<&str>) -> (SharedState, nb::Mock, pve::Mock) {
        let nb_mock = nb::Mock::default();
        let pve_mock = pve::Mock::default();
        let nb_addr = nb::serve(nb::router(nb_mock.clone())).await;
        let pve_addr = pve::serve(pve::router(pve_mock.clone())).await;
        let mut config = both_sources(&format!("http://{nb_addr}"), &format!("http://{pve_addr}"));
        config.grafana_url = grafana.map(str::to_string);
        let state = app_state_with(config, fresh_cache(), FlowSet::unconfigured());
        refresh(&state).await;
        (state, nb_mock, pve_mock)
    }

    fn vm_named<'a>(body: &'a serde_json::Value, name: &str) -> &'a serde_json::Value {
        body["sites"]
            .as_array()
            .expect("sites")
            .iter()
            .flat_map(|site| site["clusters"].as_array().into_iter().flatten())
            .flat_map(|cluster| cluster["vms"].as_array().into_iter().flatten())
            .find(|vm| vm["name"] == name)
            .unwrap_or_else(|| panic!("no vm {name}"))
    }

    #[tokio::test]
    async fn unauthenticated_inventory_routes_answer_401() {
        let (state, _, _) = live_state().await;
        let sessions = session_layer(state.prefs.session_store());
        let addr = nb::serve(api_router(state).layer(sessions)).await;
        let base = format!("http://{addr}");
        for path in [
            "/v1/tenants/acme/inventory",
            "/v1/tenants/acme/services",
            "/v1/tenants/acme/services/tududi",
            "/v1/network",
            "/v1/flows",
        ] {
            let (status, body) = get_json(&base, None, path).await;
            assert_eq!(status, 401, "{path}");
            assert_eq!(body, r#"{"error":"unauthenticated"}"#, "{path}");
        }
    }

    #[tokio::test]
    async fn member_sees_the_joined_inventory_with_rag_and_no_secret() {
        let (state, _, _) = live_state().await;
        let (base, cookie) = signed_in(state, &["ops"]).await;
        let (status, text) = get_json(&base, Some(&cookie), "/v1/tenants/acme/inventory").await;
        assert_eq!(status, 200, "{text}");
        for secret in [nb::TOKEN, pve::TOKEN, "hunter2", "vault_note", "token"] {
            assert!(!text.contains(secret), "{secret} leaked: {text}");
        }
        let body: serde_json::Value = serde_json::from_str(&text).expect("json");
        assert_eq!(body["source"]["netbox"], "ok");
        assert_eq!(body["source"]["proxmox"], "ok");
        assert_eq!(body["stale"], false);
        assert!(body["links"]["grafana"].is_null(), "unset base is null");
        assert!(vm_named(&body, "tududi")["grafana_url"].is_null());
        assert!(body["updated_at"]["netbox"].is_string());
        assert!(body["error"]["netbox"].is_null());

        let sites = body["sites"].as_array().expect("sites");
        let ids: Vec<&str> = sites
            .iter()
            .map(|s| s["id"].as_str().expect("id"))
            .collect();
        assert_eq!(ids, ["dc1", "dc2", "ghost"]);
        assert_eq!(sites[0]["org"], "ops");
        assert_eq!(sites[0]["mapped"], true);
        assert_eq!(sites[1]["mapped"], false, "no netbox_site declared");
        assert_eq!(sites[1]["clusters"], serde_json::json!([]));
        assert_eq!(sites[2]["mapped"], false, "slug NetBox does not hold");

        let cluster = &sites[0]["clusters"][0];
        assert_eq!(cluster["name"], "pve-prod");
        let nodes: Vec<&str> = cluster["nodes"]
            .as_array()
            .expect("nodes")
            .iter()
            .map(|n| n["name"].as_str().expect("name"))
            .collect();
        assert_eq!(nodes, ["pve-01", "pve-02"]);
        assert_eq!(cluster["nodes"][0]["maxcpu"], 16.0);

        let tududi = vm_named(&body, "tududi");
        assert_eq!(tududi["rag"], "red", "failed step");
        assert_eq!(tududi["vmid"], 101);
        assert_eq!(tududi["live_status"], "running");
        assert_eq!(tududi["primary_ip"], "198.51.100.5/24");
        assert_eq!(tududi["workflow"]["failed"], serde_json::json!(["step-b"]));
        assert_eq!(
            vm_named(&body, "gateway")["rag"],
            "red",
            "stopped while active"
        );
        assert_eq!(vm_named(&body, "planned-vm")["rag"], "amber", "planned");
        assert_eq!(vm_named(&body, "wiki")["rag"], "green");
        assert_eq!(vm_named(&body, "ghost")["rag"], "grey", "no live match");
        assert!(vm_named(&body, "ghost")["live_status"].is_null());
        assert!(vm_named(&body, "gateway")["workflow"].is_null());

        // Devices are site-scoped and present; prefixes are global and never in a tenant answer.
        let devices = sites[0]["devices"].as_array().expect("devices");
        assert_eq!(devices.len(), 2);
        assert_eq!(devices[0]["name"], "pve-01");
        assert_eq!(devices[0]["role"], "Hypervisor");
        assert_eq!(devices[0]["status"], "active");
        assert!(devices[1]["name"].is_null());
        assert!(devices[1]["role"].is_null());
        assert!(sites[0].get("prefixes").is_none());
        assert_eq!(
            sites[1]["devices"],
            serde_json::json!([]),
            "unmapped site has none"
        );
        assert!(!text.contains("198.51.100.0/24"), "{text}");
        assert!(!text.contains("192.0.2.0/24"), "{text}");
    }

    #[tokio::test]
    async fn a_grafana_base_becomes_the_links_and_the_per_vm_deep_links() {
        let base = "https://grafana.example.test";
        let (state, _, _) = live_state_with(Some(base)).await;
        let (url, cookie) = signed_in(state.clone(), &["ops"]).await;
        let expected = "https://grafana.example.test/d/service-overview/service-overview?var-service=tududi&var-log_service=tududi";
        let conformance =
            "https://grafana.example.test/d/service-conformance/service-deployment-conformance";

        let (_, text) = get_json(&url, Some(&cookie), "/v1/tenants/acme/inventory").await;
        let body: serde_json::Value = serde_json::from_str(&text).expect("json");
        assert_eq!(body["links"]["grafana"], base);
        assert_eq!(body["links"]["grafana_conformance"], conformance);
        assert_eq!(vm_named(&body, "tududi")["grafana_url"], expected);
        assert!(
            vm_named(&body, "wiki")["grafana_url"].is_string(),
            "every VM gets one"
        );

        let (_, text) = get_json(&url, Some(&cookie), "/v1/tenants/acme/services").await;
        let body: serde_json::Value = serde_json::from_str(&text).expect("json");
        assert_eq!(body["links"]["grafana"], base);
        assert_eq!(body["services"][0]["grafana_url"], expected);

        let (_, text) = get_json(&url, Some(&cookie), "/v1/tenants/acme/services/tududi").await;
        let body: serde_json::Value = serde_json::from_str(&text).expect("json");
        assert_eq!(body["links"]["grafana"], base);
        assert_eq!(body["vm"]["grafana_url"], expected);
        assert_eq!(body["service"]["grafana_url"], expected);

        let (admin_url, admin_cookie) = signed_in(state, &["platform-admins"]).await;
        let (_, text) = get_json(&admin_url, Some(&admin_cookie), "/v1/network").await;
        let body: serde_json::Value = serde_json::from_str(&text).expect("json");
        assert_eq!(body["links"]["grafana"], base);
        let (_, text) = get_json(&admin_url, Some(&admin_cookie), "/v1/flows").await;
        let body: serde_json::Value = serde_json::from_str(&text).expect("json");
        assert_eq!(body["links"]["grafana"], base);
        assert_eq!(body["links"]["grafana_conformance"], conformance);
    }

    #[tokio::test]
    async fn a_tenant_the_caller_cannot_see_is_404_like_an_unknown_one() {
        let (state, _, _) = live_state().await;
        let (base, cookie) = signed_in(state, &["ops"]).await;
        for suffix in ["inventory", "services", "services/tududi"] {
            let hidden = get_json(
                &base,
                Some(&cookie),
                &format!("/v1/tenants/initech/{suffix}"),
            )
            .await;
            let unknown = get_json(
                &base,
                Some(&cookie),
                &format!("/v1/tenants/globex/{suffix}"),
            )
            .await;
            assert_eq!(hidden.0, 404, "{suffix}");
            assert_eq!(hidden.1, unknown.1, "{suffix}");
        }
        assert_eq!(
            get_json(&base, Some(&cookie), "/v1/tenants/globex/inventory")
                .await
                .1,
            r#"{"error":"unknown_tenant"}"#
        );
    }

    #[tokio::test]
    async fn services_list_only_the_vms_with_workflow_fields() {
        let (state, _, _) = live_state().await;
        let (base, cookie) = signed_in(state, &["ops"]).await;
        let (status, text) = get_json(&base, Some(&cookie), "/v1/tenants/acme/services").await;
        assert_eq!(status, 200, "{text}");
        let body: serde_json::Value = serde_json::from_str(&text).expect("json");
        let names: Vec<&str> = body["services"]
            .as_array()
            .expect("services")
            .iter()
            .map(|s| s["name"].as_str().expect("name"))
            .collect();
        assert_eq!(names, ["tududi", "planned-vm"]);
        assert_eq!(body["services"][0]["rag"], "red");
        assert_eq!(
            body["services"][0]["failed_steps"],
            serde_json::json!(["step-b"])
        );
        assert_eq!(body["services"][0]["site"], "dc1");
        assert!(body["links"]["grafana"].is_null(), "unset base is null");
        assert!(body["services"][0]["grafana_url"].is_null());
        assert_eq!(body["services"][0]["cluster"], "pve-prod");
        assert_eq!(body["services"][0]["last_run"], "2026-10-26T12:00:00Z");
        assert!(!text.contains("hunter2"), "{text}");
    }

    #[tokio::test]
    async fn one_service_carries_its_vm_and_an_unknown_one_is_404() {
        let (state, _, _) = live_state().await;
        let (base, cookie) = signed_in(state, &["ops"]).await;
        let (status, text) =
            get_json(&base, Some(&cookie), "/v1/tenants/acme/services/tududi").await;
        assert_eq!(status, 200, "{text}");
        let body: serde_json::Value = serde_json::from_str(&text).expect("json");
        assert_eq!(body["service"]["name"], "tududi");
        assert_eq!(body["vm"]["vmid"], 101);
        assert_eq!(body["vm"]["rag"], "red");

        let (status, text) =
            get_json(&base, Some(&cookie), "/v1/tenants/acme/services/gateway").await;
        assert_eq!(status, 404);
        assert_eq!(text, r#"{"error":"unknown_service"}"#);
    }

    #[tokio::test]
    async fn unconfigured_sources_answer_unconfigured_and_every_site_unmapped() {
        let state = app_state_with(
            config(None, None),
            InventoryCache::new(false, false),
            FlowSet::unconfigured(),
        );
        refresh(&state).await;
        let (base, cookie) = signed_in(state, &["ops"]).await;
        let (status, text) = get_json(&base, Some(&cookie), "/v1/tenants/acme/inventory").await;
        assert_eq!(status, 200);
        let body: serde_json::Value = serde_json::from_str(&text).expect("json");
        assert_eq!(
            body["source"],
            serde_json::json!({"netbox": "unconfigured", "proxmox": "unconfigured"})
        );
        assert_eq!(
            body["updated_at"],
            serde_json::json!({"netbox": null, "proxmox": null})
        );
        assert_eq!(body["stale"], false);
        for site in body["sites"].as_array().expect("sites") {
            assert_eq!(site["mapped"], false, "{site}");
        }
    }

    #[tokio::test]
    async fn configured_but_not_yet_refreshed_is_ok_with_no_timestamp() {
        let state = app_state_with(
            both_sources("http://127.0.0.1:9", "http://127.0.0.1:9"),
            fresh_cache(),
            FlowSet::unconfigured(),
        );
        let (base, cookie) = signed_in(state, &["ops"]).await;
        let (_, text) = get_json(&base, Some(&cookie), "/v1/tenants/acme/inventory").await;
        let body: serde_json::Value = serde_json::from_str(&text).expect("json");
        assert_eq!(
            body["source"],
            serde_json::json!({"netbox": "ok", "proxmox": "ok"})
        );
        assert_eq!(body["updated_at"]["netbox"], serde_json::Value::Null);
        assert_eq!(body["sites"][0]["mapped"], false);
    }

    #[tokio::test]
    async fn a_failed_refresh_keeps_the_last_good_data_and_marks_it_stale() {
        let (state, nb_mock, _) = live_state().await;
        let (base, cookie) = signed_in(state.clone(), &["ops"]).await;

        nb_mock
            .fail
            .store(true, std::sync::atomic::Ordering::SeqCst);
        refresh(&state).await;

        let (status, text) = get_json(&base, Some(&cookie), "/v1/tenants/acme/inventory").await;
        assert_eq!(status, 200, "{text}");
        assert!(!text.contains("boom"), "{text}");
        assert!(!text.contains(nb::EXPECTED_AUTH), "{text}");
        let body: serde_json::Value = serde_json::from_str(&text).expect("json");
        assert_eq!(body["source"]["netbox"], "error");
        assert_eq!(body["source"]["proxmox"], "ok");
        assert_eq!(body["error"]["netbox"], "NetBox answered HTTP 500");
        assert_eq!(body["stale"], true);
        assert!(
            body["updated_at"]["netbox"].is_string(),
            "last good read kept"
        );
        assert_eq!(
            body["sites"][0]["mapped"], true,
            "last good data still served"
        );
        assert_eq!(
            vm_named(&body, "tududi")["rag"],
            "red",
            "a failed step stays red"
        );
        assert_eq!(
            vm_named(&body, "wiki")["rag"],
            "amber",
            "stale turns a clean VM amber"
        );
    }

    #[tokio::test]
    async fn a_proxmox_refusal_is_a_named_error_without_the_token() {
        let pve_mock = pve::Mock::default();
        let pve_addr = pve::serve(pve::router(pve_mock)).await;
        let mut config = config(None, Some(format!("http://{pve_addr}")));
        if let Some(proxmox) = config.proxmox.as_mut() {
            proxmox.token = Secret::new("wrong-pve-secret");
        }
        let state = app_state_with(
            config,
            InventoryCache::new(false, true),
            FlowSet::unconfigured(),
        );
        refresh(&state).await;
        let cache = state.inventory.read().await;
        assert_eq!(cache.proxmox.status, Source::Error);
        assert_eq!(
            cache.proxmox.error.as_deref(),
            Some("Proxmox answered HTTP 401")
        );
        assert!(!cache
            .proxmox
            .error
            .as_deref()
            .unwrap_or("")
            .contains("wrong-pve-secret"));
    }

    #[tokio::test]
    async fn network_and_flows_are_admin_only_and_not_tenant_scoped() {
        let (state, _, _) = live_state().await;
        for path in ["/v1/network", "/v1/flows"] {
            let (base, cookie) = signed_in(state.clone(), &["ops"]).await;
            let (status, text) = get_json(&base, Some(&cookie), path).await;
            assert_eq!(status, 403, "{path}");
            assert_eq!(text, r#"{"error":"forbidden"}"#, "{path}");
        }
        // A tenant member of the hidden tenant gets the same 403, not a tenant 404.
        let (base, cookie) = signed_in(state.clone(), &["finance"]).await;
        assert_eq!(get_json(&base, Some(&cookie), "/v1/network").await.0, 403);
    }

    #[tokio::test]
    async fn admin_reads_every_prefix_with_its_site_or_null() {
        let (state, _, _) = live_state().await;
        let (base, cookie) = signed_in(state, &["platform-admins"]).await;
        let (status, text) = get_json(&base, Some(&cookie), "/v1/network").await;
        assert_eq!(status, 200, "{text}");
        let body: serde_json::Value = serde_json::from_str(&text).expect("json");
        assert_eq!(body["source"]["netbox"], "ok");
        assert_eq!(body["stale"], false);
        let prefixes = body["prefixes"].as_array().expect("prefixes");
        assert_eq!(prefixes.len(), 2);
        assert_eq!(prefixes[0]["prefix"], "198.51.100.0/24");
        assert_eq!(prefixes[0]["vlan"], 20);
        assert_eq!(prefixes[0]["description"], "management");
        assert_eq!(prefixes[0]["site"], "dc-1");
        assert_eq!(prefixes[1]["prefix"], "192.0.2.0/24");
        assert!(prefixes[1]["vlan"].is_null());
        assert!(prefixes[1]["site"].is_null(), "global prefix has no site");
    }

    #[tokio::test]
    async fn flows_answer_unconfigured_or_the_declared_catalogue() {
        let (state, _, _) = live_state().await;
        let (base, cookie) = signed_in(state, &["platform-admins"]).await;
        let (status, text) = get_json(&base, Some(&cookie), "/v1/flows").await;
        assert_eq!(status, 200);
        assert_eq!(
            text,
            r#"{"source":"unconfigured","flows":[],"links":{"grafana":null,"grafana_conformance":null}}"#
        );

        let flows = FlowSet::configured(vec![Flow {
            kind: "oidc".into(),
            from: "console".into(),
            to: "authentik".into(),
            label: "sign-in".into(),
        }]);
        let state = app_state_with(config(None, None), InventoryCache::new(false, false), flows);
        let (base, cookie) = signed_in(state, &["platform-admins"]).await;
        let (_, text) = get_json(&base, Some(&cookie), "/v1/flows").await;
        assert_eq!(
            text,
            r#"{"source":"ok","flows":[{"kind":"oidc","from":"console","to":"authentik","label":"sign-in"}],"links":{"grafana":null,"grafana_conformance":null}}"#
        );
    }
}
