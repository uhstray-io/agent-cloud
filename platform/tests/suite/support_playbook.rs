//! The confined `ansible-playbook` runner and the bounded child process under it (plan 24).
//!
//! Replaces `platform/tests/test_harness_sandbox.py`. That Python suite keeps its own copy until
//! its tests are ported; this module does not change it.
//!
//! Port map (Python test → Rust fn):
//!
//! | test_harness_sandbox.py | suite/support_playbook.rs |
//! |---|---|
//! | `test_run_timeout_suppresses_captured_child_output` | `run_timeout_suppresses_captured_child_output` |
//! | `test_run_enforces_subprocess_timeout` | `run_enforces_the_timeout` |
//! | `test_run_gives_the_child_no_stdin` | `run_gives_the_child_no_stdin` |
//! | `test_run_playbook_builds_the_command_environment_and_bound` (the command, its arguments and its explicit environment) | `ansible_playbook_builds_the_command_for_the_repository_root` |
//! | `test_run_playbook_builds_the_command_environment_and_bound` (the ANSIBLE_CONFIG drop, as the child sees it) | `the_environment_reaches_the_playbook_as_specified` |
//! | `test_run_playbook_runs_a_real_playbook` | `ansible_playbook_runs_a_real_playbook` |
//! | `test_env_for_keeps_the_callers_collections_visible` | `playbook_env_keeps_the_callers_collections_visible` |
//! | `test_a_playbook_run_through_the_harness_resolves_an_installed_collection` | `a_playbook_run_resolves_an_installed_collection` |
//! | (no Python counterpart: the confinement proof) | `a_sandboxed_playbook_cannot_write_outside_its_directory` |
//!
//! Tests here run `ansible-playbook`, so the `playbook` test-group caps them (`.config/nextest.toml`).

use std::collections::BTreeMap;
use std::ffi::OsString;
use std::path::Path;
use std::time::{Duration, Instant};

use agent_cloud_tests::{process, repo};
use tempfile::TempDir;

/// The message a panic carries, whatever its payload type.
fn panic_message(call: impl FnOnce()) -> String {
    let payload = std::panic::catch_unwind(std::panic::AssertUnwindSafe(call))
        .expect_err("the call was expected to panic");
    payload
        .downcast_ref::<String>()
        .cloned()
        .or_else(|| payload.downcast_ref::<&str>().map(|s| (*s).to_owned()))
        .expect("a panic with a message")
}

/// A playbook of one debug task, written into `dir`.
fn write_play(dir: &Path, name: &str, tasks: &str) -> String {
    let path = dir.join(name);
    std::fs::write(
        &path,
        format!(
            "- hosts: localhost\n  gather_facts: false\n  connection: local\n  tasks:\n{tasks}"
        ),
    )
    .expect("write the playbook");
    path.display().to_string()
}

