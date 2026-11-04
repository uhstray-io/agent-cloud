//! The Policy page's answer as the browser reads it: `GET /v1/policies`, admin-only. The shapes are the
//! ones `src/server/policies.rs` serializes. Every struct carries `#[serde(default)]`, so a missing key reads
//! as empty. Nothing in this file writes to the API.

use serde::Deserialize;

use super::work::age_text;

/// The admin-only policy answer.
pub const POLICY_PATH: &str = "/v1/policies";

/// The state the server names. Any other word is `Unknown`, which the page shows as a failed answer
/// rather than as an empty list.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum PoliciesState {
    Ok,
    Unconfigured,
    /// No read has finished yet. Also the value when the envelope omits the field.
    #[default]
    Loading,
    Error,
    #[serde(other)]
    Unknown,
}

/// `GET /v1/policies`.
#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct PoliciesBody {
    pub state: PoliciesState,
    /// The repository the list was read from. `None` until a read has finished.
    pub repo: Option<String>,
    /// The ref the list was read from. `None` until a read has finished.
    #[serde(rename = "ref")]
    pub git_ref: Option<String>,
    pub files: Vec<PolicyFile>,
    /// When the newest good read finished. Older than the failed read, when `error` names one.
    pub refreshed_at: Option<String>,
    /// Why the newest read failed, or the file bound it was cut at. Never a credential.
    pub error: Option<String>,
}

/// One policy file. `kind` is one of `opa-policy`, `opa-test`, `opa-data` or `openbao`.
#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct PolicyFile {
    pub path: String,
    pub kind: String,
    pub name: String,
    pub last_commit: Option<LastCommit>,
    pub url: String,
}

/// The newest commit that touched a file on the ref.
#[derive(Debug, Clone, PartialEq, Default, Deserialize)]
#[serde(default)]
pub struct LastCommit {
    pub sha: String,
    pub date: String,
    pub subject: String,
}

/// The sentence an answer with no list shows. `None` when the state has a list to show.
pub fn not_ok_message(state: PoliciesState, error: Option<&str>) -> Option<String> {
    match state {
        PoliciesState::Ok => None,
        PoliciesState::Unconfigured => {
            Some("The policy source is not configured for this console.".to_string())
        }
        PoliciesState::Loading => {
            Some("The first read of the policy files has not finished.".to_string())
        }
        PoliciesState::Error => Some(format!(
            "The policy files could not be read: {}. The next read tries again.",
            error.unwrap_or("no reason was given")
        )),
        PoliciesState::Unknown => {
            Some("The console received a policy state it does not know.".to_string())
        }
    }
}

/// The files of the OPA card, in the order the server sent them.
pub fn opa_files(files: &[PolicyFile]) -> Vec<PolicyFile> {
    files
        .iter()
        .filter(|file| file.kind.starts_with("opa-"))
        .cloned()
        .collect()
}

/// The files of the OpenBao card, in the order the server sent them.
pub fn openbao_files(files: &[PolicyFile]) -> Vec<PolicyFile> {
    files
        .iter()
        .filter(|file| file.kind == "openbao")
        .cloned()
        .collect()
}

/// The label a kind tag shows. A kind the page does not know is shown as the server sent it.
pub fn kind_label(kind: &str) -> &str {
    match kind {
        "opa-policy" => "OPA policy",
        "opa-test" => "OPA test",
        "opa-data" => "OPA data",
        "openbao" => "OpenBao policy",
        other => other,
    }
}

/// When the list was read, as the header and the stale banner say it: "Read 4m ago" against the client
/// clock, or "Read at <stamp>" when the clock or the stamp cannot be read.
pub fn read_label(now_ms: Option<i64>, stamp: &str) -> String {
    match age_text(now_ms, stamp) {
        Some(age) => format!("Read {age}"),
        None => format!("Read at {stamp}"),
    }
}

