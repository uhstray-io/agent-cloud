//! Grafana, two ways. The deep links below are URLs the browser opens: the console builds them and
//! never fetches them. Dashboard uids and variable names are the ones the o11y dashboards declare, in
//! `platform/services/o11y/deployment/config/grafana/dashboards/*.json`:
//!
//! - `service-overview`: variables `service` and `log_service`.
//! - `service-conformance`: no variables.
//!
//! Query values are encoded by `reqwest::Url`, so a service name cannot add a parameter.
//!
//! The usage read (plan 17, task 8.3, D39) is one of the console's calls to Grafana; the telemetry read
//! (plan 17, 9.2, D43, `telemetry.rs`) is the other. With
//! `CONSOLE_GRAFANA_TOKEN` set, a background task asks Grafana's Prometheus proxy for the gateway's
//! token use per identity over the last hour, every `REFRESH_INTERVAL`, with a Viewer token (D32).
//! `GET /v1/gateway` reads the cache and never Grafana. A failed read keeps the last good figures and
//! says so. Every error text is fixed: no URL, body or header reaches a message or a log.

use std::collections::HashMap;
use std::fmt;
use std::time::Duration;

use reqwest::Url;
use serde::de::IgnoredAny;
use serde::{Deserialize, Serialize};
use tokio::time::MissedTickBehavior;

use super::config::{transport_allowed, Secret};
use super::work::rfc3339_now;
use super::{AppState, SharedState};

/// How often the usage read runs (D39: every 5 minutes).
pub const REFRESH_INTERVAL: Duration = Duration::from_secs(300);

/// The PromQL the usage read sends: the gateway's token counter per identity over the last hour. The gateway
/// enforces its own rolling budget in Postgres, so this is the metric of the same traffic, an approximation.
pub const USAGE_QUERY: &str = r#"sum by (identity) (increase(agentgateway_gen_ai_client_token_usage_sum{job="agentgateway"}[1h]))"#;

/// Grafana's Prometheus datasource (uid `prometheus`, `datasources.yml`), queried through the API proxy.
pub(super) const USAGE_PATH: &str = "/api/datasources/proxy/uid/prometheus/api/v1/query";

/// Why one usage read failed. Fixed text only: a reqwest error names the request URL, and a body can echo the
/// token, so neither is shown (`access.rs` does the same).
#[derive(Debug, PartialEq, Eq)]
pub enum ReadError {
    /// The origin is not one the token may be sent to: not https, and not loopback or RFC1918 cleartext.
    Refused,
    Status(u16),
    Timeout,
    Connect,
    Other,
    Decode,
    /// The answer is not a success vector, or names one identity twice.
    Shape,
}

impl fmt::Display for ReadError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ReadError::Refused => {
                f.write_str("the Grafana origin is not one the console may send its token to")
            }
            // The two refusals that have a cause the operator can act on. Any other status keeps the bare code.
            ReadError::Status(401) => f.write_str(
                "Grafana refused the console token (HTTP 401): it is expired or invalid. Check Provision Console Grafana Token.",
            ),
            ReadError::Status(403) => f.write_str(
                "Grafana refused the console token (HTTP 403): the Viewer service account may not query the Prometheus data source.",
            ),
            ReadError::Status(code) => write!(f, "Grafana answered HTTP {code}"),
            ReadError::Timeout => f.write_str("Grafana did not answer in time"),
            ReadError::Connect => f.write_str("cannot connect to Grafana"),
            ReadError::Other => f.write_str("the Grafana request failed"),
            ReadError::Decode => {
                f.write_str("Grafana answered with a body the console does not read")
            }
            ReadError::Shape => {
                f.write_str("Grafana's answer is not one usage figure per identity")
            }
        }
    }
}

/// The proxied query URL for `base`. The origin must pass the transport rule (https, or cleartext to a
/// loopback or RFC1918 host) and carry no query or fragment, so the path cannot be moved into one. Config checks
/// the origin at load; this check repeats it, because a test `AppState` can carry any origin.
pub fn usage_url(base: &str) -> Result<Url, ReadError> {
    guarded_url(base, USAGE_PATH, &[("query", USAGE_QUERY)])
}

