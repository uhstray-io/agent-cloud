//! The AI Gateway and LLM access pages' answer as the browser reads it: `GET /v1/gateway`,
//! platform admins only. The shape is the one the server serializes. Every struct carries
//! `#[serde(default)]`, so a missing key reads as empty. Nothing in this file writes to the API,
//! and the answer carries no key or credential.

use serde::Deserialize;

/// The platform-admin gateway answer.
pub const GATEWAY_PATH: &str = "/v1/gateway";

/// The state the server names. Any other word, and a missing field, is `Unknown`, which the
/// pages show as a failed answer rather than as an empty table.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum GatewayState {
    Ok,
    Unconfigured,
    #[default]
    #[serde(other)]
    Unknown,
}

/// Where a client's hourly budget came from. Any other word is `Unknown`, which shows no label.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum BudgetSource {
    Client,
    Tier,
    Default,
    #[default]
    #[serde(other)]
    Unknown,
}

/// The state of the usage read. Any other word, and a missing field, is `Unknown`, which the page shows as a
/// notice rather than as a figure.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum UsageState {
    Ok,
    Unconfigured,
    Loading,
    Error,
    #[default]
    #[serde(other)]
    Unknown,
}

/// The usage block of `GET /v1/gateway`: the read's state, when its last good figures were read, and the
/// sentence that says why the newest read failed, when it did.
#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct Usage {
    pub state: UsageState,
    pub refreshed_at: Option<String>,
    pub error: Option<String>,
}

/// `GET /v1/gateway`.
#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct GatewayBody {
    pub state: GatewayState,
    pub clients: Vec<GatewayClient>,
    pub models: Vec<String>,
    pub usage: Usage,
}

/// One gateway client: its name, its tier when it has one, its hourly token budget, the models it may call, and
/// its last-hour token count from the usage read (`None` when the read has no figure for it).
#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct GatewayClient {
    pub name: String,
    pub tier: Option<String>,
    pub tokens_per_hour: u64,
    pub budget_source: BudgetSource,
    pub allowed_models: Vec<String>,
    pub tokens_last_hour: Option<u64>,
}

/// The note under the Last hour column. The figure is the metric of the same traffic, not the gateway's counter.
pub const APPROXIMATE: &str =
    "Approximate: metric of the same traffic; the gateway enforces its own counter.";

/// One served model and the clients whose `allowed_models` name it, in the answer's order.
#[derive(Debug, Clone, PartialEq)]
pub struct ModelRow {
    pub model: String,
    pub clients: Vec<String>,
}

/// The sentence an answer with no table shows. `None` when the state has a table to show.
pub fn not_ok_message(state: GatewayState) -> Option<String> {
    match state {
        GatewayState::Ok => None,
        GatewayState::Unconfigured => {
            Some("The gateway catalogue is not configured for this console.".to_string())
        }
        GatewayState::Unknown => {
            Some("The console received a gateway state it does not know.".to_string())
        }
    }
}

/// A token budget with thousands separators: `250000000` reads `250,000,000`.
pub fn budget_text(tokens: u64) -> String {
    let digits = tokens.to_string();
    let mut grouped = String::with_capacity(digits.len() + digits.len() / 3);
    for (index, digit) in digits.chars().enumerate() {
        if index > 0 && (digits.len() - index).is_multiple_of(3) {
            grouped.push(',');
        }
        grouped.push(digit);
    }
    grouped
}

/// The small label beside a budget. `None` for a source the console does not know.
pub fn budget_source_text(source: BudgetSource) -> Option<&'static str> {
    match source {
        BudgetSource::Client => Some("client"),
        BudgetSource::Tier => Some("tier"),
        BudgetSource::Default => Some("default"),
        BudgetSource::Unknown => None,
    }
}

/// The Last hour cell's text: thousands separators, or an em dash when no figure is known.
pub fn last_hour_text(tokens: Option<u64>) -> String {
    tokens.map_or_else(|| "\u{2014}".to_string(), budget_text)
}

/// The bar's percent: the last hour's tokens against the hourly budget, capped at 100. `None` when there is no
/// figure or the budget is zero, so no bar is drawn.
pub fn usage_percent(tokens: Option<u64>, budget: u64) -> Option<f64> {
    let tokens = tokens?;
    if budget == 0 {
        return None;
    }
    Some((tokens as f64 * 100.0 / budget as f64).min(100.0))
}

/// The line that says the usage figures are not current, or `None` when they are. The server's sentence wins
/// whenever it is present, since a failed read with a last good figure is `error` and still shows figures.
pub fn usage_notice(usage: &Usage) -> Option<String> {
    if let Some(error) = usage.error.as_deref() {
        return Some(error.to_string());
    }
    match usage.state {
        UsageState::Ok => None,
        UsageState::Unconfigured => {
            Some("Token usage is not configured for this console.".to_string())
        }
        UsageState::Loading => {
            Some("Token usage is loading: the first read has not finished.".to_string())
        }
        UsageState::Error => Some("The newest usage read failed.".to_string()),
        UsageState::Unknown => {
            Some("The console received a token usage state it does not know.".to_string())
        }
    }
}

