//! The pre-push hook: the Python suite on the uv-locked environment, the fast lane, the
//! classification of a collection failure, BATS beside pytest, and the Rust suite's push lane
//! (cli tier).
//!
//! On 2026-11-09 a push skipped pytest entirely: the hook used the workstation's python3, which
//! had no `ansible`, and a missing module is fail-open. The hook now runs `uv sync --locked`
//! before both suites and puts the checkout's .venv/bin FIRST on PATH, so `python3`, `pytest`
//! and `ansible-playbook` are the locked versions. It falls back, with a warning, when uv is
//! absent, the sync fails, or the sync left no .venv. On the locked environment a missing module
//! is an undeclared import, so the push fails instead of skipping.
//!
//! A collection failure is read from the one pytest run's output (plan 21, phase 4). A failing
//! test is never classified as a collection failure, whatever its output says.
//!
//! The cases drive the real hook in a scratch repository with fake `uv`, `python3`, `bats` and
//! `parallel` on PATH, and read what ran from logs rather than from output. The fake `bats` logs
//! the first PATH entry it ran under and, on request, its arguments, a start mark, its pids, the
//! scratch directory it saw and a child process. The fake `python3` logs its own path and the
//! first PATH entry for every call, and the `-m pytest` run again on its own line. The fake `uv`
//! logs its arguments, one per line. The fake `parallel` logs
//! nothing: it only answers the presence check. The fake `mktemp` honours TMPDIR, as GNU `mktemp`
//! does and macOS `mktemp` does not, so the scratch directory is in the cases' sight. A held push
//! runs on a thread through `process::scrubbed`, its bound raised to 60 s; `HeldPush` writes
//! the release file and joins the thread on drop, so a failing case never leaves a hook running.
//!
//! Replaces platform/tests/test_pre_push_uv.bats (plan 24, Phase 4).
//!
//! Port map (every BATS @test, once):
//!
//! | BATS @test | Rust fn |
//! | --- | --- |
//! | pre-push uv: a successful locked sync puts .venv/bin first on PATH for both suites | `successful_locked_sync_puts_venv_first_on_path_for_both_suites` |
//! | pre-push uv: a sync that leaves no .venv falls back with a warning, not a blocked push | `sync_that_leaves_no_venv_falls_back_with_warning_not_blocked` |
//! | pre-push uv: UV_PROJECT_ENVIRONMENT elsewhere ignores a stale .venv and falls back | `uv_project_environment_elsewhere_ignores_stale_venv_and_falls_back` |
//! | pre-push uv: a failed sync falls back to python3 with a warning | `failed_sync_falls_back_to_python3_with_warning` |
//! | pre-push uv: without uv the suite runs on python3 with a warning | `without_uv_the_suite_runs_on_python3_with_warning` |
//! | pre-push uv: a missing module on the locked environment fails the push | `missing_module_on_locked_environment_fails_the_push` |
//! | pre-push uv: a missing module on the python3 fallback still skips, and names the module | `missing_module_on_python3_fallback_still_skips_and_names_module` |
//! | pre-push uv: a missing module in the parallel shape still skips on the python3 fallback | `missing_module_in_parallel_shape_still_skips_on_python3_fallback` |
//! | pre-push uv: a collection failure that is not a missing module blocks, locked and fallback | `collection_failure_not_a_missing_module_blocks_locked_and_fallback` |
//! | pre-push uv: a syntax error in the parallel shape blocks, locked and fallback | `syntax_error_in_parallel_shape_blocks_locked_and_fallback` |
//! | pre-push uv: a failing test that prints No module named blocks on the python3 fallback | `failing_test_that_prints_no_module_named_blocks_on_python3_fallback` |
//! | pre-push uv: a missing module next to a failing test blocks on the python3 fallback | `missing_module_next_to_failing_test_blocks_on_python3_fallback` |
//! | pre-push uv: a conftest that will not load skips on the fallback and blocks on the locked environment | `conftest_that_will_not_load_skips_on_fallback_and_blocks_locked` |
//! | pre-push fast lane: PYTEST_FAST and exactly the changed paths reach pytest | `fast_lane_pytest_fast_and_exactly_the_changed_paths_reach_pytest` |
//! | pre-push fast lane: a push with no measurable base runs the full suite, and says why | `fast_lane_push_with_no_measurable_base_runs_full_suite_and_says_why` |
//! | pre-push uv: a failing BATS run blocks the push even when pytest passes | `failing_bats_run_blocks_the_push_even_when_pytest_passes` |
//! | pre-push uv: BATS takes CI's flags when GNU parallel is installed | `bats_takes_ci_flags_when_gnu_parallel_is_installed` |
//! | pre-push uv: without GNU parallel BATS runs serially, with a note | `without_gnu_parallel_bats_runs_serially_with_a_note` |
//! | pre-push uv: BATS is already running while pytest runs | `bats_is_already_running_while_pytest_runs` |
//! | pre-push uv: a finished run leaves no temporary file behind | `a_finished_run_leaves_no_temporary_file_behind` |
//! | pre-push uv: TERM stops the BATS job, removes the hook's temporary files and frees the lock | `term_stops_the_bats_job_removes_temporary_files_and_frees_the_lock` |
//! | pre-push uv: a push to a URL with credentials never prints the URL | `a_push_to_a_url_with_credentials_never_prints_the_url` |
//! | pre-push uv: a python3 without pytest skips on the fallback and blocks on the locked environment | `python3_without_pytest_skips_on_fallback_and_blocks_on_locked` |
//! | pre-push uv: one missing module does not forgive a syntax error in another file | `one_missing_module_does_not_forgive_a_syntax_error_in_another_file` |
//! | pre-push uv: a full run that collects no tests blocks | `a_full_run_that_collects_no_tests_blocks` |
//! | pre-push uv: TERM kills the processes BATS started, not only BATS itself | `term_kills_the_processes_bats_started_not_only_bats_itself` |
//! | pre-push rust: the push lane of the Rust suite runs | `the_push_lane_of_the_rust_suite_runs` |
//! | pre-push rust: a failing Rust lane blocks the push even when the other suites pass | `a_failing_rust_lane_blocks_the_push_even_when_the_other_suites_pass` |

use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::process::Output;
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use agent_cloud_tests::process;
use agent_cloud_tests::repo;
use agent_cloud_tests::text::any_line;
use assert_cmd::Command;
use tempfile::TempDir;

const HOOK: &str = ".githooks/pre-push";
const JOB_SHELL: &str = "platform/tests/bin/parallel-job-shell";
const URL: &str = "https://example.invalid/repo.git";
const SHA: &str = "1111111111111111111111111111111111111111";

