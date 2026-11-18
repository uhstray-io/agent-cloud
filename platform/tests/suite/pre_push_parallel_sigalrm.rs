//! BATS jobs under GNU parallel get SIGALRM at its default (cli tier, security set; AC-743).
//!
//! GNU parallel starts every job with SIGALRM ignored, and ansible-core then refuses a task
//! `timeout:`, so a playbook test passed alone and failed under `bats --jobs`. The pre-push hook
//! runs parallel's jobs through platform/tests/bin/parallel-job-shell (PARALLEL_SHELL), which
//! restores the default. These cases check the wrapper itself, and that the hook hands it to
//! bats and to nothing else: a fake `bats` records its arguments, one per line, and the
//! PARALLEL_SHELL it was given, and a fake `python3` records the PARALLEL_SHELL its pytest run
//! was given (the fixture has a `pyproject.toml` and no `uv.lock`, so the hook's uv branch is
//! skipped whatever uv the machine has).
//!
//! Replaces platform/tests/test_pre_push_parallel_sigalrm.bats (plan 24, Phase 4).
//!
//! Port map (every BATS @test, once):
//!
//! | BATS @test | Rust fn |
//! | --- | --- |
//! | parallel-job-shell: a job started with SIGALRM ignored runs with it at default | `a_job_started_with_sigalrm_ignored_runs_with_it_at_default` |
//! | pre-push parallel: bats gets CI's flags and the job shell | `bats_gets_ci_flags_and_the_job_shell` |
//! | pre-push parallel: a checkout without the job shell runs BATS serially, saying why | `a_checkout_without_the_job_shell_runs_bats_serially_saying_why` |
//! | pre-push parallel: the presence check reads the binary name bats uses (BATS_PARALLEL_BINARY_NAME) | `the_presence_check_reads_the_binary_name_bats_uses` |
//!
//! The wrapper case skips, with a message, when `perl` is not on PATH, as the BATS file did:
//! the job shell is a perl script, and a machine without perl has no GNU parallel to run it.

use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::process::Output;

use agent_cloud_tests::process;
use agent_cloud_tests::repo;
use tempfile::TempDir;

const HOOK: &str = ".githooks/pre-push";
const JOB_SHELL: &str = "platform/tests/bin/parallel-job-shell";

/// Exits 42 when SIGALRM is ignored in the process that runs it, 43 when it is at its default,
/// and 44 otherwise: distinct codes, so a wrapper that exits without running it is caught.
const SIGALRM_CHECK: &str = r#"python3 -c "import signal, sys; h = signal.getsignal(signal.SIGALRM); sys.exit(42 if h == signal.SIG_IGN else 43 if h == signal.SIG_DFL else 44)""#;

/// The pushed ref, as git writes it on the hook's stdin.
const REFLINE: &str = "refs/heads/feat 1111111111111111111111111111111111111111 refs/heads/feat 0000000000000000000000000000000000000000\n";

/// What bats sees: its arguments, one per line, and PARALLEL_SHELL, each written to a file the
/// test names.
const FAKE_BATS: &str = "#!/usr/bin/env sh
printf '%s\\n' \"$@\" > \"$FAKE_BATS_ARGS\"
echo \"${PARALLEL_SHELL:-unset}\" > \"$FAKE_BATS_SHELL\"
";

/// A `parallel` that is installed and does nothing: the hook asks for it by name.
const FAKE_PARALLEL: &str = "#!/bin/sh\nexit 0\n";

/// The hook's `python3`: pytest and xdist import (so no "runs serially" note for pytest), and `-m pytest` records the
/// PARALLEL_SHELL it was given and passes.
const FAKE_PYTHON3: &str = "#!/bin/sh
case \"$1 $2\" in
  '-c import pytest' | '-c import xdist') exit 0 ;;
  '-m pytest') echo \"${PARALLEL_SHELL:-unset}\" > \"$FAKE_PYTEST_SHELL\"; exit 0 ;;
esac
exit 1
";

