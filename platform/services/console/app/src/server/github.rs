//! The GitHub reads behind the Policy page (plan 17, 8.4, D36). One ref of one repository is read for
//! two sets of files: the OPA policies (`*.rego` and `*.json` under `platform/services/opa/deployment/policies/`,
//! any depth) and the OpenBao policies (`*.hcl` directly in `platform/services/openbao/deployment/config/policies/`).
//! Each file gets its name and the last commit that touched it on the ref.
//!
//! The console's read-only installation token is sent to the API origin passed in and to no other, and no
//! redirect is followed. Every error is fixed text: no URL, response body or token reaches a message. Path
//! segments and query values are built through `reqwest::Url`, so a slash in the ref stays one query value and
//! a file path stays one segment per part. Shapes verified live against `uhstray-io/agent-cloud` (ref `dev` and
//! a slash-named branch): `commits?sha=&per_page=1` returns the tip with `commit.tree.sha`; `git/trees/{sha}?recursive=1`
//! returns `tree` (entries with `path` and `type`) and `truncated`; `contents/{path}?ref=` with
//! `Accept: application/vnd.github.raw` returns the file bytes; `commits?sha=&path=&per_page=1` returns the newest
//! commit that touched the path.

use std::fmt;

use reqwest::header::{HeaderMap, ACCEPT, USER_AGENT};
use reqwest::Url;
use serde::de::DeserializeOwned;
use serde::{Deserialize, Serialize};

use super::config::{GithubConfig, Secret};
use super::openbao::OpenbaoError;

/// The GitHub REST origin. Production always passes this; tests pass a local origin.
pub const GITHUB_API: &str = "https://api.github.com";
/// The web origin the file links point at.
const GITHUB_WEB: &str = "https://github.com";

const OPA_DIR: &str = "platform/services/opa/deployment/policies/";
const OPENBAO_DIR: &str = "platform/services/openbao/deployment/config/policies/";

const JSON_ACCEPT: &str = "application/vnd.github+json";
const RAW_ACCEPT: &str = "application/vnd.github.raw";
const API_VERSION: &str = "2022-11-28";
const CLIENT_NAME: &str = "dispatch-console";

/// The kind of a policy file, as the page groups and labels it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    /// `*.rego` under the OPA policies directory, at any depth.
    OpaPolicy,
    /// `*_test.rego` under the OPA policies directory.
    OpaTest,
    /// `*.json` under the OPA policies directory: data documents.
    OpaData,
    /// `*.hcl` directly in the OpenBao policies directory.
    OpenBao,
}

impl Kind {
    /// The word the wire carries.
    pub fn as_str(self) -> &'static str {
        match self {
            Kind::OpaPolicy => "opa-policy",
            Kind::OpaTest => "opa-test",
            Kind::OpaData => "opa-data",
            Kind::OpenBao => "openbao",
        }
    }
}

/// The kind of a repository path, or `None` when the path is not a policy file.
pub fn classify(path: &str) -> Option<Kind> {
    if let Some(rest) = path.strip_prefix(OPA_DIR) {
        let name = rest.rsplit('/').next().unwrap_or(rest);
        // `_test.rego` is checked first: it also ends in `.rego`.
        return if name.ends_with("_test.rego") {
            Some(Kind::OpaTest)
        } else if name.ends_with(".rego") {
            Some(Kind::OpaPolicy)
        } else if name.ends_with(".json") {
            Some(Kind::OpaData)
        } else {
            None
        };
    }
    let rest = path.strip_prefix(OPENBAO_DIR)?;
    (!rest.contains('/') && rest.ends_with(".hcl")).then_some(Kind::OpenBao)
}

/// The value of a Rego file's first `package` line. Comment lines (`#`) are skipped and a trailing
/// `# ...` comment is cut from the line. `None` when no package line has a value.
pub fn rego_package(text: &str) -> Option<String> {
    text.lines().find_map(|line| {
        let line = line.trim();
        if line.starts_with('#') {
            return None;
        }
        // `package` must be a whole word: `packages` is not a package line.
        let rest = line.strip_prefix("package")?;
        if !rest.starts_with(char::is_whitespace) {
            return None;
        }
        let value = rest.split('#').next().unwrap_or_default().trim();
        (!value.is_empty()).then(|| value.to_string())
    })
}

/// The last component of a repository path.
fn file_name(path: &str) -> &str {
    path.rsplit('/').next().unwrap_or(path)
}

/// The newest commit that touched one file: its short SHA, its committer date and the first line of its message.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct LastCommit {
    pub sha: String,
    pub date: String,
    pub subject: String,
}

/// One policy file as the reader found it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Found {
    pub path: String,
    pub kind: Kind,
    pub name: String,
    pub last_commit: Option<LastCommit>,
    pub url: String,
}

