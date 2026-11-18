//! The pre-push hook runs one test run per branch at a time (cli tier, security set).
//!
//! On 2026-09-24 a backgrounded push's buffered output was read as finished, and a second push
//! of the same branch started while the first was still inside the suites (docs/MISTAKES.md
//! §5.11). The hook takes a per-branch lock under the shared git dir and refuses a second push
//! of the same branch while the first holds it. These cases drive the real hook in a scratch
//! repository, with a fake `bats` that is held open on demand, so "the first push is still
//! running" is a fact the test controls rather than a timing guess.
//!
//! A held push runs on a thread through `process::scrubbed`, its bound raised to 60 s. The
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

use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::process::Output;
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use agent_cloud_tests::process;
use agent_cloud_tests::repo;
use assert_cmd::Command;
use tempfile::TempDir;

const HOOK: &str = ".githooks/pre-push";
const URL: &str = "https://example.invalid/repo.git";

/// Fake `bats`: records that the suite started, and, when FAKE_BATS_LOCKS names a file, the
/// entries of FAKE_BATS_LOCK_DIR while the suite runs; when FAKE_BATS_OWNER names a file, it
/// records its parent's pid (bats is a direct child of the hook, so that is the hook) and the
/// owner line of the lock FAKE_BATS_OWNER_LOCK, one per line; then, when FAKE_BATS_HOLD names a
/// file, it stays "running" until that file exists (bounded, so a broken test cannot hang). It
/// exits FAKE_BATS_RC (default 0).
const FAKE_BATS: &str = r#"#!/usr/bin/env sh
[ -n "${FAKE_BATS_STARTED:-}" ] && : > "$FAKE_BATS_STARTED"
[ -n "${FAKE_BATS_LOCKS:-}" ] && ls -A "$FAKE_BATS_LOCK_DIR" > "$FAKE_BATS_LOCKS"
[ -n "${FAKE_BATS_OWNER:-}" ] && { printf '%s\n' "$PPID"; sed -n 1p "$FAKE_BATS_OWNER_LOCK"; } > "$FAKE_BATS_OWNER"
if [ -n "${FAKE_BATS_HOLD:-}" ]; then
  i=0
  while [ ! -e "$FAKE_BATS_HOLD" ] && [ "$i" -lt 300 ]; do sleep 0.1; i=$((i + 1)); done
fi
exit "${FAKE_BATS_RC:-0}"
"#;

/// A `ln` that always fails, so the lock cannot be linked.
const FAILING_LN: &str = "#!/usr/bin/env sh\nexit 1\n";

