//! Sign-in gate (task 2.6). The real `/auth/login` and `/auth/callback` handlers run against an
//! in-process OpenID provider that serves discovery, JWKS, token and userinfo and signs RS256 ID
//! tokens. Only the provider is fake: discovery, the code exchange and the ID-token check are the
//! production code paths.

use std::collections::HashMap;
use std::net::SocketAddr;
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{SystemTime, UNIX_EPOCH};

use dioxus::server::axum::extract::State;
use dioxus::server::axum::http::{header, HeaderMap, StatusCode};
use dioxus::server::axum::response::{IntoResponse, Response};
use dioxus::server::axum::routing::{get, post};
use dioxus::server::axum::{Json, Router};
use openidconnect::core::{
    CoreIdToken, CoreIdTokenClaims, CoreJsonWebKeySet, CoreJwsSigningAlgorithm,
    CoreRsaPrivateSigningKey,
};
use openidconnect::url::{form_urlencoded, Url};
use openidconnect::{JsonWebKeyId, PkceCodeChallenge, PkceCodeVerifier, PrivateSigningKey};
use reqwest::header::{COOKIE, LOCATION, SET_COOKIE};
use rsa::pkcs1::EncodeRsaPrivateKey;
use serde_json::{json, Value};
use tokio::net::TcpListener;

use super::tests::{memory_sessions, serve, test_state};
use super::{api_router, auth, session_layer, SharedState};

/// The client and redirect URI that `test_state` configures for the console.
const CLIENT_ID: &str = "console";
const REDIRECT_URI: &str = "https://console.test/auth/callback";
const KID: &str = "test";
const CODE: &str = "fake-code";
const ACCESS_TOKEN: &str = "fake-access-token";
const SUBJECT: &str = "user-1";
const EMAIL: &str = "ada@example.test";
const GROUP: &str = "ops";

/// What the provider expects from the console's token request and what it signs into the ID token.
/// The test fills it in from the login redirect, as a browser would carry the values across.
#[derive(Clone, Default)]
struct Expected {
    challenge: String,
    nonce: String,
    /// Sign the ID token with a key the JWKS does not publish.
    unpublished_key: bool,
}

struct Provider {
    issuer: String,
    key: CoreRsaPrivateSigningKey,
    expected: Mutex<Expected>,
}

impl Provider {
    fn new(issuer: String) -> Self {
        let key = CoreRsaPrivateSigningKey::from_pem(
            signing_key_pem(),
            Some(JsonWebKeyId::new(KID.to_string())),
        )
        .expect("load the provider's signing key");
        Self {
            issuer,
            key,
            expected: Mutex::new(Expected::default()),
        }
    }

    fn endpoint(&self, suffix: &str) -> String {
        format!("{}{suffix}", self.issuer)
    }

    fn expect_login(&self, challenge: &str, nonce: &str) {
        let mut expected = self.expected.lock().expect("expected lock");
        expected.challenge = challenge.to_string();
        expected.nonce = nonce.to_string();
    }

    /// The next token response is signed with a second key. The JWKS still publishes only `key`,
    /// under the same kid, so the console finds the published key and the signature fails.
    fn sign_with_unpublished_key(&self) {
        // Generate the key now, before the callback request: a debug-build RSA keygen inside the
        // token handler would count against the console's token-exchange timeout.
        let _ = unpublished_signing_key();
        self.expected.lock().expect("expected lock").unpublished_key = true;
    }

    fn id_token(&self, nonce: &str, key: &CoreRsaPrivateSigningKey) -> Value {
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system clock after 1970")
            .as_secs();
        let claims: CoreIdTokenClaims = serde_json::from_value(json!({
            "iss": self.issuer,
            "sub": SUBJECT,
            "aud": [CLIENT_ID],
            "exp": now + 3600,
            "iat": now,
            "nonce": nonce,
        }))
        .expect("ID token claims");
        let token = CoreIdToken::new(
            claims,
            key,
            CoreJwsSigningAlgorithm::RsaSsaPkcs1V15Sha256,
            None,
            None,
        )
        .expect("sign the ID token");
        serde_json::to_value(token).expect("ID token as a JSON string")
    }
}

/// The provider's RSA key, in the PKCS#1 PEM form `CoreRsaPrivateSigningKey::from_pem` reads. It is
/// generated on first use and shared by every test in this binary, because generating it is the
/// slow step.
fn signing_key_pem() -> &'static str {
    static PEM: OnceLock<String> = OnceLock::new();
    PEM.get_or_init(generate_rsa_pem)
}

