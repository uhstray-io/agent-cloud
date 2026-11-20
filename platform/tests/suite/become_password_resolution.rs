//! A sudo password is resolved before anything needs escalation (static tier).
//!
//! A play that escalates at PLAY level dies at "Gathering Facts" with ok=0 on any host that still
//! requires a sudo password. The become plugin raises before any module runs, so no per-task
//! `failed_when` can catch it. That is precisely the state of a host these playbooks exist to
//! PREPARE. Measured 2026-08-25: install-podman.yml failed that way against a freshly keyed host,
//! and of the six playbooks escalating at play level only harden-ssh.yml resolved the password.
//!
//! So the ordering is the invariant. The resolver opens the play: it is the first task, or the second
//! straight after the controller-only guard (harden-ssh.yml alone opens with a pinned block; see
//! `harden_ssh_opening`). Automatic fact gathering is off (gathering runs before tasks), and
//! gathering happens explicitly straight after. The transport guard runs before the lookup it
//! protects and needs no privilege. No dynamic include carries `become`: ansible-core refuses that
//! only when a play reaches the task, and no syntax check sees it (docs/MISTAKES.md 2.17).
//! deploy-github-runner.yml is the one named gap, because its fix belongs to work on another branch.
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
//! | become: the first task is what makes escalation possible | `the_first_task_of_every_escalating_play_resolves_the_password` (self-checks: `the_first_task_check_rejects_a_later_play_without_the_resolver`, `the_first_task_check_rejects_gathering_before_the_resolver`, `the_opening_check_accepts_only_the_resolver_or_the_guard_then_the_resolver`, `the_harden_ssh_exception_accepts_only_its_pinned_block`, `the_harden_ssh_exception_is_still_needed`, `the_controller_only_guard_runs_only_assert_and_set_fact`) |
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

use crate::credential_leaks::{INCLUDE_SHORTS, include_target, tagged_never};

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
/// A task key that loops the task: `loop`, or a `with_<lookup>`.
fn is_loop_key(key: &str) -> bool {
    key == "loop" || key.starts_with("with_")
}

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

/// The plays that escalate without a valid opening. An escalating play is valid when fact gathering
/// is explicitly off AND its opening sequence ([`opening`]) resolves the password: it opens with the
/// resolver ([`opens_with_resolver`]), or, when `harden_ssh_exception` is set (for harden-ssh.yml
/// alone), it is the pinned block ([`harden_ssh_opening`]). Anything else is refused, including a
/// harmless task before the resolver: the check accepts only the shapes it names.
fn escalating_plays_not_resolving_first(plays: &[Node], harden_ssh_exception: bool) -> Vec<String> {
    plays
        .iter()
        .filter(|play| escalates(play))
        .filter(|play| {
            let seq = opening(play);
            let resolved =
                opens_with_resolver(seq) || (harden_ssh_exception && harden_ssh_opening(seq));
            play.get("gather_facts") != Some(&Node::Bool(false)) || !resolved
        })
        .map(|play| play_name(play).to_owned())
        .collect()
}

/// The task sequence a play opens with: its `pre_tasks` when it has a non-empty one, else its
/// `tasks`. A play with non-empty `roles` and no `pre_tasks` opens with its roles, which this check
/// cannot see into, so its opening is empty, as a missing or empty sequence is; an empty opening
/// resolves nothing.
fn opening(play: &Node) -> &[Node] {
    if let Some(pre) = sequence(play, "pre_tasks") {
        return pre;
    }
    if sequence(play, "roles").is_some() {
        return &[];
    }
    sequence(play, "tasks").unwrap_or(&[])
}

/// A play's task list under `key`, when it is a non-empty list.
fn sequence<'a>(play: &'a Node, key: &str) -> Option<&'a [Node]> {
    play.get(key)
        .and_then(Node::as_seq)
        .filter(|seq| !seq.is_empty())
}

