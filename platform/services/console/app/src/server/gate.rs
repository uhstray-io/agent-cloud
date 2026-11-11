//! The launch-gate client (plan 17 D49, D51). The console never calls OPA or Semaphore itself: it asks the
//! launch gate. `decide` previews a proposal's verdict and creates no task; `launch` creates the task at
//! approval. Both send the console's caller token as a bearer and the same body. The shared client follows no
//! redirect, so the token reaches only the origin the config names, and no URL, body or token reaches an error.

use std::collections::BTreeMap;
use std::time::Duration;

use reqwest::header::{ACCEPT, AUTHORIZATION};
use serde::de::DeserializeOwned;
use serde::{Deserialize, Serialize};

use super::config::{plain_origin, Secret};

const DECIDE_PATH: &str = "/v1/decide";
const LAUNCH_PATH: &str = "/v1/launch";

/// How long `decide` may take. It creates no task and makes no Semaphore call.
const DECIDE_TIMEOUT: Duration = Duration::from_secs(30);
/// How long `launch` may take. The gate's Semaphore calls each time out at 15 s (`launch_gate.py`,
/// `SEMAPHORE_TIMEOUT`), and a launch makes more than one, so the shared client's 20 s is too short.
const LAUNCH_TIMEOUT: Duration = Duration::from_secs(120);

/// The text when the gate did not answer a decision.
const NO_ANSWER: &str = "the launch gate did not answer";
/// The text when the origin is not one the console may send its token to. No request is made.
const ORIGIN_REFUSED: &str = "the launch gate origin is not one the console may send its token to";
/// The text when a launch's answer was lost or not read. The gate may already have created a task, so the
/// operator checks the template's tasks before launching again.
const LAUNCH_UNCERTAIN: &str = "the launch gate's answer was lost or not read; a task may have started, so check the template's tasks before launching again";

/// One proposal's launch request: the body both `/v1/decide` and `/v1/launch` take, sent as it serializes.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct GateCall {
    pub template: String,
    pub agent: String,
    pub workflow_id: String,
    pub step: String,
    pub survey: BTreeMap<String, String>,
    pub dry_run: bool,
}

/// What `decide` answers. `Denied` carries OPA's own sentence. `Refused` carries the gate's refusal code and detail.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum GateVerdict {
    Allowed { reason: String },
    Denied { reason: String },
    Refused { code: String, detail: String },
    Unavailable { detail: String },
}

/// What `launch` answers. `Launched` carries the task id, the message the gate tagged the task with, and OPA's reason.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LaunchOutcome {
    Launched {
        task_id: u64,
        message: String,
        reason: String,
    },
    Denied {
        reason: String,
    },
    Refused {
        status: u16,
        code: String,
        detail: String,
    },
    Unavailable {
        detail: String,
    },
}

/// The client for one launch gate. Holds the caller token. `Debug` is derived because `Secret` redacts itself.
#[derive(Clone, Debug)]
pub struct GateClient {
    base: String,
    token: Secret,
    http: reqwest::Client,
    /// Per-call limits. Each replaces the shared client's 20 s for its own call (`reqwest` `RequestBuilder::timeout`).
    decide_timeout: Duration,
    launch_timeout: Duration,
}

impl GateClient {
    /// `url` is the gate origin; a trailing slash is trimmed. `http` is the shared client, which follows no redirect.
    pub fn new(url: &str, token: Secret, http: reqwest::Client) -> Self {
        Self {
            base: url.trim_end_matches('/').to_string(),
            token,
            http,
            decide_timeout: DECIDE_TIMEOUT,
            launch_timeout: LAUNCH_TIMEOUT,
        }
    }

    /// The same client with shorter per-call limits, so a test can make a silent gate time out quickly.
    #[cfg(test)]
    fn with_timeouts(mut self, decide: Duration, launch: Duration) -> Self {
        self.decide_timeout = decide;
        self.launch_timeout = launch;
        self
    }

