//! The Telemetry page's answer (plan 17, task 9.2, D43): target health and Grafana alerts rolled up by environment,
//! Prometheus cluster label and service. A background task reads Grafana every `REFRESH_INTERVAL` into
//! `TelemetryCache`; `GET /v1/telemetry/rollups` only reads that cache, so a page load costs no Grafana request.
//!
//! Each refresh sends two GETs with the Viewer token (D32), both through the guard `grafana::guarded_url` applies:
//! the `up` series of Prometheus, through the datasource proxy the usage read uses, and the alert instances of
//! Grafana's alerting API. Targets are counted per (environment, cluster, service) label set. An alert in
//! `Alerting` counts as firing and one in `Pending` as pending. A failed `up` read keeps the last good rollup and
//! says so. A failed alerts read leaves that rollup's alert counts at zero and sets `alerts.state` to `error`.
//! No token, URL or body reaches an error or a log line.

use std::collections::{BTreeMap, HashMap};
use std::time::Duration;

use dioxus::server::axum::extract::State;
use dioxus::server::axum::response::{IntoResponse, Response};
use dioxus::server::axum::Json;
use serde::{Deserialize, Serialize};
use tokio::time::MissedTickBehavior;
use tower_sessions::Session;

use super::config::Secret;
use super::grafana::{self, guarded_url, instant_vector, read_answer, ReadError, USAGE_PATH};
use super::work::{self, rfc3339_now};
use super::{AppState, SharedState};

/// How often the telemetry read runs. Five minutes, the usage read's cadence (D39).
pub const REFRESH_INTERVAL: Duration = Duration::from_secs(300);

/// The PromQL of the target read: every series of `up`, each with the labels the page groups by.
const UP_QUERY: &str = "up";

/// Grafana's alerting API: the instance of every alert rule, with its state. The Viewer role holds
/// `fixed:alerting:reader` (Grafana docs, fixed basic role definitions).
const ALERTS_PATH: &str = "/api/prometheus/grafana/api/v1/alerts";

/// The counts every level of the rollup carries. The levels flatten them into their own object.
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq, Serialize)]
pub struct Counts {
    /// Targets whose `up` sample is 1.
    pub targets_up: u64,
    /// Every target the `up` read returned at this level.
    pub targets_total: u64,
    /// Alert instances in `Alerting`, any suffix.
    pub firing: u64,
    /// Alert instances in `Pending`, any suffix.
    pub pending: u64,
}

impl Counts {
    fn add(&mut self, other: Counts) {
        self.targets_up += other.targets_up;
        self.targets_total += other.targets_total;
        self.firing += other.firing;
        self.pending += other.pending;
    }
}

/// One service under a cluster. `grafana_url` is its Service Overview link, or `null` for a series with no
/// `service` label.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Service {
    pub name: String,
    #[serde(flatten)]
    pub counts: Counts,
    pub grafana_url: Option<String>,
}

/// One Prometheus cluster label under an environment. Its counts are the sum of its services.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Cluster {
    pub name: String,
    #[serde(flatten)]
    pub counts: Counts,
    pub services: Vec<Service>,
}

/// One environment label. Its counts are the sum of its clusters.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Environment {
    pub name: String,
    #[serde(flatten)]
    pub counts: Counts,
    pub clusters: Vec<Cluster>,
}

/// The alerts block: `error` is the sentence for the newest refresh's alerts read when that read failed, else
/// `null`.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct AlertsBlock {
    pub state: &'static str,
    pub error: Option<String>,
}

/// `GET /v1/telemetry/rollups`. `refreshed_at` and `environments` come from the last good read, and are `null` and
/// empty before one finishes.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct TelemetryBody {
    pub state: &'static str,
    pub refreshed_at: Option<String>,
    pub error: Option<String>,
    pub alerts: AlertsBlock,
    pub environments: Vec<Environment>,
}

/// One `up` series: its labels, and whether its sample is 1.
#[derive(Debug, Clone, PartialEq)]
struct UpTarget {
    labels: HashMap<String, String>,
    up: bool,
}

/// One alert instance from Grafana's alerting API: its labels and its state, such as `Alerting (NoData)`.
#[derive(Debug, Clone, PartialEq, Deserialize)]
struct AlertInstance {
    #[serde(default)]
    labels: HashMap<String, String>,
    state: String,
}

#[derive(Deserialize)]
struct AlertsAnswer {
    status: String,
    data: Option<AlertsData>,
}

#[derive(Deserialize)]
struct AlertsData {
    /// Grafana may send `null`, or no list, when no rule has an instance.
    alerts: Option<Vec<AlertInstance>>,
}

/// The last rollup that finished, with the alerts outcome of the same refresh.
struct Good {
    environments: Vec<Environment>,
    refreshed_at: String,
    /// Why that refresh's alerts read failed, or `None` when it answered.
    alerts_failure: Option<String>,
}

/// What the route reads. `good` is the last refresh whose target read finished. `failure` is why the newest target
/// read failed, as its reason only: a finished read clears it, and a failed one keeps `good` in place.
pub struct TelemetryCache {
    configured: bool,
    good: Option<Good>,
    failure: Option<String>,
}

impl TelemetryCache {
    /// `configured` is whether the Grafana origin and the token are both set. When they are not, no read runs and
    /// the page says so.
    pub fn new(configured: bool) -> Self {
        Self {
            configured,
            good: None,
            failure: None,
        }
    }

    /// Stores a finished refresh and clears the failure. `alerts_failure` is why its alerts read failed, if it did.
    fn record_ok(
        &mut self,
        environments: Vec<Environment>,
        alerts_failure: Option<String>,
        refreshed_at: String,
    ) {
        self.failure = None;
        self.good = Some(Good {
            environments,
            refreshed_at,
            alerts_failure,
        });
    }

