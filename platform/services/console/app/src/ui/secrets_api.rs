//! The Secrets page's answer as the browser reads it: `GET /v1/secrets`, admin-only. The shapes
//! are the ones `src/server/secrets.rs` serializes. Every struct carries `#[serde(default)]`, so a
//! missing key reads as empty. Nothing in this file writes to the API.

use serde::Deserialize;

use super::work::age_text;

/// The admin-only secrets answer.
pub const SECRETS_PATH: &str = "/v1/secrets";

/// The state the server names. Any other word is `Unknown`, which the page shows as a failed
/// answer rather than as an empty list.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum SecretsState {
    Ok,
    Unconfigured,
    /// No walk has finished yet. Also the value when the envelope omits the field.
    #[default]
    Loading,
    Error,
    #[serde(other)]
    Unknown,
}

/// `GET /v1/secrets`.
#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct SecretsBody {
    pub state: SecretsState,
    pub paths: Vec<SecretPath>,
    /// When the newest good walk finished. Older than the walk that failed, when `error` is set.
    pub refreshed_at: Option<String>,
    /// True when a bound stopped the walk, so the list is not the whole of OpenBao.
    pub truncated: bool,
    /// Why the newest walk failed, when it did. Carries a status only, never a credential.
    pub error: Option<String>,
}

/// One secret's metadata. Versions and times only; the server sends no value.
#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct SecretPath {
    pub path: String,
    pub current_version: u64,
    pub versions: u64,
    pub created_time: String,
    pub updated_time: String,
}

/// The sentence an answer with no table shows. `None` when the state has a table to show.
pub fn not_ok_message(state: SecretsState, error: Option<&str>) -> Option<String> {
    match state {
        SecretsState::Ok => None,
        SecretsState::Unconfigured => {
            Some("OpenBao is not configured for this console.".to_string())
        }
        SecretsState::Loading => {
            Some("The first read of OpenBao metadata has not finished.".to_string())
        }
        SecretsState::Error => Some(format!(
            "OpenBao could not be read: {}. The next read tries again.",
            error.unwrap_or("no reason was given")
        )),
        SecretsState::Unknown => {
            Some("The console received a secrets state it does not know.".to_string())
        }
    }
}

/// A time cell (`Created`, `Last changed`): the age of `stamp` on the client clock, or the stamp
/// itself when the clock or the stamp cannot be read.
pub fn age_label(now_ms: Option<i64>, stamp: &str) -> String {
    age_text(now_ms, stamp).unwrap_or_else(|| stamp.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn each_server_state_word_reads_as_its_state() {
        let read = |text: &str| {
            let value = serde_json::json!({"state": text, "paths": []});
            serde_json::from_value::<SecretsBody>(value)
                .expect("body")
                .state
        };
        assert_eq!(read("ok"), SecretsState::Ok);
        assert_eq!(read("unconfigured"), SecretsState::Unconfigured);
        assert_eq!(read("loading"), SecretsState::Loading);
        assert_eq!(read("error"), SecretsState::Error);
        assert_eq!(read("degraded-new-word"), SecretsState::Unknown);
    }

    #[test]
    fn an_ok_answer_reads_its_paths_truncation_and_stale_error() {
        let value = serde_json::json!({
            "state": "ok",
            "paths": [{
                "path": "secret/services/tududi",
                "current_version": 4,
                "versions": 3,
                "created_time": "2026-01-01T00:00:00Z",
                "updated_time": "2026-07-01T00:00:00Z"
            }],
            "refreshed_at": "2026-11-03T10:00:00Z",
            "truncated": true,
            "error": "OpenBao answered HTTP 500"
        });
        let body: SecretsBody = serde_json::from_value(value).expect("body");
        assert_eq!(body.state, SecretsState::Ok);
        assert_eq!(body.paths[0].current_version, 4);
        assert_eq!(body.paths[0].versions, 3);
        assert!(body.truncated);
        assert_eq!(body.error.as_deref(), Some("OpenBao answered HTTP 500"));
    }

    #[test]
    fn a_missing_state_reads_as_loading_and_nulls_as_none() {
        let body: SecretsBody =
            serde_json::from_value(serde_json::json!({"error": null})).expect("body");
        assert_eq!(body.state, SecretsState::Loading);
        assert_eq!(body.error, None);
        assert!(body.paths.is_empty());
        assert!(!body.truncated);
    }

    #[test]
    fn only_an_ok_answer_has_a_table() {
        assert_eq!(not_ok_message(SecretsState::Ok, None), None);
        assert_eq!(
            not_ok_message(SecretsState::Unconfigured, None).as_deref(),
            Some("OpenBao is not configured for this console.")
        );
        assert_eq!(
            not_ok_message(SecretsState::Error, Some("OpenBao answered HTTP 403")).as_deref(),
            Some(
                "OpenBao could not be read: OpenBao answered HTTP 403. The next read tries again."
            )
        );
        assert!(not_ok_message(SecretsState::Error, None)
            .expect("message")
            .contains("no reason was given"));
        assert!(not_ok_message(SecretsState::Unknown, None).is_some());
    }

    #[test]
    fn age_label_falls_back_to_the_stamp_without_a_clock() {
        assert_eq!(
            age_label(None, "2026-07-01T00:00:00Z"),
            "2026-07-01T00:00:00Z"
        );
        assert_eq!(age_label(Some(0), "not a time"), "not a time");
    }
}
