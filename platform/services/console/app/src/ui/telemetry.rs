//! The Telemetry page (plan 17, task 9.2, D43): target health and Grafana alerts by environment, Prometheus
//! cluster label and service, for platform admins. Each service links out to Grafana. No chart is drawn (D6).
//! Every status shows an icon and a word (task 6.5).

use dioxus::prelude::*;

use super::api::{self, Me};
use super::empty::TELEMETRY;
use super::icons::icon;
use super::inventory::{admin_next, end_session, keeps_polling_admin, Admin, POLL_MS};
use super::inventory_api;
use super::shell::Ctx;
use super::status::{Notice, DASH};
use super::telemetry_api::{
    alert_text, alerts_notice, health, not_ok_message, read_label, targets_text, Cluster, Counts,
    Environment, TelemetryBody, TELEMETRY_PATH,
};

/// The header line under the title. The read's age follows it once a read has finished.
const LEDE: &str = "Target health and Grafana alerts by environment, Prometheus cluster label and service. Charts stay in Grafana.";

/// The page's one poll, as `poll_admin` runs it. It also keeps the client clock each answer arrived at, so
/// the read's age is read against that clock (as the Policy page does).
async fn poll(
    mut state: Signal<Admin<TelemetryBody>>,
    mut now: Signal<Option<i64>>,
    me: Signal<Me>,
) {
    loop {
        api::wait_visible().await;
        let read = inventory_api::read::<TelemetryBody>(TELEMETRY_PATH).await;
        now.set(api::now_ms().await);
        let next = admin_next(&state.peek(), read);
        let keep = keeps_polling_admin(&next);
        let signed_out = matches!(next, Admin::SignedOut);
        state.set(next);
        if signed_out {
            end_session(me);
        }
        if !keep {
            return;
        }
        api::sleep_unless_hidden(POLL_MS).await;
    }
}

/// The Telemetry page. The header is the same in every case; the body depends on the answer.
#[component]
pub fn Telemetry() -> Element {
    let ctx = use_context::<Ctx>();
    let state = use_signal(|| Admin::<TelemetryBody>::Loading);
    let now = use_signal(|| None::<i64>);
    use_hook(move || {
        spawn(poll(state, now, ctx.me));
    });

    let shown = state.read().clone();
    let now_ms = *now.read();
    let read = match &shown {
        Admin::Shown { body, .. } => body
            .refreshed_at
            .as_deref()
            .map(|stamp| read_label(now_ms, stamp)),
        _ => None,
    };
    rsx! {
        div { class: "view telemetry",
            header { class: "view-h",
                h1 { {icon(TELEMETRY.icon)} "{TELEMETRY.title}" }
                span { class: "authority", "Source of truth: {TELEMETRY.authority}" }
                p { "{LEDE}" }
                if let Some(read) = read {
                    p { "{read}." }
                }
            }
            TelemetryView { state: shown }
        }
    }
}

/// What the page shows for each state of the poll.
#[component]
fn TelemetryView(state: Admin<TelemetryBody>) -> Element {
    match state {
        Admin::Loading => rsx! { Notice { message: "Loading telemetry\u{2026}".to_string() } },
        Admin::Forbidden => rsx! { Notice { message: "Platform admins only.".to_string() } },
        Admin::Missing => rsx! {
            Notice { message: "The telemetry answer was not found.".to_string() }
        },
        Admin::SignedOut => rsx! {
            Notice { message: "Your session ended. Sign in again to see telemetry.".to_string() }
        },
        Admin::Unavailable(reason) => rsx! {
            Notice { message: format!(
                "Telemetry could not be loaded: {reason}. It retries every {} seconds while this tab is visible.",
                POLL_MS / 1000
            ) }
        },
        Admin::Shown { body, reason } => rsx! {
            TelemetryBodyView { body, reason: reason.unwrap_or_default() }
        },
    }
}