/// What one read found: the files sorted by path, cut to the file bound, and whether the cut happened.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Listing {
    pub files: Vec<Found>,
    pub truncated: bool,
}

/// Why one read failed. Fixed text only.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FetchError {
    /// Any refusal the console does not name below: the HTTP status only.
    Status(u16),
    /// 401: the token is expired or not valid.
    Unauthorized,
    /// 403 without a rate-limit header: the App may not read this repository.
    Forbidden,
    /// 403 with the quota spent (`x-ratelimit-remaining: 0`) or a `retry-after` header.
    RateLimited,
    Timeout,
    Connect,
    Other,
    Decode,
    /// The ref has no commit, so there is no tree to list.
    NoCommit,
    /// GitHub cut the tree short; a partial list is not shown.
    TreeTruncated,
    /// The read would send a request past its bound.
    Bound,
    /// OpenBao refused or failed the token read. The error names its status only.
    OpenBao(OpenbaoError),
    /// The token field is absent or empty in OpenBao.
    TokenMissing,
}

impl fmt::Display for FetchError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            FetchError::Status(code) => write!(f, "GitHub answered HTTP {code}"),
            FetchError::Unauthorized => f.write_str(
                "GitHub refused the console token (HTTP 401): it is expired or invalid. Check that Refresh Console GitHub Token is running.",
            ),
            FetchError::Forbidden => f.write_str(
                "GitHub refused the console token (HTTP 403): the App is not permitted to read this repository.",
            ),
            FetchError::RateLimited => f.write_str(
                "GitHub rate-limited the console (HTTP 403); the next read tries again.",
            ),
            FetchError::Timeout => f.write_str("GitHub did not answer in time"),
            FetchError::Connect => f.write_str("cannot connect to GitHub"),
            FetchError::Other => f.write_str("the GitHub request failed"),
            FetchError::Decode => {
                f.write_str("GitHub answered with a body the console does not read")
            }
            FetchError::NoCommit => f.write_str("GitHub reports no commit on the configured ref"),
            FetchError::TreeTruncated => {
                f.write_str("GitHub cut the policy tree short, so the list would be partial")
            }
            FetchError::Bound => {
                f.write_str("the GitHub reads for one refresh reached their request bound")
            }
            FetchError::OpenBao(err) => write!(f, "{err}"),
            FetchError::TokenMissing => {
                f.write_str("the GitHub token is missing or empty in OpenBao")
            }
        }
    }
}

/// The error for a refused request. Only the status and two headers are read: the body is never
/// echoed. A 403 is a rate limit when the quota is spent or `retry-after` is set; any other 403 is
/// a permission refusal.
fn refusal(status: reqwest::StatusCode, headers: &HeaderMap) -> FetchError {
    match status.as_u16() {
        401 => FetchError::Unauthorized,
        403 if rate_limited(headers) => FetchError::RateLimited,
        403 => FetchError::Forbidden,
        code => FetchError::Status(code),
    }
}

/// True when GitHub says the quota is spent or asks the caller to wait.
fn rate_limited(headers: &HeaderMap) -> bool {
    headers
        .get("x-ratelimit-remaining")
        .and_then(|value| value.to_str().ok())
        == Some("0")
        || headers.contains_key("retry-after")
}

/// reqwest's own Display names the request URL, so the error is classified, not shown (as access.rs does).
fn transport_error(err: reqwest::Error) -> FetchError {
    if err.is_timeout() {
        FetchError::Timeout
    } else if err.is_connect() {
        FetchError::Connect
    } else {
        FetchError::Other
    }
}

#[derive(Deserialize)]
struct CommitRow {
    sha: String,
    commit: CommitBody,
}

#[derive(Deserialize)]
struct CommitBody {
    message: String,
    committer: Dated,
    tree: TreeRef,
}

#[derive(Deserialize)]
struct Dated {
    date: String,
}

#[derive(Deserialize)]
struct TreeRef {
    sha: String,
}

#[derive(Deserialize)]
struct TreeBody {
    truncated: bool,
    tree: Vec<Entry>,
}

#[derive(Deserialize)]
struct Entry {
    path: String,
    #[serde(rename = "type")]
    kind: String,
}

/// The URL of `api` followed by `segments`. Each segment is percent-encoded by the URL crate. A `.`,
/// a `..` or an empty segment is refused before a URL exists, so no path can leave its endpoint.
fn endpoint(api: &str, segments: &[&str]) -> Result<Url, FetchError> {
    if segments
        .iter()
        .any(|segment| segment.is_empty() || *segment == "." || *segment == "..")
    {
        return Err(FetchError::Decode);
    }
    let mut url = Url::parse(api).map_err(|_| FetchError::Other)?;
    url.path_segments_mut()
        .map_err(|()| FetchError::Other)?
        .pop_if_empty()
        .extend(segments);
    Ok(url)
}

