//! The playbook tier of the lifting loader (`src/playbook.rs`, AC-943): a lifted play runs through
//! `ansible-playbook` with the YAML 1.1 types PyYAML reads, and a false assertion in the same play
//! fails the run, so the passing run is evidence the checks were evaluated.

use agent_cloud_tests::{playbook, process};
use tempfile::TempDir;

/// The checks the play asserts over its variables. They are YAML 1.1 values after the lift:
/// `mode: 0600` is 384, `flag: yes` a boolean, `quoted: "53"` a string.
const CHECKS: &[&str] = &[
    "port == 53",
    "port is number",
    "port is not string",
    "mode == 384",
    "flag is boolean",
    "flag",
    "quoted is string",
    "quoted == '53'",
    "nothing is none",
    "ratio == 1.5",
    "octal_text == '0600'",
];

/// One play on `hosts: all` that holds the variables and asserts each check.
fn play(checks: &[&str]) -> String {
    let mut text = String::from(
        "- hosts: all\n  gather_facts: false\n  vars:\n    port: 53\n    mode: 0600\n    flag: yes\n    quoted: \"53\"\n    nothing: ~\n    ratio: 1.5\n    octal_text: '0600'\n  tasks:\n    - name: the lifted types hold\n      ansible.builtin.assert:\n        that:\n",
    );
    for check in checks {
        text.push_str(&format!("          - \"{check}\"\n"));
    }
    text
}

/// Stdout then stderr, as the BATS tests of this area read `$output`.
fn output_of(out: &std::process::Output) -> String {
    format!(
        "{}{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    )
}

#[test]
fn a_lifted_playbook_runs_with_its_yaml_1_1_types() {
    let dir = TempDir::new().unwrap();
    let inventory = dir.path().join("inventory.yml");
    std::fs::write(
        &inventory,
        "all:\n  hosts:\n    localhost:\n      ansible_connection: local\n",
    )
    .unwrap();
    let inventory = inventory.display().to_string();

    let passing = playbook::loads(&play(CHECKS));
    let path = playbook::write(dir.path(), "types.json", &passing);
    let mut cmd = process::ansible_playbook(dir.path(), &inventory, &path.display().to_string());
    let out = process::run(&mut cmd);
    assert!(
        out.status.success(),
        "the lifted play must pass:\n{}",
        output_of(&out)
    );

    let mut failing_checks = CHECKS.to_vec();
    failing_checks.push("flag is string");
    let failing = playbook::loads(&play(&failing_checks));
    let path = playbook::write(dir.path(), "types-fail.json", &failing);
    let mut cmd = process::ansible_playbook(dir.path(), &inventory, &path.display().to_string());
    let out = process::run(&mut cmd);
    let output = output_of(&out);
    assert!(
        !out.status.success(),
        "a false assertion must fail the run:\n{output}"
    );
    assert!(
        output.contains("flag is string"),
        "the failure must name the false check:\n{output}"
    );
}
