//! Server core: state, session layer, router and the background OIDC discovery.

pub mod api;
pub mod auth;
pub mod config;
pub mod grafana;
pub mod inventory;
pub mod netbox;
pub mod prefs;
pub mod proxmox;
pub mod scope;
pub mod semaphore;
pub mod sessions;
pub mod work;

use std::fs;
use std::path::Path;
use std::sync::Arc;
use std::time::{Duration, Instant};

use dioxus::server::axum::routing::{any, get, post};
use dioxus::server::axum::Router;
use tower_sessions::cookie::time::Duration as CookieDuration;
use tower_sessions::cookie::SameSite;
use tower_sessions::session::Expiry;
use tower_sessions::SessionManagerLayer;

use crate::ui;
use auth::OidcClient;
use config::Config;
use prefs::Prefs;
use scope::Scope;
use sessions::SqliteStore;

pub struct AppState {
    pub config: Config,
    /// One client for discovery, token exchange and userinfo. Redirects are not followed.
    pub http: reqwest::Client,
    /// Set once by the discovery task. Empty means `/auth/*` answers 503.
    pub oidc: tokio::sync::OnceCell<OidcClient>,
    pub scope: Scope,
    pub prefs: Prefs,
    /// Active Work list. The poller writes it; `/v1/work*` only reads it.
    pub work: Arc<tokio::sync::RwLock<work::WorkCache>>,
    /// Registry steps for roles and run steppers. Empty when `CONSOLE_WORKFLOW_FILE` is unset.
    pub workflow: Vec<work::Step>,
    /// NetBox and Proxmox reads. The refresh task writes it; `/v1/tenants/{t}/inventory`,
    /// `/services` and `/v1/network` only read it.
    pub inventory: tokio::sync::RwLock<inventory::InventoryCache>,
    /// Client for Proxmox: the shared settings plus `CONSOLE_PROXMOX_CA_FILE` roots, if set.
    pub proxmox_http: reqwest::Client,
    /// The data-flow catalogue `GET /v1/flows` answers.
    pub flows: inventory::FlowSet,
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

    let http = client_with_roots(config.extra_ca_file.as_deref(), "CONSOLE_EXTRA_CA_FILE")?;
    let proxmox_http = client_with_roots(
        config
            .proxmox
            .as_ref()
            .and_then(|proxmox| proxmox.ca_file.as_deref()),
        "CONSOLE_PROXMOX_CA_FILE",
    )?;

    let workflow = match &config.workflow_file {
        Some(path) => work::load_workflow(path)?,
        None => Vec::new(),
    };
    let flows = match &config.flows_file {
        Some(path) => inventory::FlowSet::configured(inventory::load_flows(path)?),
        None => inventory::FlowSet::unconfigured(),
    };
    let inventory_cache =
        inventory::InventoryCache::new(config.netbox.is_some(), config.proxmox.is_some());
    let work_cache = if config.semaphore.is_some() {
        work::WorkCache::awaiting_first_poll()
    } else {
        work::WorkCache::unconfigured()
    };

    Ok(Arc::new(AppState {
        config,
        http,
        oidc: tokio::sync::OnceCell::new(),
        scope,
        prefs,
        work: Arc::new(tokio::sync::RwLock::new(work_cache)),
        workflow,
        inventory: tokio::sync::RwLock::new(inventory_cache),
        proxmox_http,
        flows,
    }))
}

