//! In-process API load test (plan 17, D46, task 10.3). Ignored by default:
//! `cargo test --features server -- --ignored load_`.
//!
//! It serves the real `api_router` on a loopback port, signs in a platform admin,
//! and sends concurrent GETs to every `/v1` read route for a fixed time. The state
//! is the unconfigured one (no upstream is configured and no cache is filled), so
//! the numbers measure the router, the session layer and the empty-envelope answers,
//! not populated upstream data and not any upstream.
//!
//! The helpers below repeat the ones in `server::tests`, which are private to that
//! module. `app_state` repeats its `AppState` literal, so a new field added there
//! must be added here too; the compiler reports it.

use super::*;

use std::collections::BTreeMap;
use std::net::SocketAddr;
use std::time::{Duration, Instant};

use dioxus::server::axum::routing::get;
use dioxus::server::axum::Router;
use reqwest::header::{COOKIE, SET_COOKIE};
use tower_sessions::Session;

/// The scope the tests use, as `server::tests` defines it. Admins see every tenant.
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

/// Every unconfigured cache, the state `server::tests` builds for an admin read.
fn unconfigured_state() -> SharedState {
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
        prefs: Prefs::in_memory().expect("in-memory prefs"),
        work: Arc::new(tokio::sync::RwLock::new(work::WorkCache::unconfigured())),
        workflow: Vec::new(),
        inventory: tokio::sync::RwLock::new(inventory::InventoryCache::new(false, false)),
        proxmox_http: reqwest::Client::new(),
        flows: inventory::FlowSet::unconfigured(),
        gateway: gateway::GatewaySet::unconfigured(),
        catalog: catalog::CatalogSet::unconfigured(),
        usage: tokio::sync::RwLock::new(grafana::UsageCache::new(false)),
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

/// Signs the caller in as a platform admin. The route has no identity provider.
fn admin_login_route() -> Router {
    Router::new().route(
        "/test/login",
        get(|session: Session| async move {
            let user = auth::CurrentUser {
                sub: "admin-1".into(),
                email: Some("admin@example.test".into()),
                name: Some("Admin".into()),
                username: Some("admin".into()),
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

/// The `/v1` read routes the load runs over: 16 of the router's 18 reads. The two detail routes,
/// `/v1/work/{key}` and `/v1/tenants/{t}/services/{name}`, need an item key the empty test caches cannot
/// supply, so they are left out rather than timed on their not-found path.
const READS: [&str; 16] = [
    "/v1/me",
    "/v1/tenants",
    "/v1/tenants/acme/tree",
    "/v1/work",
    "/v1/gateway",
    "/v1/catalog",
    "/v1/roles",
    "/v1/secrets",
    "/v1/access",
    "/v1/policies",
    "/v1/delivery",
    "/v1/telemetry/rollups",
    "/v1/network",
    "/v1/flows",
    "/v1/tenants/acme/inventory",
    "/v1/tenants/acme/services",
];

const WORKERS: usize = 32;
const RUN: Duration = Duration::from_secs(10);

/// Nearest-rank percentile of a sorted, non-empty list.
fn percentile(sorted: &[Duration], p: f64) -> Duration {
    let rank = ((p * sorted.len() as f64).ceil() as usize).clamp(1, sorted.len());
    sorted[rank - 1]
}

fn ms(duration: Duration) -> f64 {
    duration.as_secs_f64() * 1000.0
}

/// One line of the report, written to the process's stdout. libtest captures `println!`
/// from a passing test, so the numbers would be hidden under the documented command
/// (`cargo test ... -- --ignored load_`) unless `--nocapture` were added.
fn report(line: &str) {
    use std::io::Write;
    let mut out = std::io::stdout().lock();
    writeln!(out, "{line}").expect("write report line");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
#[ignore]
async fn load_cached_reads() {
    let app = api_router(unconfigured_state())
        .merge(admin_login_route())
        .layer(session_layer(
            Prefs::in_memory()
                .expect("in-memory sessions")
                .session_store(),
        ));
    let addr = serve(app).await;
    let base = format!("http://{addr}");
    let client = reqwest::Client::builder()
        .no_proxy()
        .timeout(Duration::from_secs(10))
        .pool_max_idle_per_host(WORKERS)
        .build()
        .expect("client");

    let login = client
        .get(format!("{base}/test/login"))
        .send()
        .await
        .expect("login");
    let cookie = login
        .headers()
        .get_all(SET_COOKIE)
        .iter()
        .filter_map(|value| value.to_str().ok())
        .find(|value| value.starts_with("console_session="))
        .and_then(|value| value.split(';').next())
        .map(str::to_string)
        .expect("console_session cookie");

    // Preflight: every route must answer 200 to the admin before the clock starts.
    // A session that silently failed would otherwise time the 401 path.
    for path in READS {
        let status = client
            .get(format!("{base}{path}"))
            .header(COOKIE, cookie.as_str())
            .send()
            .await
            .expect("preflight request")
            .status()
            .as_u16();
        assert_eq!(
            status, 200,
            "{path} must answer 200 to an admin before the run"
        );
    }

    let deadline = Instant::now() + RUN;
    let started = Instant::now();
    let mut workers = Vec::with_capacity(WORKERS);
    for worker in 0..WORKERS {
        let client = client.clone();
        let base = base.clone();
        let cookie = cookie.clone();
        workers.push(tokio::spawn(async move {
            let mut samples: Vec<(Duration, u16)> = Vec::new();
            let mut transport_errors = 0u64;
            // Each worker starts at its own route, then walks the list round-robin.
            let mut next = worker;
            while Instant::now() < deadline {
                let path = READS[next % READS.len()];
                next += 1;
                let sent = Instant::now();
                match client
                    .get(format!("{base}{path}"))
                    .header(COOKIE, cookie.as_str())
                    .send()
                    .await
                {
                    Ok(reply) => {
                        let status = reply.status().as_u16();
                        match reply.bytes().await {
                            Ok(_) => samples.push((sent.elapsed(), status)),
                            Err(_) => transport_errors += 1,
                        }
                    }
                    Err(_) => transport_errors += 1,
                }
            }
            (samples, transport_errors)
        }));
    }

    let mut latencies: Vec<Duration> = Vec::new();
    let mut by_status: BTreeMap<u16, u64> = BTreeMap::new();
    let mut transport_errors = 0u64;
    for worker in workers {
        let (samples, errors) = worker.await.expect("worker task");
        transport_errors += errors;
        for (latency, status) in samples {
            latencies.push(latency);
            *by_status.entry(status).or_default() += 1;
        }
    }
    let elapsed = started.elapsed();
    latencies.sort();

    let total = latencies.len();
    let server_errors: u64 = by_status
        .iter()
        .filter(|(status, _)| **status >= 500)
        .map(|(_, count)| *count)
        .sum();
    // The counts print before any assertion, so a failing run still shows them.
    report(&format!(
        "load_cached_reads: workers={WORKERS} routes={} window_s={:.2} requests={total} req_per_s={:.1}",
        READS.len(),
        elapsed.as_secs_f64(),
        total as f64 / elapsed.as_secs_f64(),
    ));
    report(&format!(
        "load_cached_reads: statuses={by_status:?} transport_errors={transport_errors}"
    ));
    if total > 0 {
        report(&format!(
            "load_cached_reads: p50_ms={:.2} p95_ms={:.2} p99_ms={:.2} max_ms={:.2}",
            ms(percentile(&latencies, 0.50)),
            ms(percentile(&latencies, 0.95)),
            ms(percentile(&latencies, 0.99)),
            ms(*latencies.last().expect("non-empty")),
        ));
    }

    assert_eq!(transport_errors, 0, "transport errors under load");
    assert_eq!(server_errors, 0, "5xx under load: {by_status:?}");
    assert!(total > 0, "no request completed");
}
