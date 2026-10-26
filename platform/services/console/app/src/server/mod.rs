//! Server core: state, session layer, router and the background OIDC discovery.

pub mod api;
pub mod auth;
pub mod config;
pub mod prefs;
pub mod scope;

use std::fs;
use std::sync::Arc;
use std::time::{Duration, Instant};

use dioxus::server::axum::routing::{any, get, post};
use dioxus::server::axum::Router;
use tower_sessions::cookie::time::Duration as CookieDuration;
use tower_sessions::cookie::SameSite;
use tower_sessions::session::Expiry;
use tower_sessions::{MemoryStore, SessionManagerLayer};

use crate::ui;
use auth::OidcClient;
use config::Config;
use prefs::Prefs;
use scope::Scope;

pub struct AppState {
    pub config: Config,
    /// One client for discovery, token exchange and userinfo. Redirects are not followed.
    pub http: reqwest::Client,
    /// Set once by the discovery task. Empty means `/auth/*` answers 503.
    pub oidc: tokio::sync::OnceCell<OidcClient>,
    pub scope: Scope,
    pub prefs: Prefs,
}

pub type SharedState = Arc<AppState>;

/// How long discovery retries before the process exits and the container restarts.
const DISCOVERY_BUDGET: Duration = Duration::from_secs(60);

/// Reads the scope file, opens the preferences database and builds the HTTP
/// client. Everything here fails fast, before any socket is bound.
pub fn load(config: Config) -> Result<SharedState, String> {
    let scope_text = fs::read_to_string(&config.scope_file).map_err(|err| {
        format!(
            "CONSOLE_SCOPE_FILE: cannot read {}: {err}",
            config.scope_file.display()
        )
    })?;
    let scope = Scope::parse(&scope_text).map_err(|err| format!("CONSOLE_SCOPE_FILE: {err}"))?;

    let prefs = Prefs::open(&config.data_dir.join("prefs.db")).map_err(|err| {
        format!(
            "CONSOLE_DATA_DIR: cannot open prefs database in {}: {err}",
            config.data_dir.display()
        )
    })?;

    let mut builder = reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(Duration::from_secs(20));
    if let Some(path) = &config.extra_ca_file {
        let pem = fs::read(path).map_err(|err| {
            format!(
                "CONSOLE_EXTRA_CA_FILE: cannot read {}: {err}",
                path.display()
            )
        })?;
        let certs = reqwest::Certificate::from_pem_bundle(&pem)
            .map_err(|err| format!("CONSOLE_EXTRA_CA_FILE: not a PEM bundle: {err}"))?;
        if certs.is_empty() {
            return Err("CONSOLE_EXTRA_CA_FILE: no certificates found".to_string());
        }
        for cert in certs {
            builder = builder.add_root_certificate(cert);
        }
    }
    let http = builder
        .build()
        .map_err(|err| format!("cannot build the HTTP client: {err}"))?;

    Ok(Arc::new(AppState {
        config,
        http,
        oidc: tokio::sync::OnceCell::new(),
        scope,
        prefs,
    }))
}

/// Starts OIDC discovery in the background. The server is already listening,
/// so the healthcheck does not depend on Authentik being reachable at boot.
/// After `DISCOVERY_BUDGET` of failures the process exits non-zero.
pub fn spawn_discovery(state: SharedState) {
    tokio::spawn(async move {
        let started = Instant::now();
        let mut delay = Duration::from_secs(1);
        loop {
            match auth::discover(&state).await {
                Ok(client) => {
                    let _ = state.oidc.set(client);
                    println!(
                        "console: OIDC discovery ready for {}",
                        state.config.oidc_issuer
                    );
                    return;
                }
                Err(err) => {
                    if started.elapsed() >= DISCOVERY_BUDGET {
                        eprintln!(
                            "console: OIDC discovery gave up after {}s: {err}",
                            DISCOVERY_BUDGET.as_secs()
                        );
                        std::process::exit(1);
                    }
                    eprintln!(
                        "console: OIDC discovery not ready, retrying in {}s: {err}",
                        delay.as_secs()
                    );
                    tokio::time::sleep(delay).await;
                    delay = (delay * 2).min(Duration::from_secs(16));
                }
            }
        }
    });
}

