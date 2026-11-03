//! The Access page's answer as the browser reads it: `GET /v1/access`, platform admins only. The
//! shapes are the ones the server sends. Every struct carries `#[serde(default)]`, so a missing key
//! reads as empty, and an unknown state or status word reads as `Unknown` rather than failing the
//! whole body. Nothing in this file writes to the API.

use std::collections::BTreeMap;

use serde::Deserialize;

/// The platform-admin access answer.
pub const ACCESS_PATH: &str = "/v1/access";

/// The state the server names. Any other word is `Unknown`, which the page shows as a failed
/// answer rather than as an empty directory.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum AccessState {
    Ok,
    Unconfigured,
    /// No directory read has finished yet. Also the value when the envelope omits the field.
    #[default]
    Loading,
    Error,
    #[serde(other)]
    Unknown,
}

/// `GET /v1/access`.
#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct AccessBody {
    pub state: AccessState,
    pub users: Vec<AccessUser>,
    pub groups: Vec<AccessGroup>,
    /// Present in every state, `unconfigured` included: it describes the design, not the directory.
    pub role_map: RoleMap,
    /// When the newest directory read finished.
    pub refreshed_at: Option<String>,
    /// Why the newest directory read failed, when it did. With state `ok`, the users and groups
    /// are then the last good read.
    pub error: Option<String>,
}

/// One person or service account in the directory.
#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct AccessUser {
    pub username: String,
    pub name: String,
    pub email: Option<String>,
    pub active: bool,
    pub service_account: bool,
    pub groups: Vec<String>,
}

/// One directory group and how many members it has.
#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct AccessGroup {
    pub name: String,
    pub superuser: bool,
    pub members: u64,
}

/// The access levels and the role each one holds in each system.
#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct RoleMap {
    /// The system columns, in the order the server sends them.
    pub systems: Vec<String>,
    pub levels: Vec<RoleLevel>,
}

/// One access level: its status and its role in each system.
#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct RoleLevel {
    pub level: String,
    pub status: LevelStatus,
    /// The role this level holds in each system, keyed by system name.
    pub roles: BTreeMap<String, String>,
}

/// Whether a level is provisioned. `Target` is the design, not yet built.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum LevelStatus {
    Built,
    Target,
    /// No status, or a word this console does not know.
    #[default]
    #[serde(other)]
    Unknown,
}

/// The sentence a body with no directory to show says. `None` when the directory answered.
pub fn not_ok_message(state: AccessState, error: Option<&str>) -> Option<String> {
    match state {
        AccessState::Ok => None,
        AccessState::Unconfigured => {
            Some("Authentik is not configured for this console.".to_string())
        }
        AccessState::Loading => Some("The first directory read has not finished.".to_string()),
        AccessState::Error => Some(format!(
            "Authentik could not be read: {}. The next read tries again.",
            error.unwrap_or("no reason was given")
        )),
        AccessState::Unknown => {
            Some("The console received an access state it does not know.".to_string())
        }
    }
}

/// The banner for an `ok` body whose newest read failed. `None` when the newest read worked.
pub fn stale_line(state: AccessState, error: Option<&str>) -> Option<String> {
    match (state, error) {
        (AccessState::Ok, Some(error)) => Some(format!(
            "The newest directory read failed: {error}. Showing the last good read."
        )),
        _ => None,
    }
}

/// The people in the order the table shows them: active humans, then active service accounts,
/// then every inactive account. Each run is sorted by username.
pub fn sorted_people(users: &[AccessUser]) -> Vec<AccessUser> {
    let mut people = users.to_vec();
    people.sort_by(|a, b| (tier(a), &a.username).cmp(&(tier(b), &b.username)));
    people
}

/// 0 for an active human, 1 for an active service account, 2 for anyone inactive.
fn tier(user: &AccessUser) -> u8 {
    if !user.active {
        2
    } else if user.service_account {
        1
    } else {
        0
    }
}

/// One row of the role map: the level, its role per system in `systems` order, and its status.
#[derive(Debug, Clone, PartialEq)]
pub struct RoleRow {
    pub level: String,
    pub cells: Vec<String>,
    pub status: LevelStatus,
}

/// The role map as table rows. A level with no role for a system shows a dash.
pub fn role_rows(map: &RoleMap) -> Vec<RoleRow> {
    map.levels
        .iter()
        .map(|level| RoleRow {
            level: level.level.clone(),
            cells: map
                .systems
                .iter()
                .map(|system| {
                    level
                        .roles
                        .get(system)
                        .cloned()
                        .unwrap_or_else(|| "\u{2014}".to_string())
                })
                .collect(),
            status: level.status,
        })
        .collect()
}