/// Inverts the clients' allow lists: for each served model, the clients that may call it.
/// A model no client may call still gets a row, with no clients.
pub fn clients_by_model(clients: &[GatewayClient], models: &[String]) -> Vec<ModelRow> {
    models
        .iter()
        .map(|model| ModelRow {
            model: model.clone(),
            clients: clients
                .iter()
                .filter(|client| client.allowed_models.iter().any(|allowed| allowed == model))
                .map(|client| client.name.clone())
                .collect(),
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn client(name: &str, models: &[&str]) -> GatewayClient {
        GatewayClient {
            name: name.to_string(),
            allowed_models: models.iter().map(|m| m.to_string()).collect(),
            ..GatewayClient::default()
        }
    }

    #[test]
    fn an_ok_answer_decodes_its_clients_and_models() {
        let value = serde_json::json!({
            "state": "ok",
            "clients": [{
                "name": "research-team",
                "tier": "standard",
                "tokens_per_hour": 250000000,
                "budget_source": "tier",
                "allowed_models": ["qwen-coder", "llama-70b"]
            }],
            "models": ["qwen-coder", "llama-70b", "embed-small"]
        });
        let body: GatewayBody = serde_json::from_value(value).expect("body");
        assert_eq!(body.state, GatewayState::Ok);
        assert_eq!(body.models.len(), 3);
        let first = &body.clients[0];
        assert_eq!(first.name, "research-team");
        assert_eq!(first.tier.as_deref(), Some("standard"));
        assert_eq!(first.tokens_per_hour, 250_000_000);
        assert_eq!(first.budget_source, BudgetSource::Tier);
        assert_eq!(first.allowed_models, ["qwen-coder", "llama-70b"]);
        assert_eq!(not_ok_message(body.state), None);
    }

    #[test]
    fn a_null_tier_reads_as_none_and_a_missing_key_as_empty() {
        let value = serde_json::json!({
            "state": "ok",
            "clients": [{"name": "ops", "tier": null, "tokens_per_hour": 0}]
        });
        let body: GatewayBody = serde_json::from_value(value).expect("body");
        assert_eq!(body.clients[0].tier, None);
        assert!(body.clients[0].allowed_models.is_empty());
        assert_eq!(body.clients[0].budget_source, BudgetSource::Unknown);
        assert!(body.models.is_empty());
    }

    #[test]
    fn the_unconfigured_state_has_its_own_sentence() {
        let value = serde_json::json!({"state": "unconfigured", "clients": [], "models": []});
        let body: GatewayBody = serde_json::from_value(value).expect("body");
        assert_eq!(body.state, GatewayState::Unconfigured);
        assert_eq!(
            not_ok_message(body.state).as_deref(),
            Some("The gateway catalogue is not configured for this console.")
        );
    }

    #[test]
    fn an_unknown_or_missing_state_is_unknown_not_empty() {
        let read = |text: &str| {
            let value = serde_json::json!({"state": text});
            serde_json::from_value::<GatewayBody>(value)
                .expect("body")
                .state
        };
        assert_eq!(read("ok"), GatewayState::Ok);
        assert_eq!(read("degraded-new-word"), GatewayState::Unknown);
        let missing: GatewayBody = serde_json::from_value(serde_json::json!({})).expect("body");
        assert_eq!(missing.state, GatewayState::Unknown);
        assert!(not_ok_message(GatewayState::Unknown).is_some());
    }

    #[test]
    fn an_unknown_budget_source_word_is_unknown_and_unlabelled() {
        let value = serde_json::json!({"name": "x", "budget_source": "override"});
        let client: GatewayClient = serde_json::from_value(value).expect("client");
        assert_eq!(client.budget_source, BudgetSource::Unknown);
        assert_eq!(budget_source_text(client.budget_source), None);
        assert_eq!(budget_source_text(BudgetSource::Client), Some("client"));
        assert_eq!(budget_source_text(BudgetSource::Tier), Some("tier"));
        assert_eq!(budget_source_text(BudgetSource::Default), Some("default"));
    }

    #[test]
    fn budgets_group_by_thousands() {
        assert_eq!(budget_text(0), "0");
        assert_eq!(budget_text(999), "999");
        assert_eq!(budget_text(1000), "1,000");
        assert_eq!(budget_text(12345), "12,345");
        assert_eq!(budget_text(250_000_000), "250,000,000");
        assert_eq!(budget_text(u64::MAX), "18,446,744,073,709,551,615");
    }

    #[test]
    fn a_budget_at_the_u64_ceiling_decodes_as_the_server_sends_it() {
        let value = serde_json::json!({
            "name": "ceiling",
            "tokens_per_hour": 18446744073709551615u64
        });
        let client: GatewayClient = serde_json::from_value(value).expect("client");
        assert_eq!(client.tokens_per_hour, u64::MAX);
        assert_eq!(
            budget_text(client.tokens_per_hour),
            "18,446,744,073,709,551,615"
        );
    }

    #[test]
    fn each_model_lists_the_clients_that_may_call_it() {
        let clients = [
            client("research-team", &["qwen-coder", "llama-70b"]),
            client("ops", &["llama-70b"]),
            client("svc-none", &[]),
        ];
        let models: Vec<String> = ["qwen-coder", "llama-70b", "embed-small"]
            .iter()
            .map(|m| m.to_string())
            .collect();
        let rows = clients_by_model(&clients, &models);
        assert_eq!(rows.len(), 3);
        assert_eq!(rows[0].model, "qwen-coder");
        assert_eq!(rows[0].clients, ["research-team"]);
        assert_eq!(rows[1].model, "llama-70b");
        assert_eq!(rows[1].clients, ["research-team", "ops"]);
        assert_eq!(rows[2].model, "embed-small");
        assert!(rows[2].clients.is_empty());
    }

    #[test]
    fn a_model_named_by_a_client_but_not_served_gets_no_row() {
        let clients = [client("ops", &["retired-model"])];
        let models = ["llama-70b".to_string()];
        let rows = clients_by_model(&clients, &models);
        assert_eq!(rows.len(), 1);
        assert!(rows[0].clients.is_empty());
    }

    #[test]
    fn the_usage_block_and_each_clients_last_hour_decode() {
        let value = serde_json::json!({
            "state": "ok",
            "clients": [
                {"name": "research-team", "tokens_per_hour": 250000000, "tokens_last_hour": 1234},
                {"name": "ops", "tokens_per_hour": 10, "tokens_last_hour": null}
            ],
            "models": [],
            "usage": {"state": "error", "refreshed_at": "2026-11-04T10:00:00Z",
                      "error": "The newest usage read failed: Grafana answered HTTP 500. Showing the last good read."}
        });
        let body: GatewayBody = serde_json::from_value(value).expect("body");
        assert_eq!(body.usage.state, UsageState::Error);
        assert_eq!(
            body.usage.refreshed_at.as_deref(),
            Some("2026-11-04T10:00:00Z")
        );
        assert_eq!(body.clients[0].tokens_last_hour, Some(1234));
        assert_eq!(body.clients[1].tokens_last_hour, None);
    }

    #[test]
    fn a_missing_usage_block_or_state_word_is_unknown_not_ok() {
        let body: GatewayBody =
            serde_json::from_value(serde_json::json!({"state": "ok"})).expect("body");
        assert_eq!(body.usage.state, UsageState::Unknown);
        assert!(
            usage_notice(&body.usage).is_some(),
            "an unknown state is a notice"
        );
        let odd: Usage =
            serde_json::from_value(serde_json::json!({"state": "degraded"})).expect("usage");
        assert_eq!(odd.state, UsageState::Unknown);
    }

    #[test]
    fn last_hour_reads_thousands_or_a_dash() {
        assert_eq!(last_hour_text(Some(1_234_567)), "1,234,567");
        assert_eq!(last_hour_text(Some(0)), "0");
        assert_eq!(last_hour_text(None), "\u{2014}");
    }

    #[test]
    fn the_bar_is_the_share_of_budget_capped_and_absent_without_a_figure_or_budget() {
        assert_eq!(usage_percent(None, 1000), None, "no figure, no bar");
        assert_eq!(usage_percent(Some(5), 0), None, "no budget, no bar");
        assert_eq!(usage_percent(Some(250), 1000), Some(25.0));
        assert_eq!(usage_percent(Some(1000), 1000), Some(100.0));
        assert_eq!(
            usage_percent(Some(3000), 1000),
            Some(100.0),
            "capped at 100"
        );
        assert_eq!(usage_percent(Some(0), 1000), Some(0.0));
    }

    #[test]
    fn the_notice_names_every_state_and_shows_the_servers_sentence_first() {
        let usage = |state, error: Option<&str>| Usage {
            state,
            refreshed_at: None,
            error: error.map(str::to_string),
        };
        assert_eq!(usage_notice(&usage(UsageState::Ok, None)), None);
        assert_eq!(
            usage_notice(&usage(UsageState::Unconfigured, None)).as_deref(),
            Some("Token usage is not configured for this console.")
        );
        assert!(usage_notice(&usage(UsageState::Loading, None))
            .expect("loading")
            .contains("loading"));
        assert_eq!(
            usage_notice(&usage(UsageState::Error, None)).as_deref(),
            Some("The newest usage read failed.")
        );
        assert_eq!(
            usage_notice(&usage(
                UsageState::Error,
                Some("The newest usage read failed: boom. Showing the last good read.")
            ))
            .as_deref(),
            Some("The newest usage read failed: boom. Showing the last good read.")
        );
        assert!(usage_notice(&usage(UsageState::Unknown, None))
            .expect("unknown")
            .contains("does not know"));
    }
}
