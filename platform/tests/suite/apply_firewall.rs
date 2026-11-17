//! apply-firewall.yml, the UFW lockdown playbook, keeps its anti-lockout order and its one
//! rule definition (static tier).
//!
//! Published ports on rootful podman and Docker are DNAT'd into the container netns, so their
//! inbound traffic crosses UFW's `route` (FORWARD) chain, which `default deny (routed)` drops.
//! The playbook must therefore feed route rules into the declared set and query the engine as
//! root so detection sees the containers. These tests also pin the anti-lockout invariant (SSH
//! and every other add before enable) and the egress containment wiring that docs/MISTAKES.md
//! 2.5 is about. The exact ufw command forms are built by filter_plugins/ufw_rules.py and
//! asserted in test_ufw_rules_filter.py; runs against a stub ufw live in
//! apply_firewall_convergence_playbook.rs.
//!
//! Ports the static tests of the deleted platform/tests/test_apply_firewall.bats (plan 24). The checks read
//! the raw file text, as the BATS helpers did. BATS test 5 carried `! grep -qE ...` mid-body,
//! which `set -e` never fails on; here that absence is a real assertion.
//!
//! ## Port map (BATS @test -> Rust fn)
//!
//! | BATS @test | Rust fn |
//! |---|---|
//! | firewall: rootful flag defaults to Docker, overridable via firewall_rootful | `rootful_defaults_to_docker_and_is_overridable` |
//! | firewall: firewall_route_rules is wired into the declared rule set | `route_rules_feed_the_declared_rule_set` |
//! | firewall: no ufw rule command or tag is hand-built in the playbook (one definition) | `no_ufw_rule_command_or_tag_is_hand_built` |
//! | firewall: DETECTED ports get a route-allow mirror, gated on _rootful | `detected_ports_reach_the_declared_set_with_rootful` |
//! | firewall: detection runs as root on rootful/Docker (become follows _rootful) | `detection_become_follows_rootful` |
//! | firewall: anti-lockout intact — SSH allows first, every add before enable, reset_connection present | `ssh_allows_come_first_and_every_add_precedes_enable` |
//! | firewall: rootful podman gets a DNS-scoped bridge INPUT allow, podman+rootful-gated, before enable | `bridge_dns_detection_is_gated_on_rootful_podman` |
//! | firewall: firewall_deny_egress is wired to _deny_egress, default EMPTY | `deny_egress_defaults_to_empty` |
//! | firewall: egress denials feed the declared rule set | `egress_denials_feed_the_declared_rule_set` |
//! | firewall: every egress denial is judged by ufw_egress_problems against the SSH CIDRs | `every_egress_denial_is_judged_by_ufw_egress_problems` |
//! | firewall: egress denials are applied BEFORE enable (no uncontained window) | `egress_denials_are_added_before_enable` |
//! | firewall: the default outbound policy is allow unless an enforced allowlist proves itself | `outbound_stays_allowed_unless_an_enforced_allowlist_proves_itself` |
//! | firewall: the package index is refreshed only when ufw is missing | `package_index_is_refreshed_only_when_ufw_is_missing` |
//! | apply-firewall: the upstream source may be a LIST, and a bare string still works | `upstream_source_may_be_a_list_or_a_string` |
//! | apply-firewall: blank upstream sources stop before either detected-port rule | `apply_firewall_playbook::blank_upstream_sources_stop_before_any_detected_port_rule` |
//! | firewall: the orchestrator's SSH source must be one of the SSH CIDRs, checked BEFORE enable | `controller_cidr_is_checked_before_enable_and_the_ssh_allows` |

use agent_cloud_tests::text;
use regex::Regex;

const PLAYBOOK: &str = "platform/playbooks/apply-firewall.yml";
const COMPUTE: &str = "Compute the declared rules";
const SSH_ALLOW: &str = "Allow SSH (22/tcp) from each admin CIDR";
const OTHER_ADD: &str = "Add each other declared rule";
const CONTROLLER_REFUSAL: &str =
    "Refuse a firewall that would drop the orchestrator's SSH source (anti-orphan)";

/// 1-based position of the first `-` in a line, 0 when none (awk `index($0, "-")`).
fn dash_at(line: &str) -> usize {
    line.find('-').map_or(0, |i| i + 1)
}