    /// Keeps the last good rollup, if there is one, and stores the reason this read failed.
    fn record_error(&mut self, reason: String) {
        self.failure = Some(reason);
    }

    /// The sentence under the rollup, or none. A failed read names its reason, and says whether the last good
    /// rollup is still shown.
    fn error_sentence(&self) -> Option<String> {
        let reason = reason_text(self.failure.as_deref()?);
        Some(match &self.good {
            Some(_) => {
                format!("The newest telemetry read failed: {reason}. Showing the last good read.")
            }
            None => format!("The telemetry read failed: {reason}."),
        })
    }

    /// The alerts block: the alerts outcome of the rollup being shown. With no good rollup yet it is `ok`.
    fn alerts_block(&self) -> AlertsBlock {
        match self
            .good
            .as_ref()
            .and_then(|good| good.alerts_failure.as_deref())
        {
            Some(reason) => AlertsBlock {
                state: "error",
                error: Some(format!(
                    "Grafana alerts could not be read: {}.",
                    reason_text(reason)
                )),
            },
            None => AlertsBlock {
                state: "ok",
                error: None,
            },
        }
    }

    /// The body `GET /v1/telemetry/rollups` serves. `state` is `unconfigured` without the source, `loading` before
    /// the first read finishes, `error` when no read has finished and the newest failed, and `ok` otherwise.
    pub fn body(&self) -> TelemetryBody {
        let state = match (self.configured, &self.good) {
            (false, _) => "unconfigured",
            (true, Some(_)) => "ok",
            (true, None) if self.failure.is_some() => "error",
            (true, None) => "loading",
        };
        let (refreshed_at, environments) = match &self.good {
            Some(good) => (Some(good.refreshed_at.clone()), good.environments.clone()),
            None => (None, Vec::new()),
        };
        TelemetryBody {
            state,
            refreshed_at,
            error: self.error_sentence(),
            alerts: self.alerts_block(),
            environments,
        }
    }
}

/// A failure reason without its final full stop. Some refusals carry one, and the sentences add their own.
fn reason_text(reason: &str) -> &str {
    reason.trim_end_matches('.')
}

/// The `(environment, cluster, service)` a series or an alert instance is grouped under. A missing or empty label
/// is `None`, which the page shows as `(none)`.
type Group = (Option<String>, Option<String>, Option<String>);

fn group_of(labels: &HashMap<String, String>) -> Group {
    let label = |name: &str| labels.get(name).filter(|value| !value.is_empty()).cloned();
    (label("environment"), label("cluster"), label("service"))
}

/// The name the page shows for a group label.
fn shown(name: Option<&str>) -> String {
    name.unwrap_or("(none)").to_string()
}

/// The rollup of one refresh. Targets are counted per group, and alert instances add to the group of their labels,
/// so a group that only has alerts still gets a row, with no targets. Every level is sorted by name, and each
/// environment and cluster total is the sum of its children.
fn rollup(targets: &[UpTarget], alerts: &[AlertInstance], base: &str) -> Vec<Environment> {
    let mut groups: BTreeMap<Group, Counts> = BTreeMap::new();
    for target in targets {
        let counts = groups.entry(group_of(&target.labels)).or_default();
        counts.targets_total += 1;
        if target.up {
            counts.targets_up += 1;
        }
    }
    for alert in alerts {
        let counts = groups.entry(group_of(&alert.labels)).or_default();
        if alert.state.starts_with("Alerting") {
            counts.firing += 1;
        } else if alert.state.starts_with("Pending") {
            counts.pending += 1;
        }
    }

    type Clusters = BTreeMap<Option<String>, BTreeMap<Option<String>, Counts>>;
    let mut tree: BTreeMap<Option<String>, Clusters> = BTreeMap::new();
    for ((environment, cluster, service), counts) in groups {
        tree.entry(environment)
            .or_default()
            .entry(cluster)
            .or_default()
            .insert(service, counts);
    }

    tree.into_iter()
        .map(|(environment, clusters)| {
            let mut environment_total = Counts::default();
            let clusters = clusters
                .into_iter()
                .map(|(cluster, services)| {
                    let mut cluster_total = Counts::default();
                    let services = services
                        .into_iter()
                        .map(|(service, counts)| {
                            cluster_total.add(counts);
                            Service {
                                name: shown(service.as_deref()),
                                counts,
                                grafana_url: service
                                    .as_deref()
                                    .and_then(|name| grafana::service_overview(base, name)),
                            }
                        })
                        .collect();
                    environment_total.add(cluster_total);
                    Cluster {
                        name: shown(cluster.as_deref()),
                        counts: cluster_total,
                        services,
                    }
                })
                .collect();
            Environment {
                name: shown(environment.as_deref()),
                counts: environment_total,
                clusters,
            }
        })
        .collect()
}

/// The `up` series of the target read. A sample that does not parse to 1 is a target that does not answer.
fn parse_up(body: &[u8]) -> Result<Vec<UpTarget>, ReadError> {
    Ok(instant_vector(body)?
        .into_iter()
        .map(|(labels, sample)| UpTarget {
            labels,
            up: sample.parse::<f64>().is_ok_and(|value| value == 1.0),
        })
        .collect())
}

/// The alert instances of the alerts read. A body that is not JSON is `Decode`. A status that is not `success` is
/// `Shape`. A missing list is no instances.
fn parse_alerts(body: &[u8]) -> Result<Vec<AlertInstance>, ReadError> {
    let answer: AlertsAnswer = serde_json::from_slice(body).map_err(|_| ReadError::Decode)?;
    if answer.status != "success" {
        return Err(ReadError::Shape);
    }
    Ok(answer.data.and_then(|data| data.alerts).unwrap_or_default())
}