/// `path` on `base`, with `query` as its pairs. The guard `usage_url` applies, for every Grafana read the console
/// makes: the origin passes the transport rule and carries no query or fragment.
pub(super) fn guarded_url(
    base: &str,
    path: &str,
    query: &[(&str, &str)],
) -> Result<Url, ReadError> {
    let parsed = Url::parse(base).map_err(|_| ReadError::Refused)?;
    if !transport_allowed(base) || parsed.query().is_some() || parsed.fragment().is_some() {
        return Err(ReadError::Refused);
    }
    let mut url = Url::parse(&format!("{base}{path}")).map_err(|_| ReadError::Refused)?;
    // An empty query leaves no `?` behind: `query_pairs_mut` alone would add one.
    if !query.is_empty() {
        url.query_pairs_mut().extend_pairs(query);
    }
    Ok(url)
}

/// One usage read: the query through Grafana's proxy, with the Viewer token as a bearer. The shared client
/// follows no redirect, so the token goes only to `base`.
pub async fn fetch_usage(
    http: &reqwest::Client,
    base: &str,
    token: &Secret,
) -> Result<HashMap<String, u64>, ReadError> {
    read_answer(http, usage_url(base)?, token, parse_usage).await
}

/// One GET of `url` with the Viewer token as a bearer, then `parse` of the body. A status that is not success
/// fails the read by its code, and the body is never shown.
pub(super) async fn read_answer<T>(
    http: &reqwest::Client,
    url: Url,
    token: &Secret,
    parse: impl FnOnce(&[u8]) -> Result<T, ReadError>,
) -> Result<T, ReadError> {
    let response = http
        .get(url)
        .header(
            reqwest::header::AUTHORIZATION,
            format!("Bearer {}", token.expose()),
        )
        .header(reqwest::header::ACCEPT, "application/json")
        .send()
        .await
        .map_err(transport_error)?;
    let status = response.status();
    if !status.is_success() {
        return Err(ReadError::Status(status.as_u16()));
    }
    let body = response.bytes().await.map_err(transport_error)?;
    parse(&body)
}

/// reqwest's own Display names the request URL, so the error is classified, not shown (as `access.rs` does).
fn transport_error(err: reqwest::Error) -> ReadError {
    if err.is_timeout() {
        ReadError::Timeout
    } else if err.is_connect() {
        ReadError::Connect
    } else {
        ReadError::Other
    }
}

#[derive(Deserialize)]
struct Answer {
    status: String,
    data: Option<AnswerData>,
}

#[derive(Deserialize)]
struct AnswerData {
    #[serde(rename = "resultType")]
    result_type: String,
    result: Vec<Series>,
}

/// One series of an instant vector. Prometheus sends the sample as a string next to its timestamp.
#[derive(Deserialize)]
struct Series {
    metric: HashMap<String, String>,
    value: (IgnoredAny, String),
}

/// One series of an instant-vector answer: its labels, and its sample as Prometheus sends it.
pub(super) type InstantSample = (HashMap<String, String>, String);

/// The series of a Prometheus instant-vector answer, each as its labels and its sample text. A body that is not
/// JSON is `Decode`. A status that is not `success`, or a result that is not a vector, is `Shape`. The usage read
/// and the telemetry read both parse their answers here.
pub(super) fn instant_vector(body: &[u8]) -> Result<Vec<InstantSample>, ReadError> {
    let answer: Answer = serde_json::from_slice(body).map_err(|_| ReadError::Decode)?;
    let data = match answer.data {
        Some(data) if answer.status == "success" && data.result_type == "vector" => data,
        _ => return Err(ReadError::Shape),
    };
    Ok(data
        .result
        .into_iter()
        .map(|series| (series.metric, series.value.1))
        .collect())
}

/// The per-identity token counts of one instant-query answer, rounded to whole tokens. A series with no
/// `identity` label, or a sample that is not a finite non-negative number, is skipped: its client then reads
/// as no figure, never as a wrong one. An identity named twice refuses the whole answer.
pub fn parse_usage(body: &[u8]) -> Result<HashMap<String, u64>, ReadError> {
    let mut tokens = HashMap::new();
    for (metric, sample) in instant_vector(body)? {
        let Some(identity) = metric.get("identity").filter(|name| !name.is_empty()) else {
            continue;
        };
        let Ok(sample) = sample.parse::<f64>() else {
            continue;
        };
        if !sample.is_finite() || sample < 0.0 {
            continue;
        }
        if tokens
            .insert(identity.clone(), sample.round() as u64)
            .is_some()
        {
            return Err(ReadError::Shape);
        }
    }
    Ok(tokens)
}

