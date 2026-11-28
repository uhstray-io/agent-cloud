//! The playbook tier of `platform/playbooks/deploy-mailpit.yml`'s local-only guards (AC-1123).
//!
//! Mailpit accepts every message and every AUTH credential, so Phase 1 refuses a run before the monorepo is placed in two
//! cases: a host of `mailpit_svc` that is not named `<name>-local`, and a host whose connection is not `local`.
//!
//! - A host not named `<name>-local` refuses the play, and the refusal names that host.
//! - Several hosts with one of them not named `<name>-local` refuse the play, and the refusal names only the host that is not.
//! - A host on a connection other than `local`, set in the inventory, refuses the play on the connection guard, and the
//!   refusal names the connection.
//! - A `<name>-local` host on the local connection passes both guards, so the run goes on to place the monorepo.
//!
//! Each refusal test asserts the specific refusal message and that the monorepo placement did not start, so an earlier
//! failure (an inventory or argument error) cannot pass for the guard.
//!
//! Each test runs the playbook, unchanged, under `--check` against a temporary inventory on the local connection
//! (`process::ansible_playbook`). The connection case sets `ansible_connection` in the inventory, not as an extra var,
//! because `refuse-internal-extra-vars.yml` refuses an extra var that sets it before this guard runs. The fixtures use
//! `mailpit-local` and `mailpit-prod` host names and `127.0.0.1` addresses, and no real host.

use agent_cloud_tests::{playbook, process, repo};
use serde_json::{Value, json};
use tempfile::TempDir;

const PLAYBOOK: &str = "platform/playbooks/deploy-mailpit.yml";
const HOST_REFUSAL: &str = "Refusing: mailpit is a local-dev mail catcher";
const CONNECTION_REFUSAL: &str = "Refusing: mailpit runs over the local connection only";
const PLACEMENT: &str = "TASK [Place the monorepo + ensure podman/compose]";

struct Run {
    ok: bool,
    out: String,
}

/// Runs the deploy playbook under `--check` against an inventory whose `mailpit_svc` group holds `hosts`.
fn run(hosts: Value) -> Run {
    let tmp = TempDir::new().unwrap();
    let inv = playbook::write(
        tmp.path(),
        "inv.json",
        &json!({"all": {"children": {"mailpit_svc": {"hosts": hosts}}}}),
    );
    let mut cmd = process::ansible_playbook(
        tmp.path(),
        &inv.display().to_string(),
        &repo::path(PLAYBOOK).display().to_string(),
    );
    cmd.arg("--check");
    let output = process::run(&mut cmd);
    Run {
        ok: output.status.success(),
        out: process::output_text(&output),
    }
}

#[test]
fn a_host_not_named_local_refuses_the_play_before_the_monorepo_is_placed() {
    // Arrange: one host, named as production, on the local connection.
    let hosts = json!({"mailpit-prod": {"ansible_connection": "local"}});

    // Act
    let out = run(hosts);

    // Assert: red on the host guard, naming the host, and the placement never started.
    assert!(!out.ok, "{}", out.out);
    assert!(out.out.contains(HOST_REFUSAL), "{}", out.out);
    assert!(out.out.contains("are not: mailpit-prod."), "{}", out.out);
    assert!(!out.out.contains(PLACEMENT), "{}", out.out);
}

#[test]
fn one_host_not_named_local_refuses_the_whole_play_and_only_that_host_is_named() {
    // Arrange: a `-local` host beside a production host, both on the local connection.
    let hosts = json!({
        "mailpit-local": {"ansible_connection": "local"},
        "mailpit-prod": {"ansible_connection": "local"},
    });

    // Act
    let out = run(hosts);

    // Assert: red on the host guard, naming only the production host, and the placement never started.
    assert!(!out.ok, "{}", out.out);
    assert!(out.out.contains(HOST_REFUSAL), "{}", out.out);
    assert!(out.out.contains("are not: mailpit-prod."), "{}", out.out);
    assert!(!out.out.contains(PLACEMENT), "{}", out.out);
}

#[test]
fn a_connection_that_is_not_local_refuses_the_play_before_the_monorepo_is_placed() {
    // Arrange: a `-local` host whose connection the inventory sets to ssh. Its address is loopback, so no host is reached.
    let hosts =
        json!({"mailpit-local": {"ansible_host": "127.0.0.1", "ansible_connection": "ssh"}});

    // Act
    let out = run(hosts);

    // Assert: red on the connection guard, naming the connection, and the placement never started.
    assert!(!out.ok, "{}", out.out);
    assert!(out.out.contains(CONNECTION_REFUSAL), "{}", out.out);
    assert!(
        out.out.contains("ansible_connection is 'ssh'"),
        "{}",
        out.out
    );
    assert!(!out.out.contains(PLACEMENT), "{}", out.out);
}

#[test]
fn a_local_dev_host_on_the_local_connection_passes_both_guards() {
    // Arrange: the one host the inventory example declares, on the local connection.
    let hosts =
        json!({"mailpit-local": {"ansible_host": "127.0.0.1", "ansible_connection": "local"}});

    // Act
    let out = run(hosts);

    // Assert: the run reaches the placement, so both guards passed. The placement may then stop on its own checks
    // (it needs vars this fixture does not set), so the run's exit status is not asserted. Neither guard refused.
    assert!(out.out.contains(PLACEMENT), "{}", out.out);
    assert!(!out.out.contains(HOST_REFUSAL), "{}", out.out);
    assert!(!out.out.contains(CONNECTION_REFUSAL), "{}", out.out);
}
