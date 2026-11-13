//! A sudo password is resolved before anything needs escalation (static tier).
//!
//! A play that escalates at PLAY level dies at "Gathering Facts" with ok=0 on any host that still
//! requires a sudo password. The become plugin raises before any module runs, so no per-task
//! `failed_when` can catch it. That is precisely the state of a host these playbooks exist to
//! PREPARE. Measured 2026-08-25: install-podman.yml failed that way against a freshly keyed host,
//! and of the six playbooks escalating at play level only harden-ssh.yml resolved the password.
//!
//! So the ordering is the invariant. The resolver is the first task, automatic fact gathering is
//! off (gathering runs before tasks), and gathering happens explicitly straight after. The
//! transport guard runs before the lookup it protects and needs no privilege. No dynamic include
//! carries `become`: ansible-core refuses that only when a play reaches the task, and no syntax
//! check sees it (docs/MISTAKES.md 2.17). deploy-github-runner.yml is the one named gap, because
//! its fix belongs to work on another branch.
//!
//! Replaces platform/tests/test_become_password_resolution.bats (plan 24, Phase 4).
//!
//! ## Port map
//!
//! | BATS @test | Rust fn |
//! |---|---|
//! | become: the shared resolver task exists and does not itself need escalation | `the_password_fetch_runs_without_escalation_and_hides_its_output` |
//! | become: the resolver reads the same secret path harden-ssh does | `harden_ssh_reads_the_secret_path_the_resolver_reads` |
//! | become: every play-level-become playbook resolves a sudo password | `every_playbook_that_escalates_at_play_level_resolves_a_sudo_password` |
//! | become: automatic fact gathering is OFF wherever the resolver is used | `fact_gathering_is_off_in_every_play_that_uses_the_resolver` |
//! | become: the first task is what makes escalation possible | `the_first_task_of_every_escalating_play_resolves_the_password`, with the two self-checks `the_first_task_check_rejects_a_later_play_without_the_resolver` and `the_first_task_check_rejects_gathering_before_the_resolver` |
//! | become: gathering, where it happens at all, happens after escalation is possible | `explicit_gathering_comes_straight_after_the_resolver` |
//! | become: the resolver guards the transport it sends the credential over | `the_resolver_guards_the_transport_before_it_fetches` |
//! | become: the known gap is still the only one, and is named | `the_known_gap_still_resolves_no_password` |
//! | become: the transport guard needs no privilege, and says so | `the_transport_guard_is_a_single_assert_task`, with `the_transport_guard_task_declares_become_false` |
//! | become: no dynamic include anywhere carries a become keyword | `no_dynamic_include_task_carries_become` |

use std::collections::BTreeSet;

use agent_cloud_tests::repo;
use agent_cloud_tests::yaml::{self, Node};

const RESOLVER_TASK: &str = "platform/playbooks/tasks/resolve-become-password.yml";
const GUARD_TASK: &str = "platform/playbooks/tasks/assert-bao-transport.yml";
const HARDEN_SSH: &str = "platform/playbooks/harden-ssh.yml";
const KNOWN_GAP: &str = "platform/playbooks/deploy-github-runner.yml";
const SECRET_PATH: &str = "secret/data/services/ssh:become_password";
const RESOLVER: &str = "resolve-become-password";
const RESOLVER_FILE: &str = "resolve-become-password.yml";
const GUARD_FILE: &str = "assert-bao-transport.yml";
const PASSWORD: &str = "become_password";
const INCLUDE_KEYS: [&str; 2] = ["include_tasks", "ansible.builtin.include_tasks"];

/// The top-level playbooks, the files `ansible-playbook` is launched on. A `*` pathspec would also
/// match `tasks/*.yml`, so only entries with no further `/` after the directory are kept.
fn top_level_playbooks() -> Vec<String> {
    let mut files: Vec<String> = repo::ls_files(&[":(glob)platform/playbooks/*.yml"])
        .into_iter()
        .filter(|f| {
            f.strip_prefix("platform/playbooks/")
                .is_some_and(|rest| !rest.contains('/'))
        })
        .collect();
    files.sort();
    files
}

fn load(rel: &str) -> Node {
    yaml::load_one(&repo::path(rel)).unwrap_or_else(|| panic!("{rel}: empty document"))
}

/// The plays of a file. A playbook is a list at the top level. A file that is not a list, such as
/// requirements.yml, holds no plays, as the BATS Python check read it.
fn plays(rel: &str) -> Vec<Node> {
    load(rel).as_seq().map(<[Node]>::to_vec).unwrap_or_default()
}

