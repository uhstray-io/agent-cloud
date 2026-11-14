//! The host-access gate: `verify-host-access.yml` proves key-only SSH before `harden-ssh.yml`
//! disables password auth, and the templates and playbooks it sits between (static tier).
//!
//! What these pin: the hardening sequence has exactly one irreversible step (disabling password
//! auth), and the rule protecting it used to be a human remembering to check first. They assert
//! that the gate exists as a runnable artifact, that it cannot itself cause a lockout, and that it
//! cannot report success against nothing.
//!
//! Replaces platform/tests/test_host_access_gate.bats (plan 24, Phase 4).
//!
//! Port map (every BATS `@test` once):
//!
//! | BATS @test | Rust fn |
//! |---|---|
//! | verify-host-access: exists and is read-only | `verify_host_access_is_read_only` |
//! | verify-host-access: the empty-group guard runs OUTSIDE the targeted play | `the_group_guard_is_a_localhost_play_that_runs_first` |
//! | verify-host-access: EVERY raw probe individually tolerates failure | `every_raw_probe_tolerates_failure_on_its_own` |
//! | verify-host-access: the key-only probe refuses password and interactive auth | `the_key_only_probe_refuses_password_and_interactive_auth` |
//! | verify-host-access: the probe pins the host key instead of accepting any | `the_probe_pins_the_host_key_instead_of_accepting_any` |
//! | verify-host-access: the ssh task cannot run with an undefined key path | `the_probe_is_gated_on_a_materialised_key` |
//! | verify-host-access: cleanup removes BOTH temp files, always | `cleanup_removes_both_temp_files_always` |
//! | verify-host-access: the verdict itself gates on the key probe | `the_verdict_gates_on_the_key_probe` |
//! | verify-host-access: reads sshd's EFFECTIVE config, not the main file | `sshd_effective_config_is_read_not_the_main_file` |
//! | verify-host-access: checks what harden-ssh will need before it runs | `the_gate_checks_the_sudo_password_harden_ssh_needs` |
//! | verify-host-access: says it cannot prove the operator's own path | `the_gate_says_it_cannot_prove_the_operators_own_path` (candidate: pins text) |
//! | templates: Store SSH Password exists and stores no secret in Semaphore | `store_ssh_password_template_has_no_survey_field` |
//! | templates: Verify Host Access requires a target group | `verify_host_access_template_requires_a_target_group` |
//! | templates: Check Secrets can no longer false-green on no hosts | `check_secrets_template_names_the_target_group_once` |
//! | verify-host-access: connection params are resolved OUTSIDE the delegated block | `connection_parameters_resolve_before_the_delegated_probe` |
//! | verify-host-access: the probe refuses to run without a resolved user | `the_probe_refuses_to_run_without_a_resolved_user` |
//! | verify-host-access: the probe honours a non-default SSH port | `the_probe_honours_a_non_default_ssh_port` |
//! | verify-host-access: known_hosts uses the [host]:port form off port 22 | `known_hosts_uses_the_host_port_form_off_port_22` |
//! | templates: every Phase 1 step has a dev variant | `every_phase_1_step_has_a_dev_variant` |
//! | the probe play tolerates an unreachable host so the report still renders | `the_probe_play_tolerates_an_unreachable_host` |
//! | store-ssh-password lets the environment secret win over an extra var | `the_environment_secret_wins_over_the_extra_var` |
//! | distribute-ssh-keys offers the bootstrap password only when one exists | `the_bootstrap_password_is_offered_only_when_one_exists` |
//! | harden-ssh: a display variable cannot abort the verification it describes | `a_display_variable_in_harden_ssh_always_has_a_default` |

use agent_cloud_tests::repo;
use agent_cloud_tests::yaml::{self, Node};
use regex::Regex;

const PLAYBOOK: &str = "platform/playbooks/verify-host-access.yml";
const TEMPLATES: &str = "platform/semaphore/templates.yml";
const PIN: &str = "platform/playbooks/tasks/pin-ssh-host-key.yml";
const STORE: &str = "platform/playbooks/store-ssh-password.yml";
const DISTRIBUTE: &str = "platform/playbooks/distribute-ssh-keys.yml";
const HARDEN: &str = "platform/playbooks/harden-ssh.yml";

/// The file's text, read at run time (03 Part 1, rule 8: only deny-lists and pinned prose read it).
fn read(rel: &str) -> String {
    std::fs::read_to_string(repo::path(rel)).unwrap_or_else(|e| panic!("{rel}: {e}"))
}

