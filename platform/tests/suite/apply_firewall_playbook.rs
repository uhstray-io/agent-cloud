//! apply-firewall.yml's upstream-source normalisation, run through `ansible-playbook` (playbook
//! tier of the firewall group, plan 24).
//!
//! Three tasks of the real play are lifted (the upstream normalisation, its refusal, and the
//! declared-rule computation), fed fixed detected ports, and run with one host per
//! `firewall_upstream_source` value. A good value must yield both detected-port rules for every
//! source; a blank one must stop the run before the rules are computed. Each value is a host
//! var, so the good values share one run and the blank ones another (plan 21 F1). Runs go through `process::ansible_playbook`, so
//! they are bounded, and confined to the test's directory where a kernel sandbox
//! (sandbox-exec, bwrap) is present (`process::sandbox`).
//!
//! ## Port map (BATS @test -> Rust fn)
//!
//! | BATS @test (`test_apply_firewall.bats`) | Rust fn |
//! |---|---|
//! | apply-firewall: blank upstream sources stop before either detected-port rule | `blank_upstream_sources_stop_before_any_detected_port_rule` |

use agent_cloud_tests::{playbook, process};
use serde_json::{Map, Value, json};
use tempfile::TempDir;

const PLAYBOOK: &str = "platform/playbooks/apply-firewall.yml";

const NAMES: [&str; 3] = [
    "Normalise the upstream source(s) to a list",
    "Require an upstream source when detected ports exist",
    "Compute the declared rules (command, tag, stored spec), SSH first",
];

/// The three lifted tasks plus a debug of the rule commands, on the `targets` group.
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
    json!([{"hosts": "targets", "connection": "local", "gather_facts": false,
        "vars": {"_ssh_cidrs": ["192.0.2.0/24"], "_rules": [], "_route_rules": [], "_deny_egress": [],
                 "_allow_egress": [], "_egress_enforce": false,
                 "_rootful": true, "_detected": {"stdout_lines": ["443 tcp", "53 udp"]}},
        "tasks": tasks}])
}

/// Runs the lifted play ONCE over one host per `firewall_upstream_source` value (a host var,
/// so the values share one ansible-playbook spawn); returns whether it passed and its stdout,
/// then stderr.
fn run(dir: &TempDir, values: &[(&str, Value)]) -> (bool, String) {
    let path = playbook::write(dir.path(), "upstream.json", &lifted());
    let mut hosts = Map::new();
    for (host, value) in values {
        hosts.insert(
            (*host).into(),
            json!({"ansible_connection": "local", "firewall_upstream_source": value}),
        );
    }
    let inv = playbook::write(
        dir.path(),
        "inv.json",
        &json!({"all": {"children": {"targets": {"hosts": hosts}}}}),
    );
    let mut cmd = process::ansible_playbook(
        dir.path(),
        &inv.display().to_string(),
        &path.display().to_string(),
    );
    let out = process::run(&mut cmd);
    let stdout = String::from_utf8_lossy(&out.stdout).into_owned();
    let stderr = String::from_utf8_lossy(&out.stderr);
    (out.status.success(), format!("{stdout}{stderr}"))
}

/// The output of `host`'s result under the task header `task`: from its `ok: [host]` or
/// `fatal: [host]` line up to the next result, task or recap line.
fn host_result<'a>(output: &'a str, task: &str, host: &str) -> &'a str {
    let header = format!("TASK [{task}");
    let from = output
        .find(&header)
        .unwrap_or_else(|| panic!("no {header:?} in:\n{output}"));
    let section = &output[from..];
    let start = [format!("ok: [{host}]"), format!("fatal: [{host}]")]
        .iter()
        .filter_map(|m| section.find(m.as_str()))
        .min()
        .unwrap_or_else(|| panic!("no result for {host} under {header:?}:\n{output}"));
    let rest = &section[start..];
    let end = [
        "\nok: [",
        "\nfatal: [",
        "\nskipping: [",
        "\nchanged: [",
        "\nTASK [",
        "\nPLAY RECAP",
    ]
    .iter()
    .filter_map(|m| rest.find(m))
    .min()
    .unwrap_or(rest.len());
    &rest[..end]
}

#[test]
fn blank_upstream_sources_stop_before_any_detected_port_rule() {
    let dir = TempDir::new().unwrap();
    let good = [
        ("one", json!("192.0.2.10")),
        ("two", json!(["192.0.2.10", "198.51.100.10"])),
    ];
    let (ok, output) = run(&dir, &good);
    assert!(ok, "{output}");
    for (host, value) in &good {
        let block = host_result(&output, "Show the rule commands", host);
        assert!(
            block.contains("allow from 192.0.2.10 to any port 443 proto tcp"),
            "{value}:\n{block}"
        );
        assert!(
            block.contains("route allow proto udp from 192.0.2.10 to any port 53"),
            "{value}:\n{block}"
        );
        if value.to_string().contains("198.51.100.10") {
            assert!(
                block.contains("allow from 198.51.100.10 to any port 443 proto tcp"),
                "{block}"
            );
            assert!(
                block.contains("route allow proto udp from 198.51.100.10 to any port 53"),
                "{block}"
            );
        }
    }

    let dir = TempDir::new().unwrap();
    let blank = [
        ("b0", json!(["192.0.2.10", ""])),
        ("b1", json!(["192.0.2.10", "  "])),
        ("b2", json!(["192.0.2.10", null])),
        ("b3", json!("")),
        ("b4", json!("  ")),
        ("b5", json!([])),
    ];
    let (ok, output) = run(&dir, &blank);
    assert!(!ok, "blank values must stop the run:\n{output}");
    for (host, value) in &blank {
        let block = host_result(&output, NAMES[1], host);
        assert!(
            block.starts_with(&format!("fatal: [{host}]")),
            "{value}:\n{block}"
        );
        assert!(
            block.contains("Set firewall_upstream_source"),
            "{value}:\n{block}"
        );
    }
    // Every host stopped, so the rules were computed for none.
    assert!(
        !output.contains("TASK [Compute the declared rules"),
        "{output}"
    );
}
