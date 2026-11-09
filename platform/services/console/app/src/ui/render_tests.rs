//! Render tests for the status components (plan 17, D47, task 10.3a).
//!
//! Each state is rendered on its own with `dioxus::ssr::render_element`, and the
//! output must carry the state's icon (`<svg`) and its word. Rendering one state per
//! call keeps a neighbouring row's icon from satisfying the check.
//!
//! Reached through their public components: `RagBadge`, `Freshness` (which draws the
//! source lines), `StepList`, `Rail` and `Meter`. Four renderers are private to their
//! modules and cannot be reached from here without a visibility change: `cicd::status_badge`,
//! `telemetry::HealthBadge`, `work_detail::Stepper` and `roles::LaneCell`. Their states are
//! covered by `every_status_icon_draws_a_glyph`, which renders the icon each look
//! function names. That proves the glyph, not the component's markup.

use dioxus::prelude::*;

use super::cicd_api::{RequiredState, ReviewState};
use super::icons::icon;
use super::inventory_api::{Meta, SourceStatus, Workflow};
use super::status::{source_look, step_look, step_rows, Freshness, Meter, Rag, RagBadge, StepList};
use super::telemetry_api::health;
use super::work::Lane;
use super::work_detail::Rail;

fn render(element: Element) -> String {
    dioxus::ssr::render_element(element)
}

/// The icon and the word, both present in one state's output.
#[track_caller]
fn assert_icon_and_word(html: &str, word: &str) {
    assert!(html.contains("<svg"), "no icon drawn for {word:?}: {html}");
    assert!(html.contains(word), "no word {word:?} in: {html}");
}

/// The list item holding `text`, from its `<li` to its `</li>`.
#[track_caller]
fn list_item_with<'a>(html: &'a str, text: &str) -> &'a str {
    let at = html
        .find(text)
        .unwrap_or_else(|| panic!("no {text:?} in: {html}"));
    let start = html[..at].rfind("<li").expect("list item start");
    let end = at + html[at..].find("</li>").expect("list item end") + "</li>".len();
    &html[start..end]
}

#[test]
fn every_rag_badge_draws_its_icon_and_its_word() {
    let cases = [
        (Rag::Green, "Healthy"),
        (Rag::Amber, "Degraded"),
        (Rag::Red, "Down"),
        (Rag::Grey, "No signal"),
    ];
    for (rag, word) in cases {
        let html = render(rsx! { RagBadge { rag } });
        assert_icon_and_word(&html, word);
    }
}

#[test]
fn every_source_line_draws_its_icon_and_its_word() {
    // (state, has a read finished, the word the line must carry)
    let cases = [
        (SourceStatus::Ok, true, "Read ok"),
        (SourceStatus::Ok, false, "Waiting for first read"),
        (SourceStatus::Unconfigured, false, "Not configured"),
        (SourceStatus::Unconfigured, true, "Not configured"),
        (SourceStatus::Error, true, "Read failed"),
        (SourceStatus::Error, false, "Read failed"),
        (SourceStatus::Unknown, true, "Unknown source state"),
    ];
    for (status, has_read, word) in cases {
        let mut meta = Meta::default();
        meta.source.netbox = status;
        if has_read {
            meta.updated_at.netbox = Some("2026-11-08T10:00:00Z".into());
        }
        let html = render(rsx! { Freshness { meta, reason: String::new() } });
        let line = list_item_with(&html, "NetBox: ");
        assert_icon_and_word(line, word);
        assert!(
            line.contains(&format!("NetBox: {word}")),
            "the word is the line's own: {line}"
        );
    }
}

#[test]
fn every_step_draws_its_icon_and_its_word() {
    let cases = [
        ("pass", "Pass"),
        ("fail", "Fail"),
        ("skip", "Skipped"),
        ("running", "Pending or unknown"),
    ];
    for (status, word) in cases {
        let mut workflow = Workflow::default();
        workflow.steps.insert("step-a".into(), status.into());
        let html = render(rsx! { StepList { steps: step_rows(&workflow) } });
        assert_icon_and_word(&html, word);
    }
}

