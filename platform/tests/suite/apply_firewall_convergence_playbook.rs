//! apply-firewall.yml converges: it tags every rule it adds and prunes tagged rules no longer
//! declared (playbook tier of the firewall group, plan 24).
//!
//! The real play's tasks are lifted (from the SSH-CIDR refusal to the final report) and run
//! against a stub `ufw` and `podman` on PATH (`platform/tests/ufw_stub.py`, kept as a Python
//! fixture), which keep the firewall in a JSON file and reproduce the ufw 0.36.2 behaviour the
//! playbook relies on. Left out: the sudo resolver, fact gathering, the non-Linux skip, the apt
//! install and its dry-run stop, and each task's own `become`; none of them decides which rule
//! is added or deleted. Runs go through `process::ansible_playbook`, so they are bounded, and
//! confined to the test's directory where a kernel sandbox (sandbox-exec, bwrap) is present
//! (`process::sandbox`). Cases that differ only in their inputs share one run over
//! several hosts (`run_many`), each host with its own stub state, since every ansible-playbook
//! spawn costs seconds.
//!
//! ## Port map (pytest -> Rust fn)
//!
//! Every test of `test_apply_firewall_convergence.py` that runs ansible-playbook keeps its name
//! without the `test_` prefix; the stub-only test is in `apply_firewall_convergence.rs`.
//!
//! | pytest | Rust fn |
//! |---|---|
//! | `test_every_declared_rule_kind_is_added_with_its_tag` | `every_declared_rule_kind_is_added_with_its_tag` |
//! | `test_a_second_run_changes_nothing` | `a_second_run_changes_nothing` |
//! | `test_a_rule_from_the_whole_address_space_converges` | `a_rule_from_the_whole_address_space_converges` |
//! | `test_a_rule_retagged_by_the_add_is_not_then_pruned_as_stale` | `a_rule_retagged_by_the_add_is_not_then_pruned_as_stale` |
//! | `test_a_stale_tagged_rule_of_every_kind_is_pruned` | `a_stale_tagged_rule_of_every_kind_is_pruned` |
//! | `test_untagged_rules_are_kept_and_reported_and_a_declared_one_is_adopted` | `untagged_rules_are_kept_and_reported_and_a_declared_one_is_adopted` |
//! | `test_ssh_is_never_pruned_while_declared_and_an_old_cidr_goes` | `ssh_is_never_pruned_while_declared_and_an_old_cidr_goes` |
//! | `test_a_declared_rule_that_did_not_land_stops_the_run_before_any_delete` | `a_declared_rule_that_did_not_land_stops_the_run_before_any_delete` |
//! | `test_check_mode_reports_the_plan_and_changes_nothing` | `check_mode_reports_the_plan_and_changes_nothing` |
//! | `test_narrowing_a_subnet_rule_to_group_rules_removes_only_the_tagged_subnet_rule` | `narrowing_a_subnet_rule_to_group_rules_removes_only_the_tagged_subnet_rule` |
//! | `test_narrowing_ssh_without_the_controller_cidr_is_refused_before_any_delete` | `narrowing_ssh_without_the_controller_cidr_is_refused_before_any_delete` |
//! | `test_empty_detection_holds_the_upstream_rules_unless_told_it_is_real` | `empty_detection_holds_the_upstream_rules_unless_told_it_is_real` |
//! | `test_ssh_cidrs_spelled_non_canonically_are_matched_as_ufw_stores_them` | `ssh_cidrs_spelled_non_canonically_are_matched_as_ufw_stores_them` |
//! | `test_the_post_prune_check_fails_when_a_delete_took_an_ssh_allow` | `the_post_prune_check_fails_when_a_delete_took_an_ssh_allow` |
//! | `test_a_dual_family_rule_stored_for_one_family_only_is_still_pruned` | `a_dual_family_rule_stored_for_one_family_only_is_still_pruned` |
//! | `test_a_stale_rule_that_admits_ssh_in_any_spelling_is_refused` | `a_stale_rule_that_admits_ssh_in_any_spelling_is_refused` |
//! | `test_a_stale_udp_only_port_22_rule_is_pruned_without_the_controller_cidr` | `a_stale_udp_only_port_22_rule_is_pruned_without_the_controller_cidr` |
//! | `test_the_controller_cidr_is_compared_in_the_spelling_ufw_stores` | `the_controller_cidr_is_compared_in_the_spelling_ufw_stores` |
//! | `test_rules_tagged_in_a_declared_spelling_are_retagged_then_converge` | `rules_tagged_in_a_declared_spelling_are_retagged_then_converge` |
//! | `test_a_dry_run_over_older_tags_plans_no_delete_of_a_declared_rule` | `a_dry_run_over_older_tags_plans_no_delete_of_a_declared_rule` |
//! | `test_an_upstream_declared_non_canonically_still_holds_its_rules_on_empty_detection` | `an_upstream_declared_non_canonically_still_holds_its_rules_on_empty_detection` |
//! | `test_a_lost_ip_family_of_a_bridge_rule_is_restored_and_a_whole_one_is_unchanged` | `a_lost_ip_family_of_a_bridge_rule_is_restored_and_a_whole_one_is_unchanged` |
//! | `test_a_declared_tag_over_another_rule_does_not_stand_in_for_the_declared_rule` | `a_declared_tag_over_another_rule_does_not_stand_in_for_the_declared_rule` |
//! | `test_an_egress_denial_that_is_not_scoped_is_refused_before_any_rule_is_added` | `an_egress_denial_that_is_not_scoped_is_refused_before_any_rule_is_added` |
//! | `test_a_dry_run_over_untagged_equivalent_rules_lists_the_adds_and_does_not_fail` | `a_dry_run_over_untagged_equivalent_rules_lists_the_adds_and_does_not_fail` |
//! | `test_a_real_run_over_untagged_equivalent_rules_adds_the_tagged_ones_and_passes` | `a_real_run_over_untagged_equivalent_rules_adds_the_tagged_ones_and_passes` |
//! | `test_audit_adds_the_allowlist_then_the_audit_rule_last_and_keeps_outgoing_allowed` | `audit_adds_the_allowlist_then_the_audit_rule_last_and_keeps_outgoing_allowed` |
//! | `test_a_newly_declared_allow_lands_before_the_audit_rule` | `a_newly_declared_allow_lands_before_the_audit_rule` |
//! | `test_enforce_denies_outgoing_drops_the_audit_rule_and_passes_its_probe` | `enforce_denies_outgoing_drops_the_audit_rule_and_passes_its_probe` |
//! | `test_enforce_denies_outgoing_only_after_enabling_and_converges` | `enforce_denies_outgoing_only_after_enabling_and_converges` |
//! | `test_enforce_puts_outgoing_back_to_allow_when_a_probe_cannot_be_reached` | `enforce_puts_outgoing_back_to_allow_when_a_probe_cannot_be_reached` |
//! | `test_a_narrowed_allowlist_on_an_enforcing_host_is_proven_again_or_outgoing_is_allowed_again` | `a_narrowed_allowlist_on_an_enforcing_host_is_proven_again_or_outgoing_is_allowed_again` |
//! | `test_an_enforced_allowlist_without_dns_or_a_probe_is_refused_before_any_rule_is_added` | `an_enforced_allowlist_without_dns_or_a_probe_is_refused_before_any_rule_is_added` |
//! | `test_a_dry_run_of_an_enforced_allowlist_changes_nothing_and_says_it_would` | `a_dry_run_of_an_enforced_allowlist_changes_nothing_and_says_it_would` |
//! | `test_a_dry_run_reports_outgoing_denied_on_a_host_that_declares_no_allowlist` | `a_dry_run_reports_outgoing_denied_on_a_host_that_declares_no_allowlist` |
//! | `test_a_host_without_an_allowlist_keeps_outgoing_allowed_and_no_outbound_allow` | `a_host_without_an_allowlist_keeps_outgoing_allowed_and_no_outbound_allow` |
//! | (new: the guard had no behavioural test) | `controller_cidr_outside_the_ssh_cidrs_is_refused_before_any_rule_is_added` |
//! | `test_a_new_denial_is_refused_behind_held_outbound_allows_and_lands_first_beside_new_ones` | `a_new_denial_is_refused_behind_held_outbound_allows_and_lands_first_beside_new_ones` |

use std::collections::{BTreeMap, HashMap};
use std::net::TcpListener;
use std::path::{Path, PathBuf};

use agent_cloud_tests::http::refused_port;
use agent_cloud_tests::{playbook, process, repo, text};
use regex::Regex;
use serde_json::{Map, Value, json};
use tempfile::TempDir;

const PLAYBOOK: &str = "platform/playbooks/apply-firewall.yml";
const STUB: &str = "platform/tests/ufw_stub.py";
const FIRST: &str = "Refuse to proceed without SSH allow CIDRs (anti-lockout)";
const SKIPPED: [&str; 5] = [
    "Read whether ufw is installed",
    "Install ufw",
    "Dry run, ufw not installed: report",
    "Dry run, ufw not installed: record the verdict",
    "Dry run, ufw not installed: stop this host",
];
const SSH: &str = "192.0.2.0/24";
const AUDIT: &str = "allow out log to any";
/// The whole ufw traffic of a converged host: the two reads taken before adding.
const CONVERGED_READS: [&str; 2] = [
    r#"["ufw", "show", "added"]"#,
    r#"["ufw", "status", "verbose"]"#,
];
fn controller() -> Value {
    json!({"firewall_controller_cidr": SSH})
}

