//! The tenant tree as the settings pages read it: `GET /v1/tenants/{t}/tree`.
//!
//! The shell's `api::Tree` leaves out each site's NetBox mapping, which the settings pages show,
//! so this module declares the fuller shape. The server's `TreeBody` (`src/server/api.rs`) sends
//! `tenant`, `orgs` and `example`, and each site carries `netbox_site`, `null` when unmapped.
//! Read-only: nothing here writes.

use serde::Deserialize;

use super::api::segment;

/// Body of `GET /v1/tenants/{t}/tree`, as the settings pages need it.
#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct TenantTree {
    pub tenant: TreeTenant,
    pub orgs: Vec<TreeOrg>,
    pub example: bool,
}

#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct TreeTenant {
    pub id: String,
    pub name: String,
}

#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct TreeOrg {
    pub id: String,
    pub name: String,
    pub sites: Vec<TreeSite>,
}

#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct TreeSite {
    pub id: String,
    pub name: String,
    pub envs: Vec<String>,
    /// The NetBox site slug this site maps to, or `None` when it is not mapped.
    pub netbox_site: Option<String>,
}

/// The tenant's tree path. The tenant id is percent-encoded as one segment.
pub fn tree_path(tenant: &str) -> String {
    format!("/v1/tenants/{}/tree", segment(tenant))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn the_tree_reads_each_sites_netbox_mapping_and_null_means_unmapped() {
        let body = json!({
            "tenant": {"id": "acme", "name": "Acme"},
            "example": true,
            "orgs": [{
                "id": "ops", "name": "Ops",
                "sites": [
                    {"id": "dc1", "name": "DC 1", "envs": ["prod"], "netbox_site": "dc-1"},
                    {"id": "dc2", "name": "DC 2", "envs": [], "netbox_site": null}
                ]
            }]
        });
        let tree: TenantTree = serde_json::from_value(body).expect("tree");
        assert_eq!(tree.tenant.name, "Acme");
        assert!(tree.example);
        let sites = &tree.orgs[0].sites;
        assert_eq!(sites[0].netbox_site.as_deref(), Some("dc-1"));
        assert_eq!(sites[1].netbox_site, None);
        assert!(sites[1].envs.is_empty());
    }

    #[test]
    fn a_missing_key_reads_as_empty_not_as_a_failed_answer() {
        let tree: TenantTree = serde_json::from_value(json!({"orgs": []})).expect("tree");
        assert_eq!(tree.tenant, TreeTenant::default());
        assert!(tree.orgs.is_empty());
    }

    #[test]
    fn the_path_encodes_the_tenant_as_one_segment() {
        assert_eq!(tree_path("acme"), "/v1/tenants/acme/tree");
        assert_eq!(tree_path("a/b c"), "/v1/tenants/a%2Fb%20c/tree");
    }
}