/// Fake `bats`: logs the first PATH entry it ran under and, on request, its arguments (one per
/// line), a start mark (FAKE_BATS_MARK, removed before exit when FAKE_BATS_RELEASE is set), its
/// pids, the scratch directory it saw and a child process. Then it waits for FAKE_BATS_HOLD and,
/// with FAKE_BATS_RELEASE, for the fake pytest's release (15 s at most), and exits FAKE_BATS_RC.
const FAKE_BATS: &str = r#"#!/usr/bin/env sh
echo "${PATH%%:*}" >> "$FAKE_BATS_LOG"
[ -n "${FAKE_BATS_ARGS:-}" ] && printf '%s\n' "$@" > "$FAKE_BATS_ARGS"
[ -n "${FAKE_BATS_MARK:-}" ] && : > "$FAKE_BATS_MARK"
[ -n "${FAKE_BATS_SEEN:-}" ] && ls -A "$TMPDIR" > "$FAKE_BATS_SEEN"
[ -n "${FAKE_BATS_PIDS:-}" ] && echo "$PPID $$" > "$FAKE_BATS_PIDS"
# FAKE_BATS_CHILD: a long-lived child, as real bats runs its suite in. Its pid is written to the
# named file, so the test can show the child was alive before TERM.
if [ -n "${FAKE_BATS_CHILD:-}" ]; then
  sleep 30 &
  echo "$!" > "$FAKE_BATS_CHILD"
fi
if [ -n "${FAKE_BATS_HOLD:-}" ]; then
  i=0
  while [ ! -e "$FAKE_BATS_HOLD" ] && [ "$i" -lt 300 ]; do sleep 0.1; i=$((i + 1)); done
fi
# FAKE_BATS_RELEASE: the overlap handshake. Wait for the fake pytest to create it, then remove
# FAKE_BATS_MARK, so the mark exists only while this fake runs. 15 s leaves room for the hook's
# work between starting BATS and starting pytest. A hook that runs the suites one after the other
# finds no mark when pytest looks, so the test fails.
if [ -n "${FAKE_BATS_RELEASE:-}" ]; then
  i=0
  while [ ! -e "$FAKE_BATS_RELEASE" ] && [ "$i" -lt 150 ]; do sleep 0.1; i=$((i + 1)); done
  rm -f "$FAKE_BATS_MARK"
fi
exit "${FAKE_BATS_RC:-0}"
"#;

/// Fake python: logs its own path and the first PATH entry for every call. The one pytest run
/// logs them again as `pytest-run` and logs the fast lane's environment. It copies the
/// PYTEST_FAST_CHANGED list to FAKE_FAST_COPY when both are set, and, when FAKE_WAIT is set,
/// waits for that file; finding it, it logs `bats-started-during-pytest` and creates
/// FAKE_BATS_RELEASE when that is set. It then prints FAKE_COLLECT_OUT and exits
/// FAKE_COLLECT_RC. Every other call succeeds, except the `import pytest` probe under
/// FAKE_NO_PYTEST=1.
const FAKE_PYTHON: &str = r#"#!/usr/bin/env sh
echo "$0 first-path=${PATH%%:*}" >> "$FAKE_PY_LOG"
case "$*" in
  *"import pytest"*) [ "${FAKE_NO_PYTEST:-0}" = 1 ] && exit 1; exit 0 ;;
  *"-m pytest"*)
    echo "pytest-run $0 first-path=${PATH%%:*}" >> "$FAKE_PY_LOG"
    echo "pytest PYTEST_FAST=${PYTEST_FAST:-unset} PYTEST_FAST_CHANGED=${PYTEST_FAST_CHANGED:-unset}" >> "$FAKE_PY_LOG"
    if [ -n "${PYTEST_FAST_CHANGED:-}" ] && [ -n "${FAKE_FAST_COPY:-}" ]; then
      cp "$PYTEST_FAST_CHANGED" "$FAKE_FAST_COPY"
    fi
    if [ -n "${FAKE_WAIT:-}" ]; then
      i=0
      while [ ! -e "$FAKE_WAIT" ] && [ "$i" -lt 50 ]; do sleep 0.1; i=$((i + 1)); done
      if [ -e "$FAKE_WAIT" ]; then
        echo "bats-started-during-pytest" >> "$FAKE_PY_LOG"
        [ -n "${FAKE_BATS_RELEASE:-}" ] && : > "$FAKE_BATS_RELEASE"
      fi
    fi
    printf '%s\n' "${FAKE_COLLECT_OUT:-}"
    exit "${FAKE_COLLECT_RC:-0}" ;;
esac
exit 0
"#;

/// Fake uv: records its arguments, one per line, exits FAKE_UV_RC, and on success creates .venv/bin's python
/// and python3 as the real sync does, unless FAKE_UV_NOVENV=1. `@FAKEPY@` is the fake python.
const FAKE_UV: &str = r#"#!/usr/bin/env sh
printf '%s\n' "$@" >> "$FAKE_UV_LOG"
[ "${FAKE_UV_RC:-0}" = 0 ] || exit "$FAKE_UV_RC"
[ "${FAKE_UV_NOVENV:-0}" = 1 ] && exit 0
mkdir -p .venv/bin && cp "@FAKEPY@" .venv/bin/python && cp "@FAKEPY@" .venv/bin/python3
"#;

/// A stand-in for `mktemp -d` on the scratch PATH. The macOS `mktemp` ignores TMPDIR when it is
/// given no template, so the hook's scratch directory would be made outside the directory these
/// cases read. This one honours TMPDIR, as GNU `mktemp` does, and calls the real `mktemp` with a
/// template under it.
const FAKE_MKTEMP: &str = r#"#!/usr/bin/env sh
exec /usr/bin/mktemp -d "$TMPDIR/tmp.XXXXXXXX"
"#;

/// A `parallel` that is installed and does nothing: the hook asks for it by name.
const FAKE_PARALLEL: &str = "#!/bin/sh\nexit 0\n";

/// A fake platform/tests/lane.sh: records its arguments, one per line, and exits FAKE_LANE_RC.
const FAKE_LANE: &str = r#"#!/usr/bin/env sh
printf '%s\n' "$@" > "@LANE_ARGS@"
exit "${FAKE_LANE_RC:-0}"
"#;

/// The scratch checkout and its fakes.
struct Ctx {
    home: TempDir,
    repo: PathBuf,
    fakebin: PathBuf,
    nouv: PathBuf,
    py_log: PathBuf,
    bats_log: PathBuf,
    uv_log: PathBuf,
    tmp: PathBuf,
    fakepython: PathBuf,
}

/// A hook run: its exit status and its output, stdout and stderr together (as BATS `run` shows).
struct Run {
    code: i32,
    text: String,
}

