//! The Request a service page's answer as the browser reads it: `GET /v1/catalog`, for any signed-in member. The
//! shape is the one the server serializes for a member. Every struct carries `#[serde(default)]`, so a missing key
//! reads as empty. The host count is for platform admins: the page never reads it, and a key it does not name is
//! dropped here. Nothing in this file writes to the API.

use serde::Deserialize;

/// The service catalog answer.
pub const CATALOG_PATH: &str = "/v1/catalog";

/// The state the server names. Any other word, and a missing field, is `Unknown`, which the page shows as a
/// failed answer rather than as an empty list.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum CatalogState {
    Ok,
    Unconfigured,
    #[default]
    #[serde(other)]
    Unknown,
}

/// `GET /v1/catalog`, as a member reads it.
#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct CatalogBody {
    pub state: CatalogState,
    pub services: Vec<CatalogService>,
}

/// One deployable service: its name, the deploy template that deploys it, and whether a host already runs it.
#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct CatalogService {
    pub service: String,
    pub name: String,
    pub template: String,
    pub running: bool,
}

/// The sentence an answer with no list shows. `None` when the state has a list to show.
pub fn not_ok_message(state: CatalogState) -> Option<String> {
    match state {
        CatalogState::Ok => None,
        CatalogState::Unconfigured => {
            Some("The service catalog is not configured for this console.".to_string())
        }
        CatalogState::Unknown => {
            Some("The console received a service catalog state it does not know.".to_string())
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_ok_answer_decodes_its_services() {
        let value = serde_json::json!({
            "state": "ok",
            "services": [
                {
                    "service": "netbox",
                    "name": "NetBox",
                    "template": "Deploy NetBox",
                    "running": true
                },
                {
                    "service": "step-ca",
                    "name": "step-ca",
                    "template": "Deploy step-ca (Dev)",
                    "running": false
                }
            ]
        });
        let body: CatalogBody = serde_json::from_value(value).expect("body");
        assert_eq!(body.state, CatalogState::Ok);
        assert_eq!(body.services.len(), 2);
        let first = &body.services[0];
        assert_eq!(first.service, "netbox");
        assert_eq!(first.name, "NetBox");
        assert_eq!(first.template, "Deploy NetBox");
        assert!(first.running);
        assert!(!body.services[1].running);
        assert_eq!(not_ok_message(body.state), None);
    }

    #[test]
    fn an_admin_answer_decodes_the_same_and_its_host_count_is_not_read() {
        // The admin body carries deployed_hosts; the page names no such field, so it is dropped on decode.
        let value = serde_json::json!({
            "state": "ok",
            "services": [{
                "service": "netbox",
                "name": "NetBox",
                "template": "Deploy NetBox",
                "running": true,
                "deployed_hosts": 2
            }]
        });
        let body: CatalogBody = serde_json::from_value(value).expect("body");
        assert_eq!(
            body.services[0],
            CatalogService {
                service: "netbox".into(),
                name: "NetBox".into(),
                template: "Deploy NetBox".into(),
                running: true,
            }
        );
    }

    #[test]
    fn the_unconfigured_state_has_its_own_sentence() {
        let value = serde_json::json!({"state": "unconfigured", "services": []});
        let body: CatalogBody = serde_json::from_value(value).expect("body");
        assert_eq!(body.state, CatalogState::Unconfigured);
        assert_eq!(
            not_ok_message(body.state).as_deref(),
            Some("The service catalog is not configured for this console.")
        );
    }

    #[test]
    fn an_unknown_or_missing_state_is_unknown_not_empty() {
        let read = |text: &str| {
            let value = serde_json::json!({"state": text});
            serde_json::from_value::<CatalogBody>(value)
                .expect("body")
                .state
        };
        assert_eq!(read("ok"), CatalogState::Ok);
        assert_eq!(read("degraded-new-word"), CatalogState::Unknown);
        let missing: CatalogBody = serde_json::from_value(serde_json::json!({})).expect("body");
        assert_eq!(missing.state, CatalogState::Unknown);
        assert!(not_ok_message(CatalogState::Unknown).is_some());
    }

    #[test]
    fn a_missing_service_key_reads_as_empty_not_as_an_error() {
        let value = serde_json::json!({"state": "ok", "services": [{"service": "x"}]});
        let body: CatalogBody = serde_json::from_value(value).expect("body");
        assert_eq!(body.services[0].service, "x");
        assert_eq!(body.services[0].name, "");
        assert!(!body.services[0].running);
    }
}