/// The plays of a playbook, in file order.
fn plays(rel: &str) -> Vec<Node> {
    yaml::load_one(&repo::path(rel))
        .as_ref()
        .and_then(Node::as_seq)
        .map(<[Node]>::to_vec)
        .unwrap_or_default()
}

/// The templates of `templates.yml`, in file order.
fn templates() -> Vec<Node> {
    yaml::load_one(&repo::path(TEMPLATES))
        .as_ref()
        .and_then(|doc| doc.get("templates"))
        .and_then(Node::as_seq)
        .map(<[Node]>::to_vec)
        .unwrap_or_default()
}

/// The template named exactly `wanted`; a missing template is a failed test.
fn template<'a>(all: &'a [Node], wanted: &str) -> &'a Node {
    all.iter()
        .find(|t| str_at(t, "name") == Some(wanted))
        .unwrap_or_else(|| panic!("no template named {wanted:?}"))
}

fn survey(template: &Node) -> &[Node] {
    template
        .get("survey_vars")
        .and_then(Node::as_seq)
        .unwrap_or_default()
}

fn name(node: &Node) -> &str {
    str_at(node, "name").unwrap_or("")
}

fn str_at<'a>(node: &'a Node, key: &str) -> Option<&'a str> {
    node.get(key).and_then(Node::as_str)
}

/// The arguments of module `short` in a task, under any spelling Ansible accepts for it: the
/// short name, `ansible.builtin.<short>` or `ansible.legacy.<short>`. A check that reads one
/// spelling alone is blind to a task written under another, so every module read goes through
/// here.
fn module<'a>(task: &'a Node, short: &str) -> Option<&'a Node> {
    [
        short.to_owned(),
        format!("ansible.builtin.{short}"),
        format!("ansible.legacy.{short}"),
    ]
    .iter()
    .find_map(|key| task.get(key))
}

/// The file an `include_tasks` task includes.
fn included(task: &Node) -> Option<&str> {
    module(task, "include_tasks").and_then(Node::as_str)
}

/// A value of an action or task key, e.g. `fact(task, "_target_port")` for a set_fact.
fn fact<'a>(task: &'a Node, key: &str) -> Option<&'a str> {
    module(task, "set_fact").and_then(|facts| str_at(facts, key))
}

fn is_true(node: Option<&Node>) -> bool {
    matches!(node, Some(Node::Bool(true)))
}

fn is_false(node: Option<&Node>) -> bool {
    matches!(node, Some(Node::Bool(false)))
}

/// The text items of a `when:` or `that:` value: one string, or a list of them.
fn texts(node: Option<&Node>) -> Vec<&str> {
    match node {
        Some(Node::Str(s)) => vec![s],
        Some(Node::Seq(items)) => items.iter().filter_map(Node::as_str).collect(),
        _ => vec![],
    }
}

/// Every string in a node, keys included. Comments are not in the tree, so this is what the
/// file says, never what it notes.
fn strings(node: &Node) -> Vec<&str> {
    fn walk<'a>(node: &'a Node, out: &mut Vec<&'a str>) {
        match node {
            Node::Str(s) => out.push(s),
            Node::Seq(items) => items.iter().for_each(|item| walk(item, out)),
            Node::Map(entries) => {
                for (key, value) in entries {
                    walk(key, out);
                    walk(value, out);
                }
            }
            Node::Null | Node::Bool(_) => {}
        }
    }
    let mut out = vec![];
    walk(node, &mut out);
    out
}

/// The argv of a `command` task, one element per list item.
fn argv(task: &Node) -> Vec<&str> {
    module(task, "command")
        .and_then(|command| command.get("argv"))
        .and_then(Node::as_seq)
        .unwrap_or_default()
        .iter()
        .filter_map(Node::as_str)
        .collect()
}

/// The first task whose name contains `needle`. A missing step is a failed test.
fn named<'a>(items: &'a [Node], needle: &str) -> &'a Node {
    items
        .iter()
        .find(|item| name(item).contains(needle))
        .unwrap_or_else(|| panic!("no task named like {needle:?}"))
}

fn position_named(items: &[Node], needle: &str) -> Option<usize> {
    items.iter().position(|item| name(item).contains(needle))
}