impl Ctx {
    fn new() -> Self {
        let home = TempDir::new().expect("a temporary directory");
        let t = home.path().to_path_buf();
        let repo = t.join("repo");
        let fakebin = t.join("fakebin");
        let nouv = t.join("nouv");
        let system = t.join("system");
        let tmp = t.join("tmp");
        for dir in [&fakebin, &nouv, &system, &tmp] {
            std::fs::create_dir(dir).expect("a scratch directory");
        }
        std::fs::create_dir_all(repo.join("platform/tests/bin")).expect("the checkout");
        std::fs::copy(repo::path(JOB_SHELL), repo.join(JOB_SHELL)).expect("the job shell");
        std::fs::write(repo.join("pyproject.toml"), "").expect("pyproject.toml");
        std::fs::write(repo.join("uv.lock"), "").expect("uv.lock");
        let out = process::scrubbed("git", &t)
            .args(["init", "-q"])
            .arg(&repo)
            .output()
            .expect("git starts");
        assert!(out.status.success(), "git init failed");

        let fakepython = t.join("fakepython");
        write_executable(&fakepython, FAKE_PYTHON);
        write_executable(&fakebin.join("bats"), FAKE_BATS);
        write_executable(&nouv.join("bats"), FAKE_BATS);
        write_executable(&fakebin.join("python3"), FAKE_PYTHON);
        write_executable(&nouv.join("python3"), FAKE_PYTHON);
        write_executable(
            &fakebin.join("uv"),
            &FAKE_UV.replace("@FAKEPY@", &fakepython.display().to_string()),
        );
        write_executable(&fakebin.join("parallel"), FAKE_PARALLEL);
        write_executable(&fakebin.join("mktemp"), FAKE_MKTEMP);

        Self {
            py_log: t.join("python.log"),
            bats_log: t.join("bats.log"),
            uv_log: t.join("uv.log"),
            home,
            repo,
            fakebin,
            nouv,
            tmp,
            fakepython,
        }
    }

    fn t(&self) -> &Path {
        self.home.path()
    }

    /// The PATH a run gets: the bin directory first. The `nouv` directory is paired only with
    /// the scratch `system` directory: links to every tool of /usr/bin and /bin except uv, uvx
    /// and GNU parallel, so no real uv can be found on it, wherever the machine installs one.
    /// Joined as OsString: a non-UTF-8 entry in the inherited PATH must survive.
    fn path_for(&self, bin: &Path) -> OsString {
        let mut entries = vec![bin.to_path_buf()];
        if bin == self.nouv {
            // Filled here, not in `new`, so only the cases that use the no-uv PATH pay for it.
            let system = self.t().join("system");
            let dirs: Vec<&Path> = SYSTEM_DIRS.iter().map(Path::new).collect();
            link_system_tools(&system, &dirs);
            entries.push(system);
        } else if let Some(inherited) = std::env::var_os("PATH") {
            entries.extend(std::env::split_paths(&inherited));
        }
        std::env::join_paths(entries).expect("PATH holds the bin directory")
    }

    /// The hook in the checkout for `sha`, with BIN first on PATH, the remote named `remote` and
    /// `envs` in its environment, its stdin the push line. Not yet run.
    fn hook_command(
        &self,
        sha: &str,
        bin: &Path,
        remote: (&str, &str),
        envs: &[(&str, &str)],
    ) -> Command {
        let mut cmd = process::scrubbed("sh", self.t());
        cmd.current_dir(&self.repo)
            .env("FAKE_PY_LOG", &self.py_log)
            .env("FAKE_BATS_LOG", &self.bats_log)
            .env("FAKE_UV_LOG", &self.uv_log)
            .env("TMPDIR", &self.tmp)
            .env("PATH", self.path_for(bin));
        cmd.envs(envs.iter().copied());
        let hook = repo::path(HOOK);
        cmd.arg(hook.to_str().expect("a UTF-8 path"))
            .arg(remote.0)
            .arg(remote.1)
            .write_stdin(format!(
                "refs/heads/feat {sha} refs/heads/feat 0000000000000000000000000000000000000000\n"
            ));
        cmd
    }

    /// Runs the hook in the checkout for `sha`, with BIN first on PATH, the remote named `remote`
    /// and `envs` in its environment.
    fn run_hook(&self, sha: &str, bin: &Path, remote: (&str, &str), envs: &[(&str, &str)]) -> Run {
        let out = self
            .hook_command(sha, bin, remote, envs)
            .output()
            .expect("the hook starts");
        Run::of(out)
    }

    /// A stale `.venv` in the checkout, holding the fake python: a hook that puts it on PATH
    /// when it must not shows up in the python log.
    fn stale_venv(&self) {
        let venv = self.repo.join(".venv/bin");
        std::fs::create_dir_all(&venv).expect("the stale .venv");
        std::fs::copy(&self.fakepython, venv.join("python")).expect("the stale python");
        std::fs::copy(&self.fakepython, venv.join("python3")).expect("the stale python3");
    }

    /// The push of SHA to the ordinary remote.
    fn push(&self, bin: &Path, envs: &[(&str, &str)]) -> Run {
        self.run_hook(SHA, bin, ("origin", URL), envs)
    }

    /// The hook as a background job. The caller's `envs` name the fake bats's release file
    /// (FAKE_BATS_HOLD) and pids file (FAKE_BATS_PIDS).
    fn spawn_held(&self, envs: &[(&str, &str)]) -> JoinHandle<Output> {
        let mut cmd = self.hook_command(SHA, &self.fakebin, ("origin", URL), envs);
        // Above the 20 s readiness wait plus the fake bats's own 30 s hold: a held hook killed at
        // the scrubbed 30 s default runs no EXIT trap, so its lock and scratch files stay behind.
        cmd.timeout(Duration::from_secs(60));
        thread::spawn(move || cmd.output().expect("the held push starts"))
    }

    /// `git` in the checkout, with an identity on the command line. Returns stdout.
    fn git(&self, args: &[&str]) -> String {
        let out = process::scrubbed("git", self.t())
            .current_dir(&self.repo)
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
        String::from_utf8_lossy(&out.stdout).trim().to_owned()
    }

    /// One commit that appends `message` to `rel`; returns the new commit's sha.
    fn commit_file(&self, rel: &str, message: &str) -> String {
        let path = self.repo.join(rel);
        std::fs::create_dir_all(path.parent().expect("a parent")).expect("the directory");
        let mut body = read_or_empty(&path);
        body.push_str(message);
        body.push('\n');
        std::fs::write(&path, body).expect("the file is written");
        self.git(&["add", "--", rel]);
        self.git(&["commit", "-q", "-m", message]);
        self.git(&["rev-parse", "HEAD"])
    }
}

impl Run {
    fn of(out: Output) -> Self {
        Self {
            code: out.status.code().expect("the hook exits, it is not killed"),
            text: process::output_text(&out),
        }
    }

    fn passed(&self) -> bool {
        self.code == 0
    }

    fn failed(&self) -> bool {
        self.code != 0
    }
}

/// The directories the original BATS fixture put after `nouv` (`nouv:/usr/bin:/bin`).
const SYSTEM_DIRS: &[&str] = &["/usr/bin", "/bin"];

/// Never linked onto the no-uv PATH, wherever the machine installs them: uv and uvx would turn
/// the no-uv case into a sync, and GNU parallel would change how BATS runs.
const NOUV_EXCLUDED: &[&str] = &["uv", "uvx", "parallel"];

/// Links every entry of `from` into `dir` under its own name, the first directory winning, except
/// `NOUV_EXCLUDED`. Every tool the original fixture's PATH held stays, so the hook's own tools
/// (cksum, ln, ps, ...) need no list kept in step with it.
fn link_system_tools(dir: &Path, from: &[&Path]) {
    for source in from {
        let Ok(entries) = std::fs::read_dir(source) else {
            continue;
        };
        for entry in entries {
            let entry = entry.expect("a system directory entry");
            let name = entry.file_name();
            if NOUV_EXCLUDED.iter().any(|x| name == *x) {
                continue;
            }
            let link = dir.join(&name);
            if link.symlink_metadata().is_err() {
                std::os::unix::fs::symlink(entry.path(), link).expect("a system tool link");
            }
        }
    }
}

