//! Status presentation for the inventory pages: the RAG word and icon, the source
//! lines, the stale banner, the workflow steps, and the rollup of several RAGs.
//!
//! The server decides every RAG and every source state. This file only names
//! them, and combines the RAGs it was given by the severity below. It reads no
//! metric and invents no status: a RAG the server did not name is grey.

use dioxus::prelude::*;

use super::icons::icon;
use super::inventory_api::{Meta, SourceStatus, Workflow};
use super::work::Tone;

/// The text for a value the API did not send: an em dash, never a zero.
pub const DASH: &str = "\u{2014}";

/// One of the four RAG colours the server sends. Each one also has an icon and a word.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Rag {
    Green,
    Amber,
    Red,
    Grey,
}

impl Rag {
    /// Reads the server's word. A word the console does not know is grey: no
    /// signal, which is what the server means by an unknown state.
    pub fn from_wire(text: &str) -> Rag {
        match text {
            "green" => Rag::Green,
            "amber" => Rag::Amber,
            "red" => Rag::Red,
            _ => Rag::Grey,
        }
    }

    /// The wire word, for the RAG filter's select values.
    pub fn wire(self) -> &'static str {
        match self {
            Rag::Green => "green",
            Rag::Amber => "amber",
            Rag::Red => "red",
            Rag::Grey => "grey",
        }
    }

    /// The Lucide icon, the tone and the status word. The shape and the word
    /// carry the state; the tone only repeats it. Words follow DESIGN.md's status
    /// vocabulary (Healthy, Degraded, Down, No signal).
    pub fn look(self) -> (&'static str, Tone, &'static str) {
        match self {
            Rag::Green => ("circle-check", Tone::Ok, "Healthy"),
            Rag::Amber => ("triangle-alert", Tone::Warn, "Degraded"),
            Rag::Red => ("octagon-x", Tone::Crit, "Down"),
            Rag::Grey => ("circle-dashed", Tone::Idle, "No signal"),
        }
    }

    /// The status word alone.
    pub fn label(self) -> &'static str {
        self.look().2
    }

    /// How much a rollup takes from this RAG. Red outranks amber, amber outranks
    /// grey, and grey outranks green: an unknown part is not a healthy one.
    fn severity(self) -> u8 {
        match self {
            Rag::Red => 3,
            Rag::Amber => 2,
            Rag::Grey => 1,
            Rag::Green => 0,
        }
    }
}

/// The most severe RAG in `rags`. An empty set is grey: nothing was measured.
pub fn worst(rags: impl IntoIterator<Item = Rag>) -> Rag {
    rags.into_iter()
        .max_by_key(|rag| rag.severity())
        .unwrap_or(Rag::Grey)
}

/// A RAG as icon, tone and word. Every place a RAG is drawn uses this.
#[component]
pub fn RagBadge(rag: Rag) -> Element {
    let (shape, tone, label) = rag.look();
    rsx! {
        span { class: "st {tone.class()}", {icon(shape)} span { "{label}" } }
    }
}

/// How a source's line reads. `has_read` is whether any read has finished, which
/// tells "configured, first read pending" apart from a live source.
pub fn source_look(status: SourceStatus, has_read: bool) -> (&'static str, Tone, &'static str) {
    match (status, has_read) {
        (SourceStatus::Ok, true) => ("circle-check", Tone::Ok, "Read ok"),
        (SourceStatus::Ok, false) => ("loader-circle", Tone::Run, "Waiting for first read"),
        (SourceStatus::Unconfigured, _) => ("circle-dashed", Tone::Idle, "Not configured"),
        (SourceStatus::Error, _) => ("octagon-x", Tone::Crit, "Read failed"),
    }
}

/// The settings an admin sets to switch an authority on. Variable names only,
/// never a value. `authority` is the name the envelope uses (`netbox`, `proxmox`,
/// `flows`, `grafana`).
pub fn unconfigured_hint(authority: &str) -> &'static str {
    match authority {
        "netbox" => "Set CONSOLE_NETBOX_URL and CONSOLE_NETBOX_TOKEN on the console.",
        "proxmox" => {
            "Set CONSOLE_PROXMOX_URL, CONSOLE_PROXMOX_TOKEN_ID and CONSOLE_PROXMOX_TOKEN on the console."
        }
        "flows" => "Set CONSOLE_FLOWS_FILE on the console to load the data-flow catalogue.",
        _ => "Set CONSOLE_GRAFANA_URL on the console to link out to Grafana.",
    }
}

