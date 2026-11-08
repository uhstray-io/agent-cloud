//! The Telemetry page's answer (plan 17, task 9.2, D43): target health and Grafana alerts rolled up by environment,
//! Prometheus cluster label and service, read through Grafana by a background task into `TelemetryCache`.
//! `GET /v1/telemetry/rollups` only reads the cache.
//!
//! Scaffolding: until the read lands, the answer is `unconfigured` without the Grafana URL and token, and
//! `loading` with them.

use dioxus::server::axum::extract::State;
use dioxus::server::axum::response::{IntoResponse, Response};
use dioxus::server::axum::Json;
use serde_json::{json, Value};
use tower_sessions::Session;

use super::work;
use super::SharedState;

/// What the page's route reads.
pub struct TelemetryCache {
    configured: bool,
}

impl TelemetryCache {
    /// `configured` is whether the sources this page reads are all set.
    pub fn new(configured: bool) -> Self {
        Self { configured }
    }

    /// The answer the route serves.
    pub fn body(&self) -> Value {
        let state = if self.configured {
            "loading"
        } else {
            "unconfigured"
        };
        json!({ "state": state })
    }
}

/// The page's route (platform admins only, D25). Reads the cache, never an upstream.
pub async fn get_telemetry(State(app): State<SharedState>, session: Session) -> Response {
    if let Err(response) = work::admin_only(&session).await {
        return response;
    }
    let cache = app.telemetry.read().await;
    Json(cache.body()).into_response()
}

/// Starts the background read. Scaffolding: no read runs yet.
pub fn spawn_refresh(_state: SharedState) {}