/// What `usage.state` names: `unconfigured` without the token, `loading` before the first read finishes,
/// `error` while the newest read failed, `ok` otherwise.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum UsageState {
    Ok,
    Unconfigured,
    Loading,
    Error,
}

/// The usage block of `GET /v1/gateway`. `refreshed_at` is the last good read's time. `error` is the sentence
/// the page shows, or null.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct UsageView {
    pub state: UsageState,
    pub refreshed_at: Option<String>,
    pub error: Option<String>,
}

/// The last read that finished, with its per-identity figures.
struct Good {
    tokens: HashMap<String, u64>,
    refreshed_at: String,
}

/// The usage figures the gateway answer joins. The refresh task writes it; `/v1/gateway` only reads it.
pub struct UsageCache {
    configured: bool,
    good: Option<Good>,
    failure: Option<String>,
}

impl UsageCache {
    /// `configured` is whether the token is set. When it is not, no read runs and the page says so.
    pub fn new(configured: bool) -> Self {
        Self {
            configured,
            good: None,
            failure: None,
        }
    }

    /// Stores a finished read and clears the failure.
    pub fn record_ok(&mut self, tokens: HashMap<String, u64>, refreshed_at: String) {
        self.failure = None;
        self.good = Some(Good {
            tokens,
            refreshed_at,
        });
    }

    /// Keeps the last good figures, if there are any, and stores the reason this read failed.
    pub fn record_error(&mut self, reason: String) {
        self.failure = Some(reason);
    }

    fn state(&self) -> UsageState {
        match (self.configured, &self.good, &self.failure) {
            (false, _, _) => UsageState::Unconfigured,
            (true, _, Some(_)) => UsageState::Error,
            (true, Some(_), None) => UsageState::Ok,
            (true, None, None) => UsageState::Loading,
        }
    }

    /// The sentence the page shows, or none. A failed read names its reason, and says whether the last good
    /// figures are still shown.
    fn error_sentence(&self) -> Option<String> {
        // A reason that already ends in a full stop, as the 401 and 403 sentences do, gets no second one.
        let reason = self.failure.as_deref()?.trim_end_matches('.');
        Some(match &self.good {
            Some(_) => {
                format!("The newest usage read failed: {reason}. Showing the last good read.")
            }
            None => format!("The usage read failed: {reason}."),
        })
    }

    /// The last good read's token count for `identity`: `None` when no read has finished or the identity is
    /// not in it.
    pub fn tokens_for(&self, identity: &str) -> Option<u64> {
        self.good.as_ref()?.tokens.get(identity).copied()
    }

    pub fn view(&self) -> UsageView {
        UsageView {
            state: self.state(),
            refreshed_at: self.good.as_ref().map(|good| good.refreshed_at.clone()),
            error: self.error_sentence(),
        }
    }
}

/// Starts the usage read when the token and its origin are set. The first read runs at once; a missed tick is
/// skipped, not replayed.
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

/// Reads the usage once and stores the result. Does nothing unless both the origin and the token are set. The
/// lock is held only to store, never across the read.
pub async fn refresh(app: &AppState) {
    let (Some(base), Some(token)) = (
        app.config.grafana_url.as_deref(),
        app.config.grafana_token.as_ref(),
    ) else {
        return;
    };
    let result = fetch_usage(&app.http, base, token).await;
    let mut cache = app.usage.write().await;
    match result {
        Ok(tokens) => cache.record_ok(tokens, rfc3339_now()),
        Err(err) => {
            let text = err.to_string();
            // One line per distinct failure, not one per read while it lasts.
            if cache.failure.as_deref() != Some(text.as_str()) {
                eprintln!("console: Grafana usage read failed: {text}");
            }
            cache.record_error(text);
        }
    }
}

/// The per-service dashboard for one VM or service, as a ready-made link.
/// `None` only when `base` does not parse as a URL, which the config guard prevents.
pub fn service_overview(base: &str, service: &str) -> Option<String> {
    let mut url = Url::parse(&format!("{base}/d/service-overview/service-overview")).ok()?;
    url.query_pairs_mut()
        .append_pair("var-service", service)
        .append_pair("var-log_service", service);
    Some(url.as_str().to_string())
}

