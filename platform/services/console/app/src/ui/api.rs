//! Browser-side calls to the console's `/v1` API, and the JSON shapes they return.
//!
//! reqwest cannot take a relative URL and web-sys is not an approved dependency,
//! so each call runs as a short script through Dioxus's JS bridge
//! (`document::eval`). The request is written into the script as a JSON
//! literal, so any value inside it is data and cannot change the script.

use dioxus::prelude::*;
use serde::de::DeserializeOwned;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

/// What a call returned. `status` is 0 when the request never completed, and
/// `body` is null when the response had no JSON body.
#[derive(Debug, Clone, Deserialize)]
pub struct Reply {
    pub status: u16,
    #[serde(default)]
    pub body: Value,
}

impl Reply {
    fn unreachable() -> Reply {
        Reply {
            status: 0,
            body: Value::Null,
        }
    }
}

/// The OIDC claims `/v1/me` returns. Read-only in the console: Authentik owns them.
#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct Claims {
    pub sub: String,
    pub email: Option<String>,
    pub name: Option<String>,
    pub username: Option<String>,
    #[serde(default)]
    pub groups: Vec<String>,
}

/// The preferences the console stores per user. `default_scope` is an `org`,
/// `org/site` or `org/site/env` path of ids, or null.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Preferences {
    pub theme: String,
    pub timezone: String,
    pub default_scope: Option<String>,
}

impl Default for Preferences {
    fn default() -> Self {
        Preferences {
            theme: "system".to_string(),
            timezone: "UTC".to_string(),
            default_scope: None,
        }
    }
}

/// Body of `GET /v1/me` and `PUT /v1/me`.
#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct Account {
    pub claims: Claims,
    pub preferences: Preferences,
}

/// A tenant as `GET /v1/tenants` lists it.
#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct NamedRef {
    pub id: String,
    pub name: String,
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct TenantList {
    #[serde(default)]
    pub example: bool,
    pub tenants: Vec<NamedRef>,
}

