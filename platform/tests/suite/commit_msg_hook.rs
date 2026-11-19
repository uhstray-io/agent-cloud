//! The commit-msg hook refuses AI attribution in a commit message (cli tier, security set).
//!
//! Origin: 2026-09-22, a coding agent's harness appended attribution trailers to six commits,
//! one of them pushed (docs/MISTAKES.md 5.9). A rule carried by memory failed; this hook carries
//! it mechanically, and the attribution-free message is the behaviour these tests pin.
//!
//! Replaces platform/tests/test_commit_msg_hook.bats (plan 24, Phase 4).
//!
//! Port map (every BATS @test, once):
//!
//! | BATS @test | Rust fn |
//! | --- | --- |
//! | commit-msg: a plain message passes | `a_plain_message_passes` |
//! | commit-msg: an assistant co-author trailer is refused | `an_assistant_co_author_trailer_is_refused` |
//! | commit-msg: a session link trailer is refused | `a_session_link_trailer_is_refused` |
//! | commit-msg: a generated-with footer is refused | `a_generated_with_footer_is_refused` |
//! | commit-msg: a human co-author still passes | `a_human_co_author_still_passes` |

use std::process::Output;

use agent_cloud_tests::process;
use agent_cloud_tests::repo;
use tempfile::TempDir;

const HOOK: &str = ".githooks/commit-msg";

/// Runs the hook on a commit message holding `message`, as git runs it: the message file is its
/// one argument. The hook is executed directly, so its shebang and mode are part of the check.
fn verdict(message: &str) -> Output {
    let dir = TempDir::new().expect("a temporary directory");
    let msg = dir.path().join("msg");
    std::fs::write(&msg, message).expect("the message is written");
    let hook = repo::path(HOOK);
    process::scrubbed(hook.to_str().expect("a UTF-8 path"), dir.path())
        .arg(&msg)
        .output()
        .expect("the hook starts")
}

fn code(out: &Output) -> i32 {
    out.status.code().expect("the hook exits, it is not killed")
}

fn stderr(out: &Output) -> String {
    String::from_utf8_lossy(&out.stderr).into_owned()
}

#[test]
fn a_plain_message_passes() {
    // Arrange
    let message = "feat(x): do a thing\n\nBecause reasons.\n";

    // Act
    let out = verdict(message);

    // Assert
    assert_eq!(
        code(&out),
        0,
        "a plain message was refused: {}",
        stderr(&out)
    );
}

#[test]
fn an_assistant_co_author_trailer_is_refused() {
    // Arrange
    let message = "feat(x): do a thing\n\nCo-Authored-By: Claude Opus <noreply@anthropic.com>\n";

    // Act
    let out = verdict(message);

    // Assert
    assert_eq!(code(&out), 1);
    assert!(
        stderr(&out).contains("AI attribution"),
        "the refusal does not name the rule: {}",
        stderr(&out)
    );
}

#[test]
fn a_session_link_trailer_is_refused() {
    // Arrange
    let message = "feat(x): do a thing\n\nClaude-Session: https://claude.ai/code/session_x\n";

    // Act
    let out = verdict(message);

    // Assert
    assert_eq!(code(&out), 1);
    assert!(stderr(&out).contains("AI attribution"), "{}", stderr(&out));
}

#[test]
fn a_generated_with_footer_is_refused() {
    // Arrange
    let message =
        "feat(x): do a thing\n\nGenerated with [Claude Code](https://claude.com/claude-code)\n";

    // Act
    let out = verdict(message);

    // Assert
    assert_eq!(code(&out), 1);
    assert!(stderr(&out).contains("AI attribution"), "{}", stderr(&out));
}

#[test]
fn a_human_co_author_still_passes() {
    // Arrange
    let message = "feat(x): pair work\n\nCo-Authored-By: A Teammate <teammate@example.com>\n";

    // Act
    let out = verdict(message);

    // Assert
    assert_eq!(
        code(&out),
        0,
        "a human co-author was refused: {}",
        stderr(&out)
    );
}
