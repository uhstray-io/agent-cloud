//! The Secrets page's answer: what OpenBao metadata holds, and nothing more (plan 17 D35).
//!
//! A background task walks `secret/metadata/` every `REFRESH_INTERVAL` and keeps the result in
//! `SecretsCache`. `GET /v1/secrets` only reads that cache, so a page load costs no OpenBao
//! request, and the console's token (25 uses, D27) is spent on the walk alone. A failed walk
//! keeps the last good paths and says why. The only fields that leave this module are the ones
//! in `SecretPath`: never `custom_metadata`, never a value. The walk reads metadata only, so the
//! console never asks OpenBao for a secret's data.

use std::time::Duration;

use dioxus::server::axum::extract::State;
use dioxus::server::axum::response::{IntoResponse, Response};
use dioxus::server::axum::Json;
use serde::Serialize;
use serde_json::Value;
use tokio::time::MissedTickBehavior;
use tower_sessions::Session;

use super::openbao::{Openbao, OpenbaoError};
use super::work::{self, rfc3339_now};
use super::{AppState, SharedState};

/// How often the walk runs. Ten minutes: secret paths change on a human timescale, and every
/// folder and path in a walk is one OpenBao request.
pub const REFRESH_INTERVAL: Duration = Duration::from_secs(600);
/// Folder levels the walk descends below the KV root.
pub const MAX_DEPTH: usize = 6;
/// Secret paths the walk reads before it stops.
pub const MAX_LEAVES: usize = 1000;
/// OpenBao requests one walk may make: every LIST and every metadata read. The login that
/// precedes them is not counted.
pub const MAX_REQUESTS: usize = 1500;

/// One secret's metadata, as the page shows it. Every other field of the metadata read is dropped
/// when it is built, so this struct is the only shape a secret takes on its way out.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct SecretPath {
    /// `secret/` followed by the key, such as `secret/services/tududi`.
    pub path: String,
    pub current_version: u64,
    /// How many versions the metadata keeps and has not destroyed.
    pub versions: u64,
    pub created_time: String,
    pub updated_time: String,
}

/// What one walk found: the paths it read, sorted, and whether a bound stopped it first.
#[derive(Debug, Clone, PartialEq)]
pub struct Walk {
    pub paths: Vec<SecretPath>,
    pub truncated: bool,
}

/// Walks the metadata tree from the KV root. The root is listed with an empty path; each folder
/// is listed without its trailing slash. Each bound that cuts the walk short sets `truncated`:
/// a folder nested deeper than `max_depth` levels is skipped with its subtree, and its siblings
/// are still read; a path beyond `max_leaves` stops the walk; and a LIST or read beyond
/// `max_requests` stops it. A leaf that answers 404 was deleted during the walk, so it is
/// skipped and does not truncate anything. Any other error fails the walk.
pub async fn walk(
    bao: &Openbao,
    http: &reqwest::Client,
    max_depth: usize,
    max_leaves: usize,
    max_requests: usize,
) -> Result<Walk, OpenbaoError> {
    let mut found: Vec<SecretPath> = Vec::new();
    let mut truncated = false;
    let mut requests = 0usize;
    // Folders still to list, as (path under secret/metadata/, levels below the root).
    let mut folders: Vec<(String, usize)> = vec![(String::new(), 0)];
    'walk: while let Some((folder, depth)) = folders.pop() {
        if requests == max_requests {
            truncated = true;
            break;
        }
        requests += 1;
        for key in bao.list_metadata(http, &folder).await? {
            let path = join(&folder, &key);
            if let Some(child) = path.strip_suffix('/') {
                if depth + 1 > max_depth {
                    // Too deep: this folder and its subtree are not read. Its siblings are.
                    truncated = true;
                    continue;
                }
                folders.push((child.to_string(), depth + 1));
            } else {
                if found.len() >= max_leaves || requests == max_requests {
                    truncated = true;
                    break 'walk;
                }
                requests += 1;
                match bao.read_metadata(http, &path).await {
                    Ok(data) => found.push(secret_path(&path, &data)?),
                    // Deleted between the LIST and this read: it is no longer a secret.
                    Err(OpenbaoError::Status(404)) => {}
                    Err(err) => return Err(err),
                }
            }
        }
    }
    found.sort_by(|a, b| a.path.cmp(&b.path));
    Ok(Walk {
        paths: found,
        truncated,
    })
}

