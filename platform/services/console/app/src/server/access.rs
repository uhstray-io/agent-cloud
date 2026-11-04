//! The Access page's answer (plan 17, 8.2b): the Authentik users and groups the console's view-only
//! directory token can read, and the Access Level -> per-service role map.
//!
//! A background task reads `/api/v3/core/users/` and `/api/v3/core/groups/` every `REFRESH_INTERVAL` and
//! keeps the result in `AccessCache`. `GET /v1/access` only reads that cache, so a page load costs no
//! Authentik request. A failed read keeps the last good directory and says why. The client sends the
//! console's token to the configured origin only and never follows a redirect. No URL, response body or
//! token reaches an error or a log line.
//!
//! The role map is read once at startup from `CONSOLE_ROLE_MAP_FILE`. It is the authoritative table as
//! data: deploy-console.yml renders it from `platform/playbooks/vars/access-role-map.yml`, and
//! `platform/tests/test_console_access_role_map.py` pins the file against `plan/architecture/04-credentials-access.md`
//! and pins these serde names against the rendered JSON.
//!
//! Authentik API fields, from the 2024.12.3 schema (`https://raw.githubusercontent.com/goauthentik/authentik/version/2024.12.3/schema.yml`):
//! - `/core/users/` (schema.yml:5267): query `include_groups` (default true), `page`, `page_size`.
//!   `PaginatedUserList` (schema.yml:47690) holds `pagination` and `results`.
//! - `User` (schema.yml:55835): `username`, `name`, `email`, `is_active`, `type` (`UserTypeEnum`),
//!   `groups_obj` (nullable list of `UserGroup`, each with `name`).
//! - `/core/groups/` (schema.yml:4331): query `include_users`. The reader sends `false`: `users`, the integer
//!   pks of the direct members that the member count reads, is always serialized, and only `users_obj` depends
//!   on the flag (authentik/core/api/groups.py:67-75, 96, 171-175 at the same tag).
//!   `PaginatedGroupList` (schema.yml:46742). `Group` (schema.yml:42872): `name`, `is_superuser`, `users`.
//! - `Pagination` (schema.yml:47810): `next` is the next page number, and 0 when there is none. The server
//!   sets it that way in `authentik/api/pagination.py` at the same tag, `Pagination.get_paginated_response`.

use std::collections::BTreeMap;
use std::fmt;
use std::fs;
use std::path::Path;
use std::time::Duration;

use dioxus::server::axum::extract::State;
use dioxus::server::axum::response::{IntoResponse, Response};
use dioxus::server::axum::Json;
use serde::de::DeserializeOwned;
use serde::{Deserialize, Serialize};
use tokio::time::MissedTickBehavior;
use tower_sessions::Session;

use super::config::AuthentikConfig;
use super::work::{self, rfc3339_now};
use super::{AppState, SharedState};

/// How often the directory is read. Five minutes: users and groups change on a human timescale.
pub const REFRESH_INTERVAL: Duration = Duration::from_secs(300);
/// Entries per list page. Authentik's `Pagination` sets no `max_page_size` (authentik/api/pagination.py),
/// so this size is not capped.
pub const PAGE_SIZE: usize = 100;
/// Pages read per list. A list longer than `PAGE_SIZE * MAX_PAGES` is cut, and the answer says so.
pub const MAX_PAGES: usize = 20;

/// The sentence the page shows when a list ran past `MAX_PAGES`. The list is shown as far as it was read.
fn truncated_sentence() -> String {
    let limit = PAGE_SIZE * MAX_PAGES;
    format!("Authentik holds more than {limit} users or groups; the first {limit} are shown.")
}

/// Why one directory read failed. Fixed text only: no URL, no body and no header reaches a message.
#[derive(Debug, PartialEq, Eq)]
pub enum FetchError {
    Status(u16),
    Timeout,
    Connect,
    Other,
    Decode,
}

impl fmt::Display for FetchError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            FetchError::Status(code) => write!(f, "Authentik answered HTTP {code}"),
            FetchError::Timeout => f.write_str("Authentik did not answer in time"),
            FetchError::Connect => f.write_str("cannot connect to Authentik"),
            FetchError::Other => f.write_str("the Authentik request failed"),
            FetchError::Decode => {
                f.write_str("Authentik answered with a body the console does not read")
            }
        }
    }
}

/// One user as the Access page shows it. `service_account` is true for `type` `service_account` or
/// `internal_service_account` (`UserTypeEnum`, schema.yml). The other values, `internal` and `external`, are not.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct User {
    pub username: String,
    pub name: String,
    pub email: Option<String>,
    pub active: bool,
    pub service_account: bool,
    pub groups: Vec<String>,
}