/// Fake `ps`: the hook also calls `ps` in its cleanup once BATS has started; the test that installs
/// this one is refused before BATS starts, so its only call is the refusal's, made before any lock
/// is released. It records the entries of FAKE_PS_LOCK_DIR at that moment into FAKE_PS_LOCKS
/// (truncating, so a second call would overwrite the first). It prints nothing, so the refusal
/// carries no `Process:` line.
const RECORDING_PS: &str = r#"#!/usr/bin/env sh
[ -n "${FAKE_PS_LOCKS:-}" ] && ls -A "$FAKE_PS_LOCK_DIR" > "$FAKE_PS_LOCKS"
exit 1
"#;

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
        ctx.init_repo(&ctx.repo);
        ctx
    }

    /// A fresh repository at `repo`, with `platform/tests` so the hook reaches its BATS step.
    fn init_repo(&self, repo: &Path) {
        std::fs::create_dir_all(repo.join("platform/tests")).expect("the repository");
        let out = process::scrubbed("git", self.home.path())
            .args(["init", "-q"])
            .arg(repo)
            .output()
            .expect("git starts");
        assert!(out.status.success(), "git init failed");
    }

    /// The fake directory, then the inherited PATH, joined as OsString: a non-UTF-8 entry in
    /// the inherited PATH must survive.
    fn path(&self) -> OsString {
        let mut entries = vec![self.fakebin.clone()];
        if let Some(inherited) = std::env::var_os("PATH") {
            entries.extend(std::env::split_paths(&inherited));
        }
        std::env::join_paths(entries).expect("PATH holds the fake directory")
    }

    fn lock_dir(&self, repo: &Path) -> PathBuf {
        repo.join(".git/pre-push-locks")
    }

    /// The hook in `repo` for each branch, one stdin line per branch, with `envs`. Not yet run.
    fn hook_command(&self, repo: &Path, envs: &[(&str, &str)], branches: &[&str]) -> Command {
        let hook = repo::path(HOOK);
        let mut cmd = process::scrubbed("sh", self.home.path());
        cmd.current_dir(repo)
            .env("PATH", self.path())
            .envs(envs.iter().copied())
            .arg(hook.to_str().expect("a UTF-8 path"))
            .arg("origin")
            .arg(URL)
            .write_stdin(branches.iter().map(|b| refline(b)).collect::<String>());
        cmd
    }

    /// Runs the hook in `repo` for each branch, one stdin line per branch, in the foreground.
    fn push(&self, repo: &Path, envs: &[(&str, &str)], branches: &[&str]) -> Output {
        self.hook_command(repo, envs, branches)
            .output()
            .expect("the hook starts")
    }

    /// A foreground push of one branch in the scratch repository.
    fn push_default(&self, envs: &[(&str, &str)], branch: &str) -> Output {
        self.push(&self.repo, envs, &[branch])
    }

    /// Starts a push of `branch` that stays inside its suite until `release` exists, and returns
    /// once the suite has started (20 s at most, then it panics).
    fn start_holding(&self, repo: &Path, branch: &str) -> Holding {
        let started = self.home.path().join("started");
        let release = self.home.path().join("release");
        let _ = std::fs::remove_file(&started);
        let _ = std::fs::remove_file(&release);
        let envs = [
            ("FAKE_BATS_STARTED", started.to_str().expect("a UTF-8 path")),
            ("FAKE_BATS_HOLD", release.to_str().expect("a UTF-8 path")),
        ];
        let mut cmd = self.hook_command(repo, &envs, &[branch]);
        // Above the 20 s readiness wait plus the contender's own run: a holder killed at
        // the scrubbed 30 s default runs no EXIT trap, so its lock stays with a dead owner
        // and the contender reclaims it instead of being refused.
        cmd.timeout(Duration::from_secs(60));
        let thread = thread::spawn(move || cmd.output().expect("the held push starts"));
        // Built before the wait, so a failed wait below still releases and joins the push.
        let holding = Holding {
            release,
            thread: Some(thread),
        };
        // The bound is 20 s, as in pre_push_uv.rs's held_push, where a 10 s bound timed out in four
        // of six full-module runs on this machine (cause not identified). The held hook's own
        // 60 s bound leaves at least 40 s after the marker for the contender.
        let deadline = Instant::now() + Duration::from_secs(20);
        while !started.exists() {
            assert!(
                Instant::now() < deadline,
                "the first push never reached its suite"
            );
            thread::sleep(Duration::from_millis(10));
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

    /// The entries of the lock directory while the suite of a push of `branch` runs, read by
    /// the fake `bats`, and the push's output.
    fn push_recording_locks(&self, envs: &[(&str, &str)], branch: &str) -> (Output, String) {
        let seen = self.home.path().join("locks-seen");
        let dir = self.lock_dir(&self.repo);
        let mut all: Vec<(&str, &str)> = envs.to_vec();
        all.push(("FAKE_BATS_LOCKS", seen.to_str().expect("a UTF-8 path")));
        all.push(("FAKE_BATS_LOCK_DIR", dir.to_str().expect("a UTF-8 path")));
        let out = self.push(&self.repo, &all, &[branch]);
        let listing = std::fs::read_to_string(&seen).expect("the fake bats recorded the locks");
        (out, listing)
    }

    /// A push of `branch` whose fake `bats` records the hook's pid and the owner line of the
    /// branch's lock while the suite runs; the push's output and those two lines.
    fn push_recording_owner(&self, envs: &[(&str, &str)], branch: &str) -> (Output, String) {
        let seen = self.home.path().join("owner-seen");
        let lock = self.lockfile(&self.repo, branch);
        let mut all: Vec<(&str, &str)> = envs.to_vec();
        all.push(("FAKE_BATS_OWNER", seen.to_str().expect("a UTF-8 path")));
        all.push(("FAKE_BATS_OWNER_LOCK", lock.to_str().expect("a UTF-8 path")));
        let out = self.push(&self.repo, &all, &[branch]);
        let owner = std::fs::read_to_string(&seen).expect("the fake bats recorded the owner");
        (out, owner)
    }

    /// The name of `branch`'s lock file in the scratch repository.
    fn lock_name(&self, branch: &str) -> String {
        self.lockfile(&self.repo, branch)
            .file_name()
            .expect("a lock name")
            .to_str()
            .expect("a UTF-8 name")
            .to_owned()
    }

    /// The exact listing of the lock directory while `branch`'s push holds only its lock.
    fn held_listing(&self, branch: &str) -> String {
        format!("{}\n", self.lock_name(branch))
    }

    /// No lock file may remain once the hook has exited. A missing directory holds none; any
    /// other read error is a failure, never an empty listing.
    fn assert_no_locks(&self, repo: &Path) {
        let dir = self.lock_dir(repo);
        let left: Vec<String> = match std::fs::read_dir(&dir) {
            Ok(entries) => entries
                .map(|e| e.expect("a lock entry").path().display().to_string())
                .collect(),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Vec::new(),
            Err(e) => panic!("the lock directory {} cannot be read: {e}", dir.display()),
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
        process::output_text(&second)
    );
    let shown = process::output_text(&second);
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
        process::output_text(&first_out)
    );
    ctx.assert_no_locks(&ctx.repo);
}

#[test]
fn a_push_of_a_different_branch_is_not_blocked() {
    // Arrange
    let ctx = Ctx::new();
    let mut first = ctx.start_holding(&ctx.repo, "feat");

    // Act: the other push records the lock directory while its suite runs.
    let (other, during) = ctx.push_recording_locks(&[], "other");
    let still_held = ctx.lockfile(&ctx.repo, "feat").is_file();
    let other_lock = ctx.lockfile(&ctx.repo, "other").exists();
    first.finish();

    // Assert
    assert_eq!(
        code(&other),
        0,
        "the other branch was blocked: {}",
        process::output_text(&other)
    );
    assert!(!process::output_text(&other).contains("PUSH REFUSED"));
    assert!(still_held, "the first push's lock was touched");
    // The other push held its own lock beside feat's during its suite, so its absence after is
    // a release, not a lock never taken.
    assert_eq!(
        during,
        format!("{}\n{}\n", ctx.lock_name("feat"), ctx.lock_name("other")),
        "the other push did not hold its own lock beside feat's"
    );
    assert!(!other_lock, "the second push left a lock of its own");
}

#[test]
fn a_refused_multi_branch_push_releases_the_locks_it_took() {
    // Arrange
    let ctx = Ctx::new();
    write_executable(&ctx.fakebin.join("ps"), RECORDING_PS);
    let mut first = ctx.start_holding(&ctx.repo, "feat");
    let at_refusal = ctx.home.path().join("locks-at-refusal");
    let dir = ctx.lock_dir(&ctx.repo);

    // Act: `aaa` is taken first, then `feat` is refused, and the refusal must release `aaa`.
    let refused = ctx.push(
        &ctx.repo,
        &[
            ("FAKE_PS_LOCKS", at_refusal.to_str().expect("a UTF-8 path")),
            ("FAKE_PS_LOCK_DIR", dir.to_str().expect("a UTF-8 path")),
        ],
        &["aaa", "feat"],
    );
    let aaa_lock = ctx.lockfile(&ctx.repo, "aaa").exists();
    let feat_lock = ctx.lockfile(&ctx.repo, "feat").is_file();
    first.finish();

    // Assert
    assert_ne!(
        code(&refused),
        0,
        "the push holding feat was allowed: {}",
        process::output_text(&refused)
    );
    // The refused push held `aaa` when it was refused on `feat`, so the absence after is a release.
    let held = std::fs::read_to_string(&at_refusal).expect("the refusal recorded the locks");
    assert_eq!(
        held,
        format!("{}\n{}\n", ctx.lock_name("aaa"), ctx.lock_name("feat")),
        "the refused push did not hold aaa when feat was refused"
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
    let (out, owner) = ctx.push_recording_owner(&[], "feat");

    // Assert
    assert_eq!(
        code(&out),
        0,
        "the stale lock was not reclaimed: {}",
        process::output_text(&out)
    );
    assert!(
        process::output_text(&out).contains("reclaimed a stale lock"),
        "{}",
        process::output_text(&out)
    );
    // While the suite ran, the lock named the hook itself as owner, not the dead pid.
    let hook_pid = owner
        .lines()
        .next()
        .expect("the fake bats recorded the hook pid");
    assert_ne!(hook_pid, dead, "the recorded hook pid is the dead owner");
    assert_eq!(
        owner,
        format!("{hook_pid}\n{hook_pid}\n"),
        "the reclaimed lock was not owned by the running hook"
    );
    ctx.assert_no_locks(&ctx.repo);
}

#[test]
fn released_after_the_suites_pass() {
    // Arrange
    let ctx = Ctx::new();

    // Act
    let (out, held) = ctx.push_recording_locks(&[], "feat");

    // Assert: the push held its lock while the suite ran, and released it after.
    assert_eq!(code(&out), 0, "{}", process::output_text(&out));
    assert_eq!(
        held,
        ctx.held_listing("feat"),
        "the push never held its lock"
    );
    ctx.assert_no_locks(&ctx.repo);
}

#[test]
fn released_after_a_suite_fails() {
    // Arrange
    let ctx = Ctx::new();

    // Act
    let (out, held) = ctx.push_recording_locks(&[("FAKE_BATS_RC", "1")], "feat");

    // Assert: the push held its lock while the suite ran, and released it after.
    assert_eq!(
        code(&out),
        1,
        "expected a blocked push: {}",
        process::output_text(&out)
    );
    assert!(
        process::output_text(&out).contains("PUSH BLOCKED"),
        "{}",
        process::output_text(&out)
    );
    assert_eq!(
        held,
        ctx.held_listing("feat"),
        "the push never held its lock"
    );
    ctx.assert_no_locks(&ctx.repo);
}

#[test]
fn skip_tests_still_takes_the_lock_and_releases_it() {
    // Arrange
    let ctx = Ctx::new();

    // Act: a skipped push takes the lock, releases it, and says the suites did not run.
    let skipped = ctx.push_default(&[("SKIP_TESTS", "1")], "feat");

    // Assert
    assert_eq!(code(&skipped), 0, "{}", process::output_text(&skipped));
    assert!(
        process::output_text(&skipped).contains("TESTS SKIPPED"),
        "{}",
        process::output_text(&skipped)
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
        process::output_text(&refused)
    );
    assert!(
        process::output_text(&refused).contains("PUSH REFUSED"),
        "{}",
        process::output_text(&refused)
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
    let out = first.finish();

    // Assert: the hook's TERM handler ended the push, not a normal exit after the suite.
    assert_eq!(
        code(&out),
        143,
        "TERM did not interrupt the push: {}",
        process::output_text(&out)
    );
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
        process::output_text(&other)
    );
    assert!(!process::output_text(&other).contains("PUSH REFUSED"));
    assert!(held, "the feature/a lock is gone");
}

#[test]
fn the_printed_recovery_command_works_for_a_path_with_spaces_and_quotes() {
    // Arrange: a repository whose path holds spaces and an apostrophe.
    let ctx = Ctx::new();
    let repo = ctx.home.path().join("it's my repo");
    ctx.init_repo(&repo);
    let mut first = ctx.start_holding(&repo, "feat");

    // Act: the refusal prints a command; run exactly what the operator would paste.
    let refused = ctx.push(&repo, &[], &["feat"]);
    let shown = process::output_text(&refused);
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
    let started = ctx.home.path().join("started");

    // Act
    let out = ctx.push_default(
        &[("FAKE_BATS_STARTED", started.to_str().expect("a UTF-8 path"))],
        "feat",
    );

    // Assert
    assert_eq!(
        code(&out),
        0,
        "an infrastructure failure refused the push: {}",
        process::output_text(&out)
    );
    assert!(
        process::output_text(&out).contains("Continuing WITHOUT it"),
        "{}",
        process::output_text(&out)
    );
    assert!(
        !process::output_text(&out).contains("PUSH REFUSED"),
        "{}",
        process::output_text(&out)
    );
    assert!(started.exists(), "the suite did not run after the warning");
    // The directory the link was tried in exists, so the empty listing below is a real read.
    assert!(
        ctx.lock_dir(&ctx.repo).is_dir(),
        "the hook made no lock directory"
    );
    ctx.assert_no_locks(&ctx.repo);
}