/// The client every upstream call shares: no redirects, a 20 s timeout, and the roots of a
/// PEM bundle when `extra_ca` is set. Verification stays on in every case. `var` names the
/// setting in an error.
fn client_with_roots(extra_ca: Option<&Path>, var: &str) -> Result<reqwest::Client, String> {
    let mut builder = reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(Duration::from_secs(20));
    if let Some(path) = extra_ca {
        let pem = fs::read(path)
            .map_err(|err| format!("{var}: cannot read {}: {err}", path.display()))?;
        let certs = reqwest::Certificate::from_pem_bundle(&pem)
            .map_err(|err| format!("{var}: not a PEM bundle: {err}"))?;
        if certs.is_empty() {
            return Err(format!("{var}: no certificates found"));
        }
        for cert in certs {
            builder = builder.add_root_certificate(cert);
        }
    }
    builder
        .build()
        .map_err(|err| format!("cannot build the HTTP client: {err}"))
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
/// Sessions live in the preferences database (`sessions.rs`), so a restart keeps them.
pub fn session_layer(store: SqliteStore) -> SessionManagerLayer<SqliteStore> {
    SessionManagerLayer::new(store)
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
        .route("/v1/work", get(work::list_work))
        .route("/v1/work/{key}", get(work::get_work_item))
        .route(
            "/v1/tenants/{tenant}/inventory",
            get(inventory::get_inventory),
        )
        .route(
            "/v1/tenants/{tenant}/services",
            get(inventory::list_services),
        )
        .route(
            "/v1/tenants/{tenant}/services/{name}",
            get(inventory::get_service),
        )
        .route("/v1/network", get(inventory::get_network))
        .route("/v1/flows", get(inventory::get_flows))
        .route("/v1/{*rest}", any(api::v1_unmatched))
        .with_state(state)
}

/// Everything the process serves: the Dioxus app, the API, and the session
/// layer over both. The layer is applied last so every route sees it.
pub fn router(state: SharedState) -> Router {
    let sessions = session_layer(state.prefs.session_store());
    dioxus::server::router(ui::app)
        .merge(api_router(state))
        .layer(sessions)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::probe::{http_get_raw, parse_status_line};
    use std::io::{Read, Write};
    use std::net::{SocketAddr, TcpStream};
    use std::time::Duration;

    /// `acme` is visible to group `ops`. `initech` is visible to `finance` only, so an
    /// `ops` member must not see it. Neither lists `platform-admins`, so admins see both.
    const SCOPE: &str = r#"{
        "example": true,
        "tenants": [
            {"id": "acme", "name": "Acme", "groups": ["ops"], "orgs": [
                {"id": "ops", "name": "Ops", "sites": [{"id": "dc1", "name": "DC 1", "envs": ["prod"]}]}
            ]},
            {"id": "initech", "name": "Initech", "groups": ["finance"], "orgs": [
                {"id": "lab", "name": "Lab", "sites": [{"id": "hq", "name": "HQ", "envs": ["dev"]}]}
            ]}
        ]
    }"#;

    /// The OIDC cell stays empty, so /auth/* answers 503 in these tests.
    fn test_state() -> SharedState {
        state_with_prefs(Prefs::in_memory().expect("in-memory prefs"))
    }

    fn state_with_prefs(prefs: Prefs) -> SharedState {
        app_state(prefs, work::WorkCache::unconfigured(), Vec::new())
    }

    fn app_state(prefs: Prefs, work: work::WorkCache, workflow: Vec<work::Step>) -> SharedState {
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
                flows_file: None,
                grafana_url: None,
            },
            http: reqwest::Client::new(),
            oidc: tokio::sync::OnceCell::new(),
            scope: Scope::parse(SCOPE).expect("test scope"),
            prefs,
            work: Arc::new(tokio::sync::RwLock::new(work)),
            workflow,
            inventory: tokio::sync::RwLock::new(inventory::InventoryCache::new(false, false)),
            proxmox_http: reqwest::Client::new(),
            flows: inventory::FlowSet::unconfigured(),
        })
    }

    /// A session store over a throwaway in-memory database. Tests that do not
    /// reopen a file use it; the production store shares the state's prefs connection.
    fn memory_sessions() -> SqliteStore {
        Prefs::in_memory()
            .expect("in-memory sessions")
            .session_store()
    }

    /// Test-only route that signs a user in with `groups`, so authenticated paths
    /// run without an identity provider.
    fn test_login_route(path: &'static str, groups: &'static [&'static str]) -> Router {
        use dioxus::server::axum::routing::get;
        use tower_sessions::Session;
        Router::new().route(
            path,
            get(move |session: Session| async move {
                let user = auth::CurrentUser {
                    sub: "user-1".into(),
                    email: Some("ada@example.test".into()),
                    name: Some("Ada".into()),
                    username: Some("ada".into()),
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
        let addr = serve(api_router(test_state()).layer(session_layer(memory_sessions()))).await;
        for path in [
            "/v1/me",
            "/v1/tenants",
            "/v1/tenants/acme/tree",
            "/v1/tenants/globex/tree",
            "/v1/work",
            "/v1/work/sem-1",
            "/v1/tenants/acme/inventory",
            "/v1/tenants/acme/services",
            "/v1/tenants/acme/services/tududi",
            "/v1/network",
            "/v1/flows",
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
        let addr = serve(api_router(test_state()).layer(session_layer(memory_sessions()))).await;
        let reply = call(addr, "GET", "/healthz", None, "").await;
        assert_eq!(reply.status, 200);
        assert_eq!(reply.body, "ok");
    }

    #[tokio::test]
    async fn auth_login_is_503_until_discovery_is_ready() {
        let addr = serve(api_router(test_state()).layer(session_layer(memory_sessions()))).await;
        let reply = call(addr, "GET", "/auth/login", None, "").await;
        assert_eq!(reply.status, 503);
        assert_eq!(reply.body, r#"{"error":"oidc_unavailable"}"#);
    }

    #[tokio::test]
    async fn signed_in_api_round_trip() {
        let app = api_router(test_state())
            .merge(test_login_route("/test/login", &["ops"]))
            .layer(session_layer(memory_sessions()));
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

    /// Serves the API with a session for a user in `groups`, and returns the
    /// address and that session's cookie.
    async fn signed_in(groups: &'static [&'static str]) -> (SocketAddr, String) {
        let app = api_router(test_state())
            .merge(test_login_route("/test/login", groups))
            .layer(session_layer(memory_sessions()));
        let addr = serve(app).await;
        let login = call(addr, "GET", "/test/login", None, "").await;
        let cookie = login.cookie.expect("session cookie set on login");
        (addr, cookie)
    }

    #[tokio::test]
    async fn member_sees_only_the_tenants_naming_their_group() {
        let (addr, cookie) = signed_in(&["ops"]).await;

        let list = call(addr, "GET", "/v1/tenants", Some(cookie.clone()), "").await;
        assert_eq!(list.status, 200);
        assert_eq!(
            list.body,
            r#"{"example":true,"tenants":[{"id":"acme","name":"Acme"}]}"#
        );

        let own = call(
            addr,
            "GET",
            "/v1/tenants/acme/tree",
            Some(cookie.clone()),
            "",
        )
        .await;
        assert_eq!(own.status, 200);
        assert!(
            !own.body.contains("groups"),
            "group lists leaked: {}",
            own.body
        );

        // initech exists, but this caller holds none of its groups: it must answer
        // exactly as a tenant that does not exist, byte for byte.
        let hidden = call(
            addr,
            "GET",
            "/v1/tenants/initech/tree",
            Some(cookie.clone()),
            "",
        )
        .await;
        let unknown = call(addr, "GET", "/v1/tenants/globex/tree", Some(cookie), "").await;
        assert_eq!(hidden.status, 404);
        assert_eq!(hidden.body, r#"{"error":"unknown_tenant"}"#);
        assert_eq!(hidden.body, unknown.body);
    }

    #[tokio::test]
    async fn platform_admin_sees_every_tenant() {
        let (addr, cookie) = signed_in(&["platform-admins"]).await;

        let list = call(addr, "GET", "/v1/tenants", Some(cookie.clone()), "").await;
        assert_eq!(
            list.body,
            r#"{"example":true,"tenants":[{"id":"acme","name":"Acme"},{"id":"initech","name":"Initech"}]}"#
        );

        let tree = call(addr, "GET", "/v1/tenants/initech/tree", Some(cookie), "").await;
        assert_eq!(tree.status, 200);
    }

    #[tokio::test]
    async fn near_miss_group_name_gets_no_tenant() {
        let (addr, cookie) = signed_in(&["platform-admins-old", "users"]).await;

        let list = call(addr, "GET", "/v1/tenants", Some(cookie.clone()), "").await;
        assert_eq!(list.status, 200);
        assert_eq!(list.body, r#"{"example":true,"tenants":[]}"#);

        let tree = call(addr, "GET", "/v1/tenants/acme/tree", Some(cookie), "").await;
        assert_eq!(tree.status, 404);
        assert_eq!(tree.body, r#"{"error":"unknown_tenant"}"#);
    }

    #[tokio::test]
    async fn session_survives_a_restart_over_the_same_database() {
        let path = std::env::temp_dir().join(format!("console-restart-{}.db", std::process::id()));
        let _ = std::fs::remove_file(&path);

        // Sign in through a server built on the database file.
        let first = state_with_prefs(Prefs::open(&path).expect("open"));
        let app = api_router(first.clone())
            .merge(test_login_route("/test/login", &["ops"]))
            .layer(session_layer(first.prefs.session_store()));
        let addr = serve(app).await;
        let login = call(addr, "GET", "/test/login", None, "").await;
        let cookie = login.cookie.expect("session cookie set on login");

        // A new connection and a new store over the same file, as after a restart.
        // The route has no login step, so a 200 can only come from the stored session.
        let second = state_with_prefs(Prefs::open(&path).expect("reopen"));
        let app = api_router(second.clone()).layer(session_layer(second.prefs.session_store()));
        let addr = serve(app).await;
        let me = call(addr, "GET", "/v1/me", Some(cookie), "").await;
        let _ = std::fs::remove_file(&path);

        assert_eq!(me.status, 200, "{}", me.body);
        let me_json: serde_json::Value = serde_json::from_str(&me.body).expect("json");
        assert_eq!(me_json["claims"]["sub"], "user-1");
    }

    /// Active Work fixture: a skynet run (steps a and b), a human task, and a scheduled
    /// repeat (23) of task 21's job, so `sem-21` and `sem-23` share one stream. Task 20's
    /// environment carries a secret-shaped key, which must never reach a client.
    const WORK_TASKS: &str = r#"[
        {"id": 20, "status": "success", "created": "2026-10-26T12:00:00Z", "template_id": 1,
         "message": "skynet run-1 step-a", "tpl_alias": "Plan onboarding", "user_name": "svc-console",
         "environment": "{\"target_service\":\"tududi\",\"SECRET_TOKEN\":\"hunter2\"}"},
        {"id": 21, "status": "running", "created": "2026-10-26T12:01:00Z", "template_id": 2,
         "message": "skynet run-1 step-b", "tpl_alias": "Deploy tududi", "user_name": null,
         "environment": "{\"target_service\":\"tududi\"}"},
        {"id": 22, "status": "error", "created": "2026-10-26T12:02:00Z", "template_id": 3,
         "message": null, "tpl_alias": "Check Secrets", "user_name": "stray", "environment": null},
        {"id": 23, "status": "error", "created": "2026-10-26T12:03:00Z", "template_id": 2,
         "message": null, "tpl_alias": "Deploy tududi", "user_name": null, "schedule_id": 9,
         "environment": "{\"target_service\":\"tududi\"}"}
    ]"#;

    const WORKFLOW: &str = r#"[
        {"id": "step-a", "order": 1, "owner": "infra-agent", "label": "Plan"},
        {"id": "step-b", "order": 2, "owner": "service-agent", "label": "Build"},
        {"id": "step-c", "order": 3, "owner": "security-agent", "label": "Verify"}
    ]"#;

    /// A state whose Active Work cache is already filled from the fixture, as after one poll.
    fn work_state() -> SharedState {
        let tasks: Vec<semaphore::Task> = serde_json::from_str(WORK_TASKS).expect("tasks");
        let steps: Vec<work::Step> = serde_json::from_str(WORKFLOW).expect("steps");
        let config = config::SemaphoreConfig {
            api_url: "https://api.test".into(),
            ui_url: "https://semaphore.test:8443".into(),
            project: 1,
            token: config::Secret::new("test-token"),
        };
        let mut cache = work::WorkCache::awaiting_first_poll();
        cache.record_ok(
            work::build_items(tasks, &steps, &config),
            "2026-10-26T12:02:05Z".into(),
        );
        app_state(Prefs::in_memory().expect("in-memory prefs"), cache, steps)
    }

    /// Serves the API over `state`, with a session for a user in `groups`.
    async fn signed_in_on(
        state: SharedState,
        groups: &'static [&'static str],
    ) -> (SocketAddr, String) {
        let app = api_router(state)
            .merge(test_login_route("/test/login", groups))
            .layer(session_layer(memory_sessions()));
        let addr = serve(app).await;
        let login = call(addr, "GET", "/test/login", None, "").await;
        (addr, login.cookie.expect("session cookie set on login"))
    }

    #[tokio::test]
    async fn work_routes_answer_403_to_everyone_but_platform_admins() {
        let (addr, cookie) = signed_in_on(work_state(), &["ops"]).await;
        for path in ["/v1/work", "/v1/work/sem-21", "/v1/work/sem-999"] {
            let reply = call(addr, "GET", path, Some(cookie.clone()), "").await;
            assert_eq!(reply.status, 403, "{path}");
            assert_eq!(reply.body, r#"{"error":"forbidden"}"#, "{path}");
        }

        // The gate runs before the lookup: a real key and a missing key answer alike.
        let (addr, cookie) = signed_in_on(work_state(), &["platform-admins-old", "users"]).await;
        let real = call(addr, "GET", "/v1/work/sem-21", Some(cookie.clone()), "").await;
        let missing = call(addr, "GET", "/v1/work/sem-999", Some(cookie), "").await;
        assert_eq!(real.status, 403);
        assert_eq!(real.body, missing.body);
    }

    #[tokio::test]
    async fn admin_sees_an_unconfigured_work_list_as_empty() {
        let (addr, cookie) = signed_in_on(test_state(), &["platform-admins"]).await;
        let reply = call(addr, "GET", "/v1/work", Some(cookie), "").await;
        assert_eq!(reply.status, 200);
        let body: serde_json::Value = serde_json::from_str(&reply.body).expect("json");
        assert_eq!(
            body,
            serde_json::json!({
                "source": "unconfigured",
                "error": null,
                "updated_at": null,
                "items": []
            })
        );
    }

    #[tokio::test]
    async fn admin_gets_the_list_newest_first_and_one_item_with_its_run() {
        let (addr, cookie) = signed_in_on(work_state(), &["platform-admins"]).await;

        let list = call(addr, "GET", "/v1/work", Some(cookie.clone()), "").await;
        assert_eq!(list.status, 200);
        assert!(!list.body.contains("hunter2"), "{}", list.body);
        assert!(!list.body.contains("environment"), "{}", list.body);
        let body: serde_json::Value = serde_json::from_str(&list.body).expect("json");
        assert_eq!(body["source"], "ok");
        assert_eq!(body["updated_at"], "2026-10-26T12:02:05Z");
        let keys: Vec<&str> = body["items"]
            .as_array()
            .expect("items")
            .iter()
            .map(|item| item["key"].as_str().expect("key"))
            .collect();
        assert_eq!(keys, ["sem-23", "sem-22", "sem-21", "sem-20"]);

        // Streams: sem-23 is the newest run of sem-21's job; sem-21 is an earlier run.
        let items = body["items"].as_array().expect("items");
        let stream_of = |key: &str| {
            items
                .iter()
                .find(|item| item["key"] == key)
                .expect("item")
                .clone()
        };
        assert_eq!(stream_of("sem-23")["stream"], "tpl-2:tududi");
        assert_eq!(stream_of("sem-23")["latest_in_stream"], true);
        assert_eq!(stream_of("sem-23")["stream_runs"], 2);
        assert_eq!(stream_of("sem-23")["launched_by"], "schedule");
        assert_eq!(stream_of("sem-21")["latest_in_stream"], false);
        assert_eq!(stream_of("sem-21")["stream_runs"], 2);
        assert_eq!(stream_of("sem-22")["stream"], "tpl-3");
        assert_eq!(stream_of("sem-22")["latest_in_stream"], true);
        assert_eq!(stream_of("sem-22")["stream_runs"], 1);

        let one = call(addr, "GET", "/v1/work/sem-21", Some(cookie.clone()), "").await;
        assert_eq!(one.status, 200);
        let detail: serde_json::Value = serde_json::from_str(&one.body).expect("json");
        let history: Vec<&str> = detail["stream_history"]
            .as_array()
            .expect("stream_history")
            .iter()
            .map(|entry| entry["key"].as_str().expect("key"))
            .collect();
        assert_eq!(
            history,
            ["sem-23", "sem-21"],
            "newest run first, own run included"
        );
        assert_eq!(detail["stream_history"][0]["lane"], "failed");
        assert_eq!(detail["stream_history"][1]["lane"], "running");
        assert_eq!(detail["item"]["title"], "Deploy tududi — tududi");
        assert_eq!(detail["item"]["lane"], "running");
        assert_eq!(detail["item"]["role"], "service-agent");
        assert_eq!(detail["item"]["launched_by"], "skynet");
        assert_eq!(
            detail["item"]["receipt_url"],
            "https://semaphore.test:8443/project/1/history?t=21"
        );
        assert_eq!(detail["run"]["run_id"], "run-1");
        assert_eq!(detail["run"]["service"], "tududi");
        let steps = detail["run"]["steps"].as_array().expect("steps");
        assert_eq!(steps[0]["id"], "step-a");
        assert_eq!(steps[0]["status"], "done");
        assert_eq!(steps[0]["key"], "sem-20");
        assert_eq!(steps[1]["status"], "running");
        assert_eq!(steps[1]["key"], "sem-21");
        assert_eq!(steps[2]["status"], "not_started");
        assert!(steps[2]["key"].is_null());

        // A human task has no run.
        let human = call(addr, "GET", "/v1/work/sem-22", Some(cookie.clone()), "").await;
        let human: serde_json::Value = serde_json::from_str(&human.body).expect("json");
        assert!(human["run"].is_null());
        assert_eq!(human["item"]["launched_by"], "stray");

        let missing = call(addr, "GET", "/v1/work/sem-999", Some(cookie), "").await;
        assert_eq!(missing.status, 404);
        assert_eq!(missing.body, r#"{"error":"unknown_work"}"#);
    }

    #[tokio::test]
    async fn probe_helper_reads_live_healthz() {
        let addr = serve(api_router(test_state()).layer(session_layer(memory_sessions()))).await;
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
    #[test]
    fn a_bad_ca_file_fails_startup_by_its_setting_name() {
        let missing = client_with_roots(
            Some(Path::new("/nonexistent/console-pve-ca.pem")),
            "CONSOLE_PROXMOX_CA_FILE",
        )
        .expect_err("must fail");
        assert!(
            missing.starts_with("CONSOLE_PROXMOX_CA_FILE: cannot read"),
            "{missing}"
        );

        let path = std::env::temp_dir().join(format!("console-pve-ca-{}.pem", std::process::id()));
        std::fs::write(&path, "not a certificate\n").expect("write");
        let bad = client_with_roots(Some(&path), "CONSOLE_PROXMOX_CA_FILE").expect_err("must fail");
        let _ = std::fs::remove_file(&path);
        assert!(bad.starts_with("CONSOLE_PROXMOX_CA_FILE:"), "{bad}");
    }

    #[test]
    fn no_upstream_client_turns_certificate_verification_off() {
        // The needle is split so this file does not contain it verbatim.
        let needle = concat!("danger_", "accept_");
        for (file, text) in [
            ("mod.rs", include_str!("mod.rs")),
            ("inventory.rs", include_str!("inventory.rs")),
            ("netbox.rs", include_str!("netbox.rs")),
            ("proxmox.rs", include_str!("proxmox.rs")),
            ("semaphore.rs", include_str!("semaphore.rs")),
            ("auth.rs", include_str!("auth.rs")),
            ("work.rs", include_str!("work.rs")),
        ] {
            assert!(!text.contains(needle), "{file} turns verification off");
        }
    }
}