/// One group as the Access page shows it. `members` is the count of its direct members.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Group {
    pub name: String,
    pub superuser: bool,
    pub members: usize,
}

/// One complete directory read. `truncated` is set when either list ran past `MAX_PAGES`.
#[derive(Debug, Clone, PartialEq)]
pub struct Directory {
    pub users: Vec<User>,
    pub groups: Vec<Group>,
    pub truncated: bool,
}

/// The role map: the authoritative table as data (see the module header). Field and variant names are
/// the JSON the server reads, and `deny_unknown_fields` makes a stray key fail startup by name.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RoleMap {
    pub systems: Vec<String>,
    pub levels: Vec<RoleLevel>,
}

/// One access level: its status and its cell text under each system.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RoleLevel {
    pub level: String,
    pub status: Status,
    pub roles: BTreeMap<String, String>,
}

/// `built` when the provisioning exists today, `target` when it is the design target only.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Status {
    Built,
    Target,
}

impl RoleMap {
    /// The map when `CONSOLE_ROLE_MAP_FILE` is unset: no systems and no levels.
    pub fn empty() -> Self {
        RoleMap {
            systems: Vec::new(),
            levels: Vec::new(),
        }
    }
}

/// Reads `CONSOLE_ROLE_MAP_FILE`. A configured file that cannot be read or parsed fails startup.
pub fn load_role_map(path: &Path) -> Result<RoleMap, String> {
    let text = fs::read_to_string(path).map_err(|err| {
        format!(
            "CONSOLE_ROLE_MAP_FILE: cannot read {}: {err}",
            path.display()
        )
    })?;
    serde_json::from_str(&text).map_err(|err| format!("CONSOLE_ROLE_MAP_FILE: invalid JSON: {err}"))
}

// Wire shapes. Only the fields the page shows are declared; serde ignores the rest.

/// `PaginatedUserList` / `PaginatedGroupList`: a page of rows and where the next one starts.
#[derive(Deserialize)]
struct Page<T> {
    pagination: PageInfo,
    results: Vec<T>,
}

/// `Pagination` (schema.yml:47810). `next` is 0 when no page follows.
#[derive(Deserialize)]
struct PageInfo {
    next: i64,
}

/// `User` (schema.yml:55835). `type` is optional in the schema, so a missing one is not a service account.
#[derive(Deserialize)]
struct UserRow {
    username: String,
    #[serde(default)]
    name: String,
    #[serde(default)]
    email: Option<String>,
    is_active: bool,
    #[serde(default, rename = "type")]
    kind: Option<String>,
    #[serde(default)]
    groups_obj: Option<Vec<GroupRef>>,
}

/// `UserGroup` (schema.yml, the element of `groups_obj`): only its name is read.
#[derive(Deserialize)]
struct GroupRef {
    name: String,
}

/// `Group` (schema.yml:42872). `users` holds the pks of the direct members.
#[derive(Deserialize)]
struct GroupRow {
    name: String,
    #[serde(default)]
    is_superuser: bool,
    #[serde(default)]
    users: Vec<i64>,
}

/// Reads every user and group, following `pagination.next` up to `MAX_PAGES` pages per list.
pub async fn fetch(
    http: &reqwest::Client,
    config: &AuthentikConfig,
) -> Result<Directory, FetchError> {
    // `include_users=false` drops only the expanded `users_obj`. The pk list the member count reads is
    // always serialized (see the module header), so the groups list needs no expanded members.
    let (groups, groups_cut) = list::<GroupRow>(
        http,
        config,
        "/api/v3/core/groups/",
        ("include_users", "false"),
    )
    .await?;
    let (users, users_cut) = list::<UserRow>(
        http,
        config,
        "/api/v3/core/users/",
        ("include_groups", "true"),
    )
    .await?;
    Ok(directory(users, groups, groups_cut || users_cut))
}

/// Every row of one list. The next page is asked for with this client's own URL and `page`, never the
/// `next` value as a link, so the bearer goes only to the configured origin. The loop stops on `next` 0,
/// on an empty page, or after `MAX_PAGES`. Reaching the bound means the last page still had a next one.
async fn list<T: DeserializeOwned>(
    http: &reqwest::Client,
    config: &AuthentikConfig,
    path: &str,
    flag: (&str, &str),
) -> Result<(Vec<T>, bool), FetchError> {
    let mut rows = Vec::new();
    let mut page: i64 = 1;
    for _ in 0..MAX_PAGES {
        let query = [
            (flag.0, flag.1.to_string()),
            ("page", page.to_string()),
            ("page_size", PAGE_SIZE.to_string()),
        ];
        let body: Page<T> = get_page(http, config, path, &query).await?;
        let got = body.results.len();
        rows.extend(body.results);
        if body.pagination.next <= 0 || got == 0 {
            return Ok((rows, false));
        }
        page = body.pagination.next;
    }
    Ok((rows, true))
}