/// The stale banner. `None` when the answer is current. Otherwise it names each
/// failed authority's fixed error text; it never shows a URL or a body.
pub fn stale_line(meta: &Meta) -> Option<String> {
    if !meta.stale {
        return None;
    }
    let mut reasons = Vec::new();
    if let Some(text) = &meta.error.netbox {
        reasons.push(format!("NetBox: {text}"));
    }
    if let Some(text) = &meta.error.proxmox {
        reasons.push(format!("Proxmox: {text}"));
    }
    let why = if reasons.is_empty() {
        "a read failed".to_string()
    } else {
        reasons.join("; ")
    };
    Some(format!("Stale \u{2014} showing the last good read. {why}"))
}

/// The status of one step. `pass`, `fail` and `skip` are named; any other value
/// is pending or unknown, which the RAG rule counts as amber.
pub fn step_look(status: &str) -> (&'static str, Tone, &'static str) {
    match status {
        "pass" => ("circle-check", Tone::Ok, "Pass"),
        "fail" => ("octagon-x", Tone::Crit, "Fail"),
        "skip" => ("circle-dashed", Tone::Idle, "Skipped"),
        _ => ("triangle-alert", Tone::Warn, "Pending or unknown"),
    }
}

/// One row of a workflow's step list.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StepRow {
    pub id: String,
    pub status: String,
    pub shape: &'static str,
    pub tone: Tone,
    pub label: &'static str,
}

/// The steps in step-id order, as the server maps them.
pub fn step_rows(workflow: &Workflow) -> Vec<StepRow> {
    workflow
        .steps
        .iter()
        .map(|(id, status)| {
            let (shape, tone, label) = step_look(status);
            StepRow {
                id: id.clone(),
                status: status.clone(),
                shape,
                tone,
                label,
            }
        })
        .collect()
}

/// A workflow's steps as a vertical stepper (`.vsteps`). Each step has its icon
/// and its word.
#[component]
pub fn StepList(steps: Vec<StepRow>) -> Element {
    rsx! {
        ol { class: "vsteps",
            for step in steps {
                li { class: "vstep {step.tone.class()}", key: "{step.id}",
                    span { class: "vnum", aria_hidden: "true", {icon(step.shape)} }
                    span { class: "grow",
                        b { class: "mono", "{step.id}" }
                        span { "{step.label} " span { class: "mono", "({step.status})" } }
                    }
                }
            }
        }
    }
}

/// A usage level: a bar whose width is the value, labelled with its percent.
/// It is a level indicator only. No history, no axis.
#[component]
pub fn Meter(label: String, pct: f64) -> Element {
    let width = meter_width(pct);
    let text = meter_text(pct);
    rsx! {
        div { class: "meter", role: "img", aria_label: "{label} {text}",
            span { class: "meter-track", aria_hidden: "true",
                span { class: "meter-fill", style: "width: {width}" }
            }
            span { class: "num", "{text}" }
        }
    }
}

/// The bar width for a percent, clamped to 0 to 100.
pub fn meter_width(pct: f64) -> String {
    format!("{:.1}%", pct.clamp(0.0, 100.0))
}

/// The percent label, rounded and clamped, such as `12%`.
pub fn meter_text(pct: f64) -> String {
    format!("{:.0}%", pct.clamp(0.0, 100.0))
}

/// Shown in place of a body that is not there yet, or not allowed. One sentence,
/// in the page's empty-state card.
#[component]
pub fn Notice(message: String) -> Element {
    rsx! {
        section { class: "card",
            div { class: "empty",
                span { class: "empty-icon", aria_hidden: "true", {icon("info")} }
                p { role: "status", "{message}" }
            }
        }
    }
}

/// The banners every inventory page shows above its body: the stale line, the
/// reason the newest refresh failed, and one line per source. An empty `reason`
/// means the newest refresh worked.
#[component]
pub fn Freshness(meta: Meta, reason: String) -> Element {
    let stale = stale_line(&meta);
    let dropped = dropped_line(&meta);
    rsx! {
        if let Some(line) = stale {
            p { class: "stale", role: "status", "{line}" }
        }
        if !reason.is_empty() {
            p { class: "stale", role: "status", "The last refresh failed: {reason}." }
        }
        if let Some(line) = dropped {
            p { class: "stale", role: "status", "{line}" }
        }
        SourceStrip { meta }
    }
}

/// The warning for rows NetBox returned for another scope and the console dropped.
/// Admins only: `None` when the envelope has no diagnostics, or the count is zero. The
/// answer is still served, so this is a warning and not an error.
pub fn dropped_line(meta: &Meta) -> Option<String> {
    let count = meta.diagnostics.as_ref()?.dropped_mismatched;
    if count == 0 {
        return None;
    }
    let noun = if count == 1 { "row" } else { "rows" };
    Some(format!(
        "NetBox returned {count} {noun} the console did not ask for. They were dropped, and the answer is still served."
    ))
}

