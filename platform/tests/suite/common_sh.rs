//! `platform/lib/common.sh`, the shared bash library every `deploy.sh` sources (cli tier).
//!
//! Ported from `platform/tests/test_common.bats` (plan 24, Phase 1 pilot). Each test sources
//! the library into a scrubbed `bash -c` and asserts on the process: status, stdout, stderr
//! and the files it wrote.

use std::os::unix::fs::PermissionsExt;

use agent_cloud_tests::process::bash_with;
use predicates::prelude::*;
use tempfile::TempDir;

const LIB: &str = "platform/lib/common.sh";

/// `script` with the library sourced, run in a fresh temporary directory, engine pinned to
/// docker so `detect_runtime` never probes the host.
fn sh(dir: &TempDir, script: &str) -> assert_cmd::Command {
    let mut cmd = bash_with(LIB, script, dir.path());
    cmd.current_dir(dir.path())
        .env("CONTAINER_ENGINE", "docker")
        .env("COMPOSE_CMD", "docker compose");
    cmd
}

fn stdout_of(cmd: &mut assert_cmd::Command) -> String {
    let out = cmd.assert().success().get_output().stdout.clone();
    String::from_utf8(out).expect("utf-8 stdout")
}

#[test]
fn gen_secret_default_is_at_most_32_chars_without_base64_punctuation() {
    let dir = TempDir::new().unwrap();

    // Captured with $(...), as every caller does: when `/` or `+` is stripped, openssl's
    // trailing newline falls inside `head -c` and the raw output ends in "\n".
    let out = stdout_of(&mut sh(&dir, r#"printf '%s\n' "$(gen_secret)""#));

    // Only deterministic properties (03 Part 1, rule 5): `head -c 32` bounds the length and
    // `tr -d '/+='` removes the punctuation. How many characters `tr` strips depends on the
    // random bytes, so no lower bound is asserted.
    let value = out.trim_end_matches('\n');
    assert!(!value.is_empty() && value.len() <= 32, "{value:?}");
    assert!(
        value.chars().all(|c| c.is_ascii_alphanumeric()),
        "{value:?} keeps characters outside [A-Za-z0-9]"
    );
}

#[test]
fn gen_secret_truncates_to_the_requested_length() {
    // 48 bytes encode to 64 characters, so even after `/+=` are stripped the output always
    // exceeds the requested length. A length near the encoded size would depend on the
    // random bytes (03 Part 1, rule 5).
    for (bytes, len) in [(48, 16), (48, 1)] {
        let dir = TempDir::new().unwrap();

        let out = stdout_of(&mut sh(&dir, &format!("gen_secret {bytes} {len}")));

        assert_eq!(out.len(), len, "gen_secret {bytes} {len} -> {out:?}");
    }
}

#[test]
fn needs_gen_is_true_only_for_empty_or_placeholder_values() {
    let cases = [
        ("", true),
        ("REPLACE_ME", true),
        ("changeme123", true),
        ("placeholder_value", true),
        ("s3cur3_p4ssw0rd", false),
        ("550e8400-e29b-41d4-a716-446655440000", false),
        // Prefix match only: a real value that merely contains the word is kept.
        ("my_placeholder", false),
    ];
    for (value, expected) in cases {
        let dir = TempDir::new().unwrap();

        let assert = sh(&dir, r#"needs_gen "$1""#).arg(value).assert();

        if expected {
            assert.success();
        } else {
            assert.failure();
        }
    }
}

#[test]
fn put_secret_writes_an_owner_only_file_that_get_secret_reads_back() {
    let dir = TempDir::new().unwrap();

    let out = stdout_of(&mut sh(
        &dir,
        r#"put_secret "$PWD" key1 val1 && get_secret "$PWD" key1"#,
    ));

    assert_eq!(out, "val1");
    let mode = std::fs::metadata(dir.path().join("key1.txt"))
        .unwrap()
        .permissions()
        .mode()
        & 0o777;
    assert_eq!(mode, 0o600, "mode {mode:o}");
}

#[test]
fn put_secret_overwrites_an_existing_value() {
    let dir = TempDir::new().unwrap();

    let out = stdout_of(&mut sh(
        &dir,
        r#"put_secret "$PWD" k old && put_secret "$PWD" k new && get_secret "$PWD" k"#,
    ));

    assert_eq!(out, "new");
}

#[test]
fn get_secret_of_a_missing_name_is_empty_and_succeeds() {
    let dir = TempDir::new().unwrap();

    let out = stdout_of(&mut sh(&dir, r#"get_secret "$PWD" nonexistent"#));

    assert_eq!(out, "");
}

#[test]
fn detect_runtime_keeps_a_preset_container_engine() {
    let dir = TempDir::new().unwrap();

    let out = stdout_of(
        sh(&dir, r#"detect_runtime; printf %s "$CONTAINER_ENGINE""#)
            .env("CONTAINER_ENGINE", "podman"),
    );

    assert_eq!(out, "podman");
}

#[test]
fn info_prints_to_stdout_and_warn_prints_warn_to_stderr_only() {
    let dir = TempDir::new().unwrap();

    sh(&dir, "info 'test message'; warn 'test warning'")
        .assert()
        .success()
        .stdout(
            predicate::str::contains("test message").and(predicate::str::contains("WARN").not()),
        )
        .stderr(predicate::str::contains("WARN: test warning"));
}

/// `compose` with `COMPOSE_CMD` stubbed to `echo`, so its argv is the output.
const ECHO_COMPOSE: &str = r#"detect_runtime() { COMPOSE_CMD=echo; }; compose up -d"#;

#[test]
fn compose_appends_the_local_overlay_only_with_local_mode_and_the_file() {
    // (LOCAL_MODE, compose.local.yml present, expected argv)
    let cases = [
        (None, false, "-f compose.yml up -d"),
        (None, true, "-f compose.yml up -d"),
        (
            Some("true"),
            true,
            "-f compose.yml -f compose.local.yml up -d",
        ),
        (Some("true"), false, "-f compose.yml up -d"),
    ];
    for (local_mode, overlay, expected) in cases {
        let dir = TempDir::new().unwrap();
        std::fs::write(dir.path().join("compose.yml"), "").unwrap();
        if overlay {
            std::fs::write(dir.path().join("compose.local.yml"), "").unwrap();
        }
        let mut cmd = sh(&dir, ECHO_COMPOSE);
        if let Some(v) = local_mode {
            cmd.env("LOCAL_MODE", v);
        }

        let out = stdout_of(&mut cmd);

        assert_eq!(
            out.trim_end(),
            expected,
            "LOCAL_MODE={local_mode:?} overlay={overlay}"
        );
    }
}

#[test]
fn compose_overlays_append_last_and_empty_changes_nothing() {
    // (LOCAL_MODE, COMPOSE_OVERLAYS, expected argv)
    let cases = [
        (
            None,
            "compose.search.yml",
            "-f compose.yml -f compose.search.yml up -d",
        ),
        (
            Some("true"),
            "compose.search.yml",
            "-f compose.yml -f compose.local.yml -f compose.search.yml up -d",
        ),
        (
            Some("true"),
            "",
            "-f compose.yml -f compose.local.yml up -d",
        ),
    ];
    for (local_mode, overlays, expected) in cases {
        let dir = TempDir::new().unwrap();
        for f in ["compose.yml", "compose.local.yml", "compose.search.yml"] {
            std::fs::write(dir.path().join(f), "").unwrap();
        }
        let mut cmd = sh(&dir, ECHO_COMPOSE);
        cmd.env("COMPOSE_OVERLAYS", overlays);
        if let Some(v) = local_mode {
            cmd.env("LOCAL_MODE", v);
        }

        let out = stdout_of(&mut cmd);

        assert_eq!(
            out.trim_end(),
            expected,
            "LOCAL_MODE={local_mode:?} COMPOSE_OVERLAYS={overlays:?}"
        );
    }
}

#[test]
fn compose_fails_naming_a_declared_overlay_that_does_not_exist() {
    // A gated topology that quietly vanishes reports success while deploying the wrong stack.
    let dir = TempDir::new().unwrap();
    for f in ["compose.yml", "compose.search.yml"] {
        std::fs::write(dir.path().join(f), "").unwrap();
    }

    sh(&dir, ECHO_COMPOSE)
        .env("COMPOSE_OVERLAYS", "compose.search.yml compose.absent.yml")
        .assert()
        .failure()
        .stderr(predicate::str::contains("compose.absent.yml"));
}