/// A scratch repository with a fake `bats` and `parallel` on a PATH the hook searches first.
struct Fixture {
    home: TempDir,
    repo: PathBuf,
    fakebin: PathBuf,
    args_file: PathBuf,
    shell_file: PathBuf,
    pytest_shell_file: PathBuf,
}

impl Fixture {
    fn new() -> Self {
        let home = TempDir::new().expect("a temporary directory");
        let repo = home.path().join("repo");
        let fakebin = home.path().join("fakebin");
        std::fs::create_dir_all(repo.join("platform/tests/bin")).expect("the repository");
        std::fs::create_dir(&fakebin).expect("the fake PATH directory");
        let out = process::scrubbed("git", home.path())
            .args(["init", "-q"])
            .arg(&repo)
            .output()
            .expect("git starts");
        assert!(out.status.success(), "git init failed");
        write_executable(&fakebin.join("bats"), FAKE_BATS);
        write_executable(&fakebin.join("parallel"), FAKE_PARALLEL);
        write_executable(&fakebin.join("python3"), FAKE_PYTHON3);
        std::fs::write(repo.join("pyproject.toml"), "").expect("the pyproject.toml");
        Self {
            pytest_shell_file: home.path().join("pytest.shell"),
            args_file: home.path().join("bats.args"),
            shell_file: home.path().join("bats.shell"),
            home,
            repo,
            fakebin,
        }
    }

    /// Copies the job shell into the checkout, as a checkout that has it.
    fn with_job_shell(&self) {
        let source = repo::path(JOB_SHELL);
        let target = self.repo.join(JOB_SHELL);
        std::fs::copy(source, target).expect("the job shell is copied");
    }

    /// Runs the hook in the checkout for one pushed branch, with `extra` in its environment.
    fn push(&self, extra: &[(&str, &str)]) -> Output {
        // Joined as OsString: a non-UTF-8 entry in the inherited PATH must survive.
        let mut entries = vec![self.fakebin.clone()];
        if let Some(inherited) = std::env::var_os("PATH") {
            entries.extend(std::env::split_paths(&inherited));
        }
        let path: OsString = std::env::join_paths(entries).expect("PATH holds the fake directory");
        let hook = repo::path(HOOK);
        let mut cmd = process::scrubbed("sh", self.home.path());
        cmd.current_dir(&self.repo)
            .env("PATH", path)
            .env("FAKE_BATS_ARGS", &self.args_file)
            .env("FAKE_BATS_SHELL", &self.shell_file)
            .env("FAKE_PYTEST_SHELL", &self.pytest_shell_file);
        for (name, value) in extra {
            cmd.env(name, value);
        }
        cmd.arg(hook.to_str().expect("a UTF-8 path"))
            .arg("origin")
            .arg("https://example.invalid/repo.git")
            .write_stdin(REFLINE)
            .output()
            .expect("the hook starts")
    }

    fn bats_args(&self) -> String {
        std::fs::read_to_string(&self.args_file).expect("bats was run")
    }

    fn bats_shell(&self) -> String {
        std::fs::read_to_string(&self.shell_file).expect("bats was run")
    }

    fn pytest_shell(&self) -> String {
        std::fs::read_to_string(&self.pytest_shell_file).expect("pytest was run")
    }
}

fn write_executable(path: &Path, body: &str) {
    std::fs::write(path, body).expect("the fake is written");
    std::fs::set_permissions(path, executable()).expect("the mode");
}

fn executable() -> std::fs::Permissions {
    use std::os::unix::fs::PermissionsExt;
    std::fs::Permissions::from_mode(0o755)
}

/// Whether an executable `perl` is on PATH.
fn perl_on_path() -> bool {
    use std::os::unix::fs::PermissionsExt;
    std::env::var_os("PATH").is_some_and(|path| {
        std::env::split_paths(&path).any(|dir| {
            std::fs::metadata(dir.join("perl"))
                .is_ok_and(|m| m.is_file() && m.permissions().mode() & 0o111 != 0)
        })
    })
}