fn write_executable(path: &Path, body: &str) {
    use std::os::unix::fs::PermissionsExt;
    std::fs::write(path, body).expect("the fake is written");
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o755)).expect("the mode");
}

/// A file's text; a missing file is a failure, as `grep` on a missing file is.
fn read(path: &Path) -> String {
    std::fs::read_to_string(path).unwrap_or_else(|e| panic!("{}: {e}", path.display()))
}

/// The pytest run used the fakebin python3 with fakebin first on PATH, and no logged line names
/// `.venv/bin`.
#[track_caller]
fn assert_pytest_ran_on_fakebin(ctx: &Ctx) {
    let log = read(&ctx.py_log);
    assert!(
        any_line(
            &log,
            r"^pytest-run .*/fakebin/python3 first-path=.*/fakebin$"
        ),
        "{log}"
    );
    assert!(!log.contains(".venv/bin"), "{log}");
}

/// A file's text, empty when the file does not exist: a log never written reads as no calls. Any
/// other read error panics, so an unreadable log is never read as an empty one.
fn read_or_empty(path: &Path) -> String {
    match std::fs::read_to_string(path) {
        Ok(text) => text,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => String::new(),
        Err(e) => panic!("{}: {e}", path.display()),
    }
}

/// `items` as printf '%s\n' prints them: each line terminated.
fn terminated(items: &[&str]) -> String {
    let mut text = items.join("\n");
    text.push('\n');
    text
}

// Pytest's output for the failures the hook must classify, in the shapes it prints. The serial
// shape interrupts the run (exit 2); the parallel shape (xdist) goes on (exit 1).

/// A test module that does not import: serial shape.
fn out_missing_module() -> String {
    terminated(&[
        "_______________________ ERROR collecting test_mod.py ________________________",
        "E   ModuleNotFoundError: No module named 'nope'",
        "=========================== short test summary info ============================",
        "ERROR test_mod.py",
        "!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!",
        "1 error in 0.03s",
    ])
}

/// A test module with a syntax error: serial shape.
fn out_syntax_error() -> String {
    terminated(&[
        "_______________________ ERROR collecting test_mod.py ________________________",
        "E   SyntaxError: invalid syntax",
        "=========================== short test summary info ============================",
        "ERROR test_mod.py",
        "!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!",
        "1 error in 0.05s",
    ])
}

/// The same syntax error: parallel shape, the run goes on and names the file alone.
fn out_syntax_error_parallel() -> String {
    terminated(&[
        "_______________________ ERROR collecting test_mod.py ________________________",
        "E   SyntaxError: invalid syntax",
        "=========================== short test summary info ============================",
        "ERROR test_mod.py - SyntaxError: invalid syntax",
        "1 error in 0.70s",
    ])
}

/// A module that does not import, in the parallel shape, next to a passing test.
fn out_missing_module_parallel() -> String {
    terminated(&[
        "_______________________ ERROR collecting test_mod.py ________________________",
        "E   ModuleNotFoundError: No module named 'nope'",
        "=========================== short test summary info ============================",
        "ERROR test_mod.py - ImportError while importing test module",
        "1 passed, 1 error in 0.66s",
    ])
}

/// A conftest that does not load (exit 4, no banner, in either shape).
fn out_conftest_missing() -> String {
    terminated(&[
        "ImportError while loading conftest '/repo/conftest.py'.",
        "conftest.py:1: in <module>",
        "E   ModuleNotFoundError: No module named 'nope'",
    ])
}

/// A test that fails and whose own message says "No module named": no collection error.
fn out_failing_test() -> String {
    terminated(&[
        "_______________________ test_fail _______________________",
        "E       AssertionError: No module named widgets",
        "FAILED test_fail.py::test_fail - AssertionError: No module named widgets",
        "1 failed in 0.01s",
    ])
}

/// A module that does not import AND a test that fails: both must block on the fallback.
fn out_collection_and_failure() -> String {
    terminated(&[
        "_______________________ ERROR collecting test_mod.py ________________________",
        "E   ModuleNotFoundError: No module named 'nope'",
        "=========================== short test summary info ============================",
        "FAILED test_fail.py::test_fail - AssertionError: No module named widgets",
        "ERROR test_mod.py - ImportError while importing test module",
        "1 failed, 1 error in 0.53s",
    ])
}

#[test]
fn successful_locked_sync_puts_venv_first_on_path_for_both_suites() {
    // Arrange
    let ctx = Ctx::new();

    // Act
    let run = ctx.push(&ctx.fakebin, &[]);

    // Assert
    assert!(run.passed(), "{}", run.text);
    assert_eq!(
        read(&ctx.uv_log),
        "sync\n--locked\n--quiet\n",
        "uv was not synced locked"
    );
    assert!(
        any_line(
            &read(&ctx.py_log),
            r"\.venv/bin/python3 first-path=.*/\.venv/bin$"
        ),
        "python3 did not run from .venv/bin: {}",
        read(&ctx.py_log)
    );
    // On the `-m pytest` run itself: the import probe alone logs the same interpreter.
    assert!(
        any_line(
            &read(&ctx.py_log),
            r"^pytest-run .*/\.venv/bin/python3 first-path=.*/\.venv/bin$"
        ),
        "pytest did not run from .venv/bin: {}",
        read(&ctx.py_log)
    );
    assert!(
        !read(&ctx.py_log).contains("fakebin/python3 "),
        "{}",
        read(&ctx.py_log)
    );
    assert!(
        any_line(&read(&ctx.bats_log), r"/\.venv/bin$"),
        "bats did not run under .venv/bin"
    );
    assert!(!run.text.contains("WARNING"), "{}", run.text);
}

#[test]
fn sync_that_leaves_no_venv_falls_back_with_warning_not_blocked() {
    // Arrange
    let ctx = Ctx::new();

    // Act
    let run = ctx.push(&ctx.fakebin, &[("FAKE_UV_NOVENV", "1")]);

    // Assert
    assert!(run.passed(), "{}", run.text);
    assert_pytest_ran_on_fakebin(&ctx);
    assert!(run.text.contains("did not create .venv"), "{}", run.text);
}

