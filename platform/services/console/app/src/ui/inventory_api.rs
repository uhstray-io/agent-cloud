//! The Phase 3 inventory API as the browser reads it, and the calls that read it.
//!
//! The shapes mirror `platform/services/console/app/docs/phase3-api.md`, whose
//! field names are the serialized structs in `src/server/inventory.rs`. Every
//! struct carries `#[serde(default)]`, so a missing key reads as empty, and an
//! `Option` field reads `null` as `None`. Nothing in this file writes to the API.

use std::collections::BTreeMap;

use serde::de::DeserializeOwned;
use serde::Deserialize;

use super::api::{self, Reply};

/// The failure text for a 200 whose body does not have the expected shape.
pub const UNREADABLE: &str = "the console sent an answer it could not read";
/// The failure text for any status the inventory reads do not expect.
pub const NO_ANSWER: &str = "the console API did not answer";

/// One authority's state, as the envelope's `source` names it.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum SourceStatus {
    Ok,
    /// Also the value when the envelope omits the field.
    #[default]
    Unconfigured,
    Error,
}

#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct Sources {
    pub netbox: SourceStatus,
    pub proxmox: SourceStatus,
}

#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct Errors {
    pub netbox: Option<String>,
    pub proxmox: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct Stamps {
    pub netbox: Option<String>,
    pub proxmox: Option<String>,
}

/// Deep links out to Grafana. Each is `null` when `CONSOLE_GRAFANA_URL` is unset.
#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct Links {
    pub grafana: Option<String>,
    pub grafana_conformance: Option<String>,
}

/// Counts a reader should see that do not change the answer's state. Platform
/// admins only: a tenant member's envelope has no `diagnostics` key at all.
#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct Diagnostics {
    /// NetBox rows dropped because their own scope did not match the query that returned them.
    pub dropped_mismatched: u64,
}

/// The envelope every inventory, services, network and flows answer starts with.
/// `stale` is true when any configured authority's latest read failed.
#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct Meta {
    pub source: Sources,
    pub error: Errors,
    pub stale: bool,
    pub updated_at: Stamps,
    pub links: Links,
    /// `None` for a tenant member, and on any answer that carries no diagnostics.
    pub diagnostics: Option<Diagnostics>,
}

/// `GET /v1/tenants/{t}/inventory`.
#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct Inventory {
    #[serde(flatten)]
    pub meta: Meta,
    pub sites: Vec<Site>,
}

#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct Site {
    pub id: String,
    pub name: String,
    pub org: String,
    pub netbox_site: Option<String>,
    /// False when the site has no mapped NetBox site, or NetBox is not read.
    pub mapped: bool,
    pub clusters: Vec<Cluster>,
    pub devices: Vec<Device>,
}

#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct Cluster {
    pub name: String,
    /// Proxmox nodes that host this cluster's matched VMs.
    pub nodes: Vec<Node>,
    pub vms: Vec<Vm>,
}

/// A Proxmox node. Units are Proxmox's: `cpu` is a fraction (0 to 1), memory is
/// bytes, `uptime` is seconds. The API carries no RAG for a node.
#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct Node {
    pub name: String,
    pub status: Option<String>,
    pub cpu: Option<f64>,
    pub maxcpu: Option<f64>,
    pub mem: Option<u64>,
    pub maxmem: Option<u64>,
    pub uptime: Option<u64>,
}

/// A NetBox device at a site. Its name, role and status are each `null` when NetBox holds none.
#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct Device {
    pub name: Option<String>,
    pub role: Option<String>,
    pub status: Option<String>,
}

/// A VM. `status` is the NetBox status, `live_status` the Proxmox one (or `null`).
#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct Vm {
    pub name: String,
    pub vmid: Option<u64>,
    pub status: String,
    pub live_status: Option<String>,
    pub vcpus: Option<f64>,
    pub memory_mb: Option<u64>,
    pub disk_gb: Option<u64>,
    pub primary_ip: Option<String>,
    pub tags: Vec<String>,
    /// `null` for a VM with none of the workflow fields.
    pub workflow: Option<Workflow>,
    /// `green`, `amber`, `red` or `grey`, decided by the server.
    pub rag: String,
    pub grafana_url: Option<String>,
}

/// A VM's workflow fields. `steps` maps a step id to `pass`, `fail`, `skip`, or
/// `unknown`; any other status string is passed through as the server sent it.
#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct Workflow {
    pub steps: BTreeMap<String, String>,
    pub failed: Vec<String>,
    pub last_run: Option<String>,
}

