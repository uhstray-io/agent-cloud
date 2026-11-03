//! Read-only NetBox client for the inventory. Only the refresh task calls it; the browser
//! never talks to NetBox. Errors are fixed variants: no URL, no response body and no header
//! reaches a message or a log line.
//!
//! Two reads, kept apart so one cannot blank the other:
//!
//! - `fetch_all` reads the declared sites: each site's clusters, their VMs, and the site's
//!   devices. Every row is checked against the query that returned it. A row whose own scope
//!   does not name the requested site or cluster is dropped and counted in `dropped_mismatched`.
//!   Tenant isolation therefore holds even when NetBox ignores a filter.
//! - `fetch_prefixes` reads every prefix, paging with `offset` up to `PREFIX_PAGE_CAP` pages.
//!
//! A list that reports a next page fails the site read with `Truncated` rather than showing a
//! partial inventory as complete. Of a VM's custom fields only the three `ac_*` workflow fields
//! are read (`WorkflowFields`); every other key is discarded by serde.

use std::collections::{BTreeMap, BTreeSet};
use std::fmt;

use serde::de::DeserializeOwned;
use serde::{Deserialize, Serialize};
use serde_json::Value;

use super::config::NetboxConfig;

/// `limit` sent with every list request. One page per site list, see `Truncated`.
pub const PAGE_LIMIT: &str = "1000";

/// Prefix pages read per refresh. A prefix list longer than this is marked truncated.
pub const PREFIX_PAGE_CAP: usize = 20;

/// The site read: what the tenant views join. Keyed by site slug. A slug NetBox does not hold
/// has no entry, which is an unmapped site, not a read failure.
#[derive(Debug, Clone, Default)]
pub struct NetboxData {
    pub sites: BTreeMap<String, NbSite>,
    /// VM names held by more than one VM across every declared site, in any tenant. A name in
    /// this set gets no live join, because a guess could show another site's host.
    pub ambiguous_vm_names: BTreeSet<String>,
    /// Rows dropped because their own scope did not match the query that returned them.
    pub dropped_mismatched: u64,
}

/// The prefix read, separate from the site read. Admin-only (`GET /v1/network`).
#[derive(Debug, Clone, Default)]
pub struct PrefixData {
    pub prefixes: Vec<NbPrefix>,
    /// True when NetBox still had a page after `PREFIX_PAGE_CAP` pages.
    pub truncated: bool,
}

#[derive(Debug, Clone)]
pub struct NbSite {
    pub clusters: Vec<NbCluster>,
    pub devices: Vec<NbDevice>,
}

#[derive(Debug, Clone)]
pub struct NbCluster {
    pub name: String,
    pub vms: Vec<NbVm>,
}

#[derive(Debug, Clone)]
pub struct NbVm {
    pub name: String,
    /// NetBox status value (`active`, `planned`, `staged`, ...).
    pub status: String,
    pub vcpus: Option<f64>,
    pub memory_mb: Option<u64>,
    /// The disk size in megabytes, as NetBox stores it. NetBox's model labels it `disk (MB)`
    /// (netbox-community/netbox v4.5.0 `netbox/virtualization/models/virtualmachines.py:126-129`),
    /// and the unit changed from gigabytes to megabytes in 4.1
    /// (`docs/release-notes/version-4.1.md:8` at v4.1.0). Stored as read, never converted.
    pub disk_mb: Option<u64>,
    /// The primary address as NetBox holds it, prefix length included. Never the other addresses.
    pub primary_ip: Option<String>,
    pub tags: Vec<String>,
    /// `None` when the VM carries none of the workflow fields, so it is not a service.
    pub workflow: Option<Workflow>,
}

/// The conformance collector's view of one VM. Step statuses are the collector's own words
/// (`pass`, `fail`, `skip`); anything else arrives as `unknown`.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Workflow {
    pub steps: BTreeMap<String, String>,
    /// `ac_failed_steps` split on commas, as written.
    pub failed: Vec<String>,
    pub last_run: Option<String>,
}

/// A device at a site. Site-scoped, so it is safe in a tenant answer.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct NbDevice {
    pub name: Option<String>,
    /// The device role's name.
    pub role: Option<String>,
    pub status: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct NbPrefix {
    pub prefix: String,
    /// The VLAN id (`vid`), not its name.
    pub vlan: Option<u64>,
    pub description: String,
    /// The slug of the site the prefix's own scope names, or `None` when NetBox gives no site.
    pub site: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum FetchError {
    Timeout,
    Connect,
    Status(u16),
    Decode,
    Truncated,
    Other,
}

impl fmt::Display for FetchError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            FetchError::Timeout => f.write_str("NetBox request timed out"),
            FetchError::Connect => f.write_str("cannot connect to NetBox"),
            FetchError::Status(code) => write!(f, "NetBox answered HTTP {code}"),
            FetchError::Decode => f.write_str("NetBox response could not be decoded"),
            FetchError::Truncated => f.write_str("NetBox list has more than one page"),
            FetchError::Other => f.write_str("NetBox request failed"),
        }
    }
}

/// The `Authorization` value for a stored token. The shape decides the scheme, as
/// `platform/playbooks/tasks/netbox-api-headers.yml` does: a v2 token (`nbt_` prefix) is
/// sent as `Bearer`, a legacy v1 token as `Token`.
pub fn authorization(token: &str) -> String {
    if token.starts_with("nbt_") {
        format!("Bearer {token}")
    } else {
        format!("Token {token}")
    }
}

