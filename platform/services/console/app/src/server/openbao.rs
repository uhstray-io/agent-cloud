//! OpenBao client for the console's run-time reads (plan 17 D27). The console logs in with its
//! own AppRole, lazily on the first call, and again when its token has a minute or less left, has
//! no uses left, or is refused with HTTP 403. The grants it runs under are in
//! `platform/services/openbao/deployment/config/policies/console.hcl`: read on
//! `secret/data/services/console/*`, list and read on `secret/metadata/*`, and nothing else.
//!
//! Boot never depends on OpenBao: nothing is sent until the first call. Errors are fixed
//! variants. No URL, role id, secret id, token or response body reaches an error or a log line.

use std::fmt;
use std::time::{Duration, Instant};

use serde_json::{json, Value};

use super::config::{OpenbaoConfig, Secret};

/// A token with this much life or less is not reused, so a request is never sent just before
/// the token expires.
const REFRESH_MARGIN: Duration = Duration::from_secs(60);

/// A login token, when it stops being usable, and how many uses it has left.
pub struct Lease {
    token: Secret,
    expires_at: Instant,
    /// `None` when the token has no use limit.
    uses_left: Option<u32>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OpenbaoError {
    /// The AppRole login was refused with this HTTP status.
    Login(u16),
    /// A read answered with this HTTP status.
    Status(u16),
    /// The request did not complete.
    Transport,
    /// A response was not the shape the console reads.
    Decode,
    /// A caller path is empty or has a `.` or `..` segment, in any letter case or percent-encoded form.
    BadPath,
    /// The configured address does not parse as a URL.
    Url,
}

impl fmt::Display for OpenbaoError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            OpenbaoError::Login(code) => {
                write!(f, "OpenBao refused the console login (HTTP {code})")
            }
            OpenbaoError::Status(code) => write!(f, "OpenBao answered HTTP {code}"),
            OpenbaoError::Transport => f.write_str("cannot reach OpenBao"),
            OpenbaoError::Decode => f.write_str("OpenBao response could not be decoded"),
            OpenbaoError::BadPath => f.write_str("OpenBao path is not a valid secret path"),
            OpenbaoError::Url => f.write_str("OpenBao address is not a valid URL"),
        }
    }
}

/// Reads a login reply. `lease_duration` is required and must be positive. An absent or zero
/// `num_uses` means unlimited: an exhausted limit that was not reported is caught by the 403 retry.
pub fn parse_login(reply: &Value, now: Instant) -> Result<Lease, OpenbaoError> {
    let auth = reply.get("auth").ok_or(OpenbaoError::Decode)?;
    let token = auth
        .get("client_token")
        .and_then(Value::as_str)
        .filter(|token| !token.is_empty())
        .ok_or(OpenbaoError::Decode)?;
    let ttl = auth
        .get("lease_duration")
        .and_then(Value::as_u64)
        .filter(|ttl| *ttl > 0)
        .ok_or(OpenbaoError::Decode)?;
    let expires_at = now
        .checked_add(Duration::from_secs(ttl))
        .ok_or(OpenbaoError::Decode)?;
    let uses = auth.get("num_uses").and_then(Value::as_u64).unwrap_or(0);
    Ok(Lease {
        token: Secret::new(token),
        expires_at,
        uses_left: (uses > 0).then(|| u32::try_from(uses).unwrap_or(u32::MAX)),
    })
}

/// True while the token has more than `REFRESH_MARGIN` left and at least one use.
pub fn lease_usable(lease: &Lease, now: Instant) -> bool {
    lease.expires_at.saturating_duration_since(now) > REFRESH_MARGIN && lease.uses_left != Some(0)
}

/// An empty segment, or `.` or `..` in any letter case or percent-encoded form (`%2e%2e`,
/// `.%2E`). The url crate resolves those itself, so a caller path must not reach it.
fn is_unsafe_segment(segment: &str) -> bool {
    let folded = segment.to_ascii_lowercase().replace("%2e", ".");
    folded.is_empty() || folded == "." || folded == ".."
}

/// The console's OpenBao client. One token is held at a time, behind an async mutex that is also
/// held across a login, so concurrent callers share one login instead of racing to make several.
pub struct Openbao {
    config: OpenbaoConfig,
    base: reqwest::Url,
    lease: tokio::sync::Mutex<Option<Lease>>,
}

