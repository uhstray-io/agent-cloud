//! The pre-push hook runs one test run per branch at a time (cli tier, security set).
//!
//! On 2026-09-24 a backgrounded push's buffered output was read as finished, and a second push
//! of the same branch started while the first was still inside the suites (docs/MISTAKES.md
//! §5.11). The hook takes a per-branch lock under the shared git dir and refuses a second push
//! of the same branch while the first holds it. These cases drive the real hook in a scratch
//! repository, with a fake `bats` that is held open on demand, so "the first push is still
//! running" is a fact the test controls rather than a timing guess.
//!
//! A held push runs on a thread through `process::scrubbed`, bounded by its 30 s timeout. The
//! fake `bats` releases on a file the test writes. `Holding` is built before the wait for the
//! suite to start, so it writes that file and joins the thread on drop even when the wait fails;
//! a failing case never leaves a hook running.
//!
//! Replaces platform/tests/test_pre_push_branch_lock.bats (plan 24, Phase 4).
//!
//! Port map (every BATS @test, once):
//!
//! | BATS @test | Rust fn |
//! | --- | --- |
//! | pre-push lock: a second push of the same branch is refused while the first runs | `a_second_push_of_the_same_branch_is_refused_while_the_first_runs` |
//! | pre-push lock: a push of a different branch is not blocked | `a_push_of_a_different_branch_is_not_blocked` |
//! | pre-push lock: a refused multi-branch push releases the locks it already took | `a_refused_multi_branch_push_releases_the_locks_it_took` |
//! | pre-push lock: a stale lock whose pid is gone is reclaimed | `a_stale_lock_whose_pid_is_gone_is_reclaimed` |
//! | pre-push lock: released after the suites pass | `released_after_the_suites_pass` |
//! | pre-push lock: released after a suite fails | `released_after_a_suite_fails` |
//! | pre-push lock: SKIP_TESTS still takes the lock and releases it | `skip_tests_still_takes_the_lock_and_releases_it` |
//! | pre-push lock: released when the push is interrupted (TERM) | `released_when_the_push_is_interrupted_by_term` |
//! | pre-push lock: branches whose names sanitize alike do not share a lock | `branches_whose_names_sanitize_alike_do_not_share_a_lock` |
//! | pre-push lock: the printed recovery command works for a path with spaces and quotes | `the_printed_recovery_command_works_for_a_path_with_spaces_and_quotes` |
//! | pre-push lock: a link failure with no lock present warns and proceeds | `a_link_failure_with_no_lock_present_warns_and_proceeds` |

use std::path::{Path, PathBuf};
use std::process::Output;
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use agent_cloud_tests::process;
use agent_cloud_tests::repo;
use tempfile::TempDir;

const HOOK: &str = ".githooks/pre-push";
const URL: &str = "https://example.invalid/repo.git";

/// Fake `bats`: records that the suite started, then, when FAKE_BATS_HOLD names a file, stays
/// "running" until that file exists (bounded, so a broken test cannot hang).
const FAKE_BATS: &str = r#"#!/usr/bin/env sh
[ -n "${FAKE_BATS_STARTED:-}" ] && : > "$FAKE_BATS_STARTED"
if [ -n "${FAKE_BATS_HOLD:-}" ]; then
  i=0
  while [ ! -e "$FAKE_BATS_HOLD" ] && [ "$i" -lt 300 ]; do sleep 0.1; i=$((i + 1)); done
fi
exit "${FAKE_BATS_RC:-0}"
"#;

/// A `ln` that always fails, so the lock cannot be linked.
const FAILING_LN: &str = "#!/usr/bin/env sh\nexit 1\n";

/// A scratch repository, the hook's PATH with a fake `bats`, and the temporary home.
struct Ctx {
    home: TempDir,
    repo: PathBuf,
    fakebin: PathBuf,
}

impl Ctx {
    fn new() -> Self {
        let home = TempDir::new().expect("a temporary directory");
        let repo = home.path().join("repo");
        let fakebin = home.path().join("fakebin");
        std::fs::create_dir(&fakebin).expect("the fake PATH directory");
        write_executable(&fakebin.join("bats"), FAKE_BATS);
        let ctx = Self {
            home,
            repo,
            fakebin,
        };
        ctx.init_repo();
        ctx
    }