/// Reads every declared site: the site, its clusters with their VMs, and its devices. A slug
/// NetBox does not hold is skipped. A failed request fails the whole site read, so one refresh
/// is either complete or not used.
pub async fn fetch_all(
    http: &reqwest::Client,
    config: &NetboxConfig,
    slugs: &[String],
) -> Result<NetboxData, FetchError> {
    let mut sites = BTreeMap::new();
    let mut dropped = 0;
    for slug in slugs {
        let found: Vec<SiteRow> =
            list(http, config, "/api/dcim/sites/", &[("slug", slug.clone())]).await?;
        let (matching, lost) =
            keep_matching(found, |row| row.slug.as_deref() == Some(slug.as_str()));
        dropped += lost;
        let Some(site) = matching.first() else {
            continue;
        };
        let (read, lost) = fetch_site(http, config, site.id).await?;
        dropped += lost;
        sites.insert(slug.clone(), read);
    }
    let ambiguous_vm_names = duplicate_vm_names(&sites);
    Ok(NetboxData {
        sites,
        ambiguous_vm_names,
        dropped_mismatched: dropped,
    })
}

/// Reads one site and returns it with the number of rows dropped for not matching the query.
async fn fetch_site(
    http: &reqwest::Client,
    config: &NetboxConfig,
    site_id: u64,
) -> Result<(NbSite, u64), FetchError> {
    let by_site = [("site_id", site_id.to_string())];
    let cluster_rows: Vec<ClusterRow> =
        list(http, config, "/api/virtualization/clusters/", &by_site).await?;
    let (cluster_rows, mut dropped) =
        keep_matching(cluster_rows, |cluster| cluster.belongs_to_site(site_id));
    let mut clusters = Vec::with_capacity(cluster_rows.len());
    for cluster in cluster_rows {
        let vms: Vec<VmRow> = list(
            http,
            config,
            "/api/virtualization/virtual-machines/",
            &[("cluster_id", cluster.id.to_string())],
        )
        .await?;
        let (vms, lost) = keep_matching(vms, |vm| vm.cluster_id() == Some(cluster.id));
        dropped += lost;
        clusters.push(NbCluster {
            name: cluster.name,
            vms: vms.into_iter().map(vm_from).collect(),
        });
    }
    let devices: Vec<DeviceRow> = list(http, config, "/api/dcim/devices/", &by_site).await?;
    let (devices, lost) = keep_matching(devices, |device| {
        device.site.as_ref().map(|site| site.id) == Some(site_id)
    });
    dropped += lost;
    let site = NbSite {
        clusters,
        devices: devices.into_iter().map(device_from).collect(),
    };
    Ok((site, dropped))
}

/// Keeps the rows that pass `matches` and counts the rest.
fn keep_matching<T>(rows: Vec<T>, matches: impl Fn(&T) -> bool) -> (Vec<T>, u64) {
    let total = rows.len();
    let kept: Vec<T> = rows.into_iter().filter(|row| matches(row)).collect();
    let dropped = (total - kept.len()) as u64;
    (kept, dropped)
}

/// Names that more than one VM carries across every site in the read.
fn duplicate_vm_names(sites: &BTreeMap<String, NbSite>) -> BTreeSet<String> {
    let mut seen: BTreeMap<&str, u32> = BTreeMap::new();
    for vm in sites
        .values()
        .flat_map(|site| &site.clusters)
        .flat_map(|cluster| &cluster.vms)
    {
        *seen.entry(vm.name.as_str()).or_insert(0) += 1;
    }
    seen.into_iter()
        .filter(|(_, count)| *count > 1)
        .map(|(name, _)| name.to_string())
        .collect()
}

/// Reads every prefix, following `offset` pages to the end or to `PREFIX_PAGE_CAP` pages.
///
/// The next page is asked for with this client's own URL and an `offset`. The `next` link NetBox
/// returns is not followed, so the bearer never goes to an address the server supplied.
pub async fn fetch_prefixes(
    http: &reqwest::Client,
    config: &NetboxConfig,
) -> Result<PrefixData, FetchError> {
    let mut prefixes = Vec::new();
    let mut offset = 0usize;
    for _ in 0..PREFIX_PAGE_CAP {
        let mut query = vec![("limit", PAGE_LIMIT.to_string())];
        if offset > 0 {
            query.push(("offset", offset.to_string()));
        }
        let page: Page<PrefixRow> = get_page(http, config, "/api/ipam/prefixes/", &query).await?;
        let got = page.results.len();
        prefixes.extend(page.results.into_iter().map(prefix_from));
        if page.next.is_none() {
            return Ok(PrefixData {
                prefixes,
                truncated: false,
            });
        }
        if got == 0 {
            // A next page with no rows cannot advance the offset, so stop here.
            break;
        }
        offset += got;
    }
    Ok(PrefixData {
        prefixes,
        truncated: true,
    })
}

/// One list page, as NetBox returns it.
async fn get_page<T: DeserializeOwned>(
    http: &reqwest::Client,
    config: &NetboxConfig,
    path: &str,
    query: &[(&str, String)],
) -> Result<Page<T>, FetchError> {
    let response = http
        .get(format!("{}{path}", config.api_url))
        .header(
            reqwest::header::AUTHORIZATION,
            authorization(config.token.expose()),
        )
        .header(reqwest::header::ACCEPT, "application/json")
        .query(query)
        .send()
        .await
        .map_err(transport_error)?;
    let status = response.status();
    if !status.is_success() {
        return Err(FetchError::Status(status.as_u16()));
    }
    let body = response.bytes().await.map_err(transport_error)?;
    serde_json::from_slice(&body).map_err(|_| FetchError::Decode)
}

/// One page of a list that must fit in one page. A next page is `Truncated`.
async fn list<T: DeserializeOwned>(
    http: &reqwest::Client,
    config: &NetboxConfig,
    path: &str,
    filters: &[(&str, String)],
) -> Result<Vec<T>, FetchError> {
    let mut query = filters.to_vec();
    query.push(("limit", PAGE_LIMIT.to_string()));
    let page: Page<T> = get_page(http, config, path, &query).await?;
    if page.next.is_some() {
        return Err(FetchError::Truncated);
    }
    Ok(page.results)
}