/// A second key, distinct from the first, for the one test that signs with a key the JWKS does not
/// publish. It is generated only when that test asks for it, so no other test pays for it.
fn unpublished_signing_key() -> CoreRsaPrivateSigningKey {
    static PEM: OnceLock<String> = OnceLock::new();
    let pem = PEM.get_or_init(generate_rsa_pem);
    CoreRsaPrivateSigningKey::from_pem(pem, Some(JsonWebKeyId::new(KID.to_string())))
        .expect("load the unpublished signing key")
}

fn generate_rsa_pem() -> String {
    let key = rsa::RsaPrivateKey::new(&mut rsa::rand_core::OsRng, 2048)
        .expect("generate an RSA key for the provider");
    key.to_pkcs1_pem(rsa::pkcs1::LineEnding::LF)
        .expect("encode an RSA key for the provider")
        .to_string()
}

/// Authentik's shape: the issuer is a path ending in `/`, so discovery appends its suffix to it.
fn provider_routes(provider: Arc<Provider>) -> Router {
    Router::new()
        .route(
            "/application/o/console/.well-known/openid-configuration",
            get(discovery),
        )
        .route("/application/o/console/jwks/", get(jwks))
        .route("/application/o/console/token/", post(token))
        .route("/application/o/console/userinfo/", get(userinfo))
        .with_state(provider)
}

async fn discovery(State(provider): State<Arc<Provider>>) -> Json<Value> {
    Json(json!({
        "issuer": provider.issuer,
        "authorization_endpoint": provider.endpoint("authorize/"),
        "token_endpoint": provider.endpoint("token/"),
        "userinfo_endpoint": provider.endpoint("userinfo/"),
        "jwks_uri": provider.endpoint("jwks/"),
        "response_types_supported": ["code"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
    }))
}

async fn jwks(State(provider): State<Arc<Provider>>) -> Json<Value> {
    let keys = CoreJsonWebKeySet::new(vec![provider.key.as_verification_key()]);
    Json(serde_json::to_value(keys).expect("JWKS as JSON"))
}

/// The code grant, checked the way a provider checks it: the code, the redirect URI and the PKCE
/// verifier must all match what the login redirect promised.
async fn token(State(provider): State<Arc<Provider>>, body: String) -> Response {
    let form: HashMap<String, String> = form_urlencoded::parse(body.as_bytes())
        .into_owned()
        .collect();
    let expected = provider.expected.lock().expect("expected lock").clone();
    // The verifier must be 43 to 128 characters (RFC 7636) before it is hashed, so a short one is refused.
    let pkce_matches = form.get("code_verifier").is_some_and(|verifier| {
        (43..=128).contains(&verifier.len())
            && PkceCodeChallenge::from_code_verifier_sha256(&PkceCodeVerifier::new(
                verifier.clone(),
            ))
            .as_str()
                == expected.challenge
    });
    let valid = form.get("grant_type").map(String::as_str) == Some("authorization_code")
        && form.get("code").map(String::as_str) == Some(CODE)
        && form.get("redirect_uri").map(String::as_str) == Some(REDIRECT_URI)
        && pkce_matches;
    if !valid {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({ "error": "invalid_grant" })),
        )
            .into_response();
    }
    let unpublished = expected.unpublished_key.then(unpublished_signing_key);
    let signer = unpublished.as_ref().unwrap_or(&provider.key);
    Json(json!({
        "access_token": ACCESS_TOKEN,
        "token_type": "Bearer",
        "expires_in": 3600,
        "id_token": provider.id_token(&expected.nonce, signer),
    }))
    .into_response()
}

async fn userinfo(headers: HeaderMap) -> Response {
    let bearer = format!("Bearer {ACCESS_TOKEN}");
    let authorized = headers
        .get(header::AUTHORIZATION)
        .and_then(|value| value.to_str().ok())
        == Some(bearer.as_str());
    if !authorized {
        return (
            StatusCode::UNAUTHORIZED,
            Json(json!({ "error": "invalid_token" })),
        )
            .into_response();
    }
    Json(json!({
        "sub": SUBJECT,
        "email": EMAIL,
        "name": "Ada",
        "preferred_username": "ada",
        "groups": [GROUP],
    }))
    .into_response()
}

