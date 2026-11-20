//! `repo::playbook_files` on a temporary tree: its path-component order and its refusals (plan
//! 24, AC-958; cli tier: it starts this test binary again).
//!
//! The repository holds no pair whose string order differs from its component order, and no
//! symlink or `*.yml` directory under the roots, so tests over the real tree cannot see either.
//! Each test here builds `<tmp>/platform/{playbooks,semaphore}` and runs this test binary again
//! with only [`child_entry_point`] selected and `CARGO_MANIFEST_DIR` set to `<tmp>/platform/tests`,
//! which makes `repo::root()` the temporary tree. A name that is not UTF-8 has no fixture: APFS
//! refuses to create one.

use std::fs;
use std::os::unix::fs::symlink;
use std::path::Path;

use agent_cloud_tests::{process, repo};
use tempfile::TempDir;

const CHILD: &str = "SUPPORT_SCAN_FIXTURE_CHILD";

/// Not a test of its own: the entry point the tests below start as a child. Run by the suite, it
/// returns at once; run as a child, it prints the listing of the fixture tree, or panics with the
/// refusal.
#[test]
fn child_entry_point() {
    if std::env::var_os(CHILD).is_none() {
        return;
    }
    // libtest prints `test <name> ... ` with no newline before a test's own output, so the
    // listing starts on a line of its own.
    println!();
    for file in repo::playbook_files() {
        println!("FILE {file}");
    }
}

/// A fixture tree with both roots and `platform/tests`, the files given created empty.
fn tree(files: &[&str]) -> TempDir {
    let dir = TempDir::new().expect("a temporary directory");
    for sub in ["platform/tests", "platform/playbooks", "platform/semaphore"] {
        fs::create_dir_all(dir.path().join(sub)).expect("a fixture directory");
    }
    for file in files {
        let path = dir.path().join(file);
        fs::create_dir_all(path.parent().expect("a parent")).expect("a fixture directory");
        fs::write(path, "").expect("a fixture file");
    }
    dir
}

/// The child's listing, or its failure text when it refuses.
fn listing(root: &Path) -> Result<Vec<String>, String> {
    let home = TempDir::new().expect("a temporary directory");
    let mut cmd = process::scrubbed(
        std::env::current_exe()
            .expect("the test binary")
            .to_str()
            .expect("a UTF-8 path"),
        home.path(),
    );
    cmd.env(CHILD, "1")
        .env("CARGO_MANIFEST_DIR", root.join("platform/tests"))
        .args([
            "--exact",
            "support_scan_fixture::child_entry_point",
            "--nocapture",
            "--test-threads=1",
        ]);
    let out = cmd.output().expect("the test binary starts");
    let stdout = String::from_utf8_lossy(&out.stdout).into_owned();
    if out.status.success() {
        Ok(stdout
            .lines()
            .filter_map(|line| line.strip_prefix("FILE "))
            .map(str::to_owned)
            .collect())
    } else {
        Err(format!("{stdout}{}", String::from_utf8_lossy(&out.stderr)))
    }
}

#[test]
fn files_are_sorted_by_path_component_not_by_string() {
    // Arrange: as one string `tasks-x.yml` sorts before `tasks/a.yml` ('-' < '/'); by component
    // `tasks` comes before `tasks-x.yml`, as Python sorts `Path`s.
    let root = tree(&[
        "platform/playbooks/tasks-x.yml",
        "platform/playbooks/tasks/a.yml",
        "platform/playbooks/z.yaml",
        "platform/semaphore/s.yml",
    ]);

    // Act
    let files = listing(root.path()).expect("the listing");

    // Assert: component order, both roots, `.yaml` not listed.
    assert_eq!(
        files,
        [
            "platform/playbooks/tasks/a.yml",
            "platform/playbooks/tasks-x.yml",
            "platform/semaphore/s.yml",
        ]
    );
}

#[test]
fn a_symlink_under_the_roots_is_refused() {
    // Arrange: a symlinked directory, whose playbooks `rglob` would not list.
    let root = tree(&["outside/p.yml"]);
    symlink(
        root.path().join("outside"),
        root.path().join("platform/playbooks/linked"),
    )
    .expect("a symlink");

    // Act
    let refusal = listing(root.path()).expect_err("a refusal");

    // Assert
    assert!(
        refusal.contains("a symlink under the playbook roots"),
        "{refusal}"
    );
}

#[test]
fn a_directory_named_like_a_playbook_is_refused() {
    // Arrange
    let root = tree(&["platform/playbooks/d.yml/inner.yml"]);

    // Act
    let refusal = listing(root.path()).expect_err("a refusal");

    // Assert
    assert!(
        refusal.contains("a directory named like a playbook"),
        "{refusal}"
    );
}