/// The status tag's word.
pub fn status_label(status: LevelStatus) -> &'static str {
    match status {
        LevelStatus::Built => "built",
        LevelStatus::Target => "target",
        LevelStatus::Unknown => "unknown",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn user(username: &str, active: bool, service_account: bool) -> AccessUser {
        AccessUser {
            username: username.to_string(),
            name: format!("Person {username}"),
            email: None,
            active,
            service_account,
            groups: Vec::new(),
        }
    }

    #[test]
    fn each_server_state_word_reads_as_its_state() {
        let read = |text: &str| {
            let value = serde_json::json!({"state": text, "users": []});
            serde_json::from_value::<AccessBody>(value)
                .expect("body")
                .state
        };
        assert_eq!(read("ok"), AccessState::Ok);
        assert_eq!(read("unconfigured"), AccessState::Unconfigured);
        assert_eq!(read("loading"), AccessState::Loading);
        assert_eq!(read("error"), AccessState::Error);
        assert_eq!(read("degraded-new-word"), AccessState::Unknown);
    }

    #[test]
    fn a_full_answer_decodes_every_field() {
        let value = serde_json::json!({
            "state": "ok",
            "users": [{
                "username": "alice",
                "name": "Alice Example",
                "email": "alice@example.test",
                "active": true,
                "service_account": false,
                "groups": ["platform-admins"]
            }],
            "groups": [{"name": "platform-admins", "superuser": true, "members": 1}],
            "role_map": {
                "systems": ["semaphore", "netbox"],
                "levels": [{
                    "level": "admin",
                    "status": "built",
                    "roles": {"semaphore": "owner", "netbox": "superuser"}
                }]
            },
            "refreshed_at": "2026-11-03T10:00:00Z",
            "error": "Authentik answered HTTP 500"
        });
        let body: AccessBody = serde_json::from_value(value).expect("body");
        assert_eq!(body.state, AccessState::Ok);
        assert_eq!(body.users[0].username, "alice");
        assert_eq!(body.users[0].email.as_deref(), Some("alice@example.test"));
        assert_eq!(body.users[0].groups, vec!["platform-admins".to_string()]);
        assert!(body.groups[0].superuser);
        assert_eq!(body.groups[0].members, 1);
        assert_eq!(body.role_map.systems, vec!["semaphore", "netbox"]);
        let level = &body.role_map.levels[0];
        assert_eq!(level.status, LevelStatus::Built);
        assert_eq!(
            level.roles.get("netbox").map(String::as_str),
            Some("superuser")
        );
        assert_eq!(body.error.as_deref(), Some("Authentik answered HTTP 500"));
    }

    #[test]
    fn an_unconfigured_body_still_carries_the_role_map() {
        let value = serde_json::json!({
            "state": "unconfigured",
            "role_map": {
                "systems": ["netbox"],
                "levels": [{"level": "viewer", "status": "target", "roles": {"netbox": "view"}}]
            }
        });
        let body: AccessBody = serde_json::from_value(value).expect("body");
        assert_eq!(body.state, AccessState::Unconfigured);
        assert!(body.users.is_empty());
        assert_eq!(body.role_map.levels.len(), 1);
    }

    #[test]
    fn an_unknown_level_status_decodes_and_the_rest_survives() {
        let value = serde_json::json!({
            "state": "ok",
            "users": [{"username": "alice", "active": true}],
            "role_map": {
                "systems": ["netbox"],
                "levels": [{"level": "viewer", "status": "rolling-out", "roles": {}}]
            }
        });
        let body: AccessBody = serde_json::from_value(value).expect("body");
        assert_eq!(body.role_map.levels[0].status, LevelStatus::Unknown);
        assert_eq!(body.users[0].username, "alice");
    }

    #[test]
    fn a_missing_state_reads_as_loading_and_nulls_as_none() {
        let body: AccessBody =
            serde_json::from_value(serde_json::json!({"error": null})).expect("body");
        assert_eq!(body.state, AccessState::Loading);
        assert_eq!(body.error, None);
        assert!(body.users.is_empty());
        assert!(body.groups.is_empty());
    }

    #[test]
    fn people_sort_active_humans_then_service_accounts_then_inactive() {
        let users = vec![
            user("zoe", false, false),
            user("svc-backup", true, true),
            user("mia", true, false),
            user("old-bot", false, true),
            user("ann", true, false),
            user("svc-api", true, true),
        ];
        let sorted = sorted_people(&users);
        let names: Vec<&str> = sorted.iter().map(|u| u.username.as_str()).collect();
        assert_eq!(
            names,
            ["ann", "mia", "svc-api", "svc-backup", "old-bot", "zoe"]
        );
    }

    #[test]
    fn only_an_ok_answer_has_a_directory_to_show() {
        assert_eq!(not_ok_message(AccessState::Ok, None), None);
        assert_eq!(
            not_ok_message(AccessState::Unconfigured, None).as_deref(),
            Some("Authentik is not configured for this console.")
        );
        assert_eq!(
            not_ok_message(AccessState::Error, Some("Authentik answered HTTP 403")).as_deref(),
            Some(
                "Authentik could not be read: Authentik answered HTTP 403. The next read tries again."
            )
        );
        assert!(not_ok_message(AccessState::Error, None)
            .expect("message")
            .contains("no reason was given"));
        assert!(not_ok_message(AccessState::Unknown, None).is_some());
    }

    #[test]
    fn a_failed_newest_read_is_a_stale_banner_only_on_an_ok_body() {
        assert_eq!(
            stale_line(AccessState::Ok, Some("Authentik answered HTTP 500")).as_deref(),
            Some(
                "The newest directory read failed: Authentik answered HTTP 500. Showing the last good read."
            )
        );
        assert_eq!(stale_line(AccessState::Ok, None), None);
        assert_eq!(stale_line(AccessState::Error, Some("x")), None);
    }

    #[test]
    fn role_rows_follow_the_system_order_and_dash_a_missing_role() {
        let map = RoleMap {
            systems: vec!["semaphore".to_string(), "netbox".to_string()],
            levels: vec![RoleLevel {
                level: "viewer".to_string(),
                status: LevelStatus::Target,
                roles: BTreeMap::from([("netbox".to_string(), "view".to_string())]),
            }],
        };
        let rows = role_rows(&map);
        assert_eq!(rows[0].level, "viewer");
        assert_eq!(
            rows[0].cells,
            vec!["\u{2014}".to_string(), "view".to_string()]
        );
        assert_eq!(status_label(rows[0].status), "target");
        assert_eq!(status_label(LevelStatus::Unknown), "unknown");
    }
}
