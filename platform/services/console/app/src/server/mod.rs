//! Server core: state, session layer, router and the background OIDC discovery.

pub mod access;
pub mod api;
pub mod auth;
pub mod catalog;
pub mod config;
pub mod delivery;
pub mod gate;
pub mod gateway;
pub mod github;
pub mod grafana;
pub mod inventory;
pub mod netbox;
pub mod openbao;
pub mod policies;
pub mod prefs;
pub mod proposals;
pub mod proxmox;
pub mod scope;
pub mod secrets;
pub mod semaphore;
pub mod sessions;
pub mod telemetry;
pub mod work;

use std::fs;
use std::path::Path;
use std::sync::Arc;
use std::time::{Duration, Instant};

use dioxus::server::axum::extract::{Request, State};
use dioxus::server::axum::http::{header, HeaderMap, Method, StatusCode};
use dioxus::server::axum::middleware::{self, Next};
use dioxus::server::axum::response::Response;
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
    /// The agentgateway clients, budgets and models `GET /v1/gateway` answers (task 8.3a).
    pub gateway: gateway::GatewaySet,
    /// The deployable services `GET /v1/catalog` answers (task 8.4, D38). Members get names; admins also get host counts.
    pub catalog: catalog::CatalogSet,
    /// The gateway's last-hour token use per client (D39). The refresh task writes it; `/v1/gateway` only reads it.
    pub usage: tokio::sync::RwLock<grafana::UsageCache>,
    /// OpenBao client for run-time reads (plan 17 D27). Logs in on first use, never at boot.
    /// `None` when `CONSOLE_OPENBAO_*` is unset.
    pub openbao: Option<openbao::Openbao>,
    /// The OpenBao metadata walk for the Secrets page (plan 17 D35). The refresh task writes it;
    /// `/v1/secrets` only reads it.
    pub secrets: tokio::sync::RwLock<secrets::SecretsCache>,
    /// The Authentik directory for the Access page (plan 17, 8.2b). The refresh task writes it;
    /// `/v1/access` only reads it.
    pub access: tokio::sync::RwLock<access::AccessCache>,
    /// The Access Level -> per-service role map, read once at startup. Empty when `CONSOLE_ROLE_MAP_FILE` is unset.
    pub role_map: access::RoleMap,
    /// The OPA and OpenBao policy files for the Policy page (plan 17, 8.4, D36). The refresh task writes it;
    /// `/v1/policies` only reads it.
    pub policies: tokio::sync::RwLock<policies::PoliciesCache>,
    /// Required checks, the promotion path and open pull requests for the CI/CD page (plan 17, 9.1, D42). The
    /// refresh task writes it; `/v1/delivery` only reads it.
    pub delivery: tokio::sync::RwLock<delivery::DeliveryCache>,
    /// Target health and Grafana alerts by environment, cluster and service for the Telemetry page (plan 17, 9.2,
    /// D43). The refresh task writes it; `/v1/telemetry/rollups` only reads it.
    pub telemetry: tokio::sync::RwLock<telemetry::TelemetryCache>,
    /// The launch-gate client proposals are decided and launched through (D49, D51). `None` when
    /// `CONSOLE_LAUNCH_GATE_*` is unset.
    pub gate: Option<gate::GateClient>,
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
    let gateway = match &config.gateway_file {
        Some(path) => gateway::GatewaySet::configured(gateway::load_gateway(path)?),
        None => gateway::GatewaySet::unconfigured(),
    };
    let catalog = match &config.catalog_file {
        Some(path) => catalog::CatalogSet::configured(catalog::load_catalog(path)?),
        None => catalog::CatalogSet::unconfigured(),
    };
    let usage =
        grafana::UsageCache::new(config.grafana_token.is_some() && config.grafana_url.is_some());
    let openbao = match &config.openbao {
        Some(settings) => Some(
            openbao::Openbao::new(settings.clone())
                .map_err(|err| format!("CONSOLE_OPENBAO_URL: {err}"))?,
        ),
        None => None,
    };
    let secrets_cache = secrets::SecretsCache::new(openbao.is_some());
    let access_cache = access::AccessCache::new(config.authentik.is_some());
    let role_map = match &config.role_map_file {
        Some(path) => access::load_role_map(path)?,
        None => access::RoleMap::empty(),
    };
    let policies_cache = policies::PoliciesCache::new(openbao.is_some() && config.github.is_some());
    let delivery_cache = delivery::DeliveryCache::new(openbao.is_some() && config.github.is_some());
    let telemetry_cache = telemetry::TelemetryCache::new(
        config.grafana_token.is_some() && config.grafana_url.is_some(),
    );
    let gate = config
        .launch_gate
        .as_ref()
        .map(|settings| gate::GateClient::new(&settings.url, settings.token.clone(), http.clone()));
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
        gateway,
        catalog,
        usage: tokio::sync::RwLock::new(usage),
        openbao,
        secrets: tokio::sync::RwLock::new(secrets_cache),
        access: tokio::sync::RwLock::new(access_cache),
        role_map,
        policies: tokio::sync::RwLock::new(policies_cache),
        delivery: tokio::sync::RwLock::new(delivery_cache),
        telemetry: tokio::sync::RwLock::new(telemetry_cache),
        gate,
    }))
}

/// The client every upstream call shares: no redirects, a 20 s timeout, and the roots of a
/// PEM bundle when `extra_ca` is set. Verification stays on in every case. `var` names the
/// setting in an error.
pub(crate) fn client_with_roots(
    extra_ca: Option<&Path>,
    var: &str,
) -> Result<reqwest::Client, String> {
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
        .route("/v1/gateway", get(gateway::get_gateway))
        .route("/v1/catalog", get(catalog::get_catalog))
        .route("/v1/roles", get(work::list_roles))
        .route("/v1/secrets", get(secrets::get_secrets))
        .route("/v1/access", get(access::get_access))
        .route("/v1/policies", get(policies::get_policies))
        .route("/v1/delivery", get(delivery::get_delivery))
        .route("/v1/telemetry/rollups", get(telemetry::get_telemetry))
        .route(
            "/v1/proposals",
            get(proposals::list_proposals).post(proposals::create_proposal),
        )
        .route(
            "/v1/proposals/{id}",
            get(proposals::get_proposal).put(proposals::edit_proposal),
        )
        .route("/v1/proposals/{id}/comments", post(proposals::add_comment))
        .route(
            "/v1/proposals/{id}/approve",
            post(proposals::approve_proposal),
        )
        .route("/v1/proposals/{id}/deny", post(proposals::deny_proposal))
        .route("/v1/{*rest}", any(api::v1_unmatched))
        .layer(middleware::from_fn_with_state(
            state.clone(),
            require_console_origin,
        ))
        .with_state(state)
}

