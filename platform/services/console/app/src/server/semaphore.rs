//! Read-only client for Semaphore's task list. Only the poller calls it; the
//! browser never talks to Semaphore. Errors are fixed variants: no URL, no
//! response body and no header ever reaches a message or a log line.

use std::fmt;

use serde::Deserialize;

use super::config::SemaphoreConfig;

/// One row of `GET /api/project/{p}/tasks/last` (Semaphore `TaskWithTpl`). Only the
/// fields the Active Work view needs are declared. serde ignores the rest, so
/// `arguments`, `params`, `output` and `environment` beyond `target_service` are never
/// held. No Debug derive: `environment` can carry secrets.
#[derive(Deserialize)]
pub struct Task {
    pub id: u64,
    #[serde(default)]
    pub status: String,
    #[serde(default)]
    pub created: String,
    #[serde(default)]
    pub start: Option<String>,
    #[serde(default)]
    pub end: Option<String>,
    #[serde(default)]
    pub message: Option<String>,
    #[serde(default)]
    pub tpl_alias: Option<String>,
    #[serde(default)]
    pub user_name: Option<String>,
    #[serde(default)]
    pub environment: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum FetchError {
    Timeout,
    Connect,
    Status(u16),
    Decode,
    Other,
}

impl fmt::Display for FetchError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            FetchError::Timeout => f.write_str("Semaphore request timed out"),
            FetchError::Connect => f.write_str("cannot connect to Semaphore"),
            FetchError::Status(code) => write!(f, "Semaphore answered HTTP {code}"),
            FetchError::Decode => f.write_str("Semaphore response could not be decoded"),
            FetchError::Other => f.write_str("Semaphore request failed"),
        }
    }
}

/// The newest 200 tasks of the configured project. The token travels only in the
/// `Authorization` header.
pub async fn fetch_last(
    http: &reqwest::Client,
    config: &SemaphoreConfig,
) -> Result<Vec<Task>, FetchError> {
    let url = format!(
        "{}/api/project/{}/tasks/last",
        config.api_url, config.project
    );
    let response = http
        .get(url)
        .bearer_auth(config.token.expose())
        .send()
        .await
        .map_err(transport_error)?;
    let status = response.status();
    if !status.is_success() {
        return Err(FetchError::Status(status.as_u16()));
    }
    let body = response.bytes().await.map_err(transport_error)?;
    serde_json::from_slice::<Vec<Task>>(&body).map_err(|_| FetchError::Decode)
}