/// The empty state for an authority the console is not configured to read: its
/// name and the variables an admin sets. NetBox is named before Proxmox, the order
/// the pages depend on them. `None` when both are configured.
pub fn unconfigured_notice(meta: &Meta) -> Option<String> {
    if meta.source.netbox == SourceStatus::Unconfigured {
        return Some(format!(
            "NetBox is not configured. {}",
            unconfigured_hint("netbox")
        ));
    }
    if meta.source.proxmox == SourceStatus::Unconfigured {
        return Some(format!(
            "Proxmox is not configured. {}",
            unconfigured_hint("proxmox")
        ));
    }
    None
}

/// One line per authority: its state as icon and word, when it last read, and the
/// variable names an admin sets when it is not configured.
#[component]
fn SourceStrip(meta: Meta) -> Element {
    rsx! {
        ul { class: "sources", aria_label: "Sources",
            SourceLine {
                authority: "NetBox",
                key_name: "netbox",
                status: meta.source.netbox,
                updated: meta.updated_at.netbox.unwrap_or_default(),
                error: meta.error.netbox.unwrap_or_default(),
            }
            SourceLine {
                authority: "Proxmox",
                key_name: "proxmox",
                status: meta.source.proxmox,
                updated: meta.updated_at.proxmox.unwrap_or_default(),
                error: meta.error.proxmox.unwrap_or_default(),
            }
        }
    }
}

