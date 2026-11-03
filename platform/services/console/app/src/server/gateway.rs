//! The agentgateway clients, budgets and models the console's gateway page shows (plan 17, task 8.3a).
//!
//! `config/gateway.json` is rendered at deploy from the agentgateway host's vars
//! (`platform/services/console/deployment/templates/gateway.json.j2`) and read once at startup. It carries
//! names and figures only. A key, a `client_<name>` field, an address or a port is not a field here, and
//! `deny_unknown_fields` makes the server refuse a file that carries one, so a bad render stops startup
//! instead of reaching a response.
//!
//! `GET /v1/gateway` is platform-wide, like `/v1/flows`: the clients belong to the gateway, not to a tenant.
//! It is platform admins only (`work::admin_only`, D25).

use std::path::Path;

use dioxus::server::axum::extract::State;
use dioxus::server::axum::response::{IntoResponse, Response};
use dioxus::server::axum::Json;
use serde::{Deserialize, Serialize};
use tower_sessions::Session;

use super::work::{self, Source};
use super::SharedState;

/// The file as deploy-console.yml renders it. The field names are the wire contract with `gateway.json.j2`:
/// `test_rendered_gateway_json_matches_the_servers_structs` checks the template's output against them.
#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct GatewayFile {
    pub clients: Vec<GatewayClient>,
    pub models: Vec<String>,
}

/// One gateway client: a service identity from `agw_clients`, with the figure the gateway enforces for it.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct GatewayClient {
    pub name: String,
    /// The tier the gateway budgets it under, or null when it has none.
    pub tier: Option<String>,
    pub tokens_per_hour: u64,
    pub budget_source: BudgetSource,
    /// The public model names this client may call.
    pub allowed_models: Vec<String>,
}

/// Where a client's hourly figure comes from: its own policy, its tier, or the default rate.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum BudgetSource {
    Client,
    Tier,
    Default,
}

/// What `GET /v1/gateway` answers. `unconfigured` with empty lists when no file is set.
#[derive(Debug, Serialize)]
pub struct GatewaySet {
    pub state: Source,
    pub clients: Vec<GatewayClient>,
    pub models: Vec<String>,
}

impl GatewaySet {
    pub fn unconfigured() -> Self {
        GatewaySet {
            state: Source::Unconfigured,
            clients: Vec::new(),
            models: Vec::new(),
        }
    }

    pub fn configured(file: GatewayFile) -> Self {
        GatewaySet {
            state: Source::Ok,
            clients: file.clients,
            models: file.models,
        }
    }
}

/// Reads `CONSOLE_GATEWAY_FILE`. A configured file that cannot be read or parsed fails startup, as the flows file does.
pub fn load_gateway(path: &Path) -> Result<GatewayFile, String> {
    let text = std::fs::read_to_string(path).map_err(|err| {
        format!(
            "CONSOLE_GATEWAY_FILE: cannot read {}: {err}",
            path.display()
        )
    })?;
    serde_json::from_str(&text).map_err(|err| format!("CONSOLE_GATEWAY_FILE: invalid JSON: {err}"))
}

/// `GET /v1/gateway`: the rendered clients, budgets and models. Platform admins only.
pub async fn get_gateway(State(app): State<SharedState>, session: Session) -> Response {
    if let Err(response) = work::admin_only(&session).await {
        return response;
    }
    Json(&app.gateway).into_response()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn gateway_file(name: &str, text: &str) -> std::path::PathBuf {
        let path = std::env::temp_dir().join(format!(
            "console-gateway-{}-{name}.json",
            std::process::id()
        ));
        std::fs::write(&path, text).expect("write");
        path
    }

    const GOOD: &str = r#"{
        "clients": [
            {"name": "alpha", "tier": "developer", "tokens_per_hour": 300000,
             "budget_source": "tier", "allowed_models": ["m1"]},
            {"name": "beta", "tier": null, "tokens_per_hour": 2000000,
             "budget_source": "default", "allowed_models": []}
        ],
        "models": ["m1", "m2"]
    }"#;

    #[test]
    fn load_gateway_reads_the_declared_shape() {
        let file = load_gateway(&gateway_file("good", GOOD)).expect("valid");
        assert_eq!(file.models, vec!["m1", "m2"]);
        assert_eq!(file.clients[0].budget_source, BudgetSource::Tier);
        assert_eq!(file.clients[0].tier.as_deref(), Some("developer"));
        assert_eq!(file.clients[1].tier, None);
        assert!(file.clients[1].allowed_models.is_empty());
    }

    #[test]
    fn load_gateway_names_the_setting_on_every_error() {
        let err = load_gateway(&gateway_file("bad", "{")).expect_err("must fail");
        assert!(
            err.starts_with("CONSOLE_GATEWAY_FILE: invalid JSON"),
            "{err}"
        );
        let missing = load_gateway(&std::env::temp_dir().join("console-gateway-absent.json"))
            .expect_err("must fail");
        assert!(
            missing.starts_with("CONSOLE_GATEWAY_FILE: cannot read"),
            "{missing}"
        );
    }

    #[test]
    fn a_key_or_address_field_is_refused_not_kept() {
        // Names and figures only: an unknown field is a startup failure, never a silently kept value.
        for injected in [
            r#""key": "sk-live-value""#,
            r#""client_alpha": "sk-live-value""#,
            r#""upstream": "198.51.100.9:8000""#,
        ] {
            let text = GOOD.replacen(
                r#""name": "alpha","#,
                &format!(r#""name": "alpha", {injected},"#),
                1,
            );
            let err = load_gateway(&gateway_file("inject", &text)).expect_err("must fail");
            assert!(
                err.starts_with("CONSOLE_GATEWAY_FILE: invalid JSON"),
                "{err}"
            );
            assert!(err.contains("unknown field"), "{err}");
        }
        let top = GOOD.replacen(r#""models""#, r#""extra": 1, "models""#, 1);
        assert!(load_gateway(&gateway_file("top", &top)).is_err());
    }

    #[test]
    fn an_unknown_budget_source_is_refused() {
        let text = GOOD.replacen(
            r#""budget_source": "tier""#,
            r#""budget_source": "vendor""#,
            1,
        );
        assert!(load_gateway(&gateway_file("source", &text)).is_err());
    }

    #[test]
    fn unconfigured_and_configured_serialize_to_the_wire_shape() {
        assert_eq!(
            serde_json::to_string(&GatewaySet::unconfigured()).expect("json"),
            r#"{"state":"unconfigured","clients":[],"models":[]}"#
        );
        let set = GatewaySet::configured(load_gateway(&gateway_file("wire", GOOD)).expect("valid"));
        assert_eq!(
            serde_json::to_string(&set).expect("json"),
            r#"{"state":"ok","clients":[{"name":"alpha","tier":"developer","tokens_per_hour":300000,"budget_source":"tier","allowed_models":["m1"]},{"name":"beta","tier":null,"tokens_per_hour":2000000,"budget_source":"default","allowed_models":[]}],"models":["m1","m2"]}"#
        );
    }
}
