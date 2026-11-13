//! `configure-host-network.yml` converges a host's IPv4 configuration to the inventory (static tier).
//!
//! Two properties matter most, and both exist because of a real incident: a host was found
//! holding another host's declared address, so two machines answered one IP and an orchestrated
//! deploy authenticated against the wrong one for hours. The identity guard runs before anything
//! changes, the revert is armed before the apply and disarmed only after the intended host is
//! proven, a partial declaration is refused, and a dry run leaves nothing behind on the host.
//!
//! Replaces `platform/tests/test_configure_host_network.bats` (plan 24, Phase 4).
//!
//! Port map:
//!
//! | BATS @test | Rust fn |
//! |---|---|
//! | network config: playbook exists and lints as a playbook | playbook_exists_and_lints_as_a_playbook |
//! | network config: refuses to reconfigure a host that is not the intended one | refuses_to_reconfigure_a_host_that_is_not_the_intended_one (candidate: pins text) |
//! | network config: the identity guard runs BEFORE anything is modified | the_identity_guard_runs_before_anything_is_modified |
//! | network config: the revert is armed before the apply, disarmed only after proof | the_revert_is_armed_before_the_apply_and_disarmed_only_after_proof |
//! | network config: arming the revert is re-runnable after a previous arm | arming_the_revert_is_rerunnable_after_a_previous_arm |
//! | network config: the revert window is long enough to confirm within | the_revert_window_is_long_enough_to_confirm_within (candidate: pins text) |
//! | network config: a stale controller host key cannot fail the confirmation | a_stale_controller_host_key_cannot_fail_the_confirmation |
//! | network config: the confirmation host inherits Phase 1's transport | the_confirmation_host_inherits_phase_1s_transport |
//! | network config: the confirmation probe honours a non-default SSH port | the_confirmation_probe_honours_a_non_default_ssh_port (candidate: pins text) |
//! | network config: the apply is fire-and-forget, because it severs its own connection | the_apply_is_fire_and_forget_because_it_severs_its_own_connection |
//! | network config: a dry run leaves nothing behind on the host | a_dry_run_leaves_nothing_behind_on_the_host |
//! | network config: validation never touches the live /etc/netplan | validation_never_touches_the_live_etc_netplan |
//! | network config: the rendered config is validated before it can be applied | the_rendered_config_is_validated_before_it_can_be_applied |
//! | network config: a partial declaration is refused | a_partial_declaration_is_refused (candidate: pins text) |

use agent_cloud_tests::repo;
use agent_cloud_tests::yaml::{self, Node};
use regex::Regex;

const PB: &str = "platform/playbooks/configure-host-network.yml";

/// The `name:` of a play or task, or "" for an item without one.
fn name_of(node: &Node) -> &str {
    node.get("name").and_then(Node::as_str).unwrap_or("")
}

/// Every string in `node`, keys and values, depth first.
fn strings<'a>(node: &'a Node, out: &mut Vec<&'a str>) {
    match node {
        Node::Str(s) => out.push(s),
        Node::Seq(items) => items.iter().for_each(|item| strings(item, out)),
        Node::Map(entries) => {
            for (key, value) in entries {
                strings(key, out);
                strings(value, out);
            }
        }
        Node::Null | Node::Bool(_) => {}
    }
}

/// The text of a node: its strings, one per line. Scoped by the caller to one task.
fn text(node: &Node) -> String {
    let mut out = vec![];
    strings(node, &mut out);
    out.join("\n")
}

/// A play or import, or a block: a node that holds other tasks. Its own text is not one task's.
fn is_task(node: &Node) -> bool {
    ["hosts", "block", "tasks"]
        .iter()
        .all(|key| node.get(key).is_none())
}

/// The first task whose name contains `needle`. The name is the selector: a task is named.
fn task<'a>(tasks: &'a [Node], needle: &str) -> &'a Node {
    tasks
        .iter()
        .find(|t| name_of(t).contains(needle))
        .unwrap_or_else(|| panic!("no task named like {needle:?}"))
}

fn has_task(tasks: &[Node], needle: &str) -> bool {
    tasks.iter().any(|t| name_of(t).contains(needle))
}

/// Index of the first item whose name contains `needle`; the order of the file is the order.
fn position(tasks: &[Node], needle: &str) -> usize {
    tasks
        .iter()
        .position(|t| name_of(t).contains(needle))
        .unwrap_or_else(|| panic!("no task named like {needle:?}"))
}

/// The module arguments of a task, under the module's fully qualified name.
fn module<'a>(task: &'a Node, fq: &str) -> &'a Node {
    task.get(fq)
        .unwrap_or_else(|| panic!("task {:?} has no {fq}", name_of(task)))
}