fn allow_out() -> Value {
    json!([{"port": 443}, {"port": 53, "proto": "udp"}])
}

/// `^\["ufw", "(?!show|status)`: a ufw invocation that is not a read.
fn mutating(line: &str) -> bool {
    line.strip_prefix(r#"["ufw", ""#)
        .is_some_and(|rest| !rest.starts_with("show") && !rest.starts_with("status"))
}

fn tag(family: &str, port_proto: &str, peer: &str) -> String {
    format!("agent-cloud:{family}:{port_proto}:{peer}")
}

/// A TCP port on 127.0.0.1 that accepts connections while the listener is held.
fn listening() -> (TcpListener, u16) {
    let listener = TcpListener::bind(("127.0.0.1", 0)).expect("bind a loopback port");
    let port = listener.local_addr().unwrap().port();
    (listener, port)
}

/// Host vars: the allowlist enforced, proven by one probe to 127.0.0.1:`port`.
fn enforce(port: u16, timeout: Option<u64>) -> Value {
    let mut probe = json!({"host": "127.0.0.1", "port": port});
    if let Some(t) = timeout {
        probe["timeout"] = json!(t);
    }
    json!({"firewall_allow_egress": allow_out(), "firewall_egress_mode": "enforce",
           "firewall_egress_probes": [probe]})
}

/// One firewalled host: its vars, its stub state before the run, its podman fixture.
#[derive(Default, Clone)]
struct Host {
    vars: Option<Value>,
    state: Option<Value>,
    podman: Option<Value>,
}

/// The options of a run beside its hosts.
#[derive(Default)]
struct Opts<'a> {
    groups: Option<Value>,
    check: bool,
    drop: Option<&'a str>,
    collateral: Option<&'a str>,
}

impl Opts<'_> {
    /// A dry run (`--check`).
    fn check() -> Self {
        Self {
            check: true,
            ..Self::default()
        }
    }
}

/// One ansible-playbook run.
struct Run {
    stdout: String,
    stderr: String,
    ok: bool,
}

impl Run {
    fn all(&self) -> String {
        format!("{}{}", self.stdout, self.stderr)
    }

    /// The stdout from `marker` on.
    fn from(&self, marker: &str) -> &str {
        let at = self
            .stdout
            .find(marker)
            .unwrap_or_else(|| panic!("{marker} not in:\n{}", self.stdout));
        &self.stdout[at..]
    }
}

/// A host's stub state, log and podman fixture, under `dir/<host>`.
fn files(dir: &Path, host: &str) -> (PathBuf, PathBuf, PathBuf) {
    let base = dir.join(host);
    (
        base.join("ufw.json"),
        base.join("ufw.log"),
        base.join("podman.json"),
    )
}

/// Runs the lifted play ONCE over several firewalled hosts; returns the run and each host's
/// stub state after it (`None` when the stub never wrote one). Each host keeps its own stub
/// state, log and podman fixture (play-level `environment` from its host vars). A host that
/// fails an assert stops alone; the others run on.
fn run_many(
    dir: &Path,
    hosts: &[(&str, Host)],
    opts: Opts,
) -> (Run, HashMap<String, Option<Value>>) {
    let play = playbook::plays(PLAYBOOK).remove(0);
    let all = play["tasks"]
        .as_array()
        .expect("the firewall play has tasks");
    let first = all
        .iter()
        .position(|t| t["name"] == FIRST)
        .expect("the first lifted task");
    let mut tasks: Vec<Value> = all[first..]
        .iter()
        .filter(|t| !SKIPPED.contains(&t["name"].as_str().unwrap_or("")))
        .cloned()
        .map(|mut t| {
            t.as_object_mut().unwrap().remove("become");
            t
        })
        .collect();
    // Print each host's verdict, so a test can read what the record play would receive.
    tasks.push(json!({"name": "Show the verdict",
        "ansible.builtin.debug": {"msg": "VERDICT {{ _fw_verdict | to_json }}"}}));
    let lifted = json!([{"hosts": "targets", "gather_facts": false, "become": false,
        "vars": play["vars"],
        "environment": {"UFW_STUB_STATE": "{{ ufw_stub_state }}", "UFW_STUB_LOG": "{{ ufw_stub_log }}",
                        "PODMAN_STUB": "{{ podman_stub }}"},
        "tasks": tasks}]);
    let play_path = playbook::write(dir, "play.json", &lifted);

    let mut targets = Map::new();
    for (name, spec) in hosts {
        let (state, log, podman) = files(dir, name);
        std::fs::create_dir_all(state.parent().unwrap()).unwrap();
        if let Some(s) = &spec.state {
            std::fs::write(&state, s.to_string()).unwrap();
        }
        let fixture = spec.podman.clone().unwrap_or_else(|| json!({}));
        std::fs::write(&podman, fixture.to_string()).unwrap();
        let mut vars = json!({"ansible_connection": "local", "firewall_ssh_cidrs": [SSH]});
        let map = vars.as_object_mut().unwrap();
        if let Some(Value::Object(v)) = &spec.vars {
            map.extend(v.clone());
        }
        map.insert("ufw_stub_state".into(), json!(state.display().to_string()));
        map.insert("ufw_stub_log".into(), json!(log.display().to_string()));
        map.insert("podman_stub".into(), json!(podman.display().to_string()));
        targets.insert((*name).into(), vars);
    }
    let mut children = Map::new();
    children.insert("targets".into(), json!({"hosts": targets}));
    if let Some(Value::Object(groups)) = &opts.groups {
        for (group, members) in groups {
            let hosts: Map<String, Value> = members
                .as_object()
                .unwrap()
                .iter()
                .map(|(h, a)| (h.clone(), json!({"ansible_host": a})))
                .collect();
            children.insert(group.clone(), json!({"hosts": hosts}));
        }
    }
    let inv = playbook::write(dir, "inv.json", &json!({"all": {"children": children}}));

    let bin = dir.join("bin");
    std::fs::create_dir_all(&bin).unwrap();
    let stub = repo::path(STUB);
    for name in ["ufw", "podman"] {
        let path = bin.join(name);
        std::fs::write(
            &path,
            format!(
                "#!/bin/sh\nSTUB_AS={name} exec python3 {} \"$@\"\n",
                sh_quote(&stub.display().to_string())
            ),
        )
        .unwrap();
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).unwrap();
    }
    let mut cmd = process::ansible_playbook(
        dir,
        &inv.display().to_string(),
        &play_path.display().to_string(),
    );
    // The stubs come first, so neither a real ufw nor a real podman can be reached.
    let mut path = bin.into_os_string();
    path.push(":");
    path.push(std::env::var_os("PATH").unwrap_or_default());
    cmd.env("PATH", path);
    if let Some(drop) = opts.drop {
        cmd.env("UFW_STUB_DROP", drop);
    }
    if let Some(collateral) = opts.collateral {
        cmd.env("UFW_STUB_COLLATERAL", collateral);
    }
    if opts.check {
        cmd.arg("--check");
    }
    let out = process::run(&mut cmd);
    let run = Run {
        stdout: String::from_utf8_lossy(&out.stdout).into_owned(),
        stderr: String::from_utf8_lossy(&out.stderr).into_owned(),
        ok: out.status.success(),
    };
    let mut after = HashMap::new();
    for (name, _) in hosts {
        let (state, _, _) = files(dir, name);
        let value = std::fs::read_to_string(&state)
            .ok()
            .map(|text| serde_json::from_str(&text).unwrap());
        after.insert((*name).to_string(), value);
    }
    (run, after)
}

/// `text` as one single-quoted sh word: each `'` becomes `'\''`.
fn sh_quote(text: &str) -> String {
    format!("'{}'", text.replace('\'', "'\\''"))
}

/// Runs the lifted play once against the one host `target`; returns the run and its state after.
fn run(
    dir: &Path,
    vars: Value,
    state: Option<Value>,
    podman: Option<Value>,
    opts: Opts,
) -> (Run, Value) {
    let host = Host {
        vars: Some(vars),
        state,
        podman,
    };
    let (run, mut after) = run_many(dir, &[("target", host)], opts);
    let state = after
        .remove("target")
        .flatten()
        .unwrap_or_else(|| panic!("no stub state for target:\n{}", run.all()));
    (run, state)
}

/// The stored rules as spec -> comment.
fn rules(state: &Value) -> BTreeMap<String, String> {
    state["rules"]
        .as_array()
        .unwrap()
        .iter()
        .map(|r| (r[0].as_str().unwrap().into(), r[1].as_str().unwrap().into()))
        .collect()
}

/// The stored specs, in order.
fn specs(state: &Value) -> Vec<String> {
    state["rules"]
        .as_array()
        .unwrap()
        .iter()
        .map(|r| r[0].as_str().unwrap().to_string())
        .collect()
}