/// reqwest's own Display names the request URL, so it is classified, not shown.
fn transport_error(err: reqwest::Error) -> FetchError {
    if err.is_timeout() {
        FetchError::Timeout
    } else if err.is_connect() {
        FetchError::Connect
    } else {
        FetchError::Other
    }
}

// Wire shapes. Only the fields the inventory reads are declared; serde ignores the rest.
// Nullable NetBox fields are `Option`, so a null never fails a whole page.

#[derive(Deserialize)]
struct Page<T> {
    results: Vec<T>,
    #[serde(default)]
    next: Option<String>,
}

/// A nested reference by id (`{"id": ..., ...}`).
#[derive(Deserialize)]
struct IdRef {
    id: u64,
}

#[derive(Deserialize)]
struct SiteRow {
    id: u64,
    slug: Option<String>,
}

#[derive(Deserialize)]
struct ClusterRow {
    id: u64,
    name: String,
    /// The cluster's site, as a nested `site` object.
    site: Option<IdRef>,
    /// The cluster's scope, as a type and an id.
    scope_type: Option<String>,
    scope_id: Option<u64>,
}

impl ClusterRow {
    /// True when the cluster is the requested site's. A cluster whose scope is a location or a
    /// region (any scope type but `dcim.site`) is refused, even when its legacy `site` field
    /// names the site: the console does not yet resolve those scopes to a site, so it fails
    /// closed. A row with no scope type is judged by its legacy `site` field alone.
    fn belongs_to_site(&self, site_id: u64) -> bool {
        let legacy_site = self.site.as_ref().is_some_and(|site| site.id == site_id);
        match self.scope_type.as_deref() {
            Some("dcim.site") => legacy_site || self.scope_id == Some(site_id),
            Some(_) => false,
            None => legacy_site,
        }
    }
}

/// NetBox choice field: `{"value": ..., "label": ..., "id": ...}`.
#[derive(Deserialize)]
struct Choice {
    value: Option<String>,
}

#[derive(Deserialize)]
struct NameRef {
    name: Option<String>,
}

#[derive(Deserialize)]
struct AddressRef {
    address: Option<String>,
}

#[derive(Deserialize)]
struct TagRef {
    name: String,
}

#[derive(Deserialize)]
struct VlanRef {
    vid: Option<u64>,
}

#[derive(Deserialize)]
struct VmRow {
    name: String,
    status: Option<Choice>,
    vcpus: Option<f64>,
    memory: Option<u64>,
    disk: Option<u64>,
    primary_ip: Option<AddressRef>,
    tags: Option<Vec<TagRef>>,
    cluster: Option<IdRef>,
    custom_fields: Option<WorkflowFields>,
}

impl VmRow {
    fn cluster_id(&self) -> Option<u64> {
        self.cluster.as_ref().map(|cluster| cluster.id)
    }
}

/// The three workflow fields the conformance collector writes (`netbox-custom-fields.yml`).
#[derive(Deserialize, Default)]
struct WorkflowFields {
    ac_workflow_status: Option<Value>,
    ac_failed_steps: Option<String>,
    ac_last_run: Option<String>,
}

#[derive(Deserialize)]
struct DeviceRow {
    name: Option<String>,
    role: Option<NameRef>,
    status: Option<Choice>,
    site: Option<IdRef>,
}

#[derive(Deserialize)]
struct PrefixRow {
    prefix: String,
    vlan: Option<VlanRef>,
    description: Option<String>,
    /// The scope type (`dcim.site`, `dcim.location`, ...).
    scope_type: Option<String>,
    scope: Option<ScopeRef>,
    /// The site, as a nested `site` object.
    site: Option<SlugRef>,
}

#[derive(Deserialize)]
struct ScopeRef {
    slug: Option<String>,
}

#[derive(Deserialize)]
struct SlugRef {
    slug: Option<String>,
}

fn vm_from(row: VmRow) -> NbVm {
    NbVm {
        name: row.name,
        status: row
            .status
            .and_then(|choice| choice.value)
            .unwrap_or_default(),
        vcpus: row.vcpus,
        memory_mb: row.memory,
        disk_mb: row.disk,
        primary_ip: row.primary_ip.and_then(|ip| ip.address),
        tags: row
            .tags
            .unwrap_or_default()
            .into_iter()
            .map(|tag| tag.name)
            .collect(),
        workflow: Workflow::from_fields(row.custom_fields.unwrap_or_default()),
    }
}

fn device_from(row: DeviceRow) -> NbDevice {
    NbDevice {
        name: row.name,
        role: row.role.and_then(|role| role.name),
        status: row.status.and_then(|choice| choice.value),
    }
}

/// The site a prefix's own scope names. A scope of another type (a location, say) names no
/// site, and so does a prefix with no scope. The nested `site` object is used only when the
/// prefix carries no scope type.
fn prefix_site(row: &PrefixRow) -> Option<String> {
    match row.scope_type.as_deref() {
        Some("dcim.site") => row.scope.as_ref().and_then(|scope| scope.slug.clone()),
        Some(_) => None,
        None => row.site.as_ref().and_then(|site| site.slug.clone()),
    }
}

fn prefix_from(row: PrefixRow) -> NbPrefix {
    NbPrefix {
        site: prefix_site(&row),
        prefix: row.prefix,
        vlan: row.vlan.and_then(|vlan| vlan.vid),
        description: row.description.unwrap_or_default(),
    }
}