    /// A fresh repository at `self.repo`, with `platform/tests` so the hook reaches its BATS step.
    fn init_repo(&self) {
        std::fs::create_dir_all(self.repo.join("platform/tests")).expect("the repository");
        let out = process::scrubbed("git", self.home.path())
            .args(["init", "-q"])
            .arg(&self.repo)
            .output()
            .expect("git starts");
        assert!(out.status.success(), "git init failed");
    }

    fn path(&self) -> String {
        format!(
            "{}:{}",
            self.fakebin.display(),
            std::env::var("PATH").unwrap_or_default()
        )
    }

    fn lock_dir(&self, repo: &Path) -> PathBuf {
        repo.join(".git/pre-push-locks")
    }

    /// Runs the hook in `repo` for each branch, one stdin line per branch, in the foreground.
    fn push(&self, repo: &Path, envs: &[(&str, &str)], branches: &[&str]) -> Output {
        let hook = repo::path(HOOK);
        let mut cmd = process::scrubbed("sh", self.home.path());
        cmd.current_dir(repo).env("PATH", self.path());
        for (name, value) in envs {
            cmd.env(name, value);
        }
        cmd.arg(hook.to_str().expect("a UTF-8 path"))
            .arg("origin")
            .arg(URL)
            .write_stdin(branches.iter().map(|b| refline(b)).collect::<String>())
            .output()
            .expect("the hook starts")
    }

    /// A foreground push of one branch in the scratch repository.
    fn push_default(&self, envs: &[(&str, &str)], branch: &str) -> Output {
        self.push(&self.repo, envs, &[branch])
    }

    /// Starts a push of `branch` that stays inside its suite until `release` exists.
    fn start_holding(&self, repo: &Path, branch: &str) -> Holding {
        let started = self.home.path().join("started");
        let release = self.home.path().join("release");
        let _ = std::fs::remove_file(&started);
        let _ = std::fs::remove_file(&release);
        let hook = repo::path(HOOK);
        let mut cmd = process::scrubbed("sh", self.home.path());
        cmd.current_dir(repo)
            .env("PATH", self.path())
            .env("FAKE_BATS_STARTED", &started)
            .env("FAKE_BATS_HOLD", &release)
            .arg(hook.to_str().expect("a UTF-8 path"))
            .arg("origin")
            .arg(URL)
            .write_stdin(refline(branch));
        let thread = thread::spawn(move || cmd.output().expect("the held push starts"));
        // Built before the wait, so a failed wait below still releases and joins the push.
        let holding = Holding {
            release,
            thread: Some(thread),
        };
        // 20 s, not 10: under heavy machine load (load average above 30) the hook took more than
        // 10 s to reach BATS. The scrubbed runner's 30 s bound still caps the held hook.
        let deadline = Instant::now() + Duration::from_secs(20);
        while !started.exists() {
            assert!(
                Instant::now() < deadline,
                "the first push never reached its suite"
            );
            thread::sleep(Duration::from_millis(100));
        }
        holding
    }

    /// The lock file the hook uses for `branch` in `repo`: the readable name, then the checksum
    /// of the exact ref, as the hook computes it with `cksum`.
    fn lockfile(&self, repo: &Path, branch: &str) -> PathBuf {
        let sum = cksum(self.home.path(), &format!("refs/heads/{branch}"));
        self.lock_dir(repo)
            .join(format!("{}-{sum}.lock", sanitize(branch)))
    }

    /// The owner pid the hook writes on the first line of a lock.
    fn holder_pid(&self, repo: &Path, branch: &str) -> String {
        let text = std::fs::read_to_string(self.lockfile(repo, branch)).expect("the lock exists");
        text.lines()
            .next()
            .expect("the lock names its owner")
            .to_owned()
    }

