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
//! | become: the first task is what makes escalation possible | `the_first_task_of_every_escalating_play_resolves_the_password`, with the two self-checks `the_first_task_check_rejects_a_later_play_without_the_resolver` and `the_first_task_check_rejects_gathering_before_the_resolver`, and the self-check `the_first_task_check_follows_pre_tasks_roles_blocks_and_the_guard`, and `the_controller_only_guard_runs_only_assert_and_set_fact` |
//! | become: gathering, where it happens at all, happens after escalation is possible | `explicit_gathering_comes_straight_after_the_resolver` |
//! | become: the resolver guards the transport it sends the credential over | `the_resolver_guards_the_transport_before_it_fetches` |
//! | become: the known gap is still the only one, and is named | `the_known_gap_still_resolves_no_password` |
//! | become: the transport guard needs no privilege, and says so | `the_transport_guard_is_a_single_assert_task`, with `the_transport_guard_task_declares_become_false` |
//! | become: no dynamic include anywhere carries a become keyword | `no_dynamic_include_task_carries_become` |

use std::collections::BTreeSet;
use std::sync::LazyLock;

use agent_cloud_tests::repo;
use agent_cloud_tests::yaml::{self, Node};
use regex::Regex;

const RESOLVER_TASK: &str = "platform/playbooks/tasks/resolve-become-password.yml";
const GUARD_TASK: &str = "platform/playbooks/tasks/assert-bao-transport.yml";
const HARDEN_SSH: &str = "platform/playbooks/harden-ssh.yml";
const KNOWN_GAP: &str = "platform/playbooks/deploy-github-runner.yml";
const SECRET_PATH: &str = "secret/data/services/ssh:become_password";
const RESOLVER: &str = "resolve-become-password";
const RESOLVER_FILE: &str = "resolve-become-password.yml";
const GUARD_FILE: &str = "assert-bao-transport.yml";
const PASSWORD: &str = "become_password";
/// The stem every task that touches the sudo password shares: the store path `ssh:become_password`,
/// the play variable `ansible_become_password`, and the alias `ansible_become_pass`. A match on the
/// full `become_password` would let a task written under the alias fall out of scope.
const PASSWORD_STEM: &str = "become_pass";
/// Ansible's truthy strings for a bool field, lowercased and stripped, as `boolean(value,
/// strict=True)` reads them: `BOOLEANS_TRUE` in `ansible/module_utils/parsing/convert_bool.py:12`
/// (ansible-core 2.20.8), applied to `become`, which is `isa='bool'` at `ansible/playbook/base.py:716`
/// and coerced at `base.py:469-470`. The numeric members `1` and `1.0` reach this parser as strings,
/// since it has no int or float node, so both are listed here.
const BECOME_TRUE: &[&str] = &["y", "yes", "on", "1", "1.0", "true", "t"];
/// Ansible's task keywords: the keys of a task that are not a module call. Any other key of a task
/// names the module it calls. `action` and `local_action` are left out on purpose: they name a
/// module as their value, so they count as calls. `with_*` loop keys are matched by prefix.
const TASK_KEYWORDS: &[&str] = &[
    "name",
    "args",
    "any_errors_fatal",
    "async",
    "become",
    "become_exe",
    "become_flags",
    "become_method",
    "become_user",
    "block",
    "always",
    "changed_when",
    "check_mode",
    "collections",
    "connection",
    "debugger",
    "delay",
    "delegate_facts",
    "delegate_to",
    "diff",
    "environment",
    "failed_when",
    "ignore_errors",
    "ignore_unreachable",
    "listen",
    "loop",
    "loop_control",
    "module_defaults",
    "no_log",
    "notify",
    "poll",
    "port",
    "register",
    "remote_user",
    "rescue",
    "retries",
    "run_once",
    "tags",
    "throttle",
    "timeout",
    "until",
    "vars",
    "when",
];

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

fn mentions(node: &Node, needle: &str) -> bool {
    any_string(node, &|s: &str| s.contains(needle))
}

fn mentions_any(node: &Node, needles: &[&str]) -> bool {
    needles.iter().any(|needle| mentions(node, needle))
}

/// A task's module under any of the three spellings Ansible accepts for a module key: the short
/// name, `ansible.builtin.<short>`, or `ansible.legacy.<short>` (ansible/utils/fqcn.py). A check
/// that reads one spelling alone is blind to a task written under another, so every module read
/// goes through here.
fn module<'a>(task: &'a Node, short: &str) -> Option<&'a Node> {
    [
        short.to_owned(),
        format!("ansible.builtin.{short}"),
        format!("ansible.legacy.{short}"),
    ]
    .iter()
    .find_map(|key| task.get(key))
}