/// Whether a task may not run: it may be skipped ([`skippable`]), or it has a `loop` or `with_*` key
/// (a loop may run zero times, and its list is decided at run time). An opening include that may
/// not run leaves the resolver unproven, so it is refused.
fn may_not_run(task: &Node) -> bool {
    skippable(task) || loop_keys(task).next().is_some()
}

/// Whether a task may be skipped: a `when` of any value (a constant is no different) or a `never`
/// tag (decided by the run's `--tags`).
fn skippable(task: &Node) -> bool {
    task.get("when").is_some() || tagged_never(task)
}

/// The `loop` and `with_*` entries of a task.
fn loop_keys(task: &Node) -> impl Iterator<Item = (&str, &Node)> {
    task.as_map()
        .into_iter()
        .flatten()
        .filter_map(|(k, v)| k.as_str().filter(|k| is_loop_key(k)).map(|k| (k, v)))
}

/// Whether a task sequence opens with the resolver: its first task is the resolver include, or its
/// first task is the guard include and its second is the resolver include. Each include of that
/// opening must be able to run ([`may_not_run`]). A block is not an include, so a block is refused.
fn opens_with_resolver(seq: &[Node]) -> bool {
    match seq {
        [first, ..] if includes(first, RESOLVER_FILE) => !may_not_run(first),
        [guard, second, ..] if includes(guard, CONTROLLER_ONLY_GUARD) => {
            includes(second, RESOLVER_FILE) && !may_not_run(guard) && !may_not_run(second)
        }
        _ => false,
    }
}

/// The pinned opening of harden-ssh.yml, and of no other play. Its first task is one block, whose
/// first item is the guard include and whose second item sets the sudo password from the store
/// ([`STORE_LOOKUP`]). The guard refuses extra-var overrides before the password is fetched. The
/// block's `when`, `never` tag and loop keys are checked; its `rescue` and `always`, the store task's
/// `become` and `no_log`, the rest of the play and the names are not.
///
/// Why it exists: harden-ssh.yml runs its whole hardening in one block, so that a failure anywhere
/// is recorded as the step's result (harden-ssh.yml:51-54). The guard's loop over the play's
/// internal names (harden-ssh.yml:69-72) is required, as exactly one non-empty `loop` list: an
/// empty one runs the guard zero times. No other opening task may loop. The exception also skips
/// the resolver's transport guard; that gap is held by `platform/tests/known_unguarded_bao_plays.txt`.
///
/// Follow-up: Plane AC-998 moves harden-ssh.yml to the standard opening; delete this exception then.
fn harden_ssh_opening(seq: &[Node]) -> bool {
    let Some(block_task) = seq.first() else {
        return false;
    };
    let Some(block) = block_task.get("block").and_then(Node::as_seq) else {
        return false;
    };
    let (Some(guard), Some(store)) = (block.first(), block.get(1)) else {
        return false;
    };
    let guard_loops: Vec<_> = loop_keys(guard).collect();
    let guard_loops_over_a_list = matches!(
        guard_loops.as_slice(),
        [("loop", list)] if list.as_seq().is_some_and(|l| !l.is_empty())
    );
    !may_not_run(block_task)
        && includes(guard, CONTROLLER_ONLY_GUARD)
        && !skippable(guard)
        && guard_loops_over_a_list
        && sets_password_from_store(store)
        && !may_not_run(store)
}

/// The one include that may run before the resolver: [`opens_with_resolver`] takes it in any play,
/// [`harden_ssh_opening`] inside harden-ssh.yml's pinned block. platform/playbooks/tasks/refuse-var-overrides.yml
/// runs only assert and set_fact, which run on the controller, so nothing escalates before the
/// resolver; `the_controller_only_guard_runs_only_assert_and_set_fact` holds it to that.
const CONTROLLER_ONLY_GUARD: &str = "refuse-var-overrides.yml";