/// Body of `GET /v1/tenants/{t}/tree`.
#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct Tree {
    #[serde(default)]
    pub example: bool,
    pub orgs: Vec<Org>,
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct Org {
    pub id: String,
    pub name: String,
    pub sites: Vec<Site>,
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct Site {
    pub id: String,
    pub name: String,
    pub envs: Vec<String>,
}

/// The session as the browser sees it. `Loading` is also what the server
/// renders, so the first frame matches before the check returns.
#[derive(Debug, Clone, PartialEq)]
pub enum Me {
    Loading,
    SignedOut,
    SignedIn(Account),
    Failed,
}

const REQUEST_SCRIPT: &str = r#"
    const req = __REQUEST__;
    const init = { method: req.method, credentials: "same-origin", headers: { accept: "application/json" } };
    if (req.body !== null) {
        init.headers["content-type"] = "application/json";
        init.body = JSON.stringify(req.body);
    }
    const reply = { status: 0, body: null };
    try {
        const r = await fetch(req.path, init);
        reply.status = r.status;
        try { reply.body = await r.json(); } catch (_) { reply.body = null; }
    } catch (_) { reply.status = 0; }
    dioxus.send(reply);
"#;

/// One same-origin JSON call. `method` and `path` come from this crate's own
/// constants and tenant ids the server sent, never from typed input.
///
/// `document::eval` is Dioxus's own JS bridge, the one approved way to reach
/// `fetch` without a new crate. The script text is fixed; only the JSON-encoded
/// request is substituted into it, so no typed input runs as script.
pub async fn request(method: &str, path: &str, body: Option<Value>) -> Reply {
    let req = json!({ "method": method, "path": path, "body": body });
    let script = REQUEST_SCRIPT.replace("__REQUEST__", &req.to_string());
    let mut eval = document::eval(&script);
    eval.recv::<Reply>()
        .await
        .unwrap_or_else(|_| Reply::unreachable())
}

const WAIT_VISIBLE_SCRIPT: &str = r#"
    if (document.visibilityState !== "visible") {
        await new Promise((resolve) => {
            const onVisible = () => {
                if (document.visibilityState === "visible") {
                    document.removeEventListener("visibilitychange", onVisible);
                    resolve();
                }
            };
            document.addEventListener("visibilitychange", onVisible);
        });
    }
    dioxus.send(true);
"#;

/// Resolves once the tab is visible: at once if it is, else when it becomes so.
pub async fn wait_visible() {
    let mut eval = document::eval(WAIT_VISIBLE_SCRIPT);
    let _ = eval.recv::<bool>().await;
}

/// The sleep script. `__MS__` is replaced by `sleep_script`, and nothing else is.
const SLEEP_SCRIPT: &str = r#"
    await new Promise((resolve) => {
        const finish = () => {
            clearTimeout(timer);
            document.removeEventListener("visibilitychange", onChange);
            resolve();
        };
        const onChange = () => {
            if (document.visibilityState !== "visible") {
                finish();
            }
        };
        const timer = setTimeout(finish, __MS__);
        document.addEventListener("visibilitychange", onChange);
    });
    dioxus.send(true);
"#;

/// The sleep script with `ms` filled in. `ms` is a number from this crate, never input.
fn sleep_script(ms: u32) -> String {
    SLEEP_SCRIPT.replace("__MS__", &ms.to_string())
}

/// Resolves after `ms`, or earlier when the tab is hidden, so the caller can
/// wait for it to become visible again instead of polling while hidden.
pub async fn sleep_unless_hidden(ms: u32) {
    let mut eval = document::eval(&sleep_script(ms));
    let _ = eval.recv::<bool>().await;
}

const NOW_SCRIPT: &str = r#"dioxus.send(Date.now());"#;

/// The browser's clock in Unix milliseconds. `None` when the bridge fails.
pub async fn now_ms() -> Option<i64> {
    let mut eval = document::eval(NOW_SCRIPT);
    eval.recv::<i64>().await.ok()
}

/// The focus script for `focus`. Runs through the same `document::eval` bridge
/// as `request` (see the module doc). The id goes in as a JSON string literal,
/// so no quote in it can end the literal, and no user input is run as script.
fn focus_script(id: &str) -> String {
    format!(
        "document.getElementById({})?.focus();",
        Value::String(id.to_string())
    )
}

/// Moves keyboard focus to the element with `id`, if it is on the page.
pub fn focus(id: &str) {
    let _ = document::eval(&focus_script(id));
}

const THEME_SCRIPT: &str = r#"
    const theme = __THEME__;
    const root = document.documentElement;
    if (theme === "light" || theme === "dark") {
        root.setAttribute("data-theme", theme);
    } else {
        root.removeAttribute("data-theme");
    }
"#;

/// Puts a saved theme on `<html>`. `system` removes the attribute, so the
/// `prefers-color-scheme` rules decide.
pub fn apply_theme(theme: &str) {
    let script = THEME_SCRIPT.replace("__THEME__", &Value::String(theme.to_string()).to_string());
    let _ = document::eval(&script);
}

/// Parses a reply body into `T`. `None` when the body does not have that shape.
pub fn parse<T: DeserializeOwned>(reply: &Reply) -> Option<T> {
    serde_json::from_value(reply.body.clone()).ok()
}

/// Reads `GET /v1/me` (or the `PUT` that answers in the same shape).
pub fn classify_me(reply: &Reply) -> Me {
    match reply.status {
        200 => parse::<Account>(reply).map_or(Me::Failed, Me::SignedIn),
        401 => Me::SignedOut,
        _ => Me::Failed,
    }
}

/// The sentence a 422 from `PUT /v1/me` carries. The server writes fixed
/// sentences here, never the rejected input.
pub fn invalid_detail(reply: &Reply) -> Option<String> {
    if reply.status != 422 {
        return None;
    }
    reply.body["detail"].as_str().map(str::to_string)
}

/// Percent-encodes one path segment. Tenant ids come from the server, but the
/// URL should stay correct whatever characters an id carries.
pub fn segment(raw: &str) -> String {
    let mut out = String::with_capacity(raw.len());
    for byte in raw.bytes() {
        if byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.' | b'~') {
            out.push(byte as char);
        } else {
            out.push_str(&format!("%{byte:02X}"));
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn reply(status: u16, body: Value) -> Reply {
        Reply { status, body }
    }

    fn account_body() -> Value {
        json!({
            "claims": {
                "sub": "abc",
                "email": "ada@example.test",
                "name": "Ada",
                "username": "ada",
                "groups": ["platform-admins"]
            },
            "preferences": { "theme": "dark", "timezone": "UTC", "default_scope": null }
        })
    }

    #[test]
    fn unauthenticated_status_means_signed_out() {
        let r = reply(401, json!({"error": "unauthenticated"}));
        assert_eq!(classify_me(&r), Me::SignedOut);
    }

    #[test]
    fn signed_in_reads_claims_and_preferences() {
        match classify_me(&reply(200, account_body())) {
            Me::SignedIn(account) => {
                assert_eq!(account.claims.name.as_deref(), Some("Ada"));
                assert_eq!(account.claims.groups, vec!["platform-admins".to_string()]);
                assert_eq!(account.preferences.theme, "dark");
                assert_eq!(account.preferences.default_scope, None);
            }
            other => panic!("expected SignedIn, got {other:?}"),
        }
    }

    #[test]
    fn other_status_or_bad_body_is_failed() {
        assert_eq!(classify_me(&reply(500, Value::Null)), Me::Failed);
        assert_eq!(classify_me(&reply(0, Value::Null)), Me::Failed);
        assert_eq!(classify_me(&reply(200, json!({"claims": {}}))), Me::Failed);
    }

    #[test]
    fn missing_preferences_take_the_documented_defaults() {
        let body = json!({"claims": {"sub": "abc"}, "preferences": {}});
        match classify_me(&reply(200, body)) {
            Me::SignedIn(account) => assert_eq!(account.preferences, Preferences::default()),
            other => panic!("expected SignedIn, got {other:?}"),
        }
        assert_eq!(Preferences::default().theme, "system");
        assert_eq!(Preferences::default().timezone, "UTC");
    }

    #[test]
    fn invalid_detail_reads_only_a_422() {
        let r = reply(
            422,
            json!({"error": "invalid_preferences", "detail": "theme must be one of system, light, dark"}),
        );
        assert_eq!(
            invalid_detail(&r).as_deref(),
            Some("theme must be one of system, light, dark")
        );
        assert_eq!(invalid_detail(&reply(500, json!({"detail": "x"}))), None);
        assert_eq!(invalid_detail(&reply(422, json!({}))), None);
    }

    #[test]
    fn segment_keeps_plain_ids_and_escapes_the_rest() {
        assert_eq!(segment("uhstray"), "uhstray");
        assert_eq!(segment("tenant_1.a-b~c"), "tenant_1.a-b~c");
        assert_eq!(segment("a/b c?"), "a%2Fb%20c%3F");
    }

    #[test]
    fn focus_script_quotes_the_id_so_it_cannot_end_the_literal() {
        assert_eq!(
            focus_script("strip-sem-7"),
            r#"document.getElementById("strip-sem-7")?.focus();"#
        );
        assert_eq!(
            focus_script(r#"a");alert(1);("#),
            r#"document.getElementById("a\");alert(1);(")?.focus();"#
        );
    }

    #[test]
    fn sleep_script_carries_only_the_number() {
        let script = sleep_script(10_000);
        assert!(script.contains("setTimeout(finish, 10000)"));
        assert!(!script.contains("__MS__"));
    }
}
