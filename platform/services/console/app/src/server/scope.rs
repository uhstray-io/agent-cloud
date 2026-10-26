//! Scope file (rendered from inventory `console_tenants`). The console only
//! reads it: it is the catalogue the UI picks from, never a store it writes.

use serde::{Deserialize, Serialize};

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
    #[serde(default)]
    pub orgs: Vec<Org>,
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
}
