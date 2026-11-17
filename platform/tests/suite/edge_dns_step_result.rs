//! The static tests of the edge-dns step result (plan 24, Phase 4): the four that read the Cloudflare
//! apply playbook as YAML and check its tofu commands, its visible report and its saved-plan
//! handling by their text.
//!
//! Replaces four tests of `platform/tests/test_edge_dns_step_result.py`. The rest of that file runs
//! the tasks through ansible-playbook with a fake tofu and stays in Python.
//!
//! The playbook is read the way `playbook_yaml.plays` reads it: the leading extra-var guard import
//! is skipped, and exactly one play must remain. `all_tasks` is the Python `_all_tasks`, which walks
//! `block`, `rescue` and `always` below each task. The report and saved-plan tests read top-level
//! tasks only, as the Python `_tasks()` did.
//!
//! Port map (Python test → Rust fn):
//!
//! | Python test | Rust fn |
//! |---|---|
//! | `test_both_plans_detect_changes_by_exit_code` | `both_plans_detect_changes_by_exit_code` |
//! | `test_every_credential_bearing_tofu_command_is_no_log` | `every_credential_bearing_tofu_command_is_no_log` |
//! | `test_the_visible_report_carries_only_exit_codes_and_counts` | `the_visible_report_carries_only_exit_codes_and_counts` |
//! | `test_saved_plans_are_removed_and_shown_inside_the_no_log_boundary` | `saved_plans_are_removed_and_shown_inside_the_no_log_boundary` |

use std::collections::BTreeSet;

use agent_cloud_tests::repo;
use agent_cloud_tests::yaml::{self, Node};

const PLAYBOOK: &str = "platform/playbooks/apply-cloudflare-tofu.yml";
/// Every workflow executor opens with this import (playbook_yaml.OVERRIDE_GUARD).
const OVERRIDE_GUARD: &str = "refuse-internal-extra-vars.yml";
const DRY_RUN: &str = "Dry run: OpenTofu init, plan and show in a throwaway data directory";
/// Names that must never reach the visible report: the protected registers and the credentials.
const PROTECTED: [&str; 7] = [
    "_tf_plan",
    "_tf_apply",
    "_tf_verify",
    "_tf_init",
    "_tf_env",
    "_tf_show",
    "_cf",
];

/// The playbook's one play's tasks, the leading guard import skipped (playbook_yaml.plays).
fn play_tasks() -> Vec<Node> {
    let doc = yaml::load_one(&repo::path(PLAYBOOK)).expect("the apply playbook is empty");
    let mut docs = doc
        .as_seq()
        .expect("the playbook is a list of plays")
        .to_vec();
    if docs
        .first()
        .and_then(|d| d.get("ansible.builtin.import_playbook"))
        .and_then(Node::as_str)
        == Some(OVERRIDE_GUARD)
    {
        docs.remove(0);
    }
    assert_eq!(docs.len(), 1, "{PLAYBOOK}: expected exactly one play");
    docs[0]
        .get("tasks")
        .and_then(Node::as_seq)
        .expect("the play's tasks")
        .to_vec()
}

/// The Python `_all_tasks`: each task, then its `block`, `rescue` and `always` children, in order.
fn walk<'a>(tasks: &'a [Node], out: &mut Vec<&'a Node>) {
    for task in tasks {
        out.push(task);
        for key in ["block", "rescue", "always"] {
            if let Some(children) = task.get(key).and_then(Node::as_seq) {
                walk(children, out);
            }
        }
    }
}

fn all_tasks(tasks: &[Node]) -> Vec<&Node> {
    let mut out = Vec::new();
    walk(tasks, &mut out);
    out
}

fn name_of(task: &Node) -> Option<&str> {
    task.get("name").and_then(Node::as_str)
}

