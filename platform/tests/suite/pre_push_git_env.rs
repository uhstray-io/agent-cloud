//! The pre-push hook runs its suites with a clean git environment (a cli-tier module in the
//! security set). Its first case runs the hook; its other two checks are static assertions that
//! read the hook and the helper and run neither.
//!
//! Git exports its repository variables (GIT_DIR and friends) to hooks. Left set, a test's
//! scratch `git init` or `git config` writes the REAL repository's shared `.git/config`: on
//! 2026-09-23 that set core.bare=true and a fake user.email for every checkout (docs/MISTAKES.md
//! 3.7). The first case replays it: the hook runs as git would, GIT_DIR points at a victim
//! repository, and a fake `bats` does what the offending setup did.
//!
//! The variables the hook clears and the ones the shared bash helper clears are kept identical
//! by the last test, so one cannot gain a variable the other misses.
//!
//! Replaces platform/tests/test_pre_push_git_env.bats (plan 24, Phase 4).
//!
//! Port map (every BATS @test, once):
//!
//! | BATS @test | Rust fn |
//! | --- | --- |
//! | pre-push: a suite's git commands cannot reach the pushing repository | `a_suites_git_commands_cannot_reach_the_pushing_repository` |
//! | pre-push: the git environment is cleared before any suite runs | `the_git_environment_is_cleared_before_any_suite_runs` |
//! | pre-push: assert_helpers clears exactly the git variables the hook clears | `assert_helpers_clears_exactly_the_git_variables_the_hook_clears` |

use std::os::unix::fs::PermissionsExt;
use std::path::Path;

use agent_cloud_tests::process;
use agent_cloud_tests::repo;
use agent_cloud_tests::text;
use tempfile::TempDir;

const HOOK: &str = ".githooks/pre-push";
const HELPER: &str = "platform/tests/assert_helpers.bash";

/// One pushed ref with a non-zero sha, as git writes it on the hook's stdin.
const REFLINE: &str = "refs/heads/x 1111111111111111111111111111111111111111 refs/heads/x 0000000000000000000000000000000000000000\n";

/// What the 2026-09-23 test setup did, from a scratch directory: a fake `bats` on PATH. It first
/// writes FAKE_BATS_MARKER, so the test can tell the hook reached its BATS step. The scratch
/// repository goes under TMPDIR, the test's own TempDir: macOS `mktemp -d` ignores TMPDIR, so a
/// `mktemp -d` here left a repository in the system temp directory after every run.
const FAKE_BATS: &str = r#"#!/usr/bin/env sh
# What the 2026-09-23 test setup did, from a scratch directory.
: > "$FAKE_BATS_MARKER"
cd "$TMPDIR" && git init -q scratch && cd scratch \
  && git config user.email hacked@example.invalid && git config user.name hacked
exit 0
"#;

/// `git -C <dir> config --local <key>` read with a clean environment: its stdout, trimmed.
fn local_config(home: &Path, dir: &Path, key: &str) -> String {
    let out = process::scrubbed("git", home)
        .args(["-C"])
        .arg(dir)
        .args(["config", "--local", key])
        .output()
        .expect("git starts");
    String::from_utf8_lossy(&out.stdout).trim().to_owned()
}

#[test]
fn a_suites_git_commands_cannot_reach_the_pushing_repository() {
    // Arrange
    let home = TempDir::new().expect("a temporary directory");
    let victim = home.path().join("victim");
    let pushing = home.path().join("pushing");
    let fakebin = home.path().join("fakebin");
    for dir in [&victim, &pushing] {
        std::fs::create_dir(dir).expect("a repository directory");
        let out = process::scrubbed("git", home.path())
            .args(["init", "-q"])
            .arg(dir)
            .output()
            .expect("git starts");
        assert!(out.status.success(), "git init failed");
    }
    std::fs::create_dir_all(pushing.join("platform/tests")).expect("the tests directory");
    std::fs::create_dir(&fakebin).expect("the fake PATH directory");
    let bats = fakebin.join("bats");
    std::fs::write(&bats, FAKE_BATS).expect("the fake bats is written");
    std::fs::set_permissions(&bats, std::fs::Permissions::from_mode(0o755))
        .expect("the fake bats is executable");
    // Joined as OsString: a non-UTF-8 entry in the inherited PATH must survive.
    let mut entries = vec![fakebin.clone()];
    if let Some(inherited) = std::env::var_os("PATH") {
        entries.extend(std::env::split_paths(&inherited));
    }
    let path = std::env::join_paths(entries).expect("PATH holds the fake directory");
    let hook = repo::path(HOOK);
    let marker = home.path().join("bats-ran");

    // Act: the hook runs as git runs it, with GIT_DIR pointing at the victim repository.
    let _ = process::scrubbed("sh", home.path())
        .current_dir(&pushing)
        .env("GIT_DIR", victim.join(".git"))
        .env("PATH", path)
        .env("FAKE_BATS_MARKER", &marker)
        .arg(hook.to_str().expect("a UTF-8 path"))
        .arg("origin")
        .arg("https://example.invalid/repo.git")
        .write_stdin(REFLINE)
        .output()
        .expect("the hook starts");

    // Assert: the marker shows the hook reached the BATS step, where the setup wrote the victim.
    // Without it the victim checks below would pass whether or not the hook ran.
    assert!(
        marker.exists(),
        "the hook stopped before its BATS step, so the victim checks prove nothing"
    );
    let email = local_config(home.path(), &victim, "user.email");
    assert!(email.is_empty(), "the victim's user.email became {email:?}");
    let bare = local_config(home.path(), &victim, "core.bare");
    assert_eq!(bare, "false", "the victim's core.bare became {bare:?}");
}

#[test]
fn the_git_environment_is_cleared_before_any_suite_runs() {
    // Arrange
    let hook = text::read(HOOK);

    // Act + Assert: the first unset strictly before the first bats line; a missing one panics.
    text::assert_precedes(&hook, r"^unset GIT_DIR GIT_WORK_TREE", r"^[ \t]*bats ");
}

/// The git variables a bash file clears in its `unset GIT_DIR ...` statement, sorted. The
/// statement may span lines joined by a trailing backslash.
fn unset_git_vars(text: &str) -> Vec<String> {
    let mut words = Vec::new();
    for line in text
        .lines()
        .skip_while(|line| !line.starts_with("unset GIT_DIR"))
    {
        words.extend(
            line.replace('\\', " ")
                .split_whitespace()
                .filter(|word| word.starts_with("GIT_"))
                .map(str::to_owned),
        );
        if !line.ends_with('\\') {
            break;
        }
    }
    words.sort();
    words
}

#[test]
fn assert_helpers_clears_exactly_the_git_variables_the_hook_clears() {
    // Arrange
    let hook = text::read(HOOK);
    let helper = text::read(HELPER);

    // Act
    let hook_vars = unset_git_vars(&hook);
    let helper_vars = unset_git_vars(&helper);

    // Assert
    assert!(hook_vars.len() >= 8, "the hook clears only {hook_vars:?}");
    assert_eq!(hook_vars, helper_vars);
}