/// Starts the provider on a loopback port, then a console whose OIDC cell holds the client that
/// discovery produced from it. That is the state `spawn_discovery` leaves behind on a healthy start.
async fn console_with_provider() -> (SharedState, Arc<Provider>) {
    let listener = TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind the provider");
    let addr = listener.local_addr().expect("provider address");
    let provider = Arc::new(Provider::new(format!(
        "http://{addr}/application/o/console/"
    )));
    let app = provider_routes(provider.clone());
    tokio::spawn(async move {
        dioxus::server::axum::serve(listener, app)
            .await
            .expect("serve the provider");
    });

    let mut state = test_state();
    Arc::get_mut(&mut state)
        .expect("a fresh state has one owner")
        .config
        .oidc_issuer = provider.issuer.clone();
    let client = auth::discover(&state)
        .await
        .expect("discovery against the provider");
    assert!(
        state.oidc.set(client).is_ok(),
        "the OIDC cell was already set"
    );
    (state, provider)
}

/// One GET to the console over HTTP. Redirects are not followed, so the 302 itself is what the test
/// sees. `session` is the `console_session=...` pair a browser would send back.
struct Sent {
    status: u16,
    location: Option<String>,
    session: Option<String>,
    body: String,
}

async fn get_from(addr: SocketAddr, path: &str, cookie: Option<&str>) -> Sent {
    let client = reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .expect("HTTP client");
    let mut request = client.get(format!("http://{addr}{path}"));
    if let Some(cookie) = cookie {
        request = request.header(COOKIE, cookie);
    }
    let reply = request.send().await.expect("request to the console");
    let status = reply.status().as_u16();
    let location = reply
        .headers()
        .get(LOCATION)
        .map(|value| value.to_str().expect("ASCII Location").to_string());
    let session = reply
        .headers()
        .get_all(SET_COOKIE)
        .iter()
        .find_map(|value| value.to_str().ok())
        .and_then(|line| line.split(';').next())
        .map(str::to_string);
    let body = reply.text().await.expect("response body");
    Sent {
        status,
        location,
        session,
        body,
    }
}

/// The query parameters of an absolute URL, one value per name.
fn query_of(url: &str) -> HashMap<String, String> {
    Url::parse(url)
        .expect("absolute URL")
        .query_pairs()
        .into_owned()
        .collect()
}

#[tokio::test]
async fn sign_in_redirects_to_the_provider() {
    let (state, provider) = console_with_provider().await;
    let addr = serve(api_router(state).layer(session_layer(memory_sessions()))).await;

    let login = get_from(addr, "/auth/login", None).await;
    assert_eq!(login.status, 302, "{}", login.body);
    let location = login.location.expect("Location to the provider");
    assert!(
        location.starts_with(&provider.endpoint("authorize/")),
        "redirect is not to the provider's authorization endpoint: {location}"
    );

    let query = query_of(&location);
    assert_eq!(query["response_type"], "code");
    assert_eq!(query["client_id"], CLIENT_ID);
    assert_eq!(query["redirect_uri"], REDIRECT_URI);
    let scopes: Vec<&str> = query["scope"].split_whitespace().collect();
    for scope in ["openid", "profile", "email"] {
        assert!(
            scopes.contains(&scope),
            "scope {scope} missing from {scopes:?}"
        );
    }
    assert_eq!(query["code_challenge_method"], "S256");
    for name in ["code_challenge", "state", "nonce"] {
        assert!(!query[name].is_empty(), "{name} is empty");
    }
    assert!(login
        .session
        .expect("session cookie set at login")
        .starts_with("console_session="));
}