/// One row of `GET /v1/tenants/{t}/services`.
#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct ServiceRow {
    pub name: String,
    pub grafana_url: Option<String>,
    /// The scope site id the VM sits under.
    pub site: String,
    pub cluster: String,
    pub live_status: Option<String>,
    pub rag: String,
    pub failed_steps: Vec<String>,
    pub last_run: Option<String>,
    pub tags: Vec<String>,
}

/// `GET /v1/tenants/{t}/services`.
#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct ServicesBody {
    #[serde(flatten)]
    pub meta: Meta,
    pub services: Vec<ServiceRow>,
}

/// `GET /v1/tenants/{t}/services/{name}`.
#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct ServiceBody {
    #[serde(flatten)]
    pub meta: Meta,
    pub service: ServiceRow,
    pub vm: Vm,
}

/// `GET /v1/network` (admin-only).
#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct NetworkBody {
    #[serde(flatten)]
    pub meta: Meta,
    /// `true` when NetBox had more prefixes than the read's page cap covers. Read as
    /// `None` when absent or `null`, which the page treats as false.
    pub truncated: Option<bool>,
    pub prefixes: Vec<Prefix>,
}

#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct Prefix {
    pub prefix: String,
    /// The VLAN id (`vid`), or `null`.
    pub vlan: Option<u64>,
    pub description: String,
    /// The NetBox site slug the prefix is scoped to, or `null` for a global prefix.
    pub site: Option<String>,
}

/// `GET /v1/flows` (admin-only). The flows answer has no envelope: `source` is a bare word.
#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct FlowsBody {
    pub source: SourceStatus,
    pub flows: Vec<Flow>,
    pub links: Links,
}

#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct Flow {
    pub kind: String,
    pub from: String,
    pub to: String,
    pub label: String,
}

/// What one read came back as. `Missing` is a 404: an unknown tenant or service,
/// which the API answers the same way whether or not the caller may see it.
#[derive(Debug, Clone, PartialEq)]
pub enum Read<T> {
    Ok(T),
    SignedOut,
    Forbidden,
    Missing,
    Failed(String),
}

/// Sorts a reply into a `Read`. Pure, so the status rules have tests.
pub fn classify<T: DeserializeOwned>(reply: &Reply) -> Read<T> {
    match reply.status {
        200 => match api::parse::<T>(reply) {
            Some(body) => Read::Ok(body),
            None => Read::Failed(UNREADABLE.to_string()),
        },
        401 => Read::SignedOut,
        403 => Read::Forbidden,
        404 => Read::Missing,
        _ => Read::Failed(NO_ANSWER.to_string()),
    }
}

/// One `GET` of `path`, classified.
pub async fn read<T: DeserializeOwned>(path: &str) -> Read<T> {
    classify(&api::request("GET", path, None).await)
}

/// The tenant's inventory path. The tenant id is percent-encoded as one segment.
pub fn inventory_path(tenant: &str) -> String {
    format!("/v1/tenants/{}/inventory", api::segment(tenant))
}

/// The tenant's services list path.
pub fn services_path(tenant: &str) -> String {
    format!("/v1/tenants/{}/services", api::segment(tenant))
}

/// One service's path. Both segments are percent-encoded.
pub fn service_path(tenant: &str, name: &str) -> String {
    format!(
        "/v1/tenants/{}/services/{}",
        api::segment(tenant),
        api::segment(name)
    )
}