fn stdout_and_stderr(out: &std::process::Output) -> String {
    format!(
        "{}{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    )
}

/// A caller environment that holds exactly `vars`.
fn caller(vars: &'static [(&'static str, &'static str)]) -> impl Fn(&str) -> Option<OsString> {
    move |name| {
        vars.iter()
            .find(|(key, _)| *key == name)
            .map(|(_, value)| OsString::from(value))
    }
}

#[test]
fn run_timeout_suppresses_captured_child_output() {
    let home = TempDir::new().unwrap();
    let secret = "tempo-fixture-secret-must-not-escape";
    let mut cmd = process::scrubbed("sh", home.path());
    cmd.arg("-c")
        .arg(format!("echo {secret}; echo {secret} >&2; exec sleep 5"));

    let message = panic_message(|| {
        process::run_for(&mut cmd, Duration::from_millis(500));
    });

    assert_eq!(
        message,
        "test command timed out after 0.5s; captured output suppressed"
    );
    assert!(
        !message.contains(secret),
        "the child's output reached the panic"
    );
}

#[test]
fn run_enforces_the_timeout() {
    // `exec` makes sleep the direct child: no grandchild holds the output pipes open.
    let home = TempDir::new().unwrap();
    let mut cmd = process::scrubbed("sh", home.path());
    cmd.arg("-c").arg("exec sleep 5");

    let started = Instant::now();
    let message = panic_message(|| {
        process::run_for(&mut cmd, Duration::from_millis(100));
    });

    assert_eq!(
        message,
        "test command timed out after 0.1s; captured output suppressed"
    );
    assert!(
        started.elapsed() < Duration::from_secs(2),
        "the child outlived its bound by {:?}",
        started.elapsed()
    );
}

#[test]
fn run_gives_the_child_no_stdin() {
    // A prompt must fail at once. Without the closed stdin, `read` would wait out the 10 s bound.
    let home = TempDir::new().unwrap();
    let mut cmd = process::scrubbed("sh", home.path());
    cmd.arg("-c").arg("read -r line; echo rc=$?");

    let started = Instant::now();
    let out = process::run_for(&mut cmd, Duration::from_secs(10));

    assert!(out.status.success(), "{}", stdout_and_stderr(&out));
    assert_eq!(String::from_utf8_lossy(&out.stdout).trim(), "rc=1");
    assert!(started.elapsed() < Duration::from_secs(5));
}

#[test]
fn ansible_playbook_builds_the_command_for_the_repository_root() {
    let dir = TempDir::new().unwrap();
    let cmd = process::ansible_playbook(dir.path(), "inv.yml", "platform/playbooks/x.yml");

    let args: Vec<String> = cmd
        .get_args()
        .map(|arg| arg.to_string_lossy().into_owned())
        .collect();
    assert!(
        args.ends_with(&[
            "-i".to_owned(),
            "inv.yml".to_owned(),
            "platform/playbooks/x.yml".to_owned()
        ]),
        "arguments: {args:?}"
    );
    let program = cmd.get_program().to_string_lossy().into_owned();
    assert_eq!(
        program,
        process::sandbox().unwrap_or("ansible-playbook"),
        "the program is ansible-playbook or the sandbox that wraps it"
    );
    assert_eq!(cmd.get_current_dir(), Some(repo::root()));

    let envs: BTreeMap<String, String> = cmd
        .get_envs()
        .filter_map(|(key, value)| {
            Some((
                key.to_string_lossy().into_owned(),
                value?.to_string_lossy().into_owned(),
            ))
        })
        .collect();
    assert_eq!(envs.get("ANSIBLE_NOCOLOR").map(String::as_str), Some("1"));
    assert!(
        !envs.contains_key("ANSIBLE_CONFIG"),
        "the repository's ansible.cfg applies"
    );
    let local = dir.path().join(".ansible-state").join("local");
    assert_eq!(
        envs.get("ANSIBLE_LOCAL_TEMP").map(String::as_str),
        Some(local.to_str().unwrap())
    );
    assert!(local.is_dir(), "Ansible's state directory is created");
}

#[test]
fn ansible_playbook_runs_a_real_playbook() {
    let dir = TempDir::new().unwrap();
    let play = write_play(
        dir.path(),
        "play.yml",
        "    - ansible.builtin.debug: {msg: \"ran-from-the-shared-runner\"}\n",
    );

    let out = process::run(&mut process::ansible_playbook(
        dir.path(),
        "localhost,",
        &play,
    ));

    assert!(out.status.success(), "{}", stdout_and_stderr(&out));
    assert!(String::from_utf8_lossy(&out.stdout).contains("ran-from-the-shared-runner"));
}

#[test]
fn the_environment_reaches_the_playbook_as_specified() {
    // A caller that has ANSIBLE_CONFIG set must not pass it on. The playbook reads what the child
    // sees, so an inherited variable shows up here and not only in the Command's explicit list.
    // SAFETY: nextest runs each test in its own process, so no other thread reads the environment
    // while this one sets it (edition 2024 makes set_var unsafe).
    unsafe { std::env::set_var("ANSIBLE_CONFIG", "/elsewhere/ansible.cfg") };
    let dir = TempDir::new().unwrap();
    let play = write_play(
        dir.path(),
        "play.yml",
        concat!(
            "    - ansible.builtin.debug:\n        msg: \"cfg=[{{ lookup('env','ANSIBLE_CONFIG') }}] ",
            "local=[{{ lookup('env','ANSIBLE_LOCAL_TEMP') }}] ",
            "home=[{{ lookup('env','HOME') }}] ",
            "nocolor=[{{ lookup('env','ANSIBLE_NOCOLOR') }}]\"\n",
        ),
    );

    let out = process::run(&mut process::ansible_playbook(
        dir.path(),
        "localhost,",
        &play,
    ));

    let text = stdout_and_stderr(&out);
    assert!(out.status.success(), "{text}");
    let local = dir.path().join(".ansible-state").join("local");
    assert!(
        text.contains("cfg=[]"),
        "ANSIBLE_CONFIG reached the child: {text}"
    );
    assert!(
        text.contains(&format!("local=[{}]", local.display())),
        "Ansible's local temp is not under the run's directory: {text}"
    );
    assert!(
        text.contains(&format!("home=[{}]", dir.path().display())),
        "HOME is not the run's directory: {text}"
    );
    assert!(text.contains("nocolor=[1]"), "{text}");
}

#[test]
fn playbook_env_keeps_the_callers_collections_visible() {
    // The pure builder takes the caller's variables as input, so no environment is touched.
    let run_dir = Path::new("/run/w");
    let value = |env: &[(&str, OsString)], key: &str| -> OsString {
        env.iter()
            .find(|(k, _)| *k == key)
            .map(|(_, v)| v.clone())
            .unwrap_or_else(|| panic!("{key} is not set"))
    };

    let env = process::playbook_env(run_dir, caller(&[("ANSIBLE_HOME", "/caller/ansible-home")]));
    assert_eq!(
        value(&env, "ANSIBLE_COLLECTIONS_PATH"),
        OsString::from("/caller/ansible-home/collections:/usr/share/ansible/collections")
    );
    assert_eq!(
        value(&env, "ANSIBLE_HOME"),
        OsString::from("/run/w/.ansible-state/home"),
        "Ansible's own state still moves into the run's directory"
    );

    let env = process::playbook_env(run_dir, caller(&[("HOME", "/caller/user-home")]));
    assert_eq!(
        value(&env, "ANSIBLE_COLLECTIONS_PATH"),
        OsString::from("/caller/user-home/.ansible/collections:/usr/share/ansible/collections"),
        "without ANSIBLE_HOME, the default under the caller's home"
    );

    let env = process::playbook_env(
        run_dir,
        caller(&[("ANSIBLE_COLLECTIONS_PATH", "/explicit/collections")]),
    );
    assert_eq!(
        value(&env, "ANSIBLE_COLLECTIONS_PATH"),
        OsString::from("/explicit/collections")
    );
    assert!(env.iter().all(|(key, _)| *key != "ANSIBLE_CONFIG"));
}

#[test]
fn a_playbook_run_resolves_an_installed_collection() {
    // ansible.posix is installed for the suite (collections/requirements.yml). Before the
    // collections path was kept, this run could not resolve the module.
    let dir = TempDir::new().unwrap();
    let play = write_play(
        dir.path(),
        "play.yml",
        "    - ansible.posix.authorized_key:\n        user: nobody\n        key: x\n      \
         check_mode: true\n      failed_when: false\n",
    );

    let out = process::run(
        process::ansible_playbook(dir.path(), "localhost,", &play).arg("--syntax-check"),
    );

    assert!(out.status.success(), "{}", stdout_and_stderr(&out));
}

#[test]
fn a_sandboxed_playbook_cannot_write_outside_its_directory() {
    // On macOS sandbox-exec ships with the system, so a detector that finds none is broken, and
    // the proof below must not pass for that reason. Elsewhere a missing sandbox is expected: the
    // Linux CI runner has no bwrap, and the proof then does not run, as harness_sandbox.py documents.
    if cfg!(target_os = "macos") {
        assert_eq!(
            process::sandbox(),
            Some("sandbox-exec"),
            "on macOS the confinement proof needs sandbox-exec, and sandbox() must find it"
        );
    }
    let Some(kind) = process::sandbox() else {
        return;
    };
    let dir = TempDir::new().unwrap();
    let outside = repo::path(".sandbox-probe-playbook");
    assert!(
        !outside.exists(),
        "{} exists before the run, so the run cannot prove anything",
        outside.display()
    );
    let inside = dir.path().join("inside");
    let touch = |path: &Path| {
        format!(
            "    - ansible.builtin.file: {{path: \"{}\", state: touch}}\n",
            path.display()
        )
    };
    let tasks = format!("{}{}", touch(&inside), touch(&outside));
    let play = write_play(dir.path(), "play.yml", &tasks);

    let out = process::run(&mut process::ansible_playbook(
        dir.path(),
        "localhost,",
        &play,
    ));

    let escaped = outside.exists();
    if escaped {
        // Remove only the one file this run was supposed to be unable to create.
        let _ = std::fs::remove_file(&outside);
    }
    assert!(
        !escaped,
        "{kind} let a playbook write {}",
        outside.display()
    );
    assert!(
        !out.status.success(),
        "the write outside the run's directory should fail the run: {}",
        stdout_and_stderr(&out)
    );
    assert!(
        dir.path().join("inside").exists(),
        "the write inside the run's directory must succeed first, so the playbook really ran"
    );
}