/// The body of an answer the console read. Any state but `ok` is one sentence. An `ok` answer is the rows,
/// under the banners that say what is stale, what the alert read lacks, and what the server could not read.
#[component]
fn TelemetryBodyView(body: TelemetryBody, reason: String) -> Element {
    if let Some(message) = not_ok_message(body.state, body.error.as_deref()) {
        return rsx! { Notice { message } };
    }
    let counts_shown = body.alerts.counts_shown();
    let alerts = alerts_notice(&body.alerts);
    rsx! {
        if !reason.is_empty() {
            p { class: "stale", role: "status", "The last read failed: {reason}." }
        }
        if let Some(message) = alerts {
            Notice { message }
        }
        if let Some(message) = body.error.clone() {
            Notice { message }
        }
        if body.environments.is_empty() {
            Notice { message: "Prometheus reported no targets.".to_string() }
        }
        for environment in body.environments.iter() {
            EnvironmentCard {
                key: "{environment.name}",
                environment: environment.clone(),
                counts_shown,
            }
        }
    }
}

/// One environment: its name and rollup in the card header, then one table per cluster.
#[component]
fn EnvironmentCard(environment: Environment, counts_shown: bool) -> Element {
    let empty = environment.clusters.is_empty();
    rsx! {
        section { class: "card",
            div { class: "card-h",
                h2 { "{environment.name}" }
                Rollup { counts: environment.counts, counts_shown }
            }
            for cluster in environment.clusters.iter() {
                ClusterTable {
                    key: "{cluster.name}",
                    cluster: cluster.clone(),
                    counts_shown,
                }
            }
            if empty {
                p { class: "empty", "Prometheus reported no clusters for this environment." }
            }
        }
    }
}

/// One cluster: its name and rollup, then a row per service with its health, counts and Grafana link.
#[component]
fn ClusterTable(cluster: Cluster, counts_shown: bool) -> Element {
    let empty = cluster.services.is_empty();
    rsx! {
        div { class: "cluster",
            div { class: "card-h",
                h3 { "{cluster.name}" }
                Rollup { counts: cluster.counts, counts_shown }
            }
            div { class: "tbl-wrap",
                table { class: "tbl",
                    thead {
                        tr {
                            th { "Service" }
                            th { "Health" }
                            th { "Targets" }
                            th { "Firing" }
                            th { "Pending" }
                            th { "Grafana" }
                        }
                    }
                    tbody {
                        for service in cluster.services.iter() {
                            tr { key: "{service.name}",
                                td { class: "mono", "{service.name}" }
                                td { HealthBadge { counts: service.counts } }
                                td { class: "num", "{targets_text(service.counts.targets_up, service.counts.targets_total)}" }
                                td { class: "num", "{alert_text(service.counts.firing, counts_shown)}" }
                                td { class: "num", "{alert_text(service.counts.pending, counts_shown)}" }
                                td {
                                    if let Some(url) = service.grafana_url.clone() {
                                        a {
                                            href: "{url}",
                                            target: "_blank",
                                            rel: "noopener noreferrer",
                                            "Open in Grafana"
                                        }
                                    } else {
                                        span { class: "faint", {DASH} }
                                    }
                                }
                            }
                        }
                    }
                }
            }
            if empty {
                p { class: "empty", "Prometheus reported no services for this cluster." }
            }
        }
    }
}

/// A row's health as icon and word. The same mark in a card header and in a service row.
#[component]
fn HealthBadge(counts: Counts) -> Element {
    let (shape, tone, label) = health(counts.targets_up, counts.targets_total);
    rsx! {
        span { class: "st {tone.class()}", {icon(shape)} span { "{label}" } }
    }
}

/// A rollup as the card header shows it: health, the target count, and the firing and pending counts. An alert
/// count shows a dash, not zero, while the alert read has not finished.
#[component]
fn Rollup(counts: Counts, counts_shown: bool) -> Element {
    rsx! {
        HealthBadge { counts }
        span { class: "faint", "{targets_text(counts.targets_up, counts.targets_total)}" }
        span { class: "num", "Firing {alert_text(counts.firing, counts_shown)}" }
        span { class: "num", "Pending {alert_text(counts.pending, counts_shown)}" }
    }
}
