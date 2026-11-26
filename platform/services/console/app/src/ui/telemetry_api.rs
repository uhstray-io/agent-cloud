//! The Telemetry page's answer as the browser reads it: `GET /v1/telemetry/rollups`, admin-only (plan 17, 9.2,
//! D43). Target health and Grafana alert counts, rolled up by environment, then by the Prometheus `cluster`
//! label, then by service. Every struct carries `#[serde(default)]`, so a missing key reads as empty, and an
//! `Option` field reads `null` as `None`. Nothing in this file writes to the API.

use serde::Deserialize;

use super::status::DASH;
use super::work::{age_text, Tone};

/// The admin-only telemetry answer.
pub const TELEMETRY_PATH: &str = "/v1/telemetry/rollups";

/// The state of the whole answer. Any other word is `Unknown`, which the page shows as a failed answer
/// rather than as an empty page.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum TelemetryState {
    Ok,
    Unconfigured,
    /// No read has finished yet. Also the value when the envelope omits the field.
    #[default]
    Loading,
    Error,
    #[serde(other)]
    Unknown,
}

/// The state of the Grafana alert read. Only `ok` shows alert counts. Any other word, and a missing field,
/// is not `ok`: the counts are then shown as a dash, never as zero.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum AlertsState {
    Ok,
    Error,
    /// Also the value when the field is missing.
    #[default]
    #[serde(other)]
    Unknown,
}

/// `GET /v1/telemetry/rollups`.
#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct TelemetryBody {
    pub state: TelemetryState,
    /// When the newest good read finished, RFC 3339. `None` until a read has finished.
    pub refreshed_at: Option<String>,
    /// The sentence the server gives when the read failed or is partial. Shown even when `state` is `ok`.
    pub error: Option<String>,
    pub alerts: AlertsRead,
    pub environments: Vec<Environment>,
}

/// The alert read's state and, when it did not finish, the sentence that says why.
#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct AlertsRead {
    pub state: AlertsState,
    pub error: Option<String>,
}

impl AlertsRead {
    /// Whether the alert counts are a finished read, and so may be shown.
    pub fn counts_shown(&self) -> bool {
        self.state == AlertsState::Ok
    }
}

/// The counts one row carries. The target counts come from Prometheus. The alert counts come from Grafana and
/// are shown only when the alert read is `ok` (see `alert_text`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Deserialize)]
#[serde(default)]
pub struct Counts {
    pub targets_up: u64,
    pub targets_total: u64,
    pub firing: u64,
    pub pending: u64,
}

/// One environment, the top rollup. `name` is `(none)` when the label is missing.
#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct Environment {
    pub name: String,
    #[serde(flatten)]
    pub counts: Counts,
    pub clusters: Vec<Cluster>,
}

/// One Prometheus `cluster` label within an environment. `name` is `(none)` when the label is missing.
#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct Cluster {
    pub name: String,
    #[serde(flatten)]
    pub counts: Counts,
    pub services: Vec<Service>,
}

/// One service within a cluster. `grafana_url` is `null` when the service has no overview to link to.
#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct Service {
    pub name: String,
    #[serde(flatten)]
    pub counts: Counts,
    pub grafana_url: Option<String>,
}

/// The health of a row from its targets, as the icon name, the tone and the word the page draws. A row with no
/// targets has no health to report, so it is not "down".
pub fn health(up: u64, total: u64) -> (&'static str, Tone, &'static str) {
    if total == 0 {
        ("circle-dashed", Tone::Idle, "No targets")
    } else if up == total {
        ("circle-check", Tone::Ok, "All up")
    } else if up == 0 {
        ("octagon-x", Tone::Crit, "Down")
    } else {
        ("triangle-alert", Tone::Warn, "Partly down")
    }
}

/// The target count a row shows, such as `7 of 7 up`.
pub fn targets_text(up: u64, total: u64) -> String {
    format!("{up} of {total} up")
}

/// An alert count, or an em dash when the alert read did not finish. A failed read is never shown as zero.
pub fn alert_text(count: u64, counts_shown: bool) -> String {
    if counts_shown {
        count.to_string()
    } else {
        DASH.to_string()
    }
}

/// The banner for an alert read that did not finish: the server's sentence, then what is still shown. `None`
/// when the alert read is `ok`.
pub fn alerts_notice(alerts: &AlertsRead) -> Option<String> {
    if alerts.counts_shown() {
        return None;
    }
    Some(match alerts.error.as_deref() {
        Some(error) => format!("{error} Target health is shown; alert counts are not."),
        None => "Grafana alerts could not be read, and no reason was given. Target health is shown; alert counts are not."
            .to_string(),
    })
}

