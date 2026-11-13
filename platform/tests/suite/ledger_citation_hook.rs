//! The ledger-citations pre-commit hook runs the citation test, and only on the ledger and its
//! ratchet (static tier, security set).
//!
//! Origin: 2026-11-04, a ledger edit with a dead citation was pushed under SKIP_TESTS=1
//! (docs/MISTAKES.md 5.2). The hook is read from `.pre-commit-config.yaml` as parsed YAML, by its
//! id, as `suite/lanes.rs` reads the rust-commit-lane hook. A deleted `files` line, a dropped skip
//! or a retargeted entry turns it red.
//!
//! Replaces platform/tests/test_ledger_citation_hook.bats (plan 24, Phase 4).
//!
//! Port map (every BATS @test, once):
//!
//! | BATS @test | Rust fn |
//! | --- | --- |
//! | ledger citations hook: entry runs the citation test | `the_hook_entry_runs_the_citation_test` (candidate: pins text) |
//! | ledger citations hook: files regex covers the ledger and its citation ratchet | `the_files_pattern_is_exactly_the_ledger_and_its_citation_ratchet` (candidate: pins text) |
//! | ledger citations hook: passes no filenames and prints its output when it passes | `the_hook_passes_no_filenames_and_prints_its_output_when_it_passes` |
//! | ledger citations hook: a missing pytest skips with a message, a real failure still fails | `without_pytest_the_entry_skips_with_a_message_and_otherwise_execs_pytest` (candidate: pins text) |

use agent_cloud_tests::repo;
use agent_cloud_tests::yaml::{self, Node};

const HOOK_ID: &str = "ledger-citations";
const CITATION_TEST: &str = "platform/tests/test_ledger_test_citations.py";
const LEDGER_FILES: &str = r"^(docs/MISTAKES\.md|platform/tests/ledger_citation_ratchet\.txt)$";

/// The `ledger-citations` hook, found by its id across every repo block.
fn ledger_hook() -> Node {
    let config = yaml::load_one(&repo::path(".pre-commit-config.yaml"))
        .expect(".pre-commit-config.yaml has one document");
    config
        .get("repos")
        .and_then(Node::as_seq)
        .into_iter()
        .flatten()
        .filter_map(|block| block.get("hooks")?.as_seq())
        .flatten()
        .find(|hook| hook.get("id").and_then(Node::as_str) == Some(HOOK_ID))
        .cloned()
        .expect("a ledger-citations hook")
}

/// The hook's `entry` text. A folded (`>-`) scalar reads as one line, with its breaks as spaces.
fn entry() -> String {
    ledger_hook()
        .get("entry")
        .and_then(Node::as_str)
        .expect("the ledger-citations hook has an entry")
        .to_owned()
}

#[test]
fn the_hook_entry_runs_the_citation_test() {
    // Arrange
    let entry = entry();
    let command = format!("python3 -m pytest -q -p no:cacheprovider {CITATION_TEST}");

    // Act
    let runs_it = entry.contains(&command);

    // Assert
    assert!(runs_it, "the entry does not run `{command}`: {entry}");
}

#[test]
fn the_files_pattern_is_exactly_the_ledger_and_its_citation_ratchet() {
    // Arrange
    let hook = ledger_hook();

    // Act
    let files = hook.get("files").and_then(Node::as_str);

    // Assert
    assert_eq!(files, Some(LEDGER_FILES));
}

#[test]
fn the_hook_passes_no_filenames_and_prints_its_output_when_it_passes() {
    // Arrange
    let hook = ledger_hook();

    // Act
    let pass_filenames = hook.get("pass_filenames").cloned();
    let verbose = hook.get("verbose").cloned();

    // Assert
    assert_eq!(pass_filenames, Some(Node::Bool(false)));
    assert_eq!(verbose, Some(Node::Bool(true)));
}

#[test]
fn without_pytest_the_entry_skips_with_a_message_and_otherwise_execs_pytest() {
    // Arrange
    let entry = entry();
    let fragments = [
        r#"python3 -c "import pytest" 2>/dev/null || {"#,
        "pytest is not installed for python3; skipped (CI still runs it)",
        "exit 0; };",
        "exec python3 -m pytest",
    ];

    // Act
    let missing: Vec<&str> = fragments
        .into_iter()
        .filter(|fragment| !entry.contains(fragment))
        .collect();

    // Assert
    assert!(missing.is_empty(), "the entry lacks {missing:?}: {entry}");
}