impl Workflow {
    /// The workflow view, or `None` when the VM carries none of the three fields. A
    /// non-object `ac_workflow_status` gives no steps; a non-string step status is `unknown`.
    fn from_fields(fields: WorkflowFields) -> Option<Workflow> {
        if fields.ac_workflow_status.is_none()
            && fields.ac_failed_steps.is_none()
            && fields.ac_last_run.is_none()
        {
            return None;
        }
        let steps = match fields.ac_workflow_status {
            Some(Value::Object(map)) => map
                .into_iter()
                .map(|(step, status)| {
                    let status = match status {
                        Value::String(text) => text,
                        _ => "unknown".to_string(),
                    };
                    (step, status)
                })
                .collect(),
            _ => BTreeMap::new(),
        };
        let failed = fields
            .ac_failed_steps
            .unwrap_or_default()
            .split(',')
            .map(str::trim)
            .filter(|step| !step.is_empty())
            .map(str::to_string)
            .collect();
        let last_run = fields.ac_last_run.filter(|stamp| !stamp.trim().is_empty());
        Some(Workflow {
            steps,
            failed,
            last_run,
        })
    }
}

#[cfg(test)]
pub mod fixtures {
    //! A NetBox stand-in on loopback. It answers only the bearer it expects, filters by the
    //! query parameters the client sends, and can be switched to fail, to ignore its filters,
    //! to hold a name twice across sites, or to serve a paged prefix list.

    use std::collections::HashMap;
    use std::net::SocketAddr;
    use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
    use std::sync::Arc;

    use dioxus::server::axum::extract::{Query, State};
    use dioxus::server::axum::http::{header, HeaderMap, StatusCode};
    use dioxus::server::axum::response::{IntoResponse, Response};
    use dioxus::server::axum::routing::get;
    use dioxus::server::axum::Router;
    use serde_json::Value;

    /// A v2 token, so the expected header is the `Bearer` form.
    pub const TOKEN: &str = "nbt_console-read-token-value";
    pub const EXPECTED_AUTH: &str = "Bearer nbt_console-read-token-value";

    pub const EMPTY: &str = r#"{"count":0,"next":null,"previous":null,"results":[]}"#;