/// The lines of `text` from the first line equal to `start` up to, not including, the next line
/// equal to `end`. Used only where the file's comments are the thing under test.
fn raw_section(text: &str, start: &str, end: &str) -> String {
    let mut out = String::new();
    let mut inside = false;
    for line in text.lines() {
        if !inside && line == start {
            inside = true;
        } else if inside && line == end {
            break;
        }
        if inside {
            out.push_str(line);
            out.push('\n');
        }
    }
    out
}

/// Every value stored under `key` in any mapping of `node`.
fn values_of_key<'a>(node: &'a Node, key: &str) -> Vec<&'a Node> {
    fn walk<'a>(node: &'a Node, key: &str, out: &mut Vec<&'a Node>) {
        match node {
            Node::Map(entries) => {
                for (k, v) in entries {
                    if k.as_str() == Some(key) {
                        out.push(v);
                    }
                    walk(v, key, out);
                }
            }
            Node::Seq(items) => items.iter().for_each(|item| walk(item, key, out)),
            Node::Str(_) | Node::Null | Node::Bool(_) => {}
        }
    }
    let mut out = vec![];
    walk(node, key, &mut out);
    out
}

#[test]
fn verify_host_access_is_read_only() {
    // Arrange
    let tasks = repo::tasks(PLAYBOOK);
    let write_modules = [
        "template",
        "lineinfile",
        "blockinfile",
        "replace",
        "service",
        "systemd",
        "user",
        "authorized_key",
    ];

    // Act
    let writers: Vec<&str> = tasks
        .iter()
        .filter(|task| {
            write_modules
                .iter()
                .any(|short| module(task, short).is_some())
        })
        .map(name)
        .collect();
    let delegated = tasks
        .iter()
        .filter(|task| str_at(task, "delegate_to") == Some("localhost"))
        .count();
    let mutating = Regex::new(r"method: (POST|PUT|PATCH|DELETE)").unwrap();

    // Assert
    assert!(
        writers.is_empty(),
        "a read-only gate names write modules: {writers:?}"
    );
    assert!(
        delegated >= 2,
        "the probe and the verdict's lookup run on the runner: found {delegated} delegate_to: localhost"
    );
    assert!(
        !mutating.is_match(&read(PLAYBOOK)),
        "the gate sends a mutating HTTP method"
    );
}

