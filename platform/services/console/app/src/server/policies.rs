//! The Policy page's answer (plan 17, 8.4, D36): the OPA and OpenBao policy files on the configured GitHub
//! ref, each with its kind, its name and the last commit that touched it.
//!
//! A background task reads them every `REFRESH_INTERVAL` into `PoliciesCache`. `GET /v1/policies` only reads
//! that cache, so a page load costs no GitHub request. The console's read-only GitHub token is read from
//! OpenBao on every refresh, used for GitHub only, and never kept. A failed read keeps the last good list and
//! says why. No token, URL or response body reaches an error or a log line. The page has no decision counts:
//! the console has no source for them (OPA authz refuses data reads).

use std::time::Duration;

use dioxus::server::axum::extract::State;
use dioxus::server::axum::response::{IntoResponse, Response};
use dioxus::server::axum::Json;
use serde::Serialize;
use serde_json::Value;
use tokio::time::MissedTickBehavior;
use tower_sessions::Session;

use super::config::{GithubConfig, Secret};
use super::github::{self, FetchError, LastCommit, Listing, GITHUB_API};
use super::openbao::Openbao;
use super::work::{self, rfc3339_now};
use super::{AppState, SharedState};

/// How often the policy files are read. Fifteen minutes: policy changes arrive through reviewed pull
/// requests, and every read costs one OpenBao read and one GitHub read per listed file.
pub const REFRESH_INTERVAL: Duration = Duration::from_secs(900);
/// Policy files the page lists. The first ones by path are shown when the tree holds more.
pub const MAX_FILES: usize = 80;
/// GitHub requests one refresh may send. Its own count is the tip and the tree, then one raw read and one
/// commit read per Rego file and one commit read per other file. With `MAX_FILES` the most is 2 + 2 * 80,
/// so this bound is a backstop that a test lowers to prove it holds.
pub const MAX_REQUESTS: usize = 200;

/// One policy file as `GET /v1/policies` carries it.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct PolicyFile {
    /// The repository path, such as `platform/services/opa/deployment/policies/system/authz.rego`.
    pub path: String,
    /// `opa-policy`, `opa-test`, `opa-data` or `openbao`.
    pub kind: &'static str,
    /// The Rego package, the HCL file stem, or the file name.
    pub name: String,
    /// The newest commit that touched the file on the ref, or `null` when the ref has none.
    pub last_commit: Option<LastCommit>,
    /// The file's page on GitHub, on the ref.
    pub url: String,
}

/// The last read that finished.
struct Good {
    repo: String,
    git_ref: String,
    files: Vec<PolicyFile>,
    truncated: bool,
    refreshed_at: String,
}

/// What `GET /v1/policies` reads. `good` is the last read that finished. `failure` is why the newest read
/// failed, as its reason only. A failed read leaves `good` in place, and a read that finishes clears `failure`.
pub struct PoliciesCache {
    configured: bool,
    good: Option<Good>,
    failure: Option<String>,
}

/// `GET /v1/policies`. `repo` and `ref` name the source of the list shown, and are `null` until a read has
/// finished.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct PoliciesBody {
    pub state: &'static str,
    pub repo: Option<String>,
    #[serde(rename = "ref")]
    pub git_ref: Option<String>,
    pub files: Vec<PolicyFile>,
    pub refreshed_at: Option<String>,
    pub error: Option<String>,
}

/// A failure reason without its final full stop. The reasons GitHub's refusals carry end in one, and the
/// sentence the page shows adds its own, so a reason is never shown with two.
fn reason_text(reason: &str) -> &str {
    reason.trim_end_matches('.')
}

/// The sentence a read cut short by `MAX_FILES` shows. It names the bound the list was cut at.
fn truncated_sentence() -> String {
    format!("More than {MAX_FILES} policy files; the first {MAX_FILES} are shown.")
}