/// Why the target read failed, as the sentence names it. The shared text for a refused shape names a usage answer,
/// which this read is not, so a refused shape gets its own text here.
fn up_reason(err: &ReadError) -> String {
    match err {
        ReadError::Shape => "Grafana's answer is not a success vector of target series".to_string(),
        other => other.to_string(),
    }
}

/// Why the alerts read failed. The shared 403 text names the Prometheus data source, which a refused alerts read is
/// not, so that status keeps its bare code. A refused shape gets its own text.
fn alerts_reason(err: &ReadError) -> String {
    match err {
        ReadError::Status(403) => "Grafana answered HTTP 403".to_string(),
        ReadError::Shape => "Grafana's alerts answer did not report success".to_string(),
        other => other.to_string(),
    }
}

/// `GET /v1/telemetry/rollups` (platform admins only, D25). Reads the cache, never Grafana.
pub async fn get_telemetry(State(app): State<SharedState>, session: Session) -> Response {
    if let Err(response) = work::admin_only(&session).await {
        return response;
    }
    let cache = app.telemetry.read().await;
    Json(cache.body()).into_response()
}

/// The target read: the `up` query through Grafana's Prometheus proxy.
async fn read_targets(
    http: &reqwest::Client,
    base: &str,
    token: &Secret,
) -> Result<Vec<UpTarget>, ReadError> {
    let url = guarded_url(base, USAGE_PATH, &[("query", UP_QUERY)])?;
    read_answer(http, url, token, parse_up).await
}

/// The alerts read: Grafana's alerting API, instances and states.
async fn read_alerts(
    http: &reqwest::Client,
    base: &str,
    token: &Secret,
) -> Result<Vec<AlertInstance>, ReadError> {
    let url = guarded_url(base, ALERTS_PATH, &[])?;
    read_answer(http, url, token, parse_alerts).await
}

/// Starts the telemetry read when the Grafana origin and the token are set. The first read runs at once. A missed
/// tick is skipped, not replayed.
pub fn spawn_refresh(state: SharedState) {
    if state.config.grafana_token.is_none() || state.config.grafana_url.is_none() {
        return;
    }
    tokio::spawn(async move {
        let mut ticker = tokio::time::interval(REFRESH_INTERVAL);
        ticker.set_missed_tick_behavior(MissedTickBehavior::Delay);
        loop {
            ticker.tick().await;
            refresh(&state).await;
        }
    });
}

/// Reads Grafana once and stores the result. Does nothing unless the origin and the token are both set. Both reads
/// run each time. The lock is held only to store, never across the reads.
pub async fn refresh(app: &AppState) {
    let (Some(base), Some(token)) = (
        app.config.grafana_url.as_deref(),
        app.config.grafana_token.as_ref(),
    ) else {
        return;
    };
    let (targets, alerts) = tokio::join!(
        read_targets(&app.http, base, token),
        read_alerts(&app.http, base, token)
    );
    let mut cache = app.telemetry.write().await;
    match targets {
        Ok(targets) => {
            let (alerts, alerts_failure) = match alerts {
                Ok(instances) => (instances, None),
                Err(err) => {
                    let text = alerts_reason(&err);
                    let previous = cache
                        .good
                        .as_ref()
                        .and_then(|good| good.alerts_failure.as_deref());
                    // One line per distinct failure, not one per read while it lasts.
                    if previous != Some(text.as_str()) {
                        eprintln!("console: Grafana alerts read failed: {text}");
                    }
                    (Vec::new(), Some(text))
                }
            };
            cache.record_ok(
                rollup(&targets, &alerts, base),
                alerts_failure,
                rfc3339_now(),
            );
        }
        Err(err) => {
            // The alerts outcome of a refresh whose target read failed is not shown: the rollup it would sit
            // beside is the last good one, so the alerts block keeps that rollup's outcome.
            let text = up_reason(&err);
            if cache.failure.as_deref() != Some(text.as_str()) {
                eprintln!("console: Grafana telemetry read failed: {text}");
            }
            cache.record_error(text);
        }
    }
}

#[cfg(test)]
mod tests {

    /// The Telemetry page parses exactly what this route serves, including a failed alerts read, so a renamed key or
    /// state word on either side fails here rather than reading as a default.
    #[test]
    fn the_telemetry_page_reads_every_field_the_route_serves() {
        use crate::ui::telemetry_api as page;
        let counts = Counts {
            targets_up: 1,
            targets_total: 2,
            firing: 3,
            pending: 4,
        };
        let mut cache = TelemetryCache::new(true);
        cache.record_ok(
            vec![Environment {
                name: "prod".into(),
                counts,
                clusters: vec![Cluster {
                    name: "agent-cloud-prod".into(),
                    counts,
                    services: vec![Service {
                        name: "o11y/grafana".into(),
                        counts,
                        grafana_url: Some("https://grafana.test/d/service-overview".into()),
                    }],
                }],
            }],
            Some("Grafana answered HTTP 403".into()),
            "2026-11-08T00:00:00Z".into(),
        );
        let wire = serde_json::to_string(&cache.body()).expect("serialize");
        let read: page::TelemetryBody =
            serde_json::from_str(&wire).expect("the page parses the route");
        assert_eq!(read.state, page::TelemetryState::Ok);
        assert_eq!(read.alerts.state, page::AlertsState::Error);
        assert!(read.alerts.error.is_some());
        let env = &read.environments[0];
        assert_eq!(env.name, "prod");
        let service = &env.clusters[0].services[0];
        assert_eq!(service.name, "o11y/grafana");
        assert_eq!(
            (
                service.counts.targets_up,
                service.counts.targets_total,
                service.counts.firing,
                service.counts.pending
            ),
            (1, 2, 3, 4)
        );
        assert!(service.grafana_url.is_some());
        assert_eq!(read.refreshed_at.as_deref(), Some("2026-11-08T00:00:00Z"));
    }