#[test]
fn uv_project_environment_elsewhere_ignores_stale_venv_and_falls_back() {
    // Arrange: a stale .venv in the checkout, and UV_PROJECT_ENVIRONMENT pointing elsewhere.
    let ctx = Ctx::new();
    ctx.stale_venv();
    let elsewhere = ctx.t().join("elsewhere");

    // Act
    let run = ctx.push(
        &ctx.fakebin,
        &[(
            "UV_PROJECT_ENVIRONMENT",
            elsewhere.to_str().expect("a UTF-8 path"),
        )],
    );

    // Assert
    assert!(run.passed(), "{}", run.text);
    assert_pytest_ran_on_fakebin(&ctx);
    assert!(
        run.text.contains("UV_PROJECT_ENVIRONMENT points outside"),
        "{}",
        run.text
    );
    // The hook must not sync at all here: a sync would land outside the checkout.
    let uv = read_or_empty(&ctx.uv_log);
    assert!(uv.is_empty(), "uv was invoked: {uv}");
    // Positive control: the same fake uv, without the variable, does log its call, so the
    // empty log above means the hook did not call it.
    let control = ctx.push(&ctx.fakebin, &[]);
    assert!(control.passed(), "{}", control.text);
    assert_eq!(read(&ctx.uv_log), "sync\n--locked\n--quiet\n");
}

#[test]
fn failed_sync_falls_back_to_python3_with_warning() {
    // Arrange: a stale .venv in the checkout, which a failed sync must not put on PATH.
    let ctx = Ctx::new();
    ctx.stale_venv();

    // Act
    let run = ctx.push(&ctx.fakebin, &[("FAKE_UV_RC", "1")]);

    // Assert
    assert!(run.passed(), "{}", run.text);
    assert_pytest_ran_on_fakebin(&ctx);
    assert!(
        run.text.contains("`uv sync --locked` failed"),
        "{}",
        run.text
    );
}

#[test]
fn without_uv_the_suite_runs_on_python3_with_warning() {
    // Arrange
    let ctx = Ctx::new();

    // Act
    let run = ctx.push(&ctx.nouv, &[]);

    // Assert
    assert!(run.passed(), "{}", run.text);
    assert!(
        any_line(
            &read(&ctx.py_log),
            r"^pytest-run .*/nouv/python3 first-path=.*/nouv$"
        ),
        "{}",
        read(&ctx.py_log)
    );
    assert!(run.text.contains("uv not found"), "{}", run.text);
}

#[test]
fn missing_module_on_locked_environment_fails_the_push() {
    // Arrange
    let ctx = Ctx::new();

    // Act
    let run = ctx.push(
        &ctx.fakebin,
        &[
            ("FAKE_COLLECT_RC", "2"),
            ("FAKE_COLLECT_OUT", &out_missing_module()),
        ],
    );

    // Assert
    assert!(run.failed(), "{}", run.text);
    assert!(
        run.text.contains("Python collection FAILED"),
        "{}",
        run.text
    );
}

#[test]
fn missing_module_on_python3_fallback_still_skips_and_names_module() {
    // Arrange
    let ctx = Ctx::new();

    // Act
    let run = ctx.push(
        &ctx.nouv,
        &[
            ("FAKE_COLLECT_RC", "2"),
            ("FAKE_COLLECT_OUT", &out_missing_module()),
        ],
    );

    // Assert
    assert!(run.passed(), "{}", run.text);
    assert!(run.text.contains("not collectable here"), "{}", run.text);
    // The hook's own indented line names the module. pytest's unindented output, which the hook
    // echoes, names it too, so only the indented line shows the hook printed it.
    assert!(
        run.text
            .contains("\n          E   ModuleNotFoundError: No module named 'nope'\n"),
        "{}",
        run.text
    );
}

#[test]
fn missing_module_in_parallel_shape_still_skips_on_python3_fallback() {
    // Arrange
    let ctx = Ctx::new();

    // Act
    let run = ctx.push(
        &ctx.nouv,
        &[
            ("FAKE_COLLECT_RC", "1"),
            ("FAKE_COLLECT_OUT", &out_missing_module_parallel()),
        ],
    );

    // Assert
    assert!(run.passed(), "{}", run.text);
    assert!(run.text.contains("not collectable here"), "{}", run.text);
    assert!(!run.text.contains("PUSH BLOCKED"), "{}", run.text);
}

#[test]
fn collection_failure_not_a_missing_module_blocks_locked_and_fallback() {
    // Arrange
    let ctx = Ctx::new();

    // Act
    let locked = ctx.push(
        &ctx.fakebin,
        &[
            ("FAKE_COLLECT_RC", "2"),
            ("FAKE_COLLECT_OUT", &out_syntax_error()),
        ],
    );
    let fallback = ctx.push(
        &ctx.nouv,
        &[
            ("FAKE_COLLECT_RC", "2"),
            ("FAKE_COLLECT_OUT", &out_syntax_error()),
        ],
    );

    // Assert
    assert!(
        locked.failed(),
        "locked: a syntax error did not block: {}",
        locked.text
    );
    assert!(
        locked.text.contains("Python collection FAILED"),
        "{}",
        locked.text
    );
    assert!(
        fallback.failed(),
        "fallback: a syntax error did not block: {}",
        fallback.text
    );
    assert!(
        fallback.text.contains("Python collection FAILED"),
        "{}",
        fallback.text
    );
    assert!(
        !fallback.text.contains("not collectable here"),
        "{}",
        fallback.text
    );
}

#[test]
fn syntax_error_in_parallel_shape_blocks_locked_and_fallback() {
    // Arrange
    let ctx = Ctx::new();

    // Act
    let locked = ctx.push(
        &ctx.fakebin,
        &[
            ("FAKE_COLLECT_RC", "1"),
            ("FAKE_COLLECT_OUT", &out_syntax_error_parallel()),
        ],
    );
    let fallback = ctx.push(
        &ctx.nouv,
        &[
            ("FAKE_COLLECT_RC", "1"),
            ("FAKE_COLLECT_OUT", &out_syntax_error_parallel()),
        ],
    );

    // Assert
    assert!(
        locked.failed(),
        "locked: parallel-shape syntax error did not block: {}",
        locked.text
    );
    assert!(
        locked.text.contains("Python collection FAILED"),
        "{}",
        locked.text
    );
    assert!(
        fallback.failed(),
        "fallback: parallel-shape syntax error did not block: {}",
        fallback.text
    );
    assert!(
        !fallback.text.contains("not collectable here"),
        "{}",
        fallback.text
    );
}

#[test]
fn failing_test_that_prints_no_module_named_blocks_on_python3_fallback() {
    // Arrange
    let ctx = Ctx::new();

    // Act
    let run = ctx.push(
        &ctx.nouv,
        &[
            ("FAKE_COLLECT_RC", "1"),
            ("FAKE_COLLECT_OUT", &out_failing_test()),
        ],
    );

    // Assert
    assert!(run.failed(), "a failing test was skipped: {}", run.text);
    assert!(!run.text.contains("not collectable here"), "{}", run.text);
    assert!(run.text.contains("PUSH BLOCKED"), "{}", run.text);
}

#[test]
fn missing_module_next_to_failing_test_blocks_on_python3_fallback() {
    // Arrange
    let ctx = Ctx::new();

    // Act
    let run = ctx.push(
        &ctx.nouv,
        &[
            ("FAKE_COLLECT_RC", "1"),
            ("FAKE_COLLECT_OUT", &out_collection_and_failure()),
        ],
    );

    // Assert
    assert!(
        run.failed(),
        "a failing test was hidden by a collection skip: {}",
        run.text
    );
    assert!(!run.text.contains("not collectable here"), "{}", run.text);
    assert!(run.text.contains("PUSH BLOCKED"), "{}", run.text);
}

