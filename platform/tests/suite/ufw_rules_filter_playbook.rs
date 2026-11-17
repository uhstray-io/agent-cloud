//! The ufw rule builders and readers (`platform/playbooks/filter_plugins/ufw_rules.py`), run
//! through Ansible (playbook tier of the firewall group, plan 24).
//!
//! The filter plugin stays Python, so its cases run through the tier that can load it (plan 24
//! D5): every case of the deleted `test_ufw_rules_filter.py` becomes one call in ONE
//! `ansible-playbook` run on localhost. The play loops over the calls once per filter
//! (`item.args[0] | <filter>(*item.args[1:], **item.kw)`), writes every result as JSON to a
//! file in the test's directory, and the Rust side asserts each case, naming the pytest item it
//! came from. A call that needs another filter's output (`ufw_absent` over the declared rules)
//! takes it as a Jinja expression, so the chain runs in the plugin as the Python test chained it.
//! Every failure is collected and reported together.
//!
//! `apply_firewall_convergence_playbook.rs` proves `apply-firewall.yml` wires these filters into
//! the adds and the prune guard; the command forms, tags and spellings live here. Specs are in
//! the form `ufw show added` prints (ufw 0.36.2 src/parser.py get_command), without the leading
//! `ufw ` and the comment.
//!
//! `ufw_tag` is a module function, not an exported filter, so its two cases are read from the
//! tags `ufw_desired_rules` gives the rules they name (the SSH allow and a port-less denial).
//!
//! ## Port map (pytest -> Rust fn)
//!
//! Every pytest function below is merged into `ufw_filters_give_the_results_their_tests_expect`:
//! its cases are asserted in the one run, and each failure names this pytest test and the case.
//! `test_every_exported_filter_is_called_by_a_playbook` reads the plugin's source instead, in
//! `ufw_rules_filter::every_exported_filter_is_called_by_a_playbook` (static tier).
//!
//! | pytest (`test_ufw_rules_filter.py`) | Rust fn |
//! |---|---|
//! | test_an_address_is_spelled_as_ufw_stores_it (17 cases) | merged |
//! | test_a_rule_that_admits_ssh_is_recognised (23 cases) | merged |
//! | test_a_rule_that_does_not_admit_ssh_is_not (11 cases) | merged |
//! | test_only_a_single_ipv4_address_is_one (11 cases) | merged |
//! | test_show_added_is_read_into_spec_and_tag | merged |
//! | test_every_exported_filter_is_called_by_a_playbook | `ufw_rules_filter::every_exported_filter_is_called_by_a_playbook` |
//! | test_the_tag_names_the_rule_with_the_peer_last | merged (read from desired rules' tags) |
//! | test_every_declared_rule_kind_has_its_command_tag_and_stored_spec_in_add_order | merged |
//! | test_detected_ports_get_no_forward_mirror_on_a_rootless_host | merged |
//! | test_nothing_declared_beyond_ssh_yields_only_the_ssh_allows | merged |
//! | test_a_rule_declared_twice_appears_once_and_keeps_its_first_place | merged |
//! | test_one_address_spelled_two_ways_is_one_rule_under_one_tag | merged |
//! | test_a_rule_naming_no_address_is_marked_dual_family | merged |
//! | test_a_rule_is_held_only_by_its_tag_and_its_spec_together | merged |
//! | test_a_rule_naming_no_address_is_re_asserted_even_when_held | merged |
//! | test_a_rule_is_deleted_by_its_spec | merged |
//! | test_an_explicit_null_is_never_read_as_a_default | merged |
//! | test_an_egress_denial_the_platform_really_declares_is_accepted (10 cases) | merged |
//! | test_an_egress_denial_that_is_not_scoped_is_refused (25 cases) | merged |
//! | test_egress_containment_is_compared_in_the_spelling_ufw_stores | merged |
//! | test_an_ssh_cidr_that_is_not_an_address_cannot_prove_a_denial_safe | merged |
//! | test_every_problem_of_an_entry_is_reported_not_only_the_first | merged |
//! | test_allowed_egress_comes_after_the_denials_and_the_audit_rule_last | merged |
//! | test_the_audit_rule_exists_only_in_audit_mode_and_only_with_an_allowlist | merged |
//! | test_a_rule_to_any_address_is_dual_family_and_a_scoped_one_is_not | merged |
//! | test_an_allowlist_the_platform_would_declare_is_accepted_in_audit (5 cases) | merged |
//! | test_a_malformed_allowlist_entry_is_refused_by_name (10 cases) | merged |
//! | test_an_unknown_mode_is_refused_even_without_entries | merged |
//! | test_enforcing_needs_dns_out_and_a_probe | merged |
//! | test_enforcing_an_empty_allowlist_is_refused | merged |
//! | test_a_probe_timeout_is_a_whole_number_of_seconds (6 cases) | merged |

use std::collections::BTreeSet;

use agent_cloud_tests::{playbook, process};
use serde_json::{Value, json};
use tempfile::TempDir;

/// The filters the play can call, one looped task each.
const FILTERS: [&str; 10] = [
    "ufw_address",
    "ufw_rule_admits_port",
    "ufw_is_ipv4",
    "ufw_egress_problems",
    "ufw_allow_egress_problems",
    "ufw_parse_added",
    "ufw_desired_rules",
    "ufw_absent",
    "ufw_masking",
    "ufw_delete_args",
];

/// The pytest items `test_ufw_rules_filter.py` collected (140 passed, 2026-11-17), less the one
/// ported to the static tier. A case dropped from this port makes the count disagree.
const PYTEST_ITEMS: usize = 139;

/// The management prefixes the egress cases are judged against (RFC 5737 documentation ranges).
fn mgmt() -> Value {
    json!(["192.0.2.0/24", "198.51.100.0/24"])
}

#[derive(Default)]
struct Calls(Vec<Value>);

impl Calls {
    /// Queues `filter(*args, **kw)`; returns the index of its result.
    fn add(&mut self, filter: &str, args: Value, kw: Value) -> usize {
        assert!(FILTERS.contains(&filter), "no task calls {filter}");
        let id = self.0.len();
        self.0
            .push(json!({"id": id, "f": filter, "args": args, "kw": kw}));
        id
    }

    /// `ufw_desired_rules(["192.0.2.0/24"], **kw)`, the Python `_desired` helper's call.
    fn desired(&mut self, kw: Value) -> usize {
        self.add("ufw_desired_rules", json!([["192.0.2.0/24"]]), kw)
    }
}