/// Whether a task is exactly an include or import of `tasks/<file>`, under any spelling, as every
/// playbook includes the guard and the resolver. A templated path (`{{ dir }}/tasks/...`) could
/// render to another file, so it is not one.
fn includes(task: &Node, file: &str) -> bool {
    include_target(task, &INCLUDE_SHORTS) == Some(format!("tasks/{file}").as_str())
}

/// A hashi_vault lookup whose path argument is [`SECRET_PATH`]: the one set_fact value that reads
/// the store's password. A literal, an extra var, or a default naming the path is not one, and
/// the trailing arguments hold no brace and no parenthesis, so a filter after the lookup's `)`
/// cannot hide in them.
static STORE_LOOKUP: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(&format!(
        r#"^\s*\{{\{{\s*lookup\(\s*['"](?:community\.hashi_vault\.)?hashi_vault['"]\s*,\s*['"]{}['"]\s*(?:,[^{{}}()]*)?\)\s*\}}\}}\s*$"#,
        regex::escape(SECRET_PATH)
    ))
    .expect("a valid regex")
});

/// Whether a task is a set_fact of `ansible_become_password` from the store lookup
/// ([`STORE_LOOKUP`]). That resolves nothing by itself: only the harden-ssh exception accepts it as
/// an opening.
fn sets_password_from_store(task: &Node) -> bool {
    module(task, "set_fact").is_some_and(|fact| {
        fact.get("ansible_become_password")
            .and_then(Node::as_str)
            .is_some_and(|value| STORE_LOOKUP.is_match(value))
    })
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
        for name in escalating_plays_not_resolving_first(&plays(file), file.as_str() == HARDEN_SSH)
        {
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
    let invalid = escalating_plays_not_resolving_first(&plays, false);

    // Assert
    assert_eq!(invalid, vec!["privileged service".to_owned()]);
}

#[test]
fn the_first_task_check_rejects_gathering_before_the_resolver() {
    // Arrange
    let plays = synthetic_plays(GATHERING_BEFORE_RESOLVER);

    // Act
    let invalid = escalating_plays_not_resolving_first(&plays, false);

    // Assert
    assert_eq!(invalid, vec!["gathering before resolver".to_owned()]);
}

/// The plays of a case table, each given the header every case shares: it escalates, and fact
/// gathering is off, so a verdict is about the opening alone. A case that lost the header would not
/// escalate and would be flagged by nothing, so [`assert_named_verdicts`] fails on it.
fn case_plays(text: &str) -> Vec<Node> {
    synthetic_plays(text)
        .into_iter()
        .map(|play| {
            let Node::Map(mut entries) = play else {
                panic!("a case play is a mapping")
            };
            entries.push((Node::Str("become".to_owned()), Node::Bool(true)));
            entries.push((Node::Str("gather_facts".to_owned()), Node::Bool(false)));
            Node::Map(entries)
        })
        .collect()
}

/// Checks a table of named plays against the plays a check flagged. Every play is named `ok ...` or
/// `bad ...`, the table holds both kinds (a table of one kind proves nothing about the other), and
/// exactly the `bad ...` plays are flagged.
fn assert_named_verdicts(plays: &[Node], invalid: &[String]) {
    let names: Vec<&str> = plays.iter().map(play_name).collect();
    assert!(
        names
            .iter()
            .all(|name| name.starts_with("ok ") || name.starts_with("bad ")),
        "every case must be named `ok ...` or `bad ...`: {names:?}"
    );
    assert!(
        names.iter().any(|name| name.starts_with("ok ")),
        "the table has no `ok ...` case, so it proves nothing about acceptance"
    );
    assert!(
        names.iter().any(|name| name.starts_with("bad ")),
        "the table has no `bad ...` case, so it proves nothing about refusal"
    );
    let expected: Vec<String> = names
        .iter()
        .filter(|name| name.starts_with("bad "))
        .map(|name| (*name).to_owned())
        .collect();
    assert_eq!(invalid, expected.as_slice());
}

/// The opening check with the harden-ssh exception off. Each play is an `ok` shape with at most one
/// change, so each `bad` play is refused for the reason its name gives. [`case_plays`] gives every
/// play its header, so the verdict is about the opening alone.
const OPENING_CASES: &str = r#"
- name: ok resolver first
  tasks:
    - ansible.builtin.include_tasks: tasks/resolve-become-password.yml
- name: ok resolver first with the always tag
  tasks:
    - ansible.builtin.include_tasks: tasks/resolve-become-password.yml
      tags: [always]
- name: ok guard then resolver
  tasks:
    - ansible.builtin.include_tasks: tasks/refuse-var-overrides.yml
    - ansible.builtin.include_tasks: tasks/resolve-become-password.yml
- name: ok resolver first in pre_tasks
  pre_tasks:
    - ansible.builtin.include_tasks: tasks/resolve-become-password.yml
  tasks:
    - ansible.builtin.debug: {msg: hi}
- name: ok guard then resolver in pre_tasks
  pre_tasks:
    - ansible.builtin.include_tasks: tasks/refuse-var-overrides.yml
    - ansible.builtin.include_tasks: tasks/resolve-become-password.yml
  tasks:
    - ansible.builtin.debug: {msg: hi}
- name: bad debug before the resolver
  tasks:
    - ansible.builtin.debug: {msg: hi}
    - ansible.builtin.include_tasks: tasks/resolve-become-password.yml
- name: bad guard then debug
  tasks:
    - ansible.builtin.include_tasks: tasks/refuse-var-overrides.yml
    - ansible.builtin.debug: {msg: hi}
- name: bad two guards then the resolver
  tasks:
    - ansible.builtin.include_tasks: tasks/refuse-var-overrides.yml
    - ansible.builtin.include_tasks: tasks/refuse-var-overrides.yml
    - ansible.builtin.include_tasks: tasks/resolve-become-password.yml
- name: bad resolver with when true
  tasks:
    - ansible.builtin.include_tasks: tasks/resolve-become-password.yml
      when: true
- name: bad resolver with when false
  tasks:
    - ansible.builtin.include_tasks: tasks/resolve-become-password.yml
      when: false
- name: bad resolver with a loop
  tasks:
    - ansible.builtin.include_tasks: tasks/resolve-become-password.yml
      loop: [1]
- name: bad resolver tagged never
  tasks:
    - ansible.builtin.include_tasks: tasks/resolve-become-password.yml
      tags: [never]
- name: bad guard with when true then the resolver
  tasks:
    - ansible.builtin.include_tasks: tasks/refuse-var-overrides.yml
      when: true
    - ansible.builtin.include_tasks: tasks/resolve-become-password.yml
- name: bad resolver inside a block
  tasks:
    - block:
        - ansible.builtin.include_tasks: tasks/resolve-become-password.yml
- name: bad resolver under tasks while pre_tasks opens with a debug
  pre_tasks:
    - ansible.builtin.debug: {msg: hi}
  tasks:
    - ansible.builtin.include_tasks: tasks/resolve-become-password.yml
- name: bad resolver in tasks with roles and no pre_tasks
  roles: [something]
  tasks:
    - ansible.builtin.include_tasks: tasks/resolve-become-password.yml
- name: bad templated resolver path
  tasks:
    - ansible.builtin.include_tasks: "{{ _d }}/resolve-become-password.yml"
- name: bad templated resolver path under tasks
  tasks:
    - ansible.builtin.include_tasks: "{{ _d }}/tasks/resolve-become-password.yml"
- name: bad templated guard path then the resolver
  tasks:
    - ansible.builtin.include_tasks: "{{ _d }}/tasks/refuse-var-overrides.yml"
    - ansible.builtin.include_tasks: tasks/resolve-become-password.yml
- name: bad store lookup set_fact as the first task
  tasks:
    - ansible.builtin.set_fact:
        ansible_become_password: "{{ lookup('community.hashi_vault.hashi_vault', 'secret/data/services/ssh:become_password') }}"
- name: ok empty pre_tasks then the resolver in tasks
  pre_tasks: []
  tasks:
    - ansible.builtin.include_tasks: tasks/resolve-become-password.yml
- name: bad guard with a loop then the resolver
  tasks:
    - ansible.builtin.include_tasks: tasks/refuse-var-overrides.yml
      loop: [a]
      loop_control:
        loop_var: _rvo_name
    - ansible.builtin.include_tasks: tasks/resolve-become-password.yml
- name: bad guard tagged never then the resolver
  tasks:
    - ansible.builtin.include_tasks: tasks/refuse-var-overrides.yml
      tags: [never]
    - ansible.builtin.include_tasks: tasks/resolve-become-password.yml
- name: bad guard then the resolver with when true
  tasks:
    - ansible.builtin.include_tasks: tasks/refuse-var-overrides.yml
    - ansible.builtin.include_tasks: tasks/resolve-become-password.yml
      when: true
"#;

#[test]
fn the_opening_check_accepts_only_the_resolver_or_the_guard_then_the_resolver() {
    let pinned = pinned_play(
        "bad harden-ssh block shape with the exception off",
        PINNED_LOOP,
        STORE_VALUE,
    );
    let plays = case_plays(&format!("{OPENING_CASES}{pinned}"));
    let invalid = escalating_plays_not_resolving_first(&plays, false);
    assert_named_verdicts(&plays, &invalid);
}

/// The harden-ssh exception with it on. Each play is the pinned block with one change, except where
/// a comment says why a row needs a second. The test adds [`GUARD_LOOP_REFUSALS`] and
/// [`STORE_LOOKUP_REFUSALS`].
const HARDEN_SSH_CASES: &str = r#"
- name: ok pinned harden-ssh opening
  tasks:
    - name: Harden and verify, recording any failure
      block:
        - name: Refuse extra-var overrides of this play's internal names
          ansible.builtin.include_tasks: tasks/refuse-var-overrides.yml
          loop: [a]
          loop_control:
            loop_var: _rvo_name
        - name: Fetch sudo (become) password from OpenBao
          ansible.builtin.set_fact:
            ansible_become_password: "{{ lookup('community.hashi_vault.hashi_vault', 'secret/data/services/ssh:become_password') }}"
          become: false
          no_log: true
      rescue:
        - ansible.builtin.debug: {msg: hi}
- name: bad harden-ssh opening without the guard
  tasks:
    - block:
        - ansible.builtin.set_fact:
            ansible_become_password: "{{ lookup('community.hashi_vault.hashi_vault', 'secret/data/services/ssh:become_password') }}"
          become: false
          no_log: true
      rescue:
        - ansible.builtin.debug: {msg: hi}
- name: bad harden-ssh opening with the password before the guard
  tasks:
    - block:
        - ansible.builtin.set_fact:
            ansible_become_password: "{{ lookup('community.hashi_vault.hashi_vault', 'secret/data/services/ssh:become_password') }}"
          become: false
          no_log: true
        - ansible.builtin.include_tasks: tasks/refuse-var-overrides.yml
          loop: [a]
          loop_control:
            loop_var: _rvo_name
      rescue:
        - ansible.builtin.debug: {msg: hi}
- name: bad harden-ssh opening with a literal password
  tasks:
    - block:
        - ansible.builtin.include_tasks: tasks/refuse-var-overrides.yml
          loop: [a]
          loop_control:
            loop_var: _rvo_name
        - ansible.builtin.set_fact:
            ansible_become_password: x
          become: false
          no_log: true
      rescue:
        - ansible.builtin.debug: {msg: hi}
- name: bad harden-ssh opening whose block has a when
  tasks:
    - when: true
      block:
        - ansible.builtin.include_tasks: tasks/refuse-var-overrides.yml
          loop: [a]
          loop_control:
            loop_var: _rvo_name
        - ansible.builtin.set_fact:
            ansible_become_password: "{{ lookup('community.hashi_vault.hashi_vault', 'secret/data/services/ssh:become_password') }}"
          become: false
          no_log: true
      rescue:
        - ansible.builtin.debug: {msg: hi}
- name: bad harden-ssh opening whose block is tagged never
  tasks:
    - tags: [never]
      block:
        - ansible.builtin.include_tasks: tasks/refuse-var-overrides.yml
          loop: [a]
          loop_control:
            loop_var: _rvo_name
        - ansible.builtin.set_fact:
            ansible_become_password: "{{ lookup('community.hashi_vault.hashi_vault', 'secret/data/services/ssh:become_password') }}"
          become: false
          no_log: true
      rescue:
        - ansible.builtin.debug: {msg: hi}
- name: bad harden-ssh opening whose block loops
  tasks:
    - loop: [1]
      block:
        - ansible.builtin.include_tasks: tasks/refuse-var-overrides.yml
          loop: [a]
          loop_control:
            loop_var: _rvo_name
        - ansible.builtin.set_fact:
            ansible_become_password: "{{ lookup('community.hashi_vault.hashi_vault', 'secret/data/services/ssh:become_password') }}"
          become: false
          no_log: true
      rescue:
        - ansible.builtin.debug: {msg: hi}
- name: bad harden-ssh opening whose guard has a when
  tasks:
    - block:
        - ansible.builtin.include_tasks: tasks/refuse-var-overrides.yml
          loop: [a]
          loop_control:
            loop_var: _rvo_name
          when: true
        - ansible.builtin.set_fact:
            ansible_become_password: "{{ lookup('community.hashi_vault.hashi_vault', 'secret/data/services/ssh:become_password') }}"
          become: false
          no_log: true
      rescue:
        - ansible.builtin.debug: {msg: hi}
- name: bad harden-ssh opening whose guard is tagged never
  tasks:
    - block:
        - ansible.builtin.include_tasks: tasks/refuse-var-overrides.yml
          loop: [a]
          loop_control:
            loop_var: _rvo_name
          tags: [never]
        - ansible.builtin.set_fact:
            ansible_become_password: "{{ lookup('community.hashi_vault.hashi_vault', 'secret/data/services/ssh:become_password') }}"
          become: false
          no_log: true
      rescue:
        - ansible.builtin.debug: {msg: hi}
- name: bad harden-ssh opening whose guard path is templated
  tasks:
    - block:
        - ansible.builtin.include_tasks: "{{ _d }}/tasks/refuse-var-overrides.yml"
          loop: [a]
          loop_control:
            loop_var: _rvo_name
        - ansible.builtin.set_fact:
            ansible_become_password: "{{ lookup('community.hashi_vault.hashi_vault', 'secret/data/services/ssh:become_password') }}"
          become: false
          no_log: true
      rescue:
        - ansible.builtin.debug: {msg: hi}
- name: bad harden-ssh opening whose password set_fact has a when
  tasks:
    - block:
        - ansible.builtin.include_tasks: tasks/refuse-var-overrides.yml
          loop: [a]
          loop_control:
            loop_var: _rvo_name
        - ansible.builtin.set_fact:
            ansible_become_password: "{{ lookup('community.hashi_vault.hashi_vault', 'secret/data/services/ssh:become_password') }}"
          become: false
          no_log: true
          when: true
      rescue:
        - ansible.builtin.debug: {msg: hi}
- name: bad harden-ssh opening whose password set_fact is tagged never
  tasks:
    - block:
        - ansible.builtin.include_tasks: tasks/refuse-var-overrides.yml
          loop: [a]
          loop_control:
            loop_var: _rvo_name
        - ansible.builtin.set_fact:
            ansible_become_password: "{{ lookup('community.hashi_vault.hashi_vault', 'secret/data/services/ssh:become_password') }}"
          become: false
          no_log: true
          tags: [never]
      rescue:
        - ansible.builtin.debug: {msg: hi}
- name: bad harden-ssh opening whose password set_fact loops
  tasks:
    - block:
        - ansible.builtin.include_tasks: tasks/refuse-var-overrides.yml
          loop: [a]
          loop_control:
            loop_var: _rvo_name
        - ansible.builtin.set_fact:
            ansible_become_password: "{{ lookup('community.hashi_vault.hashi_vault', 'secret/data/services/ssh:become_password') }}"
          become: false
          no_log: true
          loop: [1]
      rescue:
        - ansible.builtin.debug: {msg: hi}
- name: bad harden-ssh opening behind a debug
  tasks:
    - ansible.builtin.debug: {msg: hi}
    - block:
        - ansible.builtin.include_tasks: tasks/refuse-var-overrides.yml
          loop: [a]
          loop_control:
            loop_var: _rvo_name
        - ansible.builtin.set_fact:
            ansible_become_password: "{{ lookup('community.hashi_vault.hashi_vault', 'secret/data/services/ssh:become_password') }}"
          become: false
          no_log: true
      rescue:
        - ansible.builtin.debug: {msg: hi}
- name: bad harden-ssh opening with a task between the guard and the password
  tasks:
    - block:
        - ansible.builtin.include_tasks: tasks/refuse-var-overrides.yml
          loop: [a]
          loop_control:
            loop_var: _rvo_name
        - ansible.builtin.debug: {msg: hi}
        - ansible.builtin.set_fact:
            ansible_become_password: "{{ lookup('community.hashi_vault.hashi_vault', 'secret/data/services/ssh:become_password') }}"
          become: false
          no_log: true
      rescue:
        - ansible.builtin.debug: {msg: hi}
# The leading debug keeps the password at block[1], so only the guard's place differs: without
# it, a check that looked for the guard anywhere in the block would still pass this table.
- name: bad harden-ssh opening with the guard after the password
  tasks:
    - block:
        - ansible.builtin.debug: {msg: hi}
        - ansible.builtin.set_fact:
            ansible_become_password: "{{ lookup('community.hashi_vault.hashi_vault', 'secret/data/services/ssh:become_password') }}"
          become: false
          no_log: true
        - ansible.builtin.include_tasks: tasks/refuse-var-overrides.yml
          loop: [a]
          loop_control:
            loop_var: _rvo_name
      rescue:
        - ansible.builtin.debug: {msg: hi}
"#;

/// harden-ssh.yml's pinned opening block as a case play, with the guard's loop line and the password
/// value given.
fn pinned_play(name: &str, guard_loop: &str, password: &str) -> String {
    format!(
        r#"
- name: {name}
  tasks:
    - block:
        - ansible.builtin.include_tasks: tasks/refuse-var-overrides.yml
          {guard_loop}
          loop_control:
            loop_var: _rvo_name
        - ansible.builtin.set_fact:
            ansible_become_password: "{password}"
          become: false
          no_log: true
      rescue:
        - ansible.builtin.debug: {{msg: hi}}
"#
    )
}

/// The guard's loop line in the pinned block.
const PINNED_LOOP: &str = "loop: [a]";

/// The guard-loop refusals: the pinned block with only the guard's loop line changed (a comment line
/// drops the loop), each refused for that line alone. Named `bad harden-ssh opening whose <row name>`.
const GUARD_LOOP_REFUSALS: &[(&str, &str)] = &[
    ("guard loop is empty", "loop: []"),
    ("guard loops with with_items", "with_items: [a]"),
    ("guard has no loop", "# no loop"),
    (
        "guard has two loop keys",
        "loop: [a]\n          with_items: [a]",
    ),
    ("guard loop is templated", "loop: \"{{ _names }}\""),
];

/// The store lookup the pinned block reads the password with.
const STORE_VALUE: &str = r#"{{ lookup('community.hashi_vault.hashi_vault', 'secret/data/services/ssh:become_password') }}"#;

/// The store-lookup refusals (docs/LESSONS.md L-010): the pinned block with only the password value
/// changed, each refused for that value alone. Named `bad harden-ssh opening whose <row name>`.
const STORE_LOOKUP_REFUSALS: &[(&str, &str)] = &[
    (
        "password default names the store path",
        r#"{{ lookup('env', 'X') | default('secret/data/services/ssh:become_password') }}"#,
    ),
    (
        "password filter follows the lookup",
        r#"{{ lookup('hashi_vault', 'secret/data/services/ssh:become_password', url=u) | default('x', true) }}"#,
    ),
    (
        "lookup argument opens a brace",
        r#"{{ lookup('hashi_vault', 'secret/data/services/ssh:become_password', url='{') }}"#,
    ),
    (
        "lookup argument closes a brace",
        r#"{{ lookup('hashi_vault', 'secret/data/services/ssh:become_password', url='}') }}"#,
    ),
    (
        "lookup argument closes a call",
        r#"{{ lookup('hashi_vault', 'secret/data/services/ssh:become_password', url=')') }}"#,
    ),
    (
        "lookup argument opens a call",
        r#"{{ lookup('hashi_vault', 'secret/data/services/ssh:become_password', x='(') }}"#,
    ),
    (
        "literal precedes the lookup",
        r#"x {{ lookup('community.hashi_vault.hashi_vault', 'secret/data/services/ssh:become_password') }}"#,
    ),
    (
        "store path is concatenated in the lookup",
        r#"{{ lookup('community.hashi_vault.hashi_vault', 'secret/data/services/ssh:become_password' ~ 'x') }}"#,
    ),
    (
        "lookup sits in a Jinja comment",
        r#"{# {{ lookup('community.hashi_vault.hashi_vault', 'secret/data/services/ssh:become_password') }} #}"#,
    ),
];

#[test]
fn the_harden_ssh_exception_accepts_only_its_pinned_block() {
    let guard_loops = GUARD_LOOP_REFUSALS
        .iter()
        .map(|(name, guard_loop)| (name, *guard_loop, STORE_VALUE));
    let values = STORE_LOOKUP_REFUSALS
        .iter()
        .map(|(name, value)| (name, PINNED_LOOP, *value));
    let refusals: String = guard_loops
        .chain(values)
        .map(|(name, guard_loop, value)| {
            pinned_play(
                &format!("bad harden-ssh opening whose {name}"),
                guard_loop,
                value,
            )
        })
        .collect();
    let plays = case_plays(&format!("{HARDEN_SSH_CASES}{refusals}"));
    let invalid = escalating_plays_not_resolving_first(&plays, true);
    assert_named_verdicts(&plays, &invalid);
}

#[test]
fn the_harden_ssh_exception_is_still_needed() {
    // Arrange
    assert!(repo::path(HARDEN_SSH).is_file(), "{HARDEN_SSH} is missing");
    let harden = plays(HARDEN_SSH);

    // Assert: refused without the exception, or the exception outlives the shape it pins; accepted
    // with it, or the exception no longer matches the file it was written for.
    assert!(
        !escalating_plays_not_resolving_first(&harden, false).is_empty(),
        "{HARDEN_SSH} now opens with the standard shape; delete the harden-ssh exception (Plane AC-998)"
    );
    assert!(
        escalating_plays_not_resolving_first(&harden, true).is_empty(),
        "{HARDEN_SSH} no longer matches the pinned block that the harden-ssh exception accepts"
    );
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
            .filter(|k| !TASK_KEYWORDS.contains(k) && !is_loop_key(k))
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
