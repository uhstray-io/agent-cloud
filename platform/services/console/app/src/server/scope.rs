//! Scope file (rendered from inventory `console_tenants`). The console only
//! reads it: it is the catalogue the UI picks from, never a store it writes.
//!
//! A tenant is the hard isolation boundary (PRINCIPLES.md, plan 17 D16). Each
//! tenant lists the Authentik groups that may see it; the API filters on them.

use serde::{Deserialize, Serialize};

/// The Authentik group that sees every tenant. It matches the UI's Settings gate
/// (`ui/shell.rs`, `is_platform_admin`) and the group declared in
/// `platform/services/authentik/deployment/blueprints/platform-groups.yaml`.
pub const ADMIN_GROUP: &str = "platform-admins";

#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct Scope {
    /// True when the file is the example catalogue, so the UI can say so.
    #[serde(default)]
    pub example: bool,
    pub tenants: Vec<Tenant>,
}

#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
pub struct Tenant {
    pub id: String,
    pub name: String,
    /// Authentik groups that may see this tenant. Empty means platform admins
    /// only. Never serialized: the API returns ids and names, not membership.
    #[serde(default, skip_serializing)]
    pub groups: Vec<String>,
    #[serde(default)]
    pub orgs: Vec<Org>,
}

impl Tenant {
    /// True when the caller is a platform admin or shares one group with this
    /// tenant. Matching is exact and case-sensitive, as Authentik names groups.
    pub fn visible_to(&self, groups: &[String]) -> bool {
        groups
            .iter()
            .any(|group| group == ADMIN_GROUP || self.groups.contains(group))
    }
}

#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
pub struct Org {
    pub id: String,
    pub name: String,
    #[serde(default)]
    pub sites: Vec<Site>,
}

#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
pub struct Site {
    pub id: String,
    pub name: String,
    #[serde(default)]
    pub envs: Vec<String>,
}

impl Scope {
    pub fn parse(text: &str) -> Result<Scope, String> {
        let scope: Scope =
            serde_json::from_str(text).map_err(|err| format!("invalid JSON: {err}"))?;
        scope.validate()?;
        Ok(scope)
    }

    pub fn tenant(&self, id: &str) -> Option<&Tenant> {
        self.tenants.iter().find(|tenant| tenant.id == id)
    }

    /// The tenants the caller may see, in scope-file order.
    pub fn visible_tenants(&self, groups: &[String]) -> Vec<&Tenant> {
        self.tenants
            .iter()
            .filter(|tenant| tenant.visible_to(groups))
            .collect()
    }

    /// The tenant when the caller may see it. A tenant the caller may not see is
    /// `None`, exactly as an unknown id is, so the API cannot probe for it.
    pub fn visible_tenant(&self, id: &str, groups: &[String]) -> Option<&Tenant> {
        self.tenant(id).filter(|tenant| tenant.visible_to(groups))
    }

    fn validate(&self) -> Result<(), String> {
        let mut tenant_ids = std::collections::HashSet::new();
        for tenant in &self.tenants {
            require_id(&tenant.id, &tenant.name)?;
            if !tenant_ids.insert(tenant.id.as_str()) {
                return Err(format!("duplicate tenant id {}", tenant.id));
            }
            for org in &tenant.orgs {
                require_id(&org.id, &org.name)?;
                for site in &org.sites {
                    require_id(&site.id, &site.name)?;
                }
            }
        }
        Ok(())
    }
}