/// One task's body lines: everything after the first line containing `- name: "<name>`
/// (a prefix match), until the next `- name:` at the same or shallower indent. The text
/// semantics of assert_helpers.bash `task_block`. Panics when the task is missing.
fn task_block(text: &str, name: &str) -> String {
    let start = format!("- name: \"{name}");
    let next = Regex::new(r"^\s*- name:").unwrap();
    let mut lines = text.lines();
    let indent = lines
        .by_ref()
        .find(|l| l.contains(&start))
        .map(dash_at)
        .unwrap_or_else(|| panic!("{PLAYBOOK}: no task named {name:?}"));
    lines
        .take_while(|l| !(next.is_match(l) && dash_at(l) <= indent))
        .collect::<Vec<_>>()
        .join("\n")
}

/// 1-based line number of the first line matching `pattern` (ERE-style regex).
fn first_line(text: &str, pattern: &str) -> Option<usize> {
    let re = Regex::new(pattern).unwrap_or_else(|e| panic!("bad pattern {pattern:?}: {e}"));
    text.lines().position(|l| re.is_match(l)).map(|i| i + 1)
}

/// The first line matching `a` comes before the first line matching `b`
/// (assert_helpers.bash `assert_precedes`).
fn assert_precedes(text: &str, a: &str, b: &str) {
    let la = first_line(text, a).unwrap_or_else(|| panic!("no match for {a:?} in {PLAYBOOK}"));
    let lb = first_line(text, b).unwrap_or_else(|| panic!("no match for {b:?} in {PLAYBOOK}"));
    assert!(
        la < lb,
        "{a:?} (line {la}) after {b:?} (line {lb}) in {PLAYBOOK}"
    );
}

fn assert_contains(haystack: &str, needle: &str, what: &str) {
    assert!(haystack.contains(needle), "{what}: missing {needle:?}");
}

#[test]
fn rootful_defaults_to_docker_and_is_overridable() {
    // Docker is always rootful; rootful podman hosts can't be told apart by engine name, so
    // they opt in with firewall_rootful: true.
    text::assert_text(
        PLAYBOOK,
        "_rootful: \"{{ firewall_rootful | default(_engine == 'docker') | bool }}\"",
    );
    text::assert_text(
        PLAYBOOK,
        "_engine: \"{{ container_engine | default('podman') }}\"",
    );
}

#[test]
fn route_rules_feed_the_declared_rule_set() {
    let pb = text::read(PLAYBOOK);
    text::assert_text(
        PLAYBOOK,
        "_route_rules: \"{{ firewall_route_rules | default([]) }}\"",
    );
    assert_contains(
        &task_block(&pb, COMPUTE),
        "route_rules=_route_rules",
        COMPUTE,
    );
}

#[test]
fn no_ufw_rule_command_or_tag_is_hand_built() {
    // The command forms and the `agent-cloud:` tag are built in ONE place, ufw_rules.py
    // ufw_desired_rules (exact text: test_ufw_rules_filter.py). A copy here could disagree
    // with the declared set the prune step compares against.
    let pb = text::read(PLAYBOOK);
    let comment = Regex::new(r"^\s*#").unwrap();
    let code: Vec<&str> = pb.lines().filter(|l| !comment.is_match(l)).collect();
    let rule = Regex::new(r"ufw (route )?(allow|deny|limit) ").unwrap();
    for line in &code {
        assert!(!line.contains("agent-cloud:"), "hand-built tag: {line}");
        assert!(!rule.is_match(line), "hand-built ufw rule command: {line}");
    }
    // Both add tasks run the prepared command with its tag, and nothing else.
    for task in [SSH_ALLOW, OTHER_ADD] {
        assert_contains(
            &task_block(&pb, task),
            "ansible.builtin.command: \"ufw {{ item.cmd }} comment '{{ item.tag }}'\"",
            task,
        );
    }
}

#[test]
fn detected_ports_reach_the_declared_set_with_rootful() {
    // Each detected published port gets a FORWARD mirror on rootful hosts only; the mirror is
    // ufw_desired_rules'. Here: the declared set is fed _rootful and the detected ports.
    let blk = task_block(&text::read(PLAYBOOK), COMPUTE);
    assert_contains(&blk, "rootful=_rootful | bool", COMPUTE);
    assert_contains(
        &blk,
        "detected=_detected.stdout_lines | default([])",
        COMPUTE,
    );
}