#[test]
fn every_lane_in_the_minimized_rail_draws_its_icon_and_names_its_word() {
    /// One lane in the rail, with the signal the rail needs.
    #[component]
    fn RailOf(lane: Lane) -> Element {
        let minimized = use_signal(|| true);
        let focus_rail = use_signal(|| false);
        rsx! { Rail { counts: vec![(lane, 3)], can_open: true, minimized, focus_rail } }
    }

    let cases = [
        (Lane::Queued, "Queued"),
        (Lane::Running, "Running"),
        (Lane::Awaiting, "Awaiting approval"),
        (Lane::Blocked, "Blocked"),
        (Lane::Failed, "Failed"),
        (Lane::Done, "Done"),
    ];
    for (lane, word) in cases {
        let html = render(rsx! { RailOf { lane } });
        assert_icon_and_word(&html, word);
        // The word is both the item's accessible name and visible text beside the count (D44).
        assert!(
            html.contains(&format!("aria-label=\"{word}: 3\"")),
            "the rail item is not named by its word: {html}"
        );
        assert!(
            html.contains(&format!("<span>{word}</span>")),
            "the rail item does not show its word: {html}"
        );
    }
}

#[test]
fn meter_names_its_label_and_percent_in_its_accessible_name() {
    // A meter draws no icon and has no status word: its states are levels. The
    // check is the image role and the label, clamped at both ends.
    let cases = [
        (42.4, "CPU 42%"),
        (0.0, "CPU 0%"),
        (100.0, "CPU 100%"),
        (150.0, "CPU 100%"),
        (-3.0, "CPU 0%"),
    ];
    for (pct, name) in cases {
        let html = render(rsx! { Meter { label: "CPU".to_string(), pct } });
        assert!(html.contains("role=\"img\""), "no image role: {html}");
        assert!(
            html.contains(&format!("aria-label=\"{name}\"")),
            "accessible name {name:?} missing: {html}"
        );
    }
}

/// Every icon name a status returns draws a glyph, and every status carries a
/// word. The private renderers take their icon from these same names, so a name
/// that draws nothing (a typo, or a shape missing from `icons.rs`) fails here.
#[test]
fn every_status_icon_draws_a_glyph() {
    let mut looks: Vec<(&str, &str)> = Vec::new();
    for rag in [Rag::Green, Rag::Amber, Rag::Red, Rag::Grey] {
        let (shape, _, word) = rag.look();
        looks.push((shape, word));
    }
    for status in [
        SourceStatus::Ok,
        SourceStatus::Unconfigured,
        SourceStatus::Error,
        SourceStatus::Unknown,
    ] {
        for has_read in [true, false] {
            let (shape, _, word) = source_look(status, has_read);
            looks.push((shape, word));
        }
    }
    for status in ["pass", "fail", "skip", "running"] {
        let (shape, _, word) = step_look(status);
        looks.push((shape, word));
    }
    for review in [
        ReviewState::Approved,
        ReviewState::ChangesRequested,
        ReviewState::NoReview,
        ReviewState::Unknown,
    ] {
        let (shape, _, word) = review.look();
        looks.push((shape, word));
    }
    for required in [
        RequiredState::Passed,
        RequiredState::Failed,
        RequiredState::Pending,
        RequiredState::Skipped,
        RequiredState::Missing,
        RequiredState::Unknown,
    ] {
        let (shape, _, word) = required.look();
        looks.push((shape, word));
    }
    for (up, total) in [(0, 0), (11, 11), (0, 11), (4, 11)] {
        let (shape, _, word) = health(up, total);
        looks.push((shape, word));
    }
    for lane in Lane::ALL {
        let (shape, _) = lane.look();
        looks.push((shape, lane.label()));
    }

    for (shape, word) in looks {
        let html = render(icon(shape));
        assert!(html.contains("<svg"), "icon {shape:?} draws no glyph");
        assert!(!word.is_empty(), "icon {shape:?} has no word beside it");
    }

    // The check can fail: an unknown name draws nothing, as icons.rs documents.
    let empty = render(icon("no-such-icon"));
    assert!(!empty.contains("<svg"), "an unknown name must draw nothing");
}
