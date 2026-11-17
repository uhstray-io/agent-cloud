//! The machine-path gate (scripts/check-machine-paths.sh) refuses a staged line that names a real
//! machine account's home directory (cli tier, security set). Its last case is a static assertion
//! in the same module: it reads the diff filters in the gate and in the pre-commit config.
//!
//! agent-cloud is a public template, so a line naming the operator's login or machine layout is
//! a leak (docs/MISTAKES.md 4.11: a path reached ledger entry 3.9 and was redacted afterwards).
//! Fixture paths that name a person are assembled with `concat!` in this file, so the gate does
//! not trip on the source it tests.
//!
//! Replaces platform/tests/test_machine_path_hook.bats (plan 24, Phase 4).
//!
//! Port map (every BATS @test, once):
//!
//! | BATS @test | Rust fn |
//! | --- | --- |
//! | machine-path hook: a macOS home directory is refused | `a_macos_home_directory_is_refused` |
//! | machine-path hook: a Linux home directory is refused | `a_linux_home_directory_is_refused` |
//! | machine-path hook: placeholders and service accounts pass | `placeholders_and_service_accounts_pass` |
//! | machine-path hook: an allowed path does not hide a personal one on the same line | `an_allowed_path_does_not_hide_a_personal_one_on_the_same_line` |
//! | machine-path hook: removed and context lines are not scanned | `removed_and_context_lines_are_not_scanned` |
//! | machine-path hook: a longer name that starts like an allowed one is refused | `a_longer_name_that_starts_like_an_allowed_one_is_refused` |
//! | machine-path hook: an added line that itself starts with ++ is still scanned | `an_added_line_that_starts_with_plus_plus_is_still_scanned` |
//! | machine-path hook: a renamed file's added lines are scanned | `a_renamed_files_added_lines_are_scanned` |
//! | staged-diff gates: every git diff --cached filter includes renamed files | `every_staged_diff_filter_includes_renamed_files` |

use std::path::Path;
use std::process::Output;

use agent_cloud_tests::process;
use agent_cloud_tests::repo;
use agent_cloud_tests::text;
use regex::Regex;
use tempfile::TempDir;

const HOOK: &str = "scripts/check-machine-paths.sh";

/// Runs the gate on a unified diff given as `lines`, through its stdin mode (`-`).
fn scan(lines: &[&str]) -> Output {
    let mut input = lines.join("\n");
    input.push('\n');
    let home = TempDir::new().expect("a temporary directory");
    let hook = repo::path(HOOK);
    process::scrubbed(hook.to_str().expect("a UTF-8 path"), home.path())
        .arg("-")
        .write_stdin(input)
        .output()
        .expect("the gate starts")
}

fn code(out: &Output) -> i32 {
    out.status.code().expect("the gate exits, it is not killed")
}

fn stdout(out: &Output) -> String {
    String::from_utf8_lossy(&out.stdout).into_owned()
}

