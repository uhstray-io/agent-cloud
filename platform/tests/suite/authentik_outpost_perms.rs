//! The embedded outpost's provider permissions on a fresh instance (AC-1135), static tier.
//!
//! `deploy-authentik.yml` Phase 3 checks the grant that lets the embedded outpost's service account
//! view the proxy providers attached to it, before the forward_auth probe runs. The check is
//! read-only, and the repair runs only when the check reports a gap, never under --check. These
//! tests read the playbook and the script it sends to the container, and check the shape of that
//! step: its place in Phase 3, the read-only probe, the guarded repair, the in-memory delivery of
//! the script, and the script's mode, refusal, repair and re-read. Structural only: nothing is run.

use agent_cloud_tests::repo;
use agent_cloud_tests::text::read;
use agent_cloud_tests::yaml::{self, Node};

const PLAYBOOK: &str = "platform/playbooks/deploy-authentik.yml";
const TEMPLATE: &str =
    "platform/services/authentik/deployment/templates/converge-outpost-perms.py.j2";
const ZZ_SSO: &str = "platform/services/authentik/deployment/templates/zz-sso-bindings.yaml.j2";
const PHASE3: &str = "Phase 3: Verify authentik";
const SHAPE_CHECK: &str =
    "Outpost permissions: the Phase 1 app list and catalog have the expected shape";
const PROBE: &str = "Outpost permissions: check the embedded outpost's view access";
const REPAIR: &str = "Outpost permissions: grant the embedded outpost view access";
const RESULT: &str = "Outpost permissions: result";
const WAIT: &str = "Wait for the embedded outpost to serve forward_auth";

/// The tasks of the Phase 3 play, in file order.
fn phase3_tasks() -> Vec<Node> {
    let doc = yaml::load_one(&repo::path(PLAYBOOK))
        .unwrap_or_else(|| panic!("{PLAYBOOK}: no YAML document"));
    let plays = doc
        .as_seq()
        .unwrap_or_else(|| panic!("{PLAYBOOK}: the document is not a play list"));
    let play = plays
        .iter()
        .find(|play| play.get("name").and_then(Node::as_str) == Some(PHASE3))
        .unwrap_or_else(|| panic!("{PLAYBOOK}: no play named `{PHASE3}`"));
    play.get("tasks")
        .and_then(Node::as_seq)
        .unwrap_or_else(|| panic!("{PHASE3}: no task list"))
        .to_vec()
}

/// Index of the first task of `tasks` whose name starts with `prefix`; panics when none does.
fn index_of(tasks: &[Node], prefix: &str) -> usize {
    tasks
        .iter()
        .position(|task| {
            task.get("name")
                .and_then(Node::as_str)
                .is_some_and(|name| name.starts_with(prefix))
        })
        .unwrap_or_else(|| panic!("{PHASE3}: no task whose name starts with `{prefix}`"))
}

/// The `ansible.builtin.command` arguments of a task; panics when the task has none.
fn command_of(task: &Node) -> &Node {
    task.get("ansible.builtin.command").unwrap_or_else(|| {
        panic!(
            "a task has no ansible.builtin.command: {:?}",
            task.get("name")
        )
    })
}

/// The words of a command's argv.
fn argv_of(task: &Node) -> Vec<&str> {
    command_of(task)
        .get("argv")
        .and_then(Node::as_seq)
        .unwrap_or_else(|| panic!("a command task has no argv"))
        .iter()
        .filter_map(Node::as_str)
        .collect()
}

/// The text a command task sends on stdin; panics when it sends none.
fn stdin_of(task: &Node) -> &str {
    command_of(task)
        .get("stdin")
        .and_then(Node::as_str)
        .unwrap_or_else(|| panic!("a command task sends no stdin"))
}

/// A string key of a task, empty when the key is absent or is not a string.
fn str_field<'a>(task: &'a Node, key: &str) -> &'a str {
    task.get(key).and_then(Node::as_str).unwrap_or_default()
}

#[test]
fn convergence_steps_sit_in_phase3_in_order_before_the_forward_auth_wait() {
    let tasks = phase3_tasks();
    let shape = index_of(&tasks, SHAPE_CHECK);
    let probe = index_of(&tasks, PROBE);
    let repair = index_of(&tasks, REPAIR);
    let result = index_of(&tasks, RESULT);
    let wait = index_of(&tasks, WAIT);
    assert!(
        shape < probe && probe < repair && repair < result && result < wait,
        "{PHASE3}: the steps must run in this order, before the forward_auth wait: shape check \
         (task {shape}), probe (task {probe}), repair (task {repair}), result (task {result}), \
         wait (task {wait})"
    );
}

#[test]
fn probe_is_read_only_runs_under_check_mode_and_retries_only_on_75() {
    let tasks = phase3_tasks();
    let probe = &tasks[index_of(&tasks, PROBE)];
    assert_eq!(
        probe.get("check_mode"),
        Some(&Node::Bool(false)),
        "{PROBE}: check_mode must be false, so the probe runs under --check"
    );
    assert_eq!(
        probe.get("changed_when"),
        Some(&Node::Bool(false)),
        "{PROBE}: changed_when must be false: the probe never writes a grant"
    );
    let until = str_field(probe, "until");
    assert!(
        until.contains("rc != 75"),
        "{PROBE}: until must retry on exit 75 (an expected provider not attached yet), got `{until}`"
    );
    let argv = argv_of(probe);
    assert!(
        argv.contains(&"OUTPOST_PERMS_MODE=check"),
        "{PROBE}: the probe must run the script in check mode, got {argv:?}"
    );
    assert!(
        probe
            .get("no_log")
            .is_none_or(|value| *value == Node::Bool(false)),
        "{PROBE}: no_log is for credential tasks only; this task prints names and booleans"
    );
}