/// Whether a key spells the module `short`, under any of the three spellings.
fn is_spelling(key: &str, short: &str) -> bool {
    key == short
        || key.strip_prefix("ansible.builtin.") == Some(short)
        || key.strip_prefix("ansible.legacy.") == Some(short)
}

/// Whether a module is called anywhere in a node, under any spelling of its key.
fn has_module(node: &Node, short: &str) -> bool {
    match node {
        Node::Map(entries) => entries.iter().any(|(k, v)| {
            k.as_str().is_some_and(|k| is_spelling(k, short)) || has_module(v, short)
        }),
        Node::Seq(items) => items.iter().any(|item| has_module(item, short)),
        Node::Str(_) | Node::Bool(_) | Node::Null => false,
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

/// Whether a play escalates. Ansible coerces `become` to a bool, so `"true"`, `yes`, `1` and `on`
/// escalate as `true` does; a bool-only comparison let every one of them through unchecked. A
/// templated value cannot be read without rendering it, so it counts as escalating: a play counted
/// in error must resolve the password, which costs it nothing.
fn escalates(play: &Node) -> bool {
    match play.get("become") {
        Some(Node::Bool(on)) => *on,
        Some(Node::Str(s)) if s.contains("{{") => true,
        Some(Node::Str(s)) => BECOME_TRUE.contains(&s.trim().to_lowercase().as_str()),
        _ => false,
    }
}

/// The check the BATS `invalid_become_plays` made. An escalating play is invalid unless fact
/// gathering is explicitly off AND the first task it actually runs ([`first_executed_task`])
/// resolves the password ([`resolves`]). A task that merely names the resolver does not count.
fn escalating_plays_not_resolving_first(plays: &[Node]) -> Vec<String> {
    plays
        .iter()
        .filter(|play| escalates(play))
        .filter(|play| {
            let resolved = first_executed_task(play).is_some_and(resolves);
            play.get("gather_facts") != Some(&Node::Bool(false)) || !resolved
        })
        .map(|play| play_name(play).to_owned())
        .collect()
}

/// The one include allowed to run before the resolver. platform/playbooks/tasks/refuse-var-overrides.yml
/// runs only assert and set_fact, which run on the controller, so nothing escalates before the
/// resolver; `the_controller_only_guard_runs_only_assert_and_set_fact` holds it to that. harden-ssh.yml
/// is the case: its block refuses overrides first, then sets the password.
const CONTROLLER_ONLY_GUARD: &str = "refuse-var-overrides.yml";

/// The file an include_tasks or import_tasks names, under any spelling, as a string or a `file:`.
fn included_file(task: &Node) -> Option<&str> {
    ["include_tasks", "import_tasks"]
        .iter()
        .filter_map(|m| module(task, m))
        .find_map(|v| match v {
            Node::Str(s) => Some(s.as_str()),
            Node::Map(_) => v.get("file").and_then(Node::as_str),
            _ => None,
        })
}

/// Exactly `tasks/<guard>`, as every playbook includes it: a templated path (`{{ dir }}/tasks/...`)
/// could render to another file, so it is not the guard.
fn is_controller_only_guard(task: &Node) -> bool {
    included_file(task) == Some(format!("tasks/{CONTROLLER_ONLY_GUARD}").as_str())
}

/// The first task a play actually runs. `pre_tasks` run before `tasks`, and so do `roles`, which
/// this check cannot see into, so a play with roles has none. A block is entered at its first
/// item, and a leading controller-only guard include is skipped. A task or block that never runs
/// (Ansible applies a block's `when` and `tags` to everything in it) is skipped too.
fn first_executed_task(play: &Node) -> Option<&Node> {
    let non_empty = |key: &str| {
        play.get(key)
            .and_then(Node::as_seq)
            .filter(|s| !s.is_empty())
    };
    if let Some(pre) = non_empty("pre_tasks") {
        return first_in(pre);
    }
    if non_empty("roles").is_some() {
        return None;
    }
    first_in(play.get("tasks").and_then(Node::as_seq)?)
}

fn first_in(seq: &[Node]) -> Option<&Node> {
    for task in seq {
        if is_controller_only_guard(task) || crate::credential_leaks::never_runs(task) {
            continue;
        }
        // A block whose every task is skipped runs nothing: the next sibling runs first.
        match task.get("block").and_then(Node::as_seq) {
            Some(block) => match first_in(block) {
                Some(first) => return Some(first),
                None => continue,
            },
            None => return Some(task),
        }
    }
    None
}

/// A hashi_vault lookup whose path argument is [`SECRET_PATH`]: the one set_fact value that reads
/// the store's password. A literal, an extra var, or a default naming the path is not one.
static STORE_LOOKUP: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(&format!(
        r#"hashi_vault['"]\s*,\s*['"]{}['"]"#,
        regex::escape(SECRET_PATH)
    ))
    .expect("a valid regex")
});