impl Openbao {
    /// Builds the client. Nothing is sent: the first call logs in.
    pub fn new(config: OpenbaoConfig) -> Result<Self, OpenbaoError> {
        let base = reqwest::Url::parse(&config.api_url).map_err(|_| OpenbaoError::Url)?;
        Ok(Openbao {
            config,
            base,
            lease: tokio::sync::Mutex::new(None),
        })
    }

    /// The keys under `secret/metadata/<path>`. An empty `path` lists the KV root,
    /// `secret/metadata/`, the one place an empty path is allowed. OpenBao answers 404 for a LIST
    /// with no keys, which is an empty list here. Every other status is an error.
    pub async fn list_metadata(
        &self,
        http: &reqwest::Client,
        path: &str,
    ) -> Result<Vec<String>, OpenbaoError> {
        let mut url = if path.is_empty() {
            self.build(&["v1", "secret", "metadata"], &[""])?
        } else {
            self.url(&["v1", "secret", "metadata"], path)?
        };
        url.query_pairs_mut().append_pair("list", "true");
        let body = match self.read(http, url).await {
            Ok(body) => body,
            // OpenBao answers 404 for a LIST with no keys.
            Err(OpenbaoError::Status(404)) => return Ok(Vec::new()),
            Err(err) => return Err(err),
        };
        body.pointer("/data/keys")
            .and_then(Value::as_array)
            .ok_or(OpenbaoError::Decode)?
            .iter()
            .map(|key| key.as_str().map(str::to_string).ok_or(OpenbaoError::Decode))
            .collect()
    }

    /// The `data` object of `GET secret/metadata/<path>`: versions and timestamps, never values.
    pub async fn read_metadata(
        &self,
        http: &reqwest::Client,
        path: &str,
    ) -> Result<Value, OpenbaoError> {
        let url = self.url(&["v1", "secret", "metadata"], path)?;
        let body = self.read(http, url).await?;
        body.get("data").cloned().ok_or(OpenbaoError::Decode)
    }

    /// The `data.data` object at `secret/data/services/console/<name>`: the secret values.
    // Consumed by the GitHub token read (Phase 6).
    #[allow(dead_code)]
    pub async fn read_console_secret(
        &self,
        http: &reqwest::Client,
        name: &str,
    ) -> Result<Value, OpenbaoError> {
        let url = self.url(&["v1", "secret", "data", "services", "console"], name)?;
        let body = self.read(http, url).await?;
        body.pointer("/data/data")
            .cloned()
            .ok_or(OpenbaoError::Decode)
    }

    /// One GET with the console's token. A 403 drops that token and retries once on a fresh
    /// login; a second 403 is an error.
    async fn read(&self, http: &reqwest::Client, url: reqwest::Url) -> Result<Value, OpenbaoError> {
        let mut retried = false;
        loop {
            let token = self.token(http).await?;
            let response = http
                .get(url.clone())
                .header("X-Vault-Token", token.expose())
                .send()
                .await
                .map_err(|_| OpenbaoError::Transport)?;
            let status = response.status();
            if status == reqwest::StatusCode::FORBIDDEN && !retried {
                self.forget(&token).await;
                retried = true;
                continue;
            }
            if !status.is_success() {
                return Err(OpenbaoError::Status(status.as_u16()));
            }
            let bytes = response
                .bytes()
                .await
                .map_err(|_| OpenbaoError::Transport)?;
            return serde_json::from_slice(&bytes).map_err(|_| OpenbaoError::Decode);
        }
    }

    /// The token for the next request, logging in first when the held one is not usable. Hands
    /// out one use of it.
    async fn token(&self, http: &reqwest::Client) -> Result<Secret, OpenbaoError> {
        let mut slot = self.lease.lock().await;
        let lease = match slot.take() {
            Some(lease) if lease_usable(&lease, Instant::now()) => lease,
            _ => self.login(http).await?,
        };
        let token = lease.token.clone();
        *slot = Some(Lease {
            token: lease.token,
            expires_at: lease.expires_at,
            uses_left: lease.uses_left.map(|left| left.saturating_sub(1)),
        });
        Ok(token)
    }