    /// The gate's decision for `call`. The gate creates no task (D51).
    pub async fn decide(&self, call: &GateCall) -> GateVerdict {
        let reply = match self
            .post(DECIDE_PATH, call, self.decide_timeout, NO_ANSWER)
            .await
        {
            Ok(reply) => reply,
            Err(detail) => {
                return GateVerdict::Unavailable {
                    detail: detail.to_string(),
                }
            }
        };
        if reply.status == 200 {
            return match parse::<DecideBody>(&reply.bytes) {
                Some(DecideBody { decision }) if decision.allowed => GateVerdict::Allowed {
                    reason: decision.reason,
                },
                Some(DecideBody { decision }) => GateVerdict::Denied {
                    reason: decision.reason,
                },
                None => GateVerdict::Unavailable {
                    detail: unreadable(reply.status),
                },
            };
        }
        match refusal(&reply.bytes) {
            Some(Refusal::Denied(reason)) => GateVerdict::Denied { reason },
            Some(Refusal::Other { code, detail }) => GateVerdict::Refused { code, detail },
            None => GateVerdict::Unavailable {
                detail: unreadable(reply.status),
            },
        }
    }

    /// Creates the task for `call` (D51). The gate decides again here, and its answer is the one that counts.
    pub async fn launch(&self, call: &GateCall) -> LaunchOutcome {
        let reply = match self
            .post(LAUNCH_PATH, call, self.launch_timeout, LAUNCH_UNCERTAIN)
            .await
        {
            Ok(reply) => reply,
            Err(detail) => {
                return LaunchOutcome::Unavailable {
                    detail: detail.to_string(),
                }
            }
        };
        if reply.status == 201 {
            return match parse::<LaunchedBody>(&reply.bytes) {
                Some(LaunchedBody {
                    task_id,
                    message,
                    decision,
                }) if decision.allowed => LaunchOutcome::Launched {
                    task_id,
                    message,
                    reason: decision.reason,
                },
                _ => LaunchOutcome::Unavailable {
                    detail: LAUNCH_UNCERTAIN.to_string(),
                },
            };
        }
        match refusal(&reply.bytes) {
            Some(Refusal::Denied(reason)) => LaunchOutcome::Denied { reason },
            Some(Refusal::Other { code, detail }) => LaunchOutcome::Refused {
                status: reply.status,
                code,
                detail,
            },
            None => LaunchOutcome::Unavailable {
                detail: LAUNCH_UNCERTAIN.to_string(),
            },
        }
    }

    /// One POST of `call` to `path` with the caller token, cut off after `timeout`. An origin the config would refuse
    /// is refused before any request. A transport failure, a timeout included, answers `on_no_answer`.
    async fn post(
        &self,
        path: &str,
        call: &GateCall,
        timeout: Duration,
        on_no_answer: &'static str,
    ) -> Result<Reply, &'static str> {
        let url = endpoint(&self.base, path).ok_or(ORIGIN_REFUSED)?;
        let response = self
            .http
            .post(url)
            .timeout(timeout)
            .header(AUTHORIZATION, format!("Bearer {}", self.token.expose()))
            .header(ACCEPT, "application/json")
            .json(call)
            .send()
            .await
            .map_err(|_| on_no_answer)?;
        let status = response.status().as_u16();
        let bytes = response.bytes().await.map_err(|_| on_no_answer)?;
        Ok(Reply {
            status,
            bytes: bytes.to_vec(),
        })
    }
}

/// The Semaphore template a proposal launches: `base`, with the ` (Dev)` suffix when `dev_variants` is on (D53).
pub fn template_name(base: &str, dev_variants: bool) -> String {
    if dev_variants {
        format!("{base} (Dev)")
    } else {
        base.to_string()
    }
}

/// A gate answer: its status and its raw body. The caller reads the body in the shape it expects.
struct Reply {
    status: u16,
    bytes: Vec<u8>,
}

/// `path` on `base`, when `base` is a plain origin. Checked on every call, not only at load, because a caller of
/// `GateClient::new` can pass any string.
fn endpoint(base: &str, path: &str) -> Option<reqwest::Url> {
    if !plain_origin(base) {
        return None;
    }
    reqwest::Url::parse(&format!("{base}{path}")).ok()
}

/// The `decision` object every gate answer carries.
#[derive(Deserialize)]
struct Decision {
    allowed: bool,
    reason: String,
}

#[derive(Deserialize)]
struct DecideBody {
    decision: Decision,
}

#[derive(Deserialize)]
struct LaunchedBody {
    task_id: u64,
    message: String,
    decision: Decision,
}

/// A refusal is `{"error": code, "detail": text}`, with `reason` on an OPA denial (`opa_denied`).
#[derive(Deserialize)]
struct RefusalBody {
    error: String,
    #[serde(default)]
    detail: String,
    reason: Option<String>,
}