#[tokio::test]
async fn callback_stores_the_verified_user() {
    let (state, provider) = console_with_provider().await;
    let addr = serve(api_router(state).layer(session_layer(memory_sessions()))).await;

    let login = get_from(addr, "/auth/login", None).await;
    let login_session = login.session.expect("session cookie set at login");
    let query = query_of(&login.location.expect("Location to the provider"));
    // The provider now signs the nonce this login issued, as a real provider would.
    provider.expect_login(&query["code_challenge"], &query["nonce"]);

    let callback = get_from(
        addr,
        &format!("/auth/callback?code={CODE}&state={}", query["state"]),
        Some(login_session.as_str()),
    )
    .await;
    assert_eq!(callback.status, 302, "{}", callback.body);
    assert_eq!(callback.location.as_deref(), Some("/"));
    let signed_in = callback.session.expect("session cookie set at callback");
    assert_ne!(
        signed_in, login_session,
        "the session id is cycled at sign-in"
    );

    let me = get_from(addr, "/v1/me", Some(signed_in.as_str())).await;
    assert_eq!(me.status, 200, "{}", me.body);
    let me_json: Value = serde_json::from_str(&me.body).expect("JSON body");
    assert_eq!(me_json["claims"]["sub"], SUBJECT);
    assert_eq!(me_json["claims"]["email"], EMAIL);
    assert_eq!(me_json["claims"]["groups"][0], GROUP);
}

#[tokio::test]
async fn callback_with_a_wrong_nonce_is_refused() {
    let (state, provider) = console_with_provider().await;
    let addr = serve(api_router(state).layer(session_layer(memory_sessions()))).await;

    let login = get_from(addr, "/auth/login", None).await;
    let login_session = login.session.expect("session cookie set at login");
    let query = query_of(&login.location.expect("Location to the provider"));
    // The provider signs a nonce that this login never issued.
    provider.expect_login(&query["code_challenge"], "a-nonce-this-login-never-issued");

    let callback = get_from(
        addr,
        &format!("/auth/callback?code={CODE}&state={}", query["state"]),
        Some(login_session.as_str()),
    )
    .await;
    assert_eq!(callback.status, 401, "{}", callback.body);
    assert_eq!(callback.body, r#"{"error":"id_token_invalid"}"#);

    // Nothing was stored. Probe with the cookie the refusal itself sets, the one the browser holds
    // afterwards: a refusal that stored a user under a new session id would be seen only here.
    let held = callback.session.unwrap_or(login_session);
    let me = get_from(addr, "/v1/me", Some(held.as_str())).await;
    assert_eq!(me.status, 401, "{}", me.body);
    assert_eq!(me.body, r#"{"error":"unauthenticated"}"#);
}

#[tokio::test]
async fn callback_with_a_wrong_state_is_refused() {
    let (state, provider) = console_with_provider().await;
    let addr = serve(api_router(state).layer(session_layer(memory_sessions()))).await;

    let login = get_from(addr, "/auth/login", None).await;
    let login_session = login.session.expect("session cookie set at login");
    let query = query_of(&login.location.expect("Location to the provider"));
    // The provider would accept this code and sign the right nonce, so only the state can refuse it.
    provider.expect_login(&query["code_challenge"], &query["nonce"]);

    let callback = get_from(
        addr,
        &format!("/auth/callback?code={CODE}&state=not-the-login-state"),
        Some(login_session.as_str()),
    )
    .await;
    assert_eq!(callback.status, 400, "{}", callback.body);
    assert_eq!(callback.body, r#"{"error":"state_mismatch"}"#);

    let held = callback.session.unwrap_or(login_session);
    let me = get_from(addr, "/v1/me", Some(held.as_str())).await;
    assert_eq!(me.status, 401, "{}", me.body);
    assert_eq!(me.body, r#"{"error":"unauthenticated"}"#);
}

#[tokio::test]
async fn callback_with_a_token_signed_by_another_key_is_refused() {
    let (state, provider) = console_with_provider().await;
    let addr = serve(api_router(state).layer(session_layer(memory_sessions()))).await;

    let login = get_from(addr, "/auth/login", None).await;
    let login_session = login.session.expect("session cookie set at login");
    let query = query_of(&login.location.expect("Location to the provider"));
    // The code, the state and the nonce are all right, so only the signature can refuse the token.
    provider.expect_login(&query["code_challenge"], &query["nonce"]);
    provider.sign_with_unpublished_key();

    let callback = get_from(
        addr,
        &format!("/auth/callback?code={CODE}&state={}", query["state"]),
        Some(login_session.as_str()),
    )
    .await;
    assert_eq!(callback.status, 401, "{}", callback.body);
    assert_eq!(callback.body, r#"{"error":"id_token_invalid"}"#);

    let held = callback.session.unwrap_or(login_session);
    let me = get_from(addr, "/v1/me", Some(held.as_str())).await;
    assert_eq!(me.status, 401, "{}", me.body);
    assert_eq!(me.body, r#"{"error":"unauthenticated"}"#);
}
