//! Read-only NetBox client for the inventory. Only the refresh task calls it; the browser
//! never talks to NetBox. Errors are fixed variants: no URL, no response body and no header
//! reaches a message or a log line.
//!
//! Each list is one page (`limit`). A list that reports a next page fails the fetch with
//! `Truncated` rather than showing a partial inventory as complete. Of a VM's custom fields
//! only the three `ac_*` workflow fields are read (`WorkflowFields`); every other key is
//! discarded by serde when the response is decoded.

use std::collections::BTreeMap;
use std::fmt;

use serde::de::DeserializeOwned;
use serde::{Deserialize, Serialize};
use serde_json::Value;

use super::config::NetboxConfig;

/// `limit` sent with every list request. One page per request, see `Truncated`.
pub const PAGE_LIMIT: &str = "1000";

/// Everything the refresh reads from NetBox, keyed by site slug. A slug NetBox does not
/// hold has no entry: the site is unmapped, which is not a read failure.
#[derive(Debug, Clone, Default)]
pub struct NetboxData {
    pub sites: BTreeMap<String, NbSite>,
    /// Every prefix NetBox holds, global ones included. Admin-only (`GET /v1/network`).
    pub prefixes: Vec<NbPrefix>,
}

/// A device at a site. Site-scoped, so it is safe in a tenant answer.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct NbDevice {
    pub name: Option<String>,
    /// The device role's name.
    pub role: Option<String>,
    pub status: Option<String>,
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
    pub disk_gb: Option<u64>,
    /// The primary address as NetBox holds it, prefix length included. Never the other addresses.
    pub primary_ip: Option<String>,
    pub tags: Vec<String>,
    /// `None` when the VM carries none of the workflow fields, so it is not a service.
    pub workflow: Option<Workflow>,
}