/// Every task of a file with the plays removed. A play holds its tasks, so a play that mentions a
/// path only through its vars would otherwise count as a task that reads it.
fn leaf_tasks(rel: &str) -> Vec<Node> {
    repo::tasks(rel)
        .into_iter()
        .filter(|t| t.get("hosts").is_none())
        .collect()
}

/// A string at any depth, in a key or a value.
fn any_string(node: &Node, pred: &dyn Fn(&str) -> bool) -> bool {
    match node {
        Node::Str(s) => pred(s),
        Node::Seq(items) => items.iter().any(|item| any_string(item, pred)),
        Node::Map(entries) => entries
            .iter()
            .any(|(k, v)| any_string(k, pred) || any_string(v, pred)),
        Node::Null | Node::Bool(_) => false,
    }
}

/// A string at any depth in a value. Keys are names, not data, so they are not searched.
fn any_value(node: &Node, pred: &dyn Fn(&str) -> bool) -> bool {
    match node {
        Node::Str(s) => pred(s),
        Node::Seq(items) => items.iter().any(|item| any_value(item, pred)),
        Node::Map(entries) => entries.iter().any(|(_, v)| any_value(v, pred)),
        Node::Null | Node::Bool(_) => false,
    }
}

fn mentions(node: &Node, needle: &str) -> bool {
    any_string(node, &|s: &str| s.contains(needle))
}

fn mentions_any(node: &Node, needles: &[&str]) -> bool {
    needles.iter().any(|needle| mentions(node, needle))
}

/// Whether a mapping has the key directly or at any depth.
fn has_key(node: &Node, key: &str) -> bool {
    match node {
        Node::Map(entries) => entries
            .iter()
            .any(|(k, v)| k.as_str() == Some(key) || has_key(v, key)),
        Node::Seq(items) => items.iter().any(|item| has_key(item, key)),
        _ => false,
    }
}

fn task_name(task: &Node) -> &str {
    task.get("name").and_then(Node::as_str).unwrap_or("")
}

fn play_name(play: &Node) -> &str {
    play.get("name")
        .and_then(Node::as_str)
        .unwrap_or("unnamed play")
}

fn escalates(play: &Node) -> bool {
    play.get("become") == Some(&Node::Bool(true))
}

/// The check the BATS `invalid_become_plays` made. An escalating play is invalid unless fact
/// gathering is explicitly off AND its first task resolves the password. Only values are searched:
/// a key named `become_password` is a name, not a resolver.
fn escalating_plays_not_resolving_first(plays: &[Node]) -> Vec<String> {
    plays
        .iter()
        .filter(|play| escalates(play))
        .filter(|play| {
            let first = play
                .get("tasks")
                .and_then(Node::as_seq)
                .and_then(<[Node]>::first);
            let resolves = first.is_some_and(|task| {
                any_value(task, &|s: &str| {
                    s.contains(RESOLVER_FILE) || s.contains(PASSWORD)
                })
            });
            play.get("gather_facts") != Some(&Node::Bool(false)) || !resolves
        })
        .map(|play| play_name(play).to_owned())
        .collect()
}

/// The plays of an in-memory playbook, for the self-checks.
fn synthetic_plays(text: &str) -> Vec<Node> {
    let docs = yaml::parse(text, "synthetic");
    let doc = docs.into_iter().next().expect("one document");
    doc.as_seq().expect("a list of plays").to_vec()
}

#[test]
fn the_password_fetch_runs_without_escalation_and_hides_its_output() {
    // Arrange
    assert!(
        repo::path(RESOLVER_TASK).is_file(),
        "{RESOLVER_TASK} is missing"
    );
    let tasks = repo::tasks(RESOLVER_TASK);

    // Act: the fetch is the task that reads the password from the store.
    let fetch = tasks
        .iter()
        .find(|t| mentions(t, PASSWORD))
        .expect("a task fetches become_password");

    // Assert: the fetch must not itself need the escalation it enables, and its output is hidden.
    assert_eq!(
        fetch.get("become"),
        Some(&Node::Bool(false)),
        "the password fetch must not itself escalate"
    );
    assert_eq!(
        fetch.get("no_log"),
        Some(&Node::Bool(true)),
        "the password fetch must hide its output"
    );
}

#[test]
fn harden_ssh_reads_the_secret_path_the_resolver_reads() {
    // Arrange
    assert!(repo::path(HARDEN_SSH).is_file(), "{HARDEN_SSH} is missing");

    // Act
    let resolver_reads = repo::tasks(RESOLVER_TASK)
        .iter()
        .any(|t| mentions(t, SECRET_PATH));
    let harden_reads = leaf_tasks(HARDEN_SSH)
        .iter()
        .any(|t| mentions(t, SECRET_PATH));

    // Assert: a disagreement lets a host be prepared by one path and fail to harden by another.
    assert!(resolver_reads, "the resolver does not read {SECRET_PATH}");
    assert!(harden_reads, "harden-ssh.yml does not read {SECRET_PATH}");
}