/// `(task.get("ansible.builtin.command") or {}).get("argv")` as strings; `None` without an argv.
fn argv_of(task: &Node) -> Option<Vec<String>> {
    let argv = task.get("ansible.builtin.command")?.get("argv")?.as_seq()?;
    Some(
        argv.iter()
            .map(|arg| arg.as_str().unwrap_or("<non-string>").to_owned())
            .collect(),
    )
}

/// Python's `argv[:2] == [...]`: the argv starts with exactly `head`.
fn starts_with_argv(task: &Node, head: &[&str]) -> bool {
    argv_of(task).is_some_and(|argv| {
        argv.len() >= head.len() && argv.iter().zip(head).all(|(a, h)| a.as_str() == *h)
    })
}

/// Python's `"<key>" in (task.get("ansible.builtin.set_fact") or {})`.
fn sets(task: &Node, key: &str) -> bool {
    task.get("ansible.builtin.set_fact")
        .and_then(Node::as_map)
        .is_some_and(|entries| entries.iter().any(|(k, _)| k.as_str() == Some(key)))
}

/// Every string in a value, dict keys included: what Python's `str()` of the value holds.
fn strings(value: &Node) -> Vec<String> {
    match value {
        Node::Str(s) => vec![s.clone()],
        Node::Map(entries) => entries
            .iter()
            .flat_map(|(k, v)| strings(k).into_iter().chain(strings(v)))
            .collect(),
        Node::Seq(items) => items.iter().flat_map(strings).collect(),
        Node::Null | Node::Bool(_) => Vec::new(),
    }
}

/// The one task of `found`; panics with `what` when there is not exactly one.
fn exactly_one<'a>(found: Vec<&'a Node>, what: &str) -> &'a Node {
    match found.as_slice() {
        [one] => one,
        other => panic!("{what}: expected one, found {}", other.len()),
    }
}

/// The one task in `all` that `pred` accepts (Python's `(task,) = [t for t in ... if ...]`).
fn only<'a>(all: &[&'a Node], what: &str, pred: impl Fn(&Node) -> bool) -> &'a Node {
    exactly_one(all.iter().copied().filter(|t| pred(t)).collect(), what)
}

fn dry_run_task<'a>(all: &[&'a Node]) -> &'a Node {
    only(all, "the dry-run task", |t| name_of(t) == Some(DRY_RUN))
}

#[test]
fn both_plans_detect_changes_by_exit_code() {
    let tasks = play_tasks();
    let all = all_tasks(&tasks);
    let plans: Vec<&Node> = all
        .iter()
        .copied()
        .filter(|t| starts_with_argv(t, &["tofu", "plan"]))
        .collect();
    assert_eq!(plans.len(), 2, "the two real plans");
    for task in &plans {
        assert!(
            argv_of(task).is_some_and(|argv| argv.iter().any(|a| a == "-detailed-exitcode")),
            "{:?}: no -detailed-exitcode",
            name_of(task)
        );
    }
    let cmd = dry_run_task(&all)
        .get("ansible.builtin.shell")
        .and_then(|shell| shell.get("cmd"))
        .and_then(Node::as_str)
        .expect("the dry run's shell cmd");
    assert!(cmd.contains("tofu plan -input=false -no-color -detailed-exitcode"));
}

#[test]
fn every_credential_bearing_tofu_command_is_no_log() {
    // PR 436 Codex review: each tofu command runs with the R2 keys and the Cloudflare token in its
    // environment, so it is its own no_log task; failures surface through the visible report.
    let tasks = play_tasks();
    let all = all_tasks(&tasks);
    let mut tofu: Vec<&Node> = all
        .iter()
        .copied()
        .filter(|t| argv_of(t).is_some_and(|argv| argv.first().map(String::as_str) == Some("tofu")))
        .collect();
    assert_eq!(
        tofu.len(),
        6,
        "init, plan, show, apply, plan after apply, show"
    );
    tofu.push(dry_run_task(&all)); // the dry run's init, plan and show, in one throwaway root
    for task in tofu {
        let name = name_of(task);
        assert_eq!(task.get("no_log"), Some(&Node::Bool(true)), "{name:?}");
        assert_eq!(
            task.get("failed_when"),
            Some(&Node::Bool(false)),
            "{name:?}"
        );
        assert_eq!(
            task.get("environment").and_then(Node::as_str),
            Some("{{ _tf_env }}"),
            "{name:?}"
        );
    }
    let env = only(&all, "the _tf_env set_fact", |t| sets(t, "_tf_env"));
    assert_eq!(env.get("no_log"), Some(&Node::Bool(true)));
    // The dry run's plan text and JSON carry the origin address, so the unpack is inside the boundary.
    let unpack = only(&all, "the _tf_dry_result set_fact", |t| {
        sets(t, "_tf_dry_result")
    });
    assert_eq!(
        unpack.get("no_log"),
        Some(&Node::Bool(true)),
        "the dry run's plan text and JSON carry the origin address"
    );
}

