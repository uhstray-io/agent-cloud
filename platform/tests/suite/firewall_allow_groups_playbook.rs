//! apply-firewall.yml's `firewall_allow_groups`: one rule per group member, from its
//! `ansible_host` (playbook tier of the firewall group, plan 24).
//!
//! The play's own vars block and its refusal task are lifted and run against a stub inventory,
//! so the test exercises the real Jinja rather than a copy of it. Runs go through
//! `process::ansible_playbook`, so they are bounded, and confined to the test's directory where a
//! kernel sandbox (sandbox-exec, bwrap) is present (`process::sandbox`).
//!
//! ## Port map (pytest -> Rust fn)
//!
//! | pytest (`test_firewall_allow_groups.py`) | Rust fn |
//! |---|---|
//! | `test_a_group_rule_expands_to_one_rule_per_member` | `a_group_rule_expands_to_one_rule_per_member` |
//! | `test_no_group_rules_leaves_the_static_rules_alone` | `no_group_rules_leaves_the_static_rules_alone` |
//! | `test_an_unresolvable_group_rule_is_refused` (3 cases) | `an_unresolvable_group_rule_is_refused` (one run, one host per case) |

use agent_cloud_tests::{playbook, process, text};
use serde_json::{Map, Value, json};
use tempfile::TempDir;

const PLAYBOOK: &str = "platform/playbooks/apply-firewall.yml";
const REFUSAL: &str =
    "Refuse a group rule that names an unknown group or a member without an IPv4 address";

/// Runs the lifted vars and refusal against `hosts` (name, host vars) with `groups`
/// (`{group: {member: ansible_host}}`); each host's computed `_rules` land in
/// `dir/rules-<host>.json`. Returns whether the run passed, its stdout (the refusal checks read
/// only stdout, as the pytest did) and its stderr (for failure messages).
fn render(dir: &TempDir, hosts: &[(&str, Value)], groups: Value) -> (bool, String, String) {
    let play = playbook::plays(PLAYBOOK).remove(0);
    let refusal: Vec<&Value> = play["tasks"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|t| t["name"] == REFUSAL)
        .collect();
    assert_eq!(refusal.len(), 1, "one refusal task");
    let mut targets = Map::new();
    for (name, host_vars) in hosts {
        let mut vars = json!({"ansible_connection": "local"});
        vars.as_object_mut()
            .unwrap()
            .extend(host_vars.as_object().unwrap().clone());
        targets.insert((*name).into(), vars);
    }
    let mut children = Map::new();
    children.insert("targets".into(), json!({"hosts": targets}));
    for (group, members) in groups.as_object().unwrap() {
        let hosts: Map<String, Value> = members
            .as_object()
            .unwrap()
            .iter()
            .map(|(h, a)| (h.clone(), json!({"ansible_host": a})))
            .collect();
        children.insert(group.clone(), json!({"hosts": hosts}));
    }
    let inv = playbook::write(
        dir.path(),
        "inv.json",
        &json!({"all": {"children": children}}),
    );
    let dest = format!(
        "{}/rules-{{{{ inventory_hostname }}}}.json",
        dir.path().display()
    );
    let lifted = json!([{"hosts": "targets", "gather_facts": false, "vars": play["vars"],
        "tasks": [refusal[0], {"name": "dump", "ansible.builtin.copy": {
            "content": "{{ _rules | to_json }}", "dest": dest, "mode": "0600"}}]}]);
    let path = playbook::write(dir.path(), "play.json", &lifted);
    let mut cmd = process::ansible_playbook(
        dir.path(),
        &inv.display().to_string(),
        &path.display().to_string(),
    );
    let run = process::run(&mut cmd);
    (
        run.status.success(),
        String::from_utf8_lossy(&run.stdout).into_owned(),
        String::from_utf8_lossy(&run.stderr).into_owned(),
    )
}

fn rules_file(dir: &TempDir, host: &str) -> std::path::PathBuf {
    dir.path().join(format!("rules-{host}.json"))
}

fn rules(dir: &TempDir) -> Value {
    serde_json::from_str(&std::fs::read_to_string(rules_file(dir, "target")).unwrap()).unwrap()
}

#[test]
fn a_group_rule_expands_to_one_rule_per_member() {
    let dir = TempDir::new().unwrap();
    let (ok, output, stderr) = render(
        &dir,
        &[(
            "target",
            json!({
                "firewall_allow_rules": [{"port": 53, "proto": "udp", "from": "192.0.2.200", "comment": "dgx"}],
                "firewall_allow_groups": [{"port": 53, "proto": "udp", "group": "members", "comment": "LAN DNS"}],
            }),
        )],
        json!({"members": {"a": "192.0.2.10", "b": "192.0.2.11"}}),
    );
    assert!(ok, "{output}{stderr}");
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
    let (ok, output, stderr) = render(
        &dir,
        &[(
            "target",
            json!({"firewall_allow_rules": [{"port": 22, "from": "192.0.2.1"}]}),
        )],
        json!({}),
    );
    assert!(ok, "{output}{stderr}");
    assert_eq!(rules(&dir), json!([{"port": 22, "from": "192.0.2.1"}]));
}

#[test]
fn an_unresolvable_group_rule_is_refused() {
    // One host per case, one run (plan 21 F1); each case has its own group names.
    let cases = [
        ("unknown_group", "missing"),         // unknown group
        ("not_ipv4", "not_ipv4_members"),     // member without an IPv4 address
        ("bad_octets", "bad_octets_members"), // four octets, not an IPv4 address
    ];
    let groups = json!({
        "unknown_group_members": {"unknown_group_a": "192.0.2.10"},
        "not_ipv4_members": {"not_ipv4_a": "dns.example.test"},
        "bad_octets_members": {"bad_octets_a": "999.1.1.1"},
    });
    let hosts: Vec<(&str, Value)> = cases
        .iter()
        .map(|(host, group)| {
            (
                *host,
                json!({"firewall_allow_groups": [{"port": 53, "group": group}]}),
            )
        })
        .collect();
    let dir = TempDir::new().unwrap();
    let (ok, output, stderr) = render(&dir, &hosts, groups);
    assert!(!ok, "every case must be refused:\n{output}{stderr}");
    for (host, _) in cases {
        assert!(
            output.contains(&format!("fatal: [{host}]: FAILED!")),
            "{host}:\n{output}"
        );
        // The refusal's own message, naming this host.
        assert!(
            text::any_line(
                &output,
                &format!(
                    r#""msg": "firewall_allow_groups on {host} .*refusing rather than dropping a source"#
                )
            ),
            "{host}:\n{output}"
        );
        assert!(!rules_file(&dir, host).exists(), "{host}");
    }
}