/// The HTML page of one file on the configured ref, `https://github.com/{repo}/blob/{ref}/{path}`.
fn html_url(source: &GithubConfig, path: &str) -> Result<String, FetchError> {
    let (owner, name) = source.repo.split_once('/').ok_or(FetchError::Decode)?;
    let mut segments = vec![owner, name, "blob"];
    segments.extend(source.git_ref.split('/'));
    segments.extend(path.split('/'));
    Ok(endpoint(GITHUB_WEB, &segments)?.to_string())
}

/// Requests one read may send. Every request, including a failed one, is counted before it is sent.
struct Reader<'a> {
    http: &'a reqwest::Client,
    token: &'a Secret,
    left: usize,
}

impl Reader<'_> {
    /// One GET with the token, the API version and the given `Accept`. A non-2xx answer is an error.
    async fn send(&mut self, url: Url, accept: &str) -> Result<reqwest::Response, FetchError> {
        self.left = self.left.checked_sub(1).ok_or(FetchError::Bound)?;
        let response = self
            .http
            .get(url)
            .bearer_auth(self.token.expose())
            .header(ACCEPT, accept)
            .header("X-GitHub-Api-Version", API_VERSION)
            .header(USER_AGENT, CLIENT_NAME)
            .send()
            .await
            .map_err(transport_error)?;
        let status = response.status();
        if !status.is_success() {
            return Err(refusal(status, response.headers()));
        }
        Ok(response)
    }

    async fn json<T: DeserializeOwned>(&mut self, url: Url) -> Result<T, FetchError> {
        let bytes = self
            .send(url, JSON_ACCEPT)
            .await?
            .bytes()
            .await
            .map_err(transport_error)?;
        serde_json::from_slice(&bytes).map_err(|_| FetchError::Decode)
    }

    async fn raw(&mut self, url: Url) -> Result<String, FetchError> {
        let bytes = self
            .send(url, RAW_ACCEPT)
            .await?
            .bytes()
            .await
            .map_err(transport_error)?;
        String::from_utf8(bytes.to_vec()).map_err(|_| FetchError::Decode)
    }
}