#[component]
fn SourceLine(
    authority: &'static str,
    key_name: &'static str,
    status: SourceStatus,
    /// Empty when no read has finished.
    updated: String,
    /// The authority's fixed error text, shown whenever the API sends one. Empty when none.
    error: String,
) -> Element {
    let (shape, tone, label) = source_look(status, !updated.is_empty());
    let spin = if tone == Tone::Run { "spin" } else { "" };
    let stamp = if updated.is_empty() {
        String::new()
    } else {
        format!(" \u{00b7} updated {updated}")
    };
    let hint = if status == SourceStatus::Unconfigured {
        unconfigured_hint(key_name)
    } else {
        ""
    };
    rsx! {
        li { class: "source",
            span { class: "st {tone.class()}",
                span { class: "{spin}", {icon(shape)} }
                "{authority}: {label}"
            }
            if !stamp.is_empty() {
                span { class: "faint", "{stamp}" }
            }
            if !error.is_empty() {
                span { class: "hint", "{authority} reports: {error}" }
            }
            if !hint.is_empty() {
                span { class: "hint", "{hint}" }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn server_words_map_to_rags_and_anything_else_is_grey() {
        assert_eq!(Rag::from_wire("green"), Rag::Green);
        assert_eq!(Rag::from_wire("amber"), Rag::Amber);
        assert_eq!(Rag::from_wire("red"), Rag::Red);
        assert_eq!(Rag::from_wire("grey"), Rag::Grey);
        assert_eq!(Rag::from_wire("purple"), Rag::Grey, "unknown is no signal");
        assert_eq!(Rag::from_wire(""), Rag::Grey);
        for rag in [Rag::Green, Rag::Amber, Rag::Red, Rag::Grey] {
            assert_eq!(Rag::from_wire(rag.wire()), rag, "wire round-trips");
        }
    }

    #[test]
    fn every_rag_has_an_icon_a_tone_and_a_word() {
        // The status vocabulary from DESIGN.md: icon and word are fixed per colour.
        assert_eq!(Rag::Green.look(), ("circle-check", Tone::Ok, "Healthy"));
        assert_eq!(
            Rag::Amber.look(),
            ("triangle-alert", Tone::Warn, "Degraded")
        );
        assert_eq!(Rag::Red.look(), ("octagon-x", Tone::Crit, "Down"));
        assert_eq!(Rag::Grey.look(), ("circle-dashed", Tone::Idle, "No signal"));
        assert_eq!(Rag::Red.label(), "Down");
        for rag in [Rag::Green, Rag::Amber, Rag::Red, Rag::Grey] {
            assert!(!rag.label().is_empty(), "a status is never icon alone");
        }
    }

    #[test]
    fn a_rollup_takes_the_most_severe_rag() {
        assert_eq!(worst([Rag::Green, Rag::Green]), Rag::Green);
        assert_eq!(
            worst([Rag::Green, Rag::Grey]),
            Rag::Grey,
            "unknown outranks healthy"
        );
        assert_eq!(worst([Rag::Grey, Rag::Amber]), Rag::Amber);
        assert_eq!(worst([Rag::Amber, Rag::Red, Rag::Green]), Rag::Red);
        assert_eq!(
            worst(Vec::new()),
            Rag::Grey,
            "nothing measured is no signal"
        );
    }

    #[test]
    fn source_lines_separate_pending_from_live_and_failed() {
        assert_eq!(
            source_look(SourceStatus::Ok, true),
            ("circle-check", Tone::Ok, "Read ok")
        );
        assert_eq!(
            source_look(SourceStatus::Ok, false).2,
            "Waiting for first read"
        );
        assert_eq!(
            source_look(SourceStatus::Unconfigured, false).2,
            "Not configured"
        );
        assert_eq!(source_look(SourceStatus::Error, true).2, "Read failed");
    }

    #[test]
    fn unconfigured_hints_name_the_variables_and_no_values() {
        assert!(unconfigured_hint("netbox").contains("CONSOLE_NETBOX_TOKEN"));
        assert!(unconfigured_hint("proxmox").contains("CONSOLE_PROXMOX_TOKEN_ID"));
        assert!(unconfigured_hint("flows").contains("CONSOLE_FLOWS_FILE"));
        assert!(unconfigured_hint("grafana").contains("CONSOLE_GRAFANA_URL"));
    }

    #[test]
    fn an_unconfigured_source_gets_an_empty_state_naming_its_variables() {
        let mut meta = Meta::default();
        meta.source.netbox = SourceStatus::Ok;
        meta.source.proxmox = SourceStatus::Ok;
        assert_eq!(
            unconfigured_notice(&meta),
            None,
            "both configured: no notice"
        );
        meta.source.netbox = SourceStatus::Unconfigured;
        let text = unconfigured_notice(&meta).expect("netbox notice");
        assert!(text.starts_with("NetBox is not configured."));
        assert!(text.contains("CONSOLE_NETBOX_URL"));
        meta.source.proxmox = SourceStatus::Unconfigured;
        assert!(
            unconfigured_notice(&meta).unwrap().starts_with("NetBox"),
            "netbox first"
        );
        meta.source.netbox = SourceStatus::Ok;
        assert!(unconfigured_notice(&meta)
            .unwrap()
            .contains("CONSOLE_PROXMOX_TOKEN"));
    }

    #[test]
    fn dropped_rows_warn_admins_and_stay_silent_otherwise() {
        use crate::ui::inventory_api::Diagnostics;
        let mut meta = Meta::default();
        assert_eq!(
            dropped_line(&meta),
            None,
            "a member's envelope has no diagnostics"
        );
        meta.diagnostics = Some(Diagnostics {
            dropped_mismatched: 0,
        });
        assert_eq!(dropped_line(&meta), None, "a zero count is silent");
        meta.diagnostics = Some(Diagnostics {
            dropped_mismatched: 1,
        });
        assert!(dropped_line(&meta)
            .unwrap()
            .starts_with("NetBox returned 1 row "));
        meta.diagnostics = Some(Diagnostics {
            dropped_mismatched: 3,
        });
        assert!(dropped_line(&meta)
            .unwrap()
            .starts_with("NetBox returned 3 rows "));
    }

    #[test]
    fn stale_line_names_each_failed_source_and_is_absent_when_current() {
        let mut meta = Meta::default();
        assert_eq!(stale_line(&meta), None);
        meta.stale = true;
        meta.error.netbox = Some("NetBox answered HTTP 500".into());
        meta.error.proxmox = Some("Proxmox read timed out".into());
        assert_eq!(
            stale_line(&meta).as_deref(),
            Some("Stale \u{2014} showing the last good read. NetBox: NetBox answered HTTP 500; Proxmox: Proxmox read timed out")
        );
        meta.error = Default::default();
        assert_eq!(
            stale_line(&meta).as_deref(),
            Some("Stale \u{2014} showing the last good read. a read failed")
        );
    }

    #[test]
    fn step_statuses_map_and_unknown_is_pending() {
        assert_eq!(step_look("pass"), ("circle-check", Tone::Ok, "Pass"));
        assert_eq!(step_look("fail"), ("octagon-x", Tone::Crit, "Fail"));
        assert_eq!(step_look("skip"), ("circle-dashed", Tone::Idle, "Skipped"));
        assert_eq!(step_look("running").2, "Pending or unknown");
        assert_eq!(step_look("unknown").2, "Pending or unknown");
    }

    #[test]
    fn step_rows_follow_the_step_ids_in_order() {
        let mut workflow = Workflow::default();
        workflow.steps.insert("b-apply".into(), "fail".into());
        workflow.steps.insert("a-plan".into(), "pass".into());
        let rows = step_rows(&workflow);
        let ids: Vec<&str> = rows.iter().map(|r| r.id.as_str()).collect();
        assert_eq!(ids, ["a-plan", "b-apply"]);
        assert_eq!(rows[1].label, "Fail");
    }

    #[test]
    fn meter_text_and_width_stay_inside_zero_and_hundred() {
        assert_eq!(meter_text(12.4), "12%");
        assert_eq!(meter_text(150.0), "100%");
        assert_eq!(meter_text(-3.0), "0%");
        assert_eq!(meter_width(250.0), "100.0%");
        assert_eq!(meter_width(33.25), "33.2%");
    }
}