    /// Drops the lease `token` came from. A lease that a concurrent caller has already replaced
    /// stays.
    async fn forget(&self, token: &Secret) {
        let mut slot = self.lease.lock().await;
        if slot.as_ref().is_some_and(|lease| lease.token == *token) {
            *slot = None;
        }
    }

    /// `POST auth/approle/login` with the AppRole pair.
    async fn login(&self, http: &reqwest::Client) -> Result<Lease, OpenbaoError> {
        let url = self.url(&["v1", "auth", "approle"], "login")?;
        let body = json!({
            "role_id": self.config.role_id.expose(),
            "secret_id": self.config.secret_id.expose(),
        });
        let response = http
            .post(url)
            .json(&body)
            .send()
            .await
            .map_err(|_| OpenbaoError::Transport)?;
        let status = response.status();
        if !status.is_success() {
            return Err(OpenbaoError::Login(status.as_u16()));
        }
        let bytes = response
            .bytes()
            .await
            .map_err(|_| OpenbaoError::Transport)?;
        let reply: Value = serde_json::from_slice(&bytes).map_err(|_| OpenbaoError::Decode)?;
        parse_login(&reply, Instant::now())
    }

    /// `prefix` followed by the segments of `path`. Each `/`-separated part of `path` is one path
    /// segment, percent-encoded by the url crate, so `?`, `#` and `%` stay inside it. A `.`, a `..`
    /// or an empty part is refused before any URL is built.
    fn url(&self, prefix: &[&str], path: &str) -> Result<reqwest::Url, OpenbaoError> {
        let segments: Vec<&str> = path.split('/').collect();
        if segments.iter().any(|segment| is_unsafe_segment(segment)) {
            return Err(OpenbaoError::BadPath);
        }
        self.build(prefix, &segments)
    }