/// The exit status of the SIGALRM_CHECK job (42 ignored, 43 default), started by perl
/// with SIGALRM ignored (as GNU parallel starts it) through the shell `shell`.
fn job_status_under(home: &Path, shell: &str) -> i32 {
    let out = process::scrubbed("perl", home)
        .arg("-e")
        .arg(r#"$SIG{ALRM} = "IGNORE"; exec @ARGV"#)
        .arg(shell)
        .arg("-c")
        .arg(SIGALRM_CHECK)
        .output()
        .expect("perl starts: GNU parallel is a perl script");
    out.status.code().expect("the job exits, it is not killed")
}

#[test]
fn a_job_started_with_sigalrm_ignored_runs_with_it_at_default() {
    // Arrange
    if !perl_on_path() {
        eprintln!("SKIP: perl is not on PATH, so the job shell (a perl script) cannot run here");
        return;
    }
    let home = TempDir::new().expect("a temporary directory");
    let job_shell = repo::path(JOB_SHELL);
    let job_shell = job_shell.to_str().expect("a UTF-8 path");

    // Act: the control proves the setup really hands the job an ignored SIGALRM.
    let control = job_status_under(home.path(), "/bin/sh");
    let wrapped = job_status_under(home.path(), job_shell);

    // Assert
    assert_eq!(
        control, 42,
        "the control job did not report SIGALRM ignored: the setup is wrong"
    );
    assert_eq!(
        wrapped, 43,
        "the wrapper did not run the job with SIGALRM at default"
    );
}

#[test]
fn bats_gets_ci_flags_and_the_job_shell() {
    // Arrange
    let fixture = Fixture::new();
    fixture.with_job_shell();

    // Act
    let out = fixture.push(&[]);

    // Assert
    assert!(out.status.success(), "{}", process::output_text(&out));
    assert_eq!(
        fixture.bats_args(),
        "--jobs\n4\n--no-parallelize-within-files\nplatform/tests/\n"
    );
    // The hook works from `git rev-parse --show-toplevel`, the resolved path.
    let toplevel = process::scrubbed("git", fixture.home.path())
        .current_dir(&fixture.repo)
        .args(["rev-parse", "--show-toplevel"])
        .output()
        .expect("git starts");
    let toplevel = String::from_utf8_lossy(&toplevel.stdout).trim().to_owned();
    assert_eq!(
        fixture.bats_shell(),
        format!("{toplevel}/platform/tests/bin/parallel-job-shell\n")
    );
    // The hook's own output was captured: it announced the parallel run.
    assert!(
        process::output_text(&out).contains("pre-push: running BATS suite in parallel...\n"),
        "{}",
        process::output_text(&out)
    );
    assert!(
        !process::output_text(&out).contains("runs serially"),
        "{}",
        process::output_text(&out)
    );
    // PARALLEL_SHELL reached bats only: the pytest run beside it got none.
    assert_eq!(fixture.pytest_shell(), "unset\n");
}

#[test]
fn a_checkout_without_the_job_shell_runs_bats_serially_saying_why() {
    // Arrange
    let fixture = Fixture::new();

    // Act
    let out = fixture.push(&[]);

    // Assert
    assert!(out.status.success(), "{}", process::output_text(&out));
    assert_eq!(fixture.bats_args(), "platform/tests/\n");
    assert_eq!(fixture.bats_shell(), "unset\n");
    assert!(
        process::output_text(&out).contains("parallel-job-shell is missing from this checkout"),
        "{}",
        process::output_text(&out)
    );
}

#[test]
fn the_presence_check_reads_the_binary_name_bats_uses() {
    // Arrange: a checkout with the job shell, but bats looks for a parallel binary that is absent.
    let fixture = Fixture::new();
    fixture.with_job_shell();

    // Act
    let out = fixture.push(&[("BATS_PARALLEL_BINARY_NAME", "no-such-parallel")]);

    // Assert
    assert!(out.status.success(), "{}", process::output_text(&out));
    assert_eq!(fixture.bats_args(), "platform/tests/\n");
    assert!(
        process::output_text(&out).contains("GNU parallel is not installed"),
        "{}",
        process::output_text(&out)
    );
}
