//! The playbook tier of the install-qemu-guest-agent pilot (plan 24).
//!
//! Replaces the two tests of `platform/tests/test_install_qemu_guest_agent.bats` that run
//! `ansible-playbook`: each expects a refusal that must fire before any host change. Those runs
//! go through `process::ansible_playbook` and `process::run`, so they are confined and bounded.
//!
//! Port map (BATS @test → Rust fn):
//!
//! | BATS @test | Rust fn |
//! |---|---|
//! | `qga: a misspelled group refuses instead of reporting success` | `a_misspelled_group_refuses_instead_of_reporting_success` |
//! | `qga: a host pattern is refused before sudo resolution` | `a_host_pattern_is_refused_before_sudo_resolution` |

use agent_cloud_tests::process;
use tempfile::TempDir;

const PLAYBOOK: &str = "platform/playbooks/install-qemu-guest-agent.yml";

/// The inventory the BATS setup wrote: one `demo_svc` group holding one local host.
fn inventory(dir: &TempDir) -> String {
    let path = dir.path().join("inventory.yml");
    std::fs::write(
        &path,
        "all:\n  children:\n    demo_svc:\n      hosts:\n        demo:\n          ansible_connection: local\n",
    )
    .expect("write the inventory");
    path.display().to_string()
}

/// Runs the playbook against `target_service`, returning whether it succeeded and its output
/// (stdout, then stderr, as BATS's `$output` holds them).
fn run_for_target(dir: &TempDir, target_service: &str) -> (bool, String) {
    let inventory = inventory(dir);
    let mut cmd = process::ansible_playbook(dir.path(), &inventory, PLAYBOOK);
    cmd.args(["-e", &format!("target_service={target_service}")]);
    let out = process::run(&mut cmd);
    let text = format!(
        "{}{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );
    (out.status.success(), text)
}

#[test]
fn a_misspelled_group_refuses_instead_of_reporting_success() {
    let dir = TempDir::new().unwrap();

    let (success, output) = run_for_target(&dir, "demo_svx");

    assert!(!success, "a misspelled group must fail the run:\n{output}");
    assert!(
        output.contains("Inventory group 'demo_svx' is absent or empty"),
        "{output}"
    );
    assert!(
        !output.contains("PLAY [Install QEMU guest agent"),
        "{output}"
    );
}

#[test]
fn a_host_pattern_is_refused_before_sudo_resolution() {
    let dir = TempDir::new().unwrap();

    let (success, output) = run_for_target(&dir, "all");

    assert!(!success, "a host pattern must fail the run:\n{output}");
    assert!(
        output.contains("target_service must be one <name>_svc group"),
        "{output}"
    );
    assert!(!output.contains("Resolve the sudo password"), "{output}");
}
