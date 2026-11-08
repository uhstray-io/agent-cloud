//! The CI/CD page's answer as the browser reads it: `GET /v1/delivery`, admin-only (plan 17, 9.1, D42).
//! The shapes are the ones the delivery answer serializes. Every struct carries `#[serde(default)]`, so a
//! missing key reads as empty, and an `Option` field reads `null` as `None`. Nothing in this file writes to
//! the API.

use serde::Deserialize;

use super::work::{age_text, Tone};

/// The admin-only delivery answer.
pub const DELIVERY_PATH: &str = "/v1/delivery";

/// The state the server names. Any other word is `Unknown`, which the page shows as a failed answer
/// rather than as an empty list.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum DeliveryState {
    Ok,
    Unconfigured,
    /// No read has finished yet. Also the value when the envelope omits the field.
    #[default]
    Loading,
    Error,
    #[serde(other)]
    Unknown,
}

/// `GET /v1/delivery`.
#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct DeliveryBody {
    pub state: DeliveryState,
    /// The repository the answer was read from. `None` until a read has finished.
    pub repo: Option<String>,
    /// The ref the pull requests into it are read against. `None` until a read has finished.
    #[serde(rename = "ref")]
    pub git_ref: Option<String>,
    /// The check names the ruleset on `main` requires.
    pub required_checks: Vec<String>,
    /// The ruleset file on GitHub, when the answer names one.
    pub ruleset_url: Option<String>,
    /// The comparison of `dev` against `main`, when one was read.
    pub promotion: Option<Promotion>,
    pub pulls: Vec<Pull>,
    /// When the newest good read finished. Older than the failed read, when `error` names one.
    pub refreshed_at: Option<String>,
    /// Why the newest read failed, as one sentence. Never a credential.
    pub error: Option<String>,
}

/// The comparison of `head` against `base`, and the open pull request that carries it into `base`, if any.
#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct Promotion {
    pub base: String,
    pub head: String,
    pub ahead_by: u64,
    pub behind_by: u64,
    pub compare_url: String,
    pub pull: Option<Pull>,
}

/// One open pull request. `required` is empty for a pull that does not target `main`.
#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct Pull {
    pub number: u64,
    pub title: String,
    pub author: String,
    pub base: String,
    pub head: String,
    pub draft: bool,
    pub url: String,
    pub updated_at: String,
    pub review: ReviewState,
    pub checks: CheckCounts,
    pub required: Vec<RequiredCheck>,
}

/// The review decision the server names for a pull. Any other word is `Unknown`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ReviewState {
    Approved,
    ChangesRequested,
    /// The wire word is `none`. The variant is not named `None`, which reads as `Option::None` in a match.
    #[serde(rename = "none")]
    NoReview,
    #[default]
    #[serde(other)]
    Unknown,
}

impl ReviewState {
    /// The status word.
    pub fn label(self) -> &'static str {
        match self {
            ReviewState::Approved => "Approved",
            ReviewState::ChangesRequested => "Changes requested",
            ReviewState::NoReview => "No review",
            ReviewState::Unknown => "Unknown review state",
        }
    }

    /// The Lucide icon, the tone and the status word. The shape and the word carry the state.
    pub fn look(self) -> (&'static str, Tone, &'static str) {
        let (shape, tone) = match self {
            ReviewState::Approved => ("circle-check", Tone::Ok),
            ReviewState::ChangesRequested => ("triangle-alert", Tone::Warn),
            ReviewState::NoReview | ReviewState::Unknown => ("circle-dashed", Tone::Idle),
        };
        (shape, tone, self.label())
    }
}

/// One check the ruleset on `main` requires, and where it stands on this pull.
#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct RequiredCheck {
    pub name: String,
    pub state: RequiredState,
}

/// The state of one required check on one pull. Any other word is `Unknown`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum RequiredState {
    Passed,
    Failed,
    Pending,
    Skipped,
    /// The check has not reported on this pull.
    Missing,
    #[default]
    #[serde(other)]
    Unknown,
}