/// The sentence an answer with no rows shows. `None` when the state has rows to show.
pub fn not_ok_message(state: TelemetryState, error: Option<&str>) -> Option<String> {
    match state {
        TelemetryState::Ok => None,
        TelemetryState::Unconfigured => {
            Some("Grafana is not configured for this console.".to_string())
        }
        TelemetryState::Loading => Some("The first telemetry read has not finished.".to_string()),
        TelemetryState::Error => Some(format!(
            "Telemetry could not be read: {}. The next read tries again.",
            error.unwrap_or("no reason was given")
        )),
        TelemetryState::Unknown => {
            Some("The console received a telemetry state it does not know.".to_string())
        }
    }
}

/// When the answer was read, as the header says it: "Read 4m ago" against the client clock, or "Read at
/// <stamp>" when the clock or the stamp cannot be read.
pub fn read_label(now_ms: Option<i64>, stamp: &str) -> String {
    match age_text(now_ms, stamp) {
        Some(age) => format!("Read {age}"),
        None => format!("Read at {stamp}"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The contract's example answer. One service carries a Grafana link and one has none.
    fn contract_example() -> serde_json::Value {
        serde_json::json!({
            "state": "ok",
            "refreshed_at": "2026-11-08T09:00:00Z",
            "error": null,
            "alerts": {"state": "ok", "error": null},
            "environments": [{
                "name": "prod",
                "targets_up": 11,
                "targets_total": 11,
                "firing": 0,
                "pending": 0,
                "clusters": [{
                    "name": "agent-cloud-prod",
                    "targets_up": 7,
                    "targets_total": 7,
                    "firing": 0,
                    "pending": 0,
                    "services": [{
                        "name": "o11y/grafana",
                        "targets_up": 1,
                        "targets_total": 1,
                        "firing": 0,
                        "pending": 0,
                        "grafana_url": "https://grafana.example.test/d/o11y"
                    }, {
                        "name": "(none)",
                        "targets_up": 0,
                        "targets_total": 2,
                        "firing": 0,
                        "pending": 0,
                        "grafana_url": null
                    }]
                }]
            }]
        })
    }

    #[test]
    fn each_server_state_word_reads_as_its_state() {
        let read = |text: &str| {
            let value = serde_json::json!({"state": text, "environments": []});
            serde_json::from_value::<TelemetryBody>(value)
                .expect("body")
                .state
        };
        assert_eq!(read("ok"), TelemetryState::Ok);
        assert_eq!(read("unconfigured"), TelemetryState::Unconfigured);
        assert_eq!(read("loading"), TelemetryState::Loading);
        assert_eq!(read("error"), TelemetryState::Error);
        assert_eq!(read("degraded-new-word"), TelemetryState::Unknown);
    }

    #[test]
    fn each_alerts_state_word_reads_as_its_state_and_only_ok_shows_counts() {
        let read = |alerts: serde_json::Value| {
            let value = serde_json::json!({"state": "ok", "alerts": alerts});
            serde_json::from_value::<TelemetryBody>(value)
                .expect("body")
                .alerts
        };
        assert_eq!(
            read(serde_json::json!({"state": "ok"})).state,
            AlertsState::Ok
        );
        assert_eq!(
            read(serde_json::json!({"state": "error"})).state,
            AlertsState::Error
        );
        assert_eq!(
            read(serde_json::json!({"state": "degraded-new-word"})).state,
            AlertsState::Unknown
        );
        // A missing `alerts` object is not an `ok` read, so its counts are not shown as zero.
        let missing: TelemetryBody =
            serde_json::from_value(serde_json::json!({"state": "ok"})).expect("body");
        assert_eq!(missing.alerts.state, AlertsState::Unknown);
        assert!(!missing.alerts.counts_shown());
        assert!(read(serde_json::json!({"state": "ok"})).counts_shown());
        assert!(!read(serde_json::json!({"state": "error"})).counts_shown());
    }

    #[test]
    fn the_contract_example_reads_its_rollups_and_links() {
        let body: TelemetryBody = serde_json::from_value(contract_example()).expect("body");
        assert_eq!(body.state, TelemetryState::Ok);
        assert_eq!(body.refreshed_at.as_deref(), Some("2026-11-08T09:00:00Z"));
        assert_eq!(body.error, None);
        assert!(body.alerts.counts_shown());
        let env = &body.environments[0];
        assert_eq!(env.name, "prod");
        assert_eq!(env.counts.targets_up, 11);
        assert_eq!(env.counts.targets_total, 11);
        let cluster = &env.clusters[0];
        assert_eq!(cluster.name, "agent-cloud-prod");
        assert_eq!(cluster.counts.targets_total, 7);
        assert_eq!(cluster.services[0].name, "o11y/grafana");
        assert_eq!(
            cluster.services[0].grafana_url.as_deref(),
            Some("https://grafana.example.test/d/o11y")
        );
        assert_eq!(cluster.services[1].name, "(none)");
        assert_eq!(cluster.services[1].counts.targets_up, 0);
        assert_eq!(cluster.services[1].grafana_url, None);
    }

    #[test]
    fn a_failed_alert_read_hides_its_counts_and_names_its_error() {
        let value = serde_json::json!({
            "state": "ok",
            "alerts": {"state": "error", "error": "Grafana answered HTTP 503."},
            "environments": [{
                "name": "prod",
                "targets_up": 4,
                "targets_total": 11,
                "firing": 0,
                "pending": 0,
                "clusters": []
            }]
        });
        let body: TelemetryBody = serde_json::from_value(value).expect("body");
        assert!(!body.alerts.counts_shown());
        // The zero in the answer is not a count: the page shows a dash.
        assert_eq!(
            alert_text(body.environments[0].counts.firing, false),
            "\u{2014}"
        );
        assert_eq!(alert_text(3, true), "3");
        // Target health still reads from the targets.
        let (_, tone, label) = health(
            body.environments[0].counts.targets_up,
            body.environments[0].counts.targets_total,
        );
        assert_eq!((tone, label), (Tone::Warn, "Partly down"));
        assert_eq!(
            alerts_notice(&body.alerts).as_deref(),
            Some("Grafana answered HTTP 503. Target health is shown; alert counts are not.")
        );
    }

    #[test]
    fn a_failed_alert_read_with_no_reason_still_says_the_counts_are_hidden() {
        let alerts = AlertsRead {
            state: AlertsState::Error,
            error: None,
        };
        let notice = alerts_notice(&alerts).expect("notice");
        assert!(notice.contains("no reason was given"));
        assert!(notice.ends_with("Target health is shown; alert counts are not."));
    }

    #[test]
    fn an_ok_alert_read_has_no_notice() {
        let alerts = AlertsRead {
            state: AlertsState::Ok,
            error: None,
        };
        assert_eq!(alerts_notice(&alerts), None);
    }

    #[test]
    fn health_names_each_case_with_its_icon_tone_and_word() {
        assert_eq!(health(0, 0), ("circle-dashed", Tone::Idle, "No targets"));
        assert_eq!(health(11, 11), ("circle-check", Tone::Ok, "All up"));
        assert_eq!(health(0, 11), ("octagon-x", Tone::Crit, "Down"));
        assert_eq!(health(4, 11), ("triangle-alert", Tone::Warn, "Partly down"));
    }

    #[test]
    fn target_counts_read_as_words() {
        assert_eq!(targets_text(7, 7), "7 of 7 up");
        assert_eq!(targets_text(0, 2), "0 of 2 up");
    }

    #[test]
    fn only_an_ok_answer_has_rows() {
        assert_eq!(not_ok_message(TelemetryState::Ok, None), None);
        assert_eq!(
            not_ok_message(TelemetryState::Unconfigured, None).as_deref(),
            Some("Grafana is not configured for this console.")
        );
        assert_eq!(
            not_ok_message(TelemetryState::Loading, None).as_deref(),
            Some("The first telemetry read has not finished.")
        );
        assert_eq!(
            not_ok_message(TelemetryState::Error, Some("Prometheus answered HTTP 502")).as_deref(),
            Some("Telemetry could not be read: Prometheus answered HTTP 502. The next read tries again.")
        );
        assert!(not_ok_message(TelemetryState::Error, None)
            .expect("message")
            .contains("no reason was given"));
        assert!(not_ok_message(TelemetryState::Unknown, None).is_some());
    }

    #[test]
    fn the_read_age_is_relative_to_the_client_clock_and_falls_back_to_the_stamp() {
        // 2026-11-08T09:00:00Z, four minutes earlier on the client clock.
        let stamp = "2026-11-08T09:00:00Z";
        let read_at = 1_794_128_400_000_i64;
        assert_eq!(read_label(Some(read_at + 240_000), stamp), "Read 4m ago");
        assert_eq!(read_label(None, stamp), "Read at 2026-11-08T09:00:00Z");
        assert_eq!(
            read_label(Some(read_at), "not a time"),
            "Read at not a time"
        );
    }

    #[test]
    fn a_missing_state_reads_as_loading_and_missing_rows_as_none() {
        let body: TelemetryBody =
            serde_json::from_value(serde_json::json!({"error": null})).expect("body");
        assert_eq!(body.state, TelemetryState::Loading);
        assert_eq!(body.error, None);
        assert_eq!(body.refreshed_at, None);
        assert!(body.environments.is_empty());
        assert_eq!(body.alerts.state, AlertsState::Unknown);
    }
}