#[test]
fn detection_become_follows_rootful() {
    // A hardcoded become: false silently found NO containers on rootful/Docker hosts (their
    // containers belong to root). It must track _rootful.
    text::assert_text(PLAYBOOK, "become: \"{{ _rootful }}\"");
    text::refute_line(PLAYBOOK, r"^\s*become:\s*false\s*$");
}

#[test]
fn ssh_allows_come_first_and_every_add_precedes_enable() {
    // The SSH allows are their own task, added before any other rule; every other add precedes
    // enabling UFW.
    let pb = text::read(PLAYBOOK);
    assert_precedes(
        &pb,
        r#"name: "Allow SSH \(22/tcp\) from each admin CIDR"#,
        r#"name: "Add each other declared rule"#,
    );
    assert_precedes(
        &pb,
        r#"name: "Add each other declared rule"#,
        "ufw --force enable",
    );
    assert_contains(
        &task_block(&pb, SSH_ALLOW),
        "loop: \"{{ _fw_to_add | selectattr('is_ssh') | list }}\"",
        SSH_ALLOW,
    );
    assert_contains(
        &task_block(&pb, OTHER_ADD),
        "loop: \"{{ _fw_to_add | rejectattr('is_ssh') | list }}\"",
        OTHER_ADD,
    );
    // The fresh-handshake check that proves SSH survives the firewall must remain.
    text::assert_text(PLAYBOOK, "ansible.builtin.meta: reset_connection");
}

#[test]
fn bridge_dns_detection_is_gated_on_rootful_podman() {
    // Podman's aardvark-dns listens on the bridge gateway (a host IP), so a rootful podman
    // container's DNS query is INPUT to the host and default-deny drops it. Docker and rootless
    // podman never cross host UFW, so detection is gated on _rootful AND podman.
    let pb = text::read(PLAYBOOK);
    text::assert_text(
        PLAYBOOK,
        "_allow_bridge_dns: \"{{ firewall_allow_bridge_dns | default(true) | bool }}\"",
    );
    let detect = "Detect podman bridge interfaces";
    let blk = task_block(&pb, detect);
    assert_contains(&blk, "podman network inspect", detect);
    assert!(
        text::any_line(&blk, r"^\s*- _allow_bridge_dns\s*$"),
        "{detect}: no _allow_bridge_dns gate"
    );
    // Removing _rootful must fail this test: the gate must also exclude rootless podman.
    assert!(
        text::any_line(&blk, r"^\s*- _rootful\s*$"),
        "{detect}: no _rootful gate"
    );
    assert!(
        text::any_line(&blk, r"^\s*- _engine == 'podman'\s*$"),
        "{detect}: no podman gate"
    );
    // The bridges found feed the declared set, which is added before enable.
    assert_contains(
        &task_block(&pb, COMPUTE),
        "bridges=_bridges.stdout_lines | default([])",
        COMPUTE,
    );
    assert_precedes(
        &pb,
        r#"name: "Detect podman bridge interfaces"#,
        r#"name: "Compute the declared rules"#,
    );
}

// Egress containment (firewall_deny_egress): the self-hosted CI runner hosts run
// repository-authored code and are denied the platform's interior at the network boundary.
// docs/MISTAKES.md 2.5 records a guard on this feature that was never evaluated against the
// declarations it judges; the predicate is a filter executed in test_ufw_rules_filter.py.

#[test]
fn deny_egress_defaults_to_empty() {
    // Default-empty keeps every existing host's rule set byte-identical.
    text::assert_text(
        PLAYBOOK,
        "_deny_egress: \"{{ firewall_deny_egress | default([]) }}\"",
    );
}

#[test]
fn egress_denials_feed_the_declared_rule_set() {
    assert_contains(
        &task_block(&text::read(PLAYBOOK), COMPUTE),
        "deny_egress=_deny_egress",
        COMPUTE,
    );
}

#[test]
fn every_egress_denial_is_judged_by_ufw_egress_problems() {
    // The guard is ufw_egress_problems, run against real declaration shapes in
    // test_ufw_rules_filter.py (docs/MISTAKES.md 2.5). Here: the playbook asserts on it, fed
    // the declared denials and SSH CIDRs, and the refusal names the recorded why.
    let task = "Validate each egress denial";
    let blk = task_block(&text::read(PLAYBOOK), task);
    assert_contains(
        &blk,
        "_fw_egress_problems: \"{{ _deny_egress | ufw_egress_problems(_ssh_cidrs) }}\"",
        task,
    );
    assert_contains(&blk, "_fw_egress_problems | length == 0", task);
    assert_contains(&blk, "§2.5", task);
}