/// The header line once a read has named its ref. It says the list is a read from GitHub, and that no
/// decision counts are shown.
pub fn subtitle(git_ref: &str) -> String {
    format!(
        "Policy files as they are on {git_ref}, read from GitHub. Decision counts are not shown: the console has no source for them."
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn each_server_state_word_reads_as_its_state() {
        let read = |text: &str| {
            let value = serde_json::json!({"state": text, "files": []});
            serde_json::from_value::<PoliciesBody>(value)
                .expect("body")
                .state
        };
        assert_eq!(read("ok"), PoliciesState::Ok);
        assert_eq!(read("unconfigured"), PoliciesState::Unconfigured);
        assert_eq!(read("loading"), PoliciesState::Loading);
        assert_eq!(read("error"), PoliciesState::Error);
        assert_eq!(read("degraded-new-word"), PoliciesState::Unknown);
    }

    #[test]
    fn an_ok_answer_reads_its_files_source_and_banner() {
        let value = serde_json::json!({
            "state": "ok",
            "repo": "uhstray-io/agent-cloud",
            "ref": "dev",
            "files": [{
                "path": "platform/services/opa/deployment/policies/system/authz.rego",
                "kind": "opa-policy",
                "name": "system.authz",
                "last_commit": {"sha": "23d9511", "date": "2026-10-30T12:00:00Z", "subject": "security(opa): refuse"},
                "url": "https://github.com/uhstray-io/agent-cloud/blob/dev/x"
            }, {
                "path": "platform/services/openbao/deployment/config/policies/console.hcl",
                "kind": "openbao",
                "name": "console",
                "last_commit": null,
                "url": "https://github.com/uhstray-io/agent-cloud/blob/dev/y"
            }],
            "refreshed_at": "2026-11-04T10:00:00Z",
            "error": "More than 80 policy files; the first 80 are shown."
        });
        let body: PoliciesBody = serde_json::from_value(value).expect("body");
        assert_eq!(body.state, PoliciesState::Ok);
        assert_eq!(body.git_ref.as_deref(), Some("dev"));
        assert_eq!(body.files[0].name, "system.authz");
        assert_eq!(
            body.files[0].last_commit.as_ref().map(|c| c.sha.as_str()),
            Some("23d9511")
        );
        assert_eq!(body.files[1].last_commit, None);
        assert_eq!(
            body.error.as_deref(),
            Some("More than 80 policy files; the first 80 are shown.")
        );
    }

    #[test]
    fn a_missing_state_reads_as_loading_and_nulls_as_none() {
        let body: PoliciesBody =
            serde_json::from_value(serde_json::json!({"error": null})).expect("body");
        assert_eq!(body.state, PoliciesState::Loading);
        assert_eq!(body.error, None);
        assert_eq!(body.git_ref, None);
        assert!(body.files.is_empty());
    }

    #[test]
    fn only_an_ok_answer_has_a_list() {
        assert_eq!(not_ok_message(PoliciesState::Ok, None), None);
        assert_eq!(
            not_ok_message(PoliciesState::Unconfigured, None).as_deref(),
            Some("The policy source is not configured for this console.")
        );
        assert_eq!(
            not_ok_message(PoliciesState::Error, Some("GitHub answered HTTP 401")).as_deref(),
            Some("The policy files could not be read: GitHub answered HTTP 401. The next read tries again.")
        );
        assert!(not_ok_message(PoliciesState::Error, None)
            .expect("message")
            .contains("no reason was given"));
        assert!(not_ok_message(PoliciesState::Unknown, None).is_some());
    }

    #[test]
    fn files_split_into_the_opa_and_openbao_cards_by_kind() {
        let file = |kind: &str, name: &str| PolicyFile {
            kind: kind.into(),
            name: name.into(),
            ..PolicyFile::default()
        };
        let files = vec![
            file("opa-policy", "a"),
            file("opa-test", "b"),
            file("opa-data", "c"),
            file("openbao", "d"),
            file("something-new", "e"),
        ];
        let opa: Vec<String> = opa_files(&files).into_iter().map(|f| f.name).collect();
        let bao: Vec<String> = openbao_files(&files).into_iter().map(|f| f.name).collect();
        assert_eq!(opa, ["a", "b", "c"]);
        assert_eq!(bao, ["d"]);
    }

    #[test]
    fn kind_tags_read_as_words_and_an_unknown_kind_is_shown_as_sent() {
        assert_eq!(kind_label("opa-policy"), "OPA policy");
        assert_eq!(kind_label("opa-test"), "OPA test");
        assert_eq!(kind_label("opa-data"), "OPA data");
        assert_eq!(kind_label("openbao"), "OpenBao policy");
        assert_eq!(kind_label("mystery"), "mystery");
    }

    #[test]
    fn the_read_age_is_relative_to_the_client_clock_and_falls_back_to_the_stamp() {
        // 2026-11-04T10:00:00Z, four minutes earlier on the client clock.
        let stamp = "2026-11-04T10:00:00Z";
        let read_at = 1_793_786_400_000_i64;
        assert_eq!(read_label(Some(read_at + 240_000), stamp), "Read 4m ago");
        assert_eq!(read_label(None, stamp), "Read at 2026-11-04T10:00:00Z");
        assert_eq!(
            read_label(Some(read_at), "not a time"),
            "Read at not a time"
        );
    }

    #[test]
    fn the_subtitle_names_the_ref_and_disclaims_decision_counts() {
        assert_eq!(
            subtitle("dev"),
            "Policy files as they are on dev, read from GitHub. Decision counts are not shown: the console has no source for them."
        );
    }
}
