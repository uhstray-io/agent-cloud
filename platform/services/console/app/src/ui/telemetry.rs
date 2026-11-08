//! The Telemetry page (plan 17, task 9.2, D43): target health and Grafana alerts by environment, Prometheus
//! cluster label and service, read from `GET /v1/telemetry/rollups`, for platform admins. No chart: each
//! service links to Grafana (D6).
//!
//! Scaffolding: the page shows its Phase 6 placeholder until the page lands.

use dioxus::prelude::*;

use super::empty::{EmptyPage, TELEMETRY};

#[component]
pub fn Telemetry() -> Element {
    rsx! { EmptyPage { page: TELEMETRY } }
}