#[test]
fn every_playbook_that_escalates_at_play_level_resolves_a_sudo_password() {
    // Arrange
    let files = top_level_playbooks();
    assert!(!files.is_empty(), "no top-level playbooks found");

    // Act
    let mut missing = Vec::new();
    for file in files.iter().filter(|f| f.as_str() != KNOWN_GAP) {
        for play in plays(file) {
            if escalates(&play) && !mentions_any(&play, &[RESOLVER, PASSWORD]) {
                missing.push(format!("{file}: {}", play_name(&play)));
            }
        }
    }

    // Assert
    assert!(
        missing.is_empty(),
        "these escalate at play level yet resolve no sudo credential, so they die at Gathering \
         Facts on any host that is not already hardened:\n{}",
        missing.join("\n")
    );
}

#[test]
fn fact_gathering_is_off_in_every_play_that_uses_the_resolver() {
    // Arrange: the resolver cannot run before gathering, so gathering must be off wherever the
    // resolver is included. This is the one that matters.
    let files = top_level_playbooks();

    // Act
    let mut offenders = Vec::new();
    for file in files.iter().filter(|f| f.as_str() != KNOWN_GAP) {
        for play in plays(file) {
            if escalates(&play)
                && mentions(&play, RESOLVER)
                && play.get("gather_facts") != Some(&Node::Bool(false))
            {
                offenders.push(format!("{file}: {}", play_name(&play)));
            }
        }
    }

    // Assert
    assert!(
        offenders.is_empty(),
        "these escalate at play level and still gather facts automatically, so the resolver has \
         no effect:\n{}",
        offenders.join("\n")
    );
}

#[test]
fn the_first_task_of_every_escalating_play_resolves_the_password() {
    // Arrange
    let files = top_level_playbooks();

    // Act
    let mut offenders = Vec::new();
    for file in files.iter().filter(|f| f.as_str() != KNOWN_GAP) {
        for name in escalating_plays_not_resolving_first(&plays(file)) {
            offenders.push(format!("{file}: {name}"));
        }
    }

    // Assert
    assert!(
        offenders.is_empty(),
        "the first task of these escalating plays does not resolve escalation:\n{}",
        offenders.join("\n")
    );
}

/// A localhost play that is valid, then a privileged play whose first task does not resolve. The
/// earlier play must not hide the later one.
const UNRESOLVED_SECOND_PLAY: &str = r#"
- name: localhost preamble
  hosts: localhost
  become: false
  tasks:
    - name: ok
- name: privileged service
  hosts: pbs_svc
  become: true
  gather_facts: false
  tasks:
    - name: missing resolver
"#;

/// Gathering before the resolver, even when the resolver is present and first.
const GATHERING_BEFORE_RESOLVER: &str = r#"
- name: gathering before resolver
  hosts: pbs_svc
  become: true
  gather_facts: true
  tasks:
    - ansible.builtin.include_tasks: tasks/resolve-become-password.yml
"#;

#[test]
fn the_first_task_check_rejects_a_later_play_without_the_resolver() {
    // Arrange
    let plays = synthetic_plays(UNRESOLVED_SECOND_PLAY);

    // Act
    let invalid = escalating_plays_not_resolving_first(&plays);

    // Assert
    assert_eq!(invalid, vec!["privileged service".to_owned()]);
}

#[test]
fn the_first_task_check_rejects_gathering_before_the_resolver() {
    // Arrange
    let plays = synthetic_plays(GATHERING_BEFORE_RESOLVER);

    // Act
    let invalid = escalating_plays_not_resolving_first(&plays);

    // Assert
    assert_eq!(invalid, vec!["gathering before resolver".to_owned()]);
}

#[test]
fn explicit_gathering_comes_straight_after_the_resolver() {
    // Arrange: where a play gathers explicitly, that task must come straight after the resolver.
    // A play that does not gather needs no setup task, so the check is conditional on one.
    let files = top_level_playbooks();

    // Act
    let mut offenders = Vec::new();
    for file in files.iter().filter(|f| f.as_str() != KNOWN_GAP) {
        for play in plays(file) {
            if !escalates(&play) || !has_key(&play, "ansible.builtin.setup") {
                continue;
            }
            let second = play
                .get("tasks")
                .and_then(Node::as_seq)
                .and_then(|tasks| tasks.get(1));
            if !second.is_some_and(|task| has_key(task, "ansible.builtin.setup")) {
                offenders.push(format!("{file}: {}", play_name(&play)));
            }
        }
    }

    // Assert
    assert!(
        offenders.is_empty(),
        "these gather facts but not in the task straight after the resolver:\n{}",
        offenders.join("\n")
    );
}