    /// `prefix` followed by `segments`, as given. Only `url` and the KV root LIST build URLs, and
    /// each checks its own segments first.
    fn build(&self, prefix: &[&str], segments: &[&str]) -> Result<reqwest::Url, OpenbaoError> {
        let mut url = self.base.clone();
        url.path_segments_mut()
            .map_err(|()| OpenbaoError::Url)?
            .pop_if_empty()
            .extend(prefix)
            .extend(segments);
        Ok(url)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::SocketAddr;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::{Arc, Mutex};

    use dioxus::server::axum::extract::State;
    use dioxus::server::axum::http::{HeaderMap, StatusCode, Uri};
    use dioxus::server::axum::routing::{get, post};
    use dioxus::server::axum::{Json, Router};

    const ROLE_ID: &str = "console-role-test";
    const SECRET_ID: &str = "console-secret-test";

    /// A fake OpenBao: AppRole login, KV reads and LIST. Each login issues a token with
    /// `num_uses` uses, and a spent or superseded token answers 403, as a real one does.
    struct Fake {
        /// Uses per login token. 0 is unlimited.
        num_uses: u64,
        reject_login: bool,
        /// Reads answered 403 before anything else, to force the retry path.
        forbid_reads: AtomicUsize,
        logins: AtomicUsize,
        reads: AtomicUsize,
        denied: AtomicUsize,
        /// The live token and its remaining uses (`None` is unlimited).
        live: Mutex<Option<(String, Option<u64>)>>,
        /// Path and query of every read that reached a handler.
        seen: Mutex<Vec<String>>,
    }

    impl Fake {
        fn new(num_uses: u64) -> Self {
            Fake {
                num_uses,
                reject_login: false,
                forbid_reads: AtomicUsize::new(0),
                logins: AtomicUsize::new(0),
                reads: AtomicUsize::new(0),
                denied: AtomicUsize::new(0),
                live: Mutex::new(None),
                seen: Mutex::new(Vec::new()),
            }
        }

        fn refusing_logins() -> Self {
            Fake {
                reject_login: true,
                ..Fake::new(0)
            }
        }
    }

    fn count(counter: &AtomicUsize) -> usize {
        counter.load(Ordering::SeqCst)
    }

    fn last_path(fake: &Fake) -> String {
        fake.seen
            .lock()
            .expect("seen")
            .last()
            .cloned()
            .unwrap_or_default()
    }

    async fn fake_login(
        State(fake): State<Arc<Fake>>,
        Json(body): Json<Value>,
    ) -> (StatusCode, Json<Value>) {
        let n = fake.logins.fetch_add(1, Ordering::SeqCst) + 1;
        let credentials_match = body["role_id"] == ROLE_ID && body["secret_id"] == SECRET_ID;
        if fake.reject_login || !credentials_match {
            return (
                StatusCode::BAD_REQUEST,
                Json(json!({"errors": ["invalid role or secret id"]})),
            );
        }
        let token = format!("tok-{n}");
        let remaining = (fake.num_uses > 0).then_some(fake.num_uses);
        *fake.live.lock().expect("live") = Some((token.clone(), remaining));
        (
            StatusCode::OK,
            Json(json!({"auth": {
                "client_token": token,
                "lease_duration": 1800,
                "num_uses": fake.num_uses,
            }})),
        )
    }

    async fn fake_read(
        State(fake): State<Arc<Fake>>,
        uri: Uri,
        headers: HeaderMap,
    ) -> (StatusCode, Json<Value>) {
        fake.seen.lock().expect("seen").push(
            uri.path_and_query()
                .map_or_else(String::new, |pq| pq.as_str().to_string()),
        );
        fake.reads.fetch_add(1, Ordering::SeqCst);
        let forced = fake
            .forbid_reads
            .fetch_update(Ordering::SeqCst, Ordering::SeqCst, |n| n.checked_sub(1))
            .is_ok();
        let presented = headers
            .get("x-vault-token")
            .and_then(|value| value.to_str().ok())
            .unwrap_or_default();
        let valid = !forced && {
            let mut live = fake.live.lock().expect("live");
            match live.as_mut() {
                Some((token, remaining))
                    if token.as_str() == presented && *remaining != Some(0) =>
                {
                    if let Some(left) = remaining.as_mut() {
                        *left -= 1;
                    }
                    true
                }
                _ => false,
            }
        };
        if !valid {
            fake.denied.fetch_add(1, Ordering::SeqCst);
            return (
                StatusCode::FORBIDDEN,
                Json(json!({"errors": ["permission denied"]})),
            );
        }
        let path = uri.path();
        let listing = uri.query() == Some("list=true");
        let (status, body) = match (listing, path) {
            (true, "/v1/secret/metadata/") => (
                StatusCode::OK,
                json!({"data": {"keys": ["services/", "top"]}}),
            ),
            (true, "/v1/secret/metadata/services/console") => (
                StatusCode::OK,
                json!({"data": {"keys": ["alpha", "beta/"]}}),
            ),
            (true, "/v1/secret/metadata/services/empty") => {
                (StatusCode::NOT_FOUND, json!({"errors": []}))
            }
            (true, "/v1/secret/metadata/services/broken") => (
                StatusCode::INTERNAL_SERVER_ERROR,
                json!({"errors": ["boom"]}),
            ),
            (false, p) if p.starts_with("/v1/secret/data/services/console/") => (
                StatusCode::OK,
                json!({"data": {"data": {"value": "hidden-ok"}, "metadata": {"version": 1}}}),
            ),
            (false, p) if p.starts_with("/v1/secret/metadata/") => {
                (StatusCode::OK, json!({"data": {"current_version": 1}}))
            }
            _ => (StatusCode::NOT_FOUND, json!({"errors": []})),
        };
        (status, Json(body))
    }

    fn router(fake: Arc<Fake>) -> Router {
        Router::new()
            .route("/v1/auth/approle/login", post(fake_login))
            .route("/v1/{*rest}", get(fake_read))
            .with_state(fake)
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

    async fn setup(fake: Fake) -> (Arc<Fake>, Openbao, reqwest::Client) {
        let fake = Arc::new(fake);
        let addr = serve(router(fake.clone())).await;
        let config = OpenbaoConfig {
            api_url: format!("http://{addr}"),
            role_id: Secret::new(ROLE_ID),
            secret_id: Secret::new(SECRET_ID),
        };
        let bao = Openbao::new(config).expect("client");
        (fake, bao, reqwest::Client::new())
    }

    #[test]
    fn login_reply_reads_ttl_and_uses() {
        let now = Instant::now();
        let reply =
            json!({"auth": {"client_token": "tok", "lease_duration": 1800, "num_uses": 25}});
        let lease = parse_login(&reply, now).expect("lease");
        assert_eq!(lease.token.expose(), "tok");
        assert_eq!(lease.expires_at, now + Duration::from_secs(1800));
        assert_eq!(lease.uses_left, Some(25));
    }

    #[test]
    fn zero_or_absent_num_uses_is_unlimited() {
        let now = Instant::now();
        for reply in [
            json!({"auth": {"client_token": "t", "lease_duration": 60, "num_uses": 0}}),
            json!({"auth": {"client_token": "t", "lease_duration": 60}}),
        ] {
            let lease = parse_login(&reply, now).expect("lease");
            assert_eq!(lease.uses_left, None, "{reply}");
        }
    }

    #[test]
    fn login_reply_without_a_token_or_a_positive_ttl_is_a_decode_error() {
        let now = Instant::now();
        for reply in [
            json!({}),
            json!({"auth": {"lease_duration": 60, "num_uses": 0}}),
            json!({"auth": {"client_token": "", "lease_duration": 60}}),
            json!({"auth": {"client_token": "t"}}),
            json!({"auth": {"client_token": "t", "lease_duration": 0}}),
            json!({"auth": {"client_token": "t", "lease_duration": "60"}}),
        ] {
            assert_eq!(
                parse_login(&reply, now).err(),
                Some(OpenbaoError::Decode),
                "{reply}"
            );
        }
    }

    #[test]
    fn lease_needs_more_than_a_minute_and_a_remaining_use() {
        let now = Instant::now();
        let lease = |secs: u64, uses: Option<u32>| Lease {
            token: Secret::new("t"),
            expires_at: now + Duration::from_secs(secs),
            uses_left: uses,
        };
        assert!(lease_usable(&lease(61, None), now), "61 s left is usable");
        assert!(!lease_usable(&lease(60, None), now), "60 s left is not");
        assert!(!lease_usable(&lease(59, None), now), "59 s left is not");
        assert!(lease_usable(&lease(1800, Some(1)), now));
        assert!(!lease_usable(&lease(1800, Some(0)), now), "no uses left");
        assert!(lease_usable(&lease(1800, None), now), "unlimited");
    }

    #[test]
    fn unparseable_address_is_refused_at_construction() {
        let config = OpenbaoConfig {
            api_url: "not a url".to_string(),
            role_id: Secret::new(ROLE_ID),
            secret_id: Secret::new(SECRET_ID),
        };
        assert_eq!(Openbao::new(config).err(), Some(OpenbaoError::Url));
    }

    #[tokio::test]
    async fn first_call_logs_in_then_later_calls_reuse_the_token() {
        let (fake, bao, http) = setup(Fake::new(0)).await;
        assert_eq!(count(&fake.logins), 0, "boot does not log in");

        let value = bao
            .read_console_secret(&http, "github")
            .await
            .expect("first read");
        assert_eq!(value["value"], "hidden-ok");
        bao.read_console_secret(&http, "github")
            .await
            .expect("second read");

        assert_eq!(count(&fake.logins), 1, "one login serves both reads");
        assert_eq!(count(&fake.reads), 2);
        assert_eq!(count(&fake.denied), 0);
    }

    #[tokio::test]
    async fn spent_token_forces_a_new_login_before_the_next_read() {
        let (fake, bao, http) = setup(Fake::new(2)).await;
        for _ in 0..3 {
            bao.read_console_secret(&http, "github")
                .await
                .expect("read");
        }
        assert_eq!(count(&fake.logins), 2, "the third read logs in again");
        assert_eq!(count(&fake.reads), 3);
        assert_eq!(count(&fake.denied), 0, "re-login is proactive, not a 403");
    }

    #[tokio::test]
    async fn forbidden_read_logs_in_once_and_retries() {
        let (fake, bao, http) = setup(Fake::new(0)).await;
        fake.forbid_reads.store(1, Ordering::SeqCst);
        let value = bao
            .read_console_secret(&http, "github")
            .await
            .expect("retry succeeds");
        assert_eq!(value["value"], "hidden-ok");
        assert_eq!(count(&fake.logins), 2);
        assert_eq!(count(&fake.reads), 2);
        assert_eq!(count(&fake.denied), 1);
    }

    #[tokio::test]
    async fn second_forbidden_read_is_a_status_error_after_one_relogin() {
        let (fake, bao, http) = setup(Fake::new(0)).await;
        fake.forbid_reads.store(usize::MAX, Ordering::SeqCst);
        let err = bao
            .read_console_secret(&http, "github")
            .await
            .expect_err("must fail");
        assert_eq!(err, OpenbaoError::Status(403));
        assert_eq!(count(&fake.logins), 2, "exactly one re-login");
        assert_eq!(count(&fake.reads), 2);
    }

    #[tokio::test]
    async fn refused_login_names_the_status_and_no_credential() {
        let (fake, bao, http) = setup(Fake::refusing_logins()).await;
        let err = bao
            .read_console_secret(&http, "github")
            .await
            .expect_err("must fail");
        assert_eq!(err, OpenbaoError::Login(400));
        let text = format!("{err} {err:?}");
        assert!(!text.contains(ROLE_ID), "{text}");
        assert!(!text.contains(SECRET_ID), "{text}");
        assert_eq!(count(&fake.logins), 1, "a refused login is not retried");
        assert_eq!(count(&fake.reads), 0);
    }

    #[tokio::test]
    async fn caller_paths_stay_inside_one_segment_and_dot_segments_never_send() {
        let (fake, bao, http) = setup(Fake::new(0)).await;

        // Encoded delimiters stay inside one segment: the server sees them percent-encoded.
        bao.read_metadata(&http, "services/a?x#y%z")
            .await
            .expect("read");
        assert_eq!(
            last_path(&fake),
            "/v1/secret/metadata/services/a%3Fx%23y%25z"
        );

        // A read of the KV root is refused: only LIST may name the root.
        for bad in [
            "",
            "services//x",
            "services/../x",
            "./x",
            "services/.",
            "services/%2e%2e/x",
            "services/.%2E",
            "%2E/x",
        ] {
            let err = bao
                .read_metadata(&http, bad)
                .await
                .expect_err("bad read path must fail");
            assert_eq!(err, OpenbaoError::BadPath, "read {bad:?}");
        }
        let err = bao
            .read_console_secret(&http, "..")
            .await
            .expect_err("dot segment must fail");
        assert_eq!(err, OpenbaoError::BadPath);

        // The one empty path that is allowed: LIST of the KV root, with its trailing slash.
        let keys = bao
            .list_metadata(&http, "")
            .await
            .expect("root list is allowed");
        assert_eq!(keys, ["services/", "top"]);
        assert_eq!(last_path(&fake), "/v1/secret/metadata/?list=true");

        // Every other bad path is still refused by LIST, before any request.
        for bad in [
            "services//x",
            "services/../x",
            "./x",
            "services/.",
            "services/%2e%2e/x",
            "services/.%2E",
            "%2E/x",
        ] {
            let err = bao
                .list_metadata(&http, bad)
                .await
                .expect_err("bad list path must fail");
            assert_eq!(err, OpenbaoError::BadPath, "list {bad:?}");
        }

        // Only the valid read and the root LIST reached the server, and the bad paths logged in never.
        assert_eq!(count(&fake.reads), 2);
        assert_eq!(count(&fake.logins), 1);
    }

    #[tokio::test]
    async fn list_returns_keys_and_an_empty_list_is_not_an_error() {
        let (fake, bao, http) = setup(Fake::new(0)).await;
        let keys = bao
            .list_metadata(&http, "services/console")
            .await
            .expect("list");
        assert_eq!(keys, ["alpha", "beta/"]);
        assert_eq!(
            last_path(&fake),
            "/v1/secret/metadata/services/console?list=true"
        );

        let keys = bao
            .list_metadata(&http, "services/empty")
            .await
            .expect("404 on LIST is an empty list");
        assert!(keys.is_empty());

        let err = bao
            .list_metadata(&http, "services/broken")
            .await
            .expect_err("other statuses pass through");
        assert_eq!(err, OpenbaoError::Status(500));
    }
}