fn map(pairs: &[(&str, &str)]) -> BTreeMap<String, String> {
    pairs
        .iter()
        .map(|(k, v)| ((*k).into(), (*v).into()))
        .collect()
}

fn ssh_rule() -> (String, String) {
    (
        format!("allow from {SSH} to any port 22 proto tcp"),
        tag("in", "22/tcp", SSH),
    )
}

fn changed(run: &Run) -> u32 {
    Regex::new(r"target\s*:.*changed=(\d+)")
        .unwrap()
        .captures(&run.stdout)
        .unwrap_or_else(|| panic!("no recap:\n{}", run.stdout))[1]
        .parse()
        .unwrap()
}

fn log(dir: &Path, host: &str) -> Vec<String> {
    std::fs::read_to_string(files(dir, host).1)
        .map(|t| t.lines().map(String::from).collect())
        .unwrap_or_default()
}

fn deletes(lines: &[String]) -> Vec<&String> {
    lines.iter().filter(|l| l.contains(r#""delete""#)).collect()
}

fn mutations(lines: &[String]) -> Vec<&String> {
    lines.iter().filter(|l| mutating(l)).collect()
}

fn ufw_calls(lines: &[String]) -> Vec<&str> {
    lines
        .iter()
        .filter(|l| l.starts_with(r#"["ufw""#))
        .map(String::as_str)
        .collect()
}

/// The verdict the record play would receive, from the "Show the verdict" debug.
fn verdict(run: &Run) -> Value {
    let caps = Regex::new(r#""msg": ("VERDICT .*")"#)
        .unwrap()
        .captures(&run.stdout)
        .unwrap_or_else(|| panic!("no verdict:\n{}", run.stdout));
    let msg: String = serde_json::from_str(&caps[1]).unwrap();
    serde_json::from_str(&msg["VERDICT ".len()..]).unwrap()
}

fn active(rules: Value) -> Option<Value> {
    Some(json!({"active": true, "rules": rules}))
}

#[test]
fn every_declared_rule_kind_is_added_with_its_tag() {
    let dir = TempDir::new().unwrap();
    let (r, state) = run(
        dir.path(),
        json!({
            "firewall_rootful": true,
            "firewall_allow_rules": [{"port": 8080, "from": "192.0.2.5"}],
            "firewall_allow_groups": [{"port": 53, "proto": "udp", "group": "members"}],
            "firewall_route_rules": [{"port": 9000, "from": "192.0.2.6"}],
            "firewall_upstream_source": "192.0.2.7",
            "firewall_deny_egress": [{"to": "198.51.100.1", "port": 8200}, {"to": "198.51.100.2"}],
        }),
        None,
        Some(
            json!({"ports": {"c1": ["443/tcp -> 0.0.0.0:443", "80/tcp -> 127.0.0.1:80"]},
                    "bridges": {"podman": "podman1"}}),
        ),
        Opts {
            groups: Some(json!({"members": {"a": "192.0.2.10"}})),
            ..Opts::default()
        },
    );
    assert!(r.ok, "{}", r.all());
    let ssh = ssh_rule();
    assert_eq!(
        rules(&state),
        map(&[
            (&ssh.0, &ssh.1),
            (
                "allow from 192.0.2.5 to any port 8080 proto tcp",
                &tag("in", "8080/tcp", "192.0.2.5")
            ),
            (
                "allow from 192.0.2.10 to any port 53 proto udp",
                &tag("in", "53/udp", "192.0.2.10")
            ),
            (
                "route allow from 192.0.2.6 to any port 9000 proto tcp",
                &tag("route", "9000/tcp", "192.0.2.6")
            ),
            (
                "allow from 192.0.2.7 to any port 443 proto tcp",
                &tag("in", "443/tcp", "192.0.2.7")
            ),
            (
                "route allow from 192.0.2.7 to any port 443 proto tcp",
                &tag("route", "443/tcp", "192.0.2.7")
            ),
            (
                "allow in on podman1 to any port 53 proto udp",
                &tag("in-on", "53/udp", "podman1")
            ),
            (
                "allow in on podman1 to any port 53 proto tcp",
                &tag("in-on", "53/tcp", "podman1")
            ),
            (
                "deny out to 198.51.100.1 port 8200 proto tcp",
                &tag("out-deny", "8200/tcp", "198.51.100.1")
            ),
            (
                "deny out to 198.51.100.2",
                &tag("out-deny", "any", "198.51.100.2")
            ),
        ])
    );
    assert_eq!(state["active"], json!(true));
    // a run that changed the firewall reports the state it left, not the one it found
    assert!(r.from("TASK [Report]").contains("Status: active"));
}

#[test]
fn a_second_run_changes_nothing() {
    let dir = TempDir::new().unwrap();
    let host = json!({"firewall_allow_rules": [{"port": 8080, "from": "192.0.2.5"}],
                      "firewall_deny_egress": [{"to": "198.51.100.1"}]});
    let (first, state) = run(dir.path(), host.clone(), None, None, Opts::default());
    assert!(first.ok, "{}", first.all());
    assert!(changed(&first) > 0);
    std::fs::remove_file(files(dir.path(), "target").1).unwrap();
    let (second, again) = run(dir.path(), host, None, None, Opts::default());
    assert!(second.ok, "{}", second.all());
    assert_eq!(changed(&second), 0, "{}", second.stdout);
    assert_eq!(again, state);
    // Converged: the two reads taken before adding are the whole run. No add, no re-read,
    // no final status capture (the report reuses the first read).
    assert_eq!(ufw_calls(&log(dir.path(), "target")), CONVERGED_READS);
    assert!(second.from("TASK [Report]").contains("Status: active"));
}

#[test]
fn a_rule_from_the_whole_address_space_converges() {
    // ufw prints 0.0.0.0/0 as `any` (the short form `allow 8080/tcp`); compared literally,
    // the declared rule would never match its stored form and fail the drift guard.
    let dir = TempDir::new().unwrap();
    let host = json!({"firewall_allow_rules": [{"port": 8080, "from": "0.0.0.0/0"}]});
    let (first, state) = run(dir.path(), host.clone(), None, None, Opts::default());
    assert!(first.ok, "{}", first.all());
    assert!(rules(&state).contains_key("allow 8080/tcp"));
    let (second, again) = run(dir.path(), host, Some(state.clone()), None, Opts::default());
    assert!(second.ok, "{}", second.all());
    assert_eq!(changed(&second), 0, "{}", second.stdout);
    assert_eq!(again, state);
}

#[test]
fn a_rule_retagged_by_the_add_is_not_then_pruned_as_stale() {
    // Stored under the tag an older spelling of its declaration produced. The add retags it;
    // the prune plan must read the rules AFTER the add, or it deletes the declared rule by spec.
    let dir = TempDir::new().unwrap();
    let spec = "allow from 192.0.2.5 to any port 8080 proto tcp";
    let ssh = ssh_rule();
    let before = active(json!([
        [ssh.0, ssh.1],
        [spec, tag("in", "8080/tcp", "192.0.2.5/32")]
    ]));
    let (r, state) = run(
        dir.path(),
        json!({"firewall_allow_rules": [{"port": 8080, "from": "192.0.2.5"}]}),
        before,
        None,
        Opts::default(),
    );
    assert!(r.ok, "{}", r.all());
    assert_eq!(rules(&state)[spec], tag("in", "8080/tcp", "192.0.2.5"));
    assert!(deletes(&log(dir.path(), "target")).is_empty());
}

#[test]
fn a_stale_tagged_rule_of_every_kind_is_pruned() {
    let dir = TempDir::new().unwrap();
    let stale = json!([
        [
            "allow from 198.51.100.7 to any port 9000 proto tcp",
            tag("in", "9000/tcp", "198.51.100.7")
        ],
        [
            "route allow from 198.51.100.7 to any port 9001 proto tcp",
            tag("route", "9001/tcp", "198.51.100.7")
        ],
        [
            "allow in on podman9 to any port 53 proto udp",
            tag("in-on", "53/udp", "podman9")
        ],
        [
            "deny out to 198.51.100.8",
            tag("out-deny", "any", "198.51.100.8")
        ],
    ]);
    let (r, state) = run(dir.path(), json!({}), active(stale), None, Opts::default());
    assert!(r.ok, "{}", r.all());
    let ssh = ssh_rule();
    assert_eq!(rules(&state), map(&[(&ssh.0, &ssh.1)]));
    let lines = log(dir.path(), "target");
    let deletes = deletes(&lines);
    // By spec, never by number: every delete names the rule, and the route one is `route delete`.
    let by_number = Regex::new(r#""delete", "\d+""#).unwrap();
    assert!(
        deletes.len() == 4 && !deletes.iter().any(|l| by_number.is_match(l)),
        "{deletes:?}"
    );
    let joined = deletes
        .iter()
        .map(|s| s.as_str())
        .collect::<Vec<_>>()
        .join("\n");
    assert!(
        joined.contains(r#"["ufw", "route", "delete", "allow", "from", "198.51.100.7""#),
        "{joined}"
    );
}

#[test]
fn untagged_rules_are_kept_and_reported_and_a_declared_one_is_adopted() {
    let dir = TempDir::new().unwrap();
    let ssh = ssh_rule();
    let untagged = json!([
        ["allow 22/tcp", ""],
        [
            "allow from 203.0.113.9 to any port 5432 proto tcp",
            "set by hand"
        ],
        [ssh.0, ""]
    ]);
    let (r, state) = run(
        dir.path(),
        json!({}),
        active(untagged),
        None,
        Opts::default(),
    );
    assert!(r.ok, "{}", r.all());
    assert_eq!(
        rules(&state),
        map(&[
            ("allow 22/tcp", ""),
            (
                "allow from 203.0.113.9 to any port 5432 proto tcp",
                "set by hand"
            ),
            // the untagged copy of a declared rule is retagged by the add, not duplicated
            (&ssh.0, &ssh.1),
        ])
    );
    let report = r.from("TASK [Report untagged rules");
    assert!(report.contains("holds 2 untagged ufw rule(s)"), "{report}");
    assert!(
        report.contains("allow 22/tcp")
            && report.contains("allow from 203.0.113.9 to any port 5432 proto tcp")
    );
}

#[test]
fn ssh_is_never_pruned_while_declared_and_an_old_cidr_goes() {
    let dir = TempDir::new().unwrap();
    let old = "198.51.100.0/24";
    let ssh = ssh_rule();
    let before = active(json!([
        [
            format!("allow from {old} to any port 22 proto tcp"),
            tag("in", "22/tcp", old)
        ],
        // a declared CIDR's SSH allow under a tag this playbook no longer writes
        [ssh.0, "agent-cloud:ssh:legacy"],
    ]));
    let (r, state) = run(dir.path(), controller(), before, None, Opts::default());
    assert!(r.ok, "{}", r.all());
    assert_eq!(rules(&state), map(&[(&ssh.0, &ssh.1)]));
}

#[test]
fn a_declared_rule_that_did_not_land_stops_the_run_before_any_delete() {
    // The new SSH allow silently fails to land while the old CIDR's tagged allow is stale:
    // pruning would leave ZERO SSH allows. The drift guard must refuse first.
    let dir = TempDir::new().unwrap();
    let old = "198.51.100.0/24";
    let before = active(json!([[
        format!("allow from {old} to any port 22 proto tcp"),
        tag("in", "22/tcp", old)
    ]]));
    let drop = format!("from {SSH} to any port 22");
    let (r, state) = run(
        dir.path(),
        controller(),
        before.clone(),
        None,
        Opts {
            drop: Some(&drop),
            ..Opts::default()
        },
    );
    assert!(!r.ok, "{}", r.all());
    assert!(
        r.stdout.contains("Declared rules not found") && r.stdout.contains("Nothing was deleted"),
        "{}",
        r.stdout
    );
    assert_eq!(state["rules"], before.unwrap()["rules"]);
    assert!(deletes(&log(dir.path(), "target")).is_empty());
}

#[test]
fn check_mode_reports_the_plan_and_changes_nothing() {
    let dir = TempDir::new().unwrap();
    let before = json!({"active": false, "rules": [
        ["allow from 198.51.100.7 to any port 9000 proto tcp", tag("in", "9000/tcp", "198.51.100.7")],
        ["allow 22/tcp", ""],
        [ssh_rule().0, "agent-cloud:ssh:legacy"],
    ]});
    let (r, state) = run(
        dir.path(),
        json!({"firewall_allow_rules": [{"port": 8080, "from": "192.0.2.5"}]}),
        Some(before.clone()),
        None,
        Opts::check(),
    );
    assert!(r.ok, "{}", r.all());
    assert_eq!(state, before);
    let lines = log(dir.path(), "target");
    assert!(mutations(&lines).is_empty(), "{lines:?}");
    let plan = r.from("TASK [Report the convergence plan]");
    assert!(
        plan.contains(&tag("in", "8080/tcp", "192.0.2.5"))
            && plan.contains(&tag("in", "22/tcp", SSH)),
        "{plan}"
    );
    assert!(
        plan.contains("allow from 198.51.100.7 to any port 9000 proto tcp"),
        "{plan}"
    );
    // the declared SSH allow under an old tag is retagged by a real run, never planned for deletion
    let delete = Regex::new(r#"(?s)"delete_stale_tagged": \[(.*?)\]"#)
        .unwrap()
        .captures(plan)
        .unwrap()[1]
        .to_string();
    assert!(!delete.contains("port 22"), "{delete}");
    assert!(
        plan.contains(r#""would_set_defaults_or_enable": true"#),
        "{plan}"
    );
}

#[test]
fn narrowing_a_subnet_rule_to_group_rules_removes_only_the_tagged_subnet_rule() {
    let dir = TempDir::new().unwrap();
    let subnet = "192.0.2.0/24";
    let wide = json!({"firewall_allow_rules": [{"port": 53, "proto": "udp", "from": subnet},
                                               {"port": 53, "proto": "tcp", "from": subnet}]});
    let (first, _) = run(
        dir.path(),
        wide,
        active(json!([["allow 8443/tcp", ""]])),
        None,
        Opts::default(),
    );
    assert!(first.ok, "{}", first.all());
    let narrow = json!({"firewall_allow_groups": [{"port": 53, "proto": "udp", "group": "members"},
                                                  {"port": 53, "proto": "tcp", "group": "members"}]});
    let members = json!({"members": {"a": "192.0.2.10", "b": "192.0.2.11"}});
    let (r, state) = run(
        dir.path(),
        narrow,
        None,
        None,
        Opts {
            groups: Some(members),
            ..Opts::default()
        },
    );
    assert!(r.ok, "{}", r.all());
    let ssh = ssh_rule();
    let mut expected = map(&[("allow 8443/tcp", ""), (&ssh.0, &ssh.1)]);
    for host in ["192.0.2.10", "192.0.2.11"] {
        for p in ["udp", "tcp"] {
            expected.insert(
                format!("allow from {host} to any port 53 proto {p}"),
                tag("in", &format!("53/{p}"), host),
            );
        }
    }
    assert_eq!(rules(&state), expected);
}

#[test]
fn narrowing_ssh_without_the_controller_cidr_is_refused_before_any_delete() {
    let dir = TempDir::new().unwrap();
    let old = "198.51.100.0/24";
    let spec = format!("allow from {old} to any port 22 proto tcp");
    let before = active(json!([[spec, tag("in", "22/tcp", old)]]));
    for check in [true, false] {
        let (r, state) = run(
            dir.path(),
            json!({}),
            before.clone(),
            None,
            Opts {
                check,
                ..Opts::default()
            },
        );
        assert!(!r.ok, "check={check}:\n{}", r.all());
        assert!(
            r.stdout
                .contains(&format!("would prune SSH from {old} on target"))
                && r.stdout.contains("declare firewall_controller_cidr"),
            "check={check}:\n{}",
            r.stdout
        );
        assert!(rules(&state).contains_key(&spec), "check={check}");
        assert!(
            deletes(&log(dir.path(), "target")).is_empty(),
            "check={check}"
        );
    }
}

#[test]
fn empty_detection_holds_the_upstream_rules_unless_told_it_is_real() {
    let dir = TempDir::new().unwrap();
    let upstream = "192.0.2.7";
    let stale = json!([
        [
            "allow from 192.0.2.7 to any port 443 proto tcp",
            tag("in", "443/tcp", upstream)
        ],
        [
            "route allow from 192.0.2.7 to any port 443 proto tcp",
            tag("route", "443/tcp", upstream)
        ],
        [
            "allow from 198.51.100.7 to any port 9000 proto tcp",
            tag("in", "9000/tcp", "198.51.100.7")
        ],
    ]);
    let case = |vars: Value| Host {
        vars: Some(vars),
        state: active(stale.clone()),
        podman: Some(json!({"ports": {}})),
    };
    let (r, states) = run_many(
        dir.path(),
        &[
            ("held", case(json!({"firewall_upstream_source": upstream}))),
            (
                "pruned",
                case(
                    json!({"firewall_upstream_source": upstream, "firewall_prune_when_detection_empty": true}),
                ),
            ),
        ],
        Opts::default(),
    );
    assert!(r.ok, "{}", r.all());
    let held = rules(states["held"].as_ref().unwrap());
    let spec = |i: usize| stale[i][0].as_str().unwrap().to_string();
    assert!(
        held.contains_key(&spec(0)) && held.contains_key(&spec(1)) && !held.contains_key(&spec(2)),
        "{held:?}"
    );
    assert!(
        r.stdout
            .contains("Port detection on held found no published ports, so 2 stale rule(s)"),
        "{}",
        r.stdout
    );
    assert!(
        !r.stdout.contains("Port detection on pruned"),
        "{}",
        r.stdout
    );
    let ssh = ssh_rule();
    assert_eq!(
        rules(states["pruned"].as_ref().unwrap()),
        map(&[(&ssh.0, &ssh.1)])
    );
}

#[test]
fn ssh_cidrs_spelled_non_canonically_are_matched_as_ufw_stores_them() {
    // ufw stores 192.0.2.5/32 as 192.0.2.5 and 198.51.100.77/24 as 198.51.100.0/24. Compared
    // raw, both would be planned for deletion and refused (no controller CIDR declared).
    let dir = TempDir::new().unwrap();
    let before = active(json!([
        [
            "allow from 192.0.2.5 to any port 22 proto tcp",
            "agent-cloud:ssh:legacy"
        ],
        [
            "allow from 198.51.100.0/24 to any port 22 proto tcp",
            "agent-cloud:ssh:legacy"
        ],
    ]));
    let (r, state) = run(
        dir.path(),
        json!({"firewall_ssh_cidrs": ["192.0.2.5/32", "198.51.100.77/24"]}),
        before.clone(),
        None,
        Opts::check(),
    );
    assert!(r.ok, "{}", r.all());
    assert_eq!(Some(state), before);
    let plan = r.from("TASK [Report the convergence plan]");
    assert!(plan.contains(r#""delete_stale_tagged": []"#), "{plan}");
}

#[test]
fn the_post_prune_check_fails_when_a_delete_took_an_ssh_allow() {
    // The declared SSH allow is in place; the stub's delete of the stale rule also takes it.
    // On the second host a hand rule still carries the SSH allow's tag after the delete: the
    // check reads tag AND spec, so that tag alone does not pass it.
    let dir = TempDir::new().unwrap();
    let ssh = ssh_rule();
    let stale = json!([
        "allow from 198.51.100.8 to any port 9001 proto tcp",
        tag("in", "9001/tcp", "198.51.100.8")
    ]);
    let rules = json!([[ssh.0, ssh.1], stale]);
    let masked = json!([
        ["allow from 203.0.113.9 to any port 5432 proto tcp", ssh.1],
        [ssh.0, ssh.1],
        stale
    ]);
    let (r, _) = run_many(
        dir.path(),
        &[
            (
                "plain",
                Host {
                    state: active(rules),
                    ..Host::default()
                },
            ),
            (
                "masked",
                Host {
                    state: active(masked),
                    ..Host::default()
                },
            ),
        ],
        Opts {
            collateral: Some("port 22"),
            ..Opts::default()
        },
    );
    assert!(!r.ok, "{}", r.all());
    for host in ["plain", "masked"] {
        assert!(
            r.stdout.contains(&format!(
                "After pruning, {host} is missing a declared SSH allow"
            )),
            "{host}:\n{}",
            r.stdout
        );
    }
    assert!(!r.stdout.contains("TASK [Enable UFW]"));
}

#[test]
fn a_dual_family_rule_stored_for_one_family_only_is_still_pruned() {
    let dir = TempDir::new().unwrap();
    let before = active(json!([[
        "allow in on podman9 to any port 53 proto udp",
        tag("in-on", "53/udp", "podman9"),
        "v4"
    ]]));
    let (r, state) = run(dir.path(), json!({}), before, None, Opts::default());
    assert!(r.ok, "{}", r.all());
    let ssh = ssh_rule();
    assert_eq!(rules(&state), map(&[(&ssh.0, &ssh.1)]));
}

const SSH_SPELLINGS: [&str; 5] = [
    "allow from 198.51.100.0/24 to any port 22", // proto any: ufw prints no proto
    "allow from 198.51.100.0/24 to any port 22,2222 proto tcp", // multiport
    "allow from 198.51.100.0/24 to any port 20:30 proto tcp", // range
    "allow from 198.51.100.0/24",                // no port clause: every port
    "route allow from 198.51.100.0/24 to any port 22 proto tcp",
];

#[test]
fn a_stale_rule_that_admits_ssh_in_any_spelling_is_refused() {
    // One host per spelling, one run: every host must refuse on its own. The spellings
    // themselves are unit-tested in test_ufw_rules_filter.py; this proves the wiring.
    let dir = TempDir::new().unwrap();
    let names: Vec<String> = (0..SSH_SPELLINGS.len()).map(|i| format!("h{i}")).collect();
    let hosts: Vec<(&str, Host)> = names
        .iter()
        .zip(SSH_SPELLINGS)
        .map(|(name, spec)| {
            let state = active(json!([[spec, "agent-cloud:in:9999/tcp:198.51.100.0/24"]]));
            (
                name.as_str(),
                Host {
                    state,
                    ..Host::default()
                },
            )
        })
        .collect();
    let (r, states) = run_many(dir.path(), &hosts, Opts::default());
    assert!(!r.ok, "{}", r.all());
    for (host, spec) in names.iter().zip(SSH_SPELLINGS) {
        assert!(
            r.stdout
                .contains(&format!("would prune SSH from 198.51.100.0/24 on {host};")),
            "{spec}:\n{}",
            r.stdout
        );
        assert!(
            rules(states[host].as_ref().unwrap()).contains_key(spec),
            "{spec}"
        );
        assert!(deletes(&log(dir.path(), host)).is_empty(), "{spec}");
    }
}

#[test]
fn a_stale_udp_only_port_22_rule_is_pruned_without_the_controller_cidr() {
    let dir = TempDir::new().unwrap();
    let spec = "allow from 198.51.100.0/24 to any port 22 proto udp";
    let (r, state) = run(
        dir.path(),
        json!({}),
        active(json!([[spec, tag("in", "22/udp", "198.51.100.0/24")]])),
        None,
        Opts::default(),
    );
    assert!(r.ok, "{}", r.all());
    assert!(!rules(&state).contains_key(spec));
}

#[test]
fn the_controller_cidr_is_compared_in_the_spelling_ufw_stores() {
    // Declared as 192.0.2.5/32, controller given as 192.0.2.5: one source, so the old CIDR's
    // SSH allow may be pruned.
    let dir = TempDir::new().unwrap();
    let old = "198.51.100.0/24";
    let before = active(json!([[
        format!("allow from {old} to any port 22 proto tcp"),
        tag("in", "22/tcp", old)
    ]]));
    let (r, state) = run(
        dir.path(),
        json!({"firewall_ssh_cidrs": ["192.0.2.5/32"], "firewall_controller_cidr": "192.0.2.5"}),
        before,
        None,
        Opts::default(),
    );
    assert!(r.ok, "{}", r.all());
    assert_eq!(
        rules(&state),
        map(&[(
            "allow from 192.0.2.5 to any port 22 proto tcp",
            &tag("in", "22/tcp", "192.0.2.5")
        )])
    );
}

#[test]
fn rules_tagged_in_a_declared_spelling_are_retagged_then_converge() {
    // Tags once carried the peer as DECLARED; they now carry it as ufw stores it. A host whose
    // rules were tagged by the earlier version (`.../32`, a CIDR with host bits set) holds
    // tags this run no longer declares. They are retagged by the add — not pruned before it,
    // SSH included, with no controller CIDR declared — and the next run changes nothing.
    let dir = TempDir::new().unwrap();
    let (ssh, web, route) = ("192.0.2.5/32", "198.51.100.77/24", "192.0.2.6/32");
    let host = json!({"firewall_ssh_cidrs": [ssh], "firewall_rootful": true, "firewall_detect_ports": false,
        "firewall_allow_rules": [{"port": 8080, "from": web}],
        "firewall_route_rules": [{"port": 9000, "from": route}],
        "firewall_deny_egress": [{"to": "198.51.100.1/32", "port": 8200}]});
    // spec, (family, port/proto, stored peer), declared peer
    let specs = [
        (
            "allow from 192.0.2.5 to any port 22 proto tcp",
            ("in", "22/tcp", "192.0.2.5"),
            ssh,
        ),
        (
            "allow from 198.51.100.0/24 to any port 8080 proto tcp",
            ("in", "8080/tcp", "198.51.100.0/24"),
            web,
        ),
        (
            "route allow from 192.0.2.6 to any port 9000 proto tcp",
            ("route", "9000/tcp", "192.0.2.6"),
            route,
        ),
        (
            "deny out to 198.51.100.1 port 8200 proto tcp",
            ("out-deny", "8200/tcp", "198.51.100.1"),
            "198.51.100.1/32",
        ),
    ];
    let before: Vec<Value> = specs
        .iter()
        .map(|(spec, (f, pp, _), declared)| json!([spec, tag(f, pp, declared)]))
        .collect();
    let (first, state) = run(
        dir.path(),
        host.clone(),
        active(json!(before)),
        None,
        Opts::default(),
    );
    assert!(first.ok, "{}", first.all());
    assert!(changed(&first) > 0);
    let expected: BTreeMap<String, String> = specs
        .iter()
        .map(|(spec, (f, pp, peer), _)| ((*spec).into(), tag(f, pp, peer)))
        .collect();
    assert_eq!(rules(&state), expected);
    let lines = log(dir.path(), "target");
    assert!(deletes(&lines).is_empty(), "{lines:?}");
    std::fs::remove_file(files(dir.path(), "target").1).unwrap();
    let (second, again) = run(dir.path(), host, Some(state.clone()), None, Opts::default());
    assert!(second.ok, "{}", second.all());
    assert_eq!(changed(&second), 0, "{}", second.stdout);
    assert_eq!(again, state);
    assert_eq!(ufw_calls(&log(dir.path(), "target")), CONVERGED_READS);
}

#[test]
fn a_dry_run_over_older_tags_plans_no_delete_of_a_declared_rule() {
    // In --check there is no add and no re-read, so a declared rule still carrying an older
    // tag spelling must not appear in the delete plan, and a static port-22 allow from a
    // source outside firewall_ssh_cidrs must not trip the anti-orphan guard (review of #348).
    let dir = TempDir::new().unwrap();
    let host = json!({"firewall_ssh_cidrs": ["192.0.2.5"], "firewall_detect_ports": false,
        "firewall_allow_rules": [{"port": 8080, "from": "198.51.100.77/24"},
                                 {"port": 22, "from": "203.0.113.9/32"}]});
    let before = active(json!([
        [
            "allow from 192.0.2.5 to any port 22 proto tcp",
            tag("in", "22/tcp", "192.0.2.5")
        ],
        [
            "allow from 198.51.100.0/24 to any port 8080 proto tcp",
            tag("in", "8080/tcp", "198.51.100.77/24")
        ],
        [
            "allow from 203.0.113.9 to any port 22 proto tcp",
            tag("in", "22/tcp", "203.0.113.9/32")
        ],
    ]));
    let (r, state) = run(dir.path(), host, before.clone(), None, Opts::check());
    assert!(r.ok, "{}", r.all());
    assert_eq!(Some(state), before);
    let plan = r.from("TASK [Report the convergence plan]");
    assert!(plan.contains(r#""delete_stale_tagged": []"#), "{plan}");
}

#[test]
fn an_upstream_declared_non_canonically_still_holds_its_rules_on_empty_detection() {
    // The held set is recognised by peer; the tag's peer is ufw's spelling (whichever version
    // wrote it), so the declared upstream is compared in that spelling too.
    let dir = TempDir::new().unwrap();
    let a = "allow from 192.0.2.7 to any port 443 proto tcp";
    let b = "route allow from 192.0.2.7 to any port 443 proto tcp";
    let stale = json!([
        [a, tag("in", "443/tcp", "192.0.2.7")],
        [b, tag("route", "443/tcp", "192.0.2.7/32")]
    ]);
    let (r, state) = run(
        dir.path(),
        json!({"firewall_upstream_source": "192.0.2.7/32"}),
        active(stale),
        Some(json!({"ports": {}})),
        Opts::default(),
    );
    assert!(r.ok, "{}", r.all());
    assert!(rules(&state).contains_key(a) && rules(&state).contains_key(b));
    assert!(
        r.stdout
            .contains("found no published ports, so 2 stale rule(s)"),
        "{}",
        r.stdout
    );
}

#[test]
fn a_lost_ip_family_of_a_bridge_rule_is_restored_and_a_whole_one_is_unchanged() {
    // `show added` prints a dual-family rule once whichever halves are stored, so presence by
    // tag cannot see a lost half. The rule is re-added every run: the lost half comes back,
    // and on a host holding both ufw only skips, which is not a change.
    let dir = TempDir::new().unwrap();
    let rootful = json!({"firewall_rootful": true, "firewall_detect_ports": false});
    let bridge = json!({"bridges": {"podman": "podman1"}});
    let ssh = ssh_rule();
    let whole = json!([
        [ssh.0, ssh.1],
        [
            "allow in on podman1 to any port 53 proto udp",
            tag("in-on", "53/udp", "podman1")
        ],
        [
            "allow in on podman1 to any port 53 proto tcp",
            tag("in-on", "53/tcp", "podman1")
        ]
    ]);
    let mut lost = whole.clone();
    lost[1].as_array_mut().unwrap().push(json!("v4")); // the v6 half of the udp rule is gone
    let (r, states) = run_many(
        dir.path(),
        &[
            (
                "lost",
                Host {
                    vars: Some(rootful.clone()),
                    podman: Some(bridge.clone()),
                    state: active(lost),
                },
            ),
            (
                "whole",
                Host {
                    vars: Some(rootful),
                    podman: Some(bridge),
                    state: active(whole.clone()),
                },
            ),
        ],
        Opts::default(),
    );
    assert!(r.ok, "{}", r.all());
    assert_eq!(states["lost"].as_ref().unwrap()["rules"], whole);
    assert_eq!(states["whole"].as_ref().unwrap()["rules"], whole);
    assert!(
        text::any_line(&r.stdout, r"\blost\s*:.*changed=[1-9]"),
        "{}",
        r.stdout
    );
    assert!(
        text::any_line(&r.stdout, r"\bwhole\s*:.*changed=0"),
        "{}",
        r.stdout
    );
    let lines = log(dir.path(), "whole");
    let adds = mutations(&lines);
    assert!(
        adds.len() == 2 && adds.iter().all(|l| l.contains(r#""in", "on", "podman1""#)),
        "{adds:?}"
    );
    // The whole cost of a converged rootful-podman host (apply-firewall.yml header, the
    // playbook README): the two reads plus two skipped adds per bridge; no re-read, no final
    // status capture.
    let mut expected = CONVERGED_READS.to_vec();
    expected.extend(adds.iter().map(|s| s.as_str()));
    assert_eq!(ufw_calls(&lines), expected);
}

#[test]
fn a_declared_tag_over_another_rule_does_not_stand_in_for_the_declared_rule() {
    // A hand rule under the reserved prefix carries the SSH allow's tag. The SSH allow is still
    // added; the masking rule is kept (its tag is declared, so it is never pruned) and reported.
    let dir = TempDir::new().unwrap();
    let masked = "allow from 203.0.113.9 to any port 5432 proto tcp";
    let ssh = ssh_rule();
    let (r, state) = run(
        dir.path(),
        json!({}),
        active(json!([[masked, ssh.1]])),
        None,
        Opts::default(),
    );
    assert!(r.ok, "{}", r.all());
    assert_eq!(rules(&state), map(&[(masked, &ssh.1), (&ssh.0, &ssh.1)]));
    assert!(
        r.stdout
            .contains("holds 1 rule(s) whose comment names a declared rule")
            && r.stdout.contains(masked),
        "{}",
        r.stdout
    );
    assert!(deletes(&log(dir.path(), "target")).is_empty());
}

#[test]
fn an_egress_denial_that_is_not_scoped_is_refused_before_any_rule_is_added() {
    // ufw_egress_problems' cases are unit-tested in test_ufw_rules_filter.py; this proves the
    // playbook refuses on it before a single rule is added, one host per shape.
    let dir = TempDir::new().unwrap();
    let bad = [
        ("null_port", json!({"to": "198.51.100.1", "port": null})),
        ("empty_port", json!({"to": "198.51.100.1", "port": ""})),
        ("port_zero", json!({"to": "198.51.100.1", "port": 0})),
        ("port_high", json!({"to": "198.51.100.1", "port": 70000})),
        (
            "null_proto",
            json!({"to": "198.51.100.1", "port": 8200, "proto": null}),
        ),
        (
            "icmp",
            json!({"to": "198.51.100.1", "port": 8200, "proto": "icmp"}),
        ),
        // a supernet of the SSH CIDR: refused even flagged broad and justified
        (
            "supernet",
            json!({"to": "192.0.0.0/16", "broad": true, "reason": "stated"}),
        ),
    ];
    let mut hosts: Vec<(&str, Host)> = bad
        .iter()
        .map(|(name, deny)| {
            let vars = Some(json!({"firewall_deny_egress": [deny]}));
            (
                *name,
                Host {
                    vars,
                    ..Host::default()
                },
            )
        })
        .collect();
    hosts.push((
        "good",
        Host {
            vars: Some(json!({"firewall_deny_egress": [
        {"to": "198.51.100.1", "port": "8200", "proto": "any"},
        {"to": "198.51.100.2", "port": 53, "proto": "udp"}]})),
            ..Host::default()
        },
    ));
    let (r, states) = run_many(dir.path(), &hosts, Opts::default());
    assert!(!r.ok, "{}", r.all());
    for (name, _) in &bad {
        assert!(
            text::any_line(
                &r.stdout,
                &format!(r"\b{}\s*:.*failed=1", regex::escape(name))
            ),
            "{name}"
        );
        assert!(mutations(&log(dir.path(), name)).is_empty(), "{name}");
        assert!(states[*name].is_none(), "{name}"); // the stub never ran a write: no state file
    }
    assert!(
        text::any_line(&r.stdout, r"\bgood\s*:.*failed=0"),
        "{}",
        r.stdout
    );
    let ssh = ssh_rule();
    assert_eq!(
        rules(states["good"].as_ref().unwrap()),
        map(&[
            (&ssh.0, &ssh.1),
            (
                "deny out to 198.51.100.1 port 8200",
                &tag("out-deny", "8200/any", "198.51.100.1")
            ),
            (
                "deny out to 198.51.100.2 port 53 proto udp",
                &tag("out-deny", "53/udp", "198.51.100.2")
            ),
        ])
    );
}

#[test]
fn a_dry_run_over_untagged_equivalent_rules_lists_the_adds_and_does_not_fail() {
    // Untagged rules equal to the declared ones are not this playbook's: a dry run plans the
    // tagged adds, and since it changed nothing the live state still lacks them. That is what
    // it WOULD change, not a failure (a dry run against a host whose rules were added by hand).
    let dir = TempDir::new().unwrap();
    let before = active(json!([
        [ssh_rule().0, ""],
        ["allow from 192.0.2.5 to any port 8080 proto tcp", ""]
    ]));
    let host = json!({"firewall_detect_ports": false, "firewall_allow_rules": [{"port": 8080, "from": "192.0.2.5"}]});
    let (r, state) = run(dir.path(), host, before.clone(), None, Opts::check());
    assert!(r.ok, "{}", r.all());
    assert_eq!(Some(state), before);
    let v = verdict(&r);
    assert_eq!(v["errors"], json!([]), "{v}");
    let would = v["would_change"].as_array().unwrap();
    assert_eq!(would.len(), 1, "{v}");
    let gap = would[0].as_str().unwrap();
    assert!(
        gap.starts_with("declared rules not held: ")
            && gap.contains(&tag("in", "8080/tcp", "192.0.2.5")),
        "{gap}"
    );
    assert!(gap.contains(&tag("in", "22/tcp", SSH)), "{gap}");
}

#[test]
fn a_real_run_over_untagged_equivalent_rules_adds_the_tagged_ones_and_passes() {
    let dir = TempDir::new().unwrap();
    let before = active(json!([[ssh_rule().0, ""]]));
    let (r, state) = run(
        dir.path(),
        json!({"firewall_detect_ports": false}),
        before,
        None,
        Opts::default(),
    );
    assert!(r.ok, "{}", r.all());
    assert!(
        rules(&state)
            .values()
            .any(|c| *c == tag("in", "22/tcp", SSH))
    );
    let v = verdict(&r);
    assert!(
        v["errors"] == json!([]) && v["would_change"] == json!([]),
        "{v}"
    );
}

// ── Outbound allowlist (firewall_allow_egress, firewall_egress_mode, firewall_egress_probes) ──

#[test]
fn audit_adds_the_allowlist_then_the_audit_rule_last_and_keeps_outgoing_allowed() {
    let dir = TempDir::new().unwrap();
    let vars = json!({"firewall_allow_egress": allow_out()});
    let (r, state) = run(dir.path(), vars.clone(), None, None, Opts::default());
    assert!(r.ok, "{}", r.all());
    let order = specs(&state);
    assert_eq!(
        order[order.len() - 3..],
        ["allow out 443/tcp", "allow out 53/udp", AUDIT]
    );
    assert_eq!(rules(&state)[AUDIT], tag("out-audit", "any", "any"));
    assert_eq!(state["outgoing"], json!("allow"));
    assert_eq!(verdict(&r)["errors"], json!([]));
    // converged: a second run changes nothing and leaves the audit rule where it is
    let (r2, state2) = run(dir.path(), vars, None, None, Opts::default());
    assert!(r2.ok, "{}", r2.all());
    assert_eq!(changed(&r2), 0, "{}", r2.stdout);
    assert_eq!(specs(&state2), order);
}

#[test]
fn a_newly_declared_allow_lands_before_the_audit_rule() {
    let dir = TempDir::new().unwrap();
    run(
        dir.path(),
        json!({"firewall_allow_egress": allow_out()}),
        None,
        None,
        Opts::default(),
    );
    let (r, state) = run(
        dir.path(),
        json!({"firewall_allow_egress": [{"port": 443}, {"port": 53, "proto": "udp"}, {"port": 80}]}),
        None,
        None,
        Opts::default(),
    );
    assert!(r.ok, "{}", r.all());
    let order = specs(&state);
    assert_eq!(order[order.len() - 2..], ["allow out 80/tcp", AUDIT]);
}

#[test]
fn enforce_denies_outgoing_drops_the_audit_rule_and_passes_its_probe() {
    let dir = TempDir::new().unwrap();
    run(
        dir.path(),
        json!({"firewall_allow_egress": allow_out()}),
        None,
        None,
        Opts::default(),
    );
    let (_listener, port) = listening();
    let (r, state) = run(dir.path(), enforce(port, None), None, None, Opts::default());
    assert!(r.ok, "{}", r.all());
    assert_eq!(state["outgoing"], json!("deny"));
    let held = rules(&state);
    assert!(!held.contains_key(AUDIT));
    assert!(
        held.contains_key("allow out 443/tcp") && held.contains_key("allow out 53/udp"),
        "{held:?}"
    );
    assert_eq!(verdict(&r)["errors"], json!([]));
}

#[test]
fn enforce_denies_outgoing_only_after_enabling_and_converges() {
    // From an inactive host: the deny lands inside the rescued probe block, after `ufw --force
    // enable` and the SSH verify, so a run that stops before the probes never leaves outgoing
    // denied and unproven. A second run changes nothing.
    let dir = TempDir::new().unwrap();
    let (_listener, port) = listening();
    let (r, state) = run(dir.path(), enforce(port, None), None, None, Opts::default());
    assert!(r.ok, "{}", r.all());
    let lines = log(dir.path(), "target");
    let at = |line: &str| {
        lines
            .iter()
            .position(|l| l == line)
            .unwrap_or_else(|| panic!("{line} not in {lines:?}"))
    };
    let enable = at(r#"["ufw", "--force", "enable"]"#);
    let deny = at(r#"["ufw", "default", "deny", "outgoing"]"#);
    assert!(enable < deny, "{lines:?}");
    assert!(
        !lines
            .iter()
            .any(|l| l == r#"["ufw", "default", "allow", "outgoing"]"#),
        "{lines:?}"
    );
    assert_eq!(state["outgoing"], json!("deny"));
    let (r2, state2) = run(dir.path(), enforce(port, None), None, None, Opts::default());
    assert!(r2.ok, "{}", r2.all());
    assert_eq!(changed(&r2), 0, "{}", r2.stdout);
    assert_eq!(state2["outgoing"], json!("deny"));
}

#[test]
fn enforce_puts_outgoing_back_to_allow_when_a_probe_cannot_be_reached() {
    let dir = TempDir::new().unwrap();
    let (r, state) = run(
        dir.path(),
        enforce(refused_port(), Some(2)),
        None,
        None,
        Opts::default(),
    );
    assert!(!r.ok, "{}", r.all());
    assert!(
        r.stdout
            .contains("a declared probe could not be reached (127.0.0.1)"),
        "{}",
        r.stdout
    );
    assert_eq!(state["outgoing"], json!("allow"));
    let held = rules(&state);
    assert!(
        held.contains_key("allow out 443/tcp") && held.contains_key("allow out 53/udp"),
        "{held:?}"
    ); // the allows are kept
}

#[test]
fn a_narrowed_allowlist_on_an_enforcing_host_is_proven_again_or_outgoing_is_allowed_again() {
    // The stale allow is pruned while outgoing stays denied, so this run never applies the
    // deny; the probes run all the same, and prove the narrowed list or restore default-allow.
    let dir = TempDir::new().unwrap();
    let ssh = ssh_rule();
    let mut held = vec![json!([ssh.0, ssh.1])];
    for port in ["443/tcp", "53/udp", "80/tcp"] {
        held.push(json!([
            format!("allow out {port}"),
            tag("out-allow", port, "any")
        ]));
    }
    let enforcing = json!({"active": true, "outgoing": "deny", "rules": held});
    let (_listener, port) = listening();
    let (r, states) = run_many(
        dir.path(),
        &[
            (
                "proven",
                Host {
                    vars: Some(enforce(port, None)),
                    state: Some(enforcing.clone()),
                    ..Host::default()
                },
            ),
            (
                "unproven",
                Host {
                    vars: Some(enforce(refused_port(), Some(1))),
                    state: Some(enforcing),
                    ..Host::default()
                },
            ),
        ],
        Opts::default(),
    );
    assert!(
        text::any_line(&r.stdout, r"\bproven\s*:.*failed=0"),
        "{}",
        r.stdout
    );
    assert!(
        text::any_line(&r.stdout, r"\bunproven\s*:.*failed=1"),
        "{}",
        r.stdout
    );
    for name in ["proven", "unproven"] {
        assert!(
            !rules(states[name].as_ref().unwrap()).contains_key("allow out 80/tcp"),
            "{name}"
        ); // pruned while outgoing is denied
        assert!(
            !log(dir.path(), name)
                .iter()
                .any(|l| l == r#"["ufw", "default", "deny", "outgoing"]"#),
            "{name}"
        );
    }
    assert_eq!(
        states["proven"].as_ref().unwrap()["outgoing"],
        json!("deny")
    );
    assert_eq!(
        states["unproven"].as_ref().unwrap()["outgoing"],
        json!("allow")
    );
}

#[test]
fn an_enforced_allowlist_without_dns_or_a_probe_is_refused_before_any_rule_is_added() {
    let dir = TempDir::new().unwrap();
    let host = |vars: Value| Host {
        vars: Some(vars),
        ..Host::default()
    };
    let hosts = [
        (
            "no_dns",
            host(
                json!({"firewall_allow_egress": [{"port": 443}], "firewall_egress_mode": "enforce",
                               "firewall_egress_probes": [{"host": "127.0.0.1", "port": 9}]}),
            ),
        ),
        (
            "no_probe",
            host(json!({"firewall_allow_egress": allow_out(), "firewall_egress_mode": "enforce"})),
        ),
        (
            "bad_mode",
            host(json!({"firewall_allow_egress": allow_out(), "firewall_egress_mode": "block"})),
        ),
        (
            "empty_enforce",
            host(json!({"firewall_allow_egress": [], "firewall_egress_mode": "enforce"})),
        ),
    ];
    let (r, states) = run_many(dir.path(), &hosts, Opts::default());
    assert!(!r.ok, "{}", r.all());
    for (name, _) in &hosts {
        assert!(
            text::any_line(
                &r.stdout,
                &format!(r"\b{}\s*:.*failed=1", regex::escape(name))
            ),
            "{name}"
        );
        assert!(mutations(&log(dir.path(), name)).is_empty(), "{name}");
        assert!(states[*name].is_none(), "{name}");
    }
    assert!(
        r.stdout.contains("must allow 53/udp out")
            && r.stdout.contains("needs firewall_egress_probes"),
        "{}",
        r.stdout
    );
    assert!(
        r.stdout.contains("'block' is not audit or enforce"),
        "{}",
        r.stdout
    );
    assert!(
        r.stdout
            .contains("enforce needs a non-empty firewall_allow_egress"),
        "{}",
        r.stdout
    );
}

#[test]
fn a_dry_run_of_an_enforced_allowlist_changes_nothing_and_says_it_would() {
    let dir = TempDir::new().unwrap();
    let ssh = ssh_rule();
    let (r, state) = run(
        dir.path(),
        enforce(9, None),
        Some(json!({"active": true, "outgoing": "allow", "rules": [[ssh.0, ssh.1]]})),
        None,
        Opts::check(),
    );
    assert!(r.ok, "{}", r.all());
    assert_eq!(state["outgoing"], json!("allow"));
    let lines = log(dir.path(), "target");
    assert!(mutations(&lines).is_empty(), "{lines:?}");
    assert!(
        r.stdout.contains(r#""would_set_defaults_or_enable": true"#),
        "{}",
        r.stdout
    );
    let v = verdict(&r);
    assert!(
        v["would_change"].as_array().unwrap().iter().any(|w| w
            .as_str()
            .unwrap()
            .contains("default outgoing policy is not deny")),
        "{v}"
    );
}

#[test]
fn a_dry_run_reports_outgoing_denied_on_a_host_that_declares_no_allowlist() {
    // A real run would put default-allow back (an enforced allowlist was removed, say), so the
    // dry run must count it: with it left out, the step would record pass instead of skip.
    let dir = TempDir::new().unwrap();
    let ssh = ssh_rule();
    let (r, state) = run(
        dir.path(),
        json!({}),
        Some(json!({"active": true, "outgoing": "deny", "rules": [[ssh.0, ssh.1]]})),
        None,
        Opts::check(),
    );
    assert!(r.ok, "{}", r.all());
    assert_eq!(state["outgoing"], json!("deny"));
    assert_eq!(
        verdict(&r)["would_change"],
        json!(["default outgoing policy is not allow"])
    );
}

#[test]
fn a_host_without_an_allowlist_keeps_outgoing_allowed_and_no_outbound_allow() {
    let dir = TempDir::new().unwrap();
    let (r, state) = run(dir.path(), json!({}), None, None, Opts::default());
    assert!(r.ok, "{}", r.all());
    assert_eq!(state["outgoing"], json!("allow"));
    assert!(!rules(&state).keys().any(|s| s.starts_with("allow out")));
}

#[test]
fn a_new_denial_is_refused_behind_held_outbound_allows_and_lands_first_beside_new_ones() {
    // ufw appends and the first match wins, so a denial added after held allows (or after the
    // audit rule, which allows everything) would never match what they admit: refused, nothing
    // changed. Declared together with a new allowlist, the denial is added first.
    // Held is read from the host, whatever the tag: an untagged allow that this run would
    // adopt keeps its place (ufw updates the comment, it does not move the rule), and one no
    // declaration names is kept, so both stay ahead of a denial added now.
    let dir = TempDir::new().unwrap();
    let deny = json!([{"to": "198.51.100.1"}]);
    let denied = "deny out to 198.51.100.1";
    let both = json!({"firewall_allow_egress": allow_out(), "firewall_deny_egress": deny});
    let held = vec![
        json!(["allow out 443/tcp", tag("out-allow", "443/tcp", "any")]),
        json!(["allow out 53/udp", tag("out-allow", "53/udp", "any")]),
        json!([AUDIT, tag("out-audit", "any", "any")]),
    ];
    let with =
        |rules: Vec<Value>| Some(json!({"active": true, "outgoing": "allow", "rules": rules}));
    let mut behind = held.clone();
    behind.push(json!([denied, ""]));
    let refused = [
        (
            "held",
            Host {
                vars: Some(both.clone()),
                state: with(held.clone()),
                ..Host::default()
            },
        ),
        (
            "adopted",
            Host {
                vars: Some(both.clone()),
                state: with(vec![json!(["allow out 443/tcp", ""])]),
                ..Host::default()
            },
        ),
        (
            "by_hand",
            Host {
                vars: Some(json!({"firewall_deny_egress": deny})),
                state: with(vec![json!(["allow out 8443/tcp", ""])]),
                ..Host::default()
            },
        ),
        (
            "limited",
            Host {
                vars: Some(json!({"firewall_deny_egress": deny})),
                state: with(vec![json!(["limit out 25/tcp", ""])]),
                ..Host::default()
            },
        ),
        (
            "behind",
            Host {
                vars: Some(both.clone()),
                state: with(behind),
                ..Host::default()
            },
        ),
    ];
    // A declared denial the host already stores ahead of every held allow is adopted where it is.
    let mut first = vec![json!([denied, ""])];
    first.extend(held);
    let mut hosts: Vec<(&str, Host)> = refused.to_vec();
    hosts.push((
        "fresh",
        Host {
            vars: Some(both.clone()),
            ..Host::default()
        },
    ));
    hosts.push((
        "ahead",
        Host {
            vars: Some(both),
            state: with(first),
            ..Host::default()
        },
    ));
    let (r, states) = run_many(dir.path(), &hosts, Opts::default());
    for (name, spec) in &refused {
        assert!(
            text::any_line(&r.stdout, &format!(r"\b{name}\s*:.*failed=1")),
            "{name}"
        );
        let lines = log(dir.path(), name);
        assert!(mutations(&lines).is_empty(), "{lines:?}");
        assert_eq!(states[*name], spec.state, "{name}");
    }
    assert!(
        r.stdout
            .contains("adds deny out to 198.51.100.1, but the host already holds outbound allows"),
        "{}",
        r.stdout
    );
    assert!(
        r.stdout.contains("(allow out 8443/tcp)") && r.stdout.contains("(limit out 25/tcp)"),
        "{}",
        r.stdout
    );
    assert!(
        text::any_line(&r.stdout, r"\bfresh\s*:.*failed=0"),
        "{}",
        r.stdout
    );
    let fresh = specs(states["fresh"].as_ref().unwrap());
    let pos = |list: &[String], s: &str| {
        list.iter()
            .position(|x| x == s)
            .unwrap_or_else(|| panic!("{s} not in {list:?}"))
    };
    assert!(
        pos(&fresh, denied) < pos(&fresh, "allow out 443/tcp")
            && pos(&fresh, "allow out 443/tcp") < pos(&fresh, AUDIT),
        "{fresh:?}"
    );
    assert!(
        text::any_line(&r.stdout, r"\bahead\s*:.*failed=0"),
        "{}",
        r.stdout
    );
    let ahead = states["ahead"].as_ref().unwrap();
    let kept = specs(ahead);
    assert!(
        pos(&kept, denied) < pos(&kept, "allow out 443/tcp")
            && pos(&kept, "allow out 443/tcp") < pos(&kept, AUDIT),
        "{kept:?}"
    );
    assert_eq!(rules(ahead)[denied], tag("out-deny", "any", "198.51.100.1"));
}

#[test]
fn controller_cidr_outside_the_ssh_cidrs_is_refused_before_any_rule_is_added() {
    // A firewall without the orchestrator's SSH source would orphan the host: refused before
    // any rule is added or the firewall is enabled.
    let dir = TempDir::new().unwrap();
    let (r, states) = run_many(
        dir.path(),
        &[(
            "target",
            Host {
                vars: Some(json!({"firewall_controller_cidr": "198.51.100.0/24"})),
                ..Host::default()
            },
        )],
        Opts::default(),
    );
    assert!(!r.ok, "{}", r.all());
    assert!(
        text::any_line(&r.stdout, r"\btarget\s*:.*failed=1"),
        "{}",
        r.stdout
    );
    assert!(
        r.stdout.contains("is not in firewall_ssh_cidrs"),
        "{}",
        r.stdout
    );
    let lines = log(dir.path(), "target");
    assert!(mutations(&lines).is_empty(), "{lines:?}");
    assert!(
        states["target"].is_none(),
        "the stub never ran a write: no state file"
    );
}