/// `folder/key`, or `key` at the root.
fn join(folder: &str, key: &str) -> String {
    if folder.is_empty() {
        key.to_string()
    } else {
        format!("{folder}/{key}")
    }
}

/// The fields the page shows, taken from one metadata read. The rest of `data` (custom metadata,
/// the CAS and retention settings) is not copied, and `data` goes no further than this function.
fn secret_path(path: &str, data: &Value) -> Result<SecretPath, OpenbaoError> {
    let number = |key: &str| {
        data.get(key)
            .and_then(Value::as_u64)
            .ok_or(OpenbaoError::Decode)
    };
    let text = |key: &str| {
        data.get(key)
            .and_then(Value::as_str)
            .map(str::to_string)
            .ok_or(OpenbaoError::Decode)
    };
    let versions = data
        .get("versions")
        .and_then(Value::as_object)
        .ok_or(OpenbaoError::Decode)?;
    // A destroyed version keeps its entry, with its data gone, so the console cannot roll back to
    // it. It is not counted. An entry with no `destroyed` flag is not destroyed.
    let kept = versions
        .values()
        .filter(|entry| entry.get("destroyed").and_then(Value::as_bool) != Some(true))
        .count();
    Ok(SecretPath {
        path: format!("secret/{path}"),
        current_version: number("current_version")?,
        versions: u64::try_from(kept).unwrap_or(u64::MAX),
        created_time: text("created_time")?,
        updated_time: text("updated_time")?,
    })
}

/// The last walk that finished.
struct Good {
    paths: Vec<SecretPath>,
    truncated: bool,
    refreshed_at: String,
}

/// What `/v1/secrets` reads. `good` is the last walk that finished. `error` is why the newest
/// walk failed, when it did. A failed walk leaves `good` in place.
pub struct SecretsCache {
    configured: bool,
    good: Option<Good>,
    error: Option<String>,
}

impl SecretsCache {
    /// `configured` is whether OpenBao is set up for the console. When it is not, no walk runs
    /// and the page says so.
    pub fn new(configured: bool) -> Self {
        Self {
            configured,
            good: None,
            error: None,
        }
    }

    /// Stores a finished walk and clears the error.
    pub fn record_ok(&mut self, walk: Walk, refreshed_at: String) {
        self.good = Some(Good {
            paths: walk.paths,
            truncated: walk.truncated,
            refreshed_at,
        });
        self.error = None;
    }

    /// Keeps the last good walk, if there is one, and stores why this walk failed.
    pub fn record_error(&mut self, error: String) {
        self.error = Some(error);
    }

    /// The body `/v1/secrets` serves. `state` is `unconfigured` without OpenBao, `loading` before
    /// the first walk finishes, `error` when no walk has finished, and `ok` otherwise. In `ok`,
    /// `error` is set when the newest walk failed, and `refreshed_at` is then the older walk's.
    pub fn body(&self) -> SecretsBody {
        let state = match (self.configured, &self.good) {
            (false, _) => "unconfigured",
            (true, Some(_)) => "ok",
            (true, None) if self.error.is_some() => "error",
            (true, None) => "loading",
        };
        let (paths, refreshed_at, truncated) = match &self.good {
            Some(good) => (
                good.paths.clone(),
                Some(good.refreshed_at.clone()),
                good.truncated,
            ),
            None => (Vec::new(), None, false),
        };
        SecretsBody {
            state,
            paths,
            refreshed_at,
            truncated,
            error: self.error.clone(),
        }
    }
}

/// `GET /v1/secrets`: the cache as the page reads it.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct SecretsBody {
    pub state: &'static str,
    pub paths: Vec<SecretPath>,
    pub refreshed_at: Option<String>,
    pub truncated: bool,
    pub error: Option<String>,
}