/// The `that:` conditions of an assert task.
fn that_list(task: &Node) -> Vec<&str> {
    match module(task, "ansible.builtin.assert").get("that") {
        Some(Node::Str(s)) => vec![s],
        Some(Node::Seq(items)) => items.iter().filter_map(Node::as_str).collect(),
        _ => vec![],
    }
}

/// The play whose name starts with `prefix`, as parsed (its `vars:` and its `tasks:`).
fn play(prefix: &str) -> Node {
    repo::tasks(PB)
        .into_iter()
        .find(|n| n.get("hosts").is_some() && name_of(n).starts_with(prefix))
        .unwrap_or_else(|| panic!("no play named {prefix:?}"))
}

/// The direct tasks of the play whose name starts with `prefix`. Phase 2 has no blocks.
fn play_tasks(prefix: &str) -> Vec<Node> {
    play(prefix)
        .get("tasks")
        .and_then(Node::as_seq)
        .unwrap_or_else(|| panic!("play {prefix:?} has no tasks"))
        .to_vec()
}

/// The `dest:` of every module argument map of one task.
fn dests(task: &Node) -> Vec<&str> {
    let Some(entries) = task.as_map() else {
        return vec![];
    };
    entries
        .iter()
        .filter_map(|(_, value)| value.get("dest").and_then(Node::as_str))
        .collect()
}

#[test]
fn playbook_exists_and_lints_as_a_playbook() {
    // Arrange
    let path = repo::path(PB);

    // Act
    let doc = yaml::load_one(&path);

    // Assert
    let plays = doc
        .as_ref()
        .and_then(Node::as_seq)
        .expect("the playbook is a sequence of plays");
    assert!(
        plays.iter().any(|p| p.get("name").is_some()),
        "no play in {PB} has a name"
    );
}

#[test]
fn refuses_to_reconfigure_a_host_that_is_not_the_intended_one() {
    // Arrange: the identity guard must compare what the host calls itself with the inventory.
    let tasks = repo::tasks(PB);

    // Act
    let asks = has_task(&tasks, "Ask the host what it calls itself");
    let guard = task(
        &tasks,
        "Refuse to continue if this is not the intended host",
    );

    // Assert
    assert!(asks, "the host is never asked what it calls itself");
    assert!(
        that_list(guard).iter().any(|c| c.starts_with(
            "(_remote_hostname.stdout | trim) == (net_expect_hostname | default(inventory_hostname"
        )),
        "the identity guard does not compare the host's own name with the inventory: {:?}",
        that_list(guard)
    );
}

#[test]
fn the_identity_guard_runs_before_anything_is_modified() {
    // Arrange
    let tasks = repo::tasks(PB);

    // Act
    let guard = position(
        &tasks,
        "Refuse to continue if this is not the intended host",
    );
    let backup = position(&tasks, "Back up the configuration the revert will restore");
    let apply = position(&tasks, "Apply it (fire-and-forget");

    // Assert
    assert!(
        guard < backup,
        "the guard (task {guard}) is after the backup (task {backup})"
    );
    assert!(
        guard < apply,
        "the guard (task {guard}) is after the apply (task {apply})"
    );
}

#[test]
fn the_revert_is_armed_before_the_apply_and_disarmed_only_after_proof() {
    // Arrange
    let tasks = repo::tasks(PB);

    // Act
    let arm = position(&tasks, "ARM THE REVERT");
    let apply = position(&tasks, "Apply it (fire-and-forget");
    let disarm = position(&tasks, "DISARM the revert");
    let ident = position(&tasks, "Refuse to disarm unless it is the intended host");

    // Assert
    assert!(arm < apply, "the revert is armed after the apply");
    assert!(apply < disarm, "the revert is disarmed before the apply");
    assert!(
        has_task(&tasks, "Wait for SSH on the new address"),
        "no wait for the new address"
    );
    assert!(
        has_task(&tasks, "Fail the run when the new address never answered"),
        "a missing new address does not fail the run"
    );
    assert!(
        ident < disarm,
        "the disarm (task {disarm}) comes before the identity check (task {ident})"
    );
    // Inside the identity task's OWN body: a match elsewhere cannot stand in for it.
    assert!(
        that_list(&tasks[ident])
            .iter()
            .any(|c| c.contains("(_new_hostname.stdout | trim) == _expect_hostname")),
        "the disarm is not gated on the new host's identity: {:?}",
        that_list(&tasks[ident])
    );
}

