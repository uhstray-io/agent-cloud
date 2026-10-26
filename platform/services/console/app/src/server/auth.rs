//! OIDC login against Authentik: authorization code + PKCE (S256), a CSRF
//! `state`, and a `nonce` checked against the ID token. Everything the
//! callback needs lives in the server-side session until it is consumed.
//!
//! Groups come from the userinfo endpoint, not from custom ID-token claims:
//! the stock `CoreClient` stays unchanged and the groups claim rides in
//! `UserInfoClaims<GroupsClaim, _>`.

use dioxus::server::axum::extract::{Query, State};
use dioxus::server::axum::http::{header, StatusCode};
use dioxus::server::axum::response::{IntoResponse, Response};
use openidconnect::core::{
    CoreAuthenticationFlow, CoreClient, CoreGenderClaim, CoreProviderMetadata,
};
use openidconnect::{
    AdditionalClaims, AuthorizationCode, ClientId, ClientSecret, CsrfToken, EndpointMaybeSet,
    EndpointNotSet, EndpointSet, IssuerUrl, Nonce, OAuth2TokenResponse, PkceCodeChallenge,
    PkceCodeVerifier, RedirectUrl, UserInfoClaims,
};
use serde::{Deserialize, Serialize};
use tower_sessions::Session;

use super::api::error;
use super::config::Config;
use super::SharedState;

/// Client after discovery: the auth, token and userinfo endpoints are set.
pub type OidcClient = CoreClient<
    EndpointSet,
    EndpointNotSet,
    EndpointNotSet,
    EndpointNotSet,
    EndpointMaybeSet,
    EndpointMaybeSet,
>;

const KEY_PKCE: &str = "auth.pkce_verifier";
const KEY_CSRF: &str = "auth.csrf_state";
const KEY_NONCE: &str = "auth.nonce";
pub const KEY_USER: &str = "user";

/// Identity kept in the session and returned as `claims` by `GET /v1/me`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CurrentUser {
    pub sub: String,
    pub email: Option<String>,
    pub name: Option<String>,
    pub username: Option<String>,
    pub groups: Vec<String>,
}

/// Additional userinfo claim carrying Authentik's `groups` (absent means none).
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct GroupsClaim {
    #[serde(default)]
    pub groups: Vec<String>,
}

impl AdditionalClaims for GroupsClaim {}

/// Fetches the provider metadata once. Errors are plain text with no secret in them.
pub async fn discover(state: &SharedState) -> Result<OidcClient, String> {
    let config: &Config = &state.config;
    let issuer = IssuerUrl::new(config.oidc_issuer.clone())
        .map_err(|err| format!("CONSOLE_OIDC_ISSUER is not a valid URL: {err}"))?;
    let metadata = CoreProviderMetadata::discover_async(issuer, &state.http)
        .await
        .map_err(|err| format!("OIDC discovery at {} failed: {err}", config.oidc_issuer))?;
    let redirect = RedirectUrl::new(format!("{}/auth/callback", config.base_url))
        .map_err(|err| format!("redirect URL is not valid: {err}"))?;
    let client = CoreClient::from_provider_metadata(
        metadata,
        ClientId::new(config.oidc_client_id.clone()),
        Some(ClientSecret::new(config.oidc_client_secret.clone())),
    )
    .set_redirect_uri(redirect);
    Ok(client)
}

pub async fn login(State(state): State<SharedState>, session: Session) -> Response {
    let Some(client) = state.oidc.get() else {
        return error(StatusCode::SERVICE_UNAVAILABLE, "oidc_unavailable");
    };
    let (pkce_challenge, pkce_verifier) = PkceCodeChallenge::new_random_sha256();
    let (url, csrf, nonce) = client
        .authorize_url(
            CoreAuthenticationFlow::AuthorizationCode,
            CsrfToken::new_random,
            Nonce::new_random,
        )
        .add_scope(openidconnect::Scope::new("profile".to_string()))
        .add_scope(openidconnect::Scope::new("email".to_string()))
        .set_pkce_challenge(pkce_challenge)
        .url();

    if store(&session, KEY_PKCE, pkce_verifier.secret())
        .await
        .is_err()
        || store(&session, KEY_CSRF, csrf.secret()).await.is_err()
        || store(&session, KEY_NONCE, nonce.secret()).await.is_err()
    {
        return error(StatusCode::INTERNAL_SERVER_ERROR, "session_unavailable");
    }
    found(url.as_str())
}

#[derive(Deserialize)]
pub struct CallbackQuery {
    code: Option<String>,
    state: Option<String>,
    error: Option<String>,
}