    pub const SITE_DC1: &str = r#"{"count":1,"next":null,"previous":null,
        "results":[{"id":5,"name":"DC 1","slug":"dc-1"}]}"#;
    pub const SITE_DC2: &str = r#"{"count":1,"next":null,"previous":null,
        "results":[{"id":6,"name":"Lab","slug":"dc-2"}]}"#;
    /// A site whose cluster list reports a second page, to prove truncation is an error.
    pub const SITE_BIG: &str = r#"{"count":1,"next":null,"previous":null,
        "results":[{"id":99,"name":"Big","slug":"big-site"}]}"#;

    pub const CLUSTERS_SITE5: &str = r#"{"count":1,"next":null,"previous":null,
        "results":[{"id":7,"name":"pve-prod","site":{"id":5,"name":"DC 1","slug":"dc-1"},
                    "type":{"id":1,"name":"Proxmox"}}]}"#;
    /// The lab site's clusters. `pve-rack` is scoped to a location, though its legacy `site`
    /// names the lab: it is refused until the console resolves scope to site.
    pub const CLUSTERS_SITE6: &str = r#"{"count":2,"next":null,"previous":null,
        "results":[{"id":8,"name":"pve-lab","site":{"id":6,"name":"Lab","slug":"dc-2"},
                    "type":{"id":1,"name":"Proxmox"}},
                   {"id":10,"name":"pve-rack","site":{"id":6,"name":"Lab","slug":"dc-2"},
                    "scope_type":"dcim.location","scope_id":3,
                    "type":{"id":1,"name":"Proxmox"}}]}"#;
    pub const CLUSTERS_TRUNCATED: &str = r#"{"count":2,"next":"https://netbox.test/api/virtualization/clusters/?offset=1000","previous":null,
        "results":[{"id":9,"name":"pve-big","site":{"id":99,"name":"Big","slug":"big-site"},"type":{"id":1,"name":"Proxmox"}}]}"#;

    /// Five VMs of cluster 7, covering every RAG rule: `tududi` has a failed step, `gateway`
    /// is stopped while NetBox says active, `planned-vm` is planned, `wiki` is clean and
    /// running, `ghost` has no live match. `vault_note` must never leave the console.
    pub const VMS_CLUSTER7: &str = r#"{"count":5,"next":null,"previous":null,"results":[
        {"id":101,"name":"tududi","status":{"value":"active","label":"Active","id":1},
         "vcpus":2.0,"memory":4096,"disk":40960,
         "primary_ip":{"id":9,"address":"198.51.100.5/24","family":{"value":4,"label":"IPv4"}},
         "tags":[{"id":1,"name":"tududi-sync","slug":"tududi-sync"}],
         "cluster":{"id":7,"name":"pve-prod"},
         "custom_fields":{"ac_workflow_status":{"step-a":"pass","step-b":"fail"},
                          "ac_failed_steps":"step-b","ac_last_run":"2026-10-26T12:00:00Z",
                          "vault_note":"hunter2-custom-value"}},
        {"id":102,"name":"gateway","status":{"value":"active"},"vcpus":1.0,"memory":2048,"disk":20480,
         "primary_ip":null,"tags":[],"cluster":{"id":7,"name":"pve-prod"},"custom_fields":{}},
        {"id":103,"name":"planned-vm","status":{"value":"planned"},"vcpus":1.0,"memory":1024,"disk":10240,
         "primary_ip":null,"tags":[],"cluster":{"id":7,"name":"pve-prod"},
         "custom_fields":{"ac_workflow_status":{"step-a":"pass"},"ac_failed_steps":"","ac_last_run":null}},
        {"id":104,"name":"wiki","status":{"value":"active"},"vcpus":2.0,"memory":4096,"disk":30720,
         "primary_ip":{"id":10,"address":"198.51.100.8/24"},"tags":[],
         "cluster":{"id":7,"name":"pve-prod"},"custom_fields":{}},
        {"id":105,"name":"ghost","status":{"value":"active"},"vcpus":null,"memory":null,"disk":null,
         "primary_ip":null,"tags":null,"cluster":{"id":7,"name":"pve-prod"},"custom_fields":null}
    ]}"#;

    /// The lab cluster's VM: a name no Proxmox guest carries, so it never joins.
    pub const VMS_CLUSTER8: &str = r#"{"count":1,"next":null,"previous":null,"results":[
        {"id":201,"name":"lab-wiki","status":{"value":"active"},"vcpus":1.0,"memory":1024,"disk":10240,
         "primary_ip":null,"tags":[],"cluster":{"id":8,"name":"pve-lab"},"custom_fields":{}}
    ]}"#;

    /// The lab cluster with a second VM named `wiki`, the same name as a prod VM, so the name
    /// is held twice across tenants.
    pub const VMS_CLUSTER8_DUP: &str = r#"{"count":2,"next":null,"previous":null,"results":[
        {"id":201,"name":"lab-wiki","status":{"value":"active"},"vcpus":1.0,"memory":1024,"disk":10240,
         "primary_ip":null,"tags":[],"cluster":{"id":8,"name":"pve-lab"},"custom_fields":{}},
        {"id":202,"name":"wiki","status":{"value":"active"},"vcpus":1.0,"memory":1024,"disk":10240,
         "primary_ip":null,"tags":[],"cluster":{"id":8,"name":"pve-lab"},"custom_fields":{}}
    ]}"#;

    pub const DEVICES_SITE5: &str = r#"{"count":2,"next":null,"previous":null,"results":[
        {"id":21,"name":"pve-01","role":{"id":1,"name":"Hypervisor","slug":"hypervisor"},"status":{"value":"active","label":"Active"},"site":{"id":5,"name":"DC 1"}},
        {"id":22,"name":null,"role":null,"status":null,"site":{"id":5,"name":"DC 1"}}
    ]}"#;
    pub const DEVICES_SITE6: &str = r#"{"count":1,"next":null,"previous":null,"results":[
        {"id":23,"name":"lab-01","role":{"id":2,"name":"Lab host","slug":"lab-host"},"status":{"value":"active"},"site":{"id":6,"name":"Lab"}}
    ]}"#;

    /// Every prefix, unfiltered. Row 31 is scoped to site dc-1; row 32 has no scope.
    pub const PREFIXES_ALL: &str = r#"{"count":2,"next":null,"previous":null,"results":[
        {"id":31,"prefix":"198.51.100.0/24","vlan":{"id":3,"vid":20,"name":"mgmt"},"description":"management",
         "scope_type":"dcim.site","scope":{"id":5,"slug":"dc-1","name":"DC 1"}},
        {"id":32,"prefix":"192.0.2.0/24","vlan":null,"description":"documentation",
         "scope_type":null,"scope":null}
    ]}"#;

    #[derive(Clone, Default)]
    pub struct Mock {
        pub fail: Arc<AtomicBool>,
        /// Fail only the prefix list, as a prefix-only outage would.
        pub fail_prefixes: Arc<AtomicBool>,
        /// Answer every list with every fixture row, ignoring the query, as a NetBox that
        /// dropped a filter would.
        pub ignore_filters: Arc<AtomicBool>,
        /// Give the lab cluster a second VM named `wiki`.
        pub lab_wiki_duplicate: Arc<AtomicBool>,
        /// When non-zero, the prefix list is this many synthetic rows, paged by `offset`.
        pub prefix_total: Arc<AtomicUsize>,
    }

    fn auth(headers: &HeaderMap) -> String {
        headers
            .get(header::AUTHORIZATION)
            .and_then(|value| value.to_str().ok())
            .unwrap_or("")
            .to_string()
    }

    /// The failure answer when switched on, else the refusal for a wrong bearer. The 500
    /// echoes the header it got, so a leak would show in the error text.
    fn refuse(mock: &Mock, headers: &HeaderMap) -> Option<Response> {
        if mock.fail.load(Ordering::SeqCst) {
            return Some(
                (
                    StatusCode::INTERNAL_SERVER_ERROR,
                    format!("boom {}", auth(headers)),
                )
                    .into_response(),
            );
        }
        if auth(headers) != EXPECTED_AUTH {
            return Some(StatusCode::FORBIDDEN.into_response());
        }
        None
    }

    fn answer(body: impl Into<String>) -> Response {
        (StatusCode::OK, body.into()).into_response()
    }

    /// Every result row of the given pages, in one page. Used when filters are ignored.
    fn merged(bodies: &[&str]) -> String {
        let mut rows = Vec::new();
        for body in bodies {
            let page: Value = serde_json::from_str(body).expect("fixture json");
            rows.extend(page["results"].as_array().expect("results").iter().cloned());
        }
        serde_json::json!({"count": rows.len(), "next": null, "previous": null, "results": rows})
            .to_string()
    }

    /// The lab cluster's VMs, with or without the duplicate `wiki`.
    fn lab_vms(mock: &Mock) -> &'static str {
        if mock.lab_wiki_duplicate.load(Ordering::SeqCst) {
            VMS_CLUSTER8_DUP
        } else {
            VMS_CLUSTER8
        }
    }

    type Q = Query<HashMap<String, String>>;

    async fn sites(State(mock): State<Mock>, headers: HeaderMap, Query(q): Q) -> Response {
        if let Some(refused) = refuse(&mock, &headers) {
            return refused;
        }
        if mock.ignore_filters.load(Ordering::SeqCst) {
            return answer(merged(&[SITE_DC1, SITE_DC2]));
        }
        answer(match q.get("slug").map(String::as_str) {
            Some("dc-1") => SITE_DC1,
            Some("dc-2") => SITE_DC2,
            Some("big-site") => SITE_BIG,
            _ => EMPTY,
        })
    }

    async fn clusters(State(mock): State<Mock>, headers: HeaderMap, Query(q): Q) -> Response {
        if let Some(refused) = refuse(&mock, &headers) {
            return refused;
        }
        if mock.ignore_filters.load(Ordering::SeqCst) {
            return answer(merged(&[CLUSTERS_SITE5, CLUSTERS_SITE6]));
        }
        answer(match q.get("site_id").map(String::as_str) {
            Some("5") => CLUSTERS_SITE5,
            Some("6") => CLUSTERS_SITE6,
            Some("99") => CLUSTERS_TRUNCATED,
            _ => EMPTY,
        })
    }

    async fn vms(State(mock): State<Mock>, headers: HeaderMap, Query(q): Q) -> Response {
        if let Some(refused) = refuse(&mock, &headers) {
            return refused;
        }
        if mock.ignore_filters.load(Ordering::SeqCst) {
            return answer(merged(&[VMS_CLUSTER7, lab_vms(&mock)]));
        }
        answer(match q.get("cluster_id").map(String::as_str) {
            Some("7") => VMS_CLUSTER7,
            Some("8") => lab_vms(&mock),
            _ => EMPTY,
        })
    }

    async fn devices(State(mock): State<Mock>, headers: HeaderMap, Query(q): Q) -> Response {
        if let Some(refused) = refuse(&mock, &headers) {
            return refused;
        }
        if mock.ignore_filters.load(Ordering::SeqCst) {
            return answer(merged(&[DEVICES_SITE5, DEVICES_SITE6]));
        }
        answer(match q.get("site_id").map(String::as_str) {
            Some("5") => DEVICES_SITE5,
            Some("6") => DEVICES_SITE6,
            _ => EMPTY,
        })
    }

    async fn prefixes(State(mock): State<Mock>, headers: HeaderMap, Query(q): Q) -> Response {
        if let Some(refused) = refuse(&mock, &headers) {
            return refused;
        }
        if mock.fail_prefixes.load(Ordering::SeqCst) {
            return (StatusCode::INTERNAL_SERVER_ERROR, "prefix boom").into_response();
        }
        let total = mock.prefix_total.load(Ordering::SeqCst);
        if total == 0 {
            return answer(PREFIXES_ALL);
        }
        let offset: usize = q.get("offset").and_then(|v| v.parse().ok()).unwrap_or(0);
        let limit: usize = q.get("limit").and_then(|v| v.parse().ok()).unwrap_or(25);
        let end = (offset + limit).min(total);
        let rows: Vec<String> = (offset..end)
            .map(|i| {
                format!(
                    r#"{{"id":{i},"prefix":"2001:db8:{i:x}::/64","vlan":null,"description":"synthetic","scope_type":null,"scope":null}}"#
                )
            })
            .collect();
        let next = if end < total {
            format!("\"https://netbox.test/api/ipam/prefixes/?offset={end}&limit={limit}\"")
        } else {
            "null".to_string()
        };
        answer(format!(
            r#"{{"count":{total},"next":{next},"previous":null,"results":[{}]}}"#,
            rows.join(",")
        ))
    }

    pub fn router(mock: Mock) -> Router {
        Router::new()
            .route("/api/dcim/sites/", get(sites))
            .route("/api/virtualization/clusters/", get(clusters))
            .route("/api/virtualization/virtual-machines/", get(vms))
            .route("/api/dcim/devices/", get(devices))
            .route("/api/ipam/prefixes/", get(prefixes))
            .with_state(mock)
    }

    /// Serves `router` on loopback and returns its address.
    pub async fn serve(app: Router) -> SocketAddr {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind");
        let addr = listener.local_addr().expect("addr");
        tokio::spawn(async move {
            dioxus::server::axum::serve(listener, app)
                .await
                .expect("serve");
        });
        addr
    }

    pub fn config_for(addr: SocketAddr) -> super::super::config::NetboxConfig {
        super::super::config::NetboxConfig {
            api_url: format!("http://{addr}"),
            token: super::super::config::Secret::new(TOKEN),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::fixtures::{config_for, router, serve, Mock, EXPECTED_AUTH, TOKEN};
    use super::*;
    use std::sync::atomic::Ordering;

    fn slugs(list: &[&str]) -> Vec<String> {
        list.iter().map(|slug| slug.to_string()).collect()
    }

    #[test]
    fn v2_tokens_are_bearer_and_legacy_tokens_are_token() {
        assert_eq!(authorization("nbt_abc.def"), "Bearer nbt_abc.def");
        assert_eq!(authorization("0123456789abcdef"), "Token 0123456789abcdef");
    }

    #[test]
    fn every_failure_text_is_fixed_and_names_no_token() {
        for err in [
            FetchError::Timeout,
            FetchError::Connect,
            FetchError::Status(401),
            FetchError::Decode,
            FetchError::Truncated,
            FetchError::Other,
        ] {
            let text = err.to_string();
            assert!(text.contains("NetBox"), "{text}");
            assert!(!text.contains(TOKEN) && !text.contains("nbt_"), "{text}");
        }
        assert_eq!(
            FetchError::Status(500).to_string(),
            "NetBox answered HTTP 500"
        );
    }

    #[tokio::test]
    async fn fetch_all_reads_the_site_tree_with_the_bearer_header() {
        let addr = serve(router(Mock::default())).await;
        let data = fetch_all(
            &reqwest::Client::new(),
            &config_for(addr),
            &slugs(&["dc-1"]),
        )
        .await
        .expect("fetch");
        let site = data.sites.get("dc-1").expect("dc-1 read");
        assert_eq!(site.clusters.len(), 1);
        assert_eq!(site.clusters[0].name, "pve-prod");
        let names: Vec<&str> = site.clusters[0]
            .vms
            .iter()
            .map(|vm| vm.name.as_str())
            .collect();
        assert_eq!(names, ["tududi", "gateway", "planned-vm", "wiki", "ghost"]);

        let tududi = &site.clusters[0].vms[0];
        assert_eq!(tududi.status, "active");
        assert_eq!(tududi.vcpus, Some(2.0));
        assert_eq!(tududi.memory_mb, Some(4096));
        assert_eq!(tududi.disk_mb, Some(40960));
        assert_eq!(tududi.primary_ip.as_deref(), Some("198.51.100.5/24"));
        assert_eq!(tududi.tags, ["tududi-sync"]);
        let workflow = tududi.workflow.as_ref().expect("workflow");
        assert_eq!(
            workflow.steps.get("step-b").map(String::as_str),
            Some("fail")
        );
        assert_eq!(workflow.failed, ["step-b"]);
        assert_eq!(workflow.last_run.as_deref(), Some("2026-10-26T12:00:00Z"));

        assert!(site.clusters[0].vms[1].workflow.is_none(), "no ac_ fields");
        assert!(
            site.clusters[0].vms[4].tags.is_empty(),
            "null tags read empty"
        );

        assert_eq!(site.devices.len(), 2);
        assert_eq!(
            site.devices[0],
            NbDevice {
                name: Some("pve-01".into()),
                role: Some("Hypervisor".into()),
                status: Some("active".into()),
            }
        );
        assert_eq!(
            site.devices[1],
            NbDevice {
                name: None,
                role: None,
                status: None,
            }
        );
        assert_eq!(data.dropped_mismatched, 0, "an honest server drops nothing");
        assert!(data.ambiguous_vm_names.is_empty());
    }

    #[tokio::test]
    async fn an_unknown_slug_is_unmapped_not_an_error() {
        let addr = serve(router(Mock::default())).await;
        let data = fetch_all(
            &reqwest::Client::new(),
            &config_for(addr),
            &slugs(&["missing", "dc-1"]),
        )
        .await
        .expect("fetch");
        assert!(!data.sites.contains_key("missing"));
        assert!(data.sites.contains_key("dc-1"));
    }

    #[tokio::test]
    async fn filtered_rows_from_a_server_that_ignores_filters_are_dropped_and_counted() {
        let mock = Mock::default();
        mock.ignore_filters.store(true, Ordering::SeqCst);
        let addr = serve(router(mock)).await;
        let data = fetch_all(
            &reqwest::Client::new(),
            &config_for(addr),
            &slugs(&["dc-1"]),
        )
        .await
        .expect("fetch");
        // One row each dropped: the dc-2 site, the lab cluster and the rack cluster, the lab VM,
        // the lab device.
        assert_eq!(data.dropped_mismatched, 5);
        let site = &data.sites["dc-1"];
        assert_eq!(site.clusters.len(), 1, "only the site's own cluster");
        assert_eq!(site.clusters[0].name, "pve-prod");
        assert!(site.clusters[0].vms.iter().all(|vm| vm.name != "lab-wiki"));
        assert_eq!(site.clusters[0].vms.len(), 5);
        assert_eq!(site.devices.len(), 2);
        assert!(site
            .devices
            .iter()
            .all(|d| d.name.as_deref() != Some("lab-01")));
    }

    #[tokio::test]
    async fn a_name_held_twice_across_declared_sites_is_ambiguous() {
        let mock = Mock::default();
        mock.lab_wiki_duplicate.store(true, Ordering::SeqCst);
        let addr = serve(router(mock)).await;
        let data = fetch_all(
            &reqwest::Client::new(),
            &config_for(addr),
            &slugs(&["dc-1", "dc-2"]),
        )
        .await
        .expect("fetch");
        assert!(data.ambiguous_vm_names.contains("wiki"));
        assert!(!data.ambiguous_vm_names.contains("tududi"));
    }

    #[tokio::test]
    async fn a_cluster_scoped_to_a_location_is_dropped_and_counted() {
        let addr = serve(router(Mock::default())).await;
        let data = fetch_all(
            &reqwest::Client::new(),
            &config_for(addr),
            &slugs(&["dc-2"]),
        )
        .await
        .expect("fetch");
        let names: Vec<&str> = data.sites["dc-2"]
            .clusters
            .iter()
            .map(|cluster| cluster.name.as_str())
            .collect();
        assert_eq!(names, ["pve-lab"], "the rack cluster is not shown");
        assert_eq!(data.dropped_mismatched, 1, "and it is counted");
    }

    #[test]
    fn a_cluster_is_judged_by_its_scope_when_it_has_one() {
        let row = |json: &str| -> ClusterRow { serde_json::from_str(json).expect("cluster") };
        assert!(
            row(r#"{"id":1,"name":"a","scope_type":"dcim.site","scope_id":5}"#).belongs_to_site(5)
        );
        assert!(
            !row(r#"{"id":1,"name":"a","scope_type":"dcim.site","scope_id":6}"#).belongs_to_site(5)
        );
        assert!(
            !row(
                r#"{"id":1,"name":"a","scope_type":"dcim.location","scope_id":5,"site":{"id":5}}"#
            )
            .belongs_to_site(5),
            "a location scope is refused even when the legacy site matches"
        );
        assert!(
            !row(r#"{"id":1,"name":"a","scope_type":"dcim.region","scope_id":5}"#)
                .belongs_to_site(5)
        );
        assert!(row(r#"{"id":1,"name":"a","site":{"id":5}}"#).belongs_to_site(5));
        assert!(
            !row(r#"{"id":1,"name":"a"}"#).belongs_to_site(5),
            "no site named, no match"
        );
    }

    #[tokio::test]
    async fn a_refused_bearer_is_a_status_error_without_the_token() {
        let addr = serve(router(Mock::default())).await;
        let mut config = config_for(addr);
        config.token = super::super::config::Secret::new("0000000000000000wrong");
        let err = fetch_all(&reqwest::Client::new(), &config, &slugs(&["dc-1"]))
            .await
            .expect_err("must fail");
        assert_eq!(err, FetchError::Status(403));
        assert!(!err.to_string().contains("wrong"));
    }

    #[tokio::test]
    async fn a_failing_server_is_a_status_error_whose_body_is_not_kept() {
        let mock = Mock::default();
        mock.fail.store(true, Ordering::SeqCst);
        let addr = serve(router(mock)).await;
        let err = fetch_all(
            &reqwest::Client::new(),
            &config_for(addr),
            &slugs(&["dc-1"]),
        )
        .await
        .expect_err("must fail");
        assert_eq!(err, FetchError::Status(500));
        let text = err.to_string();
        assert!(!text.contains("boom"), "{text}");
        assert!(!text.contains(EXPECTED_AUTH), "{text}");
    }

    #[tokio::test]
    async fn a_list_with_a_next_page_fails_instead_of_truncating() {
        let addr = serve(router(Mock::default())).await;
        let err = fetch_all(
            &reqwest::Client::new(),
            &config_for(addr),
            &slugs(&["big-site"]),
        )
        .await
        .expect_err("must fail");
        assert_eq!(err, FetchError::Truncated);
    }

    #[tokio::test]
    async fn connection_refused_is_a_connect_error() {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").expect("bind");
        let addr = listener.local_addr().expect("addr");
        drop(listener);
        let err = fetch_all(
            &reqwest::Client::new(),
            &config_for(addr),
            &slugs(&["dc-1"]),
        )
        .await
        .expect_err("must fail");
        assert_eq!(err, FetchError::Connect);
    }

    #[tokio::test]
    async fn prefixes_are_read_across_offset_pages_to_the_end() {
        let mock = Mock::default();
        mock.prefix_total.store(2500, Ordering::SeqCst);
        let addr = serve(router(mock)).await;
        let data = fetch_prefixes(&reqwest::Client::new(), &config_for(addr))
            .await
            .expect("fetch");
        assert_eq!(data.prefixes.len(), 2500);
        assert!(!data.truncated);
        assert_eq!(data.prefixes[2499].prefix, "2001:db8:9c3::/64");
    }

    #[tokio::test]
    async fn prefix_paging_stops_at_the_cap_and_says_so() {
        let mock = Mock::default();
        mock.prefix_total.store(25_000, Ordering::SeqCst);
        let addr = serve(router(mock)).await;
        let data = fetch_prefixes(&reqwest::Client::new(), &config_for(addr))
            .await
            .expect("fetch");
        assert_eq!(data.prefixes.len(), PREFIX_PAGE_CAP * 1000);
        assert!(data.truncated);
    }

    #[tokio::test]
    async fn a_prefix_failure_is_its_own_error() {
        let mock = Mock::default();
        mock.fail_prefixes.store(true, Ordering::SeqCst);
        let addr = serve(router(mock)).await;
        let err = fetch_prefixes(&reqwest::Client::new(), &config_for(addr))
            .await
            .expect_err("must fail");
        assert_eq!(err, FetchError::Status(500));
    }

    #[tokio::test]
    async fn prefix_site_comes_from_the_prefix_own_scope() {
        let addr = serve(router(Mock::default())).await;
        let data = fetch_prefixes(&reqwest::Client::new(), &config_for(addr))
            .await
            .expect("fetch");
        let scoped = data
            .prefixes
            .iter()
            .find(|p| p.prefix == "198.51.100.0/24")
            .expect("scoped");
        assert_eq!(scoped.site.as_deref(), Some("dc-1"));
        assert_eq!(scoped.vlan, Some(20));
        assert_eq!(scoped.description, "management");
        let global = data
            .prefixes
            .iter()
            .find(|p| p.prefix == "192.0.2.0/24")
            .expect("global");
        assert_eq!(global.site, None, "no scope, no site");
    }

    fn prefix(json: &str) -> PrefixRow {
        serde_json::from_str(json).expect("prefix row")
    }

    #[test]
    fn prefix_site_takes_only_a_site_scope_or_the_legacy_site() {
        let location =
            prefix(r#"{"prefix":"a","scope_type":"dcim.location","scope":{"slug":"rack-a"}}"#);
        assert_eq!(prefix_site(&location), None, "a location names no site");
        let legacy = prefix(r#"{"prefix":"b","site":{"id":5,"slug":"dc-1"}}"#);
        assert_eq!(prefix_site(&legacy).as_deref(), Some("dc-1"));
        // The field is a slug: a scope with a name and no slug gives no site, not the name.
        let named_only =
            prefix(r#"{"prefix":"c","scope_type":"dcim.site","scope":{"name":"DC 2"}}"#);
        assert_eq!(prefix_site(&named_only), None);
    }

    fn fields(json: &str) -> WorkflowFields {
        serde_json::from_str(json).expect("fields")
    }

    #[test]
    fn workflow_is_absent_without_any_ac_field() {
        assert!(Workflow::from_fields(fields(r#"{"unrelated":"x"}"#)).is_none());
        assert!(Workflow::from_fields(fields(r#"{"ac_last_run":null}"#)).is_none());
    }

    #[test]
    fn workflow_splits_failed_steps_and_marks_non_string_status_unknown() {
        let workflow = Workflow::from_fields(fields(
            r#"{"ac_workflow_status":{"a":"pass","b":"fail","c":7},
                "ac_failed_steps":" b , d ,,","ac_last_run":""}"#,
        ))
        .expect("workflow");
        assert_eq!(workflow.failed, ["b", "d"]);
        assert_eq!(workflow.steps["c"], "unknown");
        assert_eq!(workflow.last_run, None, "an empty stamp is no stamp");
    }

    #[test]
    fn a_non_object_status_gives_no_steps_but_still_a_workflow() {
        let workflow =
            Workflow::from_fields(fields(r#"{"ac_workflow_status":"oops"}"#)).expect("workflow");
        assert!(workflow.steps.is_empty());
        assert!(workflow.failed.is_empty());
    }
}
