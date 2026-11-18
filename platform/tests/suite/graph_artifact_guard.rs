//! The graph-artifact commit gate (scripts/check-graph-artifact.sh) and its pre-commit wiring
//! (cli tier, security set).
//!
//! Origin: 2026-09-23, codebase-memory-mcp's auto-index rewrote `artifact.json` under a
//! path-derived project name while the graph it describes was deleted from the working tree
//! (docs/MISTAKES.md 6.6). Each case replays the gate in a throwaway repository, staging exactly
//! the state it is about. The wiring test reads `.pre-commit-config.yaml` as parsed YAML: a file
//! filter would skip a graph-only deletion, because pre-commit leaves deleted paths out of the
//! file list it matches.
//!
//! Replaces platform/tests/test_graph_artifact_guard.bats (plan 24, Phase 4).
//!
//! Port map (every BATS @test, once):
//!
//! | BATS @test | Rust fn |
//! | --- | --- |
//! | graph guard: nothing staged under .codebase-memory passes | `nothing_staged_under_codebase_memory_passes` |
//! | graph guard: a consistent regeneration passes | `a_consistent_regeneration_passes` |
//! | graph guard: refuses the graph staged for deletion (the 2026-09-23 state) | `refuses_the_graph_staged_for_deletion` |
//! | graph guard: refuses a path-derived project id | `refuses_a_path_derived_project_id` |
//! | graph guard: refuses metadata that describes a different graph | `refuses_metadata_that_describes_a_different_graph` |
//! | graph guard: refuses artifact.json that is not JSON | `refuses_artifact_json_that_is_not_json` |
//! | graph guard: wired into the pre-commit suite for .codebase-memory changes | `the_pre_commit_hook_runs_the_guard_on_every_commit` |

use std::path::{Path, PathBuf};
use std::process::Output;

use agent_cloud_tests::process;
use agent_cloud_tests::repo;
use agent_cloud_tests::yaml::{self, Node};
use tempfile::TempDir;

const GUARD: &str = "scripts/check-graph-artifact.sh";
const GRAPH: &str = ".codebase-memory/graph.db.zst";
const ARTIFACT: &str = ".codebase-memory/artifact.json";