#[test]
fn the_visible_report_carries_only_exit_codes_and_counts() {
    let tasks = play_tasks();
    let report = exactly_one(
        tasks.iter().filter(|t| sets(t, "_tf_report")).collect(),
        "the _tf_report set_fact",
    );
    assert!(report.get("no_log").is_none());
    let keys: BTreeSet<String> = report
        .get("ansible.builtin.set_fact")
        .and_then(|facts| facts.get("_tf_report"))
        .and_then(Node::as_map)
        .expect("the _tf_report mapping")
        .iter()
        .map(|(k, _)| k.as_str().expect("a string key").to_owned())
        .collect();
    let expected: BTreeSet<String> = [
        "init_rc",
        "plan_rc",
        "apply_rc",
        "apply_changed",
        "verify_rc",
        "show_rc",
        "verify_show_rc",
        "plan_changes",
        "verify_changes",
        "plan_actions",
        "verify_actions",
        "plan_guard",
        "verify_guard",
    ]
    .into_iter()
    .map(String::from)
    .collect();
    assert_eq!(keys, expected);
    for task in &tasks {
        if let Some(debug) = task.get("ansible.builtin.debug") {
            let shown = strings(debug).join("\n");
            for protected in PROTECTED {
                assert!(
                    !shown.contains(protected),
                    "{:?}: the visible output names {protected}",
                    name_of(task)
                );
            }
        }
    }
}

#[test]
fn saved_plans_are_removed_and_shown_inside_the_no_log_boundary() {
    let tasks = play_tasks();
    let block = exactly_one(
        tasks
            .iter()
            .filter(|t| name_of(t) == Some("OpenTofu run"))
            .collect(),
        "the OpenTofu run block",
    );
    let inner = block
        .get("block")
        .and_then(Node::as_seq)
        .expect("the OpenTofu run block's tasks");
    let shows: Vec<&Node> = inner
        .iter()
        .filter(|t| starts_with_argv(t, &["tofu", "show"]))
        .collect();
    assert_eq!(shows.len(), 2);
    for task in shows {
        assert_eq!(
            task.get("no_log"),
            Some(&Node::Bool(true)),
            "{:?}",
            name_of(task)
        );
    }
    let always = block
        .get("always")
        .and_then(Node::as_seq)
        .expect("the OpenTofu run block's always list");
    let cleanup = match always {
        [one] => one,
        other => panic!("the always list: expected one task, found {}", other.len()),
    };
    assert_eq!(
        cleanup
            .get("ansible.builtin.file")
            .and_then(|file| file.get("state"))
            .and_then(Node::as_str),
        Some("absent"),
    );
    let loops: BTreeSet<String> = cleanup
        .get("loop")
        .and_then(Node::as_seq)
        .expect("the cleanup loop")
        .iter()
        .map(|item| item.as_str().expect("a string loop item").to_owned())
        .collect();
    let expected: BTreeSet<String> = ["{{ _tf_planfile }}", "{{ _tf_verify_planfile }}"]
        .into_iter()
        .map(String::from)
        .collect();
    assert_eq!(loops, expected);
}