/// `GET /v1/secrets` (platform admins only, as `/v1/network`). Reads the cache, never OpenBao.
pub async fn get_secrets(State(app): State<SharedState>, session: Session) -> Response {
    if let Err(response) = work::admin_only(&session).await {
        return response;
    }
    let cache = app.secrets.read().await;
    Json(cache.body()).into_response()
}

/// Walks OpenBao once and stores the result. Does nothing when OpenBao is not configured. The
/// lock is held only to store, never across the walk.
pub async fn refresh(app: &AppState) {
    let Some(bao) = app.openbao.as_ref() else {
        return;
    };
    let result = walk(bao, &app.http, MAX_DEPTH, MAX_LEAVES, MAX_REQUESTS).await;
    let mut cache = app.secrets.write().await;
    match result {
        Ok(found) => cache.record_ok(found, rfc3339_now()),
        Err(err) => {
            let text = err.to_string();
            log_failure(cache.error.as_deref(), &text);
            cache.record_error(text);
        }
    }
}

/// One line per distinct failure, not one per walk while it lasts.
fn log_failure(previous: Option<&str>, text: &str) {
    if previous != Some(text) {
        eprintln!("console: OpenBao metadata walk failed: {text}");
    }
}

/// Starts the walk when OpenBao is configured. The first walk runs at once. A missed tick is
/// skipped, not replayed.
pub fn spawn_refresh(state: SharedState) {
    if state.openbao.is_none() {
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::SocketAddr;
    use std::sync::atomic::{AtomicU16, AtomicUsize, Ordering};
    use std::sync::{Arc, Mutex};

    use dioxus::server::axum::http::{StatusCode, Uri};
    use dioxus::server::axum::routing::{get, post};
    use dioxus::server::axum::Router;
    use serde_json::json;

    use crate::server::auth;
    use crate::server::config::{Config, OpenbaoConfig, Secret};
    use crate::server::inventory::{FlowSet, InventoryCache};
    use crate::server::prefs::Prefs;
    use crate::server::scope::Scope;
    use crate::server::{api_router, session_layer};

    const ROLE_ID: &str = "console-role-test";
    const SECRET_ID: &str = "console-secret-test";
    /// Planted in one secret's custom metadata and in every data read. No response may carry it.
    const CANARY: &str = "CANARY-7f3a";
    const SCOPE: &str = r#"{"example": true, "tenants": []}"#;

    /// A fake OpenBao: AppRole login, metadata LIST and read, and data reads. It holds the tree
    /// below, and it records every request that reaches a handler.
    struct Fake {
        /// When non-zero, every read answers with this status.
        fail_with: AtomicU16,
        /// Requests that reached a `secret/data/` path. The canary test needs zero.
        data_hits: AtomicUsize,
        /// Path and query of every request that reached a handler.
        seen: Mutex<Vec<String>>,
        /// One leaf and the status its metadata read answers with.
        fail_leaf: Mutex<Option<(String, u16)>>,
    }

    impl Fake {
        fn new() -> Self {
            Fake {
                fail_with: AtomicU16::new(0),
                data_hits: AtomicUsize::new(0),
                seen: Mutex::new(Vec::new()),
                fail_leaf: Mutex::new(None),
            }
        }

        fn fail_leaf(&self, key: &str, status: u16) {
            *self.fail_leaf.lock().expect("fail_leaf") = Some((key.to_string(), status));
        }

        fn seen_paths(&self) -> Vec<String> {
            self.seen.lock().expect("seen").clone()
        }
    }

    async fn fake_login() -> (StatusCode, Json<Value>) {
        (
            StatusCode::OK,
            Json(json!({"auth": {
                "client_token": "tok",
                "lease_duration": 1800,
                "num_uses": 0,
            }})),
        )
    }

    async fn fake_read(State(fake): State<Arc<Fake>>, uri: Uri) -> (StatusCode, Json<Value>) {
        fake.seen.lock().expect("seen").push(
            uri.path_and_query()
                .map_or_else(String::new, |pq| pq.as_str().to_string()),
        );
        let forced = fake.fail_with.load(Ordering::SeqCst);
        if forced != 0 {
            let status = StatusCode::from_u16(forced).expect("status");
            return (status, Json(json!({"errors": ["denied"]})));
        }
        let path = uri.path();
        if path.starts_with("/v1/secret/data/") {
            fake.data_hits.fetch_add(1, Ordering::SeqCst);
            return (
                StatusCode::OK,
                Json(json!({"data": {"data": {"value": CANARY}, "metadata": {"version": 1}}})),
            );
        }
        let not_found = (StatusCode::NOT_FOUND, Json(json!({"errors": []})));
        let Some(key) = path.strip_prefix("/v1/secret/metadata/") else {
            return not_found;
        };
        let key = key.trim_end_matches('/');
        if uri.query() == Some("list=true") {
            let keys = match key {
                // `gone` is listed but has no metadata: a secret deleted after the LIST.
                "" => json!(["services/", "zeta", "alpha", "gone"]),
                "services" => json!(["console/", "tududi", "authentik-oidc"]),
                "services/console" => json!(["github", "openbao-approle"]),
                _ => return not_found,
            };
            return (StatusCode::OK, Json(json!({"data": {"keys": keys}})));
        }
        let failing = fake.fail_leaf.lock().expect("fail_leaf").clone();
        if let Some((name, status)) = failing {
            if name == key {
                let status = StatusCode::from_u16(status).expect("status");
                return (status, Json(json!({"errors": ["denied"]})));
            }
        }
        match leaf(key) {
            Some(data) => (StatusCode::OK, Json(json!({"data": data}))),
            None => not_found,
        }
    }

    /// Metadata of one leaf: the versions kept (`oldest..=current`), the last change and the
    /// custom metadata. Only `alpha` carries the canary.
    fn metadata(oldest: u64, current: u64, updated: &str, custom: Value) -> Value {
        let versions: serde_json::Map<String, Value> = (oldest..=current)
            .map(|version| {
                (
                    version.to_string(),
                    json!({"created_time": "2026-01-01T00:00:00Z", "deletion_time": "", "destroyed": false}),
                )
            })
            .collect();
        json!({
            "cas_required": false,
            "created_time": "2026-01-01T00:00:00Z",
            "current_version": current,
            "custom_metadata": custom,
            "delete_version_after": "0s",
            "max_versions": 0,
            "oldest_version": oldest,
            "updated_time": updated,
            "versions": versions,
        })
    }

    fn leaf(key: &str) -> Option<Value> {
        Some(match key {
            "alpha" => metadata(1, 2, "2026-02-01T00:00:00Z", json!({"x": CANARY})),
            "zeta" => metadata(1, 1, "2026-03-01T00:00:00Z", json!({})),
            "services/authentik-oidc" => metadata(1, 1, "2026-04-01T00:00:00Z", json!({})),
            "services/console/github" => {
                metadata(1, 7, "2026-05-01T00:00:00Z", json!({"owner": "console"}))
            }
            "services/console/openbao-approle" => metadata(1, 1, "2026-06-01T00:00:00Z", json!({})),
            "services/tududi" => {
                let mut data = metadata(2, 4, "2026-07-01T00:00:00Z", json!({}));
                // Version 3 is destroyed: its entry stays, its data is gone. Version 2 has no
                // `destroyed` key at all, which is not destroyed. Two of the three are kept.
                data["versions"]["3"]["destroyed"] = json!(true);
                data["versions"]["2"]
                    .as_object_mut()
                    .expect("version entry")
                    .remove("destroyed");
                data
            }
            _ => return None,
        })
    }

    /// The tree's leaves, sorted, as the walk must return them.
    fn expected_paths() -> Vec<SecretPath> {
        let row = |path: &str, current: u64, versions: u64, updated: &str| SecretPath {
            path: path.to_string(),
            current_version: current,
            versions,
            created_time: "2026-01-01T00:00:00Z".to_string(),
            updated_time: updated.to_string(),
        };
        vec![
            row("secret/alpha", 2, 2, "2026-02-01T00:00:00Z"),
            row(
                "secret/services/authentik-oidc",
                1,
                1,
                "2026-04-01T00:00:00Z",
            ),
            row(
                "secret/services/console/github",
                7,
                7,
                "2026-05-01T00:00:00Z",
            ),
            row(
                "secret/services/console/openbao-approle",
                1,
                1,
                "2026-06-01T00:00:00Z",
            ),
            row("secret/services/tududi", 4, 2, "2026-07-01T00:00:00Z"),
            row("secret/zeta", 1, 1, "2026-03-01T00:00:00Z"),
        ]
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

    /// The fake OpenBao on a local port, and the handle the test keeps to inspect it.
    async fn fake_bao() -> (Arc<Fake>, SocketAddr) {
        let fake = Arc::new(Fake::new());
        let app = Router::new()
            .route("/v1/auth/approle/login", post(fake_login))
            .route("/v1/{*rest}", get(fake_read))
            .with_state(fake.clone());
        (fake, serve(app).await)
    }

    fn client_for(addr: SocketAddr) -> Openbao {
        Openbao::new(OpenbaoConfig {
            api_url: format!("http://{addr}"),
            role_id: Secret::new(ROLE_ID),
            secret_id: Secret::new(SECRET_ID),
        })
        .expect("client")
    }

    /// The application state, with OpenBao when one is given.
    fn state(openbao: Option<Openbao>) -> SharedState {
        let configured = openbao.is_some();
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
                authentik: None,
                role_map_file: None,
            },
            http: reqwest::Client::new(),
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
            openbao,
            secrets: tokio::sync::RwLock::new(SecretsCache::new(configured)),
            access: tokio::sync::RwLock::new(crate::server::access::AccessCache::new(false)),
            role_map: crate::server::access::RoleMap::empty(),
        })
    }

    /// `GET /test/login`: signs a user in with `groups`.
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

    /// Serves the API over `state` and returns its address and a session cookie for a user in
    /// `groups`.
    async fn app_for(state: SharedState, groups: &'static [&'static str]) -> (SocketAddr, String) {
        let sessions = session_layer(Prefs::in_memory().expect("sessions").session_store());
        let app = api_router(state).merge(login_route(groups)).layer(sessions);
        let addr = serve(app).await;
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
    async fn fetch(addr: SocketAddr, path: &str, cookie: Option<&str>) -> (u16, String) {
        let client = reqwest::Client::new();
        let mut request = client.get(format!("http://{addr}{path}"));
        if let Some(cookie) = cookie {
            request = request.header("cookie", cookie);
        }
        let reply = request.send().await.expect("send");
        let status = reply.status().as_u16();
        (status, reply.text().await.expect("body"))
    }

    #[test]
    fn a_destroyed_version_is_not_counted_and_a_missing_flag_is_not_destroyed() {
        let data = json!({
            "current_version": 3,
            "created_time": "2026-01-01T00:00:00Z",
            "updated_time": "2026-02-01T00:00:00Z",
            "versions": {
                "1": {"destroyed": true},
                "2": {"destroyed": false},
                "3": {},
            },
        });
        let row = secret_path("x", &data).expect("row");
        assert_eq!(
            row.versions, 2,
            "versions 2 and 3 are kept; version 1 is destroyed"
        );
        assert_eq!(row.current_version, 3);
    }

    #[test]
    fn a_secret_path_serializes_exactly_the_declared_fields() {
        let row = SecretPath {
            path: "secret/x".into(),
            current_version: 1,
            versions: 1,
            created_time: "c".into(),
            updated_time: "u".into(),
        };
        let value = serde_json::to_value(&row).expect("json");
        let mut keys: Vec<&str> = value
            .as_object()
            .expect("object")
            .keys()
            .map(String::as_str)
            .collect();
        keys.sort_unstable();
        assert_eq!(
            keys,
            [
                "created_time",
                "current_version",
                "path",
                "updated_time",
                "versions"
            ]
        );
    }

    /// The fake tree is ten OpenBao requests from the root: three LISTs, and seven metadata reads
    /// (`gone` among them, which is read and answers 404).
    const FULL_WALK_REQUESTS: usize = 10;

    #[tokio::test]
    async fn walk_reads_every_leaf_recursively_sorted_and_counts_versions() {
        let (fake, addr) = fake_bao().await;
        let bao = client_for(addr);
        let http = reqwest::Client::new();

        let found = walk(&bao, &http, MAX_DEPTH, MAX_LEAVES, MAX_REQUESTS)
            .await
            .expect("walk");
        assert_eq!(found.paths, expected_paths());
        assert!(!found.truncated);
        assert_eq!(
            fake.seen_paths().len(),
            FULL_WALK_REQUESTS,
            "each LIST and each read is one request"
        );

        // The walk reads metadata and nothing else: no data path, no other prefix.
        assert_eq!(fake.data_hits.load(Ordering::SeqCst), 0);
        for path in fake.seen_paths() {
            assert!(
                path.starts_with("/v1/secret/metadata/") || path.starts_with("/v1/auth/"),
                "unexpected request {path}"
            );
        }
    }

    fn names(found: &Walk) -> Vec<&str> {
        found.paths.iter().map(|path| path.path.as_str()).collect()
    }

    #[tokio::test]
    async fn a_folder_past_the_depth_bound_is_skipped_and_its_siblings_are_read() {
        let (_fake, addr) = fake_bao().await;
        let bao = client_for(addr);
        let http = reqwest::Client::new();

        // services/console is two levels down. A depth bound of one skips it with its subtree,
        // and the siblings beside it (services/tududi, services/authentik-oidc, the root's own
        // leaves) are still read.
        let shallow = walk(&bao, &http, 1, MAX_LEAVES, MAX_REQUESTS)
            .await
            .expect("walk");
        assert_eq!(
            names(&shallow),
            [
                "secret/alpha",
                "secret/services/authentik-oidc",
                "secret/services/tududi",
                "secret/zeta",
            ]
        );
        assert!(
            shallow.truncated,
            "a skipped subtree makes the answer partial"
        );

        // At depth zero every folder is past the bound: only the root's own leaves are read.
        let root_only = walk(&bao, &http, 0, MAX_LEAVES, MAX_REQUESTS)
            .await
            .expect("walk");
        assert_eq!(names(&root_only), ["secret/alpha", "secret/zeta"]);
        assert!(root_only.truncated);
    }

    #[tokio::test]
    async fn a_path_past_the_leaf_bound_stops_the_walk() {
        let (_fake, addr) = fake_bao().await;
        let bao = client_for(addr);
        let http = reqwest::Client::new();

        let few = walk(&bao, &http, MAX_DEPTH, 2, MAX_REQUESTS)
            .await
            .expect("walk");
        assert_eq!(few.paths.len(), 2, "the leaf bound caps the secrets read");
        assert!(few.truncated, "a path past the leaf bound");

        // Exactly as many leaves as the bound is the whole tree, not a truncated one.
        let exact = walk(&bao, &http, MAX_DEPTH, 6, MAX_REQUESTS)
            .await
            .expect("walk");
        assert_eq!(exact.paths, expected_paths());
        assert!(!exact.truncated);
    }

    #[tokio::test]
    async fn the_request_cap_stops_the_walk_before_the_request_past_it() {
        let (fake, addr) = fake_bao().await;
        let bao = client_for(addr);
        let http = reqwest::Client::new();

        // The cap counts LISTs and reads together, so the whole tree fits in exactly its requests.
        let whole = walk(&bao, &http, MAX_DEPTH, MAX_LEAVES, FULL_WALK_REQUESTS)
            .await
            .expect("walk");
        assert_eq!(whole.paths, expected_paths());
        assert!(!whole.truncated);

        // One fewer stops the walk on the request it cannot make. Exactly the cap is sent.
        let before = fake.seen_paths().len();
        let capped = walk(&bao, &http, MAX_DEPTH, MAX_LEAVES, FULL_WALK_REQUESTS - 1)
            .await
            .expect("walk");
        assert!(capped.truncated, "the cap cut the walk short");
        assert_eq!(
            fake.seen_paths().len() - before,
            FULL_WALK_REQUESTS - 1,
            "no request past the cap"
        );

        // A cap of zero sends nothing and says the answer is partial.
        let before = fake.seen_paths().len();
        let none = walk(&bao, &http, MAX_DEPTH, MAX_LEAVES, 0)
            .await
            .expect("walk");
        assert!(none.paths.is_empty() && none.truncated);
        assert_eq!(fake.seen_paths().len(), before);
    }

    #[tokio::test]
    async fn a_leaf_deleted_during_the_walk_is_skipped_and_truncates_nothing() {
        let (_fake, addr) = fake_bao().await;
        let bao = client_for(addr);
        let http = reqwest::Client::new();

        // `gone` is listed and then answers 404. It is left out, and the walk is otherwise whole.
        let found = walk(&bao, &http, MAX_DEPTH, MAX_LEAVES, MAX_REQUESTS)
            .await
            .expect("walk");
        assert_eq!(found.paths, expected_paths());
        assert!(!found.truncated);
    }

    #[tokio::test]
    async fn any_other_leaf_error_fails_the_walk() {
        let (fake, addr) = fake_bao().await;
        let bao = client_for(addr);
        let http = reqwest::Client::new();

        fake.fail_leaf("zeta", 500);
        let err = walk(&bao, &http, MAX_DEPTH, MAX_LEAVES, MAX_REQUESTS)
            .await
            .expect_err("a 500 on a leaf fails the walk");
        assert_eq!(err, OpenbaoError::Status(500));

        // A 403 is an error too, after the one re-login the client makes.
        fake.fail_leaf("zeta", 403);
        let err = walk(&bao, &http, MAX_DEPTH, MAX_LEAVES, MAX_REQUESTS)
            .await
            .expect_err("a 403 on a leaf fails the walk");
        assert_eq!(err, OpenbaoError::Status(403));
    }

    #[test]
    fn body_states_follow_configuration_and_the_walks_that_finished() {
        let cache = SecretsCache::new(false);
        let body = cache.body();
        assert_eq!(body.state, "unconfigured");
        assert!(body.paths.is_empty() && body.error.is_none());

        let mut cache = SecretsCache::new(true);
        assert_eq!(cache.body().state, "loading", "no walk has finished yet");

        cache.record_error("OpenBao answered HTTP 403".into());
        let body = cache.body();
        assert_eq!(
            body.state, "error",
            "nothing good yet, so the page sees the error"
        );
        assert_eq!(body.error.as_deref(), Some("OpenBao answered HTTP 403"));
        assert!(body.paths.is_empty() && body.refreshed_at.is_none());

        cache.record_ok(
            Walk {
                paths: expected_paths(),
                truncated: false,
            },
            "2026-11-03T10:00:00Z".into(),
        );
        let body = cache.body();
        assert_eq!(body.state, "ok");
        assert_eq!(body.error, None, "a good walk clears the error");
        assert_eq!(body.paths.len(), 6);
        assert_eq!(body.refreshed_at.as_deref(), Some("2026-11-03T10:00:00Z"));
    }

    #[tokio::test]
    async fn no_openbao_means_no_walk_and_an_unconfigured_page() {
        let app = state(None);
        refresh(&app).await;
        assert_eq!(app.secrets.read().await.body().state, "unconfigured");
    }

    #[tokio::test]
    async fn a_failed_walk_keeps_the_last_good_paths_and_says_why() {
        let (fake, addr) = fake_bao().await;
        let app = state(Some(client_for(addr)));

        refresh(&app).await;
        let first = app.secrets.read().await.body();
        assert_eq!(first.state, "ok");
        assert_eq!(first.paths, expected_paths());
        let refreshed_at = first.refreshed_at.clone();
        assert!(refreshed_at.is_some());

        // OpenBao now refuses every read. The page keeps the paths and names the status.
        fake.fail_with.store(500, Ordering::SeqCst);
        refresh(&app).await;
        let stale = app.secrets.read().await.body();
        assert_eq!(stale.state, "ok", "the last good walk is still served");
        assert_eq!(stale.error.as_deref(), Some("OpenBao answered HTTP 500"));
        assert_eq!(stale.paths, expected_paths());
        assert_eq!(
            stale.refreshed_at, refreshed_at,
            "the stamp is the older walk's"
        );

        // The same answer, over HTTP, to an administrator.
        let (addr, cookie) = app_for(app.clone(), &["platform-admins"]).await;
        let (status, body) = fetch(addr, "/v1/secrets", Some(&cookie)).await;
        assert_eq!(status, 200);
        let body: Value = serde_json::from_str(&body).expect("json");
        assert_eq!(body["state"], "ok");
        assert_eq!(body["error"], "OpenBao answered HTTP 500");
        assert_eq!(body["paths"].as_array().expect("paths").len(), 6);

        // The next good walk clears the error.
        fake.fail_with.store(0, Ordering::SeqCst);
        refresh(&app).await;
        let healed = app.secrets.read().await.body();
        assert_eq!(healed.error, None);
    }

    #[tokio::test]
    async fn a_walk_that_never_finished_is_an_error_with_the_status_only() {
        let (fake, addr) = fake_bao().await;
        fake.fail_with.store(403, Ordering::SeqCst);
        let app = state(Some(client_for(addr)));
        refresh(&app).await;

        let (addr, cookie) = app_for(app.clone(), &["platform-admins"]).await;
        let (status, body) = fetch(addr, "/v1/secrets", Some(&cookie)).await;
        assert_eq!(status, 200);
        let value: Value = serde_json::from_str(&body).expect("json");
        assert_eq!(value["state"], "error");
        assert_eq!(value["error"], "OpenBao answered HTTP 403");
        assert_eq!(value["paths"], json!([]));

        // The error names no credential and no address.
        assert!(!body.contains(ROLE_ID), "{body}");
        assert!(!body.contains(SECRET_ID), "{body}");
        assert!(!body.contains("127.0.0.1"), "{body}");
    }

    #[tokio::test]
    async fn the_canary_never_leaves_the_api() {
        let (fake, addr) = fake_bao().await;
        let app = state(Some(client_for(addr)));
        refresh(&app).await;

        let (addr, cookie) = app_for(app.clone(), &["platform-admins"]).await;
        let (status, body) = fetch(addr, "/v1/secrets", Some(&cookie)).await;
        assert_eq!(status, 200);

        // The walk did run: the paths are there, and the canary is not.
        let value: Value = serde_json::from_str(&body).expect("json");
        assert_eq!(value["paths"].as_array().expect("paths").len(), 6);
        assert!(!body.contains(CANARY), "the canary left the API: {body}");
        assert!(!body.contains("custom_metadata"), "{body}");

        // And the fake OpenBao saw no read of any secret's data.
        assert_eq!(fake.data_hits.load(Ordering::SeqCst), 0);
        for path in fake.seen_paths() {
            assert!(!path.contains("/v1/secret/data/"), "data read {path}");
        }
    }

    #[tokio::test]
    async fn a_page_load_reads_the_cache_and_not_openbao() {
        let (fake, addr) = fake_bao().await;
        let app = state(Some(client_for(addr)));
        refresh(&app).await;
        let before = fake.seen_paths().len();

        let (addr, cookie) = app_for(app.clone(), &["platform-admins"]).await;
        for _ in 0..3 {
            let (status, _) = fetch(addr, "/v1/secrets", Some(&cookie)).await;
            assert_eq!(status, 200);
        }
        assert_eq!(
            fake.seen_paths().len(),
            before,
            "a page load sent no OpenBao request"
        );
    }

    #[tokio::test]
    async fn members_get_403_and_platform_admins_get_the_answer() {
        let (_fake, addr) = fake_bao().await;
        let app = state(Some(client_for(addr)));
        refresh(&app).await;

        for groups in [&["ops"][..], &["platform-admins-old", "users"][..]] {
            let (addr, cookie) = app_for(app.clone(), groups).await;
            let (status, body) = fetch(addr, "/v1/secrets", Some(&cookie)).await;
            assert_eq!(status, 403, "{groups:?}");
            assert_eq!(body, r#"{"error":"forbidden"}"#, "{groups:?}");
        }

        let (addr, cookie) = app_for(app.clone(), &["platform-admins"]).await;
        let (status, body) = fetch(addr, "/v1/secrets", Some(&cookie)).await;
        assert_eq!(status, 200);
        let value: Value = serde_json::from_str(&body).expect("json");
        assert_eq!(value["state"], "ok");
        assert_eq!(value["truncated"], false);
    }
}