impl RequiredState {
    /// The status word.
    pub fn label(self) -> &'static str {
        match self {
            RequiredState::Passed => "Passed",
            RequiredState::Failed => "Failed",
            RequiredState::Pending => "Pending",
            RequiredState::Skipped => "Skipped",
            RequiredState::Missing => "Missing",
            RequiredState::Unknown => "Unknown check state",
        }
    }

    /// The Lucide icon, the tone and the status word. The shape and the word carry the state.
    pub fn look(self) -> (&'static str, Tone, &'static str) {
        let (shape, tone) = match self {
            RequiredState::Passed => ("circle-check", Tone::Ok),
            RequiredState::Failed => ("octagon-x", Tone::Crit),
            RequiredState::Pending => ("loader-circle", Tone::Run),
            RequiredState::Missing => ("triangle-alert", Tone::Warn),
            RequiredState::Skipped | RequiredState::Unknown => ("circle-dashed", Tone::Idle),
        };
        (shape, tone, self.label())
    }
}

/// How many checks on a pull are in each state.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Deserialize)]
#[serde(default)]
pub struct CheckCounts {
    pub passed: u64,
    pub failed: u64,
    pub pending: u64,
    pub skipped: u64,
}

/// The sentence an answer with no data shows. `None` when the state has data to show.
pub fn not_ok_message(state: DeliveryState, error: Option<&str>) -> Option<String> {
    match state {
        DeliveryState::Ok => None,
        DeliveryState::Unconfigured => {
            Some("The GitHub source is not configured for this console.".to_string())
        }
        DeliveryState::Loading => {
            Some("The first read of delivery state has not finished.".to_string())
        }
        DeliveryState::Error => Some(format!(
            "Delivery state could not be read: {}. The next read tries again.",
            error.unwrap_or("no reason was given")
        )),
        DeliveryState::Unknown => {
            Some("The console received a delivery state it does not know.".to_string())
        }
    }
}

/// The header line once a read has named its ref. The page shows the Phase 6 purpose line until then.
pub fn subtitle(git_ref: &str) -> String {
    format!(
        "Open pull requests into {git_ref} and main, their checks and reviews, and the promotion path."
    )
}

/// The Checks cell: `14 passed · 0 failed · 0 pending · 3 skipped`, in that order.
pub fn checks_summary(counts: &CheckCounts) -> String {
    format!(
        "{} passed \u{b7} {} failed \u{b7} {} pending \u{b7} {} skipped",
        counts.passed, counts.failed, counts.pending, counts.skipped
    )
}

/// The promotion sentence, such as `dev is 12 commits ahead of main and 0 behind.` Only the ahead count
/// takes the singular noun, so one commit reads `1 commit ahead`.
pub fn promotion_sentence(promotion: &Promotion) -> String {
    let noun = if promotion.ahead_by == 1 {
        "commit"
    } else {
        "commits"
    };
    format!(
        "{} is {} {noun} ahead of {} and {} behind.",
        promotion.head, promotion.ahead_by, promotion.base, promotion.behind_by
    )
}

/// When the answer was read, as the header says it: "Read 4m ago" against the client clock, or
/// "Read at <stamp>" when the clock or the stamp cannot be read.
pub fn read_label(now_ms: Option<i64>, stamp: &str) -> String {
    match age_text(now_ms, stamp) {
        Some(age) => format!("Read {age}"),
        None => format!("Read at {stamp}"),
    }
}

/// A time cell (`Updated`): the age of `stamp` on the client clock, or the stamp itself when the clock
/// or the stamp cannot be read.
pub fn age_label(now_ms: Option<i64>, stamp: &str) -> String {
    age_text(now_ms, stamp).unwrap_or_else(|| stamp.to_string())
}