#[test]
fn repair_runs_only_outside_check_mode_on_a_reported_gap_and_reports_its_repair() {
    let tasks = phase3_tasks();
    let repair = &tasks[index_of(&tasks, REPAIR)];
    assert!(
        repair.get("check_mode").is_none(),
        "{REPAIR}: no check_mode key: the repair is guarded by its when, not forced under --check"
    );
    let when = str_field(repair, "when");
    assert!(
        when.starts_with("not ansible_check_mode and "),
        "{REPAIR}: when must start with `not ansible_check_mode and`, got `{when}`"
    );
    assert!(
        when.contains("'outpost-perms: would repair' in _outpost_perms_probe.stdout"),
        "{REPAIR}: when must run only on the probe's would-repair line, got `{when}`"
    );
    let changed = str_field(repair, "changed_when");
    assert!(
        changed.contains("outpost-perms: REPAIRED"),
        "{REPAIR}: changed_when must key on the REPAIRED line, got `{changed}`"
    );
    let argv = argv_of(repair);
    assert!(
        argv.contains(&"OUTPOST_PERMS_MODE=repair"),
        "{REPAIR}: the repair must run the script in repair mode, got {argv:?}"
    );
    assert!(
        repair
            .get("no_log")
            .is_none_or(|value| *value == Node::Bool(false)),
        "{REPAIR}: no_log is for credential tasks only; this task prints names and booleans"
    );
}

#[test]
fn both_steps_send_the_same_script_on_stdin_and_nothing_is_written_to_the_host() {
    let text = read(PLAYBOOK);
    assert!(
        !text.contains("/tmp/converge-outpost-perms.py"),
        "{PLAYBOOK}: the script must not be written to the host; it is sent on stdin"
    );
    let tasks = phase3_tasks();
    let probe = stdin_of(&tasks[index_of(&tasks, PROBE)]);
    let repair = stdin_of(&tasks[index_of(&tasks, REPAIR)]);
    assert_eq!(
        probe, repair,
        "{PHASE3}: the probe and the repair must send the same script"
    );
    assert!(
        probe.contains("lookup('ansible.builtin.template'")
            && probe.contains("converge-outpost-perms.py.j2"),
        "{PROBE}: the script must be rendered in memory from the template, got `{probe}`"
    );
    assert!(
        probe.contains("template_vars={'authentik_apps': _enabled_apps}"),
        "{PROBE}: authentik_apps must be passed from Phase 1; it is undefined in Phase 3 when unset"
    );
}

#[test]
fn expected_providers_use_the_forward_auth_selection_the_sso_bindings_make() {
    let template = read(TEMPLATE);
    let sso = read(ZZ_SSO);
    for (rel, text) in [(TEMPLATE, &template), (ZZ_SSO, &sso)] {
        assert!(
            text.contains("{% for slug in (authentik_apps | sort) %}"),
            "{rel}: the enabled-app loop is not `authentik_apps | sort`"
        );
        assert!(
            text.contains("{% if authentik_app_catalog[slug].type == 'forward_auth' %}"),
            "{rel}: the provider selection is not catalog type forward_auth"
        );
    }
}

#[test]
fn script_takes_its_mode_from_the_environment_and_repairs_only_when_told_to() {
    let text = read(TEMPLATE);
    assert!(
        text.contains(r#"MODE = os.environ.get("OUTPOST_PERMS_MODE", "check")"#),
        "{TEMPLATE}: MODE must come from OUTPOST_PERMS_MODE and default to check"
    );
    assert!(
        text.contains(r#"if MODE != "repair":"#),
        "{TEMPLATE}: every mode other than repair must stop before the repair"
    );
    assert!(
        !text.contains("ansible_check_mode"),
        "{TEMPLATE}: the mode is set per run by the playbook, not by the Ansible check flag"
    );
}

#[test]
fn script_refuses_without_the_grant_and_rereads_after_the_repair() {
    let text = read(TEMPLATE);
    let refusal = "outpost-perms: Outpost.build_user_permissions is not available on this \
                   Authentik image; refusing to guess a repair";
    assert!(
        text.contains(refusal),
        "{TEMPLATE}: the refusal message is missing or changed"
    );
    assert!(
        text.contains("sys.exit(3)"),
        "{TEMPLATE}: no sys.exit(3) for the refusal"
    );
    assert!(
        text.contains("sys.exit(75)"),
        "{TEMPLATE}: no sys.exit(75) for providers not attached"
    );
    assert!(
        text.contains("sys.exit(4)"),
        "{TEMPLATE}: no sys.exit(4) for a grant still missing"
    );
    let call = text
        .find(".build_user_permissions(")
        .unwrap_or_else(|| panic!("{TEMPLATE}: the repair does not call build_user_permissions"));
    let after_repair = &text[call..];
    assert!(
        after_repair.contains("has_perm(") && after_repair.contains("User.objects.get("),
        "{TEMPLATE}: after the repair the state must be read again through a fresh user object"
    );
    let lower = text.to_lowercase();
    assert!(
        !lower.contains("token") && !lower.contains("authorization"),
        "{TEMPLATE}: the script must not reference the outpost token or any Authorization header"
    );
}
