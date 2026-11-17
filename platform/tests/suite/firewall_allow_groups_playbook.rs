//! apply-firewall.yml's `firewall_allow_groups`: one rule per group member, from its
//! `ansible_host` (playbook tier of the firewall group, plan 24).
//!
//! The play's own vars block and its refusal task are lifted and run against a stub inventory,
//! so the test exercises the real Jinja rather than a copy of it. Runs go through
//! `process::ansible_playbook`, so they are confined to the test's directory and bounded.
//!
//! ## Port map (pytest -> Rust fn)
//!
//! | pytest (`test_firewall_allow_groups.py`) | Rust fn |
//! |---|---|
//! | `test_a_group_rule_expands_to_one_rule_per_member` | `a_group_rule_expands_to_one_rule_per_member` |
//! | `test_no_group_rules_leaves_the_static_rules_alone` | `no_group_rules_leaves_the_static_rules_alone` |
//! | `test_an_unresolvable_group_rule_is_refused` (3 cases) | `an_unresolvable_group_rule_is_refused` (one loop) |

use agent_cloud_tests::{playbook, process};
use serde_json::{Map, Value, json};
use tempfile::TempDir;

const PLAYBOOK: &str = "platform/playbooks/apply-firewall.yml";
const REFUSAL: &str =
    "Refuse a group rule that names an unknown group or a member without an IPv4 address";

/// Runs the lifted vars and refusal against host `target` with `host_vars` and `groups`
/// (`{group: {member: ansible_host}}`); the computed `_rules` land in `dir/rules.json`.
fn render(dir: &TempDir, host_vars: Value, groups: Value) -> (bool, String) {
    let play = playbook::plays(PLAYBOOK).remove(0);
    let refusal: Vec<&Value> = play["tasks"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|t| t["name"] == REFUSAL)
        .collect();
    assert_eq!(refusal.len(), 1, "one refusal task");
    let mut target = json!({"ansible_connection": "local"});
    target
        .as_object_mut()
        .unwrap()
        .extend(host_vars.as_object().unwrap().clone());
    let mut children = Map::new();
    for (group, members) in groups.as_object().unwrap() {
        let hosts: Map<String, Value> = members
            .as_object()
            .unwrap()
            .iter()
            .map(|(h, a)| (h.clone(), json!({"ansible_host": a})))
            .collect();
        children.insert(group.clone(), json!({"hosts": hosts}));
    }
    let inventory = json!({"all": {"hosts": {"target": target}, "children": children}});
    let inv = playbook::write(dir.path(), "inv.json", &inventory);
    let out = dir.path().join("rules.json");
    let lifted = json!([{"hosts": "target", "gather_facts": false, "vars": play["vars"],
        "tasks": [refusal[0], {"name": "dump", "ansible.builtin.copy": {
            "content": "{{ _rules | to_json }}", "dest": out.display().to_string(), "mode": "0600"}}]}]);
    let path = playbook::write(dir.path(), "play.json", &lifted);
    let mut cmd = process::ansible_playbook(
        dir.path(),
        &inv.display().to_string(),
        &path.display().to_string(),
    );
    let run = process::run(&mut cmd);
    let text = format!(
        "{}{}",
        String::from_utf8_lossy(&run.stdout),
        String::from_utf8_lossy(&run.stderr)
    );
    (run.status.success(), text)
}

fn rules(dir: &TempDir) -> Value {
    serde_json::from_str(&std::fs::read_to_string(dir.path().join("rules.json")).unwrap()).unwrap()
}

#[test]
fn a_group_rule_expands_to_one_rule_per_member() {
    let dir = TempDir::new().unwrap();
    let (ok, output) = render(
        &dir,
        json!({
            "firewall_allow_rules": [{"port": 53, "proto": "udp", "from": "192.0.2.200", "comment": "dgx"}],
            "firewall_allow_groups": [{"port": 53, "proto": "udp", "group": "members", "comment": "LAN DNS"}],
        }),
        json!({"members": {"a": "192.0.2.10", "b": "192.0.2.11"}}),
    );
    assert!(ok, "{output}");
    assert_eq!(
        rules(&dir),
        json!([
            {"port": 53, "proto": "udp", "from": "192.0.2.200", "comment": "dgx"},
            {"port": 53, "proto": "udp", "from": "192.0.2.10", "comment": "LAN DNS: a"},
            {"port": 53, "proto": "udp", "from": "192.0.2.11", "comment": "LAN DNS: b"},
        ])
    );
}

#[test]
fn no_group_rules_leaves_the_static_rules_alone() {
    let dir = TempDir::new().unwrap();
    let (ok, output) = render(
        &dir,
        json!({"firewall_allow_rules": [{"port": 22, "from": "192.0.2.1"}]}),
        json!({}),
    );
    assert!(ok, "{output}");
    assert_eq!(rules(&dir), json!([{"port": 22, "from": "192.0.2.1"}]));
}

#[test]
fn an_unresolvable_group_rule_is_refused() {
    for (case, groups, rule_group) in [
        (
            "unknown group",
            json!({"members": {"a": "192.0.2.10"}}),
            "missing",
        ),
        (
            "member without an IPv4 address",
            json!({"members": {"a": "dns.example.test"}}),
            "members",
        ),
        (
            "four octets, not an IPv4 address",
            json!({"members": {"a": "999.1.1.1"}}),
            "members",
        ),
    ] {
        let dir = TempDir::new().unwrap();
        let (ok, output) = render(
            &dir,
            json!({"firewall_allow_groups": [{"port": 53, "group": rule_group}]}),
            groups,
        );
        assert!(!ok, "{case}: must be refused:\n{output}");
        assert!(
            output.contains("refusing rather than dropping a source"),
            "{case}:\n{output}"
        );
        assert!(!dir.path().join("rules.json").exists(), "{case}");
    }
}