#[test]
fn the_group_guard_is_a_localhost_play_that_runs_first() {
    // Arrange
    let plays = plays(PLAYBOOK);
    let guard_at = plays
        .iter()
        .position(|play| name(play) == "Pre-flight: validate the target group")
        .expect("a pre-flight play named for the target group");
    let verify_at = plays
        .iter()
        .position(|play| name(play).starts_with("Verify host access"))
        .expect("the host-targeted play");
    let guard = &plays[guard_at];

    // Act
    let guard_tasks = guard
        .get("tasks")
        .and_then(Node::as_seq)
        .unwrap_or_default();
    let checks: Vec<&str> = guard_tasks
        .iter()
        .flat_map(|task| texts(module(task, "assert").and_then(|check| check.get("that"))))
        .collect();
    let verify_hosts = str_at(&plays[verify_at], "hosts").unwrap_or_default();
    let undefined_default = Regex::new(r#"default\('ungrouped'\)|target_service: "ungrouped""#)
        .unwrap()
        .is_match(&read(PLAYBOOK));

    // Assert
    assert!(
        guard_at < verify_at,
        "the guard (play {guard_at}) must run before the host-targeted play (play {verify_at})"
    );
    assert_eq!(
        str_at(guard, "hosts"),
        Some("localhost"),
        "the guard must run on localhost: a guard inside the targeted play is skipped with it"
    );
    assert!(
        checks
            .iter()
            .any(|c| c.contains("target_service in groups")),
        "the guard does not assert the group exists: {checks:?}"
    );
    assert!(
        checks
            .iter()
            .any(|c| c.contains("groups[target_service] | default([]) | length) > 0")),
        "the guard does not assert the group has hosts: {checks:?}"
    );
    assert!(
        verify_hosts.contains("default('__verify_host_access_no_target__')"),
        "the targeted play's sentinel must be a name no inventory can hold: {verify_hosts}"
    );
    assert!(
        !undefined_default,
        "a fallback to the real group 'ungrouped' would skip the play and report success"
    );
}

#[test]
fn every_raw_probe_tolerates_failure_on_its_own() {
    // Arrange
    let tasks = repo::tasks(PLAYBOOK);

    // Act
    let raw: Vec<&Node> = tasks
        .iter()
        .filter(|task| module(task, "raw").is_some())
        .collect();
    let intolerant: Vec<&str> = raw
        .iter()
        .filter(|task| !is_false(task.get("failed_when")))
        .map(|task| name(task))
        .collect();

    // Assert
    assert!(raw.len() >= 4, "found {} raw probes", raw.len());
    assert!(
        intolerant.is_empty(),
        "raw probes that can fail the run: {intolerant:?}"
    );
}

#[test]
fn the_key_only_probe_refuses_password_and_interactive_auth() {
    // Arrange
    let tasks = repo::tasks(PLAYBOOK);
    let probe = named(&tasks, "Connect using the key ONLY");

    // Act
    let args = argv(probe);

    // Assert
    for option in [
        "PreferredAuthentications=publickey",
        "PubkeyAuthentication=yes",
        "PasswordAuthentication=no",
        "KbdInteractiveAuthentication=no",
        "BatchMode=yes",
    ] {
        assert!(
            args.contains(&option),
            "the key-only probe does not pass {option}: {args:?}"
        );
    }
}

#[test]
fn the_probe_pins_the_host_key_instead_of_accepting_any() {
    // Arrange
    let tasks = repo::tasks(PLAYBOOK);
    let args = argv(named(&tasks, "Connect using the key ONLY"));
    let pin_tasks = repo::tasks(PIN);

    // Act
    let includes_pin = tasks
        .iter()
        .any(|task| included(task) == Some("tasks/pin-ssh-host-key.yml"));
    let reads_the_host = pin_tasks.iter().any(|task| {
        name(task).contains("read the target's own host key over the existing connection")
    });

    // Assert
    assert!(
        args.contains(&"StrictHostKeyChecking=yes"),
        "the probe does not refuse an unknown host key: {args:?}"
    );
    assert!(
        !args.iter().any(|arg| arg.contains("accept-new")),
        "the probe trusts a first-seen host key: {args:?}"
    );
    assert!(
        args.iter()
            .any(|arg| arg.starts_with("UserKnownHostsFile=")),
        "the probe does not use the pinned known_hosts: {args:?}"
    );
    assert!(
        includes_pin,
        "the gate does not include tasks/pin-ssh-host-key.yml"
    );
    assert!(
        reads_the_host,
        "the shared pin task does not read the target's own host key"
    );
}

#[test]
fn the_probe_is_gated_on_a_materialised_key() {
    // Arrange
    let tasks = repo::tasks(PLAYBOOK);
    let probe = named(&tasks, "Connect using the key ONLY");

    // Act
    let when = texts(probe.get("when"));

    // Assert
    assert!(
        when.iter()
            .any(|w| w.contains("_probe_key.materialised | default(false) | bool")),
        "the probe runs without a materialised key: {when:?}"
    );
    assert!(
        when.iter().any(|w| w.contains("_svc_key | length > 0")),
        "the probe runs with no key in the store: {when:?}"
    );
}

#[test]
fn cleanup_removes_both_temp_files_always() {
    // Arrange
    let tasks = repo::tasks(PLAYBOOK);
    let probe = named(&tasks, "Key-only reachability probe");
    let always = probe
        .get("always")
        .and_then(Node::as_seq)
        .unwrap_or_default();
    let pin = repo::tasks(PIN);

    // Act
    let removes_key = always.iter().any(|task| {
        included(task) == Some("tasks/remove-ssh-key.yml")
            && task
                .get("vars")
                .and_then(|vars| str_at(vars, "ssh_key_result_var"))
                == Some("_probe_key")
    });
    let pinned_path = pin.iter().find_map(|task| fact(task, "_pshk_kh"));
    let pin_task = named(&tasks, "Pin the target's SSH host key");
    let pin_writes_into_scratch = pin_task
        .get("vars")
        .and_then(|vars| str_at(vars, "ssh_key_result_var"));

    // Assert
    assert!(
        removes_key,
        "the always block does not remove the probe key with tasks/remove-ssh-key.yml"
    );
    assert_eq!(
        pinned_path,
        Some("{{ lookup('ansible.builtin.vars', ssh_key_result_var).known_hosts }}"),
        "the pin is not written into the materialised key's scratch directory"
    );
    assert_eq!(
        pin_writes_into_scratch,
        Some("_probe_key"),
        "the host-key pin is handed a different key than the wipe removes"
    );
}

#[test]
fn the_verdict_gates_on_the_key_probe() {
    // Arrange
    let tasks = repo::tasks(PLAYBOOK);
    let verdict_at =
        position_named(&tasks, "Verdict: is it safe to run harden-ssh").expect("the verdict task");

    // Act
    let verdict: Vec<&str> = tasks[verdict_at..].iter().flat_map(strings).collect();

    // Assert
    for needle in [
        "_keyprobe.rc",
        "PENDING-OPERATOR",
        "NO-GO",
        "_svc_key_len",
        "_hostkey_ok",
    ] {
        assert!(
            verdict.iter().any(|s| s.contains(needle)),
            "the verdict does not consult {needle}"
        );
    }
    assert!(
        !verdict.iter().any(|s| s.contains("'GO —")),
        "an unqualified GO claims both directions are proven"
    );
}

#[test]
fn sshd_effective_config_is_read_not_the_main_file() {
    // Arrange
    let tasks = repo::tasks(PLAYBOOK);

    // Act
    let reads_effective_config = tasks.iter().any(|task| {
        module(task, "raw")
            .and_then(Node::as_str)
            .is_some_and(|cmd| cmd.contains("sshd -T"))
    });

    // Assert
    assert!(
        reads_effective_config,
        "no task reads `sshd -T`, which accounts for sshd_config.d drop-ins"
    );
}

#[test]
fn the_gate_checks_the_sudo_password_harden_ssh_needs() {
    // Arrange
    let tasks = repo::tasks(PLAYBOOK);
    let all: Vec<&str> = tasks.iter().flat_map(strings).collect();

    // Act
    let reads_become_password = all.iter().any(|s| s.contains("become_password"));
    let names_the_store_step = all.iter().any(|s| s.contains("Store SSH Password first"));

    // Assert
    assert!(
        reads_become_password,
        "the gate does not check the sudo password is in the store"
    );
    assert!(
        names_the_store_step,
        "the verdict does not name the step that stores the sudo password"
    );
}

#[test]
fn the_gate_says_it_cannot_prove_the_operators_own_path() {
    // Arrange
    let text = read(PLAYBOOK);
    let says_it = Regex::new(r"(?i)cannot (tell you|prove).*workstation|two directions").unwrap();

    // Act
    let found = text.lines().any(|line| says_it.is_match(line));

    // Assert
    assert!(
        found,
        "the gate does not say it cannot prove the operator's workstation path"
    );
}

#[test]
fn store_ssh_password_template_has_no_survey_field() {
    // Arrange
    let all = templates();

    // Act
    let store = template(&all, "Store SSH Password");

    // Assert
    assert!(
        store.get("survey_vars").is_none(),
        "Semaphore persists survey values, so the password cannot be one"
    );
}

#[test]
fn verify_host_access_template_requires_a_target_group() {
    // Arrange
    let all = templates();
    let verify = template(&all, "Verify Host Access");

    // Act
    let required: Vec<&str> = survey(verify)
        .iter()
        .filter(|field| is_true(field.get("required")))
        .filter_map(|field| str_at(field, "name"))
        .collect();

    // Assert
    assert_eq!(
        required,
        vec!["target_service"],
        "the gate must require exactly the target group"
    );
}

#[test]
fn check_secrets_template_names_the_target_group_once() {
    // Arrange
    let all = templates();
    let check = template(&all, "Check Secrets");

    // Act
    let targets = survey(check)
        .iter()
        .filter(|field| str_at(field, "name") == Some("target_service"))
        .count();

    // Assert
    assert_eq!(targets, 1, "target_service is not declared exactly once");
}

#[test]
fn connection_parameters_resolve_before_the_delegated_probe() {
    // Arrange
    let tasks = repo::tasks(PLAYBOOK);
    let resolve_at = position_named(&tasks, "Resolve the target's connection parameters")
        .expect("the connection-parameter task");
    let probe_at =
        position_named(&tasks, "Key-only reachability probe").expect("the delegated probe");
    let resolve = &tasks[resolve_at];

    // Act
    let addr = fact(resolve, "_target_addr").unwrap_or_default();
    let user = fact(resolve, "_target_user").unwrap_or_default();
    let play_defines_addr = plays(PLAYBOOK).iter().any(|play| {
        play.get("vars")
            .and_then(|vars| vars.get("_target_addr"))
            .is_some()
    });

    // Assert
    assert!(
        addr.contains("hostvars[inventory_hostname].ansible_host"),
        "the target address is not read from the target's hostvars: {addr}"
    );
    assert!(
        user.contains("hostvars[inventory_hostname].ansible_user"),
        "the target user is not read from the target's hostvars: {user}"
    );
    assert!(
        resolve_at < probe_at,
        "the connection parameters resolve inside the delegated probe"
    );
    assert!(
        !play_defines_addr,
        "a play-level _target_addr is evaluated lazily, in the delegated context"
    );
}

#[test]
fn the_probe_refuses_to_run_without_a_resolved_user() {
    // Arrange
    let tasks = repo::tasks(PLAYBOOK);
    let probe = named(&tasks, "Connect using the key ONLY");

    // Act
    let when = texts(probe.get("when"));
    let args = argv(probe);

    // Assert
    assert!(
        when.iter()
            .any(|w| w.contains("_target_user | default('') | trim | length) > 0")),
        "the probe runs with an empty user: {when:?}"
    );
    assert!(
        args.iter()
            .any(|arg| arg.contains("{{ _target_user }}@{{ _target_addr }}")),
        "the ssh target is not the resolved user and address: {args:?}"
    );
    assert!(
        !args.iter().any(|arg| arg.contains("{{ ansible_user }}@")),
        "the ssh target uses the delegated connection user: {args:?}"
    );
}

#[test]
fn the_probe_honours_a_non_default_ssh_port() {
    // Arrange
    let tasks = repo::tasks(PLAYBOOK);
    let resolve = named(&tasks, "Resolve the target's connection parameters");
    let args = argv(named(&tasks, "Connect using the key ONLY"));

    // Act
    let port_fact = fact(resolve, "_target_port").unwrap_or_default();
    let passes_port = args
        .windows(2)
        .any(|pair| pair[0] == "-p" && pair[1] == "{{ _target_port }}");

    // Assert
    assert!(
        port_fact.contains("hostvars[inventory_hostname].ansible_port"),
        "the port is not read from the target's hostvars: {port_fact}"
    );
    assert!(
        passes_port,
        "the probe connects to port 22 whatever the host declares: {args:?}"
    );
}

#[test]
fn known_hosts_uses_the_host_port_form_off_port_22() {
    // Arrange
    let pin = repo::tasks(PIN);
    let tasks = repo::tasks(PLAYBOOK);

    // Act
    let content = pin
        .iter()
        .filter_map(|task| module(task, "copy"))
        .find_map(|copy| str_at(copy, "content"))
        .unwrap_or_default();
    let line = pin
        .iter()
        .find_map(|task| fact(task, "_pshk_line"))
        .unwrap_or_default();
    let include = tasks
        .iter()
        .find(|task| included(task) == Some("tasks/pin-ssh-host-key.yml"))
        .expect("the gate includes the pin task");
    let port_handed_over = include
        .get("vars")
        .and_then(|vars| str_at(vars, "ssh_host_key_port"));

    // Assert
    assert!(
        content.contains("{{ _pshk_line }}"),
        "known_hosts is not written from the pinned line: {content}"
    );
    assert!(
        line.contains("(_pshk_pin.port | int) == 22"),
        "the pinned line does not switch on the port: {line}"
    );
    assert!(
        line.contains("']:'"),
        "the pinned line does not build the [host]:port form: {line}"
    );
    assert_eq!(
        port_handed_over,
        Some("{{ _target_port }}"),
        "the pin is not handed the port the probe connects to"
    );
}

#[test]
fn every_phase_1_step_has_a_dev_variant() {
    // Arrange
    let all = templates();
    let steps = [
        "Store SSH Password",
        "Verify Host Access",
        "Generate Service SSH Key",
        "Distribute SSH Keys",
        "Install Podman",
        "Harden SSH",
        "Apply Firewall",
    ];

    // Act
    let without: Vec<&str> = steps
        .into_iter()
        .filter(|step| !is_true(template(&all, step).get("dev_variant")))
        .collect();

    // Assert
    assert!(
        without.is_empty(),
        "Phase 1 steps that would run from main: {without:?}"
    );
}

#[test]
fn the_probe_play_tolerates_an_unreachable_host() {
    // Arrange
    let plays = plays(PLAYBOOK);

    // Act
    let probe = plays
        .iter()
        .find(|play| name(play).starts_with("Verify host access"))
        .expect("the host-targeted play");

    // Assert
    assert!(
        is_true(probe.get("ignore_unreachable")),
        "an unreachable host is dropped before the report, so the verdict is an exit code"
    );
}

#[test]
fn the_environment_secret_wins_over_the_extra_var() {
    // Arrange
    let text = read(STORE);
    let doc = yaml::load_one(&repo::path(STORE)).expect("store-ssh-password.yml has a document");
    let tasks = repo::tasks(STORE);

    // Act
    let precedence =
        Regex::new(r"lookup\('env', *'SSH_PASSWORD'\) *\| *default\(ssh_password").unwrap();
    let reversed = Regex::new(r"_ssh_password:.*ssh_password *\| *default\(lookup").unwrap();
    let bare_extra_var = Regex::new(r"(?m)^ *- ssh_password is defined").unwrap();
    let defined_env_first = values_of_key(&doc, "_ssh_password")
        .iter()
        .any(|value| value.as_str().is_some_and(|v| precedence.is_match(v)));
    let all = strings(&doc);
    let writes_resolved_fact = all
        .iter()
        .any(|s| s.contains("'become_password': _ssh_password"))
        && all
            .iter()
            .any(|s| s.contains("'login_password': _ssh_password"));
    let includes_transport_guard = tasks
        .iter()
        .any(|task| included(task) == Some("tasks/assert-bao-transport.yml"));
    let template_slice = raw_section(
        &read(TEMPLATES),
        "  - name: Store SSH Password",
        "  - name: Verify Host Access",
    );
    let leaking_form = Regex::new(r"(?m)^ *# *-e ssh_password=<the bootstrap password>$").unwrap();

    // Assert
    assert!(
        defined_env_first,
        "_ssh_password does not take the environment secret first"
    );
    assert!(
        !text.lines().any(|line| reversed.is_match(line)),
        "_ssh_password takes the extra var first, so a stray -e overrides the secret"
    );
    assert!(
        writes_resolved_fact,
        "the write does not consume the resolved _ssh_password"
    );
    assert!(
        !text.lines().any(|line| bare_extra_var.is_match(line)),
        "the playbook falls back to the bare extra var"
    );
    assert!(
        includes_transport_guard,
        "the password is sent without the cleartext guard"
    );
    assert!(
        template_slice.contains("SSH_PASSWORD"),
        "the template does not name the SSH_PASSWORD environment secret"
    );
    assert!(
        !template_slice
            .lines()
            .any(|line| leaking_form.is_match(line)),
        "the template still teaches the -e ssh_password form that leaks it"
    );
}

#[test]
fn the_bootstrap_password_is_offered_only_when_one_exists() {
    // Arrange
    let tasks = repo::tasks(DISTRIBUTE);
    let offer = named(&tasks, "Offer it as a password fallback");

    // Act
    let password = module(offer, "add_host").and_then(|add| str_at(add, "ansible_password"));
    let when = texts(offer.get("when"));
    let rescue_clears_it = tasks
        .iter()
        .filter_map(|task| task.get("rescue").and_then(Node::as_seq))
        .flatten()
        .filter_map(|task| module(task, "set_fact"))
        .any(|facts| str_at(facts, "_bootstrap_pw") == Some(""));

    // Assert
    assert_eq!(
        password,
        Some("{{ _bootstrap_pw }}"),
        "the fallback does not carry the bootstrap password"
    );
    assert_eq!(
        when,
        vec!["_bootstrap_pw | length > 0"],
        "the fallback is offered with no password"
    );
    assert!(
        rescue_clears_it,
        "a failed read of the password can abort the run instead of falling back to keys"
    );
}

#[test]
fn a_display_variable_in_harden_ssh_always_has_a_default() {
    // Arrange
    let doc = yaml::load_one(&repo::path(HARDEN)).expect("harden-ssh.yml has a document");
    let uses = Regex::new(r"\{\{\s*service_name\b[^}]*\}\}").unwrap();

    // Act
    let found: Vec<&str> = strings(&doc)
        .into_iter()
        .flat_map(|s| uses.find_iter(s).map(|m| m.as_str()))
        .collect();
    let undefaulted: Vec<&str> = found
        .iter()
        .copied()
        .filter(|use_| !use_.contains("default("))
        .collect();

    // Assert
    assert!(!found.is_empty(), "service_name is not referenced at all");
    assert!(
        undefaulted.is_empty(),
        "undefaulted uses of service_name: {undefaulted:?}"
    );
}
