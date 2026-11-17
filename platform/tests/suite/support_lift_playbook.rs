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

/// One play on `hosts: localhost`, connected locally, that holds the variables and asserts each
/// check.
fn play(checks: &[&str]) -> String {
    let mut text = String::from(
        "- hosts: localhost\n  gather_facts: false\n  connection: local\n  vars:\n    port: 53\n    mode: 0600\n    flag: yes\n    quoted: \"53\"\n    nothing: ~\n    ratio: 1.5\n    octal_text: '0600'\n  tasks:\n    - name: the lifted types hold\n      ansible.builtin.assert:\n        that:\n",
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

/// Lifts the play with `checks`, runs it through `ansible-playbook` against the inline inventory
/// `localhost,` in a directory of its own, and returns whether it passed and its output.
fn run_checks(checks: &[&str]) -> (bool, String) {
    let dir = TempDir::new().unwrap();
    let path = playbook::write(dir.path(), "types.json", &playbook::loads(&play(checks)));
    let mut cmd = process::ansible_playbook(dir.path(), "localhost,", &path.display().to_string());
    let out = process::run(&mut cmd);
    (out.status.success(), output_of(&out))
}

#[test]
fn a_lifted_playbook_runs_with_its_yaml_1_1_types() {
    let (passed, output) = run_checks(CHECKS);
    assert!(passed, "the lifted play must pass:\n{output}");

    // The same play with one false assertion must fail: the run really evaluates the checks.
    let mut failing = CHECKS.to_vec();
    failing.push("flag is string");
    let (passed, output) = run_checks(&failing);
    assert!(!passed, "a false assertion must fail the run:\n{output}");
    assert!(
        output.contains("flag is string"),
        "the failure must name the false check:\n{output}"
    );
}