/// Session cookie `console_session`: Secure, HttpOnly, SameSite=Lax, 8h of inactivity.
/// The memory store logs everyone out on restart (accepted for Phase 1).
pub fn session_layer() -> SessionManagerLayer<MemoryStore> {
    SessionManagerLayer::new(MemoryStore::default())
        .with_name("console_session")
        .with_secure(true)
        .with_http_only(true)
        .with_same_site(SameSite::Lax)
        .with_expiry(Expiry::OnInactivity(CookieDuration::hours(8)))
}

async fn healthz() -> &'static str {
    "ok"
}

/// Plain axum routes for health, auth and the JSON API. The UI is not involved.
pub fn api_router(state: SharedState) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/auth/login", get(auth::login))
        .route("/auth/callback", get(auth::callback))
        .route("/auth/logout", post(auth::logout))
        .route("/v1/me", get(api::get_me).put(api::put_me))
        .route("/v1/tenants", get(api::list_tenants))
        .route("/v1/tenants/{tenant}/tree", get(api::tenant_tree))
        .route("/v1/{*rest}", any(api::v1_unmatched))
        .with_state(state)
}

/// Everything the process serves: the Dioxus app, the API, and the session
/// layer over both. The layer is applied last so every route sees it.
pub fn router(state: SharedState) -> Router {
    dioxus::server::router(ui::app)
        .merge(api_router(state))
        .layer(session_layer())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::probe::{http_get_raw, parse_status_line};
    use std::io::{Read, Write};
    use std::net::{SocketAddr, TcpStream};
    use std::time::Duration;

    const SCOPE: &str = r#"{
        "example": true,
        "tenants": [{"id": "acme", "name": "Acme", "orgs": [
            {"id": "ops", "name": "Ops", "sites": [{"id": "dc1", "name": "DC 1", "envs": ["prod"]}]}
        ]}]
    }"#;

    /// The OIDC cell stays empty, so /auth/* answers 503 in these tests.
    fn test_state() -> SharedState {
        Arc::new(AppState {
            config: Config {
                base_url: "https://console.test".into(),
                oidc_issuer: "https://auth.test/application/o/console/".into(),
                oidc_client_id: "console".into(),
                oidc_client_secret: "test-secret".into(),
                extra_ca_file: None,
                data_dir: std::env::temp_dir(),
                scope_file: std::env::temp_dir(),
            },
            http: reqwest::Client::new(),
            oidc: tokio::sync::OnceCell::new(),
            scope: Scope::parse(SCOPE).expect("test scope"),
            prefs: Prefs::in_memory().expect("in-memory prefs"),
        })
    }

    /// Test-only route that signs a fixed user in, so authenticated paths run
    /// without an identity provider.
    fn test_login_route() -> Router {
        use dioxus::server::axum::routing::get;
        use tower_sessions::Session;
        Router::new().route(
            "/test/login",
            get(|session: Session| async move {
                let user = auth::CurrentUser {
                    sub: "user-1".into(),
                    email: Some("ada@example.test".into()),
                    name: Some("Ada".into()),
                    username: Some("ada".into()),
                    groups: vec!["ops".into()],
                };
                session
                    .insert(auth::KEY_USER, &user)
                    .await
                    .expect("insert user");
                "signed in"
            }),
        )
    }

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

    struct Reply {
        status: u16,
        cookie: Option<String>,
        set_cookie_line: Option<String>,
        body: String,
    }

    /// One raw HTTP/1.1 request. `cookie` is sent as-is, e.g. `console_session=...`.
    fn raw_request(
        addr: SocketAddr,
        method: &str,
        path: &str,
        cookie: Option<&str>,
        body: &str,
    ) -> Reply {
        let mut stream =
            TcpStream::connect_timeout(&addr, Duration::from_secs(5)).expect("connect");
        stream
            .set_read_timeout(Some(Duration::from_secs(5)))
            .expect("timeout");
        let cookie_line = cookie
            .map(|c| format!("Cookie: {c}\r\n"))
            .unwrap_or_default();
        let request = format!(
            "{method} {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n{cookie_line}\
             Content-Type: application/json\r\nContent-Length: {}\r\n\r\n{body}",
            body.len()
        );
        stream.write_all(request.as_bytes()).expect("write");
        let mut raw = Vec::new();
        stream.read_to_end(&mut raw).expect("read");
        let text = String::from_utf8_lossy(&raw).into_owned();
        let (head, body) = text.split_once("\r\n\r\n").unwrap_or((text.as_str(), ""));
        let status = parse_status_line(head.lines().next().unwrap_or("")).expect("status line");
        let cookie = head
            .lines()
            .find_map(|line| {
                line.strip_prefix("set-cookie: ")
                    .or_else(|| line.strip_prefix("Set-Cookie: "))
            })
            .and_then(|value| value.split(';').next())
            .map(str::to_string);
        let set_cookie_line = head
            .lines()
            .find(|line| line.to_ascii_lowercase().starts_with("set-cookie:"))
            .map(str::to_string);
        Reply {
            status,
            cookie,
            set_cookie_line,
            body: body.to_string(),
        }
    }

    async fn call(
        addr: SocketAddr,
        method: &'static str,
        path: &'static str,
        cookie: Option<String>,
        body: &'static str,
    ) -> Reply {
        tokio::task::spawn_blocking(move || {
            raw_request(addr, method, path, cookie.as_deref(), body)
        })
        .await
        .expect("blocking request")
    }

    #[tokio::test]
    async fn unauthenticated_v1_routes_answer_401_json() {
        let addr = serve(api_router(test_state()).layer(session_layer())).await;
        for path in [
            "/v1/me",
            "/v1/tenants",
            "/v1/tenants/acme/tree",
            "/v1/tenants/globex/tree",
            "/v1/nope",
        ] {
            let reply = call(addr, "GET", path, None, "").await;
            assert_eq!(reply.status, 401, "{path}");
            assert_eq!(reply.body, r#"{"error":"unauthenticated"}"#, "{path}");
        }
        let put = call(addr, "PUT", "/v1/me", None, "not json").await;
        assert_eq!(
            put.status, 401,
            "PUT without session is 401 even with a bad body"
        );
    }

    #[tokio::test]
    async fn healthz_answers_ok_without_session() {
        let addr = serve(api_router(test_state()).layer(session_layer())).await;
        let reply = call(addr, "GET", "/healthz", None, "").await;
        assert_eq!(reply.status, 200);
        assert_eq!(reply.body, "ok");
    }

    #[tokio::test]
    async fn auth_login_is_503_until_discovery_is_ready() {
        let addr = serve(api_router(test_state()).layer(session_layer())).await;
        let reply = call(addr, "GET", "/auth/login", None, "").await;
        assert_eq!(reply.status, 503);
        assert_eq!(reply.body, r#"{"error":"oidc_unavailable"}"#);
    }

    #[tokio::test]
    async fn signed_in_api_round_trip() {
        let app = api_router(test_state())
            .merge(test_login_route())
            .layer(session_layer());
        let addr = serve(app).await;

        let login = call(addr, "GET", "/test/login", None, "").await;
        let cookie = login.cookie.clone().expect("session cookie set on login");
        let set_cookie = login.set_cookie_line.clone().expect("Set-Cookie line");
        for attribute in ["Secure", "HttpOnly", "SameSite=Lax", "console_session="] {
            assert!(
                set_cookie.contains(attribute),
                "cookie missing {attribute}: {set_cookie}"
            );
        }

        let me = call(addr, "GET", "/v1/me", Some(cookie.clone()), "").await;
        assert_eq!(me.status, 200);
        let me_json: serde_json::Value = serde_json::from_str(&me.body).expect("json");
        assert_eq!(me_json["claims"]["sub"], "user-1");
        assert_eq!(me_json["claims"]["groups"][0], "ops");
        assert_eq!(me_json["preferences"]["theme"], "system");
        assert_eq!(me_json["preferences"]["timezone"], "UTC");

        let put_ok = call(
            addr,
            "PUT",
            "/v1/me",
            Some(cookie.clone()),
            r#"{"preferences":{"theme":"dark","timezone":"Europe/Berlin","default_scope":"acme/ops"}}"#,
        )
        .await;
        assert_eq!(put_ok.status, 200);
        let put_json: serde_json::Value = serde_json::from_str(&put_ok.body).expect("json");
        assert_eq!(put_json["preferences"]["theme"], "dark");
        assert_eq!(put_json["preferences"]["default_scope"], "acme/ops");

        let again = call(addr, "GET", "/v1/me", Some(cookie.clone()), "").await;
        let again_json: serde_json::Value = serde_json::from_str(&again.body).expect("json");
        assert_eq!(again_json["preferences"]["timezone"], "Europe/Berlin");

        let bad_theme = call(
            addr,
            "PUT",
            "/v1/me",
            Some(cookie.clone()),
            r#"{"preferences":{"theme":"neon"}}"#,
        )
        .await;
        assert_eq!(bad_theme.status, 422);
        assert!(bad_theme.body.contains("invalid_preferences"));

        let bad_json = call(addr, "PUT", "/v1/me", Some(cookie.clone()), "not json").await;
        assert_eq!(bad_json.status, 422);

        let unknown_field = call(
            addr,
            "PUT",
            "/v1/me",
            Some(cookie.clone()),
            r#"{"preferences":{},"extra":1}"#,
        )
        .await;
        assert_eq!(unknown_field.status, 422);

        let tenants = call(addr, "GET", "/v1/tenants", Some(cookie.clone()), "").await;
        assert_eq!(tenants.status, 200);
        assert_eq!(
            tenants.body,
            r#"{"example":true,"tenants":[{"id":"acme","name":"Acme"}]}"#
        );

        let tree = call(
            addr,
            "GET",
            "/v1/tenants/acme/tree",
            Some(cookie.clone()),
            "",
        )
        .await;
        assert_eq!(tree.status, 200);
        let tree_json: serde_json::Value = serde_json::from_str(&tree.body).expect("json");
        assert_eq!(tree_json["tenant"]["id"], "acme");
        assert_eq!(tree_json["orgs"][0]["sites"][0]["envs"][0], "prod");
        assert_eq!(tree_json["example"], true);

        let unknown = call(
            addr,
            "GET",
            "/v1/tenants/globex/tree",
            Some(cookie.clone()),
            "",
        )
        .await;
        assert_eq!(unknown.status, 404);
        assert_eq!(unknown.body, r#"{"error":"unknown_tenant"}"#);

        let unmatched = call(addr, "GET", "/v1/nope", Some(cookie), "").await;
        assert_eq!(unmatched.status, 404);
        assert_eq!(unmatched.body, r#"{"error":"not_found"}"#);
    }

    #[tokio::test]
    async fn probe_helper_reads_live_healthz() {
        let addr = serve(api_router(test_state()).layer(session_layer())).await;
        let text = tokio::task::spawn_blocking(move || {
            http_get_raw(addr, "/healthz", Duration::from_secs(5))
        })
        .await
        .expect("join")
        .expect("get");
        assert!(
            text.starts_with("HTTP/1.1 200") || text.starts_with("HTTP/1.0 200"),
            "{text}"
        );
    }
}