pub async fn callback(
    State(app): State<SharedState>,
    session: Session,
    Query(query): Query<CallbackQuery>,
) -> Response {
    let Some(client) = app.oidc.get() else {
        return error(StatusCode::SERVICE_UNAVAILABLE, "oidc_unavailable");
    };

    // Each attempt's values are consumed here, whatever the outcome, so a
    // callback cannot be replayed against the same session.
    let (Ok(csrf), Ok(verifier), Ok(nonce)) = (
        take(&session, KEY_CSRF).await,
        take(&session, KEY_PKCE).await,
        take(&session, KEY_NONCE).await,
    ) else {
        return error(StatusCode::INTERNAL_SERVER_ERROR, "session_unavailable");
    };

    if let Some(idp_error) = query.error {
        // The callback query is attacker-controlled text, so it is escaped and capped
        // before it reaches the log: a raw newline could forge a second log line.
        eprintln!(
            "console: login ended at the identity provider: {}",
            log_safe(&idp_error)
        );
        return found("/");
    }
    let (Some(expected_state), Some(verifier), Some(nonce), Some(code), Some(returned_state)) =
        (csrf, verifier, nonce, query.code, query.state)
    else {
        return error(StatusCode::BAD_REQUEST, "login_state_missing");
    };
    if !state_matches(Some(&expected_state), Some(&returned_state)) {
        return error(StatusCode::BAD_REQUEST, "state_mismatch");
    }

    let exchange = match client.exchange_code(AuthorizationCode::new(code)) {
        Ok(request) => request
            .set_pkce_verifier(PkceCodeVerifier::new(verifier))
            .request_async(&app.http)
            .await
            .map_err(|_| "token_exchange_failed"),
        Err(_) => Err("token_endpoint_missing"),
    };
    // The token response and its parse errors can contain tokens: log nothing from them.
    let token = match exchange {
        Ok(token) => token,
        Err(code) => return error(StatusCode::BAD_GATEWAY, code),
    };

    let Some(id_token) = token.extra_fields().id_token() else {
        return error(StatusCode::BAD_GATEWAY, "id_token_missing");
    };
    let verifier_claims = client.id_token_verifier();
    let id_claims = match id_token.claims(&verifier_claims, &Nonce::new(nonce)) {
        Ok(claims) => claims,
        Err(_) => return error(StatusCode::UNAUTHORIZED, "id_token_invalid"),
    };
    let subject = id_claims.subject().clone();

    let info_request = match client.user_info(token.access_token().clone(), Some(subject.clone())) {
        Ok(request) => request,
        Err(_) => return error(StatusCode::BAD_GATEWAY, "userinfo_endpoint_missing"),
    };
    let info: UserInfoClaims<GroupsClaim, CoreGenderClaim> =
        match info_request.request_async(&app.http).await {
            Ok(info) => info,
            Err(_) => return error(StatusCode::BAD_GATEWAY, "userinfo_failed"),
        };

    let user = CurrentUser {
        sub: subject.as_str().to_string(),
        email: info.email().map(|value| value.as_str().to_string()),
        name: info
            .name()
            .and_then(|localized| localized.get(None))
            .map(|value| value.as_str().to_string()),
        username: info
            .preferred_username()
            .map(|value| value.as_str().to_string()),
        groups: info.additional_claims().groups.clone(),
    };

    // A new session id on login: the pre-login cookie cannot be reused as a logged-in one.
    if session.cycle_id().await.is_err() || session.insert(KEY_USER, &user).await.is_err() {
        return error(StatusCode::INTERNAL_SERVER_ERROR, "session_unavailable");
    }
    found("/")
}

pub async fn logout(session: Session) -> Response {
    if session.flush().await.is_err() {
        return error(StatusCode::INTERNAL_SERVER_ERROR, "session_unavailable");
    }
    found("/")
}

/// The signed-in user for this request, if any. A session value that does not
/// parse counts as signed out.
pub async fn current_user(session: &Session) -> Option<CurrentUser> {
    session.get::<CurrentUser>(KEY_USER).await.ok().flatten()
}

/// CSRF check on the callback: both values must be present and equal.
pub fn state_matches(expected: Option<&str>, returned: Option<&str>) -> bool {
    match (expected, returned) {
        (Some(expected), Some(returned)) => !expected.is_empty() && expected == returned,
        _ => false,
    }
}

async fn store(
    session: &Session,
    key: &str,
    value: &str,
) -> Result<(), tower_sessions::session::Error> {
    session.insert(key, value).await
}

async fn take(
    session: &Session,
    key: &str,
) -> Result<Option<String>, tower_sessions::session::Error> {
    session.remove::<String>(key).await
}

/// Untrusted text as one log field: at most 64 characters, debug-escaped, so
/// quotes, control characters and newlines cannot break or forge a log line.
pub fn log_safe(text: &str) -> String {
    let shown: String = text.chars().take(64).collect();
    format!("{shown:?}")
}

/// 302 to `location`. axum's `Redirect` offers 303/307/308 only, and the contract says 302.
fn found(location: &str) -> Response {
    (
        StatusCode::FOUND,
        [(header::LOCATION, location.to_string())],
    )
        .into_response()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn state_must_be_present_and_equal() {
        assert!(state_matches(Some("abc"), Some("abc")));
        assert!(!state_matches(Some("abc"), Some("abd")));
        assert!(!state_matches(Some("abc"), None));
        assert!(!state_matches(None, Some("abc")));
        assert!(!state_matches(Some(""), Some("")));
    }

    #[test]
    fn group_claim_defaults_to_empty() {
        let claim: GroupsClaim = serde_json::from_str("{}").expect("missing groups is fine");
        assert!(claim.groups.is_empty());
    }

    #[test]
    fn logged_idp_error_is_escaped_and_capped() {
        let forged = "access_denied\nconsole: forged line";
        assert_eq!(log_safe(forged), "\"access_denied\\nconsole: forged line\"");
        let long = "x".repeat(200);
        // 64 characters plus the two quotes the debug form adds.
        assert_eq!(log_safe(&long).chars().count(), 66);
    }
}