fn require_id(id: &str, name: &str) -> Result<(), String> {
    if id.trim().is_empty() || name.trim().is_empty() {
        return Err("every tenant, org and site needs a non-empty id and name".to_string());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    const SAMPLE: &str = r#"{
        "example": true,
        "tenants": [{
            "id": "acme", "name": "Acme",
            "orgs": [{"id": "ops", "name": "Ops", "sites": [
                {"id": "dc1", "name": "DC 1", "envs": ["prod", "dev"]}
            ]}]
        }]
    }"#;

    #[test]
    fn parses_and_looks_up_tenant_tree() {
        let scope = Scope::parse(SAMPLE).expect("valid scope");
        assert!(scope.example);
        let acme = scope.tenant("acme").expect("tenant found");
        assert_eq!(acme.name, "Acme");
        assert_eq!(acme.orgs[0].sites[0].envs, vec!["prod", "dev"]);
    }

    #[test]
    fn unknown_tenant_is_none() {
        let scope = Scope::parse(SAMPLE).expect("valid scope");
        assert!(scope.tenant("globex").is_none());
    }

    #[test]
    fn example_defaults_to_false() {
        let scope = Scope::parse(r#"{"tenants": []}"#).expect("valid scope");
        assert!(!scope.example);
    }

    #[test]
    fn rejects_empty_id_and_duplicate_tenant() {
        assert!(Scope::parse(r#"{"tenants": [{"id": " ", "name": "x"}]}"#).is_err());
        let dup = r#"{"tenants": [{"id": "a", "name": "A"}, {"id": "a", "name": "B"}]}"#;
        assert!(Scope::parse(dup)
            .unwrap_err()
            .contains("duplicate tenant id a"));
    }

    #[test]
    fn rejects_malformed_json() {
        assert!(Scope::parse("{").is_err());
    }

    const GROUPED: &str = r#"{"tenants": [
        {"id": "acme", "name": "Acme", "groups": ["ops"]},
        {"id": "initech", "name": "Initech", "groups": ["finance", "ops"]},
        {"id": "hidden", "name": "Hidden"}
    ]}"#;

    fn names(list: &[&str]) -> Vec<String> {
        list.iter().map(|name| name.to_string()).collect()
    }

    fn ids(scope: &Scope, groups: &[String]) -> Vec<String> {
        scope
            .visible_tenants(groups)
            .iter()
            .map(|tenant| tenant.id.clone())
            .collect()
    }

    #[test]
    fn platform_admin_sees_every_tenant_including_ungrouped_ones() {
        let scope = Scope::parse(GROUPED).expect("valid scope");
        assert_eq!(
            ids(&scope, &names(&[ADMIN_GROUP])),
            ["acme", "initech", "hidden"]
        );
    }

    #[test]
    fn member_sees_only_tenants_that_list_one_of_their_groups() {
        let scope = Scope::parse(GROUPED).expect("valid scope");
        assert_eq!(ids(&scope, &names(&["ops"])), ["acme", "initech"]);
        assert_eq!(ids(&scope, &names(&["finance"])), ["initech"]);
    }

    #[test]
    fn non_member_sees_no_tenant() {
        let scope = Scope::parse(GROUPED).expect("valid scope");
        assert!(ids(&scope, &names(&["users"])).is_empty());
        assert!(ids(&scope, &[]).is_empty());
        assert!(scope.visible_tenant("acme", &names(&["users"])).is_none());
    }

    #[test]
    fn tenant_without_groups_is_platform_admin_only() {
        let scope = Scope::parse(GROUPED).expect("valid scope");
        assert!(scope
            .visible_tenant("hidden", &names(&["ops", "finance"]))
            .is_none());
        assert!(scope
            .visible_tenant("hidden", &names(&[ADMIN_GROUP]))
            .is_some());
    }

    #[test]
    fn group_match_is_exact_and_case_sensitive() {
        let scope = Scope::parse(GROUPED).expect("valid scope");
        assert!(ids(&scope, &names(&["Ops"])).is_empty());
        assert!(ids(&scope, &names(&["ops-old"])).is_empty());
        assert!(ids(&scope, &names(&["platform-admins-old"])).is_empty());
    }

    #[test]
    fn visible_tenant_is_the_lookup_gated_by_membership() {
        let scope = Scope::parse(GROUPED).expect("valid scope");
        let finance = names(&["finance"]);
        assert_eq!(
            scope
                .visible_tenant("initech", &finance)
                .map(|t| t.name.as_str()),
            Some("Initech")
        );
        // acme exists in the file but lists no group this caller has.
        assert!(scope.visible_tenant("acme", &finance).is_none());
        assert!(scope.visible_tenant("nope", &finance).is_none());
    }

    #[test]
    fn groups_are_read_but_never_serialized() {
        let scope = Scope::parse(GROUPED).expect("valid scope");
        let json = serde_json::to_string(&scope.tenants[0]).expect("serialize tenant");
        assert!(!json.contains("groups"), "{json}");
        assert_eq!(scope.tenants[0].groups, names(&["ops"]));
    }
}