#[test]
fn conftest_that_will_not_load_skips_on_fallback_and_blocks_locked() {
    // Arrange
    let ctx = Ctx::new();

    // Act
    let fallback = ctx.push(
        &ctx.nouv,
        &[
            ("FAKE_COLLECT_RC", "4"),
            ("FAKE_COLLECT_OUT", &out_conftest_missing()),
        ],
    );
    let locked = ctx.push(
        &ctx.fakebin,
        &[
            ("FAKE_COLLECT_RC", "4"),
            ("FAKE_COLLECT_OUT", &out_conftest_missing()),
        ],
    );

    // Assert
    assert!(
        fallback.passed(),
        "fallback did not skip a conftest missing a module: {}",
        fallback.text
    );
    assert!(
        fallback.text.contains("not collectable here"),
        "{}",
        fallback.text
    );
    assert!(
        fallback.text.contains("No module named 'nope'"),
        "{}",
        fallback.text
    );
    assert!(
        locked.failed(),
        "locked environment did not block a conftest missing a module: {}",
        locked.text
    );
    assert!(
        locked.text.contains("Python collection FAILED"),
        "{}",
        locked.text
    );
}

#[test]
fn fast_lane_pytest_fast_and_exactly_the_changed_paths_reach_pytest() {
    // Arrange: the dev base, then two commits of the push; only their paths are expected.
    let ctx = Ctx::new();
    let base = ctx.commit_file("platform/tests/existing_test.py", "base");
    ctx.git(&["update-ref", "refs/remotes/origin/dev", &base]);
    ctx.commit_file("platform/tests/test_new.py", "new-test");
    let tip = ctx.commit_file("platform/lib/helper.sh", "helper");
    let copy = ctx.t().join("fast-list.txt");

    // Act
    let run = ctx.run_hook(
        &tip,
        &ctx.fakebin,
        ("origin", URL),
        &[("FAKE_FAST_COPY", copy.to_str().expect("a UTF-8 path"))],
    );

    // Assert
    assert!(run.passed(), "{}", run.text);
    assert!(
        read(&ctx.py_log).contains("pytest PYTEST_FAST=1 PYTEST_FAST_CHANGED="),
        "{}",
        read(&ctx.py_log)
    );
    assert!(run.text.contains("FAST LANE"), "{}", run.text);
    assert!(run.text.contains("CI runs the full suite"), "{}", run.text);
    // The list is exactly the two paths the push changed: not the base's file, no extra line,
    // and byte for byte (no carriage return, final newline present), so no `terminated()` here.
    let list = read(&copy);
    assert_eq!(
        list, "platform/lib/helper.sh\nplatform/tests/test_new.py\n",
        "changed list was {list:?}"
    );
}

#[test]
fn fast_lane_push_with_no_measurable_base_runs_full_suite_and_says_why() {
    // Arrange
    let ctx = Ctx::new();
    let base = ctx.commit_file("platform/tests/existing_test.py", "base");
    ctx.git(&["update-ref", "refs/remotes/origin/dev", &base]);

    // Act: the pushed sha does not exist, so there is no merge base.
    let run = ctx.push(&ctx.fakebin, &[]);

    // Assert
    assert!(run.passed(), "{}", run.text);
    assert!(
        read(&ctx.py_log).contains("pytest PYTEST_FAST=unset PYTEST_FAST_CHANGED=unset"),
        "{}",
        read(&ctx.py_log)
    );
    assert!(
        run.text.contains("full Python suite, not the fast lane"),
        "{}",
        run.text
    );
    assert!(run.text.contains("no merge base"), "{}", run.text);
}

#[test]
fn failing_bats_run_blocks_the_push_even_when_pytest_passes() {
    // Arrange
    let ctx = Ctx::new();

    // Act
    let run = ctx.push(&ctx.fakebin, &[("FAKE_BATS_RC", "1")]);

    // Assert
    assert!(
        run.failed(),
        "a failing BATS run did not block: {}",
        run.text
    );
    assert!(run.text.contains("PUSH BLOCKED"), "{}", run.text);
    assert!(
        read(&ctx.py_log).contains("pytest PYTEST_FAST"),
        "{}",
        read(&ctx.py_log)
    );
    assert!(!run.text.contains("Python suite FAILED"), "{}", run.text);
}

#[test]
fn bats_takes_ci_flags_when_gnu_parallel_is_installed() {
    // Arrange
    let ctx = Ctx::new();
    let args = ctx.t().join("bats.args");

    // Act
    let run = ctx.push(
        &ctx.fakebin,
        &[("FAKE_BATS_ARGS", args.to_str().expect("a UTF-8 path"))],
    );

    // Assert
    assert!(run.passed(), "{}", run.text);
    // Exact, one argument per line: an extra empty argument shows as an empty line.
    assert_eq!(
        read(&args),
        "--jobs\n4\n--no-parallelize-within-files\nplatform/tests/\n"
    );
}

#[test]
fn without_gnu_parallel_bats_runs_serially_with_a_note() {
    // The BATS file skipped when GNU parallel was installed system-wide, because its fallback PATH
    // (`nouv:/usr/bin:/bin`) found it. This fallback PATH leaves parallel out (`NOUV_EXCLUDED`),
    // so the case could run there; the skip stays because a port keeps its original's skips
    // (docs/MISTAKES.md 2.40), and it says so on stderr.
    if SYSTEM_DIRS
        .iter()
        .any(|dir| Path::new(dir).join("parallel").exists())
    {
        eprintln!("skipped: GNU parallel is installed system-wide, so the fallback PATH finds it");
        return;
    }
    // Arrange
    let ctx = Ctx::new();
    let args = ctx.t().join("bats.args");

    // Act
    let run = ctx.push(
        &ctx.nouv,
        &[("FAKE_BATS_ARGS", args.to_str().expect("a UTF-8 path"))],
    );

    // Assert
    assert!(run.passed(), "{}", run.text);
    // Exact, one argument per line: an extra empty argument shows as an empty line.
    assert_eq!(read(&args), "platform/tests/\n");
    assert!(
        run.text.contains("GNU parallel is not installed"),
        "{}",
        run.text
    );
}

#[test]
fn bats_is_already_running_while_pytest_runs() {
    // Arrange: a two-way handshake. The fake bats holds its mark until the fake pytest has seen
    // it and released it, then removes the mark, so a hook that runs the suites one after the
    // other leaves pytest no mark to see (the original BATS test's mark outlived bats).
    let ctx = Ctx::new();
    let mark = ctx.t().join("bats.started");
    let release = ctx.t().join("bats.release");

    // Act
    let run = ctx.push(
        &ctx.fakebin,
        &[
            ("FAKE_BATS_MARK", mark.to_str().expect("a UTF-8 path")),
            ("FAKE_WAIT", mark.to_str().expect("a UTF-8 path")),
            ("FAKE_BATS_RELEASE", release.to_str().expect("a UTF-8 path")),
        ],
    );

    // Assert
    assert!(run.passed(), "{}", run.text);
    assert!(
        read(&ctx.py_log).contains("bats-started-during-pytest"),
        "{}",
        read(&ctx.py_log)
    );
}