#[test]
fn arming_the_revert_is_rerunnable_after_a_previous_arm() {
    // Arrange: systemd-run creates a timer AND a service, so both must be stopped and reset.
    let tasks = repo::tasks(PB);
    let arm = task(&tasks, "ARM THE REVERT before applying anything");
    let script = text(module(arm, "ansible.builtin.shell"));

    // Act
    let cleanup: Vec<&str> = script
        .lines()
        .map(str::trim)
        .filter(|l| l.starts_with("systemctl stop") || l.starts_with("systemctl reset-failed"))
        .collect();
    let stop = Regex::new(r"systemctl stop .*agent-cloud-netrevert\.service").unwrap();
    let reset = Regex::new(r"systemctl reset-failed .*agent-cloud-netrevert\.service").unwrap();

    // Assert
    assert!(
        cleanup.iter().any(|l| stop.is_match(l)),
        "the arm does not stop agent-cloud-netrevert.service: {cleanup:?}"
    );
    assert!(
        cleanup.iter().any(|l| reset.is_match(l)),
        "the arm does not reset-failed agent-cloud-netrevert.service: {cleanup:?}"
    );
}

#[test]
fn the_revert_window_is_long_enough_to_confirm_within() {
    // Arrange
    let tasks = repo::tasks(PB);

    // Act
    let window = task(
        &tasks,
        "Require a revert window long enough to confirm within",
    );

    // Assert
    assert!(
        that_list(window)
            .iter()
            .any(|c| c.contains("(_revert_seconds | int) >= 60")),
        "the window has no floor of 60 seconds: {:?}",
        that_list(window)
    );
}

#[test]
fn a_stale_controller_host_key_cannot_fail_the_confirmation() {
    // Arrange: the controller's key for the address is stale; the confirmation play drops it.
    let phase2 = play_tasks("Phase 2");
    let all = repo::tasks(PB);

    // Act
    let known_hosts = phase2
        .iter()
        .filter_map(|t| t.get("ansible.builtin.known_hosts"))
        .collect::<Vec<_>>();
    let stale = task(&phase2, "Drop the controller's stale host key");
    let loop_items = stale.get("loop").and_then(Node::as_str).unwrap_or_default();

    // Assert
    assert!(!known_hosts.is_empty(), "Phase 2 never touches known_hosts");
    assert!(
        known_hosts
            .iter()
            .any(|k| k.get("state").and_then(Node::as_str) == Some("absent")),
        "no known_hosts task removes the stale key (state: absent)"
    );
    // Delegated to the controller: the stale entry is the controller's, not the target's.
    assert_eq!(
        stale.get("delegate_to").and_then(Node::as_str),
        Some("localhost"),
        "the stale key is dropped on the target, not on the controller"
    );
    // A non-default port is a different known_hosts key: both forms are dropped.
    assert!(
        loop_items.contains("ansible_port"),
        "the drop ignores the SSH port: {loop_items:?}"
    );
    assert!(
        loop_items.contains("[' ~ ansible_host ~ ']:"),
        "the drop never names the [host]:port form: {loop_items:?}"
    );
    assert!(
        position(&all, "Drop the controller's stale host key")
            < position(&all, "Ask whoever answered what it calls itself"),
        "the stale key is dropped after the identity probe that needs it gone"
    );
}

#[test]
fn the_confirmation_host_inherits_phase_1s_transport() {
    // Arrange: add_host starts from nothing, so every transport setting must be named.
    let tasks = repo::tasks(PB);
    let add = module(
        task(&tasks, "Publish the new address for the confirmation play"),
        "ansible.builtin.add_host",
    );

    // Act / Assert
    for key in [
        "ansible_connection",
        "ansible_private_key_file",
        "ansible_ssh_private_key_file",
        "ansible_ssh_common_args",
        "ansible_ssh_extra_args",
        "ansible_python_interpreter",
    ] {
        assert!(
            add.get(key).is_some(),
            "add_host does not carry {key} from Phase 1"
        );
    }
    // Each defaults to omit: a literal empty string is not "unset" and would override a value.
    for key in ["ansible_private_key_file", "ansible_ssh_common_args"] {
        assert_ne!(
            add.get(key).and_then(Node::as_str),
            Some(""),
            "{key} is an empty string, not omit"
        );
    }
}

#[test]
fn the_confirmation_probe_honours_a_non_default_ssh_port() {
    // Arrange
    let tasks = repo::tasks(PB);
    let add = module(
        task(&tasks, "Publish the new address for the confirmation play"),
        "ansible.builtin.add_host",
    );
    let wait = module(
        task(&tasks, "Wait for SSH on the new address"),
        "ansible.builtin.wait_for",
    );

    // Act
    let added_port = add.get("ansible_port").and_then(Node::as_str);
    let probe_port = wait.get("port").and_then(Node::as_str);

    // Assert
    assert_eq!(
        added_port,
        Some("{{ ansible_port | default(22) }}"),
        "Phase 1 does not carry the SSH port forward"
    );
    assert_eq!(
        probe_port,
        Some("{{ ansible_port | default(22) }}"),
        "the confirmation probe does not use the carried SSH port"
    );
    assert_ne!(
        probe_port,
        Some("22"),
        "the confirmation probe is hard-wired to port 22"
    );
}