/// Whether a task makes escalation possible: an include or import of exactly
/// `tasks/resolve-become-password.yml` (a templated path could render to another file), or a
/// set_fact of `ansible_become_password` from the store lookup ([`STORE_LOOKUP`]).
fn resolves(task: &Node) -> bool {
    let sets = module(task, "set_fact").is_some_and(|f| {
        f.get("ansible_become_password")
            .and_then(Node::as_str)
            .is_some_and(|v| STORE_LOOKUP.is_match(v))
    });
    included_file(task) == Some(format!("tasks/{RESOLVER_FILE}").as_str()) || sets
}

#[test]
fn the_controller_only_guard_runs_only_assert_and_set_fact() {
    // The exemption above holds only while the guard escalates nothing: any module but assert and
    // set_fact (under any spelling) could need the privilege the resolver has not provided yet.
    let file = format!("platform/playbooks/tasks/{CONTROLLER_ONLY_GUARD}");
    let tasks = leaf_tasks(&file);
    assert!(!tasks.is_empty(), "{file} has no tasks");
    let mut other = Vec::new();
    for task in &tasks {
        let calls = called_modules(task);
        if calls.is_empty() {
            other.push(format!("{:?} calls no module", task_name(task)));
        }
        for call in calls
            .into_iter()
            .filter(|m| m != "assert" && m != "set_fact")
        {
            other.push(format!("{:?} calls {call}", task_name(task)));
        }
    }
    assert!(
        other.is_empty(),
        "{file} must run only assert and set_fact to stay exempt before the resolver: {other:?}"
    );
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

    // Every task that touches the password hides its output, not only the first one. The set_fact
    // that puts it into the play's variables is the one a first-task check cannot see.
    let touching: Vec<&Node> = tasks
        .iter()
        .filter(|t| mentions(t, PASSWORD_STEM))
        .collect();
    assert!(
        touching.len() >= 2,
        "the fetch and the task that uses it both touch the password, but {} matched: a rename \
         would shrink this check to nothing",
        touching.len()
    );
    for task in touching {
        assert_eq!(
            task.get("no_log"),
            Some(&Node::Bool(true)),
            "task {:?} touches the sudo password and must hide its output",
            task_name(task)
        );
    }
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

/// Each play names its expected verdict: `ok ...` must pass, `bad ...` must be flagged.
const FIRST_EXECUTED_CASES: &str = r#"
- name: bad pre_tasks before the resolver
  hosts: h
  become: true
  gather_facts: false
  pre_tasks:
    - ansible.builtin.debug: {msg: hi}
  tasks:
    - ansible.builtin.include_tasks: tasks/resolve-become-password.yml
- name: bad roles before the resolver
  hosts: h
  become: true
  gather_facts: false
  roles: [something]
  tasks:
    - ansible.builtin.include_tasks: tasks/resolve-become-password.yml
- name: bad debug naming the resolver
  hosts: h
  become: true
  gather_facts: false
  tasks:
    - ansible.builtin.debug: {msg: resolve-become-password.yml}
- name: ok resolver in pre_tasks
  hosts: h
  become: true
  gather_facts: false
  pre_tasks:
    - ansible.builtin.include_tasks: tasks/resolve-become-password.yml
  tasks:
    - ansible.builtin.debug: {msg: hi}
- name: ok guard then set_fact inside a block
  hosts: h
  become: true
  gather_facts: false
  tasks:
    - block:
        - ansible.builtin.include_tasks: tasks/refuse-var-overrides.yml
        - ansible.builtin.set_fact:
            ansible_become_password: "{{ lookup('community.hashi_vault.hashi_vault', 'secret/data/services/ssh:become_password') }}"
- name: bad set_fact of a literal password
  hosts: h
  become: true
  gather_facts: false
  tasks:
    - ansible.builtin.set_fact: {ansible_become_password: x}
- name: bad set_fact of a default naming the store path
  hosts: h
  become: true
  gather_facts: false
  tasks:
    - ansible.builtin.set_fact:
        ansible_become_password: "{{ lookup('env', 'X') | default('secret/data/services/ssh:become_password') }}"
- name: bad templated resolver path
  hosts: h
  become: true
  gather_facts: false
  tasks:
    - ansible.builtin.include_tasks: "{{ _d }}/resolve-become-password.yml"
- name: bad templated guard path, then the resolver
  hosts: h
  become: true
  gather_facts: false
  tasks:
    - ansible.builtin.include_tasks: "{{ _d }}/tasks/refuse-var-overrides.yml"
    - ansible.builtin.include_tasks: tasks/resolve-become-password.yml
- name: bad debug then resolver inside a block
  hosts: h
  become: true
  gather_facts: false
  tasks:
    - block:
        - ansible.builtin.debug: {msg: hi}
        - ansible.builtin.include_tasks: tasks/resolve-become-password.yml
- name: bad resolver that never runs
  hosts: h
  become: true
  gather_facts: false
  tasks:
    - ansible.builtin.include_tasks: tasks/resolve-become-password.yml
      when: false
- name: bad resolver tagged never
  hosts: h
  become: true
  gather_facts: false
  tasks:
    - ansible.builtin.include_tasks: tasks/resolve-become-password.yml
      tags: [never]
- name: bad resolver in a block that never runs
  hosts: h
  become: true
  gather_facts: false
  tasks:
    - block:
        - ansible.builtin.include_tasks: tasks/resolve-become-password.yml
      when: false
- name: bad resolver in a block tagged never
  hosts: h
  become: true
  gather_facts: false
  tasks:
    - block:
        - ansible.builtin.include_tasks: tasks/resolve-become-password.yml
      tags: [never]
- name: ok a block of only a skipped task, then the resolver
  hosts: h
  become: true
  gather_facts: false
  tasks:
    - block:
        - ansible.builtin.debug: {msg: hi}
          when: false
    - ansible.builtin.include_tasks: tasks/resolve-become-password.yml
- name: bad guard then debug
  hosts: h
  become: true
  gather_facts: false
  tasks:
    - ansible.builtin.include_tasks: tasks/refuse-var-overrides.yml
    - ansible.builtin.debug: {msg: hi}
"#;

#[test]
fn the_first_task_check_follows_pre_tasks_roles_blocks_and_the_guard() {
    let plays = synthetic_plays(FIRST_EXECUTED_CASES);
    let invalid = escalating_plays_not_resolving_first(&plays);
    let expected: Vec<String> = plays
        .iter()
        .map(|p| play_name(p).to_owned())
        .filter(|n| n.starts_with("bad "))
        .collect();
    assert_eq!(invalid, expected);
}

#[test]
fn explicit_gathering_comes_straight_after_the_resolver() {
    // Arrange: where a play gathers explicitly, that task must come straight after the resolver.
    // A play that does not gather needs no setup task, so the check is conditional on one.
    let files = top_level_playbooks();

    // Act
    let mut offenders = Vec::new();
    let mut gathering_plays = 0;
    for file in files.iter().filter(|f| f.as_str() != KNOWN_GAP) {
        for play in plays(file) {
            if !escalates(&play) || !has_module(&play, "setup") {
                continue;
            }
            gathering_plays += 1;
            let second = play
                .get("tasks")
                .and_then(Node::as_seq)
                .and_then(|tasks| tasks.get(1));
            if second.is_none_or(|task| module(task, "setup").is_none()) {
                offenders.push(format!("{file}: {}", play_name(&play)));
            }
        }
    }

    // Assert: the check is conditional on gathering, so it must find some gathering play, or it
    // passes for nothing.
    assert!(
        gathering_plays > 0,
        "no escalating play gathers facts, so the check would pass for nothing"
    );
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

    let called: BTreeSet<String> = tasks.iter().flat_map(called_modules).collect();

    assert_eq!(
        called.into_iter().collect::<Vec<_>>(),
        vec!["assert".to_owned()],
        "the transport guard must call only assert, under any spelling"
    );
}

/// The modules a task calls: every key that is not a task keyword, with its `ansible.builtin.` or
/// `ansible.legacy.` prefix removed. A key is a call whatever the module is, so a `ping:` or an
/// `ansible.legacy.command:` added to the guard shows up here.
fn called_modules(task: &Node) -> Vec<String> {
    match task {
        Node::Map(entries) => entries
            .iter()
            .filter_map(|(k, _)| k.as_str())
            .filter(|k| !TASK_KEYWORDS.contains(k) && !k.starts_with("with_"))
            .map(|k| {
                k.strip_prefix("ansible.builtin.")
                    .or_else(|| k.strip_prefix("ansible.legacy."))
                    .unwrap_or(k)
                    .to_owned()
            })
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
                let include = module(item, "include_tasks").is_some();
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