/// D53: a state-changing request under `/v1/` is refused 403 `origin_refused` unless its `Origin` is the console's own
/// base URL and its body is JSON. The session cookie is SameSite=Lax, which another `uhstray.io` subdomain passes, so
/// this check carries the protection. GET and HEAD, and every path outside `/v1/` (the `/auth/*` routes), pass through.
async fn require_console_origin(
    State(app): State<SharedState>,
    request: Request,
    next: Next,
) -> Response {
    let changes_state =
        [Method::POST, Method::PUT, Method::PATCH, Method::DELETE].contains(request.method());
    if changes_state
        && request.uri().path().starts_with("/v1/")
        && !same_origin_json(request.headers(), &app.config.base_url)
    {
        return api::error(StatusCode::FORBIDDEN, "origin_refused");
    }
    next.run(request).await
}

/// One `Origin` value, exactly `base_url`, and a `Content-Type` whose media type is `application/json`. The media
/// type is the value before any `;`, so `application/json; charset=utf-8` passes and `application/jsonx` does not.
fn same_origin_json(headers: &HeaderMap, base_url: &str) -> bool {
    let mut origins = headers.get_all(header::ORIGIN).iter();
    let origin_ok = origins.next().and_then(|value| value.to_str().ok()) == Some(base_url)
        && origins.next().is_none();
    let json = headers
        .get(header::CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.split(';').next())
        .is_some_and(|media| media.trim().eq_ignore_ascii_case("application/json"));
    origin_ok && json
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
mod load_test;

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
        app_state(
            prefs,
            work::WorkCache::unconfigured(),
            Vec::new(),
            gateway::GatewaySet::unconfigured(),
            catalog::CatalogSet::unconfigured(),
            grafana::UsageCache::new(false),
        )
    }

    fn app_state(
        prefs: Prefs,
        work: work::WorkCache,
        workflow: Vec<work::Step>,
        gateway_set: gateway::GatewaySet,
        catalog_set: catalog::CatalogSet,
        usage: grafana::UsageCache,
    ) -> SharedState {
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
                grafana_url: None,
                grafana_token: None,
                authentik: None,
                role_map_file: None,
                github: None,
                launch_gate: None,
                launch_dev_variants: true,
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
            gateway: gateway_set,
            catalog: catalog_set,
            usage: tokio::sync::RwLock::new(usage),
            openbao: None,
            secrets: tokio::sync::RwLock::new(secrets::SecretsCache::new(false)),
            access: tokio::sync::RwLock::new(access::AccessCache::new(false)),
            role_map: access::RoleMap::empty(),
            policies: tokio::sync::RwLock::new(policies::PoliciesCache::new(false)),
            delivery: tokio::sync::RwLock::new(delivery::DeliveryCache::new(false)),
            telemetry: tokio::sync::RwLock::new(telemetry::TelemetryCache::new(false)),
            gate: None,
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

    /// The console's own origin, as the test AppState names it in `base_url`.
    const CONSOLE_ORIGIN: &str = "https://console.test";

    /// One raw HTTP/1.1 request from the console's own origin with a JSON body. `cookie` is sent as-is.
    fn raw_request(
        addr: SocketAddr,
        method: &str,
        path: &str,
        cookie: Option<&str>,
        body: &str,
    ) -> Reply {
        raw_request_with(
            addr,
            method,
            path,
            cookie,
            &[
                ("Origin", CONSOLE_ORIGIN),
                ("Content-Type", "application/json"),
            ],
            body,
        )
    }

    /// One raw HTTP/1.1 request carrying exactly `headers`, beyond Host, Connection, Cookie and Content-Length.
    fn raw_request_with(
        addr: SocketAddr,
        method: &str,
        path: &str,
        cookie: Option<&str>,
        headers: &[(&str, &str)],
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
        let header_lines: String = headers
            .iter()
            .map(|(name, value)| format!("{name}: {value}\r\n"))
            .collect();
        let request = format!(
            "{method} {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n{cookie_line}{header_lines}Content-Length: {}\r\n\r\n{body}",
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

    /// `call` with exactly `headers`, for the origin tests.
    async fn call_with(
        addr: SocketAddr,
        method: &'static str,
        path: &'static str,
        cookie: Option<String>,
        headers: &'static [(&'static str, &'static str)],
        body: &'static str,
    ) -> Reply {
        tokio::task::spawn_blocking(move || {
            raw_request_with(addr, method, path, cookie.as_deref(), headers, body)
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
            "/v1/gateway",
            "/v1/catalog",
            "/v1/roles",
            "/v1/secrets",
            "/v1/policies",
            "/v1/delivery",
            "/v1/telemetry/rollups",
            "/v1/proposals",
            "/v1/proposals/p1",
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

    fn gateway_state(gateway_set: gateway::GatewaySet) -> SharedState {
        gateway_state_with_usage(gateway_set, grafana::UsageCache::new(false))
    }

    fn gateway_state_with_usage(
        gateway_set: gateway::GatewaySet,
        usage: grafana::UsageCache,
    ) -> SharedState {
        app_state(
            Prefs::in_memory().expect("in-memory prefs"),
            work::WorkCache::unconfigured(),
            Vec::new(),
            gateway_set,
            catalog::CatalogSet::unconfigured(),
            usage,
        )
    }

    /// Signs in with `groups` on a fresh app over `state`, then asks for `/v1/gateway`.
    async fn gateway_reply(state: SharedState, groups: &'static [&'static str]) -> Reply {
        let app = api_router(state)
            .merge(test_login_route("/test/login", groups))
            .layer(session_layer(memory_sessions()));
        let addr = serve(app).await;
        let login = call(addr, "GET", "/test/login", None, "").await;
        let cookie = login.cookie.clone().expect("session cookie set on login");
        call(addr, "GET", "/v1/gateway", Some(cookie), "").await
    }

    #[tokio::test]
    async fn gateway_answers_platform_admins_only_with_the_rendered_file() {
        let file = gateway::GatewayFile {
            clients: vec![gateway::GatewayClient {
                name: "alpha".into(),
                tier: None,
                tokens_per_hour: 2000000,
                budget_source: gateway::BudgetSource::Default,
                allowed_models: vec!["m1".into()],
            }],
            models: vec!["m1".into()],
        };
        let state = gateway_state(gateway::GatewaySet::configured(file));

        let member = gateway_reply(state.clone(), &["ops"]).await;
        assert_eq!(member.status, 403);
        assert_eq!(member.body, r#"{"error":"forbidden"}"#);

        let admin = gateway_reply(state, &["platform-admins"]).await;
        assert_eq!(admin.status, 200);
        assert_eq!(
            admin.body,
            r#"{"state":"ok","clients":[{"name":"alpha","tier":null,"tokens_per_hour":2000000,"budget_source":"default","allowed_models":["m1"],"tokens_last_hour":null}],"models":["m1"],"usage":{"state":"unconfigured","refreshed_at":null,"error":null}}"#
        );
    }

    #[tokio::test]
    async fn gateway_answers_unconfigured_when_no_file_is_set() {
        let state = gateway_state(gateway::GatewaySet::unconfigured());
        let admin = gateway_reply(state, &["platform-admins"]).await;
        assert_eq!(admin.status, 200);
        assert_eq!(
            admin.body,
            r#"{"state":"unconfigured","clients":[],"models":[],"usage":{"state":"unconfigured","refreshed_at":null,"error":null}}"#
        );
    }

    #[tokio::test]
    async fn gateway_shows_each_clients_last_hour_from_the_usage_read_by_name() {
        let file = gateway::GatewayFile {
            clients: vec![
                gateway::GatewayClient {
                    name: "alpha".into(),
                    tier: None,
                    tokens_per_hour: 2000000,
                    budget_source: gateway::BudgetSource::Default,
                    allowed_models: vec!["m1".into()],
                },
                gateway::GatewayClient {
                    name: "beta".into(),
                    tier: None,
                    tokens_per_hour: 500,
                    budget_source: gateway::BudgetSource::Default,
                    allowed_models: vec![],
                },
            ],
            models: vec!["m1".into()],
        };
        let mut usage = grafana::UsageCache::new(true);
        usage.record_ok(
            std::collections::HashMap::from([("alpha".to_string(), 1234)]),
            "2026-11-04T10:00:00Z".into(),
        );
        let state = gateway_state_with_usage(gateway::GatewaySet::configured(file), usage);
        let admin = gateway_reply(state, &["platform-admins"]).await;
        assert_eq!(admin.status, 200);
        let body: serde_json::Value = serde_json::from_str(&admin.body).expect("json");
        assert_eq!(body["clients"][0]["name"], "alpha");
        assert_eq!(body["clients"][0]["tokens_last_hour"], 1234);
        assert!(body["clients"][1]["tokens_last_hour"].is_null());
        assert_eq!(body["usage"]["state"], "ok");
        assert_eq!(body["usage"]["refreshed_at"], "2026-11-04T10:00:00Z");
        assert!(body["usage"]["error"].is_null());
    }

    #[tokio::test]
    async fn roles_answer_platform_admins_only_with_each_owner_and_its_steps() {
        let (addr, cookie) = signed_in_on(work_state(), &["ops"]).await;
        let member = call(addr, "GET", "/v1/roles", Some(cookie), "").await;
        assert_eq!(member.status, 403);
        assert_eq!(member.body, r#"{"error":"forbidden"}"#);

        let (addr, cookie) = signed_in_on(work_state(), &["platform-admins"]).await;
        let admin = call(addr, "GET", "/v1/roles", Some(cookie), "").await;
        assert_eq!(admin.status, 200);
        assert_eq!(
            admin.body,
            r#"{"roles":[{"name":"infra-agent","steps":[{"id":"step-a","order":1,"label":"Plan"}]},{"name":"security-agent","steps":[{"id":"step-c","order":3,"label":"Verify"}]},{"name":"service-agent","steps":[{"id":"step-b","order":2,"label":"Build"}]}]}"#
        );
    }

    fn catalog_state(catalog_set: catalog::CatalogSet) -> SharedState {
        app_state(
            Prefs::in_memory().expect("in-memory prefs"),
            work::WorkCache::unconfigured(),
            Vec::new(),
            gateway::GatewaySet::unconfigured(),
            catalog_set,
            grafana::UsageCache::new(false),
        )
    }

    /// Signs in with `groups` on a fresh app over `state`, then asks for `/v1/catalog`.
    async fn catalog_reply(state: SharedState, groups: &'static [&'static str]) -> Reply {
        let app = api_router(state)
            .merge(test_login_route("/test/login", groups))
            .layer(session_layer(memory_sessions()));
        let addr = serve(app).await;
        let login = call(addr, "GET", "/test/login", None, "").await;
        let cookie = login.cookie.clone().expect("session cookie set on login");
        call(addr, "GET", "/v1/catalog", Some(cookie), "").await
    }

    fn two_service_catalog() -> catalog::CatalogSet {
        catalog::CatalogSet::configured(catalog::CatalogFile {
            services: vec![
                catalog::CatalogEntry {
                    service: "netbox".into(),
                    name: "NetBox".into(),
                    template: "Deploy NetBox".into(),
                    deployed_hosts: 2,
                },
                catalog::CatalogEntry {
                    service: "step-ca".into(),
                    name: "step-ca".into(),
                    template: "Deploy step-ca (Dev)".into(),
                    deployed_hosts: 0,
                },
            ],
        })
    }

    #[tokio::test]
    async fn catalog_answers_members_without_a_host_count_and_admins_with_it() {
        let state = catalog_state(two_service_catalog());

        let member = catalog_reply(state.clone(), &["ops"]).await;
        assert_eq!(member.status, 200);
        assert_eq!(
            member.body,
            r#"{"state":"ok","services":[{"service":"netbox","name":"NetBox","template":"Deploy NetBox","running":true},{"service":"step-ca","name":"step-ca","template":"Deploy step-ca (Dev)","running":false}]}"#
        );
        assert!(!member.body.contains("deployed_hosts"), "{}", member.body);

        let admin = catalog_reply(state, &["platform-admins"]).await;
        assert_eq!(admin.status, 200);
        assert_eq!(
            admin.body,
            r#"{"state":"ok","services":[{"service":"netbox","name":"NetBox","template":"Deploy NetBox","running":true,"deployed_hosts":2},{"service":"step-ca","name":"step-ca","template":"Deploy step-ca (Dev)","running":false,"deployed_hosts":0}]}"#
        );
    }

    #[tokio::test]
    async fn catalog_answers_unconfigured_to_members_and_admins_alike() {
        let state = catalog_state(catalog::CatalogSet::unconfigured());
        let member = catalog_reply(state.clone(), &["ops"]).await;
        assert_eq!(member.status, 200);
        assert_eq!(member.body, r#"{"state":"unconfigured","services":[]}"#);
        let admin = catalog_reply(state, &["platform-admins"]).await;
        assert_eq!(admin.status, 200);
        assert_eq!(admin.body, r#"{"state":"unconfigured","services":[]}"#);
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

    /// D53: a state-changing `/v1/` request is refused unless its Origin is the console's own and its body is JSON.
    /// A refused write changes nothing; GET is never refused; `/auth/*` sits outside the rule.
    #[tokio::test]
    async fn state_changing_v1_requests_need_the_console_origin_and_a_json_body() {
        let (addr, cookie) = signed_in(&["ops"]).await;
        const BODY: &str = r#"{"preferences":{"theme":"dark"}}"#;
        let refused: [(&str, &'static [(&'static str, &'static str)]); 6] = [
            (
                "cross origin",
                &[
                    ("Origin", "https://evil.test"),
                    ("Content-Type", "application/json"),
                ],
            ),
            ("no origin", &[("Content-Type", "application/json")]),
            (
                "a repeated origin",
                &[
                    ("Origin", CONSOLE_ORIGIN),
                    ("Origin", CONSOLE_ORIGIN),
                    ("Content-Type", "application/json"),
                ],
            ),
            (
                "a text body",
                &[("Origin", CONSOLE_ORIGIN), ("Content-Type", "text/plain")],
            ),
            (
                "a form body",
                &[
                    ("Origin", CONSOLE_ORIGIN),
                    ("Content-Type", "application/x-www-form-urlencoded"),
                ],
            ),
            (
                "a media type that only starts with application/json",
                &[
                    ("Origin", CONSOLE_ORIGIN),
                    ("Content-Type", "application/jsonx"),
                ],
            ),
        ];
        for (name, headers) in refused {
            let reply = call_with(addr, "PUT", "/v1/me", Some(cookie.clone()), headers, BODY).await;
            assert_eq!(reply.status, 403, "{name}");
            assert_eq!(reply.body, r#"{"error":"origin_refused"}"#, "{name}");
        }

        let me = call(addr, "GET", "/v1/me", Some(cookie.clone()), "").await;
        let me_json: serde_json::Value = serde_json::from_str(&me.body).expect("json");
        assert_eq!(
            me_json["preferences"]["theme"], "system",
            "a refused write changed nothing"
        );

        let same_origin = call_with(
            addr,
            "PUT",
            "/v1/me",
            Some(cookie.clone()),
            &[
                ("Origin", CONSOLE_ORIGIN),
                ("Content-Type", "application/json; charset=utf-8"),
            ],
            BODY,
        )
        .await;
        assert_eq!(same_origin.status, 200, "{}", same_origin.body);

        // The catch-all under /v1 is covered too: the refusal comes before the 404 an unknown path would get.
        let unknown = call_with(
            addr,
            "POST",
            "/v1/nope",
            Some(cookie.clone()),
            &[
                ("Origin", "https://evil.test"),
                ("Content-Type", "application/json"),
            ],
            "{}",
        )
        .await;
        assert_eq!(unknown.status, 403);
        assert_eq!(unknown.body, r#"{"error":"origin_refused"}"#);

        let tenants = call_with(
            addr,
            "GET",
            "/v1/tenants",
            Some(cookie.clone()),
            &[("Origin", "https://evil.test")],
            "",
        )
        .await;
        assert_eq!(tenants.status, 200, "GET is never refused");

        let logout = call_with(
            addr,
            "POST",
            "/auth/logout",
            Some(cookie.clone()),
            &[
                ("Origin", "https://evil.test"),
                ("Content-Type", "application/json"),
            ],
            "",
        )
        .await;
        assert_ne!(logout.status, 403, "/auth/* is outside the rule");
        assert_ne!(logout.body, r#"{"error":"origin_refused"}"#);
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
        app_state(
            Prefs::in_memory().expect("in-memory prefs"),
            cache,
            steps,
            gateway::GatewaySet::unconfigured(),
            catalog::CatalogSet::unconfigured(),
            grafana::UsageCache::new(false),
        )
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

    // ---- Proposals (plan 17 D48-D53) -------------------------------------------------------------------------

    /// The proposal routes' scope: `acme` maps its `dc1` site to NetBox `dc1`, and no site maps `other`.
    const PROPOSAL_SCOPE: &str = r#"{
        "tenants": [
            {"id": "acme", "name": "Acme", "groups": ["ops"], "orgs": [
                {"id": "ops", "name": "Ops", "sites": [{"id": "dc1", "name": "DC 1", "envs": ["prod"], "netbox_site": "dc1"}]}
            ]}
        ]
    }"#;

    const PROPOSE_HONCHO: &str = r#"{"kind": "verify", "service": "honcho", "check_mode": false, "note": "before the upgrade"}"#;

    const PROPOSAL_ROUTES: [(&str, &str); 7] = [
        ("GET", "/v1/proposals"),
        ("POST", "/v1/proposals"),
        ("GET", "/v1/proposals/p1"),
        ("PUT", "/v1/proposals/p1"),
        ("POST", "/v1/proposals/p1/comments"),
        ("POST", "/v1/proposals/p1/approve"),
        ("POST", "/v1/proposals/p1/deny"),
    ];

    /// NetBox as the proposal tests need it. `dc1` holds `honcho` (a service) and `plain` (no workflow fields);
    /// `other` holds `elsewhere`, a service in a NetBox site that no scope maps.
    fn proposal_netbox() -> netbox::NetboxData {
        fn vm(name: &str, workflow: bool) -> netbox::NbVm {
            netbox::NbVm {
                name: name.into(),
                status: "active".into(),
                vcpus: None,
                memory_mb: None,
                disk_mb: None,
                primary_ip: None,
                tags: Vec::new(),
                tag_slugs: Vec::new(),
                workflow: workflow.then(|| netbox::Workflow {
                    steps: std::collections::BTreeMap::new(),
                    failed: Vec::new(),
                    last_run: None,
                }),
            }
        }
        let site = |vms: Vec<netbox::NbVm>| netbox::NbSite {
            clusters: vec![netbox::NbCluster {
                name: "prod".into(),
                vms,
            }],
            devices: Vec::new(),
        };
        netbox::NetboxData {
            sites: std::collections::BTreeMap::from([
                (
                    "dc1".to_string(),
                    site(vec![vm("honcho", true), vm("plain", false)]),
                ),
                ("other".to_string(), site(vec![vm("elsewhere", true)])),
            ]),
            ambiguous_vm_names: Default::default(),
            duplicate_cluster_names: Default::default(),
            dropped_mismatched: 0,
        }
    }

    /// The proposal routes over the fixture scope, with `gate_client` as the launch gate (or none) and the NetBox read
    /// `netbox_data` (or none, as before the first read).
    fn proposal_state(
        gate_client: Option<gate::GateClient>,
        netbox_data: Option<netbox::NetboxData>,
    ) -> SharedState {
        let mut app = Arc::try_unwrap(app_state(
            Prefs::in_memory().expect("in-memory prefs"),
            work::WorkCache::unconfigured(),
            Vec::new(),
            gateway::GatewaySet::unconfigured(),
            catalog::CatalogSet::unconfigured(),
            grafana::UsageCache::new(false),
        ))
        .ok()
        .expect("a fresh state is not shared yet");
        app.scope = Scope::parse(PROPOSAL_SCOPE).expect("proposal scope");
        app.gate = gate_client;
        let mut cache = inventory::InventoryCache::new(false, false);
        cache.netbox = inventory::SourceState {
            status: work::Source::Ok,
            error: None,
            updated_at: Some("2026-11-09T10:00:00Z".into()),
            data: netbox_data,
        };
        app.inventory = tokio::sync::RwLock::new(cache);
        Arc::new(app)
    }

    /// A fake launch gate. `decide` and `launch` answer their queued replies in order, and past the queue `decide`
    /// allows and `launch` creates task 4217. Each records the body it received. `launch` waits 300 ms before it
    /// counts nothing and answers, so two approvals can overlap.
    #[derive(Default)]
    struct FakeGate {
        decide_replies: std::sync::Mutex<std::collections::VecDeque<(u16, serde_json::Value)>>,
        launch_replies: std::sync::Mutex<std::collections::VecDeque<(u16, serde_json::Value)>>,
        decide_bodies: std::sync::Mutex<Vec<serde_json::Value>>,
        launch_bodies: std::sync::Mutex<Vec<serde_json::Value>>,
    }

    async fn fake_decide(State(fake): State<Arc<FakeGate>>, body: String) -> (StatusCode, String) {
        fake.decide_bodies
            .lock()
            .expect("bodies")
            .push(serde_json::from_str(&body).unwrap_or(serde_json::Value::Null));
        let queued = fake.decide_replies.lock().expect("replies").pop_front();
        let (status, reply) = queued.unwrap_or((
            200,
            serde_json::json!({"decision": {"allowed": true, "reason": "read-only"}}),
        ));
        (
            StatusCode::from_u16(status).expect("status"),
            reply.to_string(),
        )
    }

    async fn fake_launch(State(fake): State<Arc<FakeGate>>, body: String) -> (StatusCode, String) {
        fake.launch_bodies
            .lock()
            .expect("bodies")
            .push(serde_json::from_str(&body).unwrap_or(serde_json::Value::Null));
        tokio::time::sleep(Duration::from_millis(300)).await;
        let queued = fake.launch_replies.lock().expect("replies").pop_front();
        let (status, reply) = queued.unwrap_or((
            201,
            serde_json::json!({
                "task_id": 4217,
                "message": "console p1 service-validate",
                "decision": {"allowed": true, "reason": "read-only"}
            }),
        ));
        (
            StatusCode::from_u16(status).expect("status"),
            reply.to_string(),
        )
    }

    /// Serves a fake gate and returns its origin, the shape `GateClient::new` takes.
    async fn serve_gate(fake: Arc<FakeGate>) -> String {
        let addr = serve(
            Router::new()
                .route("/v1/decide", post(fake_decide))
                .route("/v1/launch", post(fake_launch))
                .with_state(fake),
        )
        .await;
        format!("http://{addr}")
    }

    fn gate_client(origin: &str) -> gate::GateClient {
        gate::GateClient::new(
            origin,
            config::Secret::new("console-caller-token"),
            client_with_roots(None, "CONSOLE_EXTRA_CA_FILE").expect("client"),
        )
    }

    fn decide_replies(
        fake: &FakeGate,
        replies: impl IntoIterator<Item = (u16, serde_json::Value)>,
    ) {
        fake.decide_replies.lock().expect("replies").extend(replies);
    }

    fn launch_replies(
        fake: &FakeGate,
        replies: impl IntoIterator<Item = (u16, serde_json::Value)>,
    ) {
        fake.launch_replies.lock().expect("replies").extend(replies);
    }

    /// A test-only route that signs `sub` in as a platform admin named `name`, so two admins can share one server.
    fn admin_login_route(path: &'static str, sub: &'static str, name: &'static str) -> Router {
        use tower_sessions::Session;
        Router::new().route(
            path,
            get(move |session: Session| async move {
                let user = auth::CurrentUser {
                    sub: sub.into(),
                    email: None,
                    name: Some(name.into()),
                    username: None,
                    groups: vec!["platform-admins".into()],
                };
                session
                    .insert(auth::KEY_USER, &user)
                    .await
                    .expect("insert user");
                "signed in"
            }),
        )
    }

    /// The proposal routes over `state`, with two platform admins signed in: `user-1` (Ada, who proposes in these
    /// tests) and `user-2` (Grace). Returns the address and each admin's cookie.
    async fn proposal_server(state: SharedState) -> (SocketAddr, String, String) {
        let app = api_router(state)
            .merge(test_login_route("/test/login", &["platform-admins"]))
            .merge(admin_login_route("/test/login-grace", "user-2", "Grace"))
            .layer(session_layer(memory_sessions()));
        let addr = serve(app).await;
        let ada = call(addr, "GET", "/test/login", None, "")
            .await
            .cookie
            .expect("ada's cookie");
        let grace = call(addr, "GET", "/test/login-grace", None, "")
            .await
            .cookie
            .expect("grace's cookie");
        (addr, ada, grace)
    }

    /// `raw_request` with owned arguments, so a test builds its path and body at run time.
    async fn send(
        addr: SocketAddr,
        method: &'static str,
        path: String,
        cookie: Option<String>,
        body: String,
    ) -> Reply {
        tokio::task::spawn_blocking(move || {
            raw_request(addr, method, &path, cookie.as_deref(), &body)
        })
        .await
        .expect("blocking request")
    }

    fn json_of(reply: &Reply) -> serde_json::Value {
        serde_json::from_str(&reply.body).unwrap_or_else(|err| panic!("{err}: {}", reply.body))
    }

    /// The ids of a list answer, in the order it gave them.
    fn ids_of(reply: &Reply) -> Vec<String> {
        json_of(reply)["proposals"]
            .as_array()
            .expect("proposals")
            .iter()
            .map(|p| p["id"].as_str().expect("id").to_string())
            .collect()
    }

    #[tokio::test]
    async fn proposal_routes_answer_401_to_strangers_and_403_to_members() {
        let stranger =
            serve(api_router(proposal_state(None, None)).layer(session_layer(memory_sessions())))
                .await;
        let (member, member_cookie) = signed_in_on(proposal_state(None, None), &["ops"]).await;
        for (method, path) in PROPOSAL_ROUTES {
            let reply = call(stranger, method, path, None, "{}").await;
            assert_eq!(reply.status, 401, "{method} {path}");
            assert_eq!(
                reply.body, r#"{"error":"unauthenticated"}"#,
                "{method} {path}"
            );

            let reply = call(member, method, path, Some(member_cookie.clone()), "{}").await;
            assert_eq!(reply.status, 403, "{method} {path}");
            assert_eq!(reply.body, r#"{"error":"forbidden"}"#, "{method} {path}");
        }
    }

    #[tokio::test]
    async fn create_previews_through_decide_and_stores_only_what_inventory_lists() {
        let fake = Arc::new(FakeGate::default());
        let origin = serve_gate(fake.clone()).await;
        let (addr, ada, _) = proposal_server(proposal_state(
            Some(gate_client(&origin)),
            Some(proposal_netbox()),
        ))
        .await;

        let created = send(
            addr,
            "POST",
            "/v1/proposals".into(),
            Some(ada.clone()),
            PROPOSE_HONCHO.into(),
        )
        .await;
        assert_eq!(created.status, 201, "{}", created.body);
        let body = json_of(&created);
        assert_eq!(body["id"], "p1");
        assert_eq!(body["kind"], "verify");
        assert_eq!(body["service"], "honcho");
        assert_eq!(body["template"], "Verify Service Health (Dev)");
        assert_eq!(body["agent"], "service-agent");
        assert_eq!(body["step"], "service-validate");
        assert_eq!(
            body["survey"],
            serde_json::json!({"target_service": "honcho_svc"})
        );
        assert_eq!(body["state"], "awaiting");
        assert_eq!(body["preview"]["state"], "allowed");
        assert_eq!(body["preview"]["reason"], "read-only");
        assert!(body["decision"].is_null());
        assert!(body["launch"].is_null());
        assert_eq!(
            body["proposer"],
            serde_json::json!({"sub": "user-1", "name": "Ada"})
        );
        assert_eq!(
            fake.decide_bodies.lock().expect("bodies").clone(),
            vec![serde_json::json!({
                "template": "Verify Service Health (Dev)",
                "agent": "service-agent",
                "workflow_id": "p1",
                "step": "service-validate",
                "survey": {"target_service": "honcho_svc"},
                "dry_run": false,
            })],
            "the preview asks the gate with the proposal's own id, and nothing launches"
        );
        assert!(fake.launch_bodies.lock().expect("bodies").is_empty());

        // Each refusal names its rule and stores nothing.
        let refused = [
            r#"{"kind": "request", "service": "honcho", "check_mode": false, "note": ""}"#.to_string(),
            r#"{"kind": "verify", "service": "Honcho", "check_mode": false, "note": ""}"#.to_string(),
            r#"{"kind": "verify", "service": "unlisted", "check_mode": false, "note": ""}"#.to_string(),
            r#"{"kind": "verify", "service": "plain", "check_mode": false, "note": ""}"#.to_string(),
            r#"{"kind": "verify", "service": "elsewhere", "check_mode": false, "note": ""}"#.to_string(),
            format!(r#"{{"kind": "verify", "service": "honcho", "check_mode": false, "note": "{}"}}"#, "n".repeat(2001)),
            r#"{"kind": "verify", "service": "honcho"}"#.to_string(),
            r#"{"kind": "verify", "service": "honcho", "check_mode": false, "note": "", "extra": 1}"#.to_string(),
        ];
        for text in refused {
            let reply = send(
                addr,
                "POST",
                "/v1/proposals".into(),
                Some(ada.clone()),
                text.clone(),
            )
            .await;
            assert_eq!(reply.status, 400, "{text}: {}", reply.body);
            assert_eq!(json_of(&reply)["error"], "bad_request", "{text}");
            assert!(json_of(&reply)["detail"].is_string(), "{text}");
        }
        let listed = send(
            addr,
            "GET",
            "/v1/proposals".into(),
            Some(ada.clone()),
            String::new(),
        )
        .await;
        assert_eq!(ids_of(&listed), ["p1"], "a refused create stores nothing");

        let (no_gate, no_gate_cookie, _) =
            proposal_server(proposal_state(None, Some(proposal_netbox()))).await;
        let reply = send(
            no_gate,
            "POST",
            "/v1/proposals".into(),
            Some(no_gate_cookie),
            PROPOSE_HONCHO.into(),
        )
        .await;
        assert_eq!(reply.status, 503);
        assert_eq!(reply.body, r#"{"error":"gate_unconfigured"}"#);

        let (unread, unread_cookie, _) =
            proposal_server(proposal_state(Some(gate_client(&origin)), None)).await;
        let reply = send(
            unread,
            "POST",
            "/v1/proposals".into(),
            Some(unread_cookie),
            PROPOSE_HONCHO.into(),
        )
        .await;
        assert_eq!(reply.status, 400);
        assert!(json_of(&reply)["detail"]
            .as_str()
            .expect("detail")
            .contains("not been read"));
    }

    #[tokio::test]
    async fn a_denied_preview_shows_opas_reason_and_cannot_be_approved_until_edited() {
        let fake = Arc::new(FakeGate::default());
        decide_replies(
            &fake,
            [(
                403,
                serde_json::json!({
                    "error": "opa_denied",
                    "detail": "OPA denied the launch",
                    "reason": "proposal required for this step"
                }),
            )],
        );
        let origin = serve_gate(fake.clone()).await;
        let (addr, ada, _) = proposal_server(proposal_state(
            Some(gate_client(&origin)),
            Some(proposal_netbox()),
        ))
        .await;

        let created = send(
            addr,
            "POST",
            "/v1/proposals".into(),
            Some(ada.clone()),
            PROPOSE_HONCHO.into(),
        )
        .await;
        assert_eq!(created.status, 201, "{}", created.body);
        assert_eq!(json_of(&created)["preview"]["state"], "denied");
        assert_eq!(
            json_of(&created)["preview"]["reason"],
            "proposal required for this step"
        );

        let approve = "/v1/proposals/p1/approve";
        let reply = send(
            addr,
            "POST",
            approve.into(),
            Some(ada.clone()),
            r#"{"note": null}"#.into(),
        )
        .await;
        assert_eq!(reply.status, 409);
        assert_eq!(reply.body, r#"{"error":"preview_not_allowed"}"#);
        assert!(
            fake.launch_bodies.lock().expect("bodies").is_empty(),
            "no launch on a denied preview"
        );

        // Editing asks the gate again; the queue is empty now, so the gate allows.
        let edited = send(
            addr,
            "PUT",
            "/v1/proposals/p1".into(),
            Some(ada.clone()),
            r#"{"check_mode": false, "note": "re-run"}"#.into(),
        )
        .await;
        assert_eq!(edited.status, 200, "{}", edited.body);
        assert_eq!(json_of(&edited)["preview"]["state"], "allowed");
        let reply = send(
            addr,
            "POST",
            approve.into(),
            Some(ada),
            r#"{"note": null}"#.into(),
        )
        .await;
        assert_eq!(reply.status, 200, "{}", reply.body);
        assert_eq!(json_of(&reply)["state"], "launched");
    }

    #[tokio::test]
    async fn self_approval_launches_once_and_records_who_approved_it() {
        let fake = Arc::new(FakeGate::default());
        let origin = serve_gate(fake.clone()).await;
        let (addr, ada, grace) = proposal_server(proposal_state(
            Some(gate_client(&origin)),
            Some(proposal_netbox()),
        ))
        .await;
        for _ in 0..2 {
            let created = send(
                addr,
                "POST",
                "/v1/proposals".into(),
                Some(ada.clone()),
                PROPOSE_HONCHO.into(),
            )
            .await;
            assert_eq!(created.status, 201, "{}", created.body);
        }

        let approved = send(
            addr,
            "POST",
            "/v1/proposals/p1/approve".into(),
            Some(ada.clone()),
            r#"{"note": "  go  "}"#.into(),
        )
        .await;
        assert_eq!(approved.status, 200, "{}", approved.body);
        let body = json_of(&approved);
        assert_eq!(body["state"], "launched");
        assert_eq!(
            body["launch"],
            serde_json::json!({"task_id": 4217, "message": "console p1 service-validate", "reason": "read-only"})
        );
        assert_eq!(body["decision"]["action"], "approved");
        assert_eq!(
            body["decision"]["by"],
            serde_json::json!({"sub": "user-1", "name": "Ada"})
        );
        assert_eq!(
            body["decision"]["self_approved"], true,
            "the proposer approved it"
        );
        assert_eq!(body["decision"]["note"], "go", "the note is trimmed");
        assert_eq!(
            fake.launch_bodies.lock().expect("bodies").clone(),
            vec![serde_json::json!({
                "template": "Verify Service Health (Dev)",
                "agent": "service-agent",
                "workflow_id": "p1",
                "step": "service-validate",
                "survey": {"target_service": "honcho_svc"},
                "dry_run": false,
            })]
        );

        let again = send(
            addr,
            "POST",
            "/v1/proposals/p1/approve".into(),
            Some(grace.clone()),
            r#"{"note": null}"#.into(),
        )
        .await;
        assert_eq!(again.status, 409);
        assert_eq!(again.body, r#"{"error":"not_awaiting"}"#);
        assert_eq!(
            fake.launch_bodies.lock().expect("bodies").len(),
            1,
            "one launch in all"
        );

        let other = send(
            addr,
            "POST",
            "/v1/proposals/p2/approve".into(),
            Some(grace),
            r#"{}"#.into(),
        )
        .await;
        assert_eq!(other.status, 200, "{}", other.body);
        assert_eq!(
            json_of(&other)["decision"]["self_approved"],
            false,
            "grace did not propose it"
        );
        assert_eq!(json_of(&other)["decision"]["note"], serde_json::Value::Null);
    }

    #[tokio::test]
    async fn a_refused_or_unanswered_launch_records_launch_failed_with_its_code() {
        let fake = Arc::new(FakeGate::default());
        launch_replies(
            &fake,
            [
                (
                    409,
                    serde_json::json!({"error": "already_running", "detail": "template X already has a running task: 9"}),
                ),
                (
                    403,
                    serde_json::json!({"error": "opa_denied", "detail": "OPA denied the launch", "reason": "branch pin"}),
                ),
                (502, serde_json::json!("bad gateway")),
            ],
        );
        let origin = serve_gate(fake.clone()).await;
        let (addr, ada, _) = proposal_server(proposal_state(
            Some(gate_client(&origin)),
            Some(proposal_netbox()),
        ))
        .await;
        for _ in 0..3 {
            let created = send(
                addr,
                "POST",
                "/v1/proposals".into(),
                Some(ada.clone()),
                PROPOSE_HONCHO.into(),
            )
            .await;
            assert_eq!(created.status, 201, "{}", created.body);
        }

        let refused = send(
            addr,
            "POST",
            "/v1/proposals/p1/approve".into(),
            Some(ada.clone()),
            r#"{"note": null}"#.into(),
        )
        .await;
        assert_eq!(refused.status, 200, "{}", refused.body);
        assert_eq!(json_of(&refused)["state"], "launch_failed");
        assert_eq!(
            json_of(&refused)["launch"],
            serde_json::json!({"error": {"status": 409, "code": "already_running", "detail": "template X already has a running task: 9", "reason": null}})
        );

        let denied = send(
            addr,
            "POST",
            "/v1/proposals/p2/approve".into(),
            Some(ada.clone()),
            r#"{"note": null}"#.into(),
        )
        .await;
        assert_eq!(json_of(&denied)["state"], "launch_failed");
        assert_eq!(
            json_of(&denied)["launch"],
            serde_json::json!({"error": {"status": 403, "code": "opa_denied", "detail": "OPA denied the launch", "reason": "branch pin"}})
        );

        let unanswered = send(
            addr,
            "POST",
            "/v1/proposals/p3/approve".into(),
            Some(ada),
            r#"{"note": null}"#.into(),
        )
        .await;
        assert_eq!(json_of(&unanswered)["state"], "launch_failed");
        assert_eq!(json_of(&unanswered)["launch"]["error"]["status"], 0);
        assert_eq!(
            json_of(&unanswered)["launch"]["error"]["code"],
            "gate_unavailable"
        );
    }

    #[tokio::test]
    async fn put_re_previews_comments_and_a_denial_refuses_later_changes() {
        let fake = Arc::new(FakeGate::default());
        let origin = serve_gate(fake.clone()).await;
        let (addr, ada, grace) = proposal_server(proposal_state(
            Some(gate_client(&origin)),
            Some(proposal_netbox()),
        ))
        .await;
        let created = send(
            addr,
            "POST",
            "/v1/proposals".into(),
            Some(ada.clone()),
            PROPOSE_HONCHO.into(),
        )
        .await;
        assert_eq!(created.status, 201, "{}", created.body);

        let edited = send(
            addr,
            "PUT",
            "/v1/proposals/p1".into(),
            Some(ada.clone()),
            r#"{"check_mode": true, "note": "dry first"}"#.into(),
        )
        .await;
        assert_eq!(edited.status, 200, "{}", edited.body);
        assert_eq!(json_of(&edited)["check_mode"], true);
        assert_eq!(json_of(&edited)["note"], "dry first");
        assert_eq!(json_of(&edited)["preview"]["state"], "allowed");
        assert_eq!(
            fake.decide_bodies
                .lock()
                .expect("bodies")
                .last()
                .expect("a decide")["dry_run"],
            true,
            "the re-preview asks in check mode"
        );
        let bad_edit = send(
            addr,
            "PUT",
            "/v1/proposals/p1".into(),
            Some(ada.clone()),
            r#"{"check_mode": "yes"}"#.into(),
        )
        .await;
        assert_eq!(bad_edit.status, 400);

        let commented = send(
            addr,
            "POST",
            "/v1/proposals/p1/comments".into(),
            Some(grace.clone()),
            r#"{"text": "  Looks right.  "}"#.into(),
        )
        .await;
        assert_eq!(commented.status, 201, "{}", commented.body);
        assert_eq!(json_of(&commented)["comments"][0]["by"]["name"], "Grace");
        assert_eq!(json_of(&commented)["comments"][0]["text"], "Looks right.");
        let blank = send(
            addr,
            "POST",
            "/v1/proposals/p1/comments".into(),
            Some(grace.clone()),
            r#"{"text": "   "}"#.into(),
        )
        .await;
        assert_eq!(blank.status, 400);
        let missing = send(
            addr,
            "POST",
            "/v1/proposals/p9/comments".into(),
            Some(grace.clone()),
            r#"{"text": "hi"}"#.into(),
        )
        .await;
        assert_eq!(missing.status, 404);

        let denied = send(
            addr,
            "POST",
            "/v1/proposals/p1/deny".into(),
            Some(grace.clone()),
            r#"{"note": null}"#.into(),
        )
        .await;
        assert_eq!(denied.status, 200, "{}", denied.body);
        assert_eq!(json_of(&denied)["state"], "denied");
        assert_eq!(json_of(&denied)["decision"]["action"], "denied");
        assert_eq!(json_of(&denied)["decision"]["self_approved"], false);

        let late_approve = send(
            addr,
            "POST",
            "/v1/proposals/p1/approve".into(),
            Some(grace.clone()),
            r#"{"note": null}"#.into(),
        )
        .await;
        assert_eq!(late_approve.status, 409);
        assert_eq!(late_approve.body, r#"{"error":"not_awaiting"}"#);
        let late_edit = send(
            addr,
            "PUT",
            "/v1/proposals/p1".into(),
            Some(ada.clone()),
            r#"{"check_mode": false, "note": ""}"#.into(),
        )
        .await;
        assert_eq!(late_edit.status, 409);
        assert_eq!(late_edit.body, r#"{"error":"not_awaiting"}"#);
        let after = send(
            addr,
            "POST",
            "/v1/proposals/p1/comments".into(),
            Some(ada),
            r#"{"text": "noted"}"#.into(),
        )
        .await;
        assert_eq!(after.status, 201, "comments are allowed in any state");
        assert!(fake.launch_bodies.lock().expect("bodies").is_empty());
    }

    #[tokio::test]
    async fn two_concurrent_approvals_launch_once() {
        let fake = Arc::new(FakeGate::default());
        let origin = serve_gate(fake.clone()).await;
        let (addr, ada, _) = proposal_server(proposal_state(
            Some(gate_client(&origin)),
            Some(proposal_netbox()),
        ))
        .await;
        let created = send(
            addr,
            "POST",
            "/v1/proposals".into(),
            Some(ada.clone()),
            PROPOSE_HONCHO.into(),
        )
        .await;
        assert_eq!(created.status, 201, "{}", created.body);

        let (first, second) = tokio::join!(
            send(
                addr,
                "POST",
                "/v1/proposals/p1/approve".into(),
                Some(ada.clone()),
                r#"{"note": null}"#.into()
            ),
            send(
                addr,
                "POST",
                "/v1/proposals/p1/approve".into(),
                Some(ada),
                r#"{"note": null}"#.into()
            ),
        );
        let mut statuses = [first.status, second.status];
        statuses.sort_unstable();
        assert_eq!(statuses, [200, 409], "{} / {}", first.body, second.body);
        let refused = if first.status == 409 { &first } else { &second };
        assert_eq!(refused.body, r#"{"error":"not_awaiting"}"#);
        assert_eq!(
            fake.launch_bodies.lock().expect("bodies").len(),
            1,
            "two approvals of one proposal launch once (D53)"
        );
    }

    #[tokio::test]
    async fn proposal_writes_need_the_console_origin_and_a_json_body() {
        let fake = Arc::new(FakeGate::default());
        let origin = serve_gate(fake.clone()).await;
        let (addr, ada, grace) = proposal_server(proposal_state(
            Some(gate_client(&origin)),
            Some(proposal_netbox()),
        ))
        .await;
        let refused_headers: [&'static [(&'static str, &'static str)]; 3] = [
            &[
                ("Origin", "https://evil.test"),
                ("Content-Type", "application/json"),
            ],
            &[("Content-Type", "application/json")],
            &[("Origin", CONSOLE_ORIGIN), ("Content-Type", "text/plain")],
        ];
        for headers in refused_headers {
            let reply = call_with(
                addr,
                "POST",
                "/v1/proposals",
                Some(ada.clone()),
                headers,
                PROPOSE_HONCHO,
            )
            .await;
            assert_eq!(reply.status, 403, "{headers:?}");
            assert_eq!(reply.body, r#"{"error":"origin_refused"}"#);
        }
        let listed = send(
            addr,
            "GET",
            "/v1/proposals".into(),
            Some(ada.clone()),
            String::new(),
        )
        .await;
        assert!(
            ids_of(&listed).is_empty(),
            "a refused create stores nothing"
        );

        let created = send(
            addr,
            "POST",
            "/v1/proposals".into(),
            Some(ada.clone()),
            PROPOSE_HONCHO.into(),
        )
        .await;
        assert_eq!(created.status, 201, "{}", created.body);
        let cross_approve = call_with(
            addr,
            "POST",
            "/v1/proposals/p1/approve",
            Some(grace.clone()),
            &[
                ("Origin", "https://evil.test"),
                ("Content-Type", "application/json"),
            ],
            r#"{"note": null}"#,
        )
        .await;
        assert_eq!(cross_approve.status, 403);
        assert_eq!(cross_approve.body, r#"{"error":"origin_refused"}"#);
        let cross_deny = call_with(
            addr,
            "POST",
            "/v1/proposals/p1/deny",
            Some(grace.clone()),
            &[
                ("Origin", "https://evil.test"),
                ("Content-Type", "application/json"),
            ],
            r#"{"note": null}"#,
        )
        .await;
        assert_eq!(cross_deny.status, 403);
        assert!(
            fake.launch_bodies.lock().expect("bodies").is_empty(),
            "nothing launched"
        );
        let still = send(
            addr,
            "GET",
            "/v1/proposals/p1".into(),
            Some(grace),
            String::new(),
        )
        .await;
        assert_eq!(
            json_of(&still)["state"],
            "awaiting",
            "a refused approval changed nothing"
        );
    }

    #[tokio::test]
    async fn the_list_filters_orders_newest_first_and_unknown_ids_are_not_found() {
        let fake = Arc::new(FakeGate::default());
        let origin = serve_gate(fake.clone()).await;
        let (addr, ada, grace) = proposal_server(proposal_state(
            Some(gate_client(&origin)),
            Some(proposal_netbox()),
        ))
        .await;
        for _ in 0..3 {
            let created = send(
                addr,
                "POST",
                "/v1/proposals".into(),
                Some(ada.clone()),
                PROPOSE_HONCHO.into(),
            )
            .await;
            assert_eq!(created.status, 201, "{}", created.body);
        }
        let denied = send(
            addr,
            "POST",
            "/v1/proposals/p2/deny".into(),
            Some(grace),
            r#"{"note": "not now"}"#.into(),
        )
        .await;
        assert_eq!(denied.status, 200, "{}", denied.body);

        let list = |query: &str| {
            send(
                addr,
                "GET",
                format!("/v1/proposals{query}"),
                Some(ada.clone()),
                String::new(),
            )
        };
        assert_eq!(ids_of(&list("").await), ["p3", "p2", "p1"]);
        assert_eq!(ids_of(&list("?state=all").await), ["p3", "p2", "p1"]);
        assert_eq!(ids_of(&list("?state=awaiting").await), ["p3", "p1"]);
        assert_eq!(ids_of(&list("?state=decided").await), ["p2"]);
        let bogus = list("?state=launched").await;
        assert_eq!(bogus.status, 400);
        assert_eq!(json_of(&bogus)["error"], "bad_request");

        let missing = send(
            addr,
            "GET",
            "/v1/proposals/p9".into(),
            Some(ada.clone()),
            String::new(),
        )
        .await;
        assert_eq!(missing.status, 404);
        assert_eq!(missing.body, r#"{"error":"not_found"}"#);
        let malformed = send(
            addr,
            "GET",
            "/v1/proposals/x1".into(),
            Some(ada.clone()),
            String::new(),
        )
        .await;
        assert_eq!(malformed.status, 404);
        let no_row = send(
            addr,
            "POST",
            "/v1/proposals/p9/approve".into(),
            Some(ada),
            r#"{"note": null}"#.into(),
        )
        .await;
        assert_eq!(no_row.status, 404);
        assert_eq!(no_row.body, r#"{"error":"not_found"}"#);
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
            ("openbao.rs", include_str!("openbao.rs")),
            ("secrets.rs", include_str!("secrets.rs")),
            ("github.rs", include_str!("github.rs")),
            ("policies.rs", include_str!("policies.rs")),
            ("grafana.rs", include_str!("grafana.rs")),
            ("gate.rs", include_str!("gate.rs")),
        ] {
            assert!(!text.contains(needle), "{file} turns verification off");
        }
    }
}