/// The conformance collector's view of one VM. Step statuses are the collector's own
/// words (`pass`, `fail`, `skip`); anything else arrives as `unknown`.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Workflow {
    pub steps: BTreeMap<String, String>,
    /// `ac_failed_steps` split on commas, as written.
    pub failed: Vec<String>,
    pub last_run: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct NbPrefix {
    pub prefix: String,
    /// The VLAN id (`vid`), not its name.
    pub vlan: Option<u64>,
    pub description: String,
    /// The NetBox slug of the site the prefix is scoped to, or `None` for a global prefix.
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

/// Reads every declared site and its clusters with their VMs, then every prefix NetBox
/// holds. A prefix is attributed to a declared site when the site's own prefix list
/// names it; a prefix no declared site lists is global (`site: None`). A slug NetBox does
/// not hold is skipped. Any failed request fails the whole read, so one refresh is either
/// complete or not used.
pub async fn fetch_all(
    http: &reqwest::Client,
    config: &NetboxConfig,
    slugs: &[String],
) -> Result<NetboxData, FetchError> {
    let mut sites = BTreeMap::new();
    let mut site_of_prefix: BTreeMap<u64, String> = BTreeMap::new();
    for slug in slugs {
        let found: Vec<SiteRow> =
            list(http, config, "/api/dcim/sites/", &[("slug", slug.clone())]).await?;
        let Some(site) = found.first() else {
            continue;
        };
        sites.insert(slug.clone(), fetch_site(http, config, site.id).await?);
        let scoped: Vec<PrefixRow> = list(
            http,
            config,
            "/api/ipam/prefixes/",
            &[("site_id", site.id.to_string())],
        )
        .await?;
        for prefix in scoped {
            site_of_prefix.insert(prefix.id, slug.clone());
        }
    }
    let all: Vec<PrefixRow> = list(http, config, "/api/ipam/prefixes/", &[]).await?;
    let prefixes = all
        .into_iter()
        .map(|row| {
            let site = site_of_prefix.get(&row.id).cloned();
            prefix_from(row, site)
        })
        .collect();
    Ok(NetboxData { sites, prefixes })
}

async fn fetch_site(
    http: &reqwest::Client,
    config: &NetboxConfig,
    site_id: u64,
) -> Result<NbSite, FetchError> {
    let by_site = [("site_id", site_id.to_string())];
    let cluster_rows: Vec<ClusterRow> =
        list(http, config, "/api/virtualization/clusters/", &by_site).await?;
    let mut clusters = Vec::with_capacity(cluster_rows.len());
    for cluster in cluster_rows {
        let vms: Vec<VmRow> = list(
            http,
            config,
            "/api/virtualization/virtual-machines/",
            &[("cluster_id", cluster.id.to_string())],
        )
        .await?;
        clusters.push(NbCluster {
            name: cluster.name,
            vms: vms.into_iter().map(vm_from).collect(),
        });
    }
    let devices: Vec<DeviceRow> = list(http, config, "/api/dcim/devices/", &by_site).await?;
    Ok(NbSite {
        clusters,
        devices: devices.into_iter().map(device_from).collect(),
    })
}

async fn list<T: DeserializeOwned>(
    http: &reqwest::Client,
    config: &NetboxConfig,
    path: &str,
    filters: &[(&str, String)],
) -> Result<Vec<T>, FetchError> {
    let mut query = filters.to_vec();
    query.push(("limit", PAGE_LIMIT.to_string()));
    let response = http
        .get(format!("{}{path}", config.api_url))
        .header(
            reqwest::header::AUTHORIZATION,
            authorization(config.token.expose()),
        )
        .header(reqwest::header::ACCEPT, "application/json")
        .query(&query)
        .send()
        .await
        .map_err(transport_error)?;
    let status = response.status();
    if !status.is_success() {
        return Err(FetchError::Status(status.as_u16()));
    }
    let body = response.bytes().await.map_err(transport_error)?;
    let page: Page<T> = serde_json::from_slice(&body).map_err(|_| FetchError::Decode)?;
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

#[derive(Deserialize)]
struct SiteRow {
    id: u64,
}

#[derive(Deserialize)]
struct ClusterRow {
    id: u64,
    name: String,
}

/// NetBox choice field: `{"value": ..., "label": ..., "id": ...}`.
#[derive(Deserialize)]
struct Choice {
    value: Option<String>,
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
    custom_fields: Option<WorkflowFields>,
}

/// The three workflow fields the conformance collector writes (`netbox-custom-fields.yml`).
#[derive(Deserialize, Default)]
struct WorkflowFields {
    ac_workflow_status: Option<Value>,
    ac_failed_steps: Option<String>,
    ac_last_run: Option<String>,
}

#[derive(Deserialize)]
struct NameRef {
    name: Option<String>,
}

#[derive(Deserialize)]
struct DeviceRow {
    name: Option<String>,
    role: Option<NameRef>,
    status: Option<Choice>,
}

#[derive(Deserialize)]
struct PrefixRow {
    id: u64,
    prefix: String,
    vlan: Option<VlanRef>,
    description: Option<String>,
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
        disk_gb: row.disk,
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

fn prefix_from(row: PrefixRow, site: Option<String>) -> NbPrefix {
    NbPrefix {
        prefix: row.prefix,
        vlan: row.vlan.and_then(|vlan| vlan.vid),
        description: row.description.unwrap_or_default(),
        site,
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
    //! A NetBox stand-in on loopback. Answers only the bearer it expects, filters by the
    //! query parameters the client sends, and can be switched to fail every request.

    use std::collections::HashMap;
    use std::net::SocketAddr;
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::Arc;

    use dioxus::server::axum::extract::{Query, State};
    use dioxus::server::axum::http::{header, HeaderMap, StatusCode};
    use dioxus::server::axum::response::{IntoResponse, Response};
    use dioxus::server::axum::routing::get;
    use dioxus::server::axum::Router;

    /// A v2 token, so the expected header is the `Bearer` form.
    pub const TOKEN: &str = "nbt_console-read-token-value";
    pub const EXPECTED_AUTH: &str = "Bearer nbt_console-read-token-value";

    pub const EMPTY: &str = r#"{"count":0,"next":null,"previous":null,"results":[]}"#;

    pub const SITE_DC1: &str = r#"{"count":1,"next":null,"previous":null,
        "results":[{"id":5,"name":"DC 1","slug":"dc-1"}]}"#;
    /// A site whose cluster list reports a second page, to prove truncation is an error.
    pub const SITE_BIG: &str = r#"{"count":1,"next":null,"previous":null,
        "results":[{"id":99,"name":"Big","slug":"big-site"}]}"#;

    pub const CLUSTERS_SITE5: &str = r#"{"count":1,"next":null,"previous":null,
        "results":[{"id":7,"name":"pve-prod","type":{"id":1,"name":"Proxmox"}}]}"#;
    pub const CLUSTERS_TRUNCATED: &str = r#"{"count":2,"next":"https://netbox.test/api/virtualization/clusters/?offset=1000","previous":null,
        "results":[{"id":8,"name":"pve-big","type":{"id":1,"name":"Proxmox"}}]}"#;

    /// Five VMs of cluster 7, covering every RAG rule: `tududi` has a failed step,
    /// `gateway` is stopped while NetBox says active, `planned-vm` is planned, `wiki` is
    /// clean and running, `ghost` has no live match. `vault_note` must never leave the console.
    pub const VMS_CLUSTER7: &str = r#"{"count":5,"next":null,"previous":null,"results":[
        {"id":101,"name":"tududi","status":{"value":"active","label":"Active","id":1},
         "vcpus":2.0,"memory":4096,"disk":40,
         "primary_ip":{"id":9,"address":"198.51.100.5/24","family":{"value":4,"label":"IPv4"}},
         "tags":[{"id":1,"name":"tududi-sync","slug":"tududi-sync"}],
         "cluster":{"id":7,"name":"pve-prod"},
         "custom_fields":{"ac_workflow_status":{"step-a":"pass","step-b":"fail"},
                          "ac_failed_steps":"step-b","ac_last_run":"2026-10-26T12:00:00Z",
                          "vault_note":"hunter2-custom-value"}},
        {"id":102,"name":"gateway","status":{"value":"active"},"vcpus":1.0,"memory":2048,"disk":20,
         "primary_ip":null,"tags":[],"custom_fields":{}},
        {"id":103,"name":"planned-vm","status":{"value":"planned"},"vcpus":1.0,"memory":1024,"disk":10,
         "primary_ip":null,"tags":[],
         "custom_fields":{"ac_workflow_status":{"step-a":"pass"},"ac_failed_steps":"","ac_last_run":null}},
        {"id":104,"name":"wiki","status":{"value":"active"},"vcpus":2.0,"memory":4096,"disk":30,
         "primary_ip":{"id":10,"address":"198.51.100.8/24"},"tags":[],"custom_fields":{}},
        {"id":105,"name":"ghost","status":{"value":"active"},"vcpus":null,"memory":null,"disk":null,
         "primary_ip":null,"tags":null,"custom_fields":null}
    ]}"#;

    /// Devices at site 5: one named hypervisor, one unnamed device with no role or status.
    pub const DEVICES_SITE5: &str = r#"{"count":2,"next":null,"previous":null,"results":[
        {"id":21,"name":"pve-01","role":{"id":1,"name":"Hypervisor","slug":"hypervisor"},"status":{"value":"active","label":"Active"}},
        {"id":22,"name":null,"role":null,"status":null}
    ]}"#;

    /// Prefixes scoped to site 5 (id 31), as `?site_id=5` lists them.
    pub const PREFIXES_SITE5: &str = r#"{"count":1,"next":null,"previous":null,"results":[
        {"id":31,"prefix":"198.51.100.0/24","vlan":{"id":3,"vid":20,"name":"mgmt"},"description":"management"}
    ]}"#;

    /// Every prefix, unfiltered: the site-5 one plus a global one (id 32) with no site.
    pub const PREFIXES_ALL: &str = r#"{"count":2,"next":null,"previous":null,"results":[
        {"id":31,"prefix":"198.51.100.0/24","vlan":{"id":3,"vid":20,"name":"mgmt"},"description":"management"},
        {"id":32,"prefix":"192.0.2.0/24","vlan":null,"description":"documentation"}
    ]}"#;

    #[derive(Clone, Default)]
    pub struct Mock {
        pub fail: Arc<AtomicBool>,
    }

    fn auth(headers: &HeaderMap) -> String {
        headers
            .get(header::AUTHORIZATION)
            .and_then(|value| value.to_str().ok())
            .unwrap_or("")
            .to_string()
    }

    /// The failure answer when switched on, else the refusal for a wrong bearer.
    /// The 500 echoes the header it got, so a leak would show in the error text.
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

    fn answer(body: &'static str) -> Response {
        (StatusCode::OK, body).into_response()
    }

    type Q = Query<HashMap<String, String>>;

    async fn sites(State(mock): State<Mock>, headers: HeaderMap, Query(q): Q) -> Response {
        if let Some(refused) = refuse(&mock, &headers) {
            return refused;
        }
        answer(match q.get("slug").map(String::as_str) {
            Some("dc-1") => SITE_DC1,
            Some("big-site") => SITE_BIG,
            _ => EMPTY,
        })
    }

    async fn clusters(State(mock): State<Mock>, headers: HeaderMap, Query(q): Q) -> Response {
        if let Some(refused) = refuse(&mock, &headers) {
            return refused;
        }
        answer(match q.get("site_id").map(String::as_str) {
            Some("5") => CLUSTERS_SITE5,
            Some("99") => CLUSTERS_TRUNCATED,
            _ => EMPTY,
        })
    }

    async fn vms(State(mock): State<Mock>, headers: HeaderMap, Query(q): Q) -> Response {
        if let Some(refused) = refuse(&mock, &headers) {
            return refused;
        }
        answer(match q.get("cluster_id").map(String::as_str) {
            Some("7") => VMS_CLUSTER7,
            _ => EMPTY,
        })
    }

    async fn devices(State(mock): State<Mock>, headers: HeaderMap, Query(q): Q) -> Response {
        if let Some(refused) = refuse(&mock, &headers) {
            return refused;
        }
        answer(match q.get("site_id").map(String::as_str) {
            Some("5") => DEVICES_SITE5,
            _ => EMPTY,
        })
    }

    async fn prefixes(State(mock): State<Mock>, headers: HeaderMap, Query(q): Q) -> Response {
        if let Some(refused) = refuse(&mock, &headers) {
            return refused;
        }
        answer(match q.get("site_id").map(String::as_str) {
            Some("5") => PREFIXES_SITE5,
            None => PREFIXES_ALL,
            _ => EMPTY,
        })
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
        let slugs = vec!["dc-1".to_string()];
        let data = fetch_all(&reqwest::Client::new(), &config_for(addr), &slugs)
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
        assert_eq!(tududi.disk_gb, Some(40));
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

        // Prefixes: the site-scoped one is attributed; the global one is not.
        let scoped = data
            .prefixes
            .iter()
            .find(|p| p.prefix == "198.51.100.0/24")
            .expect("scoped");
        assert_eq!(scoped.vlan, Some(20));
        assert_eq!(scoped.description, "management");
        assert_eq!(scoped.site.as_deref(), Some("dc-1"));
        let global = data
            .prefixes
            .iter()
            .find(|p| p.prefix == "192.0.2.0/24")
            .expect("global");
        assert_eq!(global.vlan, None);
        assert_eq!(global.site, None, "no declared site lists it");
    }

    #[tokio::test]
    async fn an_unknown_slug_is_unmapped_not_an_error() {
        let addr = serve(router(Mock::default())).await;
        let slugs = vec!["missing".to_string(), "dc-1".to_string()];
        let data = fetch_all(&reqwest::Client::new(), &config_for(addr), &slugs)
            .await
            .expect("fetch");
        assert!(!data.sites.contains_key("missing"));
        assert!(data.sites.contains_key("dc-1"));
    }

    #[tokio::test]
    async fn a_refused_bearer_is_a_status_error_without_the_token() {
        let addr = serve(router(Mock::default())).await;
        let mut config = config_for(addr);
        config.token = super::super::config::Secret::new("0000000000000000wrong");
        let err = fetch_all(&reqwest::Client::new(), &config, &["dc-1".into()])
            .await
            .expect_err("must fail");
        assert_eq!(err, FetchError::Status(403));
        assert!(!err.to_string().contains("wrong"));
    }

    #[tokio::test]
    async fn a_failing_server_is_a_status_error_whose_body_is_not_kept() {
        let mock = Mock::default();
        mock.fail.store(true, std::sync::atomic::Ordering::SeqCst);
        let addr = serve(router(mock)).await;
        let err = fetch_all(&reqwest::Client::new(), &config_for(addr), &["dc-1".into()])
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
            &["big-site".into()],
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
        let err = fetch_all(&reqwest::Client::new(), &config_for(addr), &["dc-1".into()])
            .await
            .expect_err("must fail");
        assert_eq!(err, FetchError::Connect);
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