enum Refusal {
    Denied(String),
    Other { code: String, detail: String },
}

fn refusal(bytes: &[u8]) -> Option<Refusal> {
    let body: RefusalBody = parse(bytes)?;
    match body.reason {
        Some(reason) if body.error == "opa_denied" => Some(Refusal::Denied(reason)),
        _ => Some(Refusal::Other {
            code: body.error,
            detail: body.detail,
        }),
    }
}

fn parse<T: DeserializeOwned>(bytes: &[u8]) -> Option<T> {
    serde_json::from_slice(bytes).ok()
}

/// The text for an answer whose body the console does not read. It names the status code only.
fn unreadable(status: u16) -> String {
    format!("the launch gate answered HTTP {status} with a body the console does not read")
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::VecDeque;
    use std::net::TcpListener;
    use std::sync::{Arc, Mutex};

    use crate::server::client_with_roots;
    use dioxus::server::axum::extract::State;
    use dioxus::server::axum::http::{header, HeaderMap, StatusCode};
    use dioxus::server::axum::routing::post;
    use dioxus::server::axum::Router;
    use serde_json::{json, Value};

    const TOKEN: &str = "console-caller-token-value";

    fn call() -> GateCall {
        GateCall {
            template: "Snapshot Service Assessment (Dev)".into(),
            agent: "service-agent".into(),
            workflow_id: "p42".into(),
            step: "service-assess".into(),
            survey: BTreeMap::from([("service".to_string(), "postiz".to_string())]),
            dry_run: false,
        }
    }

    /// One request the fake gate received.
    #[derive(Clone, Debug, PartialEq)]
    struct Seen {
        path: &'static str,
        bearer: Option<String>,
        content_type: Option<String>,
        body: Value,
    }

    /// A fake gate. It answers the queued replies in order and records each request. Past its queue it answers
    /// 500 with the bearer echoed in the body, to prove the body never reaches an error.
    #[derive(Default)]
    struct Fake {
        replies: Mutex<VecDeque<(u16, String)>>,
        seen: Mutex<Vec<Seen>>,
    }

    fn queue(fake: &Fake, replies: impl IntoIterator<Item = (u16, Value)>) {
        let mut queue = fake.replies.lock().expect("replies");
        for (status, body) in replies {
            queue.push_back((status, body.to_string()));
        }
    }

    fn answer(
        fake: &Fake,
        path: &'static str,
        headers: &HeaderMap,
        body: &str,
    ) -> (StatusCode, String) {
        let header_text = |name: header::HeaderName| {
            headers
                .get(name)
                .and_then(|value| value.to_str().ok())
                .map(str::to_string)
        };
        fake.seen.lock().expect("seen").push(Seen {
            path,
            bearer: header_text(header::AUTHORIZATION),
            content_type: header_text(header::CONTENT_TYPE),
            body: serde_json::from_str(body).unwrap_or(Value::Null),
        });
        match fake.replies.lock().expect("replies").pop_front() {
            Some((code, reply)) => (StatusCode::from_u16(code).expect("status"), reply),
            None => (
                StatusCode::INTERNAL_SERVER_ERROR,
                format!(
                    "echo {}",
                    header_text(header::AUTHORIZATION).unwrap_or_default()
                ),
            ),
        }
    }

    async fn fake_launch(
        State(fake): State<Arc<Fake>>,
        headers: HeaderMap,
        body: String,
    ) -> (StatusCode, String) {
        answer(&fake, LAUNCH_PATH, &headers, &body)
    }

    async fn fake_decide(
        State(fake): State<Arc<Fake>>,
        headers: HeaderMap,
        body: String,
    ) -> (StatusCode, String) {
        answer(&fake, DECIDE_PATH, &headers, &body)
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

    async fn serve(fake: Arc<Fake>) -> String {
        serve_app(
            Router::new()
                .route(DECIDE_PATH, post(fake_decide))
                .route(LAUNCH_PATH, post(fake_launch))
                .with_state(fake),
        )
        .await
    }

    /// The client the console really uses: `server::client_with_roots`, the builder `server::load` calls.
    fn production_client() -> reqwest::Client {
        client_with_roots(None, "CONSOLE_EXTRA_CA_FILE").expect("the production client")
    }

    fn gate(base: &str) -> GateClient {
        GateClient::new(base, Secret::new(TOKEN), production_client())
    }

    #[test]
    fn template_name_appends_the_dev_suffix_only_when_asked() {
        assert_eq!(
            template_name("Snapshot Service Assessment", true),
            "Snapshot Service Assessment (Dev)"
        );
        assert_eq!(
            template_name("Snapshot Service Assessment", false),
            "Snapshot Service Assessment"
        );
    }

    #[tokio::test]
    async fn decide_maps_an_allowed_and_a_denied_decision_to_their_verdicts() {
        let fake = Arc::new(Fake::default());
        queue(
            &fake,
            [
                (
                    200,
                    json!({"decision": {"allowed": true, "reason": "rule ok"}}),
                ),
                (
                    200,
                    json!({"decision": {"allowed": false, "reason": "branch pin"}}),
                ),
            ],
        );
        let client = gate(&serve(fake.clone()).await);
        assert_eq!(
            client.decide(&call()).await,
            GateVerdict::Allowed {
                reason: "rule ok".into()
            }
        );
        assert_eq!(
            client.decide(&call()).await,
            GateVerdict::Denied {
                reason: "branch pin".into()
            }
        );
    }

    #[tokio::test]
    async fn launch_maps_a_created_task_to_launched_with_its_tag_and_reason() {
        let fake = Arc::new(Fake::default());
        queue(
            &fake,
            [(
                201,
                json!({
                    "task_id": 4217,
                    "message": "console p42 service-assess",
                    "decision": {"allowed": true, "reason": "assessment is read-only"}
                }),
            )],
        );
        let client = gate(&serve(fake.clone()).await);
        assert_eq!(
            client.launch(&call()).await,
            LaunchOutcome::Launched {
                task_id: 4217,
                message: "console p42 service-assess".into(),
                reason: "assessment is read-only".into(),
            }
        );
    }

    #[tokio::test]
    async fn an_opa_denial_is_denied_with_opa_s_own_reason_on_both_routes() {
        let fake = Arc::new(Fake::default());
        let denial = json!({
            "error": "opa_denied",
            "detail": "OPA denied the launch",
            "reason": "proposal required for this step"
        });
        queue(&fake, [(403, denial.clone()), (403, denial)]);
        let client = gate(&serve(fake.clone()).await);
        assert_eq!(
            client.decide(&call()).await,
            GateVerdict::Denied {
                reason: "proposal required for this step".into()
            }
        );
        assert_eq!(
            client.launch(&call()).await,
            LaunchOutcome::Denied {
                reason: "proposal required for this step".into()
            }
        );
    }

    #[tokio::test]
    async fn other_refusals_keep_the_gates_code_and_detail_and_a_launch_keeps_its_status() {
        let fake = Arc::new(Fake::default());
        queue(
            &fake,
            [
                (
                    409,
                    json!({"error": "already_running", "detail": "template X already has a running task: 9"}),
                ),
                (
                    503,
                    json!({"error": "opa_unavailable", "detail": "OPA decision unavailable"}),
                ),
                (
                    400,
                    json!({"error": "bad_request", "detail": "dry_run must be true or false"}),
                ),
            ],
        );
        let client = gate(&serve(fake.clone()).await);
        assert_eq!(
            client.launch(&call()).await,
            LaunchOutcome::Refused {
                status: 409,
                code: "already_running".into(),
                detail: "template X already has a running task: 9".into(),
            }
        );
        assert_eq!(
            client.decide(&call()).await,
            GateVerdict::Refused {
                code: "opa_unavailable".into(),
                detail: "OPA decision unavailable".into(),
            }
        );
        assert_eq!(
            client.decide(&call()).await,
            GateVerdict::Refused {
                code: "bad_request".into(),
                detail: "dry_run must be true or false".into(),
            }
        );
    }

    #[tokio::test]
    async fn an_unreadable_launch_answer_is_uncertain_and_an_unreadable_decision_names_its_status()
    {
        let fake = Arc::new(Fake::default());
        queue(
            &fake,
            [
                // A created task whose body the console cannot read.
                (201, json!("not the launch shape")),
                // A proxy's plain-text failure: no JSON at all.
                (502, json!("bad gateway")),
                (200, json!("not the decision shape")),
            ],
        );
        let client = gate(&serve(fake.clone()).await);
        assert_eq!(
            client.launch(&call()).await,
            LaunchOutcome::Unavailable {
                detail: LAUNCH_UNCERTAIN.into()
            }
        );
        assert_eq!(
            client.launch(&call()).await,
            LaunchOutcome::Unavailable {
                detail: LAUNCH_UNCERTAIN.into()
            }
        );
        match client.decide(&call()).await {
            GateVerdict::Unavailable { detail } => {
                assert_eq!(detail, unreadable(200), "a decision names only its status");
            }
            other => panic!("expected Unavailable, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn a_silent_gate_times_out_as_unavailable_and_a_launch_says_it_may_have_started() {
        async fn slow() -> &'static str {
            tokio::time::sleep(Duration::from_secs(2)).await;
            "late"
        }
        let origin = serve_app(
            Router::new()
                .route(DECIDE_PATH, post(slow))
                .route(LAUNCH_PATH, post(slow)),
        )
        .await;
        // The shared client's 20 s would outlast the fake's 2 s, so only the per-call limit can cut this off.
        let client = GateClient::new(&origin, Secret::new(TOKEN), production_client())
            .with_timeouts(Duration::from_millis(200), Duration::from_millis(200));

        let verdict = client.decide(&call()).await;
        assert_eq!(
            verdict,
            GateVerdict::Unavailable {
                detail: NO_ANSWER.into()
            }
        );
        let outcome = client.launch(&call()).await;
        assert_eq!(
            outcome,
            LaunchOutcome::Unavailable {
                detail: LAUNCH_UNCERTAIN.into()
            }
        );
        let port = origin.rsplit(':').next().expect("port");
        for text in [format!("{verdict:?}"), format!("{outcome:?}")] {
            assert!(!text.contains(TOKEN), "the token reached an error: {text}");
            assert!(
                !text.contains(port),
                "the gate address reached an error: {text}"
            );
        }
    }

    #[tokio::test]
    async fn each_call_has_its_own_limit_and_the_limits_are_the_contract_values() {
        assert_eq!(DECIDE_TIMEOUT, Duration::from_secs(30));
        assert_eq!(LAUNCH_TIMEOUT, Duration::from_secs(120));
        let defaults = gate("http://127.0.0.1:1");
        assert_eq!(defaults.decide_timeout, DECIDE_TIMEOUT);
        assert_eq!(defaults.launch_timeout, LAUNCH_TIMEOUT);

        // Each answer takes 1 s. A decide limit of 100 ms cuts the decision off, and a launch limit of 3 s lets
        // the launch answer arrive: a launch must not inherit the decide limit.
        async fn slow_decide() -> &'static str {
            tokio::time::sleep(Duration::from_secs(1)).await;
            r#"{"decision": {"allowed": true, "reason": "late"}}"#
        }
        async fn slow_launch() -> (StatusCode, &'static str) {
            tokio::time::sleep(Duration::from_secs(1)).await;
            (
                StatusCode::CREATED,
                r#"{"task_id": 9, "message": "m", "decision": {"allowed": true, "reason": "r"}}"#,
            )
        }
        let origin = serve_app(
            Router::new()
                .route(DECIDE_PATH, post(slow_decide))
                .route(LAUNCH_PATH, post(slow_launch)),
        )
        .await;
        let client = GateClient::new(&origin, Secret::new(TOKEN), production_client())
            .with_timeouts(Duration::from_millis(100), Duration::from_secs(3));
        assert_eq!(
            client.decide(&call()).await,
            GateVerdict::Unavailable {
                detail: NO_ANSWER.into()
            }
        );
        assert_eq!(
            client.launch(&call()).await,
            LaunchOutcome::Launched {
                task_id: 9,
                message: "m".into(),
                reason: "r".into(),
            }
        );
    }

    #[tokio::test]
    async fn a_refused_connection_is_unavailable_and_names_no_address() {
        // Bind, note the port, drop the listener: nothing answers there now.
        let port = TcpListener::bind("127.0.0.1:0")
            .expect("bind")
            .local_addr()
            .expect("addr")
            .port();
        let client = gate(&format!("http://127.0.0.1:{port}"));
        let detail = match client.decide(&call()).await {
            GateVerdict::Unavailable { detail } => detail,
            other => panic!("expected Unavailable, got {other:?}"),
        };
        assert_eq!(detail, NO_ANSWER);
        assert!(!detail.contains(&port.to_string()));
        assert!(!detail.contains("127.0.0.1"));
    }

    #[tokio::test]
    async fn an_answer_that_is_not_json_names_no_token_even_when_the_body_echoes_it() {
        // The empty queue answers 500 with the bearer echoed in the body.
        let fake = Arc::new(Fake::default());
        let client = gate(&serve(fake.clone()).await);
        let verdict = client.decide(&call()).await;
        assert_eq!(
            verdict,
            GateVerdict::Unavailable {
                detail: unreadable(500),
            }
        );
        assert!(!format!("{verdict:?}").contains(TOKEN));
    }

    #[tokio::test]
    async fn the_token_is_a_bearer_and_the_body_is_exactly_the_gate_contract() {
        let fake = Arc::new(Fake::default());
        queue(
            &fake,
            [(200, json!({"decision": {"allowed": true, "reason": "ok"}}))],
        );
        let client = gate(&serve(fake.clone()).await);
        client.decide(&call()).await;
        let seen = fake.seen.lock().expect("seen").clone();
        assert_eq!(
            seen,
            [Seen {
                path: DECIDE_PATH,
                bearer: Some(format!("Bearer {TOKEN}")),
                content_type: Some("application/json".into()),
                body: json!({
                    "template": "Snapshot Service Assessment (Dev)",
                    "agent": "service-agent",
                    "workflow_id": "p42",
                    "step": "service-assess",
                    "survey": {"service": "postiz"},
                    "dry_run": false,
                }),
            }]
        );
    }

    #[tokio::test]
    async fn a_redirect_is_not_followed_so_the_token_never_reaches_another_origin() {
        // The gate answers 302 to a second fake, which would allow the decision if the client followed it.
        let recorder = Arc::new(Mutex::new(Vec::<Option<String>>::new()));
        async fn record(
            State(seen): State<Arc<Mutex<Vec<Option<String>>>>>,
            headers: HeaderMap,
        ) -> (StatusCode, String) {
            seen.lock().expect("seen").push(
                headers
                    .get(header::AUTHORIZATION)
                    .and_then(|value| value.to_str().ok())
                    .map(str::to_string),
            );
            (
                StatusCode::OK,
                json!({"decision": {"allowed": true, "reason": "ok"}}).to_string(),
            )
        }
        let elsewhere = serve_app(
            Router::new()
                .route(DECIDE_PATH, post(record))
                .with_state(recorder.clone()),
        )
        .await;
        async fn redirect(
            State(target): State<String>,
        ) -> (StatusCode, [(header::HeaderName, String); 1]) {
            (StatusCode::FOUND, [(header::LOCATION, target)])
        }
        let source = serve_app(
            Router::new()
                .route(DECIDE_PATH, post(redirect))
                .with_state(format!("{elsewhere}{DECIDE_PATH}")),
        )
        .await;

        let verdict = gate(&source).decide(&call()).await;
        assert_eq!(
            verdict,
            GateVerdict::Unavailable {
                detail: unreadable(302),
            }
        );
        assert!(
            recorder.lock().expect("seen").is_empty(),
            "the redirect target received a request"
        );
    }

    #[tokio::test]
    async fn an_origin_the_config_would_refuse_is_refused_before_any_request() {
        // A cleartext public host and an origin with a path: neither is contacted.
        for base in ["http://gate.example.test:8443", "http://127.0.0.1:1/extra"] {
            let client = gate(base);
            assert_eq!(
                client.decide(&call()).await,
                GateVerdict::Unavailable {
                    detail: ORIGIN_REFUSED.into()
                },
                "{base}"
            );
            assert_eq!(
                client.launch(&call()).await,
                LaunchOutcome::Unavailable {
                    detail: ORIGIN_REFUSED.into()
                },
                "{base}"
            );
        }
    }

    #[tokio::test]
    async fn a_trailing_slash_on_the_gate_origin_is_trimmed() {
        let fake = Arc::new(Fake::default());
        queue(
            &fake,
            [(200, json!({"decision": {"allowed": true, "reason": "ok"}}))],
        );
        let base = serve(fake.clone()).await;
        let verdict = gate(&format!("{base}/")).decide(&call()).await;
        assert_eq!(
            verdict,
            GateVerdict::Allowed {
                reason: "ok".into()
            }
        );
    }
}