#[test]
fn the_resolver_guards_the_transport_before_it_fetches() {
    // Arrange: the AppRole secret_id, the returned token and the sudo password all cross the
    // connection, so the shared cleartext guard must be included, and before the lookup.
    let tasks = repo::tasks(RESOLVER_TASK);

    // Act
    let guard = tasks.iter().position(|t| mentions(t, GUARD_FILE));
    let fetch = tasks.iter().position(|t| mentions(t, PASSWORD));

    // Assert
    let guard = guard.expect("the resolver includes the transport guard");
    let fetch = fetch.expect("the resolver fetches become_password");
    assert!(
        guard < fetch,
        "the transport guard (task {guard}) must come before the fetch (task {fetch})"
    );
}

#[test]
fn the_known_gap_still_resolves_no_password() {
    // If someone fixes deploy-github-runner.yml, this must fail so the exclusion is deleted rather
    // than outliving its reason.
    let resolves =
        repo::path(KNOWN_GAP).is_file() && mentions_any(&load(KNOWN_GAP), &[RESOLVER, PASSWORD]);

    assert!(
        !resolves,
        "{KNOWN_GAP} now resolves a sudo password; remove it from the known gap"
    );
}

#[test]
fn the_transport_guard_is_a_single_assert_task() {
    // The guard runs before the password is resolved, so it must be a pure precondition check.
    // A closed allow-list: exactly one module belongs in it.
    let tasks = leaf_tasks(GUARD_TASK);

    let modules: BTreeSet<&str> = tasks.iter().flat_map(module_keys).collect();

    assert_eq!(
        modules.into_iter().collect::<Vec<_>>(),
        vec!["ansible.builtin.assert"],
        "the transport guard must contain only ansible.builtin.assert"
    );
}

/// The `ansible.builtin.` module keys of one task.
fn module_keys(task: &Node) -> Vec<&str> {
    match task {
        Node::Map(entries) => entries
            .iter()
            .filter_map(|(k, _)| k.as_str())
            .filter(|k| k.starts_with("ansible.builtin."))
            .collect(),
        _ => vec![],
    }
}

#[test]
fn the_transport_guard_task_declares_become_false() {
    // Scoped to the guard's own assert task, not the file: the file holds one task today, and the
    // point of the declaration is to survive the guard growing a task.
    let tasks = repo::tasks(GUARD_TASK);

    let guard = tasks
        .iter()
        .find(|t| task_name(t).starts_with("Require a non-cleartext transport"))
        .expect("the guard's assert task is named 'Require a non-cleartext transport'");

    assert_eq!(
        guard.get("become"),
        Some(&Node::Bool(false)),
        "the guard task must declare become: false"
    );
}

/// Every include carrying `become`, at any depth in a file. `become` is not a valid attribute on a
/// TaskInclude, so such a task fails the moment a play reaches it.
fn include_with_become(node: &Node, file: &str, out: &mut Vec<String>) {
    match node {
        Node::Seq(items) => {
            for item in items {
                let include = INCLUDE_KEYS.iter().any(|key| item.get(key).is_some());
                if include && item.get("become").is_some() {
                    out.push(format!("{file}: {}", task_name(item)));
                }
                include_with_become(item, file, out);
            }
        }
        Node::Map(entries) => {
            for (_, value) in entries {
                include_with_become(value, file, out);
            }
        }
        _ => {}
    }
}

#[test]
fn no_dynamic_include_task_carries_become() {
    // Arrange: every YAML file under the playbooks, not only the top level. Dynamic includes are
    // not parsed by --syntax-check or ansible-lint, so this is the only static proof.
    let files = repo::ls_files(&[":(glob)platform/playbooks/**/*.yml"]);
    assert!(!files.is_empty(), "no playbook YAML files found");

    // Act
    let mut offenders = Vec::new();
    for file in &files {
        let text =
            std::fs::read_to_string(repo::path(file)).unwrap_or_else(|e| panic!("{file}: {e}"));
        for doc in yaml::parse(&text, file) {
            include_with_become(&doc, file, &mut offenders);
        }
    }

    // Assert
    assert!(
        offenders.is_empty(),
        "include_tasks with a become keyword (invalid on a TaskInclude):\n{}",
        offenders.join("\n")
    );
}
