//! The agent roles as the browser reads them: `GET /v1/roles`, platform admins only. The shape is the one the
//! server serializes (`work::roles_from`). Every struct carries `#[serde(default)]`, so a missing key reads as
//! empty. Nothing in this file writes to the API.

use serde::Deserialize;

use super::api::{self, Reply};

/// The platform-admin roles answer.
pub const ROLES_PATH: &str = "/v1/roles";

/// What the shell knows of the registry's roles. `Failed` is any answer that is not a readable 200, so an error
/// body is never read as an empty registry. `Forbidden` is a 403: the session is not a platform admin.
#[derive(Debug, Clone, PartialEq)]
pub enum RoleState {
    Loading,
    Ready(RoleList),
    Failed,
    Forbidden,
}

/// The state a `GET /v1/roles` reply names. Only a 200 is decoded, as `classify_me` gates its own decode, so an
/// error body that happens to lack a `roles` key cannot read as an empty registry.
pub fn role_state(reply: &Reply) -> RoleState {
    match reply.status {
        200 => api::parse::<RoleList>(reply).map_or(RoleState::Failed, RoleState::Ready),
        403 => RoleState::Forbidden,
        _ => RoleState::Failed,
    }
}

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

    fn reply(status: u16, body: serde_json::Value) -> Reply {
        Reply { status, body }
    }

    #[test]
    fn only_a_readable_200_is_a_registry() {
        let ok = reply(
            200,
            serde_json::json!({"roles": [{"name": "infra-agent", "steps": []}]}),
        );
        match role_state(&ok) {
            RoleState::Ready(list) => assert_eq!(list.roles[0].name, "infra-agent"),
            other => panic!("expected Ready, got {other:?}"),
        }
    }

    #[test]
    fn a_non_200_is_failed_even_when_its_body_decodes_as_a_registry() {
        // The error body lacks `roles`. Decoding it with the serde defaults would give an empty registry, which
        // the sidebar would call "No roles in the registry". It must read as a failure instead.
        assert_eq!(
            role_state(&reply(500, serde_json::json!({"detail": "boom"}))),
            RoleState::Failed
        );
        assert_eq!(
            role_state(&reply(502, serde_json::json!({"error": "bad gateway"}))),
            RoleState::Failed
        );
        assert_eq!(
            role_state(&reply(0, serde_json::Value::Null)),
            RoleState::Failed,
            "no answer at all"
        );
    }

    #[test]
    fn a_403_is_forbidden_not_failed() {
        assert_eq!(
            role_state(&reply(403, serde_json::json!({"error": "forbidden"}))),
            RoleState::Forbidden
        );
    }

    #[test]
    fn a_200_that_is_not_the_roles_shape_is_failed() {
        assert_eq!(
            role_state(&reply(200, serde_json::json!({"roles": "not a list"}))),
            RoleState::Failed
        );
    }
}
