//! JSON API under `/v1`. Every route checks the session first and answers 401
//! JSON without one, so an unauthenticated probe learns nothing about tenants.
//! The tenant routes then filter on the caller's groups: a tenant is a hard
//! isolation boundary, so a member sees only the tenants that name one of their groups.

use dioxus::server::axum::extract::rejection::JsonRejection;
use dioxus::server::axum::extract::{Path, State};
use dioxus::server::axum::http::StatusCode;
use dioxus::server::axum::response::{IntoResponse, Response};
use dioxus::server::axum::Json;
use serde::{Deserialize, Serialize};
use serde_json::json;
use tower_sessions::Session;

use super::auth::{self, CurrentUser};
use super::prefs::Preferences;
use super::scope::Org;
use super::SharedState;

/// `{"error": code}` with the given status.
pub fn error(status: StatusCode, code: &str) -> Response {
    (status, Json(json!({ "error": code }))).into_response()
}

fn unauthenticated() -> Response {
    error(StatusCode::UNAUTHORIZED, "unauthenticated")
}

#[derive(Serialize)]
struct MeBody {
    claims: CurrentUser,
    preferences: Preferences,
}

pub async fn get_me(State(app): State<SharedState>, session: Session) -> Response {
    let Some(user) = auth::current_user(&session).await else {
        return unauthenticated();
    };
    me_response(&app, user).await
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PutMe {
    preferences: Preferences,
}

/// Replaces the preferences. Fields left out of `preferences` take their defaults.
pub async fn put_me(
    State(app): State<SharedState>,
    session: Session,
    body: Result<Json<PutMe>, JsonRejection>,
) -> Response {
    let Some(user) = auth::current_user(&session).await else {
        return unauthenticated();
    };
    let Json(body) = match body {
        Ok(body) => body,
        Err(_) => return invalid_preferences("request body must be JSON {\"preferences\": {...}}"),
    };
    let prefs = match body.preferences.validate() {
        Ok(prefs) => prefs,
        Err(reason) => return invalid_preferences(reason),
    };
    if app.prefs.put(&user.sub, &prefs).await.is_err() {
        return error(StatusCode::INTERNAL_SERVER_ERROR, "preferences_unavailable");
    }
    me_response(&app, user).await
}

async fn me_response(app: &SharedState, user: CurrentUser) -> Response {
    match app.prefs.get(&user.sub).await {
        Ok(preferences) => Json(MeBody {
            claims: user,
            preferences,
        })
        .into_response(),
        Err(_) => error(StatusCode::INTERNAL_SERVER_ERROR, "preferences_unavailable"),
    }
}

fn invalid_preferences(detail: &str) -> Response {
    (
        StatusCode::UNPROCESSABLE_ENTITY,
        Json(json!({ "error": "invalid_preferences", "detail": detail })),
    )
        .into_response()
}

#[derive(Serialize)]
struct NamedRef {
    id: String,
    name: String,
}

#[derive(Serialize)]
struct TenantList {
    example: bool,
    tenants: Vec<NamedRef>,
}

/// Only the tenants the caller may see (membership rule in `scope.rs`).
pub async fn list_tenants(State(app): State<SharedState>, session: Session) -> Response {
    let Some(user) = auth::current_user(&session).await else {
        return unauthenticated();
    };
    let tenants = app
        .scope
        .visible_tenants(&user.groups)
        .into_iter()
        .map(|tenant| NamedRef {
            id: tenant.id.clone(),
            name: tenant.name.clone(),
        })
        .collect();
    Json(TenantList {
        example: app.scope.example,
        tenants,
    })
    .into_response()
}

#[derive(Serialize)]
struct TreeBody {
    tenant: NamedRef,
    orgs: Vec<Org>,
    example: bool,
}

/// A tenant the caller may not see answers exactly as an unknown one does, so the
/// route cannot be used to learn that a tenant exists.
pub async fn tenant_tree(
    State(app): State<SharedState>,
    session: Session,
    Path(tenant_id): Path<String>,
) -> Response {
    let Some(user) = auth::current_user(&session).await else {
        return unauthenticated();
    };
    let Some(tenant) = app.scope.visible_tenant(&tenant_id, &user.groups) else {
        return error(StatusCode::NOT_FOUND, "unknown_tenant");
    };
    Json(TreeBody {
        tenant: NamedRef {
            id: tenant.id.clone(),
            name: tenant.name.clone(),
        },
        orgs: tenant.orgs.clone(),
        example: app.scope.example,
    })
    .into_response()
}

/// Any other `/v1/*` path: 401 without a session, 404 with one.
pub async fn v1_unmatched(session: Session) -> Response {
    if auth::current_user(&session).await.is_none() {
        return unauthenticated();
    }
    error(StatusCode::NOT_FOUND, "not_found")
}