/// Runs every call in one `ansible-playbook` run; returns the results in call order.
fn run(calls: &Calls) -> Vec<Value> {
    let dir = TempDir::new().unwrap();
    let out = dir.path().join("results.json");
    let mut tasks: Vec<Value> = FILTERS
        .iter()
        .map(|f| {
            json!({
                "name": format!("Call {f}"),
                "ansible.builtin.debug": {"msg": format!(
                    "{{{{ {{'id': item.id, 'r': item.args[0] | {f}(*item.args[1:], **item.kw)}} | to_json }}}}")},
                "loop": format!("{{{{ calls | selectattr('f', 'equalto', '{f}') | list }}}}"),
                "loop_control": {"label": "{{ item.id }}"},
                "register": format!("r_{f}"),
            })
        })
        .collect();
    let gathered: Vec<String> = FILTERS.iter().map(|f| format!("r_{f}.results")).collect();
    tasks.push(json!({
        "name": "Write the results",
        "ansible.builtin.copy": {
            "content": format!("{{{{ ({}) | map(attribute='msg') | list | to_json }}}}", gathered.join(" + ")),
            "dest": out.display().to_string(),
            "mode": "0600",
        },
    }));
    let play = json!([{"hosts": "localhost", "connection": "local", "gather_facts": false,
        "vars": {"calls": calls.0}, "tasks": tasks}]);
    let path = playbook::write(dir.path(), "ufw_filters.json", &play);
    let mut cmd = process::ansible_playbook(dir.path(), "localhost,", &path.display().to_string());
    let output = process::run(&mut cmd);
    assert!(
        output.status.success(),
        "the filter play failed:\n{}{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    let text = std::fs::read_to_string(&out).unwrap_or_else(|e| panic!("{}: {e}", out.display()));
    let rows: Vec<Value> = serde_json::from_str(&text).expect("the results file is JSON");
    let mut results = vec![Value::Null; calls.0.len()];
    let mut seen = BTreeSet::new();
    for row in rows {
        // A JSON-shaped msg may come back as the string or as the structure it spells.
        let row = match row {
            Value::String(s) => serde_json::from_str(&s).expect("each result is JSON"),
            other => other,
        };
        let id = row["id"].as_u64().expect("a result carries its id") as usize;
        assert!(seen.insert(id), "result {id} written twice");
        results[id] = row["r"].clone();
    }
    assert_eq!(seen.len(), calls.0.len(), "every call returns one result");
    results
}

/// The assertions of the run, collected so one failure does not hide the next.
#[derive(Default)]
struct Check {
    items: BTreeSet<String>,
    failures: Vec<String>,
}

impl Check {
    fn that(&mut self, item: &str, ok: bool, detail: impl FnOnce() -> String) {
        self.items.insert(item.to_string());
        if !ok {
            self.failures.push(format!("{item}: {}", detail()));
        }
    }

    fn eq(&mut self, item: &str, got: &Value, want: &Value) {
        self.that(item, got == want, || format!("got {got}, want {want}"));
    }

    /// Some problem in `problems` contains `why` (the pytest `any(why in p for p in problems)`).
    fn names(&mut self, item: &str, problems: &Value, why: &str) {
        let hit = problems.as_array().is_some_and(|ps| {
            ps.iter()
                .any(|p| p.as_str().is_some_and(|p| p.contains(why)))
        });
        self.that(item, hit, || {
            format!("no problem contains {why:?}: {problems}")
        });
    }
}

/// The listed fields of each rule, as one array per rule.
fn fields(rules: &Value, keys: &[&str]) -> Value {
    Value::Array(
        rules
            .as_array()
            .unwrap_or(&Vec::new())
            .iter()
            .map(|r| Value::Array(keys.iter().map(|k| r[*k].clone()).collect()))
            .collect(),
    )
}

/// One field of each rule.
fn column(rules: &Value, key: &str) -> Value {
    Value::Array(
        rules
            .as_array()
            .unwrap_or(&Vec::new())
            .iter()
            .map(|r| r[key].clone())
            .collect(),
    )
}

/// `{spec: dual_family}` for each rule.
fn dual_by_spec(rules: &Value) -> Value {
    let mut map = serde_json::Map::new();
    for r in rules.as_array().unwrap_or(&Vec::new()) {
        map.insert(
            r["spec"].as_str().unwrap_or("").to_string(),
            r["dual_family"].clone(),
        );
    }
    Value::Object(map)
}

fn held(spec: &str, tag: &str) -> Value {
    json!({"spec": spec, "tag": tag, "family": "", "peer": ""})
}

fn empty(v: &Value) -> bool {
    v.as_array().is_some_and(Vec::is_empty)
}

#[test]
fn ufw_filters_give_the_results_their_tests_expect() {
    let mut c = Calls::default();
    let none = json!({});

    // ── ufw_address
    let address = [
        ("192.0.2.5", "192.0.2.5"),
        ("192.0.2.5/32", "192.0.2.5"),
        ("192.0.2.5/255.255.255.255", "192.0.2.5"),
        ("192.0.2.77/24", "192.0.2.0/24"),
        ("192.0.2.0/24", "192.0.2.0/24"),
        ("192.0.2.9/255.255.255.0", "192.0.2.0/24"),
        ("2001:db8::1/128", "2001:db8::1"),
        ("2001:0db8::0001", "2001:db8::1"),
        ("2001:db8::/32", "2001:db8::/32"),
        ("any", "any"),
        (" 192.0.2.5 ", "192.0.2.5"),
        ("not-an-address", "not-an-address"),
        ("0.0.0.0/0", "any"),
        ("::/0", "any"),
        ("0.0.0.0/0.0.0.0", "any"),
        ("0.0.0.5/0", "any"),
        ("::1/0", "::1/0"),
    ];
    let address_ids: Vec<usize> = address
        .iter()
        .map(|(given, _)| c.add("ufw_address", json!([given]), none.clone()))
        .collect();

    // ── ufw_rule_admits_port
    let admits = [
        "allow from 192.0.2.0/24 to any port 22 proto tcp",
        "allow from 192.0.2.0/24 to any port 22",
        "allow from 192.0.2.0/24 to any port 22,2222 proto tcp",
        "allow from 192.0.2.0/24 to any port 2222,22 proto tcp",
        "allow from 192.0.2.0/24 to any port 20:30 proto tcp",
        "allow from 192.0.2.0/24",
        "allow from 192.0.2.0/24 proto tcp",
        "allow from 192.0.2.0/24 port 5000 to any port 22 proto tcp",
        "allow 22/tcp",
        "allow 22",
        "allow 22,80/tcp",
        "allow 1:1024/tcp",
        "limit 22/tcp",
        "limit from 192.0.2.0/24 to any port 22 proto tcp",
        "allow in on eth0 to any port 22 proto tcp",
        "allow in to any port 22 proto tcp",
        "allow log from 192.0.2.0/24 to any port 22 proto tcp",
        "route allow from 192.0.2.0/24 to any port 22 proto tcp",
        "route allow from 192.0.2.0/24",
        "route allow out on eth1 to 192.0.2.164 port 22 proto tcp",
        "route allow in on eth0 out on eth1 to 192.0.2.164 port 22 proto tcp",
        "allow OpenSSH",
        "allow from 192.0.2.0/24 to any app OpenSSH",
    ];
    let admits_not = [
        "allow from 192.0.2.0/24 to any port 22 proto udp",
        "allow 22/udp",
        "allow from 192.0.2.0/24 to any port 2222 proto tcp",
        "allow from 192.0.2.0/24 to any port 23:30 proto tcp",
        "allow from 192.0.2.0/24 to any port 80,443 proto tcp",
        "allow from 192.0.2.0/24 proto udp",
        "allow in on podman1 to any port 53 proto udp",
        "deny from 192.0.2.0/24 to any port 22 proto tcp",
        "deny out to 192.0.2.164 port 22 proto tcp",
        "allow out to 192.0.2.164 port 22 proto tcp",
        "route deny from 192.0.2.0/24 to any port 22 proto tcp",
    ];
    let admits_ids: Vec<usize> = admits
        .iter()
        .map(|s| c.add("ufw_rule_admits_port", json!([s, 22]), none.clone()))
        .collect();
    let admits_not_ids: Vec<usize> = admits_not
        .iter()
        .map(|s| c.add("ufw_rule_admits_port", json!([s, 22]), none.clone()))
        .collect();

    // ── ufw_is_ipv4
    let ipv4 = [
        (json!("192.0.2.10"), true),
        (json!(" 192.0.2.10 "), true),
        (json!("0.0.0.0"), true),
        (json!("999.1.1.1"), false),
        (json!("192.0.2.256"), false),
        (json!("192.0.2"), false),
        (json!("192.0.2.10/32"), false),
        (json!("2001:db8::1"), false),
        (json!("dns.example.test"), false),
        (json!(""), false),
        (json!(null), false),
    ];
    let ipv4_ids: Vec<usize> = ipv4
        .iter()
        .map(|(v, _)| c.add("ufw_is_ipv4", json!([v]), none.clone()))
        .collect();

    // ── ufw_parse_added
    let show_added = c.add(
        "ufw_parse_added",
        json!([[
            "Added user rules (see 'ufw status' for running firewall):",
            "ufw allow from 192.0.2.0/24 to any port 22 proto tcp comment 'agent-cloud:in:22/tcp:192.0.2.0/24'",
            "ufw route allow from 192.0.2.7 to any port 443 proto tcp comment 'agent-cloud:route:443/tcp:192.0.2.7'",
            "ufw allow from 2001:db8::1 to any port 80 proto tcp comment 'agent-cloud:in:80/tcp:2001:db8::1'",
            "ufw allow 8443/tcp",
            "ufw allow from 203.0.113.9 to any port 5432 proto tcp comment 'set by hand'",
            "ufw deny out to 198.51.100.2 comment 'agent-cloud:legacy'",
            "(None)",
        ]]),
        none.clone(),
    );

    // ── ufw_tag, through the rules that carry it
    let tag_ssh = c.desired(none.clone());
    let tag_deny = c.desired(json!({"deny_egress": [{"to": "198.51.100.2"}]}));

    // ── ufw_desired_rules
    let every_kind = c.add(
        "ufw_desired_rules",
        json!([["192.0.2.0/24", "198.51.100.77/24"]]),
        json!({
            "allow_rules": [{"port": 8080, "from": "192.0.2.5/32"}, {"port": 53, "proto": "udp", "from": "192.0.2.10"}],
            "route_rules": [{"port": 9000, "from": "192.0.2.6"}],
            "detected": ["443 tcp", "", "53 udp"], "upstreams": ["192.0.2.7", "192.0.2.8"], "rootful": true,
            "bridges": ["podman1"],
            "deny_egress": [{"to": "198.51.100.1", "port": 8200}, {"to": "198.51.100.2", "port": 53, "proto": "udp"},
                            {"to": "198.51.100.3"}],
        }),
    );
    let rootless = c.desired(json!({"detected": ["443 tcp"], "upstreams": ["192.0.2.7"]}));
    let nothing = c.desired(none.clone());
    let nothing_detected = c.desired(json!({"detected": ["443 tcp"]}));
    let twice = c.add(
        "ufw_desired_rules",
        json!([["192.0.2.0/24"]]),
        json!({"allow_rules": [{"port": 22, "from": "192.0.2.0/24"}], "detected": ["8080 tcp"],
               "upstreams": ["192.0.2.5"], "route_rules": []}),
    );
    let twice_again = c.add(
        "ufw_desired_rules",
        json!([["192.0.2.0/24"]]),
        json!({"allow_rules": [{"port": 8080, "from": "192.0.2.5"}], "detected": ["8080 tcp"],
               "upstreams": ["192.0.2.5"]}),
    );
    let two_ways = c.add(
        "ufw_desired_rules",
        json!([["192.0.2.5/32", "192.0.2.5"]]),
        json!({
            "allow_rules": [{"port": 80, "from": "192.0.2.10/32"}, {"port": 80, "from": "192.0.2.10"}],
            "route_rules": [{"port": 9000, "from": "198.51.100.77/24"}],
            "detected": ["443 tcp"], "upstreams": ["192.0.2.7/255.255.255.255", "192.0.2.7"],
            "deny_egress": [{"to": "198.51.100.1/32", "port": 8200}, {"to": "198.51.100.1", "port": 8200}],
            "bridges": ["podman1"],
        }),
    );
    let two_ways_read = c.add(
        "ufw_parse_added",
        json!([[
            "ufw allow from 192.0.2.7 to any port 443 proto tcp comment 'agent-cloud:in:443/tcp:192.0.2.7/32'",
            "ufw allow from 192.0.2.7 to any port 443 proto tcp comment 'agent-cloud:in:443/tcp:192.0.2.7'",
        ]]),
        none.clone(),
    );
    let dual = c.add(
        "ufw_desired_rules",
        json!([["192.0.2.0/24", "any"]]),
        json!({"allow_rules": [{"port": 80, "from": "any"}], "bridges": ["podman1"],
               "deny_egress": [{"to": "198.51.100.3"}]}),
    );

    // ── ufw_absent / ufw_masking over the declared rules (computed in the same run)
    let held_desired =
        "(['192.0.2.0/24'] | ufw_desired_rules(allow_rules=[{'port': 8080, 'from': '192.0.2.5'}]))";
    let desired_expr = format!("{{{{ {held_desired} }}}}");
    let ssh_spec = format!("{{{{ {held_desired}[0].spec }}}}");
    let ssh_tag = format!("{{{{ {held_desired}[0].tag }}}}");
    let web_spec = format!("{{{{ {held_desired}[1].spec }}}}");
    let web_tag = format!("{{{{ {held_desired}[1].tag }}}}");
    let held_rules = c.add(
        "ufw_desired_rules",
        json!([["192.0.2.0/24"]]),
        json!({"allow_rules": [{"port": 8080, "from": "192.0.2.5"}]}),
    );
    let masked = held(
        "allow from 203.0.113.9 to any port 5432 proto tcp",
        &ssh_tag,
    );
    let absent_masked = c.add(
        "ufw_absent",
        json!([desired_expr, [masked, held(&web_spec, &web_tag)]]),
        none.clone(),
    );
    let absent_retag = c.add(
        "ufw_absent",
        json!([
            desired_expr,
            [held(&ssh_spec, ""), held(&web_spec, "agent-cloud:old")]
        ]),
        none.clone(),
    );
    let absent_held = c.add(
        "ufw_absent",
        json!([
            desired_expr,
            [held(&ssh_spec, &ssh_tag), held(&web_spec, &web_tag)]
        ]),
        none.clone(),
    );
    let masking = c.add(
        "ufw_masking",
        json!([
            [
                masked,
                held(&web_spec, &web_tag),
                held("allow 8443/tcp", "")
            ],
            desired_expr
        ]),
        none.clone(),
    );
    let bridge_desired = "(['192.0.2.0/24'] | ufw_desired_rules(bridges=['podman1']))";
    // Each declared rule held as stored: its own tag and spec, family and peer blank (the Python
    // `_held`). The rule's other keys ride along; ufw_absent reads only the tag and the spec.
    let bridge_held =
        format!("{{{{ {bridge_desired} | map('combine', {{'family': '', 'peer': ''}}) | list }}}}");
    let bridge_expr = format!("{{{{ {bridge_desired} }}}}");
    let reassert_off = c.add(
        "ufw_absent",
        json!([bridge_expr, bridge_held]),
        none.clone(),
    );
    let reassert_on = c.add(
        "ufw_absent",
        json!([bridge_expr, bridge_held]),
        json!({"reassert_dual_family": true}),
    );

    // ── ufw_delete_args
    let delete_in = c.add(
        "ufw_delete_args",
        json!(["allow from 192.0.2.7 to any port 443 proto tcp"]),
        none.clone(),
    );
    let delete_route = c.add(
        "ufw_delete_args",
        json!(["route allow from 192.0.2.7 to any port 443 proto tcp"]),
        none.clone(),
    );

    // ── an explicit null
    let null_port = c.desired(json!({"deny_egress": [{"to": "198.51.100.1", "port": null}]}));
    let null_proto =
        c.desired(json!({"allow_rules": [{"port": 80, "proto": null, "from": "192.0.2.5"}]}));

    // ── ufw_egress_problems
    let egress_ok = [
        json!({"to": "192.0.2.164", "port": 8200, "comment": "secret store"}),
        json!({"to": "192.0.2.117", "port": 3000}),
        json!({"to": "192.0.2.110/32", "port": 8006}),
        json!({"to": "192.0.2.5/255.255.255.255"}),
        json!({"to": " 192.0.2.9 ", "port": "8200", "proto": "any"}),
        json!({"to": "192.0.2.10", "port": 53, "proto": "udp"}),
        json!({"to": "2001:db8::1/128"}),
        json!({"to": "2001:db8::1", "broad": false}),
        json!({"to": "203.0.113.0/24", "broad": true, "reason": "third-party range"}),
        json!({"to": "192.0.2.128/25", "broad": "yes", "reason": "the upper half, not the whole prefix"}),
    ];
    let contains = "contains or equals the SSH CIDR 192.0.2.0/24";
    let egress_bad = [
        (
            json!({"to": "192.0.0.0/16", "broad": true, "reason": "stated"}),
            contains,
        ),
        (
            json!({"to": "192.0.2.0/24", "broad": true, "reason": "stated"}),
            contains,
        ),
        (
            json!({"to": "192.0.2.1/24", "broad": true, "reason": "stated"}),
            contains,
        ),
        (
            json!({"to": "192.0.2.77/255.255.255.0", "broad": true, "reason": "x"}),
            "contains or equals",
        ),
        (
            json!({"to": "0.0.0.0/0", "broad": true, "reason": "everything"}),
            "contains or equals the SSH CIDR 198.51.100.0/24",
        ),
        (json!({"to": "192.0.2.0/24"}), "names more than one address"),
        (
            json!({"to": "203.0.113.0/24"}),
            "names more than one address",
        ),
        (
            json!({"to": "203.0.113.0/24", "broad": true}),
            "requires a `reason`",
        ),
        (
            json!({"to": "203.0.113.0/24", "broad": true, "reason": "  "}),
            "requires a `reason`",
        ),
        (
            json!({"to": "203.0.113.0/24", "broad": "maybe", "reason": "x"}),
            "`broad` must be true or false",
        ),
        (json!({"port": 8200}), "`to` is required"),
        (json!({"to": ""}), "`to` is required"),
        (json!({"to": null}), "`to` is required"),
        (json!({"to": "any"}), "`to` is required"),
        (json!({"to": "vault.example.test"}), "`to` is required"),
        (json!({"to": "999.1.1.1"}), "`to` is required"),
        (
            json!({"to": "198.51.100.1", "port": null}),
            "is not one port",
        ),
        (json!({"to": "198.51.100.1", "port": ""}), "is not one port"),
        (json!({"to": "198.51.100.1", "port": 0}), "is not one port"),
        (
            json!({"to": "198.51.100.1", "port": 70000}),
            "is not one port",
        ),
        (
            json!({"to": "198.51.100.1", "port": "22,80"}),
            "is not one port",
        ),
        (
            json!({"to": "198.51.100.1", "port": true}),
            "is not one port",
        ),
        (
            json!({"to": "198.51.100.1", "port": 8200, "proto": null}),
            "is not tcp, udp or any",
        ),
        (
            json!({"to": "198.51.100.1", "port": 8200, "proto": "icmp"}),
            "is not tcp, udp or any",
        ),
        (json!("192.0.2.164"), "an entry is a mapping"),
    ];
    let egress_ok_ids: Vec<usize> = egress_ok
        .iter()
        .map(|e| c.add("ufw_egress_problems", json!([[e], mgmt()]), none.clone()))
        .collect();
    let egress_bad_ids: Vec<usize> = egress_bad
        .iter()
        .map(|(e, _)| c.add("ufw_egress_problems", json!([[e], mgmt()]), none.clone()))
        .collect();
    let egress = |c: &mut Calls, entries: Value, cidrs: Value| {
        c.add("ufw_egress_problems", json!([entries, cidrs]), json!({}))
    };
    let spelling = [
        egress(
            &mut c,
            json!([{"to": "192.0.2.5"}]),
            json!(["192.0.2.5/32"]),
        ),
        egress(
            &mut c,
            json!([{"to": "192.0.2.5/32"}]),
            json!(["192.0.2.5"]),
        ),
        egress(
            &mut c,
            json!([{"to": "192.0.2.0/28", "broad": true, "reason": "x"}]),
            json!(["192.0.2.5"]),
        ),
        egress(
            &mut c,
            json!([{"to": "2001:db8::/16", "broad": true, "reason": "x"}]),
            json!(["2001:db8::/32"]),
        ),
    ];
    let families = egress(
        &mut c,
        json!([{"to": "192.0.0.0/8", "broad": true, "reason": "x"}]),
        json!(["2001:db8::/32"]),
    );
    let mgmt_nothing = egress(&mut c, json!([]), json!(["mgmt-net"]));
    let mgmt_one = egress(&mut c, json!([{"to": "198.51.100.1"}]), json!(["mgmt-net"]));
    let any_host = egress(&mut c, json!([{"to": "198.51.100.1"}]), json!(["any"]));
    let any_all = egress(
        &mut c,
        json!([{"to": "::/0", "broad": true, "reason": "x"}]),
        json!(["any"]),
    );
    let every_problem = egress(
        &mut c,
        json!([{"to": "192.0.0.0/16", "port": 0, "proto": "icmp"}]),
        mgmt(),
    );

    // ── the outbound allowlist
    let allowed_order = c.desired(json!({
        "deny_egress": [{"to": "198.51.100.1"}],
        "allow_egress": [{"port": 443}, {"port": 53, "proto": "udp"}, {"port": 3100, "to": "192.0.2.9"}],
        "egress_audit": true,
    }));
    let audit_without_mode = c.desired(json!({"allow_egress": [{"port": 443}]}));
    let audit_without_list = c.desired(json!({"egress_audit": true}));
    let any_dual = c.add(
        "ufw_desired_rules",
        json!([[]]),
        json!({"allow_egress": [{"port": 443}, {"port": 3100, "to": "192.0.2.9"}], "egress_audit": true}),
    );
    let allow = |c: &mut Calls, args: Value| c.add("ufw_allow_egress_problems", args, json!({}));
    let allow_ok = [
        json!([{"port": 443}]),
        json!([{"port": "80:81", "proto": "tcp"}]),
        json!([{"port": 53, "proto": "udp", "to": "192.0.2.53"}]),
        json!([{"port": 3100, "to": "192.0.2.0/24"}]),
        json!([{"port": 443, "to": "any"}]),
    ];
    let allow_ok_ids: Vec<usize> = allow_ok
        .iter()
        .map(|e| allow(&mut c, json!([e, "audit"])))
        .collect();
    let allow_bad = [
        (json!({"proto": "tcp"}), "`port` must be one port"),
        (json!({"port": 0}), "`port` must be one port"),
        (json!({"port": 70000}), "`port` must be one port"),
        (json!({"port": "443,80"}), "`port` must be one port"),
        (json!({"port": "90:80"}), "`port` must be one port"),
        (json!({"port": null}), "`port` must be one port"),
        (
            json!({"port": 443, "proto": "any"}),
            "`proto` 'any' is not tcp or udp",
        ),
        (json!({"port": 443, "proto": "icmp"}), "is not tcp or udp"),
        (
            json!({"port": 443, "to": "github.com"}),
            "is not `any` or an IPv4/IPv6 address",
        ),
        (json!("443/tcp"), "an entry is a mapping"),
    ];
    let allow_bad_ids: Vec<usize> = allow_bad
        .iter()
        .map(|(e, _)| allow(&mut c, json!([[e], "audit"])))
        .collect();
    let mode_block = allow(&mut c, json!([[], "block"]));
    let mode_audit = allow(&mut c, json!([[], "audit"]));
    let dns = json!({"port": 53, "proto": "udp"});
    let probe = json!([{"host": "prime.plane.so", "port": 443}]);
    let enforce_ok = allow(&mut c, json!([[{"port": 443}, dns], "enforce", probe]));
    let enforce_range = allow(
        &mut c,
        json!([[{"port": "50:60", "proto": "udp"}], "enforce", probe]),
    );
    let enforce_no_dns = allow(
        &mut c,
        json!([[{"port": 443}, {"port": 53}], "enforce", probe]),
    );
    let enforce_no_probe = allow(&mut c, json!([[dns], "enforce", []]));
    let enforce_bad_probe = allow(
        &mut c,
        json!([[dns], "enforce", [{"host": "x"}, "x:443", {"host": "x", "port": 0}]]),
    );
    let enforce_empty = allow(&mut c, json!([[], "enforce"]));
    let timeouts = [
        (json!(5), true),
        (json!("30"), true),
        (json!(0), false),
        (json!("ten"), false),
        (json!(-1), false),
        (json!(true), false),
    ];
    let timeout_ids: Vec<usize> = timeouts
        .iter()
        .map(|(t, _)| {
            allow(
                &mut c,
                json!([[{"port": 53, "proto": "udp"}], "enforce",
                       [{"host": "prime.plane.so", "port": 443, "timeout": t}]]),
            )
        })
        .collect();

    let started = std::time::Instant::now();
    let r = run(&c);
    eprintln!(
        "the filter play ran {} calls in {:?}",
        c.0.len(),
        started.elapsed()
    );
    let mut k = Check::default();

    for ((given, stored), id) in address.iter().zip(&address_ids) {
        k.eq(
            &format!("test_an_address_is_spelled_as_ufw_stores_it[{given:?}]"),
            &r[*id],
            &json!(stored),
        );
    }
    for (spec, id) in admits.iter().zip(&admits_ids) {
        k.eq(
            &format!("test_a_rule_that_admits_ssh_is_recognised[{spec:?}]"),
            &r[*id],
            &json!(true),
        );
    }
    for (spec, id) in admits_not.iter().zip(&admits_not_ids) {
        k.eq(
            &format!("test_a_rule_that_does_not_admit_ssh_is_not[{spec:?}]"),
            &r[*id],
            &json!(false),
        );
    }
    for ((value, ok), id) in ipv4.iter().zip(&ipv4_ids) {
        k.eq(
            &format!("test_only_a_single_ipv4_address_is_one[{value}]"),
            &r[*id],
            &json!(ok),
        );
    }
    k.eq(
        "test_show_added_is_read_into_spec_and_tag",
        &r[show_added],
        &json!([
            {"spec": "allow from 192.0.2.0/24 to any port 22 proto tcp", "tag": "agent-cloud:in:22/tcp:192.0.2.0/24",
             "family": "in", "peer": "192.0.2.0/24"},
            {"spec": "route allow from 192.0.2.7 to any port 443 proto tcp",
             "tag": "agent-cloud:route:443/tcp:192.0.2.7", "family": "route", "peer": "192.0.2.7"},
            // the peer is the last field, so an IPv6 peer keeps its colons
            {"spec": "allow from 2001:db8::1 to any port 80 proto tcp", "tag": "agent-cloud:in:80/tcp:2001:db8::1",
             "family": "in", "peer": "2001:db8::1"},
            {"spec": "allow 8443/tcp", "tag": "", "family": "", "peer": ""},
            // a hand comment is not a tag; its spec still loses the comment
            {"spec": "allow from 203.0.113.9 to any port 5432 proto tcp", "tag": "", "family": "", "peer": ""},
            // a reserved-prefix comment IS a tag (it is pruned when undeclared), whatever its shape
            {"spec": "deny out to 198.51.100.2", "tag": "agent-cloud:legacy", "family": "", "peer": ""},
        ]),
    );

    let item = "test_the_tag_names_the_rule_with_the_peer_last";
    k.eq(
        item,
        &r[tag_ssh][0]["tag"],
        &json!("agent-cloud:in:22/tcp:192.0.2.0/24"),
    );
    k.eq(
        item,
        &r[tag_deny][1]["tag"],
        &json!("agent-cloud:out-deny:any:198.51.100.2"),
    );

    k.eq(
        "test_every_declared_rule_kind_has_its_command_tag_and_stored_spec_in_add_order",
        &fields(&r[every_kind], &["cmd", "tag", "spec", "family", "is_ssh"]),
        &json!([
            // SSH first: these must land before any other rule and before enable (anti-lockout)
            [
                "allow from 192.0.2.0/24 to any port 22 proto tcp",
                "agent-cloud:in:22/tcp:192.0.2.0/24",
                "allow from 192.0.2.0/24 to any port 22 proto tcp",
                "in",
                true
            ],
            // the command keeps the declared spelling; the tag and the spec use ufw's stored one
            [
                "allow from 198.51.100.77/24 to any port 22 proto tcp",
                "agent-cloud:in:22/tcp:198.51.100.0/24",
                "allow from 198.51.100.0/24 to any port 22 proto tcp",
                "in",
                true
            ],
            [
                "allow from 192.0.2.5/32 to any port 8080 proto tcp",
                "agent-cloud:in:8080/tcp:192.0.2.5",
                "allow from 192.0.2.5 to any port 8080 proto tcp",
                "in",
                false
            ],
            [
                "allow from 192.0.2.10 to any port 53 proto udp",
                "agent-cloud:in:53/udp:192.0.2.10",
                "allow from 192.0.2.10 to any port 53 proto udp",
                "in",
                false
            ],
            // FORWARD rules proto-first, the form verified on the first rootful host
            [
                "route allow proto tcp from 192.0.2.6 to any port 9000",
                "agent-cloud:route:9000/tcp:192.0.2.6",
                "route allow from 192.0.2.6 to any port 9000 proto tcp",
                "route",
                false
            ],
            // each detected port from every upstream, mirrored on FORWARD because rootful
            [
                "allow from 192.0.2.7 to any port 443 proto tcp",
                "agent-cloud:in:443/tcp:192.0.2.7",
                "allow from 192.0.2.7 to any port 443 proto tcp",
                "in",
                false
            ],
            [
                "route allow proto tcp from 192.0.2.7 to any port 443",
                "agent-cloud:route:443/tcp:192.0.2.7",
                "route allow from 192.0.2.7 to any port 443 proto tcp",
                "route",
                false
            ],
            [
                "allow from 192.0.2.8 to any port 443 proto tcp",
                "agent-cloud:in:443/tcp:192.0.2.8",
                "allow from 192.0.2.8 to any port 443 proto tcp",
                "in",
                false
            ],
            [
                "route allow proto tcp from 192.0.2.8 to any port 443",
                "agent-cloud:route:443/tcp:192.0.2.8",
                "route allow from 192.0.2.8 to any port 443 proto tcp",
                "route",
                false
            ],
            [
                "allow from 192.0.2.7 to any port 53 proto udp",
                "agent-cloud:in:53/udp:192.0.2.7",
                "allow from 192.0.2.7 to any port 53 proto udp",
                "in",
                false
            ],
            [
                "route allow proto udp from 192.0.2.7 to any port 53",
                "agent-cloud:route:53/udp:192.0.2.7",
                "route allow from 192.0.2.7 to any port 53 proto udp",
                "route",
                false
            ],
            [
                "allow from 192.0.2.8 to any port 53 proto udp",
                "agent-cloud:in:53/udp:192.0.2.8",
                "allow from 192.0.2.8 to any port 53 proto udp",
                "in",
                false
            ],
            [
                "route allow proto udp from 192.0.2.8 to any port 53",
                "agent-cloud:route:53/udp:192.0.2.8",
                "route allow from 192.0.2.8 to any port 53 proto udp",
                "route",
                false
            ],
            // bridge DNS: port 53 only, never the whole bridge
            [
                "allow in on podman1 to any port 53 proto udp",
                "agent-cloud:in-on:53/udp:podman1",
                "allow in on podman1 to any port 53 proto udp",
                "in-on",
                false
            ],
            [
                "allow in on podman1 to any port 53 proto tcp",
                "agent-cloud:in-on:53/tcp:podman1",
                "allow in on podman1 to any port 53 proto tcp",
                "in-on",
                false
            ],
            [
                "deny out to 198.51.100.1 port 8200 proto tcp",
                "agent-cloud:out-deny:8200/tcp:198.51.100.1",
                "deny out to 198.51.100.1 port 8200 proto tcp",
                "out-deny",
                false
            ],
            [
                "deny out to 198.51.100.2 port 53 proto udp",
                "agent-cloud:out-deny:53/udp:198.51.100.2",
                "deny out to 198.51.100.2 port 53 proto udp",
                "out-deny",
                false
            ],
            // no port: the destination is denied entirely
            [
                "deny out to 198.51.100.3",
                "agent-cloud:out-deny:any:198.51.100.3",
                "deny out to 198.51.100.3",
                "out-deny",
                false
            ],
        ]),
    );

    let cts = ["cmd", "tag", "spec"];
    k.eq(
        "test_detected_ports_get_no_forward_mirror_on_a_rootless_host",
        &fields(&r[rootless], &cts),
        &json!([
            [
                "allow from 192.0.2.0/24 to any port 22 proto tcp",
                "agent-cloud:in:22/tcp:192.0.2.0/24",
                "allow from 192.0.2.0/24 to any port 22 proto tcp"
            ],
            [
                "allow from 192.0.2.7 to any port 443 proto tcp",
                "agent-cloud:in:443/tcp:192.0.2.7",
                "allow from 192.0.2.7 to any port 443 proto tcp"
            ],
        ]),
    );

    // The regression guard for every host that declares no extras: no stray rule appears;
    // detected ports without an upstream are refused before this runs; none here, no rule.
    let item = "test_nothing_declared_beyond_ssh_yields_only_the_ssh_allows";
    let ssh_only = json!(["allow from 192.0.2.0/24 to any port 22 proto tcp"]);
    k.eq(item, &column(&r[nothing], "cmd"), &ssh_only);
    k.eq(item, &column(&r[nothing_detected], "cmd"), &ssh_only);

    let item = "test_a_rule_declared_twice_appears_once_and_keeps_its_first_place";
    k.eq(
        item,
        &fields(&r[twice], &["tag", "is_ssh"]),
        &json!([
            ["agent-cloud:in:22/tcp:192.0.2.0/24", true],
            ["agent-cloud:in:8080/tcp:192.0.2.5", false]
        ]),
    );
    k.eq(
        item,
        &column(&r[twice_again], "tag"),
        &json!([
            "agent-cloud:in:22/tcp:192.0.2.0/24",
            "agent-cloud:in:8080/tcp:192.0.2.5"
        ]),
    );

    // ufw stores both spellings as one rule; two tags for it would leave one declared tag absent.
    let item = "test_one_address_spelled_two_ways_is_one_rule_under_one_tag";
    k.eq(
        item,
        &column(&r[two_ways], "tag"),
        &json!([
            "agent-cloud:in:22/tcp:192.0.2.5",
            "agent-cloud:in:80/tcp:192.0.2.10",
            "agent-cloud:route:9000/tcp:198.51.100.0/24",
            "agent-cloud:in:443/tcp:192.0.2.7",
            "agent-cloud:in-on:53/udp:podman1",
            "agent-cloud:in-on:53/tcp:podman1",
            "agent-cloud:out-deny:8200/tcp:198.51.100.1",
        ]),
    );
    // and a stored tag's peer reads back in the same spelling, whichever version wrote it
    let read = &r[two_ways_read];
    k.that(
        item,
        read.as_array().is_some_and(|a| a.len() == 2)
            && read[0]["peer"] == json!("192.0.2.7")
            && read[1]["peer"] == json!("192.0.2.7")
            && read[0]["tag"] != read[1]["tag"],
        || format!("old and new tags must read back one peer: {read}"),
    );

    k.eq(
        "test_a_rule_naming_no_address_is_marked_dual_family",
        &dual_by_spec(&r[dual]),
        &json!({
            "allow from 192.0.2.0/24 to any port 22 proto tcp": false,
            "allow 22/tcp": true,       // from any: ufw's short form, both families
            "allow 80/tcp": true,
            "allow in on podman1 to any port 53 proto udp": true,
            "allow in on podman1 to any port 53 proto tcp": true,
            "deny out to 198.51.100.3": false,
        }),
    );

    let item = "test_a_rule_is_held_only_by_its_tag_and_its_spec_together";
    let desired = &r[held_rules];
    k.that(
        item,
        desired.as_array().is_some_and(|a| a.len() == 2),
        || format!("two declared rules (SSH, web): {desired}"),
    );
    // the same tag over another rule does not stand in for the declared one (SSH included)
    k.eq(item, &r[absent_masked], &json!([desired[0]]));
    // the declared spec without its tag (untagged, or an old tag) is absent too: it is retagged
    k.eq(item, &r[absent_retag], desired);
    k.eq(item, &r[absent_held], &json!([]));
    let masked_held = held(
        "allow from 203.0.113.9 to any port 5432 proto tcp",
        desired[0]["tag"].as_str().unwrap_or("<no SSH tag>"),
    );
    k.eq(item, &r[masking], &json!([masked_held]));

    let item = "test_a_rule_naming_no_address_is_re_asserted_even_when_held";
    k.eq(item, &r[reassert_off], &json!([]));
    k.eq(
        item,
        &column(&r[reassert_on], "spec"),
        &json!([
            "allow in on podman1 to any port 53 proto udp",
            "allow in on podman1 to any port 53 proto tcp"
        ]),
    );

    let item = "test_a_rule_is_deleted_by_its_spec";
    k.eq(
        item,
        &r[delete_in],
        &json!("delete allow from 192.0.2.7 to any port 443 proto tcp"),
    );
    k.eq(
        item,
        &r[delete_route],
        &json!("route delete allow from 192.0.2.7 to any port 443 proto tcp"),
    );

    // A null egress port must not widen a scoped denial to the whole destination, and a null
    // proto must not become tcp: both render as written, which ufw refuses (the playbook's
    // egress validation refuses them first).
    let item = "test_an_explicit_null_is_never_read_as_a_default";
    k.that(
        item,
        r[null_port].as_array().is_some_and(|a| a.len() == 2),
        || format!("SSH and the denial: {}", r[null_port]),
    );
    k.eq(
        item,
        &r[null_port][1]["cmd"],
        &json!("deny out to 198.51.100.1 port None proto tcp"),
    );
    k.that(
        item,
        r[null_proto].as_array().is_some_and(|a| a.len() == 2),
        || format!("SSH and the allow: {}", r[null_proto]),
    );
    k.eq(
        item,
        &r[null_proto][1]["cmd"],
        &json!("allow from 192.0.2.5 to any port 80 proto None"),
    );

    // The shape the runner hosts declare: every target INSIDE the management prefix, which is
    // why the guard must never be phrased as "reject anything within an SSH CIDR" (MISTAKES 2.5).
    for (entry, id) in egress_ok.iter().zip(&egress_ok_ids) {
        k.eq(
            &format!("test_an_egress_denial_the_platform_really_declares_is_accepted[{entry}]"),
            &r[*id],
            &json!([]),
        );
    }
    for ((entry, why), id) in egress_bad.iter().zip(&egress_bad_ids) {
        k.names(
            &format!("test_an_egress_denial_that_is_not_scoped_is_refused[{entry}]"),
            &r[*id],
            why,
        );
    }

    // A /32 SSH CIDR and the bare address are one host; a broad range over a single-host SSH
    // source contains it; IPv6: a supernet is refused, and families never contain each other.
    let item = "test_egress_containment_is_compared_in_the_spelling_ufw_stores";
    for id in spelling {
        k.that(item, !empty(&r[id]), || {
            format!("call {id}: refused, got {}", r[id])
        });
    }
    k.eq(item, &r[families], &json!([]));

    let item = "test_an_ssh_cidr_that_is_not_an_address_cannot_prove_a_denial_safe";
    k.eq(item, &r[mgmt_nothing], &json!([]));
    k.that(
        item,
        r[mgmt_one].as_array().is_some_and(|a| a.len() == 1)
            && r[mgmt_one][0]
                .as_str()
                .is_some_and(|p| p.contains("SSH CIDR 'mgmt-net' is not an address")),
        || format!("one problem naming the CIDR: {}", r[mgmt_one]),
    );
    // `any` is the whole address space: a host inside it is fine, the whole space is not
    k.eq(item, &r[any_host], &json!([]));
    k.that(item, !empty(&r[any_all]), || {
        format!("refused, got {}", r[any_all])
    });

    // not broad, contains the SSH CIDR, port, proto
    k.that(
        "test_every_problem_of_an_entry_is_reported_not_only_the_first",
        r[every_problem].as_array().is_some_and(|a| a.len() == 4),
        || format!("four problems: {}", r[every_problem]),
    );

    // Stored forms measured on ufw 0.36.2 (Ubuntu 24.04, 2026-11-09): `allow out to any port 443
    // proto tcp` is stored as `allow out 443/tcp`, a scoped one keeps `to ... port ... proto`, and
    // the audit rule is `allow out log to any`.
    let got = fields(&r[allowed_order], &cts);
    k.eq(
        "test_allowed_egress_comes_after_the_denials_and_the_audit_rule_last",
        &Value::Array(got.as_array().map(|a| a[1..].to_vec()).unwrap_or_default()),
        &json!([
            [
                "deny out to 198.51.100.1",
                "agent-cloud:out-deny:any:198.51.100.1",
                "deny out to 198.51.100.1"
            ],
            [
                "allow out to any port 443 proto tcp",
                "agent-cloud:out-allow:443/tcp:any",
                "allow out 443/tcp"
            ],
            [
                "allow out to any port 53 proto udp",
                "agent-cloud:out-allow:53/udp:any",
                "allow out 53/udp"
            ],
            [
                "allow out to 192.0.2.9 port 3100 proto tcp",
                "agent-cloud:out-allow:3100/tcp:192.0.2.9",
                "allow out to 192.0.2.9 port 3100 proto tcp"
            ],
            [
                "allow out log to any",
                "agent-cloud:out-audit:any:any",
                "allow out log to any"
            ],
        ]),
    );

    let item = "test_the_audit_rule_exists_only_in_audit_mode_and_only_with_an_allowlist";
    for id in [audit_without_mode, audit_without_list] {
        let logged = column(&r[id], "cmd")
            .as_array()
            .unwrap()
            .iter()
            .any(|cmd| cmd.as_str().is_some_and(|cmd| cmd.contains("log")));
        k.that(item, !logged, || format!("no audit rule: {}", r[id]));
    }

    k.eq(
        "test_a_rule_to_any_address_is_dual_family_and_a_scoped_one_is_not",
        &dual_by_spec(&r[any_dual]),
        &json!({"allow out 443/tcp": true, "allow out to 192.0.2.9 port 3100 proto tcp": false,
                "allow out log to any": true}),
    );

    for (entries, id) in allow_ok.iter().zip(&allow_ok_ids) {
        k.eq(
            &format!(
                "test_an_allowlist_the_platform_would_declare_is_accepted_in_audit[{entries}]"
            ),
            &r[*id],
            &json!([]),
        );
    }
    for ((entry, why), id) in allow_bad.iter().zip(&allow_bad_ids) {
        k.names(
            &format!("test_a_malformed_allowlist_entry_is_refused_by_name[{entry}]"),
            &r[*id],
            why,
        );
    }

    let item = "test_an_unknown_mode_is_refused_even_without_entries";
    k.eq(
        item,
        &r[mode_block],
        &json!(["firewall_egress_mode 'block' is not audit or enforce"]),
    );
    k.eq(item, &r[mode_audit], &json!([]));

    let item = "test_enforcing_needs_dns_out_and_a_probe";
    k.eq(item, &r[enforce_ok], &json!([]));
    k.eq(item, &r[enforce_range], &json!([]));
    k.eq(
        item,
        &r[enforce_no_dns],
        &json!(["an enforced allowlist must allow 53/udp out, or the host cannot resolve names"]),
    );
    k.eq(
        item,
        &r[enforce_no_probe],
        &json!(["an enforced allowlist needs firewall_egress_probes [{host, port}] to prove it after enabling"]),
    );
    let bad = &r[enforce_bad_probe];
    k.that(
        item,
        bad.as_array().is_some_and(|a| {
            a.len() == 3
                && a.iter().all(|p| {
                    p.as_str()
                        .is_some_and(|p| p.contains("a probe is a mapping"))
                })
        }),
        || format!("three probe problems: {bad}"),
    );

    k.eq(
        "test_enforcing_an_empty_allowlist_is_refused",
        &r[enforce_empty],
        &json!(["firewall_egress_mode enforce needs a non-empty firewall_allow_egress"]),
    );

    for ((timeout, ok), id) in timeouts.iter().zip(&timeout_ids) {
        k.that(
            &format!("test_a_probe_timeout_is_a_whole_number_of_seconds[{timeout}]"),
            empty(&r[*id]) == *ok,
            || format!("accepted must be {ok}: {}", r[*id]),
        );
    }

    assert!(
        k.failures.is_empty(),
        "{} case(s) failed:\n{}",
        k.failures.len(),
        k.failures.join("\n")
    );
    assert_eq!(
        k.items.len(),
        PYTEST_ITEMS,
        "pytest items asserted (one per parametrized case, one per plain test)"
    );
}