#[test]
fn the_apply_is_fire_and_forget_because_it_severs_its_own_connection() {
    // Arrange
    let tasks = repo::tasks(PB);
    let apply = task(&tasks, "Apply it (fire-and-forget");

    // Act / Assert: async with poll 0, so a dropped connection is not reported as a failure.
    assert_eq!(
        apply.get("poll").and_then(Node::as_str),
        Some("0"),
        "the apply waits for its own result, and the severed connection fails it"
    );
    assert!(apply.get("async").is_some(), "the apply is not async");
}

#[test]
fn a_dry_run_leaves_nothing_behind_on_the_host() {
    // Arrange: the dry run stops before any mutating task, and nothing under /etc/netplan is written
    // before that stop.
    let phase1 = play("Phase 1");
    let tasks = repo::tasks(PB);

    // Act
    let confirm = phase1
        .get("vars")
        .and_then(|v| v.get("_confirm"))
        .and_then(Node::as_str);
    let stop = position(&tasks, "Stop here on a dry run");
    let backup = position(&tasks, "Back up the configuration");
    let install = position(&tasks, "Install the validated configuration");

    // Assert
    assert_eq!(
        confirm,
        Some("{{ net_confirm | default(false) | bool }}"),
        "the dry-run switch is not net_confirm"
    );
    assert!(stop < backup, "the dry-run stop comes after the backup");
    assert!(stop < install, "the dry-run stop comes after the install");
    for before in tasks[..stop].iter().filter(|t| is_task(t)) {
        assert!(
            !dests(before).iter().any(|d| d.starts_with("/etc/netplan")),
            "{:?} writes under /etc/netplan before the dry-run stop",
            name_of(before)
        );
        assert!(
            !text(before).contains(".staged"),
            "{:?} stages a file before the dry-run stop",
            name_of(before)
        );
    }
}

#[test]
fn validation_never_touches_the_live_etc_netplan() {
    // Arrange: the validation runs in an isolated root, so the live directory is read at most once.
    let tasks = repo::tasks(PB);
    let validate = task(
        &tasks,
        "Validate the rendered configuration in an isolated root",
    );
    let script = text(module(validate, "ansible.builtin.shell"));

    // Act
    let bare: Vec<String> = script
        .replace("$root/etc/netplan", "SANDBOX")
        .lines()
        .filter(|l| l.contains("/etc/netplan"))
        .map(|l| l.trim().to_owned())
        .collect();
    let permitted_read =
        Regex::new(r#"^cp -a /etc/netplan/\. "SANDBOX/?"\s*(2>/dev/null)?\s*(\|\| true)?\s*$"#)
            .unwrap();
    let applying = Regex::new(r"netplan\s+(apply|try)").unwrap();

    // Assert
    assert!(
        script.contains("netplan generate --root-dir"),
        "validation does not run netplan generate"
    );
    assert!(
        !bare.is_empty(),
        "the permitted read of the live directory is missing from validation"
    );
    let offending: Vec<&String> = bare
        .iter()
        .filter(|l| !permitted_read.is_match(l))
        .collect();
    assert!(
        offending.is_empty(),
        "validation touches the live /etc/netplan beyond the permitted read: {offending:?}"
    );
    // A command with no path still acts on the live system: netplan's applying subcommands.
    assert!(
        !applying.is_match(&script),
        "validation applies the config before the revert is armed"
    );
}

#[test]
fn the_rendered_config_is_validated_before_it_can_be_applied() {
    // Arrange
    let tasks = repo::tasks(PB);

    // Act
    let validate = module(
        task(
            &tasks,
            "Validate the rendered configuration in an isolated root",
        ),
        "ansible.builtin.shell",
    );

    // Assert
    assert!(
        text(validate).contains("netplan generate"),
        "the rendered config is never validated with netplan generate"
    );
}

#[test]
fn a_partial_declaration_is_refused() {
    // Arrange
    let tasks = repo::tasks(PB);

    // Act
    let declaration = that_list(task(&tasks, "Require a complete network declaration"));

    // Assert: an address without a gateway is unreachable off its own subnet.
    assert!(
        declaration
            .iter()
            .any(|c| c.contains("net_gateway is match")),
        "a missing gateway is not refused: {declaration:?}"
    );
    assert!(
        declaration
            .iter()
            .any(|c| c.contains("net_address is match")),
        "a malformed address is not refused: {declaration:?}"
    );
}