/// The admin-only network answer.
pub const NETWORK_PATH: &str = "/v1/network";
/// The admin-only data-flow catalogue.
pub const FLOWS_PATH: &str = "/v1/flows";

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn reply(status: u16, body: serde_json::Value) -> Reply {
        Reply { status, body }
    }

    /// The inventory example from `phase3-api.md`, with a mapped site and an unmapped one.
    fn inventory_body() -> serde_json::Value {
        json!({
            "source": {"netbox": "ok", "proxmox": "unconfigured"},
            "error": {"netbox": null, "proxmox": null},
            "stale": false,
            "updated_at": {"netbox": "2026-10-26T12:01:00Z", "proxmox": null},
            "links": {"grafana": "https://grafana.example.test", "grafana_conformance": null},
            "sites": [
                {
                    "id": "dc1", "name": "DC 1", "org": "ops",
                    "netbox_site": "dc-1", "mapped": true,
                    "clusters": [{
                        "name": "pve-prod",
                        "nodes": [{"name": "pve-01", "status": "online", "cpu": 0.12, "maxcpu": 16,
                                   "mem": 8589934592u64, "maxmem": 68719476736u64, "uptime": 123456}],
                        "vms": [{
                            "name": "tududi", "vmid": 101, "status": "active", "live_status": "running",
                            "vcpus": 2.0, "memory_mb": 4096, "disk_gb": 40,
                            "primary_ip": "198.51.100.5/24", "tags": ["tududi-sync"],
                            "workflow": {"steps": {"step-a": "pass", "step-b": "fail"},
                                         "failed": ["step-b"], "last_run": "2026-10-26T12:00:00Z"},
                            "rag": "red",
                            "grafana_url": "https://grafana.example.test/d/service-overview/service-overview?var-service=tududi&var-log_service=tududi"
                        }]
                    }],
                    "devices": [{"name": "pve-01", "role": "Hypervisor", "status": "active"},
                                {"name": null, "role": null, "status": null}]
                },
                {"id": "dc2", "name": "DC 2", "org": "ops", "netbox_site": null, "mapped": false,
                 "clusters": [], "devices": []}
            ]
        })
    }

    #[test]
    fn inventory_reads_the_contract_example() {
        let Read::Ok(inv) = classify::<Inventory>(&reply(200, inventory_body())) else {
            panic!("expected Ok");
        };
        assert_eq!(inv.meta.source.netbox, SourceStatus::Ok);
        assert_eq!(inv.meta.source.proxmox, SourceStatus::Unconfigured);
        assert_eq!(inv.meta.updated_at.proxmox, None);
        assert_eq!(
            inv.meta.links.grafana.as_deref(),
            Some("https://grafana.example.test")
        );
        assert_eq!(inv.sites.len(), 2);
        let dc1 = &inv.sites[0];
        assert!(dc1.mapped);
        let node = &dc1.clusters[0].nodes[0];
        assert_eq!(node.cpu, Some(0.12));
        assert_eq!(node.maxmem, Some(68_719_476_736));
        let vm = &dc1.clusters[0].vms[0];
        assert_eq!(vm.rag, "red");
        assert_eq!(vm.live_status.as_deref(), Some("running"));
        let workflow = vm.workflow.as_ref().expect("a service VM has a workflow");
        assert_eq!(workflow.failed, ["step-b"]);
        assert_eq!(workflow.steps["step-a"], "pass");
        assert_eq!(
            dc1.devices[1].name, None,
            "a null device name reads as None"
        );
        let dc2 = &inv.sites[1];
        assert!(!dc2.mapped && dc2.clusters.is_empty() && dc2.devices.is_empty());
    }

    #[test]
    fn a_null_workflow_and_missing_keys_read_as_empty() {
        let body = json!({
            "source": {"netbox": "ok"},
            "sites": [{"id": "dc1", "clusters": [{"name": "c", "vms": [
                {"name": "plain", "status": "active", "workflow": null, "rag": "grey"}
            ]}]}]
        });
        let Read::Ok(inv) = classify::<Inventory>(&reply(200, body)) else {
            panic!("expected Ok");
        };
        assert_eq!(inv.meta.source.proxmox, SourceStatus::Unconfigured);
        assert!(!inv.meta.stale, "a missing stale reads as false");
        let vm = &inv.sites[0].clusters[0].vms[0];
        assert!(vm.workflow.is_none());
        assert!(vm.tags.is_empty());
        assert_eq!(vm.grafana_url, None);
    }

    #[test]
    fn services_and_service_read_the_flattened_envelope() {
        let body = json!({
            "source": {"netbox": "ok", "proxmox": "error"},
            "error": {"netbox": null, "proxmox": "Proxmox answered HTTP 500"},
            "stale": true,
            "updated_at": {"netbox": "2026-10-26T12:01:00Z", "proxmox": "2026-10-26T11:00:00Z"},
            "links": {"grafana": null, "grafana_conformance": null},
            "services": [{
                "name": "tududi", "grafana_url": null, "site": "dc1", "cluster": "pve-prod",
                "live_status": null, "rag": "amber", "failed_steps": ["step-b"],
                "last_run": "2026-10-26T12:00:00Z", "tags": ["Products"]
            }]
        });
        let Read::Ok(list) = classify::<ServicesBody>(&reply(200, body)) else {
            panic!("expected Ok");
        };
        assert!(list.meta.stale);
        assert_eq!(list.meta.source.proxmox, SourceStatus::Error);
        assert_eq!(
            list.meta.error.proxmox.as_deref(),
            Some("Proxmox answered HTTP 500")
        );
        assert_eq!(list.services[0].rag, "amber");
        assert_eq!(list.services[0].live_status, None);

        let single = json!({
            "source": {"netbox": "ok"}, "stale": false,
            "service": {"name": "tududi", "site": "dc1", "cluster": "pve-prod", "rag": "amber"},
            "vm": {"name": "tududi", "status": "active", "rag": "amber"}
        });
        let Read::Ok(detail) = classify::<ServiceBody>(&reply(200, single)) else {
            panic!("expected Ok");
        };
        assert_eq!(detail.service.name, "tududi");
        assert_eq!(detail.vm.status, "active");
    }

    #[test]
    fn network_and_flows_read_their_own_shapes() {
        let network = json!({
            "source": {"netbox": "ok"}, "stale": false,
            "prefixes": [
                {"prefix": "198.51.100.0/24", "vlan": 20, "description": "management", "site": "dc-1"},
                {"prefix": "192.0.2.0/24", "vlan": null, "description": "documentation", "site": null}
            ]
        });
        let Read::Ok(net) = classify::<NetworkBody>(&reply(200, network)) else {
            panic!("expected Ok");
        };
        assert_eq!(net.prefixes[1].site, None, "a global prefix has no site");
        assert_eq!(net.prefixes[0].vlan, Some(20));
        assert_eq!(net.truncated, None, "an absent truncated is not set");

        let cut = json!({"source": {"netbox": "ok"}, "truncated": true, "prefixes": []});
        let Read::Ok(cut) = classify::<NetworkBody>(&reply(200, cut)) else {
            panic!("expected Ok");
        };
        assert_eq!(cut.truncated, Some(true));
        let nulled = json!({"truncated": null, "prefixes": []});
        assert!(
            matches!(classify::<NetworkBody>(&reply(200, nulled)), Read::Ok(_)),
            "an explicit null does not fail the whole answer"
        );

        let flows = json!({
            "source": "ok",
            "flows": [{"kind": "oidc", "from": "console", "to": "authentik", "label": "sign-in"}],
            "links": {"grafana": null, "grafana_conformance": null}
        });
        let Read::Ok(set) = classify::<FlowsBody>(&reply(200, flows)) else {
            panic!("expected Ok");
        };
        assert_eq!(set.source, SourceStatus::Ok);
        assert_eq!(set.flows[0].kind, "oidc");

        let none = json!({"source": "unconfigured", "flows": [], "links": {}});
        let Read::Ok(empty) = classify::<FlowsBody>(&reply(200, none)) else {
            panic!("expected Ok");
        };
        assert_eq!(empty.source, SourceStatus::Unconfigured);
    }

    #[test]
    fn statuses_sort_into_reads() {
        assert_eq!(
            classify::<Inventory>(&reply(401, json!({"error": "unauthenticated"}))),
            Read::SignedOut
        );
        assert_eq!(
            classify::<NetworkBody>(&reply(403, json!({"error": "forbidden"}))),
            Read::Forbidden
        );
        assert_eq!(
            classify::<ServiceBody>(&reply(404, json!({"error": "unknown_service"}))),
            Read::Missing
        );
        assert_eq!(
            classify::<Inventory>(&reply(500, serde_json::Value::Null)),
            Read::Failed(NO_ANSWER.to_string())
        );
        assert_eq!(
            classify::<Inventory>(&reply(200, json!({"sites": "not a list"}))),
            Read::Failed(UNREADABLE.to_string())
        );
    }

    #[test]
    fn diagnostics_are_read_for_admins_and_absent_for_members() {
        let admin = json!({
            "source": {"netbox": "ok"}, "stale": false,
            "diagnostics": {"dropped_mismatched": 2}, "sites": []
        });
        let Read::Ok(inv) = classify::<Inventory>(&reply(200, admin)) else {
            panic!("expected Ok");
        };
        assert_eq!(
            inv.meta.diagnostics,
            Some(Diagnostics {
                dropped_mismatched: 2
            })
        );
        let member = json!({"source": {"netbox": "ok"}, "sites": []});
        let Read::Ok(inv) = classify::<Inventory>(&reply(200, member)) else {
            panic!("expected Ok");
        };
        assert_eq!(inv.meta.diagnostics, None);
    }

    #[test]
    fn paths_encode_each_segment() {
        assert_eq!(inventory_path("uhstray"), "/v1/tenants/uhstray/inventory");
        assert_eq!(services_path("uhstray"), "/v1/tenants/uhstray/services");
        assert_eq!(
            service_path("uhstray", "a/b c"),
            "/v1/tenants/uhstray/services/a%2Fb%20c"
        );
    }
}