    /// No lock file may remain once the hook has exited.
    fn assert_no_locks(&self, repo: &Path) {
        let dir = self.lock_dir(repo);
        let left: Vec<String> = match std::fs::read_dir(&dir) {
            Ok(entries) => entries
                .map(|e| e.expect("a lock entry").path().display().to_string())
                .collect(),
            Err(_) => Vec::new(),
        };
        assert!(left.is_empty(), "left behind: {left:?}");
    }
}

/// A push held inside its suite. `finish` releases it and returns its output; dropping it
/// without finishing releases and joins it too.
struct Holding {
    release: PathBuf,
    thread: Option<JoinHandle<Output>>,
}

impl Holding {
    fn finish(&mut self) -> Output {
        std::fs::write(&self.release, "").expect("the release file is written");
        self.thread
            .take()
            .expect("the push is finished once")
            .join()
            .expect("the held push thread did not panic")
    }
}

impl Drop for Holding {
    fn drop(&mut self) {
        if let Some(thread) = self.thread.take() {
            let _ = std::fs::write(&self.release, "");
            let _ = thread.join();
        }
    }
}

fn write_executable(path: &Path, body: &str) {
    use std::os::unix::fs::PermissionsExt;
    std::fs::write(path, body).expect("the fake is written");
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o755)).expect("the mode");
}

/// One push line per ref, as git writes them on the hook's stdin.
fn refline(branch: &str) -> String {
    format!(
        "refs/heads/{branch} 1111111111111111111111111111111111111111 refs/heads/{branch} 0000000000000000000000000000000000000000\n"
    )
}

/// The readable part of a lock name: the characters the hook keeps, others as `_`.
fn sanitize(branch: &str) -> String {
    branch
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-') {
                c
            } else {
                '_'
            }
        })
        .collect()
}

/// `cksum` of `text`, the first field, as the hook's `cksum | awk` takes it.
fn cksum(home: &Path, text: &str) -> String {
    let out = process::scrubbed("cksum", home)
        .write_stdin(text)
        .output()
        .expect("cksum starts");
    String::from_utf8_lossy(&out.stdout)
        .split_whitespace()
        .next()
        .expect("cksum prints a checksum")
        .to_owned()
}

