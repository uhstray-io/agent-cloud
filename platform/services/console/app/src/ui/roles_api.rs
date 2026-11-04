//! The agent roles as the browser reads them: `GET /v1/roles`, platform admins only. The shape is the one the
//! server serializes (`work::roles_from`). Every struct carries `#[serde(default)]`, so a missing key reads as
//! empty. Nothing in this file writes to the API.

use serde::Deserialize;

/// The platform-admin roles answer.
pub const ROLES_PATH: &str = "/v1/roles";

/// `GET /v1/roles`: the registry's roles, sorted by name.
#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct RoleList {
    pub roles: Vec<Role>,
}

/// One agent role: its name, which is the registry owner, and the steps it owns in order.
#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct Role {
    pub name: String,
    pub steps: Vec<RoleStep>,
}

/// One registry step as a role lists it.
#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct RoleStep {
    pub id: String,
    pub order: u32,
    pub label: String,
}

/// The role that `name` names, if the registry has one. Names match exactly.
pub fn role_named<'a>(list: &'a RoleList, name: &str) -> Option<&'a Role> {
    list.roles.iter().find(|role| role.name == name)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roles_decode_with_their_steps_in_the_order_sent() {
        let value = serde_json::json!({
            "roles": [
                {"name": "infra-agent", "steps": [{"id": "step-a", "order": 1, "label": "Plan"}]},
                {"name": "service-agent", "steps": [
                    {"id": "step-b", "order": 2, "label": "Build"},
                    {"id": "step-d", "order": 4, "label": "Deploy"}
                ]}
            ]
        });
        let list: RoleList = serde_json::from_value(value).expect("roles");
        assert_eq!(list.roles.len(), 2);
        assert_eq!(list.roles[1].steps[1].label, "Deploy");
        assert_eq!(list.roles[1].steps[1].order, 4);
    }

    #[test]
    fn a_missing_key_reads_as_empty() {
        let list: RoleList = serde_json::from_value(serde_json::json!({})).expect("roles");
        assert!(list.roles.is_empty());
        let role: Role = serde_json::from_value(serde_json::json!({"name": "x"})).expect("role");
        assert!(role.steps.is_empty());
    }

    #[test]
    fn a_role_is_found_by_its_exact_name() {
        let list = RoleList {
            roles: vec![Role {
                name: "service-agent".into(),
                steps: Vec::new(),
            }],
        };
        assert_eq!(
            role_named(&list, "service-agent").map(|role| role.name.as_str()),
            Some("service-agent")
        );
        assert!(role_named(&list, "service").is_none(), "no prefix match");
        assert!(
            role_named(&list, "Service-Agent").is_none(),
            "no case folding"
        );
    }
}