/// Lists the policy files on the configured ref. The tip commit is read first, and its SHA is the
/// snapshot every later read names, so one refresh reads one state of the repository.
///
/// Order: the tip (1 request), the whole tree (1), then for each file one raw read (Rego only) and
/// one commit read. Files are sorted by path and cut to `max_files` before any per-file request, so
/// the same files are always the ones shown. A tree GitHub truncated is an error, not a partial list.
/// `max_requests` bounds the whole read: a request past it is not sent, and the read fails.
pub async fn list(
    http: &reqwest::Client,
    api: &str,
    token: &Secret,
    source: &GithubConfig,
    max_files: usize,
    max_requests: usize,
) -> Result<Listing, FetchError> {
    let (owner, repo_name) = source.repo.split_once('/').ok_or(FetchError::Decode)?;
    let mut reader = Reader {
        http,
        token,
        left: max_requests,
    };

    let mut tip_url = endpoint(api, &["repos", owner, repo_name, "commits"])?;
    tip_url
        .query_pairs_mut()
        .append_pair("sha", &source.git_ref)
        .append_pair("per_page", "1");
    let tip: Vec<CommitRow> = reader.json(tip_url).await?;
    let tip = tip.into_iter().next().ok_or(FetchError::NoCommit)?;
    let snapshot = tip.sha.clone();

    let mut tree_url = endpoint(
        api,
        &[
            "repos",
            owner,
            repo_name,
            "git",
            "trees",
            tip.commit.tree.sha.as_str(),
        ],
    )?;
    tree_url.query_pairs_mut().append_pair("recursive", "1");
    let tree: TreeBody = reader.json(tree_url).await?;
    if tree.truncated {
        return Err(FetchError::TreeTruncated);
    }

    let mut matched: Vec<(String, Kind)> = tree
        .tree
        .into_iter()
        .filter(|entry| entry.kind == "blob")
        .filter_map(|entry| classify(&entry.path).map(|kind| (entry.path, kind)))
        .collect();
    matched.sort_by(|a, b| a.0.cmp(&b.0));
    let truncated = matched.len() > max_files;
    matched.truncate(max_files);

    let mut files = Vec::with_capacity(matched.len());
    for (path, kind) in matched {
        let name = match kind {
            Kind::OpaPolicy | Kind::OpaTest => {
                let mut segments = vec!["repos", owner, repo_name, "contents"];
                segments.extend(path.split('/'));
                let mut url = endpoint(api, &segments)?;
                url.query_pairs_mut().append_pair("ref", &snapshot);
                let text = reader.raw(url).await?;
                rego_package(&text).unwrap_or_else(|| file_name(&path).to_string())
            }
            Kind::OpaData => file_name(&path).to_string(),
            Kind::OpenBao => file_name(&path)
                .strip_suffix(".hcl")
                .unwrap_or(file_name(&path))
                .to_string(),
        };

        let mut commits_url = endpoint(api, &["repos", owner, repo_name, "commits"])?;
        commits_url
            .query_pairs_mut()
            .append_pair("sha", &snapshot)
            .append_pair("path", &path)
            .append_pair("per_page", "1");
        let commits: Vec<CommitRow> = reader.json(commits_url).await?;
        let last_commit = commits.into_iter().next().map(|row| LastCommit {
            sha: row.sha.chars().take(7).collect(),
            date: row.commit.committer.date,
            subject: row
                .commit
                .message
                .lines()
                .next()
                .unwrap_or_default()
                .to_string(),
        });

        let url = html_url(source, &path)?;
        files.push(Found {
            path,
            kind,
            name,
            last_commit,
            url,
        });
    }
    Ok(Listing { files, truncated })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeMap;
    use std::net::SocketAddr;
    use std::sync::{Arc, Mutex};

    use dioxus::server::axum::extract::{Path, Query, State};
    use dioxus::server::axum::http::{HeaderMap, HeaderName, HeaderValue, StatusCode};
    use dioxus::server::axum::response::{IntoResponse, Response};
    use dioxus::server::axum::routing::get;
    use dioxus::server::axum::{Json, Router};
    use serde_json::{json, Value};

    /// The token the fake GitHub accepts. Not shaped like a real GitHub token.
    const TOKEN: &str = "console-github-canary-token";
    const SHA_TIP: &str = "5f1c2a9d0e7b4c3a2b1d0e9f8a7b6c5d4e3f2a1b";
    const TREE_SHA: &str = "aa262c85779bdf49d3a69e8e937dc4631f614cdc";

    /// Every path the fake reads, as the request path and query that reached it.
    #[derive(Default)]
    struct Fake {
        /// Set to refuse every request with this status.
        fail_with: Mutex<Option<u16>>,
        /// Set to cut the tree short.
        truncated: Mutex<bool>,
        /// Tree entries as (path, type).
        entries: Mutex<Vec<(String, String)>>,
        /// Raw file text by path.
        texts: Mutex<BTreeMap<String, String>>,
        /// Newest commit by path. A path absent from the map has no commit on the ref.
        commits: Mutex<BTreeMap<String, Value>>,
        /// `(path, query)` of every request that reached a handler.
        seen: Mutex<Vec<(String, String)>>,
        /// Requests whose `Authorization` was not the expected bearer.
        unauthorized: Mutex<usize>,
        /// Headers sent with every refusal, as (name, value).
        fail_headers: Mutex<Vec<(String, String)>>,
    }

    fn record(fake: &Fake, path: &str, query: &str) {
        fake.seen
            .lock()
            .expect("seen")
            .push((path.to_string(), query.to_string()));
    }

    /// A refusal the fake sends: the status, the JSON body `denied` and the headers the test set.
    fn refusal(status: StatusCode, headers: &[(String, String)]) -> Response {
        let mut response = (status, Json(json!({"message": "denied"}))).into_response();
        for (name, value) in headers {
            response.headers_mut().insert(
                HeaderName::from_bytes(name.as_bytes()).expect("header name"),
                HeaderValue::from_str(value).expect("header value"),
            );
        }
        response
    }

    /// The refusal this request gets, if any: a wrong bearer is a 401; otherwise the status and
    /// headers the test set for every request.
    fn refuse(fake: &Fake, headers: &HeaderMap) -> Option<Response> {
        let expected = format!("Bearer {TOKEN}");
        let presented = headers
            .get("authorization")
            .and_then(|value| value.to_str().ok());
        if presented != Some(expected.as_str()) {
            *fake.unauthorized.lock().expect("unauthorized") += 1;
            return Some(refusal(StatusCode::UNAUTHORIZED, &[]));
        }
        let failing = *fake.fail_with.lock().expect("fail_with");
        let extra = fake.fail_headers.lock().expect("fail_headers").clone();
        failing.map(|code| refusal(StatusCode::from_u16(code).expect("status"), &extra))
    }

    async fn fake_commits(
        State(fake): State<Arc<Fake>>,
        headers: HeaderMap,
        Query(query): Query<BTreeMap<String, String>>,
    ) -> Response {
        record(&fake, "commits", &flat_query(&query));
        if let Some(response) = refuse(&fake, &headers) {
            return response;
        }
        if let Some(path) = query.get("path") {
            let commit = fake.commits.lock().expect("commits").get(path).cloned();
            return Json(json!(commit.into_iter().collect::<Vec<_>>())).into_response();
        }
        Json(json!([{
            "sha": SHA_TIP,
            "commit": {
                "message": "feat(policy): tip\n\nbody",
                "committer": {"date": "2026-11-01T00:00:00Z"},
                "tree": {"sha": TREE_SHA}
            }
        }]))
        .into_response()
    }

    async fn fake_tree(
        State(fake): State<Arc<Fake>>,
        headers: HeaderMap,
        Path((_owner, _name, tree)): Path<(String, String, String)>,
        Query(query): Query<BTreeMap<String, String>>,
    ) -> Response {
        record(&fake, &format!("tree/{tree}"), &flat_query(&query));
        if let Some(response) = refuse(&fake, &headers) {
            return response;
        }
        let entries: Vec<Value> = fake
            .entries
            .lock()
            .expect("entries")
            .iter()
            .map(|(path, kind)| json!({"path": path, "type": kind, "mode": "100644", "sha": "x"}))
            .collect();
        let truncated = *fake.truncated.lock().expect("truncated");
        Json(json!({"sha": tree, "truncated": truncated, "tree": entries})).into_response()
    }

    async fn fake_contents(
        State(fake): State<Arc<Fake>>,
        headers: HeaderMap,
        Path((_owner, _name, path)): Path<(String, String, String)>,
        Query(query): Query<BTreeMap<String, String>>,
    ) -> Response {
        record(&fake, &format!("contents/{path}"), &flat_query(&query));
        if let Some(response) = refuse(&fake, &headers) {
            return response;
        }
        let raw = headers.get("accept").and_then(|value| value.to_str().ok()) == Some(RAW_ACCEPT);
        if !raw {
            return (StatusCode::NOT_ACCEPTABLE, "not raw".to_string()).into_response();
        }
        match fake.texts.lock().expect("texts").get(&path) {
            Some(text) => (StatusCode::OK, text.clone()).into_response(),
            None => (StatusCode::NOT_FOUND, "missing".to_string()).into_response(),
        }
    }

    /// The query as the fake received it, decoded, `k=v` pairs joined with `&`, for assertions.
    fn flat_query(query: &BTreeMap<String, String>) -> String {
        query
            .iter()
            .map(|(k, v)| format!("{k}={v}"))
            .collect::<Vec<_>>()
            .join("&")
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

    async fn fake_github(fake: Arc<Fake>) -> String {
        let app = Router::new()
            .route("/repos/{owner}/{name}/commits", get(fake_commits))
            .route("/repos/{owner}/{name}/git/trees/{tree}", get(fake_tree))
            .route("/repos/{owner}/{name}/contents/{*path}", get(fake_contents))
            .with_state(fake);
        format!("http://{}", serve(app).await)
    }

    fn source(git_ref: &str) -> GithubConfig {
        GithubConfig {
            repo: "uhstray-io/agent-cloud".into(),
            git_ref: git_ref.into(),
        }
    }

    /// The OPA and OpenBao files the tree test uses, plus files that are not policies.
    fn seeded() -> Arc<Fake> {
        let fake = Arc::new(Fake::default());
        *fake.entries.lock().expect("entries") = [
            ("README.md", "blob"),
            (
                "platform/services/opa/deployment/policies/system/authz.rego",
                "blob",
            ),
            (
                "platform/services/opa/deployment/policies/system/authz_test.rego",
                "blob",
            ),
            (
                "platform/services/opa/deployment/policies/agentcloud/data.json",
                "blob",
            ),
            (
                "platform/services/opa/deployment/policies/notes.txt",
                "blob",
            ),
            ("platform/services/opa/deployment/policies/system", "tree"),
            (
                "platform/services/openbao/deployment/config/policies/console.hcl",
                "blob",
            ),
            (
                "platform/services/openbao/deployment/config/policies/README.md",
                "blob",
            ),
            (
                "platform/services/openbao/deployment/config/policies/nested/x.hcl",
                "blob",
            ),
            (
                "platform/services/openbao/deployment/config/policies/broken.hcl",
                "tree",
            ),
        ]
        .into_iter()
        .map(|(path, kind)| (path.to_string(), kind.to_string()))
        .collect();
        {
            let mut texts = fake.texts.lock().expect("texts");
            texts.insert(
                "platform/services/opa/deployment/policies/system/authz.rego".into(),
                "# package doc: not this one\n\n  package system.authz   # the rules\n\ndefault allow := false\n".into(),
            );
            texts.insert(
                "platform/services/opa/deployment/policies/system/authz_test.rego".into(),
                "package system.authz_test\n".into(),
            );
        }
        {
            let mut commits = fake.commits.lock().expect("commits");
            commits.insert(
                "platform/services/opa/deployment/policies/system/authz.rego".into(),
                json!({"sha": "23d9511aaaa", "commit": {
                    "message": "security(opa): refuse decisions\n\nlong body",
                    "committer": {"date": "2026-10-30T12:00:00Z"},
                    "tree": {"sha": "t1"}}}),
            );
            commits.insert(
                "platform/services/openbao/deployment/config/policies/console.hcl".into(),
                json!({"sha": "9ab0c1d2e3f", "commit": {
                    "message": "feat(openbao): console read",
                    "committer": {"date": "2026-09-01T00:00:00Z"},
                    "tree": {"sha": "t2"}}}),
            );
        }
        fake
    }

    #[test]
    fn classify_maps_each_policy_kind_and_refuses_everything_else() {
        assert_eq!(
            classify("platform/services/opa/deployment/policies/system/authz.rego"),
            Some(Kind::OpaPolicy)
        );
        assert_eq!(
            classify("platform/services/opa/deployment/policies/system/authz_test.rego"),
            Some(Kind::OpaTest),
            "the _test suffix wins over .rego"
        );
        assert_eq!(
            classify("platform/services/opa/deployment/policies/agentcloud/data.json"),
            Some(Kind::OpaData)
        );
        assert_eq!(
            classify("platform/services/openbao/deployment/config/policies/console.hcl"),
            Some(Kind::OpenBao)
        );
        for not_policy in [
            "platform/services/opa/deployment/policies/notes.txt",
            "platform/services/opa/deployment/policies/system",
            "platform/services/openbao/deployment/config/policies/README.md",
            "platform/services/openbao/deployment/config/policies/nested/x.hcl",
            "platform/services/openbao/deployment/config/policies.hcl",
            "platform/services/opa/deployment/other/x.rego",
            "README.md",
        ] {
            assert_eq!(classify(not_policy), None, "{not_policy}");
        }
    }

    #[test]
    fn the_rego_package_is_the_first_real_package_line_without_its_comment() {
        assert_eq!(rego_package("package a.b\n").as_deref(), Some("a.b"));
        assert_eq!(
            rego_package("# package not.this\n  package   system.authz   # rules\n").as_deref(),
            Some("system.authz")
        );
        assert_eq!(
            rego_package("\t package x_test\n").as_deref(),
            Some("x_test")
        );
        assert_eq!(rego_package("packages are not a line\n"), None);
        assert_eq!(
            rego_package("package\n"),
            None,
            "a bare keyword names nothing"
        );
        assert_eq!(rego_package("package   # only a comment\n"), None);
        assert_eq!(rego_package("default allow := false\n"), None);
    }

    #[test]
    fn the_html_url_is_the_blob_page_on_the_ref() {
        let url = html_url(
            &source("feat/policy-page"),
            "platform/services/opa/deployment/policies/system/authz.rego",
        )
        .expect("url");
        assert_eq!(
            url,
            "https://github.com/uhstray-io/agent-cloud/blob/feat/policy-page/platform/services/opa/deployment/policies/system/authz.rego"
        );
    }

    #[test]
    fn endpoint_refuses_dot_segments_and_encodes_the_rest() {
        assert!(endpoint("http://x", &["a", "..", "b"]).is_err());
        assert!(endpoint("http://x", &["a", ".", "b"]).is_err());
        assert!(endpoint("http://x", &["a", "", "b"]).is_err());
        let url = endpoint("http://x", &["a?b", "c#d"]).expect("url");
        assert_eq!(url.as_str(), "http://x/a%3Fb/c%23d");
    }

    /// The four files `seeded` lists, in path order: OPA data, OPA policy, OPA test, OpenBao policy.
    #[tokio::test]
    async fn a_listing_names_kinds_commits_and_links_only_the_policy_files() {
        let fake = seeded();
        let api = fake_github(fake.clone()).await;
        let listing = list(
            &reqwest::Client::new(),
            &api,
            &Secret::new(TOKEN),
            &source("dev"),
            80,
            200,
        )
        .await
        .expect("listing");

        assert!(!listing.truncated);
        let rows: Vec<(String, &str, Option<String>)> = listing
            .files
            .iter()
            .map(|f| {
                (
                    f.name.clone(),
                    f.kind.as_str(),
                    f.last_commit.as_ref().map(|c| c.sha.clone()),
                )
            })
            .collect();
        assert_eq!(
            rows,
            vec![
                ("data.json".to_string(), "opa-data", None),
                (
                    "system.authz".to_string(),
                    "opa-policy",
                    Some("23d9511".to_string())
                ),
                ("system.authz_test".to_string(), "opa-test", None),
                (
                    "console".to_string(),
                    "openbao",
                    Some("9ab0c1d".to_string())
                ),
            ]
        );
        let authz = &listing.files[1];
        assert_eq!(
            authz.path,
            "platform/services/opa/deployment/policies/system/authz.rego"
        );
        assert_eq!(
            authz.url,
            "https://github.com/uhstray-io/agent-cloud/blob/dev/platform/services/opa/deployment/policies/system/authz.rego"
        );
        let commit = authz.last_commit.as_ref().expect("commit");
        assert_eq!(commit.date, "2026-10-30T12:00:00Z");
        assert_eq!(
            commit.subject, "security(opa): refuse decisions",
            "first line only"
        );
    }

    #[tokio::test]
    async fn the_reads_carry_the_token_the_raw_accept_and_one_snapshot() {
        let fake = seeded();
        let api = fake_github(fake.clone()).await;
        list(
            &reqwest::Client::new(),
            &api,
            &Secret::new(TOKEN),
            &source("dev"),
            80,
            200,
        )
        .await
        .expect("listing");

        assert_eq!(
            *fake.unauthorized.lock().expect("unauthorized"),
            0,
            "every request bore the token"
        );
        let seen = fake.seen.lock().expect("seen").clone();
        assert!(
            seen.iter()
                .any(|(p, q)| p == "commits" && q == "per_page=1&sha=dev"),
            "{seen:?}"
        );
        assert!(
            seen.iter()
                .any(|(p, q)| *p == format!("tree/{TREE_SHA}") && q == "recursive=1"),
            "{seen:?}"
        );
        let raw: Vec<_> = seen
            .iter()
            .filter(|(p, _)| p.starts_with("contents/"))
            .collect();
        assert_eq!(
            raw.len(),
            2,
            "one raw read per Rego file, none for JSON or HCL"
        );
        assert!(
            raw.iter().all(|(_, q)| *q == format!("ref={SHA_TIP}")),
            "the raw reads name the tip SHA, not the branch: {raw:?}"
        );
        let per_file: Vec<_> = seen
            .iter()
            .filter(|(p, q)| p == "commits" && q.starts_with("path="))
            .collect();
        assert_eq!(per_file.len(), 4, "one commit read per listed file");
        assert!(
            per_file
                .iter()
                .all(|(_, q)| q.ends_with(&format!("sha={SHA_TIP}"))),
            "{per_file:?}"
        );
    }

    #[tokio::test]
    async fn a_slash_named_ref_reaches_github_as_one_query_value() {
        let fake = seeded();
        let api = fake_github(fake.clone()).await;
        let listing = list(
            &reqwest::Client::new(),
            &api,
            &Secret::new(TOKEN),
            &source("ci/python-o11y-shard"),
            80,
            200,
        )
        .await
        .expect("listing");

        let seen = fake.seen.lock().expect("seen").clone();
        assert!(
            seen.iter()
                .any(|(p, q)| p == "commits" && q == "per_page=1&sha=ci/python-o11y-shard"),
            "{seen:?}"
        );
        assert!(listing.files[0].url.contains("/blob/ci/python-o11y-shard/"));
    }

    #[tokio::test]
    async fn the_file_bound_cuts_by_path_before_any_file_is_read() {
        let fake = seeded();
        let api = fake_github(fake.clone()).await;
        let listing = list(
            &reqwest::Client::new(),
            &api,
            &Secret::new(TOKEN),
            &source("dev"),
            2,
            200,
        )
        .await
        .expect("listing");

        assert!(listing.truncated, "two of four files kept is a cut");
        let paths: Vec<&str> = listing.files.iter().map(|f| f.path.as_str()).collect();
        assert_eq!(
            paths,
            [
                "platform/services/opa/deployment/policies/agentcloud/data.json",
                "platform/services/opa/deployment/policies/system/authz.rego",
            ]
        );
        let seen = fake.seen.lock().expect("seen").clone();
        assert!(
            !seen.iter().any(|(p, q)| p.contains("authz_test")
                || q.contains("authz_test")
                || q.contains("console.hcl")),
            "a file past the bound is never read: {seen:?}"
        );
    }

    #[tokio::test]
    async fn the_request_bound_stops_the_read_before_the_request_past_it() {
        let fake = seeded();
        let api = fake_github(fake.clone()).await;
        // Tip and tree (2), one commit for data.json (1), a raw and a commit read for each of the two
        // Rego files (4), one commit for console.hcl (1): exactly eight.
        list(
            &reqwest::Client::new(),
            &api,
            &Secret::new(TOKEN),
            &source("dev"),
            80,
            8,
        )
        .await
        .expect("the bound itself is allowed");

        let before = fake.seen.lock().expect("seen").len();
        let err = list(
            &reqwest::Client::new(),
            &api,
            &Secret::new(TOKEN),
            &source("dev"),
            80,
            7,
        )
        .await
        .expect_err("one request short");
        assert_eq!(err, FetchError::Bound);
        assert_eq!(
            fake.seen.lock().expect("seen").len() - before,
            7,
            "exactly the bound was sent, none past it"
        );
    }

    #[tokio::test]
    async fn a_truncated_tree_is_an_error_not_a_partial_list() {
        let fake = seeded();
        *fake.truncated.lock().expect("truncated") = true;
        let api = fake_github(fake.clone()).await;
        let err = list(
            &reqwest::Client::new(),
            &api,
            &Secret::new(TOKEN),
            &source("dev"),
            80,
            200,
        )
        .await
        .expect_err("a truncated tree must not list");
        assert_eq!(err, FetchError::TreeTruncated);
    }

    /// One listing against the fake, with the refusal the test set. Returns the error.
    async fn refused_listing(fake: &Arc<Fake>) -> FetchError {
        let api = fake_github(fake.clone()).await;
        list(
            &reqwest::Client::new(),
            &api,
            &Secret::new(TOKEN),
            &source("dev"),
            80,
            200,
        )
        .await
        .expect_err("the fake refused the request")
    }

    #[tokio::test]
    async fn a_server_error_names_its_status_and_never_the_token() {
        let fake = seeded();
        *fake.fail_with.lock().expect("fail_with") = Some(500);
        let err = refused_listing(&fake).await;
        assert_eq!(err, FetchError::Status(500));
        assert_eq!(err.to_string(), "GitHub answered HTTP 500");
        assert!(!err.to_string().contains(TOKEN));
    }

    #[tokio::test]
    async fn a_401_names_the_expired_token_and_the_refresh_task() {
        let fake = seeded();
        *fake.fail_with.lock().expect("fail_with") = Some(401);
        let err = refused_listing(&fake).await;
        assert_eq!(err, FetchError::Unauthorized);
        assert_eq!(
            err.to_string(),
            "GitHub refused the console token (HTTP 401): it is expired or invalid. Check that Refresh Console GitHub Token is running."
        );
        assert!(!err.to_string().contains(TOKEN));
    }

    #[tokio::test]
    async fn a_403_without_a_rate_limit_names_the_missing_permission() {
        let fake = seeded();
        *fake.fail_with.lock().expect("fail_with") = Some(403);
        let err = refused_listing(&fake).await;
        assert_eq!(err, FetchError::Forbidden);
        assert_eq!(
            err.to_string(),
            "GitHub refused the console token (HTTP 403): the App is not permitted to read this repository."
        );
        assert!(!err.to_string().contains(TOKEN));
    }

    #[tokio::test]
    async fn a_403_with_no_quota_left_is_a_rate_limit() {
        let fake = seeded();
        *fake.fail_with.lock().expect("fail_with") = Some(403);
        *fake.fail_headers.lock().expect("fail_headers") =
            vec![("x-ratelimit-remaining".into(), "0".into())];
        let err = refused_listing(&fake).await;
        assert_eq!(err, FetchError::RateLimited);
        assert_eq!(
            err.to_string(),
            "GitHub rate-limited the console (HTTP 403); the next read tries again."
        );
        assert!(!err.to_string().contains(TOKEN));
    }

    #[tokio::test]
    async fn a_403_with_retry_after_is_a_rate_limit() {
        let fake = seeded();
        *fake.fail_with.lock().expect("fail_with") = Some(403);
        *fake.fail_headers.lock().expect("fail_headers") =
            vec![("retry-after".into(), "60".into())];
        let err = refused_listing(&fake).await;
        assert_eq!(err, FetchError::RateLimited);
        assert!(!err.to_string().contains(TOKEN));
    }

    #[tokio::test]
    async fn a_403_with_quota_left_is_still_a_permission_refusal() {
        let fake = seeded();
        *fake.fail_with.lock().expect("fail_with") = Some(403);
        *fake.fail_headers.lock().expect("fail_headers") =
            vec![("x-ratelimit-remaining".into(), "4999".into())];
        let err = refused_listing(&fake).await;
        assert_eq!(err, FetchError::Forbidden);
    }

    #[tokio::test]
    async fn a_rego_file_without_a_package_line_shows_its_file_name() {
        let fake = seeded();
        fake.texts.lock().expect("texts").insert(
            "platform/services/opa/deployment/policies/system/authz_test.rego".into(),
            "default x := 1\n".into(),
        );
        let api = fake_github(fake.clone()).await;
        let listing = list(
            &reqwest::Client::new(),
            &api,
            &Secret::new(TOKEN),
            &source("dev"),
            80,
            200,
        )
        .await
        .expect("listing");
        assert_eq!(listing.files[2].name, "authz_test.rego");
    }
}