impl PoliciesCache {
    /// `configured` is whether OpenBao and the GitHub source are both set. When they are not, no read runs and
    /// the page says so.
    pub fn new(configured: bool) -> Self {
        Self {
            configured,
            good: None,
            failure: None,
        }
    }

    /// Stores a finished read and clears the failure. A read cut short by the file bound keeps its files, and the
    /// body says so.
    pub fn record_ok(&mut self, source: &GithubConfig, listing: Listing, refreshed_at: String) {
        self.failure = None;
        let files = listing
            .files
            .into_iter()
            .map(|found| PolicyFile {
                path: found.path,
                kind: found.kind.as_str(),
                name: found.name,
                last_commit: found.last_commit,
                url: found.url,
            })
            .collect();
        self.good = Some(Good {
            repo: source.repo.clone(),
            git_ref: source.git_ref.clone(),
            files,
            truncated: listing.truncated,
            refreshed_at,
        });
    }

    /// Keeps the last good list, if there is one, and stores the reason this read failed.
    pub fn record_error(&mut self, reason: String) {
        self.failure = Some(reason);
    }

    /// The sentence the page shows under the list, or none. A failed read names its reason, and says whether the
    /// last good read is still shown. A read cut short names the bound. A failure takes precedence over the
    /// truncation sentence, since the failed read is the newer fact.
    fn error_sentence(&self) -> Option<String> {
        match (&self.failure, &self.good) {
            (Some(reason), Some(_)) => Some(format!(
                "The newest policy read failed: {}. Showing the last good read.",
                reason_text(reason)
            )),
            (Some(reason), None) => {
                Some(format!("The policy read failed: {}.", reason_text(reason)))
            }
            (None, Some(good)) if good.truncated => Some(truncated_sentence()),
            (None, _) => None,
        }
    }

    /// The body `GET /v1/policies` serves. `state` is `unconfigured` without the source, `loading` before the first
    /// read finishes, `error` when no read has finished and the newest failed, and `ok` otherwise.
    pub fn body(&self) -> PoliciesBody {
        let state = match (self.configured, &self.good) {
            (false, _) => "unconfigured",
            (true, Some(_)) => "ok",
            (true, None) if self.failure.is_some() => "error",
            (true, None) => "loading",
        };
        let (repo, git_ref, files, refreshed_at) = match &self.good {
            Some(good) => (
                Some(good.repo.clone()),
                Some(good.git_ref.clone()),
                good.files.clone(),
                Some(good.refreshed_at.clone()),
            ),
            None => (None, None, Vec::new(), None),
        };
        PoliciesBody {
            state,
            repo,
            git_ref,
            files,
            refreshed_at,
            error: self.error_sentence(),
        }
    }
}

/// `GET /v1/policies` (platform admins only, as `/v1/secrets`). Reads the cache, never OpenBao or GitHub.
pub async fn get_policies(State(app): State<SharedState>, session: Session) -> Response {
    if let Err(response) = work::admin_only(&session).await {
        return response;
    }
    let cache = app.policies.read().await;
    Json(cache.body()).into_response()
}

/// Reads the policy files once and stores the result. Does nothing unless OpenBao and the GitHub source are both
/// configured. The lock is held only to store, never across the reads.
pub async fn refresh(app: &AppState) {
    refresh_from(app, GITHUB_API).await;
}

/// `refresh` against `api`. Production passes `GITHUB_API`; tests pass a local origin.
async fn refresh_from(app: &AppState, api: &str) {
    let (Some(bao), Some(source)) = (app.openbao.as_ref(), app.config.github.as_ref()) else {
        return;
    };
    let result = read(bao, &app.http, api, source).await;
    let mut cache = app.policies.write().await;
    match result {
        Ok(listing) => cache.record_ok(source, listing, rfc3339_now()),
        Err(err) => {
            let text = err.to_string();
            log_failure(cache.failure.as_deref(), &text);
            cache.record_error(text);
        }
    }
}