/// `git` in `dir`, with an identity given on the command line, so no repository config is
/// written. Panics unless git succeeds.
fn git(dir: &Path, args: &[&str]) {
    let out = process::scrubbed("git", dir)
        .current_dir(dir)
        .args([
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgsign=false",
        ])
        .args(args)
        .output()
        .expect("git starts");
    assert!(
        out.status.success(),
        "git {args:?} failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );
}

/// `artifact.json` as the graph tooling writes it: a project and the size of the graph it
/// describes.
fn write_meta(repo: &Path, project: &str, compressed_size: u64) {
    let body = format!(
        "{{\"schema_version\": 2, \"project\": \"{project}\", \"compressed_size\": {compressed_size}}}\n"
    );
    std::fs::write(repo.join(ARTIFACT), body).expect("artifact.json is written");
}

/// A repository whose base commit holds a consistent graph artifact: a graph of 11 bytes under
/// the documented project id.
fn base_repo(home: &TempDir) -> PathBuf {
    let repo = home.path().join("repo");
    git(
        home.path(),
        &["init", "-q", repo.to_str().expect("a UTF-8 path")],
    );
    std::fs::create_dir(repo.join(".codebase-memory")).expect("the artifact directory");
    std::fs::write(repo.join(GRAPH), "graph-bytes").expect("the graph is written");
    write_meta(&repo, "agent-cloud", 11);
    git(&repo, &["add", "-A"]);
    git(&repo, &["commit", "-qm", "base"]);
    repo
}

/// Runs the guard in `repo`, as the pre-commit hook does, and returns its output.
fn run_guard(home: &TempDir, repo: &Path) -> Output {
    let guard = repo::path(GUARD);
    process::scrubbed("sh", home.path())
        .current_dir(repo)
        .arg(guard.to_str().expect("a UTF-8 path"))
        .output()
        .expect("the guard starts")
}

fn code(out: &Output) -> i32 {
    out.status
        .code()
        .expect("the guard exits, it is not killed")
}

fn stderr(out: &Output) -> String {
    String::from_utf8_lossy(&out.stderr).into_owned()
}

#[test]
fn nothing_staged_under_codebase_memory_passes() {
    // Arrange
    let home = TempDir::new().expect("a temporary directory");
    let repo = base_repo(&home);
    std::fs::write(repo.join("other"), "x").expect("a file is written");
    git(&repo, &["add", "other"]);

    // Act
    let out = run_guard(&home, &repo);

    // Assert
    assert_eq!(code(&out), 0, "{}", stderr(&out));
}

#[test]
fn a_consistent_regeneration_passes() {
    // Arrange
    let home = TempDir::new().expect("a temporary directory");
    let repo = base_repo(&home);
    std::fs::write(repo.join(GRAPH), "new-graph-bytes!").expect("the graph is rewritten");
    write_meta(&repo, "agent-cloud", 16);
    git(&repo, &["add", "-A"]);

    // Act
    let out = run_guard(&home, &repo);

    // Assert
    assert_eq!(code(&out), 0, "{}", stderr(&out));
}

#[test]
fn refuses_the_graph_staged_for_deletion() {
    // Arrange
    let home = TempDir::new().expect("a temporary directory");
    let repo = base_repo(&home);
    git(&repo, &["rm", "-q", "--cached", GRAPH]);
    write_meta(&repo, "Users-user-Documents-GitHub-agent-cloud", 2_391_196);
    git(&repo, &["add", ARTIFACT]);

    // Act
    let out = run_guard(&home, &repo);

    // Assert
    assert_ne!(code(&out), 0);
    assert!(
        stderr(&out).contains("staged for deletion"),
        "the refusal does not name the deletion: {}",
        stderr(&out)
    );
}

#[test]
fn refuses_a_path_derived_project_id() {
    // Arrange
    let home = TempDir::new().expect("a temporary directory");
    let repo = base_repo(&home);
    write_meta(&repo, "Users-user-Documents-GitHub-agent-cloud", 11);
    git(&repo, &["add", "-A"]);

    // Act
    let out = run_guard(&home, &repo);

    // Assert
    assert_ne!(code(&out), 0);
    assert!(
        stderr(&out).contains("expected 'agent-cloud'"),
        "{}",
        stderr(&out)
    );
}

#[test]
fn refuses_metadata_that_describes_a_different_graph() {
    // Arrange
    let home = TempDir::new().expect("a temporary directory");
    let repo = base_repo(&home);
    write_meta(&repo, "agent-cloud", 999);
    git(&repo, &["add", "-A"]);

    // Act
    let out = run_guard(&home, &repo);

    // Assert
    assert_ne!(code(&out), 0);
    assert!(
        stderr(&out).contains("compressed_size 999"),
        "{}",
        stderr(&out)
    );
}

#[test]
fn refuses_artifact_json_that_is_not_json() {
    // Arrange
    let home = TempDir::new().expect("a temporary directory");
    let repo = base_repo(&home);
    std::fs::write(repo.join(ARTIFACT), "not json").expect("artifact.json is written");
    git(&repo, &["add", "-A"]);

    // Act
    let out = run_guard(&home, &repo);

    // Assert
    assert_ne!(code(&out), 0);
    assert!(stderr(&out).contains("not valid JSON"), "{}", stderr(&out));
}

/// The `graph-artifact-consistent` hook, found by its id across every repo block.
fn graph_hook() -> Node {
    let config = yaml::load_one(&repo::path(".pre-commit-config.yaml"))
        .expect(".pre-commit-config.yaml has one document");
    config
        .get("repos")
        .and_then(Node::as_seq)
        .into_iter()
        .flatten()
        .filter_map(|block| block.get("hooks")?.as_seq())
        .flatten()
        .find(|hook| hook.get("id").and_then(Node::as_str) == Some("graph-artifact-consistent"))
        .cloned()
        .expect("a graph-artifact-consistent hook")
}

#[test]
fn the_pre_commit_hook_runs_the_guard_on_every_commit() {
    // Arrange
    let hook = graph_hook();

    // Act
    let entry = hook.get("entry").and_then(Node::as_str);
    let always_run = hook.get("always_run").cloned();
    let has_files_filter = hook.get("files").is_some();

    // Assert
    assert_eq!(entry, Some("sh scripts/check-graph-artifact.sh"));
    assert_eq!(always_run, Some(Node::Bool(true)));
    assert!(
        !has_files_filter,
        "a files: filter skips a graph-only deletion"
    );
}