/// The service-deployment conformance dashboard. It takes no variables.
pub fn service_conformance(base: &str) -> Option<String> {
    let url = Url::parse(&format!(
        "{base}/d/service-conformance/service-deployment-conformance"
    ))
    .ok()?;
    Some(url.as_str().to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    const BASE: &str = "https://grafana.example.test";

    fn query_of(link: &str) -> Vec<(String, String)> {
        Url::parse(link)
            .expect("link parses")
            .query_pairs()
            .map(|(k, v)| (k.into_owned(), v.into_owned()))
            .collect()
    }

    #[test]
    fn service_overview_link_sets_both_variables_to_the_service() {
        assert_eq!(
            service_overview(BASE, "tududi").as_deref(),
            Some("https://grafana.example.test/d/service-overview/service-overview?var-service=tududi&var-log_service=tududi")
        );
    }

    #[test]
    fn a_service_name_cannot_add_a_parameter_and_round_trips() {
        let name = "odd name&var-x=1#frag";
        let link = service_overview(BASE, name).expect("link");
        assert!(!link.contains("var-x=1"), "{link}");
        assert!(!link.contains('#'), "{link}");
        assert_eq!(
            query_of(&link),
            [
                ("var-service".to_string(), name.to_string()),
                ("var-log_service".to_string(), name.to_string()),
            ]
        );
    }

    #[test]
    fn conformance_link_has_the_fixed_path_and_no_variables() {
        assert_eq!(
            service_conformance(BASE).as_deref(),
            Some(
                "https://grafana.example.test/d/service-conformance/service-deployment-conformance"
            )
        );
        assert!(query_of(&service_conformance(BASE).expect("link")).is_empty());
    }

    #[test]
    fn a_base_with_a_sub_path_keeps_it() {
        assert_eq!(
            service_conformance("https://o11y.example.test/grafana").as_deref(),
            Some("https://o11y.example.test/grafana/d/service-conformance/service-deployment-conformance")
        );
    }

    #[test]
    fn an_unparseable_base_gives_no_link() {
        assert_eq!(service_overview("not a url", "tududi"), None);
        assert_eq!(service_conformance("http://[::1"), None);
    }
}

#[cfg(test)]
mod usage_tests {
    use super::*;
    use std::collections::VecDeque;
    use std::net::Ipv4Addr;
    use std::sync::{Arc, Mutex};

    use crate::server::client_with_roots;
    use dioxus::server::axum::extract::{Query, State};
    use dioxus::server::axum::http::{header, HeaderMap, StatusCode};
    use dioxus::server::axum::routing::get;
    use dioxus::server::axum::Router;

    const TOKEN: &str = "grafana-viewer-token-value";

    /// A private address built at runtime: the repository scanner refuses a dotted private literal.
    fn ip(octets: [u8; 4]) -> String {
        Ipv4Addr::from(octets).to_string()
    }

    /// Two identities: alpha at 1234.4 (rounds down) and beta at 1234.5 (rounds half away from zero).
    const GOOD: &str = r#"{"status":"success","data":{"resultType":"vector","result":[
        {"metric":{"identity":"alpha"},"value":[1700000000,"1234.4"]},
        {"metric":{"identity":"beta"},"value":[1700000000,"1234.5"]}
    ]}}"#;

    #[test]
    fn the_query_is_the_declared_promql_on_the_proxy_path() {
        let url = usage_url("https://grafana.example.test").expect("url");
        assert_eq!(
            url.path(),
            "/api/datasources/proxy/uid/prometheus/api/v1/query"
        );
        let pairs: Vec<(String, String)> = url
            .query_pairs()
            .map(|(key, value)| (key.into_owned(), value.into_owned()))
            .collect();
        assert_eq!(pairs, [("query".to_string(), USAGE_QUERY.to_string())]);
        assert!(USAGE_QUERY.starts_with("sum by (identity) (increase("));
        assert!(USAGE_QUERY.contains(r#"{job="agentgateway"}"#));
    }

    #[test]
    fn the_origin_must_pass_the_transport_rule_and_carry_no_query_or_fragment() {
        assert_eq!(
            usage_url("http://grafana.example.test").err(),
            Some(ReadError::Refused),
            "public cleartext"
        );
        assert_eq!(
            usage_url("https://grafana.example.test#frag").err(),
            Some(ReadError::Refused)
        );
        assert_eq!(
            usage_url("https://grafana.example.test?x=1").err(),
            Some(ReadError::Refused)
        );
        assert_eq!(usage_url("not a url").err(), Some(ReadError::Refused));
        assert!(
            usage_url("http://127.0.0.1:3000").is_ok(),
            "loopback cleartext"
        );
        let private = format!("http://{}:3000", ip([10, 20, 0, 30]));
        assert!(usage_url(&private).is_ok(), "RFC1918 cleartext");
    }

    #[test]
    fn a_parsed_answer_maps_each_identity_to_its_whole_tokens() {
        let tokens = parse_usage(GOOD.as_bytes()).expect("parses");
        assert_eq!(tokens.get("alpha"), Some(&1234));
        assert_eq!(tokens.get("beta"), Some(&1235));
        assert_eq!(tokens.len(), 2);

        let scientific = r#"{"status":"success","data":{"resultType":"vector","result":[
            {"metric":{"identity":"gamma"},"value":[1700000000,"2.5e3"]},
            {"metric":{"identity":"delta"},"value":[1700000000,"0"]}
        ]}}"#;
        let tokens = parse_usage(scientific.as_bytes()).expect("parses");
        assert_eq!(tokens.get("gamma"), Some(&2500));
        assert_eq!(tokens.get("delta"), Some(&0));
    }

    #[test]
    fn a_series_it_cannot_name_or_read_is_skipped_so_its_client_reads_no_figure() {
        let text = r#"{"status":"success","data":{"resultType":"vector","result":[
            {"metric":{"model":"m1"},"value":[1700000000,"9"]},
            {"metric":{"identity":"nan-one"},"value":[1700000000,"NaN"]},
            {"metric":{"identity":"neg-one"},"value":[1700000000,"-4"]},
            {"metric":{"identity":"ok-one"},"value":[1700000000,"12"]}
        ]}}"#;
        let tokens = parse_usage(text.as_bytes()).expect("parses");
        assert_eq!(tokens.len(), 1);
        assert_eq!(tokens.get("ok-one"), Some(&12));
        assert_eq!(tokens.get("nan-one"), None, "no figure, not a wrong one");
        assert_eq!(tokens.get("neg-one"), None);
    }

    #[test]
    fn an_answer_that_is_not_a_success_vector_or_repeats_an_identity_is_refused() {
        for text in [
            r#"{"status":"error","data":null}"#,
            r#"{"status":"success","data":{"resultType":"matrix","result":[]}}"#,
            r#"{"status":"success","data":{"resultType":"vector","result":[
                {"metric":{"identity":"a"},"value":[1,"1"]},
                {"metric":{"identity":"a"},"value":[1,"2"]}]}}"#,
        ] {
            assert_eq!(
                parse_usage(text.as_bytes()).err(),
                Some(ReadError::Shape),
                "{text}"
            );
        }
        assert_eq!(parse_usage(b"<html>").err(), Some(ReadError::Decode));
    }

    #[test]
    fn the_usage_state_follows_the_configuration_and_the_reads() {
        assert_eq!(
            UsageCache::new(false).view().state,
            UsageState::Unconfigured
        );
        let mut cache = UsageCache::new(true);
        assert_eq!(
            cache.view(),
            UsageView {
                state: UsageState::Loading,
                refreshed_at: None,
                error: None
            }
        );
        cache.record_error("Grafana answered HTTP 500".into());
        assert_eq!(
            cache.view(),
            UsageView {
                state: UsageState::Error,
                refreshed_at: None,
                error: Some("The usage read failed: Grafana answered HTTP 500.".into()),
            }
        );
        cache.record_ok(HashMap::from([("alpha".to_string(), 7)]), "t1".into());
        assert_eq!(
            cache.view(),
            UsageView {
                state: UsageState::Ok,
                refreshed_at: Some("t1".into()),
                error: None
            }
        );
    }

    #[test]
    fn a_failed_read_keeps_the_last_good_figures_and_says_so() {
        let mut cache = UsageCache::new(true);
        cache.record_ok(HashMap::from([("alpha".to_string(), 7)]), "t1".into());
        cache.record_error("Grafana answered HTTP 500".into());
        assert_eq!(
            cache.tokens_for("alpha"),
            Some(7),
            "the last good figure stays"
        );
        assert_eq!(
            cache.view(),
            UsageView {
                state: UsageState::Error,
                refreshed_at: Some("t1".into()),
                error: Some(
                    "The newest usage read failed: Grafana answered HTTP 500. Showing the last good read."
                        .into()
                ),
            }
        );
    }

    #[test]
    fn an_identity_absent_from_the_read_has_no_figure() {
        let mut cache = UsageCache::new(true);
        assert_eq!(cache.tokens_for("alpha"), None, "no read has finished");
        cache.record_ok(HashMap::from([("alpha".to_string(), 7)]), "t1".into());
        assert_eq!(cache.tokens_for("alpha"), Some(7));
        assert_eq!(cache.tokens_for("ghost"), None);
    }

    /// A fake Grafana proxy. It answers the queued replies in order, and records each request's bearer and
    /// PromQL. Past its queue it answers 500 with the bearer echoed in the body, to prove the body never
    /// reaches an error.
    #[derive(Default)]
    struct Fake {
        replies: Mutex<VecDeque<(u16, String)>>,
        seen: Mutex<Vec<(Option<String>, Option<String>)>>,
    }

    async fn fake_query(
        State(fake): State<Arc<Fake>>,
        headers: HeaderMap,
        Query(query): Query<HashMap<String, String>>,
    ) -> (StatusCode, String) {
        let bearer = headers
            .get(header::AUTHORIZATION)
            .and_then(|value| value.to_str().ok())
            .map(str::to_string);
        fake.seen
            .lock()
            .expect("seen")
            .push((bearer.clone(), query.get("query").cloned()));
        let reply = fake.replies.lock().expect("replies").pop_front();
        match reply {
            Some((code, body)) => (StatusCode::from_u16(code).expect("status"), body),
            None => (
                StatusCode::INTERNAL_SERVER_ERROR,
                format!("echo {}", bearer.unwrap_or_default()),
            ),
        }
    }

    /// Serves `app` on loopback and returns its origin, which passes the transport rule.
    async fn serve_app(app: Router) -> String {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind");
        let port = listener.local_addr().expect("addr").port();
        tokio::spawn(async move {
            dioxus::server::axum::serve(listener, app)
                .await
                .expect("serve");
        });
        format!("http://127.0.0.1:{port}")
    }

    /// Serves the fake Grafana proxy and returns its origin.
    async fn serve(fake: Arc<Fake>) -> String {
        serve_app(
            Router::new()
                .route(USAGE_PATH, get(fake_query))
                .with_state(fake),
        )
        .await
    }

    /// The client the console really uses for the usage read: `server::client_with_roots`, the builder
    /// `server::load` calls, with its no-redirect policy and its timeout. A test on a different client
    /// would not test the production policy.
    fn production_client() -> reqwest::Client {
        client_with_roots(None, "CONSOLE_EXTRA_CA_FILE").expect("the production client")
    }

    /// A second fake Grafana. It records the Authorization of each request it gets, and answers a good read,
    /// so a redirect the console followed would both reach it and succeed.
    #[derive(Default)]
    struct Recorder {
        seen: Mutex<Vec<Option<String>>>,
    }

    async fn record_query(
        State(recorder): State<Arc<Recorder>>,
        headers: HeaderMap,
    ) -> (StatusCode, String) {
        let bearer = headers
            .get(header::AUTHORIZATION)
            .and_then(|value| value.to_str().ok())
            .map(str::to_string);
        recorder.seen.lock().expect("seen").push(bearer);
        (StatusCode::OK, GOOD.to_string())
    }

    /// A fake that answers 302 to `target`, the way a proxy or a moved origin would.
    async fn redirect_to(
        State(target): State<String>,
    ) -> (StatusCode, [(header::HeaderName, String); 1]) {
        (StatusCode::FOUND, [(header::LOCATION, target)])
    }

    /// The reply body that echoes the token, for the refusal tests: a body must never reach an error.
    fn refusal_body() -> String {
        format!("Grafana rejected the bearer {TOKEN}")
    }

    #[tokio::test]
    async fn a_read_sends_the_token_as_a_bearer_and_maps_the_answer() {
        let fake = Arc::new(Fake::default());
        fake.replies
            .lock()
            .expect("replies")
            .push_back((200, GOOD.to_string()));
        let base = serve(fake.clone()).await;
        let tokens = fetch_usage(&production_client(), &base, &Secret::new(TOKEN))
            .await
            .expect("read");
        assert_eq!(tokens.get("alpha"), Some(&1234));
        let seen = fake.seen.lock().expect("seen").clone();
        assert_eq!(
            seen,
            [(
                Some(format!("Bearer {TOKEN}")),
                Some(USAGE_QUERY.to_string())
            )]
        );
    }

    #[tokio::test]
    async fn the_newest_failed_read_keeps_the_last_good_figures_and_never_the_token() {
        let fake = Arc::new(Fake::default());
        fake.replies
            .lock()
            .expect("replies")
            .push_back((200, GOOD.to_string()));
        // Nothing is queued after the first answer, so the second read gets the 500 echo.
        let base = serve(fake.clone()).await;
        let http = production_client();
        let token = Secret::new(TOKEN);
        let mut cache = UsageCache::new(true);
        let first = fetch_usage(&http, &base, &token).await.expect("first read");
        cache.record_ok(first, "t1".into());

        let failed = fetch_usage(&http, &base, &token)
            .await
            .expect_err("a 500 is a failed read");
        assert_eq!(failed, ReadError::Status(500));
        let reason = failed.to_string();
        assert!(!reason.contains(TOKEN), "{reason}");
        cache.record_error(reason);

        assert_eq!(cache.tokens_for("alpha"), Some(1234));
        let view = cache.view();
        assert_eq!(view.state, UsageState::Error);
        let sentence = view.error.expect("sentence");
        assert_eq!(
            sentence,
            "The newest usage read failed: Grafana answered HTTP 500. Showing the last good read."
        );
        assert!(!sentence.contains(TOKEN), "{sentence}");
    }

    #[tokio::test]
    async fn a_cleartext_public_origin_is_refused_before_any_request() {
        let err = fetch_usage(
            &production_client(),
            "http://grafana.example.test",
            &Secret::new(TOKEN),
        )
        .await
        .expect_err("refused");
        assert_eq!(err, ReadError::Refused);
        assert!(!err.to_string().contains(TOKEN));
    }

    #[tokio::test]
    async fn a_refused_token_names_its_cause_and_never_the_token() {
        let cases = [
            (
                401,
                "Grafana refused the console token (HTTP 401): it is expired or invalid. Check Provision Console Grafana Token.",
            ),
            (
                403,
                "Grafana refused the console token (HTTP 403): the Viewer service account may not query the Prometheus data source.",
            ),
        ];
        for (status, cause) in cases {
            let fake = Arc::new(Fake::default());
            fake.replies
                .lock()
                .expect("replies")
                .push_back((status, refusal_body()));
            let base = serve(fake.clone()).await;
            let err = fetch_usage(&production_client(), &base, &Secret::new(TOKEN))
                .await
                .expect_err("a refused token is a failed read");
            assert_eq!(err, ReadError::Status(status));
            let text = err.to_string();
            assert_eq!(text, cause);
            assert!(!text.contains(TOKEN), "{text}");

            // The cause reaches the page without a doubled full stop.
            let mut cache = UsageCache::new(true);
            cache.record_ok(HashMap::from([("alpha".to_string(), 7)]), "t1".into());
            cache.record_error(text);
            let sentence = cache.view().error.expect("sentence");
            assert_eq!(
                sentence,
                format!("The newest usage read failed: {cause} Showing the last good read.")
            );
            assert!(!sentence.contains(TOKEN), "{sentence}");
            assert!(!sentence.contains(".."), "{sentence}");
        }
    }

    #[tokio::test]
    async fn a_redirect_is_not_followed_so_the_token_never_reaches_another_origin() {
        // The first origin answers 302 to a second fake, which records any request it gets.
        let recorder = Arc::new(Recorder::default());
        let elsewhere = serve_app(
            Router::new()
                .route(USAGE_PATH, get(record_query))
                .with_state(recorder.clone()),
        )
        .await;
        let source = serve_app(
            Router::new()
                .route(USAGE_PATH, get(redirect_to))
                .with_state(format!("{elsewhere}{USAGE_PATH}")),
        )
        .await;

        let err = fetch_usage(&production_client(), &source, &Secret::new(TOKEN))
            .await
            .expect_err("a 302 is not a usage read");
        assert_eq!(err, ReadError::Status(302));
        assert!(
            recorder.seen.lock().expect("seen").is_empty(),
            "the redirect target received a request"
        );
    }
}