/// The required checks a pull's Required cell shows. Only a pull into `main` has any: the answer names none
/// for a pull into another base, so the cell shows a dash for it.
pub fn required_shown(pull: &Pull) -> &[RequiredCheck] {
    if pull.base == "main" {
        pull.required.as_slice()
    } else {
        &[]
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ui::work::parse_rfc3339_ms;

    #[test]
    fn each_server_state_word_reads_as_its_state() {
        let read = |text: &str| {
            let value = serde_json::json!({"state": text, "pulls": []});
            serde_json::from_value::<DeliveryBody>(value)
                .expect("body")
                .state
        };
        assert_eq!(read("ok"), DeliveryState::Ok);
        assert_eq!(read("unconfigured"), DeliveryState::Unconfigured);
        assert_eq!(read("loading"), DeliveryState::Loading);
        assert_eq!(read("error"), DeliveryState::Error);
        assert_eq!(read("degraded-new-word"), DeliveryState::Unknown);
        assert_eq!(DELIVERY_PATH, "/v1/delivery");
    }

    #[test]
    fn an_ok_answer_reads_its_promotion_pulls_and_checks() {
        let value = serde_json::json!({
            "state": "ok",
            "repo": "uhstray-io/agent-cloud",
            "ref": "dev",
            "required_checks": ["Static Analysis", "Security Scan", "Unit Tests"],
            "ruleset_url": "https://github.com/uhstray-io/agent-cloud/blob/dev/.github/rulesets/protect-main.json",
            "promotion": {
                "base": "main",
                "head": "dev",
                "ahead_by": 12,
                "behind_by": 0,
                "compare_url": "https://github.com/uhstray-io/agent-cloud/compare/main...dev",
                "pull": {
                    "number": 916,
                    "title": "Promote dev",
                    "author": "jestyr27",
                    "base": "main",
                    "head": "dev",
                    "draft": false,
                    "url": "https://github.com/uhstray-io/agent-cloud/pull/916",
                    "updated_at": "2026-11-07T09:00:00Z",
                    "review": "changes_requested",
                    "checks": {"passed": 2, "failed": 1, "pending": 0, "skipped": 0},
                    "required": [
                        {"name": "Static Analysis", "state": "failed"},
                        {"name": "Unit Tests", "state": "missing"}
                    ]
                }
            },
            "pulls": [{
                "number": 915,
                "title": "docs: x",
                "author": "jestyr27",
                "base": "dev",
                "head": "docs/x",
                "draft": true,
                "url": "https://github.com/uhstray-io/agent-cloud/pull/915",
                "updated_at": "2026-11-06T09:00:00Z",
                "review": "approved",
                "checks": {"passed": 14, "failed": 0, "pending": 0, "skipped": 3},
                "required": []
            }],
            "refreshed_at": "2026-11-08T10:00:00Z",
            "error": null
        });
        let body: DeliveryBody = serde_json::from_value(value).expect("body");
        assert_eq!(body.state, DeliveryState::Ok);
        assert_eq!(body.repo.as_deref(), Some("uhstray-io/agent-cloud"));
        assert_eq!(body.git_ref.as_deref(), Some("dev"));
        assert_eq!(body.required_checks.len(), 3);
        let promotion = body.promotion.as_ref().expect("promotion");
        assert_eq!((promotion.ahead_by, promotion.behind_by), (12, 0));
        let pull = promotion.pull.as_ref().expect("promotion pull");
        assert_eq!(pull.number, 916);
        assert_eq!(pull.review, ReviewState::ChangesRequested);
        assert_eq!(pull.required[1].state, RequiredState::Missing);
        assert_eq!(body.pulls[0].checks.skipped, 3);
        assert!(body.pulls[0].draft);
        assert_eq!(body.pulls[0].review, ReviewState::Approved);
        assert!(body.pulls[0].required.is_empty());
        assert_eq!(body.error, None);
    }

    #[test]
    fn a_missing_state_reads_as_loading_and_nulls_as_none() {
        let body: DeliveryBody = serde_json::from_value(serde_json::json!({
            "error": null,
            "promotion": {"base": "main", "head": "dev", "ahead_by": 1, "behind_by": 0, "compare_url": "x", "pull": null}
        }))
        .expect("body");
        assert_eq!(body.state, DeliveryState::Loading);
        assert_eq!(body.error, None);
        assert_eq!(body.git_ref, None);
        assert_eq!(body.repo, None);
        assert_eq!(body.promotion.expect("promotion").pull, None);
        assert!(body.pulls.is_empty());
    }

    #[test]
    fn an_unknown_review_or_check_word_reads_as_unknown() {
        let pull: Pull = serde_json::from_value(serde_json::json!({
            "review": "dismissed",
            "required": [{"name": "Unit Tests", "state": "queued"}]
        }))
        .expect("pull");
        assert_eq!(pull.review, ReviewState::Unknown);
        assert_eq!(pull.required[0].state, RequiredState::Unknown);
        let silent: Pull = serde_json::from_value(serde_json::json!({})).expect("pull");
        assert_eq!(silent.review, ReviewState::Unknown);
    }

    #[test]
    fn only_an_ok_answer_has_data_to_show() {
        assert_eq!(not_ok_message(DeliveryState::Ok, None), None);
        assert_eq!(
            not_ok_message(DeliveryState::Unconfigured, None).as_deref(),
            Some("The GitHub source is not configured for this console.")
        );
        assert_eq!(
            not_ok_message(DeliveryState::Loading, None).as_deref(),
            Some("The first read of delivery state has not finished.")
        );
        assert_eq!(
            not_ok_message(DeliveryState::Error, Some("GitHub answered HTTP 401")).as_deref(),
            Some("Delivery state could not be read: GitHub answered HTTP 401. The next read tries again.")
        );
        assert!(not_ok_message(DeliveryState::Error, None)
            .expect("message")
            .contains("no reason was given"));
        assert_eq!(
            not_ok_message(DeliveryState::Unknown, None).as_deref(),
            Some("The console received a delivery state it does not know.")
        );
    }

    #[test]
    fn each_review_word_reads_as_its_icon_tone_and_label() {
        assert_eq!(
            ReviewState::Approved.look(),
            ("circle-check", Tone::Ok, "Approved")
        );
        assert_eq!(
            ReviewState::ChangesRequested.look(),
            ("triangle-alert", Tone::Warn, "Changes requested")
        );
        assert_eq!(
            ReviewState::NoReview.look(),
            ("circle-dashed", Tone::Idle, "No review")
        );
        assert_eq!(
            ReviewState::Unknown.look(),
            ("circle-dashed", Tone::Idle, "Unknown review state")
        );
    }

    #[test]
    fn each_required_word_reads_as_its_icon_tone_and_label() {
        assert_eq!(
            RequiredState::Passed.look(),
            ("circle-check", Tone::Ok, "Passed")
        );
        assert_eq!(
            RequiredState::Failed.look(),
            ("octagon-x", Tone::Crit, "Failed")
        );
        assert_eq!(
            RequiredState::Pending.look(),
            ("loader-circle", Tone::Run, "Pending")
        );
        assert_eq!(
            RequiredState::Skipped.look(),
            ("circle-dashed", Tone::Idle, "Skipped")
        );
        assert_eq!(
            RequiredState::Missing.look(),
            ("triangle-alert", Tone::Warn, "Missing")
        );
        assert_eq!(
            RequiredState::Unknown.look(),
            ("circle-dashed", Tone::Idle, "Unknown check state")
        );
    }

    #[test]
    fn checks_summary_names_each_count_in_order() {
        let counts = CheckCounts {
            passed: 14,
            failed: 0,
            pending: 0,
            skipped: 3,
        };
        assert_eq!(
            checks_summary(&counts),
            "14 passed \u{b7} 0 failed \u{b7} 0 pending \u{b7} 3 skipped"
        );
    }

    #[test]
    fn the_promotion_sentence_pluralises_only_the_ahead_count() {
        let promo = |ahead_by: u64, behind_by: u64| Promotion {
            base: "main".into(),
            head: "dev".into(),
            ahead_by,
            behind_by,
            compare_url: String::new(),
            pull: None,
        };
        assert_eq!(
            promotion_sentence(&promo(12, 0)),
            "dev is 12 commits ahead of main and 0 behind."
        );
        assert_eq!(
            promotion_sentence(&promo(1, 0)),
            "dev is 1 commit ahead of main and 0 behind."
        );
        assert_eq!(
            promotion_sentence(&promo(0, 2)),
            "dev is 0 commits ahead of main and 2 behind."
        );
    }

    #[test]
    fn the_read_age_is_relative_to_the_client_clock_and_falls_back_to_the_stamp() {
        let stamp = "2026-11-08T10:00:00Z";
        let read_at = parse_rfc3339_ms(stamp).expect("stamp");
        assert_eq!(read_label(Some(read_at + 240_000), stamp), "Read 4m ago");
        assert_eq!(read_label(None, stamp), "Read at 2026-11-08T10:00:00Z");
        assert_eq!(
            read_label(Some(read_at), "not a time"),
            "Read at not a time"
        );
        assert_eq!(age_label(Some(read_at + 120_000), stamp), "2m ago");
        assert_eq!(age_label(None, stamp), stamp);
    }

    #[test]
    fn only_a_pull_into_main_shows_required_checks() {
        let check = RequiredCheck {
            name: "Unit Tests".into(),
            state: RequiredState::Passed,
        };
        let into_main = Pull {
            base: "main".into(),
            required: vec![check.clone()],
            ..Pull::default()
        };
        let into_dev = Pull {
            base: "dev".into(),
            required: vec![check],
            ..Pull::default()
        };
        assert_eq!(required_shown(&into_main).len(), 1);
        assert!(required_shown(&into_dev).is_empty());
    }

    #[test]
    fn the_subtitle_names_the_ref_and_the_two_targets() {
        assert_eq!(
            subtitle("dev"),
            "Open pull requests into dev and main, their checks and reviews, and the promotion path."
        );
    }
}