#[test]
fn egress_denials_are_added_before_enable() {
    // Validation precedes building the rules, and the rules precede enabling.
    let pb = text::read(PLAYBOOK);
    assert_precedes(
        &pb,
        r#"name: "Validate each egress denial"#,
        r#"name: "Compute the declared rules"#,
    );
    assert_precedes(
        &pb,
        r#"name: "Compute the declared rules"#,
        r#"name: "Add each other declared rule"#,
    );
    assert_precedes(
        &pb,
        r#"name: "Add each other declared rule"#,
        "ufw --force enable",
    );
}

#[test]
fn outbound_stays_allowed_unless_an_enforced_allowlist_proves_itself() {
    // Egress containment is specific denials, not default-deny outbound, which would break
    // package fetches, DNS and NTP. The one exception, an enforced firewall_allow_egress, is
    // applied only inside the probe block, after enable and the SSH verify.
    let pb = text::read(PLAYBOOK);
    text::assert_text(PLAYBOOK, "ufw default allow outgoing");
    let deny_lines = pb
        .lines()
        .filter(|l| l.contains("ufw default deny outgoing"))
        .count();
    assert_eq!(
        deny_lines, 1,
        "`ufw default deny outgoing` must occur on exactly one line"
    );
    let task = "Prove the enforced outbound allowlist";
    assert_contains(&task_block(&pb, task), "ufw default deny outgoing", task);
    assert_precedes(
        &pb,
        r#"name: "Verify SSH still works after enabling"#,
        "ufw default deny outgoing",
    );
}

#[test]
fn package_index_is_refreshed_only_when_ufw_is_missing() {
    // Under an enforced outbound allowlist a refresh on every run would stop each later run at
    // the install, before any rule or mode change.
    let pb = text::read(PLAYBOOK);
    assert_contains(
        &task_block(&pb, "Install ufw"),
        "update_cache: \"{{ 'install ok installed' not in _ufw_installed.stdout }}\"",
        "Install ufw",
    );
    assert_precedes(
        &pb,
        r#"name: "Read whether ufw is installed""#,
        r#"name: "Install ufw""#,
    );
}

#[test]
fn upstream_source_may_be_a_list_or_a_string() {
    // A service can have more than one upstream (a reverse proxy AND its automation host);
    // widening one value to a CIDR would grant the whole subnet, so it takes a list.
    let pb = text::read(PLAYBOOK);
    text::assert_text(PLAYBOOK, "_upstream_sources");
    // A string is normalised to a one-element list, so single-address hosts are untouched.
    let norm = "Normalise the upstream source(s) to a list";
    let blk = task_block(&pb, norm);
    assert!(!blk.is_empty(), "{norm}: empty task block");
    assert_contains(&blk, "is not string", norm);
    // The declared set fans out over the normalised list, never the raw variable, which
    // would silently apply only the first source.
    let blk = task_block(&pb, COMPUTE);
    assert_contains(&blk, "upstreams=_upstream_sources", COMPUTE);
    assert!(
        !blk.contains("firewall_upstream_source"),
        "{COMPUTE}: reads the raw firewall_upstream_source"
    );
    // The guard still refuses an empty declaration, on the normalised list.
    let guard = "Require an upstream source when detected ports exist";
    assert_contains(
        &task_block(&pb, guard),
        "_upstream_sources | length > 0",
        guard,
    );
}

#[test]
fn controller_cidr_is_checked_before_enable_and_the_ssh_allows() {
    // Semaphore reaches every host over SSH; a firewall without its source orphans the host.
    // Compared in the spelling ufw stores (behaviour: apply_firewall_convergence_playbook.rs).
    let pb = text::read(PLAYBOOK);
    assert_contains(
        &task_block(&pb, CONTROLLER_REFUSAL),
        "(firewall_controller_cidr | ufw_address) in _fw_ssh_cidrs_stored",
        "the refusal task",
    );
    let name = regex::escape(CONTROLLER_REFUSAL);
    // The enable task, not the first comment that names the command.
    assert_precedes(
        &pb,
        &name,
        r#"ansible\.builtin\.command: "ufw --force enable""#,
    );
    assert_precedes(
        &pb,
        &name,
        r#"name: "Allow SSH \(22/tcp\) from each admin CIDR"#,
    );
}