/// reqwest's own Display names the request URL, so it is classified, not shown.
fn transport_error(err: reqwest::Error) -> FetchError {
    if err.is_timeout() {
        FetchError::Timeout
    } else if err.is_connect() {
        FetchError::Connect
    } else {
        FetchError::Other
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::server::config::Secret;
    use dioxus::server::axum::http::{header, HeaderMap, StatusCode};
    use dioxus::server::axum::response::{IntoResponse, Response};
    use dioxus::server::axum::routing::get;
    use dioxus::server::axum::Router;
    use std::net::SocketAddr;

    const TOKEN: &str = "test-token-value";

    /// Answers the task list only when the Bearer token matches. The 500 path echoes
    /// the header it received, so a leak would show up in the error text.
    async fn task_list(headers: HeaderMap) -> Response {
        let auth = headers
            .get(header::AUTHORIZATION)
            .and_then(|value| value.to_str().ok())
            .unwrap_or("")
            .to_string();
        if auth == format!("Bearer {TOKEN}") {
            (StatusCode::OK, FIXTURE).into_response()
        } else if auth == "Bearer boom" {
            (StatusCode::INTERNAL_SERVER_ERROR, format!("boom {auth}")).into_response()
        } else if auth == "Bearer garbage" {
            (StatusCode::OK, "{not json, secret=leaked").into_response()
        } else {
            StatusCode::UNAUTHORIZED.into_response()
        }
    }

    /// Real `TaskWithTpl` shape with the fields the console must never expose:
    /// `environment` carries a secret, and `arguments`, `params`, `output` are present.
    const FIXTURE: &str = r#"[
        {"id": 12, "template_id": 3, "project_id": 1, "status": "success",
         "created": "2026-10-26T12:00:00Z", "start": "2026-10-26T12:00:01Z",
         "end": "2026-10-26T12:01:00Z", "message": "skynet service_onboarding:abc step-1",
         "tpl_alias": "Deploy tududi", "user_name": "svc-console",
         "environment": "{\"target_service\":\"tududi\",\"SECRET_TOKEN\":\"hunter2\"}",
         "arguments": "{\"password\":\"arg-secret\"}", "params": {"x": 1},
         "output": "log line with hunter2"},
        {"id": 11, "status": "running", "created": "2026-10-26T11:00:00Z",
         "start": null, "end": null, "message": null, "tpl_alias": "Check Secrets",
         "user_name": null, "environment": null}
    ]"#;

    async fn serve(app: Router) -> SocketAddr {
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

    fn config_for(addr: SocketAddr, token: &str) -> SemaphoreConfig {
        SemaphoreConfig {
            api_url: format!("http://{addr}"),
            ui_url: "https://semaphore.test:8443".to_string(),
            project: 1,
            token: Secret::new(token),
        }
    }

    fn app() -> Router {
        Router::new().route("/api/project/1/tasks/last", get(task_list))
    }

    #[tokio::test]
    async fn fetch_sends_bearer_and_decodes_only_the_declared_fields() {
        let addr = serve(app()).await;
        let config = config_for(addr, TOKEN);
        let http = reqwest::Client::new();
        let tasks = fetch_last(&http, &config).await.expect("fetch");
        assert_eq!(tasks.len(), 2);
        assert_eq!(tasks[0].id, 12);
        assert_eq!(tasks[0].tpl_alias.as_deref(), Some("Deploy tududi"));
        assert_eq!(tasks[1].environment, None);
        assert_eq!(tasks[1].start, None);
    }

    #[tokio::test]
    async fn wrong_token_is_a_status_error_without_the_token_in_it() {
        let addr = serve(app()).await;
        let config = config_for(addr, "wrong-token");
        let http = reqwest::Client::new();
        let err = fetch_last(&http, &config).await.err().expect("must fail");
        assert_eq!(err, FetchError::Status(401));
        assert!(!err.to_string().contains("wrong-token"));
    }

    #[tokio::test]
    async fn server_error_text_has_no_token_and_no_body() {
        let addr = serve(app()).await;
        let config = config_for(addr, "boom");
        let http = reqwest::Client::new();
        let err = fetch_last(&http, &config).await.err().expect("must fail");
        assert_eq!(err, FetchError::Status(500));
        let text = err.to_string();
        assert_eq!(text, "Semaphore answered HTTP 500");
        assert!(!text.contains("boom"), "{text}");
        assert!(!text.contains("Bearer"), "{text}");
    }

    #[tokio::test]
    async fn undecodable_body_is_a_decode_error_without_body_text() {
        let addr = serve(app()).await;
        let config = config_for(addr, "garbage");
        let http = reqwest::Client::new();
        let err = fetch_last(&http, &config).await.err().expect("must fail");
        assert_eq!(err, FetchError::Decode);
        let text = err.to_string();
        assert!(!text.contains("leaked"), "{text}");
        assert!(!text.contains("not json"), "{text}");
    }

    #[tokio::test]
    async fn connection_refused_is_a_connect_error() {
        // Bind then drop: the port is free, so connecting is refused.
        let listener = std::net::TcpListener::bind("127.0.0.1:0").expect("bind");
        let addr = listener.local_addr().expect("addr");
        drop(listener);
        let config = config_for(addr, TOKEN);
        let http = reqwest::Client::new();
        let err = fetch_last(&http, &config).await.err().expect("must fail");
        assert_eq!(err, FetchError::Connect);
    }

    #[tokio::test]
    async fn fixture_secrets_never_reach_the_work_items() {
        use crate::server::work::{build_items, Step};
        let addr = serve(app()).await;
        let config = config_for(addr, TOKEN);
        let http = reqwest::Client::new();
        let tasks = fetch_last(&http, &config).await.expect("fetch");
        let steps = vec![Step {
            id: "step-1".into(),
            order: 1,
            owner: "service-agent".into(),
            label: "Plan".into(),
        }];
        let items = build_items(tasks, &steps, &config);
        let json = serde_json::to_string(&items).expect("json");
        for leaked in [
            "hunter2",
            "arg-secret",
            "SECRET_TOKEN",
            "environment",
            "output",
        ] {
            assert!(!json.contains(leaked), "{leaked} leaked: {json}");
        }
        assert!(json.contains("tududi"), "{json}");
    }
}
