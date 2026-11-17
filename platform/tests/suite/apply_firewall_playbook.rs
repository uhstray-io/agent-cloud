//! apply-firewall.yml's upstream-source normalisation, run through `ansible-playbook` (playbook
//! tier of the firewall group, plan 24).
//!
//! Three tasks of the real play are lifted (the upstream normalisation, its refusal, and the
//! declared-rule computation), fed fixed detected ports, and run once per
//! `firewall_upstream_source` value: the value is an extra var, so each needs its own run. A
//! good value must yield both detected-port rules for every source; a blank one must stop the
//! run before the rules are computed. Runs go through `process::ansible_playbook`, so they are
//! confined to the test's directory and bounded.
//!
//! ## Port map (BATS @test -> Rust fn)
//!
//! | BATS @test (`test_apply_firewall.bats`) | Rust fn |
//! |---|---|
//! | apply-firewall: blank upstream sources stop before either detected-port rule | `blank_upstream_sources_stop_before_any_detected_port_rule` |

use agent_cloud_tests::{playbook, process};
use serde_json::{Value, json};
use tempfile::TempDir;

const PLAYBOOK: &str = "platform/playbooks/apply-firewall.yml";

const NAMES: [&str; 3] = [
    "Normalise the upstream source(s) to a list",
    "Require an upstream source when detected ports exist",
    "Compute the declared rules (command, tag, stored spec), SSH first",
];

/// The three lifted tasks plus a debug of the rule commands, on localhost.
fn lifted() -> Value {
    let play = playbook::plays(PLAYBOOK).remove(0);
    let mut tasks: Vec<Value> = play["tasks"]
        .as_array()
        .expect("the firewall play has tasks")
        .iter()
        .filter(|task| NAMES.contains(&task["name"].as_str().unwrap_or("")))
        .cloned()
        .collect();
    let names: Vec<&str> = tasks.iter().map(|t| t["name"].as_str().unwrap()).collect();
    assert_eq!(names, NAMES, "the lifted tasks, in play order");
    tasks.push(json!({"name": "Show the rule commands",
        "ansible.builtin.debug": {"msg": "{{ _fw_desired | map(attribute='cmd') | list }}"}}));
    json!([{"hosts": "localhost", "connection": "local", "gather_facts": false,
        "vars": {"_ssh_cidrs": ["192.0.2.0/24"], "_rules": [], "_route_rules": [], "_deny_egress": [],
                 "_allow_egress": [], "_egress_enforce": false,
                 "_rootful": true, "_detected": {"stdout_lines": ["443 tcp", "53 udp"]}},
        "tasks": tasks}])
}

/// Runs the lifted play with `firewall_upstream_source` set to `values` (JSON text).
fn run(dir: &TempDir, path: &str, values: &str) -> (bool, String) {
    let mut cmd = process::ansible_playbook(dir.path(), "localhost,", path);
    cmd.args(["-e", &format!("{{\"firewall_upstream_source\":{values}}}")]);
    let out = process::run(&mut cmd);
    let text = format!(
        "{}{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );
    (out.status.success(), text)
}

#[test]
fn blank_upstream_sources_stop_before_any_detected_port_rule() {
    let dir = TempDir::new().unwrap();
    let path = playbook::write(dir.path(), "upstream.json", &lifted())
        .display()
        .to_string();
    for values in [r#""192.0.2.10""#, r#"["192.0.2.10","198.51.100.10"]"#] {
        let (ok, output) = run(&dir, &path, values);
        assert!(ok, "{values}:\n{output}");
        assert!(
            output.contains("allow from 192.0.2.10 to any port 443 proto tcp"),
            "{values}:\n{output}"
        );
        assert!(
            output.contains("route allow proto udp from 192.0.2.10 to any port 53"),
            "{values}:\n{output}"
        );
        if values.contains("198.51.100.10") {
            assert!(
                output.contains("allow from 198.51.100.10 to any port 443 proto tcp"),
                "{output}"
            );
            assert!(
                output.contains("route allow proto udp from 198.51.100.10 to any port 53"),
                "{output}"
            );
        }
    }
    for values in [
        r#"["192.0.2.10",""]"#,
        r#"["192.0.2.10","  "]"#,
        r#"["192.0.2.10",null]"#,
        r#""""#,
        r#""  ""#,
        "[]",
    ] {
        let (ok, output) = run(&dir, &path, values);
        assert!(!ok, "{values} must stop the run:\n{output}");
        assert!(
            output.contains("Set firewall_upstream_source"),
            "{values}:\n{output}"
        );
        assert!(
            !output.contains("TASK [Compute the declared rules"),
            "{values}:\n{output}"
        );
    }
}