/// One list page, as Authentik returns it. The bearer token is the console's view-only token.
async fn get_page<T: DeserializeOwned>(
    http: &reqwest::Client,
    config: &AuthentikConfig,
    path: &str,
    query: &[(&str, String)],
) -> Result<Page<T>, FetchError> {
    let response = http
        .get(format!("{}{path}", config.api_url))
        .header(
            reqwest::header::AUTHORIZATION,
            format!("Bearer {}", config.token.expose()),
        )
        .header(reqwest::header::ACCEPT, "application/json")
        .query(query)
        .send()
        .await
        .map_err(transport_error)?;
    let status = response.status();
    if !status.is_success() {
        return Err(FetchError::Status(status.as_u16()));
    }
    let body = response.bytes().await.map_err(transport_error)?;
    serde_json::from_slice(&body).map_err(|_| FetchError::Decode)
}

/// reqwest's own Display names the request URL, so the error is classified, not shown (as netbox.rs does).
fn transport_error(err: reqwest::Error) -> FetchError {
    if err.is_timeout() {
        FetchError::Timeout
    } else if err.is_connect() {
        FetchError::Connect
    } else {
        FetchError::Other
    }
}

/// The wire rows as the page shows them, sorted by username and by group name so the answer is stable.
fn directory(users: Vec<UserRow>, groups: Vec<GroupRow>, truncated: bool) -> Directory {
    let mut groups: Vec<Group> = groups
        .into_iter()
        .map(|row| Group {
            name: row.name,
            superuser: row.is_superuser,
            members: row.users.len(),
        })
        .collect();
    groups.sort_by(|a, b| a.name.cmp(&b.name));

    let mut users: Vec<User> = users
        .into_iter()
        .map(|row| {
            let mut names: Vec<String> = row
                .groups_obj
                .unwrap_or_default()
                .into_iter()
                .map(|group| group.name)
                .collect();
            names.sort();
            User {
                username: row.username,
                name: row.name,
                // Authentik keeps an empty string for "no address"; the page shows null.
                email: row.email.filter(|email| !email.is_empty()),
                active: row.is_active,
                service_account: matches!(
                    row.kind.as_deref(),
                    Some("service_account" | "internal_service_account")
                ),
                groups: names,
            }
        })
        .collect();
    users.sort_by(|a, b| a.username.cmp(&b.username));

    Directory {
        users,
        groups,
        truncated,
    }
}

/// What `GET /v1/access` reads. `good` is the last read that finished. `failure` is why the newest read
/// failed, as its reason only. A failed read leaves `good` in place, and a read that finishes clears `failure`.
pub struct AccessCache {
    configured: bool,
    good: Option<Good>,
    failure: Option<String>,
}

/// The last read that finished, and when.
struct Good {
    directory: Directory,
    refreshed_at: String,
}

impl AccessCache {
    /// `configured` is whether the Authentik directory is set up. When it is not, no read runs and the
    /// page says so.
    pub fn new(configured: bool) -> Self {
        Self {
            configured,
            good: None,
            failure: None,
        }
    }

    /// Stores a finished read and clears the failure. A read cut short by the page bound keeps its rows, and
    /// the body says so.
    pub fn record_ok(&mut self, directory: Directory, refreshed_at: String) {
        self.failure = None;
        self.good = Some(Good {
            directory,
            refreshed_at,
        });
    }

    /// Keeps the last good directory, if there is one, and stores the reason this read failed.
    pub fn record_error(&mut self, reason: String) {
        self.failure = Some(reason);
    }

    /// The sentence the page shows under the directory, or none. A failed read names its reason, and says
    /// whether the last good read is still shown. A read cut short names the bound. A failure takes precedence
    /// over the truncation sentence, since the failed read is the newer fact.
    fn error_sentence(&self) -> Option<String> {
        match (&self.failure, &self.good) {
            (Some(reason), Some(_)) => Some(format!(
                "The newest directory read failed: {reason}. Showing the last good read."
            )),
            (Some(reason), None) => Some(format!("The directory read failed: {reason}.")),
            (None, Some(good)) if good.directory.truncated => Some(truncated_sentence()),
            (None, _) => None,
        }
    }