    use super::*;
    use std::sync::{Arc, Mutex};

    use dioxus::server::axum::extract::{Query, State as AxumState};
    use dioxus::server::axum::http::{header, HeaderMap, StatusCode};
    use dioxus::server::axum::routing::get;
    use dioxus::server::axum::Router;
    use serde_json::Value;

    use crate::server::auth;
    use crate::server::config::Config;
    use crate::server::inventory::{FlowSet, InventoryCache};
    use crate::server::prefs::Prefs;
    use crate::server::scope::Scope;
    use crate::server::{api_router, client_with_roots, session_layer};

    const BASE: &str = "https://grafana.example.test";
    const TOKEN: &str = "grafana-viewer-token-value";
    const SCOPE: &str = r#"{"example": true, "tenants": []}"#;

    /// The `up` answer the fake Grafana gives: one target of the contract's example service, answering.
    const UP_OK: &str = r#"{"status":"success","data":{"resultType":"vector","result":[
        {"metric":{"environment":"prod","cluster":"agent-cloud-prod","service":"o11y/grafana"},"value":[1700000000,"1"]}
    ]}}"#;

    /// The alerts answer: the same service has one instance firing.
    const ALERTS_FIRING: &str = r#"{"status":"success","data":{"alerts":[
        {"labels":{"alertname":"GrafanaDown","environment":"prod","cluster":"agent-cloud-prod","service":"o11y/grafana"},"state":"Alerting","activeAt":"2026-11-08T04:00:00Z","value":"1"}
    ]}}"#;

    fn labels(pairs: &[(&str, &str)]) -> HashMap<String, String> {
        pairs
            .iter()
            .map(|(key, value)| (key.to_string(), value.to_string()))
            .collect()
    }

    fn target(pairs: &[(&str, &str)], up: bool) -> UpTarget {
        UpTarget {
            labels: labels(pairs),
            up,
        }
    }

    fn alert(pairs: &[(&str, &str)], state: &str) -> AlertInstance {
        AlertInstance {
            labels: labels(pairs),
            state: state.to_string(),
        }
    }

    #[test]
    fn targets_group_by_three_labels_and_a_missing_or_empty_one_is_none() {
        let targets = [
            target(
                &[
                    ("environment", "prod"),
                    ("cluster", "agent-cloud-prod"),
                    ("service", "o11y/grafana"),
                ],
                true,
            ),
            target(
                &[
                    ("environment", "prod"),
                    ("cluster", "agent-cloud-prod"),
                    ("service", "o11y/grafana"),
                ],
                false,
            ),
            target(
                &[
                    ("environment", "prod"),
                    ("cluster", "agent-cloud-prod"),
                    ("service", ""),
                ],
                true,
            ),
            target(&[("cluster", "dgx-spark"), ("service", "vllm")], true),
        ];
        let envs = rollup(&targets, &[], BASE);

        // `(none)` is the absent environment, and it sorts first.
        let names: Vec<&str> = envs.iter().map(|env| env.name.as_str()).collect();
        assert_eq!(names, ["(none)", "prod"]);

        let no_environment = &envs[0];
        assert_eq!(no_environment.clusters[0].name, "dgx-spark");
        let vllm = &no_environment.clusters[0].services[0];
        assert_eq!(vllm.name, "vllm");
        assert_eq!(
            vllm.grafana_url,
            grafana::service_overview(BASE, "vllm"),
            "a named service gets its link"
        );

        let prod = &envs[1];
        let services = &prod.clusters[0].services;
        let names: Vec<&str> = services.iter().map(|svc| svc.name.as_str()).collect();
        assert_eq!(names, ["(none)", "o11y/grafana"]);
        assert_eq!(services[0].grafana_url, None, "(none) has no link");
        assert_eq!(services[1].counts.targets_total, 2);
        assert_eq!(services[1].counts.targets_up, 1);
    }

    #[test]
    fn up_counts_every_series_and_only_a_sample_of_one_as_up() {
        let body = r#"{"status":"success","data":{"resultType":"vector","result":[
            {"metric":{"environment":"prod","cluster":"c","service":"a"},"value":[1,"1"]},
            {"metric":{"environment":"prod","cluster":"c","service":"b"},"value":[1,"0"]},
            {"metric":{"environment":"prod","cluster":"c","service":"d"},"value":[1,"NaN"]},
            {"metric":{"environment":"prod","cluster":"c","service":"e"},"value":[1,"1.0"]}
        ]}}"#;
        let targets = parse_up(body.as_bytes()).expect("parses");
        let envs = rollup(&targets, &[], BASE);
        assert_eq!(envs[0].counts.targets_total, 4);
        assert_eq!(
            envs[0].counts.targets_up, 2,
            "1 and 1.0 answer; 0 and NaN do not"
        );
    }

    #[test]
    fn alerting_and_pending_count_by_prefix_and_no_other_state_counts() {
        let group = [("environment", "prod"), ("cluster", "c"), ("service", "a")];
        let alerts = [
            "Alerting",
            "Alerting (NoData)",
            "Alerting (Error)",
            "Pending",
            "Pending (NoData)",
            "Normal",
            "Normal (NoData, KeepLast)",
            "NoData",
            "Error",
        ]
        .map(|state| alert(&group, state));
        let envs = rollup(&[target(&group, true)], &alerts, BASE);
        assert_eq!(envs[0].counts.firing, 3, "Alerting and its suffixes");
        assert_eq!(
            envs[0].counts.pending, 2,
            "Pending and its suffix, nothing else"
        );
    }

    #[test]
    fn an_alert_only_group_gets_a_row_with_no_targets() {
        let envs = rollup(
            &[],
            &[alert(
                &[
                    ("environment", "staging"),
                    ("cluster", "x"),
                    ("service", "y"),
                ],
                "Alerting",
            )],
            BASE,
        );
        assert_eq!(envs.len(), 1);
        assert_eq!(envs[0].name, "staging");
        assert_eq!(
            envs[0].counts,
            Counts {
                targets_up: 0,
                targets_total: 0,
                firing: 1,
                pending: 0
            }
        );
    }

    #[test]
    fn levels_sort_by_name_and_each_total_is_the_sum_of_its_children() {
        let targets = [
            target(
                &[
                    ("environment", "staging"),
                    ("cluster", "c2"),
                    ("service", "s1"),
                ],
                true,
            ),
            target(
                &[
                    ("environment", "prod"),
                    ("cluster", "c2"),
                    ("service", "s9"),
                ],
                true,
            ),
            target(
                &[
                    ("environment", "prod"),
                    ("cluster", "c1"),
                    ("service", "s2"),
                ],
                true,
            ),
            target(
                &[
                    ("environment", "prod"),
                    ("cluster", "c1"),
                    ("service", "s1"),
                ],
                false,
            ),
            target(
                &[
                    ("environment", "prod"),
                    ("cluster", "c1"),
                    ("service", "s1"),
                ],
                true,
            ),
        ];
        let alerts = [alert(
            &[
                ("environment", "prod"),
                ("cluster", "c1"),
                ("service", "s2"),
            ],
            "Alerting",
        )];
        let envs = rollup(&targets, &alerts, BASE);

        let env_names: Vec<&str> = envs.iter().map(|env| env.name.as_str()).collect();
        assert_eq!(env_names, ["prod", "staging"]);
        let prod = &envs[0];
        let cluster_names: Vec<&str> = prod.clusters.iter().map(|c| c.name.as_str()).collect();
        assert_eq!(cluster_names, ["c1", "c2"]);
        let c1 = &prod.clusters[0];
        let service_names: Vec<&str> = c1.services.iter().map(|s| s.name.as_str()).collect();
        assert_eq!(service_names, ["s1", "s2"]);

        assert_eq!(c1.counts.targets_total, 3);
        assert_eq!(c1.counts.targets_up, 2);
        assert_eq!(c1.counts.firing, 1);
        assert_eq!(
            prod.counts.targets_total, 4,
            "the environment sums its clusters"
        );
        assert_eq!(prod.counts.targets_up, 3);
        assert_eq!(prod.counts.firing, 1);
        assert_eq!(envs[1].counts.targets_total, 1);
    }

    #[test]
    fn a_service_link_encodes_its_name_and_the_contract_shape_holds() {
        let targets = [target(
            &[
                ("environment", "prod"),
                ("cluster", "agent-cloud-prod"),
                ("service", "o11y/grafana"),
            ],
            true,
        )];
        let mut cache = TelemetryCache::new(true);
        cache.record_ok(
            rollup(&targets, &[], BASE),
            None,
            "2026-11-08T05:00:00Z".into(),
        );
        let body = serde_json::to_value(cache.body()).expect("json");
        let expected = serde_json::json!({
            "state": "ok",
            "refreshed_at": "2026-11-08T05:00:00Z",
            "error": null,
            "alerts": {"state": "ok", "error": null},
            "environments": [{
                "name": "prod",
                "targets_up": 1,
                "targets_total": 1,
                "firing": 0,
                "pending": 0,
                "clusters": [{
                    "name": "agent-cloud-prod",
                    "targets_up": 1,
                    "targets_total": 1,
                    "firing": 0,
                    "pending": 0,
                    "services": [{
                        "name": "o11y/grafana",
                        "targets_up": 1,
                        "targets_total": 1,
                        "firing": 0,
                        "pending": 0,
                        "grafana_url": "https://grafana.example.test/d/service-overview/service-overview?var-service=o11y%2Fgrafana&var-log_service=o11y%2Fgrafana"
                    }]
                }]
            }]
        });
        assert_eq!(body, expected);
    }

    #[test]
    fn an_alerts_answer_parses_its_states_and_tolerates_extra_keys_and_no_list() {
        let instances = parse_alerts(ALERTS_FIRING.as_bytes()).expect("parses");
        assert_eq!(instances.len(), 1);
        assert_eq!(instances[0].state, "Alerting");
        assert_eq!(
            instances[0].labels.get("service").map(String::as_str),
            Some("o11y/grafana")
        );

        for text in [
            r#"{"status":"success","data":{"alerts":null}}"#,
            r#"{"status":"success","data":{}}"#,
            r#"{"status":"success","data":null}"#,
        ] {
            assert!(
                parse_alerts(text.as_bytes()).expect("parses").is_empty(),
                "{text}"
            );
        }
        assert_eq!(
            parse_alerts(br#"{"status":"error","data":null}"#).err(),
            Some(ReadError::Shape)
        );
        assert_eq!(parse_alerts(b"<html>").err(), Some(ReadError::Decode));
    }

    #[test]
    fn a_target_answer_that_is_not_a_vector_is_refused() {
        let matrix = r#"{"status":"success","data":{"resultType":"matrix","result":[]}}"#;
        assert_eq!(parse_up(matrix.as_bytes()).err(), Some(ReadError::Shape));
    }

    #[test]
    fn the_body_states_follow_the_configuration_and_the_reads() {
        let unconfigured = TelemetryCache::new(false).body();
        assert_eq!(unconfigured.state, "unconfigured");

        let loading = TelemetryCache::new(true).body();
        assert_eq!(
            loading,
            TelemetryBody {
                state: "loading",
                refreshed_at: None,
                error: None,
                alerts: AlertsBlock {
                    state: "ok",
                    error: None
                },
                environments: Vec::new(),
            }
        );

        let mut cache = TelemetryCache::new(true);
        cache.record_error("Grafana answered HTTP 500".into());
        let failed = cache.body();
        assert_eq!(failed.state, "error");
        assert_eq!(failed.refreshed_at, None);
        assert_eq!(failed.environments, Vec::new());
        assert_eq!(
            failed.error.as_deref(),
            Some("The telemetry read failed: Grafana answered HTTP 500.")
        );
        assert_eq!(
            failed.alerts,
            AlertsBlock {
                state: "ok",
                error: None
            },
            "no rollup, so no alerts failure to show"
        );
    }

    #[test]
    fn a_failed_read_after_a_good_one_keeps_the_rollup_and_names_the_failure() {
        let mut cache = TelemetryCache::new(true);
        cache.record_ok(Vec::new(), None, "t1".into());
        cache.record_error("Grafana answered HTTP 500".into());
        let body = cache.body();
        assert_eq!(body.state, "ok");
        assert_eq!(body.refreshed_at.as_deref(), Some("t1"));
        assert_eq!(
            body.error.as_deref(),
            Some("The newest telemetry read failed: Grafana answered HTTP 500. Showing the last good read.")
        );
    }

    #[test]
    fn the_alerts_block_names_its_failure_and_the_targets_keep_their_rollup() {
        let mut cache = TelemetryCache::new(true);
        cache.record_ok(
            Vec::new(),
            Some("Grafana answered HTTP 403".into()),
            "t1".into(),
        );
        let body = cache.body();
        assert_eq!(body.error, None, "the target read is fine");
        assert_eq!(
            body.alerts,
            AlertsBlock {
                state: "error",
                error: Some("Grafana alerts could not be read: Grafana answered HTTP 403.".into())
            }
        );
    }

    #[test]
    fn the_refusals_and_shapes_name_the_telemetry_reads_and_the_usage_text_stays_for_the_rest() {
        assert_eq!(
            alerts_reason(&ReadError::Status(403)),
            "Grafana answered HTTP 403"
        );
        assert_eq!(
            up_reason(&ReadError::Shape),
            "Grafana's answer is not a success vector of target series"
        );
        assert_eq!(
            alerts_reason(&ReadError::Shape),
            "Grafana's alerts answer did not report success"
        );
        assert!(
            up_reason(&ReadError::Status(403)).contains("Prometheus data source"),
            "the target read queries the Prometheus proxy, so its 403 text holds"
        );
    }

    #[test]
    fn the_refresh_runs_every_five_minutes() {
        assert_eq!(REFRESH_INTERVAL, Duration::from_secs(300));
    }

    #[test]
    fn both_read_urls_carry_the_guard_and_no_stray_question_mark() {
        let targets = guarded_url(BASE, USAGE_PATH, &[("query", UP_QUERY)]).expect("url");
        assert_eq!(targets.query(), Some("query=up"));

        let alerts = guarded_url(BASE, ALERTS_PATH, &[]).expect("url");
        assert_eq!(
            alerts.as_str(),
            "https://grafana.example.test/api/prometheus/grafana/api/v1/alerts"
        );

        for base in [
            "https://grafana.example.test?x=1",
            "https://grafana.example.test#frag",
            "http://grafana.example.test",
        ] {
            assert_eq!(
                guarded_url(base, ALERTS_PATH, &[]).err(),
                Some(ReadError::Refused),
                "{base}"
            );
        }
    }

    /// A fake Grafana: one reply per read, and a record of each request's path, bearer and `query` parameter.
    #[derive(Clone)]
    struct Reply {
        status: u16,
        body: String,
    }

    impl Reply {
        fn ok(body: &str) -> Self {
            Self {
                status: 200,
                body: body.to_string(),
            }
        }

        fn fail(status: u16, body: &str) -> Self {
            Self {
                status,
                body: body.to_string(),
            }
        }
    }

    #[derive(Clone)]
    struct Seen {
        path: String,
        bearer: Option<String>,
        query: Option<String>,
    }

    struct Fake {
        up: Mutex<Reply>,
        alerts: Mutex<Reply>,
        seen: Mutex<Vec<Seen>>,
    }

    impl Fake {
        fn new(up: Reply, alerts: Reply) -> Arc<Self> {
            Arc::new(Self {
                up: Mutex::new(up),
                alerts: Mutex::new(alerts),
                seen: Mutex::new(Vec::new()),
            })
        }
    }

    fn record(fake: &Fake, path: &str, headers: &HeaderMap, query: Option<String>) {
        let bearer = headers
            .get(header::AUTHORIZATION)
            .and_then(|value| value.to_str().ok())
            .map(str::to_string);
        fake.seen.lock().expect("seen").push(Seen {
            path: path.to_string(),
            bearer,
            query,
        });
    }

    fn answer(slot: &Mutex<Reply>) -> (StatusCode, String) {
        let reply = slot.lock().expect("reply").clone();
        (
            StatusCode::from_u16(reply.status).expect("status"),
            reply.body,
        )
    }

    async fn up_handler(
        AxumState(fake): AxumState<Arc<Fake>>,
        headers: HeaderMap,
        Query(query): Query<HashMap<String, String>>,
    ) -> (StatusCode, String) {
        record(&fake, USAGE_PATH, &headers, query.get("query").cloned());
        answer(&fake.up)
    }

    async fn alerts_handler(
        AxumState(fake): AxumState<Arc<Fake>>,
        headers: HeaderMap,
    ) -> (StatusCode, String) {
        record(&fake, ALERTS_PATH, &headers, None);
        answer(&fake.alerts)
    }

    async fn serve_app(app: Router) -> std::net::SocketAddr {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind");
        let addr = listener.local_addr().expect("addr");
        tokio::spawn(async move {
            dioxus::server::axum::serve(listener, app)
                .await
                .expect("serve");
        });
        addr
    }

    /// Serves the fake Grafana and returns its origin. Loopback cleartext passes the transport rule.
    async fn serve_fake(fake: Arc<Fake>) -> String {
        let app = Router::new()
            .route(USAGE_PATH, get(up_handler))
            .route(ALERTS_PATH, get(alerts_handler))
            .with_state(fake);
        format!("http://{}", serve_app(app).await)
    }

    /// The client the console really uses: `client_with_roots`, with its no-redirect policy and timeout.
    fn production_client() -> reqwest::Client {
        client_with_roots(None, "CONSOLE_EXTRA_CA_FILE").expect("the production client")
    }

    /// The application state. `origin` sets the Grafana origin and token, so the cache is configured exactly when
    /// a read can run.
    fn state(origin: Option<&str>) -> SharedState {
        Arc::new(AppState {
            config: Config {
                base_url: "https://console.test".into(),
                oidc_issuer: "https://auth.test/application/o/console/".into(),
                oidc_client_id: "console".into(),
                oidc_client_secret: "test-secret".into(),
                extra_ca_file: None,
                data_dir: std::env::temp_dir(),
                scope_file: std::env::temp_dir(),
                semaphore: None,
                workflow_file: None,
                netbox: None,
                proxmox: None,
                openbao: None,
                flows_file: None,
                gateway_file: None,
                catalog_file: None,
                grafana_url: origin.map(str::to_string),
                grafana_token: origin.map(|_| Secret::new(TOKEN)),
                authentik: None,
                role_map_file: None,
                github: None,
            },
            http: production_client(),
            oidc: tokio::sync::OnceCell::new(),
            scope: Scope::parse(SCOPE).expect("scope"),
            prefs: Prefs::in_memory().expect("prefs"),
            work: Arc::new(tokio::sync::RwLock::new(work::WorkCache::unconfigured())),
            workflow: Vec::new(),
            inventory: tokio::sync::RwLock::new(InventoryCache::new(false, false)),
            proxmox_http: reqwest::Client::new(),
            flows: FlowSet::unconfigured(),
            gateway: crate::server::gateway::GatewaySet::unconfigured(),
            catalog: crate::server::catalog::CatalogSet::unconfigured(),
            usage: tokio::sync::RwLock::new(crate::server::grafana::UsageCache::new(false)),
            openbao: None,
            secrets: tokio::sync::RwLock::new(crate::server::secrets::SecretsCache::new(false)),
            access: tokio::sync::RwLock::new(crate::server::access::AccessCache::new(false)),
            role_map: crate::server::access::RoleMap::empty(),
            policies: tokio::sync::RwLock::new(crate::server::policies::PoliciesCache::new(false)),
            delivery: tokio::sync::RwLock::new(crate::server::delivery::DeliveryCache::new(false)),
            telemetry: tokio::sync::RwLock::new(TelemetryCache::new(origin.is_some())),
        })
    }

    /// One refresh, then the body the route would serve.
    async fn read_once(app: &SharedState) -> Value {
        refresh(app).await;
        serde_json::to_value(app.telemetry.read().await.body()).expect("json")
    }

    #[tokio::test]
    async fn a_refresh_serves_the_rollup_and_both_reads_send_the_viewer_token() {
        let fake = Fake::new(Reply::ok(UP_OK), Reply::ok(ALERTS_FIRING));
        let origin = serve_fake(fake.clone()).await;
        let app = state(Some(&origin));
        let body = read_once(&app).await;

        assert_eq!(body["state"], "ok");
        assert_eq!(body["error"], Value::Null);
        assert_eq!(
            body["alerts"],
            serde_json::json!({"state": "ok", "error": null})
        );
        assert_eq!(body["environments"][0]["name"], "prod");
        assert_eq!(body["environments"][0]["firing"], 1);
        assert_eq!(
            body["environments"][0]["clusters"][0]["services"][0]["targets_up"],
            1
        );

        let bearer = format!("Bearer {TOKEN}");
        let seen = fake.seen.lock().expect("seen").clone();
        assert_eq!(seen.len(), 2, "one target read and one alerts read");
        assert!(seen
            .iter()
            .all(|read| read.bearer.as_deref() == Some(bearer.as_str())));
        assert!(seen
            .iter()
            .any(|read| read.path == USAGE_PATH && read.query.as_deref() == Some("up")));
        assert!(seen.iter().any(|read| read.path == ALERTS_PATH));
        assert!(!body.to_string().contains(TOKEN));
    }

    #[tokio::test]
    async fn an_alerts_403_keeps_the_rollup_and_marks_only_the_alerts_block() {
        let refusal = format!("Grafana rejected the bearer {TOKEN}");
        let fake = Fake::new(Reply::ok(UP_OK), Reply::fail(403, &refusal));
        let origin = serve_fake(fake).await;
        let app = state(Some(&origin));
        let body = read_once(&app).await;

        assert_eq!(body["state"], "ok");
        assert_eq!(body["error"], Value::Null, "the target read is fine");
        assert_eq!(
            body["alerts"],
            serde_json::json!({
                "state": "error",
                "error": "Grafana alerts could not be read: Grafana answered HTTP 403."
            })
        );
        assert_eq!(body["environments"][0]["targets_total"], 1);
        assert_eq!(
            body["environments"][0]["firing"], 0,
            "no alert counts from a failed read"
        );
        assert!(!body.to_string().contains(TOKEN));
    }

    #[tokio::test]
    async fn an_up_500_after_a_good_read_keeps_the_last_good_rollup_and_says_so() {
        let fake = Fake::new(Reply::ok(UP_OK), Reply::ok(ALERTS_FIRING));
        let origin = serve_fake(fake.clone()).await;
        let app = state(Some(&origin));
        let first = read_once(&app).await;
        assert_eq!(first["environments"][0]["firing"], 1);

        *fake.up.lock().expect("up") = Reply::fail(500, "upstream detail");
        let second = read_once(&app).await;

        assert_eq!(second["state"], "ok");
        assert_eq!(
            second["refreshed_at"], first["refreshed_at"],
            "the stamp is the older read's"
        );
        assert_eq!(second["environments"], first["environments"]);
        assert_eq!(second["alerts"], first["alerts"]);
        assert_eq!(
            second["error"],
            "The newest telemetry read failed: Grafana answered HTTP 500. Showing the last good read."
        );
        assert!(!second.to_string().contains("upstream detail"));
    }

    #[tokio::test]
    async fn with_no_good_read_and_both_reads_failing_the_body_is_an_error_with_alerts_ok() {
        let fake = Fake::new(Reply::fail(500, "boom"), Reply::fail(403, "denied"));
        let origin = serve_fake(fake).await;
        let app = state(Some(&origin));
        let body = read_once(&app).await;

        assert_eq!(body["state"], "error");
        assert_eq!(body["refreshed_at"], Value::Null);
        assert_eq!(body["environments"], serde_json::json!([]));
        assert_eq!(
            body["error"],
            "The telemetry read failed: Grafana answered HTTP 500."
        );
        assert_eq!(
            body["alerts"],
            serde_json::json!({"state": "ok", "error": null})
        );
    }

    #[tokio::test]
    async fn a_cleartext_public_origin_is_refused_before_any_request_and_names_no_token() {
        let app = state(Some("http://grafana.example.test"));
        let body = read_once(&app).await;

        assert_eq!(body["state"], "error");
        assert_eq!(
            body["error"],
            "The telemetry read failed: the Grafana origin is not one the console may send its token to."
        );
        assert!(!body.to_string().contains(TOKEN));
    }

    #[tokio::test]
    async fn an_unconfigured_cache_runs_no_read() {
        let app = state(None);
        refresh(&app).await;
        let body = serde_json::to_value(app.telemetry.read().await.body()).expect("json");
        assert_eq!(body["state"], "unconfigured");
        assert_eq!(body["environments"], serde_json::json!([]));
    }

    /// The login route: signs a user in with `groups`.
    fn login_route(groups: &'static [&'static str]) -> Router {
        Router::new().route(
            "/test/login",
            get(move |session: Session| async move {
                let user = auth::CurrentUser {
                    sub: "user-1".into(),
                    email: None,
                    name: None,
                    username: None,
                    groups: groups.iter().map(|group| group.to_string()).collect(),
                };
                session
                    .insert(auth::KEY_USER, &user)
                    .await
                    .expect("insert user");
                "signed in"
            }),
        )
    }

    /// Serves the API over `state` and returns its address and a session cookie for a user in `groups`.
    async fn app_for(
        state: SharedState,
        groups: &'static [&'static str],
    ) -> (std::net::SocketAddr, String) {
        let sessions = session_layer(Prefs::in_memory().expect("sessions").session_store());
        let app = api_router(state).merge(login_route(groups)).layer(sessions);
        let addr = serve_app(app).await;
        let reply = reqwest::get(format!("http://{addr}/test/login"))
            .await
            .expect("login");
        let cookie = reply
            .headers()
            .get("set-cookie")
            .expect("session cookie")
            .to_str()
            .expect("ascii cookie")
            .split(';')
            .next()
            .expect("cookie pair")
            .to_string();
        (addr, cookie)
    }

    /// One GET, with `cookie` when given. Returns the status and the body text.
    async fn fetch(addr: std::net::SocketAddr, path: &str, cookie: Option<&str>) -> (u16, String) {
        let client = reqwest::Client::new();
        let mut request = client.get(format!("http://{addr}{path}"));
        if let Some(cookie) = cookie {
            request = request.header("cookie", cookie);
        }
        let reply = request.send().await.expect("send");
        let status = reply.status().as_u16();
        (status, reply.text().await.expect("body"))
    }

    #[tokio::test]
    async fn members_get_403_signed_out_gets_401_and_platform_admins_get_the_rollup() {
        let fake = Fake::new(Reply::ok(UP_OK), Reply::ok(ALERTS_FIRING));
        let origin = serve_fake(fake).await;
        let app = state(Some(&origin));
        refresh(&app).await;

        for groups in [&["ops"][..], &["platform-admins-old", "users"][..]] {
            let (addr, cookie) = app_for(app.clone(), groups).await;
            let (status, body) = fetch(addr, "/v1/telemetry/rollups", Some(&cookie)).await;
            assert_eq!(status, 403, "{groups:?}");
            assert_eq!(body, r#"{"error":"forbidden"}"#, "{groups:?}");
        }

        let (addr, _) = app_for(app.clone(), &["platform-admins"]).await;
        let (status, body) = fetch(addr, "/v1/telemetry/rollups", None).await;
        assert_eq!(status, 401);
        assert_eq!(body, r#"{"error":"unauthenticated"}"#);

        let (addr, cookie) = app_for(app.clone(), &["platform-admins"]).await;
        let (status, body) = fetch(addr, "/v1/telemetry/rollups", Some(&cookie)).await;
        assert_eq!(status, 200);
        let value: Value = serde_json::from_str(&body).expect("json");
        assert_eq!(value["state"], "ok");
        assert_eq!(value["environments"][0]["name"], "prod");
        assert!(!body.contains(TOKEN), "{body}");
    }
}