/// One read: the token from OpenBao now, then the listing from GitHub with it.
async fn read(
    bao: &Openbao,
    http: &reqwest::Client,
    api: &str,
    source: &GithubConfig,
) -> Result<Listing, FetchError> {
    let token = token(bao, http).await?;
    github::list(http, api, &token, source, MAX_FILES, MAX_REQUESTS).await
}

/// The console's GitHub token: the `token` field at `secret/services/console/github`, read now. It is
/// refreshed in OpenBao by `refresh-console-github-token.yml`, so a value read earlier may be spent.
async fn token(bao: &Openbao, http: &reqwest::Client) -> Result<Secret, FetchError> {
    let data = bao
        .read_console_secret(http, "github")
        .await
        .map_err(FetchError::OpenBao)?;
    data.get("token")
        .and_then(Value::as_str)
        .filter(|token| !token.is_empty())
        .map(Secret::new)
        .ok_or(FetchError::TokenMissing)
}

/// One line per distinct failure, not one per read while it lasts.
fn log_failure(previous: Option<&str>, text: &str) {
    if previous != Some(text) {
        eprintln!("console: GitHub policy read failed: {text}");
    }
}

/// Starts the policy read when OpenBao and the GitHub source are configured. The first read runs at once. A missed
/// tick is skipped, not replayed.
pub fn spawn_refresh(state: SharedState) {
    if state.openbao.is_none() || state.config.github.is_none() {
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
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Arc;

    use dioxus::server::axum::extract::State as AxumState;
    use dioxus::server::axum::http::{StatusCode, Uri};
    use dioxus::server::axum::routing::{get, post};
    use dioxus::server::axum::Router;
    use serde_json::json;

    use crate::server::auth;
    use crate::server::config::{Config, OpenbaoConfig};
    use crate::server::inventory::{FlowSet, InventoryCache};
    use crate::server::openbao::Openbao;
    use crate::server::prefs::Prefs;
    use crate::server::scope::Scope;
    use crate::server::{api_router, session_layer};

    const ROLE_ID: &str = "console-role-test";
    const SECRET_ID: &str = "console-secret-test";
    /// The token the fake OpenBao holds. Not shaped like a real GitHub token.
    const TOKEN: &str = "console-github-canary-token";
    const SHA_TIP: &str = "5f1c2a9d0e7b4c3a2b1d0e9f8a7b6c5d4e3f2a1b";
    const TREE_SHA: &str = "aa262c85779bdf49d3a69e8e937dc4631f614cdc";
    const SCOPE: &str = r#"{"example": true, "tenants": []}"#;

    /// A fake OpenBao: AppRole login and the console's GitHub secret. It counts every read of it.
    struct FakeBao {
        /// The `token` field it answers with. `None` omits the field.
        token: Option<String>,
        /// When non-zero, the secret read answers with this status.
        fail_with: AtomicUsize,
        reads: AtomicUsize,
    }

    async fn bao_login() -> (StatusCode, Json<Value>) {
        (
            StatusCode::OK,
            Json(json!({"auth": {
                "client_token": "tok",
                "lease_duration": 1800,
                "num_uses": 0,
            }})),
        )
    }

    async fn bao_read(
        AxumState(fake): AxumState<Arc<FakeBao>>,
        uri: Uri,
    ) -> (StatusCode, Json<Value>) {
        if uri.path() != "/v1/secret/data/services/console/github" {
            return (StatusCode::NOT_FOUND, Json(json!({"errors": []})));
        }
        fake.reads.fetch_add(1, Ordering::SeqCst);
        let forced = fake.fail_with.load(Ordering::SeqCst);
        if forced != 0 {
            let status = StatusCode::from_u16(forced as u16).expect("status");
            return (status, Json(json!({"errors": ["denied"]})));
        }
        let data = match &fake.token {
            Some(token) => json!({"token": token, "refreshed_at": "2026-11-04T00:00:00Z"}),
            None => json!({"refreshed_at": "2026-11-04T00:00:00Z"}),
        };
        (StatusCode::OK, Json(json!({"data": {"data": data}})))
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

    async fn fake_bao(token: Option<&str>) -> (Arc<FakeBao>, Openbao) {
        let fake = Arc::new(FakeBao {
            token: token.map(str::to_string),
            fail_with: AtomicUsize::new(0),
            reads: AtomicUsize::new(0),
        });
        let app = Router::new()
            .route("/v1/auth/approle/login", post(bao_login))
            .route("/v1/{*rest}", get(bao_read))
            .with_state(fake.clone());
        let addr = serve(app).await;
        let client = Openbao::new(OpenbaoConfig {
            api_url: format!("http://{addr}"),
            role_id: Secret::new(ROLE_ID),
            secret_id: Secret::new(SECRET_ID),
        })
        .expect("client");
        (fake, client)
    }

    /// A fake GitHub that serves the tree of a small repository. Every request reaches `github::list` as
    /// the real client sends it, so these tests run the same code the refresh runs.
    async fn fake_github() -> String {
        use dioxus::server::axum::extract::Query;
        async fn commits(
            Query(query): Query<std::collections::BTreeMap<String, String>>,
        ) -> Json<Value> {
            if query.contains_key("path") {
                return Json(json!([]));
            }
            Json(json!([{
                "sha": SHA_TIP,
                "commit": {
                    "message": "tip",
                    "committer": {"date": "2026-11-01T00:00:00Z"},
                    "tree": {"sha": TREE_SHA}
                }
            }]))
        }
        async fn tree() -> Json<Value> {
            Json(json!({"truncated": false, "tree": [
                {"path": "platform/services/openbao/deployment/config/policies/console.hcl", "type": "blob"},
            ]}))
        }
        async fn contents() -> (StatusCode, String) {
            (StatusCode::OK, "package x\n".to_string())
        }
        let app = Router::new()
            .route("/repos/{owner}/{name}/commits", get(commits))
            .route("/repos/{owner}/{name}/git/trees/{tree}", get(tree))
            .route("/repos/{owner}/{name}/contents/{*path}", get(contents));
        format!("http://{}", serve(app).await)
    }

    fn source() -> GithubConfig {
        GithubConfig {
            repo: "uhstray-io/agent-cloud".into(),
            git_ref: "dev".into(),
        }
    }

    /// The application state. OpenBao and the GitHub source are both present when `openbao` is given and
    /// `github` is `Some`, so the cache is configured exactly when a read can run.
    fn state(openbao: Option<Openbao>, github: Option<GithubConfig>) -> SharedState {
        let configured = openbao.is_some() && github.is_some();
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
                grafana_url: None,
                authentik: None,
                role_map_file: None,
                github,
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
            openbao,
            secrets: tokio::sync::RwLock::new(crate::server::secrets::SecretsCache::new(false)),
            access: tokio::sync::RwLock::new(crate::server::access::AccessCache::new(false)),
            role_map: crate::server::access::RoleMap::empty(),
            policies: tokio::sync::RwLock::new(PoliciesCache::new(configured)),
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

    /// Serves the API over `state` and returns its address and a session cookie for a user in `groups`.
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
    fn the_cache_is_unconfigured_without_a_source_and_loading_before_a_read() {
        let body = PoliciesCache::new(false).body();
        assert_eq!(body.state, "unconfigured");
        assert!(body.files.is_empty() && body.error.is_none());
        assert_eq!(body.repo, None);
        assert_eq!(body.git_ref, None);

        let body = PoliciesCache::new(true).body();
        assert_eq!(body.state, "loading", "no read has finished yet");
    }

    #[test]
    fn a_failed_read_with_no_good_list_is_an_error_naming_its_reason() {
        let mut cache = PoliciesCache::new(true);
        cache.record_error("GitHub answered HTTP 401".into());
        let body = cache.body();
        assert_eq!(body.state, "error");
        assert_eq!(
            body.error.as_deref(),
            Some("The policy read failed: GitHub answered HTTP 401.")
        );
        assert!(body.files.is_empty() && body.refreshed_at.is_none());
    }

    #[test]
    fn a_reason_that_already_ends_in_a_full_stop_is_shown_with_one() {
        let refused = "GitHub refused the console token (HTTP 403): the App is not permitted to read this repository.";
        let mut cache = PoliciesCache::new(true);
        cache.record_error(refused.into());
        assert_eq!(
            cache.body().error.as_deref(),
            Some("The policy read failed: GitHub refused the console token (HTTP 403): the App is not permitted to read this repository.")
        );

        let mut cache = PoliciesCache::new(true);
        cache.record_ok(
            &source(),
            Listing {
                files: Vec::new(),
                truncated: false,
            },
            "2026-11-04T10:00:00Z".into(),
        );
        cache.record_error(refused.into());
        assert_eq!(
            cache.body().error.as_deref(),
            Some("The newest policy read failed: GitHub refused the console token (HTTP 403): the App is not permitted to read this repository. Showing the last good read.")
        );
    }

    #[test]
    fn a_read_cut_short_by_the_file_bound_keeps_its_files_and_says_so() {
        let mut cache = PoliciesCache::new(true);
        let listing = Listing {
            files: Vec::new(),
            truncated: true,
        };
        cache.record_ok(&source(), listing, "2026-11-04T10:00:00Z".into());
        let body = cache.body();
        assert_eq!(body.state, "ok");
        assert_eq!(
            body.error.as_deref(),
            Some("More than 80 policy files; the first 80 are shown.")
        );
        assert_eq!(body.repo.as_deref(), Some("uhstray-io/agent-cloud"));
        assert_eq!(body.git_ref.as_deref(), Some("dev"));
    }

    #[tokio::test]
    async fn no_openbao_or_no_github_source_means_no_read_and_an_unconfigured_page() {
        let (bao_fake, bao) = fake_bao(Some(TOKEN)).await;
        let app = state(None, Some(source()));
        refresh(&app).await;
        assert_eq!(app.policies.read().await.body().state, "unconfigured");

        let app = state(Some(bao), None);
        refresh(&app).await;
        assert_eq!(app.policies.read().await.body().state, "unconfigured");

        assert_eq!(
            bao_fake.reads.load(Ordering::SeqCst),
            0,
            "an unconfigured page reads no token"
        );
    }

    #[tokio::test]
    async fn a_refresh_lists_the_policy_files_and_serves_them() {
        let (_bao_fake, bao) = fake_bao(Some(TOKEN)).await;
        let api = fake_github().await;
        let app = state(Some(bao), Some(source()));
        refresh_from(&app, &api).await;

        let body = app.policies.read().await.body();
        assert_eq!(body.state, "ok");
        assert_eq!(body.error, None);
        assert_eq!(body.files.len(), 1);
        assert_eq!(body.files[0].kind, "openbao");
        assert_eq!(body.files[0].name, "console");
        assert_eq!(
            body.files[0].last_commit.as_ref().map(|c| c.sha.as_str()),
            None,
            "the fake has no commit for the file, so none is shown"
        );
        assert!(body.refreshed_at.is_some());
    }

    #[tokio::test]
    async fn a_failed_read_keeps_the_last_good_list_and_says_why_then_heals() {
        let (bao_fake, bao) = fake_bao(Some(TOKEN)).await;
        let api = fake_github().await;
        let app = state(Some(bao), Some(source()));
        refresh_from(&app, &api).await;
        let first = app.policies.read().await.body();
        assert_eq!(first.state, "ok");

        // OpenBao now refuses the secret read. The page keeps the list and names the status.
        bao_fake.fail_with.store(500, Ordering::SeqCst);
        refresh_from(&app, &api).await;
        let stale = app.policies.read().await.body();
        assert_eq!(stale.state, "ok", "the last good list is still served");
        assert_eq!(stale.files, first.files, "the list is kept, not cleared");
        assert_eq!(
            stale.refreshed_at, first.refreshed_at,
            "the stamp is the older read's"
        );
        assert_eq!(
            stale.error.as_deref(),
            Some("The newest policy read failed: OpenBao answered HTTP 500. Showing the last good read.")
        );

        // The next good read clears the failure.
        bao_fake.fail_with.store(0, Ordering::SeqCst);
        refresh_from(&app, &api).await;
        assert_eq!(app.policies.read().await.body().error, None);
    }

    #[tokio::test]
    async fn a_missing_token_field_is_its_own_sentence_and_no_request_is_sent() {
        let (_bao_fake, bao) = fake_bao(None).await;
        let api = fake_github().await;
        let app = state(Some(bao), Some(source()));
        refresh_from(&app, &api).await;
        let body = app.policies.read().await.body();
        assert_eq!(body.state, "error");
        assert_eq!(
            body.error.as_deref(),
            Some("The policy read failed: the GitHub token is missing or empty in OpenBao.")
        );
        assert!(!body.error.unwrap_or_default().contains(TOKEN));
    }

    #[tokio::test]
    async fn the_token_never_reaches_a_body_or_an_error() {
        // The token is in the fake OpenBao. A refused GitHub read must name a status only.
        let (_bao_fake, bao) = fake_bao(Some(TOKEN)).await;
        let app = state(Some(bao), Some(source()));
        refresh_from(&app, "http://127.0.0.1:9").await;
        let body = app.policies.read().await.body();
        let text = serde_json::to_string(&body).expect("json");
        assert_eq!(body.state, "error");
        assert!(!text.contains(TOKEN), "{text}");
        assert!(!body.error.unwrap_or_default().contains(TOKEN));
    }

    #[tokio::test]
    async fn members_get_403_signed_out_gets_401_and_platform_admins_get_the_answer() {
        let (_bao_fake, bao) = fake_bao(Some(TOKEN)).await;
        let api = fake_github().await;
        let app = state(Some(bao), Some(source()));
        refresh_from(&app, &api).await;

        for groups in [&["ops"][..], &["platform-admins-old", "users"][..]] {
            let (addr, cookie) = app_for(app.clone(), groups).await;
            let (status, body) = fetch(addr, "/v1/policies", Some(&cookie)).await;
            assert_eq!(status, 403, "{groups:?}");
            assert_eq!(body, r#"{"error":"forbidden"}"#, "{groups:?}");
        }

        let (addr, _) = app_for(app.clone(), &["platform-admins"]).await;
        let (status, body) = fetch(addr, "/v1/policies", None).await;
        assert_eq!(status, 401);
        assert_eq!(body, r#"{"error":"unauthenticated"}"#);

        let (addr, cookie) = app_for(app.clone(), &["platform-admins"]).await;
        let (status, body) = fetch(addr, "/v1/policies", Some(&cookie)).await;
        assert_eq!(status, 200);
        let value: Value = serde_json::from_str(&body).expect("json");
        assert_eq!(value["state"], "ok");
        assert_eq!(value["ref"], "dev");
        assert_eq!(value["repo"], "uhstray-io/agent-cloud");
        assert_eq!(value["files"][0]["kind"], "openbao");
        assert!(!body.contains(TOKEN), "{body}");
    }

    #[tokio::test]
    async fn an_unconfigured_page_is_answered_without_a_read_or_a_token() {
        let app = state(None, None);
        let (addr, cookie) = app_for(app.clone(), &["platform-admins"]).await;
        let (status, body) = fetch(addr, "/v1/policies", Some(&cookie)).await;
        assert_eq!(status, 200);
        let value: Value = serde_json::from_str(&body).expect("json");
        assert_eq!(value["state"], "unconfigured");
        assert_eq!(value["files"], json!([]));
        assert!(value["repo"].is_null() && value["ref"].is_null());
    }
}