/// `git` in `dir`, with an identity on the command line. Panics unless git succeeds.
fn git(dir: &Path, args: &[&str]) {
    let out = process::scrubbed("git", dir)
        .current_dir(dir)
        .args([
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
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

#[test]
fn a_macos_home_directory_is_refused() {
    // Arrange
    let lines = [
        "+++ b/docs/x.md",
        concat!("+  wrote over /", "Users", "/alice/.ssh/known_hosts"),
    ];

    // Act
    let out = scan(&lines);

    // Assert
    assert_eq!(code(&out), 1);
    assert!(
        stdout(&out).contains("alice"),
        "the leak is not named: {}",
        stdout(&out)
    );
}

#[test]
fn a_linux_home_directory_is_refused() {
    // Arrange
    let lines = [concat!("+  key: /", "home", "/bob/.ssh/id_ed25519")];

    // Act
    let out = scan(&lines);

    // Assert
    assert_eq!(code(&out), 1);
}

#[test]
fn placeholders_and_service_accounts_pass() {
    // Arrange: a template placeholder in each spelling, and the image users the gate allows.
    let lines = [
        "+  /home/<user>/.config",
        "+  /home/{{ ansible_user }}/x",
        "+  /home/${USER}/x",
        "+  /home/step/certs",
        "+  /home/frappe/bench",
        "+  /home/node/app",
    ];

    // Act
    let out = scan(&lines);

    // Assert
    assert_eq!(code(&out), 0, "{}", stdout(&out));
}

#[test]
fn an_allowed_path_does_not_hide_a_personal_one_on_the_same_line() {
    // Arrange
    let lines = [concat!(
        "+  cp /home/step/ca.crt /",
        "Users",
        "/carol/ca.crt"
    )];

    // Act
    let out = scan(&lines);

    // Assert
    assert_eq!(code(&out), 1);
}

#[test]
fn removed_and_context_lines_are_not_scanned() {
    // Arrange
    let lines = [
        concat!("-  /", "Users", "/dave/old"),
        concat!("   /", "Users", "/dave/context"),
    ];

    // Act
    let out = scan(&lines);

    // Assert
    assert_eq!(code(&out), 0, "{}", stdout(&out));
}

#[test]
fn a_longer_name_that_starts_like_an_allowed_one_is_refused() {
    // Arrange: `stepper` begins with the allowed `step`, but it is a different account.
    let lines = [concat!("+  /", "home", "/stepper/.ssh")];

    // Act
    let out = scan(&lines);

    // Assert
    assert_eq!(code(&out), 1);
}

#[test]
fn an_added_line_that_starts_with_plus_plus_is_still_scanned() {
    // Arrange
    let lines = ["+++ b/docs/x.md", concat!("+++ /", "Users", "/erin/notes")];

    // Act
    let out = scan(&lines);

    // Assert
    assert_eq!(code(&out), 1);
}

#[test]
fn a_renamed_files_added_lines_are_scanned() {
    // Arrange: a rename with enough unchanged lines that git reports R, not a delete and an add.
    let home = TempDir::new().expect("a temporary directory");
    let repo_dir = home.path().join("repo");
    std::fs::create_dir(&repo_dir).expect("the repository directory");
    git(&repo_dir, &["init", "-q"]);
    let body: String = (1..=40).map(|n| format!("{n}\n")).collect();
    std::fs::write(repo_dir.join("a.txt"), body).expect("a.txt is written");
    git(&repo_dir, &["add", "a.txt"]);
    git(&repo_dir, &["commit", "-qm", "init"]);
    git(&repo_dir, &["mv", "a.txt", "b.txt"]);
    let mut renamed = std::fs::read_to_string(repo_dir.join("b.txt")).expect("b.txt reads");
    renamed.push_str(concat!("/", "Users", "/frank/x\n"));
    std::fs::write(repo_dir.join("b.txt"), renamed).expect("b.txt is written");
    git(&repo_dir, &["add", "b.txt"]);

    // Act: the gate with no argument reads the staged diff, as the pre-commit hook runs it.
    let hook = repo::path(HOOK);
    let out = process::scrubbed(hook.to_str().expect("a UTF-8 path"), home.path())
        .current_dir(&repo_dir)
        .output()
        .expect("the gate starts");

    // Assert
    assert_eq!(code(&out), 1, "{}", stdout(&out));
}

#[test]
fn every_staged_diff_filter_includes_renamed_files() {
    // A --diff-filter without R skips a renamed file's content, so a leak moved in by a rename
    // passes the gate (review of #339). Both spellings git accepts are read: --diff-filter=ACMR
    // and --diff-filter ACMR.
    let filter = Regex::new(r"--diff-filter[= ][A-Za-z]+").expect("a valid pattern");
    let mut found = Vec::new();
    for rel in [".pre-commit-config.yaml", HOOK] {
        let body = text::read(rel);
        found.extend(filter.find_iter(&body).map(|m| m.as_str().to_owned()));
    }

    assert!(found.len() >= 4, "found only {found:?}");
    let without_rename: Vec<&String> = found.iter().filter(|spec| !spec.contains('R')).collect();
    assert!(
        without_rename.is_empty(),
        "a diff filter omits renamed files: {without_rename:?}"
    );
}