#[test]
fn a_finished_run_leaves_no_temporary_file_behind() {
    // Arrange
    let ctx = Ctx::new();
    let seen = ctx.t().join("scratch-seen");

    // Act
    let run = ctx.push(
        &ctx.fakebin,
        &[("FAKE_BATS_SEEN", seen.to_str().expect("a UTF-8 path"))],
    );

    // Assert
    assert!(run.passed(), "{}", run.text);
    let during = read(&seen);
    assert!(
        any_line(&during, r"^tmp\."),
        "the scratch directory did not exist while BATS ran: {during:?}"
    );
    let left: Vec<_> = std::fs::read_dir(&ctx.tmp)
        .expect("the temporary directory reads")
        .map(|e| e.expect("an entry").path())
        .collect();
    assert!(left.is_empty(), "left behind: {left:?}");
}

/// The hook's push held inside its suite by the fake bats, on a thread. `finish` releases the
/// suite and returns the hook's output. Dropping it unfinished releases and joins it too, so a
/// failed assertion never leaves the hook running.
struct HeldPush {
    release: PathBuf,
    handle: Option<JoinHandle<Output>>,
}

impl HeldPush {
    fn finish(&mut self) -> Output {
        std::fs::write(&self.release, "").expect("the release file is written");
        self.handle
            .take()
            .expect("the push is finished once")
            .join()
            .expect("the held push did not panic")
    }
}

impl Drop for HeldPush {
    fn drop(&mut self) {
        if let Some(handle) = self.handle.take() {
            let _ = std::fs::write(&self.release, "");
            let _ = handle.join();
        }
    }
}

/// Runs a push in the background with the fake bats held open. Returns the hook pid, the bats pid
/// and the held push. The guard exists before the wait for the pids, so a failed wait releases it.
fn held_push(ctx: &Ctx, envs: &[(&str, &str)]) -> (String, String, HeldPush) {
    let pids = ctx.t().join("bats.pids");
    let release = ctx.t().join("release");
    let mut all = vec![
        ("FAKE_BATS_PIDS", pids.to_str().expect("a UTF-8 path")),
        ("FAKE_BATS_HOLD", release.to_str().expect("a UTF-8 path")),
    ];
    all.extend_from_slice(envs);
    let handle = ctx.spawn_held(&all);
    let held = HeldPush {
        release,
        handle: Some(handle),
    };
    // The bound is 20 s: a 10 s bound timed out in four of six full-module runs on this machine,
    // and the cause is not identified. `spawn_held`'s 60 s bound caps the held
    // hook.
    let deadline = Instant::now() + Duration::from_secs(20);
    let (hook, bats) = loop {
        let text = read_or_empty(&pids);
        let mut parts = text.split_whitespace();
        if let (Some(hook), Some(bats)) = (parts.next(), parts.next()) {
            break (hook.to_owned(), bats.to_owned());
        }
        assert!(
            Instant::now() < deadline,
            "BATS never started; the hook's python log: {:?}; its bats log: {:?}",
            read_or_empty(&ctx.py_log),
            read_or_empty(&ctx.bats_log)
        );
        thread::sleep(Duration::from_millis(10));
    };
    (hook, bats, held)
}

/// Sends `signal` (a name such as TERM) to `pid` through the shell's `kill`.
fn send_signal(home: &Path, signal: &str, pid: &str) -> Output {
    process::scrubbed("sh", home)
        .arg("-c")
        .arg("kill -\"$2\" \"$1\"")
        .arg("sh")
        .arg(pid)
        .arg(signal)
        .output()
        .expect("sh starts")
}

/// The pid the fake bats writes to `file`, once it is there (bounded wait).
fn wait_for_pid(file: &Path) -> String {
    // The fake writes this pid after the pids line that held_push already waited for, so the
    // bound covers only the gap between two writes by one shell. 20 s matches held_push's bound;
    // a file still missing after it means the fake never reached that line.
    let deadline = Instant::now() + Duration::from_secs(20);
    loop {
        let text = read_or_empty(file);
        if let Some(pid) = text.split_whitespace().next() {
            return pid.to_owned();
        }
        assert!(
            Instant::now() < deadline,
            "BATS never wrote its child's pid"
        );
        thread::sleep(Duration::from_millis(100));
    }
}

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

fn wait_until_gone(home: &Path, pid: &str, bound: Duration) -> bool {
    let deadline = Instant::now() + bound;
    while alive(home, pid) {
        if Instant::now() >= deadline {
            return false;
        }
        thread::sleep(Duration::from_millis(100));
    }
    true
}

#[test]
fn term_stops_the_bats_job_removes_temporary_files_and_frees_the_lock() {
    // Arrange: the fake bats holds the suite open until released; the hook is then signalled.
    let ctx = Ctx::new();
    let seen = ctx.t().join("scratch-seen");
    let (hook_pid, bats_pid, mut held) = held_push(
        &ctx,
        &[("FAKE_BATS_SEEN", seen.to_str().expect("a UTF-8 path"))],
    );
    let during = read(&seen);
    assert!(
        any_line(&during, r"^tmp\."),
        "the scratch directory did not exist while BATS ran: {during:?}"
    );
    // The lock this push holds, proved present and owned by the hook before TERM, so its absence
    // afterwards is the hook's release and not a lock that was never taken here.
    let locks = ctx.repo.join(".git/pre-push-locks");
    let lock_paths = |when: &str| -> Vec<PathBuf> {
        std::fs::read_dir(&locks)
            .unwrap_or_else(|e| panic!("the lock directory does not read {when}: {e}"))
            .map(|e| e.expect("entry").path())
            .collect()
    };
    let before = lock_paths("before TERM");
    assert_eq!(before.len(), 1, "the push holds no single lock: {before:?}");
    let owner = read(&before[0]);
    assert_eq!(
        owner.lines().next(),
        Some(hook_pid.as_str()),
        "the lock is not the hook's: {owner:?}"
    );

    // Act
    let term = send_signal(ctx.t(), "TERM", &hook_pid);
    let hook_gone = wait_until_gone(ctx.t(), &hook_pid, Duration::from_secs(5));
    let bats_gone = wait_until_gone(ctx.t(), &bats_pid, Duration::from_secs(5));
    held.finish();

    // Assert
    assert!(term.status.success(), "the hook could not be signalled");
    assert!(hook_gone, "the hook survived TERM");
    assert!(
        bats_gone,
        "the BATS job survived TERM to the hook (pid {bats_pid})"
    );
    let left: Vec<_> = std::fs::read_dir(&ctx.tmp)
        .expect("tmp reads")
        .map(|e| e.expect("entry").path())
        .collect();
    assert!(left.is_empty(), "left behind: {left:?}");
    let held_locks = lock_paths("after TERM");
    assert!(held_locks.is_empty(), "lock left behind: {held_locks:?}");
}