    /// The body `GET /v1/access` serves. `state` is `unconfigured` without the directory, `loading` before
    /// the first read finishes, `error` when no read has finished and the newest failed, and `ok` otherwise.
    pub fn body<'a>(&'a self, role_map: &'a RoleMap) -> AccessBody<'a> {
        let state = match (self.configured, &self.good) {
            (false, _) => "unconfigured",
            (true, Some(_)) => "ok",
            (true, None) if self.failure.is_some() => "error",
            (true, None) => "loading",
        };
        let (users, groups, refreshed_at) = match &self.good {
            Some(good) => (
                good.directory.users.as_slice(),
                good.directory.groups.as_slice(),
                Some(good.refreshed_at.as_str()),
            ),
            None => (&[][..], &[][..], None),
        };
        AccessBody {
            state,
            users,
            groups,
            role_map,
            refreshed_at,
            error: self.error_sentence(),
        }
    }
}

/// `GET /v1/access` (platform admins only). `role_map` is present in every state.
#[derive(Serialize)]
pub struct AccessBody<'a> {
    pub state: &'static str,
    pub users: &'a [User],
    pub groups: &'a [Group],
    pub role_map: &'a RoleMap,
    pub refreshed_at: Option<&'a str>,
    pub error: Option<String>,
}

/// `GET /v1/access` (platform admins only, as `/v1/secrets`). Reads the cache, never Authentik.
pub async fn get_access(State(app): State<SharedState>, session: Session) -> Response {
    if let Err(response) = work::admin_only(&session).await {
        return response;
    }
    let cache = app.access.read().await;
    Json(cache.body(&app.role_map)).into_response()
}

/// Reads the directory once and stores the result. Does nothing when Authentik is not configured. The
/// lock is held only to store, never across the read.
pub async fn refresh(app: &AppState) {
    let Some(config) = app.config.authentik.as_ref() else {
        return;
    };
    let result = fetch(&app.http, config).await;
    let mut cache = app.access.write().await;
    match result {
        Ok(directory) => cache.record_ok(directory, rfc3339_now()),
        Err(err) => {
            let text = err.to_string();
            log_failure(cache.failure.as_deref(), &text);
            cache.record_error(text);
        }
    }
}

/// One line per distinct failure, not one per read while it lasts.
fn log_failure(previous: Option<&str>, text: &str) {
    if previous != Some(text) {
        eprintln!("console: Authentik directory read failed: {text}");
    }
}

