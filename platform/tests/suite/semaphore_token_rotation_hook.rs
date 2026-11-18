//! The Bash PreToolUse guard that blocks an interpreter reading stdin from a secret file (cli tier,
//! security set).
//!
//! `.claude/hooks/guard-secret-stdin.py` refuses a Bash command that feeds a secret file to an
//! interpreter's stdin. This is the docs/MISTAKES.md 4.10 leak: the operator's token file was
//! parsed as a program's source, and the SyntaxError printed the token.
//! The guard reads a JSON event on stdin and answers with exit 2 on a block. These cases run the
//! hook as Claude Code runs it, and check that the project settings register it.
//!
//! The decision logic itself stays in platform/tests/test_semaphore_token_rotation_hook.py, which
//! imports the guard in-process (its two deterministic cases of `unsafe_command`).
//!
//! Replaces the three hook-level cases of platform/tests/test_semaphore_token_rotation_hook.py
//! (plan 24, Phase 4).
//!
//! Port map (each ported Python test, once):
//!
//! | Python test | Rust fn |
//! | --- | --- |
//! | test_hook_stdin_protocol_blocks_without_echoing_command | `the_hook_blocks_a_secret_stdin_program_without_echoing_the_command` |
//! | test_hook_allows_safe_script_file_command | `the_hook_allows_a_script_file_fed_a_secret_path` |
//! | test_project_settings_registers_the_bash_pretool_guard | `the_project_settings_register_the_bash_pretool_guard` |

use std::process::Output;

use agent_cloud_tests::process;
use agent_cloud_tests::repo;
use agent_cloud_tests::text;
use serde_json::Value;
use tempfile::TempDir;

const GUARD: &str = ".claude/hooks/guard-secret-stdin.py";

/// Runs the guard on `event`, one Bash tool event as JSON text (as the harness sends it). Returns
/// the exit status and the output.
fn run_guard(event: &str) -> Output {
    let home = TempDir::new().expect("a temporary directory");
    let guard = repo::path(GUARD);
    process::scrubbed("python3", home.path())
        .arg(guard.to_str().expect("a UTF-8 path"))
        .write_stdin(event)
        .output()
        .expect("python3 starts")
}

fn code(out: &Output) -> i32 {
    out.status
        .code()
        .expect("the guard exits, it is not killed")
}

#[test]
fn the_hook_blocks_a_secret_stdin_program_without_echoing_the_command() {
    // Arrange: an interpreter reads its program from a heredoc and its stdin from a secret file.
    // The heredoc body is the marker the refusal must not repeat.
    let event = r#"{"tool_input": {"command": "python3 - <<'PY' < site-config/secrets/operator/token\nsecret payload\nPY"}}"#;

    // Act
    let out = run_guard(event);

    // Assert
    assert_eq!(code(&out), 2, "the command was not blocked");
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(
        !stderr.contains("secret payload"),
        "the refusal echoed the command: {stderr}"
    );
    assert!(
        !stderr.contains("token"),
        "the refusal echoed the secret path: {stderr}"
    );
}

#[test]
fn the_hook_allows_a_script_file_fed_a_secret_path() {
    // Arrange: a script file (not stdin code) reads the secret, which is allowed.
    let event = r#"{"tool_input": {"command": "python3 scripts/read-token.py < site-config/secrets/operator/token"}}"#;

    // Act
    let out = run_guard(event);

    // Assert
    assert_eq!(code(&out), 0, "{}", String::from_utf8_lossy(&out.stderr));
}

#[test]
fn the_project_settings_register_the_bash_pretool_guard() {
    // Arrange: parsed as JSON, as the original's json.loads did. A YAML reader also accepts text
    // that is not JSON, such as a trailing comma.
    let settings: Value = serde_json::from_str(&text::read(".claude/settings.json"))
        .expect(".claude/settings.json is JSON");

    // Act
    let hook = &settings["hooks"]["PreToolUse"][0];
    let inner = &hook["hooks"][0];

    // Assert
    assert_eq!(hook["matcher"], "Bash");
    assert_eq!(inner["type"], "command");
    let command = inner["command"].as_str();
    assert!(
        command.is_some_and(|c| c.contains("guard-secret-stdin.py")),
        "the registered command is {command:?}"
    );
}
