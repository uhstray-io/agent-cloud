//! BATS jobs under GNU parallel get SIGALRM at its default (cli tier, security set; AC-743).
//!
//! GNU parallel starts every job with SIGALRM ignored, and ansible-core then refuses a task
//! `timeout:`, so a playbook test passed alone and failed under `bats --jobs`. The pre-push hook
//! runs parallel's jobs through platform/tests/bin/parallel-job-shell (PARALLEL_SHELL), which
//! restores the default. These cases check the wrapper itself, and that the hook hands it to
//! bats and to nothing else. A fake `bats` records its argv and the PARALLEL_SHELL it was given.
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
//! `perl` is a precondition, not a skip: GNU parallel is itself a perl script, so a machine
//! that runs the job shell has perl (the BATS file skipped without it).

use std::path::{Path, PathBuf};
use std::process::Output;

use agent_cloud_tests::process;
use agent_cloud_tests::repo;
use tempfile::TempDir;

const HOOK: &str = ".githooks/pre-push";
const JOB_SHELL: &str = "platform/tests/bin/parallel-job-shell";

/// Exits 0 only when SIGALRM is at its default disposition in the process that runs it.
const SIGALRM_CHECK: &str = r#"python3 -c "import signal, sys; sys.exit(signal.getsignal(signal.SIGALRM) != signal.SIG_DFL)""#;

/// The pushed ref, as git writes it on the hook's stdin.
const REFLINE: &str = "refs/heads/feat 1111111111111111111111111111111111111111 refs/heads/feat 0000000000000000000000000000000000000000\n";

/// What bats sees: its argv and PARALLEL_SHELL, each written to a file the test names.
const FAKE_BATS: &str = "#!/usr/bin/env sh
echo \"$*\" > \"$FAKE_BATS_ARGS\"
echo \"${PARALLEL_SHELL:-unset}\" > \"$FAKE_BATS_SHELL\"
";

/// A `parallel` that is installed and does nothing: the hook asks for it by name.
const FAKE_PARALLEL: &str = "#!/bin/sh\nexit 0\n";

/// A scratch repository with a fake `bats` and `parallel` on a PATH the hook searches first.
struct Fixture {
    home: TempDir,
    repo: PathBuf,
    fakebin: PathBuf,
    args_file: PathBuf,
    shell_file: PathBuf,
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
        Self {
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
        let path = format!(
            "{}:{}",
            self.fakebin.display(),
            std::env::var("PATH").unwrap_or_default()
        );
        let hook = repo::path(HOOK);
        let mut cmd = process::scrubbed("sh", self.home.path());
        cmd.current_dir(&self.repo)
            .env("PATH", path)
            .env("FAKE_BATS_ARGS", &self.args_file)
            .env("FAKE_BATS_SHELL", &self.shell_file);
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
}

fn write_executable(path: &Path, body: &str) {
    std::fs::write(path, body).expect("the fake is written");
    std::fs::set_permissions(path, executable()).expect("the mode");
}

fn executable() -> std::fs::Permissions {
    use std::os::unix::fs::PermissionsExt;
    std::fs::Permissions::from_mode(0o755)
}

fn text(out: &Output) -> String {
    format!(
        "{}{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    )
}

/// The exit status of a job that exits 0 only when SIGALRM is at its default, started by perl
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
    let home = TempDir::new().expect("a temporary directory");
    let job_shell = repo::path(JOB_SHELL);
    let job_shell = job_shell.to_str().expect("a UTF-8 path");

    // Act: the control proves the setup really hands the job an ignored SIGALRM.
    let control = job_status_under(home.path(), "/bin/sh");
    let wrapped = job_status_under(home.path(), job_shell);

    // Assert
    assert_eq!(
        control, 1,
        "the control job saw SIGALRM at default: the setup is wrong"
    );
    assert_eq!(wrapped, 0, "the wrapper left SIGALRM ignored");
}

#[test]
fn bats_gets_ci_flags_and_the_job_shell() {
    // Arrange
    let fixture = Fixture::new();
    fixture.with_job_shell();

    // Act
    let out = fixture.push(&[]);

    // Assert
    assert!(out.status.success(), "{}", text(&out));
    assert_eq!(
        fixture.bats_args(),
        "--jobs 4 --no-parallelize-within-files platform/tests/\n"
    );
    // The hook works from `git rev-parse --show-toplevel`, the resolved path.
    let toplevel = process::scrubbed("git", fixture.home.path())
        .current_dir(&fixture.repo)
        .args(["rev-parse", "--show-toplevel"])
        .output()
        .expect("git starts");
    let toplevel = String::from_utf8_lossy(&toplevel.stdout).trim().to_owned();
    assert_eq!(
        fixture.bats_shell().trim_end(),
        format!("{toplevel}/platform/tests/bin/parallel-job-shell")
    );
    assert!(!text(&out).contains("runs serially"), "{}", text(&out));
}

#[test]
fn a_checkout_without_the_job_shell_runs_bats_serially_saying_why() {
    // Arrange
    let fixture = Fixture::new();

    // Act
    let out = fixture.push(&[]);

    // Assert
    assert!(out.status.success(), "{}", text(&out));
    assert_eq!(fixture.bats_args(), "platform/tests/\n");
    assert_eq!(fixture.bats_shell().trim_end(), "unset");
    assert!(
        text(&out).contains("parallel-job-shell is missing from this checkout"),
        "{}",
        text(&out)
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
    assert!(out.status.success(), "{}", text(&out));
    assert_eq!(fixture.bats_args(), "platform/tests/\n");
    assert!(
        text(&out).contains("GNU parallel is not installed"),
        "{}",
        text(&out)
    );
}