/// Starts the directory read when Authentik is configured. The first read runs at once. A missed tick is
/// skipped, not replayed.
pub fn spawn_refresh(state: SharedState) {
    if state.config.authentik.is_none() {
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
    use std::collections::HashMap;
    use std::net::SocketAddr;
    use std::sync::atomic::{AtomicU16, AtomicUsize, Ordering};
    use std::sync::{Arc, Mutex};

    use dioxus::server::axum::extract::Query;
    use dioxus::server::axum::http::{header, HeaderMap, StatusCode, Uri};
    use dioxus::server::axum::routing::get;
    use dioxus::server::axum::Router;
    use serde_json::{json, Value};

    use crate::server::auth;
    use crate::server::config::{AuthentikConfig, Config, Secret};
    use crate::server::inventory::{FlowSet, InventoryCache};
    use crate::server::prefs::Prefs;
    use crate::server::scope::Scope;
    use crate::server::{api_router, session_layer};

    const TOKEN: &str = "svc-console-directory-token-value";
    const SCOPE: &str = r#"{"example": true, "tenants": []}"#;

    /// A fake Authentik: the two list endpoints, paged the way Authentik pages them. It records the query
    /// of every request, and it can refuse every request, echoing the bearer into the body to prove
    /// that the body never reaches an error.
    struct Fake {
        users: Mutex<Vec<Value>>,
        groups: Mutex<Vec<Value>>,
        fail_with: AtomicU16,
        requests: AtomicUsize,
        queries: Mutex<Vec<HashMap<String, String>>>,
    }

    impl Fake {
        fn new(users: Vec<Value>, groups: Vec<Value>) -> Self {
            Fake {
                users: Mutex::new(users),
                groups: Mutex::new(groups),
                fail_with: AtomicU16::new(0),
                requests: AtomicUsize::new(0),
                queries: Mutex::new(Vec::new()),
            }
        }

        fn pages_requested(&self, path_flag: &str) -> Vec<String> {
            self.queries
                .lock()
                .expect("queries")
                .iter()
                .filter(|q| q.contains_key(path_flag))
                .filter_map(|q| q.get("page").cloned())
                .collect()
        }
    }

    async fn fake_list(
        State(fake): State<Arc<Fake>>,
        headers: HeaderMap,
        uri: Uri,
        Query(query): Query<HashMap<String, String>>,
    ) -> (StatusCode, Json<Value>) {
        fake.requests.fetch_add(1, Ordering::SeqCst);
        fake.queries.lock().expect("queries").push(query.clone());
        let bearer = headers
            .get(header::AUTHORIZATION)
            .and_then(|value| value.to_str().ok())
            .unwrap_or("")
            .to_string();
        let forced = fake.fail_with.load(Ordering::SeqCst);
        if forced != 0 {
            let status = StatusCode::from_u16(forced).expect("status");
            return (status, Json(json!({"detail": format!("boom {bearer}")})));
        }
        if bearer != format!("Bearer {TOKEN}") {
            return (StatusCode::FORBIDDEN, Json(json!({"detail": "no"})));
        }
        let rows = match uri.path() {
            "/api/v3/core/users/" => fake.users.lock().expect("users").clone(),
            "/api/v3/core/groups/" => fake.groups.lock().expect("groups").clone(),
            _ => return (StatusCode::NOT_FOUND, Json(json!({"detail": "not found"}))),
        };
        let size: usize = query
            .get("page_size")
            .and_then(|s| s.parse().ok())
            .unwrap_or(20);
        let page: usize = query.get("page").and_then(|s| s.parse().ok()).unwrap_or(1);
        let start = (page - 1) * size;
        let end = (start + size).min(rows.len());
        let next = if end < rows.len() { page + 1 } else { 0 };
        let slice: Vec<Value> = rows.get(start..end).unwrap_or(&[]).to_vec();
        (
            StatusCode::OK,
            Json(json!({
                "pagination": {
                    "next": next,
                    "previous": page.saturating_sub(1),
                    "count": rows.len(),
                    "current": page,
                    "total_pages": rows.len().div_ceil(size.max(1)),
                    "start_index": start + 1,
                    "end_index": end,
                },
                "results": slice,
            })),
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

    async fn fake_authentik(fake: Arc<Fake>) -> SocketAddr {
        let app = Router::new()
            .route("/api/v3/core/users/", get(fake_list))
            .route("/api/v3/core/groups/", get(fake_list))
            .with_state(fake);
        serve(app).await
    }

    fn client_for(addr: SocketAddr) -> AuthentikConfig {
        AuthentikConfig {
            api_url: format!("http://{addr}"),
            token: Secret::new(TOKEN),
        }
    }

    fn user(
        pk: i64,
        username: &str,
        kind: Option<&str>,
        groups: Option<Vec<&str>>,
        active: bool,
    ) -> Value {
        let mut row = json!({
            "pk": pk,
            "username": username,
            "name": format!("{username} display"),
            "email": format!("{username}@example.test"),
            "is_active": active,
            "is_superuser": false,
        });
        if let Some(kind) = kind {
            row["type"] = json!(kind);
        }
        if let Some(groups) = groups {
            row["groups_obj"] = json!(groups
                .iter()
                .map(|name| json!({"pk": "00000000-0000-0000-0000-000000000000", "name": name}))
                .collect::<Vec<_>>());
        }
        row
    }

    fn group(name: &str, superuser: bool, members: &[i64]) -> Value {
        json!({"pk": "00000000-0000-0000-0000-000000000000", "num_pk": 1, "name": name,
               "is_superuser": superuser, "users": members})
    }

    /// Four users and three groups. `carol` has no `groups_obj` at all, and no `type`. `ak-outpost-example`
    /// is an `internal_service_account`, which counts as a service account.
    fn small_directory() -> (Vec<Value>, Vec<Value>) {
        let users = vec![
            user(
                1,
                "alice",
                Some("internal"),
                Some(vec!["platform-admins"]),
                true,
            ),
            user(
                2,
                "bob",
                Some("service_account"),
                Some(vec!["platform-admins", "ops"]),
                false,
            ),
            json!({"pk": 3, "username": "carol", "name": "", "email": "", "is_active": true, "groups_obj": null}),
            user(
                4,
                "ak-outpost-example",
                Some("internal_service_account"),
                None,
                true,
            ),
        ];
        let groups = vec![
            group("platform-admins", true, &[1, 2]),
            group("ops", false, &[2]),
            group("empty", false, &[]),
        ];
        (users, groups)
    }

    fn expected_small() -> Directory {
        Directory {
            users: vec![
                User {
                    username: "ak-outpost-example".into(),
                    name: "ak-outpost-example display".into(),
                    email: Some("ak-outpost-example@example.test".into()),
                    active: true,
                    service_account: true,
                    groups: Vec::new(),
                },
                User {
                    username: "alice".into(),
                    name: "alice display".into(),
                    email: Some("alice@example.test".into()),
                    active: true,
                    service_account: false,
                    groups: vec!["platform-admins".into()],
                },
                User {
                    username: "bob".into(),
                    name: "bob display".into(),
                    email: Some("bob@example.test".into()),
                    active: false,
                    service_account: true,
                    groups: vec!["ops".into(), "platform-admins".into()],
                },
                User {
                    username: "carol".into(),
                    name: String::new(),
                    email: None,
                    active: true,
                    service_account: false,
                    groups: Vec::new(),
                },
            ],
            groups: vec![
                Group {
                    name: "empty".into(),
                    superuser: false,
                    members: 0,
                },
                Group {
                    name: "ops".into(),
                    superuser: false,
                    members: 1,
                },
                Group {
                    name: "platform-admins".into(),
                    superuser: true,
                    members: 2,
                },
            ],
            truncated: false,
        }
    }

    fn role_map_fixture() -> RoleMap {
        RoleMap {
            systems: vec!["Semaphore".into(), "NetBox".into()],
            levels: vec![RoleLevel {
                level: "Admin".into(),
                status: Status::Built,
                roles: BTreeMap::from([
                    (
                        "Semaphore".to_string(),
                        "admin flag on user (global admin)".to_string(),
                    ),
                    (
                        "NetBox".to_string(),
                        "superuser / Full Access on all object types".to_string(),
                    ),
                ]),
            }],
        }
    }

    /// The application state, with the directory when one is given.
    fn state(authentik: Option<AuthentikConfig>, role_map: RoleMap) -> SharedState {
        let configured = authentik.is_some();
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
                grafana_url: None,
                grafana_token: None,
                gateway_file: None,
                authentik,
                role_map_file: None,
            },
            http: reqwest::Client::new(),
            oidc: tokio::sync::OnceCell::new(),
            scope: Scope::parse(SCOPE).expect("scope"),
            prefs: Prefs::in_memory().expect("prefs"),
            work: Arc::new(tokio::sync::RwLock::new(
                super::super::work::WorkCache::unconfigured(),
            )),
            workflow: Vec::new(),
            inventory: tokio::sync::RwLock::new(InventoryCache::new(false, false)),
            proxmox_http: reqwest::Client::new(),
            flows: FlowSet::unconfigured(),
            gateway: crate::server::gateway::GatewaySet::unconfigured(),
            usage: tokio::sync::RwLock::new(crate::server::grafana::UsageCache::new(false)),
            openbao: None,
            secrets: tokio::sync::RwLock::new(super::super::secrets::SecretsCache::new(false)),
            access: tokio::sync::RwLock::new(AccessCache::new(configured)),
            role_map,
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
    async fn fetch_route(addr: SocketAddr, path: &str, cookie: Option<&str>) -> (u16, String) {
        let client = reqwest::Client::new();
        let mut request = client.get(format!("http://{addr}{path}"));
        if let Some(cookie) = cookie {
            request = request.header("cookie", cookie);
        }
        let reply = request.send().await.expect("send");
        let status = reply.status().as_u16();
        (status, reply.text().await.expect("body"))
    }

    #[tokio::test]
    async fn a_read_resolves_groups_counts_members_and_maps_service_accounts() {
        let (users, groups) = small_directory();
        let fake = Arc::new(Fake::new(users, groups));
        let addr = fake_authentik(fake.clone()).await;
        let directory = fetch(&reqwest::Client::new(), &client_for(addr))
            .await
            .expect("read");
        assert_eq!(directory, expected_small());
        // The groups list asks without expanded members, and the users list asks for group objects.
        let queries = fake.queries.lock().expect("queries").clone();
        assert!(queries
            .iter()
            .any(|q| q.get("include_users").map(String::as_str) == Some("false")));
        assert!(queries
            .iter()
            .any(|q| q.get("include_groups").map(String::as_str) == Some("true")));
        assert!(queries
            .iter()
            .all(|q| q.get("page_size").map(String::as_str) == Some("100")));
    }

    #[tokio::test]
    async fn pages_are_followed_until_next_is_zero() {
        // 250 users is three pages at 100. Every page is asked for, and the third page's next is 0.
        let users: Vec<Value> = (1..=250)
            .map(|pk| user(pk, &format!("user{pk:03}"), None, None, true))
            .collect();
        let fake = Arc::new(Fake::new(users, Vec::new()));
        let addr = fake_authentik(fake.clone()).await;
        let directory = fetch(&reqwest::Client::new(), &client_for(addr))
            .await
            .expect("read");
        assert_eq!(directory.users.len(), 250);
        assert!(!directory.truncated);
        assert_eq!(fake.pages_requested("include_groups"), vec!["1", "2", "3"]);
    }

    #[tokio::test]
    async fn a_list_past_the_page_bound_is_cut_and_marked_truncated_not_failed() {
        let limit = PAGE_SIZE * MAX_PAGES;
        let users: Vec<Value> = (1..=(limit as i64 + 50))
            .map(|pk| user(pk, &format!("u{pk:05}"), None, None, true))
            .collect();
        let fake = Arc::new(Fake::new(users, Vec::new()));
        let addr = fake_authentik(fake.clone()).await;
        let directory = fetch(&reqwest::Client::new(), &client_for(addr))
            .await
            .expect("a long list is an answer, not an error");
        assert!(directory.truncated);
        assert_eq!(directory.users.len(), limit);
        assert_eq!(fake.pages_requested("include_groups").len(), MAX_PAGES);
    }

    #[test]
    fn a_truncated_read_keeps_its_rows_and_names_the_bound_as_its_error() {
        let mut cache = AccessCache::new(true);
        let directory = Directory {
            users: Vec::new(),
            groups: Vec::new(),
            truncated: true,
        };
        cache.record_ok(directory, "2026-11-03T00:00:00Z".into());
        let role_map = RoleMap::empty();
        let body = serde_json::to_value(cache.body(&role_map)).expect("json");
        assert_eq!(body["state"], "ok");
        assert_eq!(
            body["error"],
            "Authentik holds more than 2000 users or groups; the first 2000 are shown."
        );
    }

    #[tokio::test]
    async fn refresh_keeps_the_last_good_directory_and_says_why_then_heals() {
        let (users, groups) = small_directory();
        let fake = Arc::new(Fake::new(users, groups));
        let addr = fake_authentik(fake.clone()).await;
        let app = state(Some(client_for(addr)), RoleMap::empty());

        refresh(&app).await;
        let first =
            serde_json::to_value(app.access.read().await.body(&app.role_map)).expect("json");
        assert_eq!(first["state"], "ok");
        assert_eq!(first["error"], Value::Null);
        assert_eq!(first["users"].as_array().expect("users").len(), 4);
        let refreshed_at = first["refreshed_at"].clone();
        assert!(refreshed_at.is_string());

        // Authentik now refuses every read. The page keeps the directory and names the status only.
        fake.fail_with.store(500, Ordering::SeqCst);
        refresh(&app).await;
        let stale =
            serde_json::to_value(app.access.read().await.body(&app.role_map)).expect("json");
        assert_eq!(stale["state"], "ok", "the last good read is still served");
        assert_eq!(
            stale["error"],
            "The newest directory read failed: Authentik answered HTTP 500. Showing the last good read."
        );
        assert_eq!(stale["users"], first["users"]);
        assert_eq!(stale["groups"], first["groups"]);
        assert_eq!(
            stale["refreshed_at"], refreshed_at,
            "the stamp is the older read's"
        );
        let text = stale.to_string();
        assert!(
            !text.contains(TOKEN),
            "the console token must never reach the answer"
        );

        fake.fail_with.store(0, Ordering::SeqCst);
        refresh(&app).await;
        let healed =
            serde_json::to_value(app.access.read().await.body(&app.role_map)).expect("json");
        assert_eq!(healed["error"], Value::Null);
    }

    #[tokio::test]
    async fn a_failed_first_read_is_an_error_with_no_directory_and_no_token_in_it() {
        let fake = Arc::new(Fake::new(Vec::new(), Vec::new()));
        fake.fail_with.store(503, Ordering::SeqCst);
        let addr = fake_authentik(fake.clone()).await;
        let app = state(Some(client_for(addr)), RoleMap::empty());
        refresh(&app).await;
        let body = serde_json::to_value(app.access.read().await.body(&app.role_map)).expect("json");
        assert_eq!(body["state"], "error");
        assert_eq!(
            body["error"],
            "The directory read failed: Authentik answered HTTP 503."
        );
        assert_eq!(body["users"], json!([]));
        assert_eq!(body["refreshed_at"], Value::Null);
        assert!(!body.to_string().contains(TOKEN));
    }

    #[test]
    fn a_read_error_is_fixed_text_and_names_no_url_or_body() {
        for err in [
            FetchError::Status(403),
            FetchError::Timeout,
            FetchError::Connect,
            FetchError::Other,
            FetchError::Decode,
        ] {
            let text = err.to_string();
            assert!(!text.contains("http"), "{text}");
            assert!(!text.contains(TOKEN), "{text}");
        }
    }

    #[test]
    fn states_follow_configuration_and_the_reads_that_finished() {
        let role_map = role_map_fixture();
        let state_of = |cache: &AccessCache| {
            serde_json::to_value(cache.body(&role_map)).expect("json")["state"]
                .as_str()
                .expect("state")
                .to_string()
        };
        assert_eq!(state_of(&AccessCache::new(false)), "unconfigured");
        let mut cache = AccessCache::new(true);
        assert_eq!(state_of(&cache), "loading");
        cache.record_error("Authentik answered HTTP 500".into());
        assert_eq!(state_of(&cache), "error");
        cache.record_ok(expected_small(), "2026-11-03T00:00:00Z".into());
        assert_eq!(state_of(&cache), "ok");
    }

    #[test]
    fn the_role_map_is_present_in_every_state() {
        let role_map = role_map_fixture();
        let unconfigured =
            serde_json::to_value(AccessCache::new(false).body(&role_map)).expect("json");
        assert_eq!(unconfigured["state"], "unconfigured");
        assert_eq!(
            unconfigured["role_map"]["systems"],
            json!(["Semaphore", "NetBox"])
        );
        assert_eq!(unconfigured["role_map"]["levels"][0]["status"], "built");
        assert_eq!(unconfigured["role_map"]["levels"][0]["level"], "Admin");
        assert_eq!(
            unconfigured["role_map"]["levels"][0]["roles"]["NetBox"],
            "superuser / Full Access on all object types"
        );
        assert_eq!(unconfigured["refreshed_at"], Value::Null);
        assert_eq!(unconfigured["users"], json!([]));
    }

    #[test]
    fn the_role_map_loads_from_its_file_and_names_the_setting_on_error() {
        let dir = std::env::temp_dir().join(format!("console-role-map-{}", std::process::id()));
        fs::create_dir_all(&dir).expect("dir");
        let good = dir.join("role-map.json");
        fs::write(
            &good,
            serde_json::to_string(&role_map_fixture()).expect("json"),
        )
        .expect("write");
        assert_eq!(load_role_map(&good).expect("loads"), role_map_fixture());

        let bad = dir.join("bad.json");
        fs::write(&bad, r#"{"systems": [], "levels": [], "notes": "extra"}"#).expect("write");
        let err = load_role_map(&bad).expect_err("unknown key refused");
        assert!(
            err.starts_with("CONSOLE_ROLE_MAP_FILE: invalid JSON"),
            "{err}"
        );

        let missing = dir.join("absent.json");
        let err = load_role_map(&missing).expect_err("missing file refused");
        assert!(
            err.starts_with("CONSOLE_ROLE_MAP_FILE: cannot read"),
            "{err}"
        );

        let status = dir.join("status.json");
        fs::write(
            &status,
            r#"{"systems": [], "levels": [{"level": "X", "status": "done", "roles": {}}]}"#,
        )
        .expect("write");
        assert!(
            load_role_map(&status).is_err(),
            "only built and target are statuses"
        );
        fs::remove_dir_all(&dir).expect("cleanup");
    }

    #[tokio::test]
    async fn members_get_403_signed_out_gets_401_and_platform_admins_get_the_answer() {
        let (users, groups) = small_directory();
        let fake = Arc::new(Fake::new(users, groups));
        let addr = fake_authentik(fake).await;
        let app = state(Some(client_for(addr)), role_map_fixture());
        refresh(&app).await;

        let (addr, _cookie) = app_for(app.clone(), &["platform-admins"]).await;
        let (status, body) = fetch_route(addr, "/v1/access", None).await;
        assert_eq!(status, 401);
        assert_eq!(body, r#"{"error":"unauthenticated"}"#);

        for groups in [&["ops"][..], &["platform-admins-old", "users"][..]] {
            let (addr, cookie) = app_for(app.clone(), groups).await;
            let (status, body) = fetch_route(addr, "/v1/access", Some(&cookie)).await;
            assert_eq!(status, 403, "{groups:?}");
            assert_eq!(body, r#"{"error":"forbidden"}"#, "{groups:?}");
        }

        let (addr, cookie) = app_for(app.clone(), &["platform-admins"]).await;
        let (status, body) = fetch_route(addr, "/v1/access", Some(&cookie)).await;
        assert_eq!(status, 200);
        let value: Value = serde_json::from_str(&body).expect("json");
        assert_eq!(value["state"], "ok");
        assert_eq!(value["users"].as_array().expect("users").len(), 4);
        assert_eq!(value["groups"].as_array().expect("groups").len(), 3);
        assert_eq!(value["role_map"]["systems"], json!(["Semaphore", "NetBox"]));
        assert_eq!(value["error"], Value::Null);
    }

    #[tokio::test]
    async fn an_unconfigured_directory_is_answered_without_a_read_or_a_token() {
        let app = state(None, role_map_fixture());
        refresh(&app).await;
        let (addr, cookie) = app_for(app.clone(), &["platform-admins"]).await;
        let (status, body) = fetch_route(addr, "/v1/access", Some(&cookie)).await;
        assert_eq!(status, 200);
        let value: Value = serde_json::from_str(&body).expect("json");
        assert_eq!(value["state"], "unconfigured");
        assert_eq!(value["role_map"]["levels"][0]["level"], "Admin");
        assert_eq!(value["refreshed_at"], Value::Null);
    }
}