fn text(out: &Output) -> String {
    format!(
        "{}{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    )
}

fn code(out: &Output) -> i32 {
    out.status.code().expect("the hook exits, it is not killed")
}

/// Whether a process with `pid` exists, by the shell's own `kill -0`.
fn alive(home: &Path, pid: &str) -> bool {
    process::scrubbed("sh", home)
        .arg("-c")
        .arg("kill -0 \"$1\" 2>/dev/null")
        .arg("sh")
        .arg(pid)
        .output()
        .expect("sh starts")
        .status
        .success()
}

fn signal_term(home: &Path, pid: &str) {
    let out = process::scrubbed("sh", home)
        .arg("-c")
        .arg("kill -TERM \"$1\"")
        .arg("sh")
        .arg(pid)
        .output()
        .expect("sh starts");
    assert!(out.status.success(), "the hook could not be signalled");
}

/// A pid that is no longer alive: the pid of a shell that has exited.
fn dead_pid(home: &Path) -> String {
    for _ in 0..5 {
        let out = process::scrubbed("sh", home)
            .arg("-c")
            .arg("echo $$")
            .output()
            .expect("sh starts");
        let pid = String::from_utf8_lossy(&out.stdout).trim().to_owned();
        if !alive(home, &pid) {
            return pid;
        }
    }
    panic!("five pids in a row were still alive");
}

#[test]
fn a_second_push_of_the_same_branch_is_refused_while_the_first_runs() {
    // Arrange
    let ctx = Ctx::new();
    let mut first = ctx.start_holding(&ctx.repo, "feat");

    // Act
    let second = ctx.push_default(&[], "feat");
    let holder = ctx.holder_pid(&ctx.repo, "feat");
    let first_out = first.finish();

    // Assert
    assert_ne!(
        code(&second),
        0,
        "the second push was allowed: {}",
        text(&second)
    );
    let shown = text(&second);
    assert!(shown.contains("PUSH REFUSED"), "{shown}");
    assert!(shown.contains("refs/heads/feat"), "{shown}");
    assert!(
        shown.contains(&format!("pid {holder}")),
        "the refusal does not name the holder: {shown}"
    );
    assert_eq!(
        code(&first_out),
        0,
        "the first push failed: {}",
        text(&first_out)
    );
    ctx.assert_no_locks(&ctx.repo);
}

#[test]
fn a_push_of_a_different_branch_is_not_blocked() {
    // Arrange
    let ctx = Ctx::new();
    let mut first = ctx.start_holding(&ctx.repo, "feat");

    // Act
    let other = ctx.push_default(&[], "other");
    let still_held = ctx.lockfile(&ctx.repo, "feat").is_file();
    let other_lock = ctx.lockfile(&ctx.repo, "other").exists();
    first.finish();

    // Assert
    assert_eq!(
        code(&other),
        0,
        "the other branch was blocked: {}",
        text(&other)
    );
    assert!(!text(&other).contains("PUSH REFUSED"));
    assert!(still_held, "the first push's lock was touched");
    assert!(!other_lock, "the second push left a lock of its own");
}

#[test]
fn a_refused_multi_branch_push_releases_the_locks_it_took() {
    // Arrange
    let ctx = Ctx::new();
    let mut first = ctx.start_holding(&ctx.repo, "feat");

    // Act: `aaa` is taken first, then `feat` is refused, and the refusal must release `aaa`.
    let refused = ctx.push(&ctx.repo, &[], &["aaa", "feat"]);
    let aaa_lock = ctx.lockfile(&ctx.repo, "aaa").exists();
    let feat_lock = ctx.lockfile(&ctx.repo, "feat").is_file();
    first.finish();

    // Assert
    assert_ne!(
        code(&refused),
        0,
        "the push holding feat was allowed: {}",
        text(&refused)
    );
    assert!(!aaa_lock, "the aaa lock leaked from a refused push");
    assert!(feat_lock, "the first push's lock is gone");
}

#[test]
fn a_stale_lock_whose_pid_is_gone_is_reclaimed() {
    // Arrange
    let ctx = Ctx::new();
    let dead = dead_pid(ctx.home.path());
    std::fs::create_dir_all(ctx.lock_dir(&ctx.repo)).expect("the lock directory");
    std::fs::write(
        ctx.lockfile(&ctx.repo, "feat"),
        format!("{dead}\nref=refs/heads/feat remote=origin started=then\n"),
    )
    .expect("the stale lock is written");

    // Act
    let out = ctx.push_default(&[], "feat");

    // Assert
    assert_eq!(
        code(&out),
        0,
        "the stale lock was not reclaimed: {}",
        text(&out)
    );
    assert!(
        text(&out).contains("reclaimed a stale lock"),
        "{}",
        text(&out)
    );
    ctx.assert_no_locks(&ctx.repo);
}

#[test]
fn released_after_the_suites_pass() {
    // Arrange
    let ctx = Ctx::new();

    // Act
    let out = ctx.push_default(&[], "feat");

    // Assert
    assert_eq!(code(&out), 0, "{}", text(&out));
    ctx.assert_no_locks(&ctx.repo);
}

#[test]
fn released_after_a_suite_fails() {
    // Arrange
    let ctx = Ctx::new();

    // Act
    let out = ctx.push_default(&[("FAKE_BATS_RC", "1")], "feat");

    // Assert
    assert_eq!(code(&out), 1, "expected a blocked push: {}", text(&out));
    assert!(text(&out).contains("PUSH BLOCKED"), "{}", text(&out));
    ctx.assert_no_locks(&ctx.repo);
}

#[test]
fn skip_tests_still_takes_the_lock_and_releases_it() {
    // Arrange
    let ctx = Ctx::new();

    // Act: a skipped push takes the lock, releases it, and says the suites did not run.
    let skipped = ctx.push_default(&[("SKIP_TESTS", "1")], "feat");

    // Assert
    assert_eq!(code(&skipped), 0, "{}", text(&skipped));
    assert!(
        text(&skipped).contains("TESTS SKIPPED"),
        "{}",
        text(&skipped)
    );
    ctx.assert_no_locks(&ctx.repo);

    // And skipping the tests does not skip the lock: a skipped push is refused while one runs.
    let mut first = ctx.start_holding(&ctx.repo, "feat");
    let refused = ctx.push_default(&[("SKIP_TESTS", "1")], "feat");
    first.finish();
    assert_ne!(
        code(&refused),
        0,
        "SKIP_TESTS bypassed the lock: {}",
        text(&refused)
    );
    assert!(
        text(&refused).contains("PUSH REFUSED"),
        "{}",
        text(&refused)
    );
}

#[test]
fn released_when_the_push_is_interrupted_by_term() {
    // Arrange
    let ctx = Ctx::new();
    let mut first = ctx.start_holding(&ctx.repo, "feat");
    let hook_pid = ctx.holder_pid(&ctx.repo, "feat");

    // Act: TERM to the hook, then the release lets the suite end so the shell runs its trap.
    signal_term(ctx.home.path(), &hook_pid);
    first.finish();

    // Assert
    ctx.assert_no_locks(&ctx.repo);
}

#[test]
fn branches_whose_names_sanitize_alike_do_not_share_a_lock() {
    // Arrange: `feature/a` and `feature_a` both read as `feature_a`, but the checksum tells them apart.
    let ctx = Ctx::new();
    let mut first = ctx.start_holding(&ctx.repo, "feature/a");

    // Act
    let other = ctx.push_default(&[], "feature_a");
    let held = ctx.lockfile(&ctx.repo, "feature/a").is_file();
    first.finish();

    // Assert
    assert_eq!(
        code(&other),
        0,
        "feature_a was blocked by feature/a: {}",
        text(&other)
    );
    assert!(!text(&other).contains("PUSH REFUSED"));
    assert!(held, "the feature/a lock is gone");
}

#[test]
fn the_printed_recovery_command_works_for_a_path_with_spaces_and_quotes() {
    // Arrange: a repository whose path holds spaces and an apostrophe.
    let ctx = Ctx::new();
    let repo = ctx.home.path().join("it's my repo");
    std::fs::create_dir(&repo).expect("the repository directory");
    let init = process::scrubbed("git", ctx.home.path())
        .args(["init", "-q"])
        .arg(&repo)
        .output()
        .expect("git starts");
    assert!(init.status.success(), "git init failed");
    std::fs::create_dir_all(repo.join("platform/tests")).expect("the tests directory");
    let mut first = ctx.start_holding(&repo, "feat");

    // Act: the refusal prints a command; run exactly what the operator would paste.
    let refused = ctx.push(&repo, &[], &["feat"]);
    let shown = text(&refused);
    let command = shown.lines().find_map(|line| {
        line.split_once("remove the lock:  ")
            .map(|(_, rest)| rest.to_owned())
    });
    let command = command.expect("no recovery command was printed");
    let recovered = process::scrubbed("sh", ctx.home.path())
        .arg("-c")
        .arg(&command)
        .output()
        .expect("sh starts");
    let lock_gone = !ctx.lockfile(&repo, "feat").exists();
    first.finish();

    // Assert
    assert_ne!(code(&refused), 0, "the second push was allowed: {shown}");
    assert!(
        recovered.status.success(),
        "the printed command failed: {command}"
    );
    assert!(
        lock_gone,
        "the printed command did not remove the lock: {command}"
    );
}

#[test]
fn a_link_failure_with_no_lock_present_warns_and_proceeds() {
    // Arrange: no hard links on this filesystem, as far as the hook can tell.
    let ctx = Ctx::new();
    write_executable(&ctx.fakebin.join("ln"), FAILING_LN);

    // Act
    let out = ctx.push_default(&[], "feat");

    // Assert
    assert_eq!(
        code(&out),
        0,
        "an infrastructure failure refused the push: {}",
        text(&out)
    );
    assert!(
        text(&out).contains("Continuing WITHOUT it"),
        "{}",
        text(&out)
    );
    assert!(!text(&out).contains("PUSH REFUSED"), "{}", text(&out));
    ctx.assert_no_locks(&ctx.repo);
}
