//! The process helpers of `src/lib.rs` (`process`): `output_text` and `fake_bin`, the Rust forms of
//! the stdout-then-stderr join and of `harness_sandbox.py`'s `fake_bin`. The fake-bin test runs its
//! double through `process::run`, as every child a test starts does (03 Part 1, rule 11).

use std::os::unix::fs::PermissionsExt;
use std::os::unix::process::ExitStatusExt;
use std::path::PathBuf;
use std::process::{ExitStatus, Output};

use agent_cloud_tests::process::{fake_bin, output_text, run, scrubbed};
use tempfile::TempDir;

fn output(stdout: &[u8], stderr: &[u8]) -> Output {
    Output {
        status: ExitStatus::from_raw(0),
        stdout: stdout.to_vec(),
        stderr: stderr.to_vec(),
    }
}

#[test]
fn output_text_joins_stdout_then_stderr_with_nothing_between_them() {
    assert_eq!(output_text(&output(b"one\n", b"two\n")), "one\ntwo\n");
    assert_eq!(output_text(&output(b"out", b"err")), "outerr");
}

#[test]
fn output_text_replaces_bytes_that_are_not_utf8() {
    assert_eq!(
        output_text(&output(b"a\xffb", b"\xc3")),
        "a\u{fffd}b\u{fffd}"
    );
}

#[test]
fn fake_bin_runs_each_script_as_a_program_on_the_returned_path() {
    let dir = TempDir::new().unwrap();
    let path = fake_bin(dir.path(), &[("ac-fake-hello", "echo from-the-double\n")]);
    let mut cmd = scrubbed("sh", dir.path());
    cmd.env("PATH", path).arg("-c").arg("ac-fake-hello");
    let out = run(&mut cmd);
    assert!(out.status.success(), "{}", output_text(&out));
    assert_eq!(output_text(&out), "from-the-double\n");
}

#[test]
fn fake_bin_writes_a_sh_script_with_mode_0755() {
    let dir = TempDir::new().unwrap();
    fake_bin(dir.path(), &[("ac-fake-mode", "echo x\n")]);
    let script = dir.path().join("fakebin").join("ac-fake-mode");
    assert_eq!(
        std::fs::read_to_string(&script).unwrap(),
        "#!/bin/sh\necho x\n"
    );
    let mode = std::fs::metadata(&script).unwrap().permissions().mode() & 0o777;
    assert_eq!(mode, 0o755);
}

#[test]
fn fake_bin_puts_its_directory_first_and_the_inherited_path_after_it() {
    let dir = TempDir::new().unwrap();
    let path = fake_bin(dir.path(), &[("ac-fake-order", "true\n")]);
    let entries: Vec<PathBuf> = std::env::split_paths(&path).collect();
    let inherited = std::env::var_os("PATH").expect("the test runner sets PATH");
    assert_eq!(entries[0], dir.path().join("fakebin"));
    assert_eq!(
        entries[1..].to_vec(),
        std::env::split_paths(&inherited).collect::<Vec<_>>()
    );
}

#[test]
#[should_panic(expected = "fakebin: File exists")]
fn fake_bin_refuses_a_directory_it_already_made() {
    let dir = TempDir::new().unwrap();
    fake_bin(dir.path(), &[]);
    fake_bin(dir.path(), &[]);
}