#[test]
fn a_push_to_a_url_with_credentials_never_prints_the_url() {
    // git passes the URL itself as the remote name when a push names a URL, and that URL can carry
    // a token. The fallback message must name the remote without it. The URL is assembled from
    // two literals so this file holds no credential-shaped URI for the secret scan to match.
    let ctx = Ctx::new();
    let url = concat!("https://user:SECRET", "-TOKEN@example.invalid/repo.git");

    // Act: the URL is both the remote name and its URL, as git runs the hook for a URL push.
    let run = ctx.run_hook(SHA, &ctx.fakebin, (url, url), &[]);

    // Assert
    // The messages never print the output: it would carry the URL when the hook leaks it.
    assert!(
        run.text.contains("full Python suite, not the fast lane"),
        "the full-suite message is missing"
    );
    assert!(
        run.text.contains("refs/remotes/<url>/dev"),
        "the remote is not named <url>"
    );
    assert!(!run.text.contains("SECRET-TOKEN"), "the URL was printed");
}

#[test]
fn python3_without_pytest_skips_on_fallback_and_blocks_on_locked() {
    // Arrange
    let ctx = Ctx::new();

    // Act
    let fallback = ctx.push(&ctx.nouv, &[("FAKE_NO_PYTEST", "1")]);
    let locked = ctx.push(&ctx.fakebin, &[("FAKE_NO_PYTEST", "1")]);

    // Assert
    assert!(fallback.passed(), "fallback blocked: {}", fallback.text);
    assert!(
        fallback.text.contains("not collectable here"),
        "{}",
        fallback.text
    );
    assert!(
        fallback.text.contains("No module named pytest"),
        "{}",
        fallback.text
    );
    assert!(
        locked.failed(),
        "locked environment passed: {}",
        locked.text
    );
    assert!(
        locked
            .text
            .contains("pytest is not importable on the locked uv environment"),
        "{}",
        locked.text
    );
}

#[test]
fn one_missing_module_does_not_forgive_a_syntax_error_in_another_file() {
    // Arrange
    let ctx = Ctx::new();
    let out = terminated(&[
        "_______________________ ERROR collecting test_a.py ________________________",
        "E   ModuleNotFoundError: No module named 'nope'",
        "_______________________ ERROR collecting test_b.py ________________________",
        "E   SyntaxError: invalid syntax",
        "=========================== short test summary info ============================",
        "ERROR test_a.py - ImportError while importing test module",
        "ERROR test_b.py - SyntaxError: invalid syntax",
        "1 passed, 2 errors in 0.70s",
    ]);

    // Act
    let run = ctx.push(
        &ctx.nouv,
        &[("FAKE_COLLECT_RC", "1"), ("FAKE_COLLECT_OUT", &out)],
    );

    // Assert
    assert!(run.failed(), "a syntax error was forgiven: {}", run.text);
    assert!(!run.text.contains("not collectable here"), "{}", run.text);
}

#[test]
fn a_full_run_that_collects_no_tests_blocks() {
    // Arrange
    let ctx = Ctx::new();

    // Act
    let run = ctx.push(
        &ctx.fakebin,
        &[
            ("FAKE_COLLECT_RC", "5"),
            ("FAKE_COLLECT_OUT", "no tests ran in 0.01s"),
        ],
    );

    // Assert
    assert!(run.failed(), "an empty full run passed: {}", run.text);
    assert!(
        run.text
            .contains("collected no tests (exit 5) on a full run"),
        "{}",
        run.text
    );
}

#[test]
fn term_kills_the_processes_bats_started_not_only_bats_itself() {
    // Arrange: the fake bats starts a long-lived child. It is alive before TERM, so its absence
    // afterwards is the hook's doing.
    let ctx = Ctx::new();
    let child_file = ctx.t().join("bats.child");
    let (hook_pid, _bats_pid, mut held) = held_push(
        &ctx,
        &[(
            "FAKE_BATS_CHILD",
            child_file.to_str().expect("a UTF-8 path"),
        )],
    );
    let child = wait_for_pid(&child_file);
    assert!(
        alive(ctx.t(), &child),
        "the child BATS started was not alive before TERM"
    );

    // Act
    let term = send_signal(ctx.t(), "TERM", &hook_pid);
    let child_gone = wait_until_gone(ctx.t(), &child, Duration::from_secs(10));
    if !child_gone {
        // Do not leave the long-lived child running behind a failing case.
        let _ = send_signal(ctx.t(), "KILL", &child);
    }
    held.finish();

    // Assert
    assert!(term.status.success(), "the hook could not be signalled");
    assert!(
        child_gone,
        "a process BATS started (pid {child}) outlived TERM to the hook"
    );
}

/// The fake lane script the hook calls, recording its argument in `args`.
fn fake_lane(ctx: &Ctx, args: &Path) {
    let dir = ctx.repo.join("platform/tests");
    let body = FAKE_LANE.replace("@LANE_ARGS@", &args.display().to_string());
    write_executable(&dir.join("lane.sh"), &body);
}

#[test]
fn the_push_lane_of_the_rust_suite_runs() {
    // Arrange
    let ctx = Ctx::new();
    let args = ctx.t().join("lane.args");
    fake_lane(&ctx, &args);

    // Act
    let run = ctx.push(&ctx.fakebin, &[]);

    // Assert
    assert!(run.passed(), "{}", run.text);
    // Exact, one argument per line: an extra empty argument shows as an empty line.
    assert_eq!(
        read(&args),
        "push\n",
        "lane.sh was called with the wrong lane"
    );
}

#[test]
fn a_failing_rust_lane_blocks_the_push_even_when_the_other_suites_pass() {
    // Arrange
    let ctx = Ctx::new();
    let args = ctx.t().join("lane.args");
    fake_lane(&ctx, &args);

    // Act
    let run = ctx.push(&ctx.fakebin, &[("FAKE_LANE_RC", "1")]);

    // Assert
    assert!(
        run.failed(),
        "a failing Rust lane did not block: {}",
        run.text
    );
    assert!(run.text.contains("Rust suite FAILED"), "{}", run.text);
    assert!(run.text.contains("PUSH BLOCKED"), "{}", run.text);
}

#[test]
fn the_no_uv_path_links_every_system_tool_but_uv_uvx_and_parallel() {
    // Arrange: a stand-in system directory holding uv, as /usr/bin does where a distribution
    // packages it there.
    let t = TempDir::new().expect("a temporary directory");
    let sys = t.path().join("sys");
    let linked = t.path().join("linked");
    std::fs::create_dir_all(&sys).expect("the system directory");
    std::fs::create_dir_all(&linked).expect("the link directory");
    for name in ["cksum", "git", "parallel", "uv", "uvx"] {
        write_executable(&sys.join(name), "#!/bin/sh\nexit 0\n");
    }

    // Act
    link_system_tools(&linked, &[sys.as_path()]);

    // Assert
    let mut names: Vec<String> = std::fs::read_dir(&linked)
        .expect("the link directory reads")
        .map(|e| {
            e.expect("an entry")
                .file_name()
                .to_string_lossy()
                .into_owned()
        })
        .collect();
    names.sort();
    assert_eq!(names, ["cksum", "git"]);
}
