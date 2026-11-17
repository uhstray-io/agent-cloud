//! The static tests of the Cloudflare Grafana apply gate (plan 24, Phase 4): the three that read
//! the apply playbook, the Semaphore templates and the o11y tasks file as YAML and text.
//!
//! Replaces three tests of `platform/tests/test_cloudflare_grafana_plan_guard.py`. The rest of
//! that file imports the guard script in-process and stays in Python.
//!
//! Port map (Python test → Rust fn):
//!
//! | Python test | Rust fn |
//! |---|---|
//! | `test_apply_is_bound_to_the_saved_plan_and_digest_is_a_survey_input` | `apply_is_bound_to_the_saved_plan_and_digest_is_a_survey_input` |
//! | `test_generic_cloudflare_executor_stays_unrestricted_and_grafana_uses_wrapper` | `generic_cloudflare_executor_stays_unrestricted_and_grafana_uses_wrapper` |
//! | `test_editor_exception_is_scoped_and_capacity_gates_stay_open` | `editor_exception_is_scoped_and_capacity_gates_stay_open` |
//!
//! The Python helper `_tasks()` read the `localhost` play with `yaml.safe_load`; the Rust reads
//! it with the suite's PyYAML-compatible loader. Python's `" ".join(s.split())` is `normalise`.

use std::collections::HashMap;

use agent_cloud_tests::repo;
use agent_cloud_tests::text::read;
use agent_cloud_tests::yaml::{self, Node};

const PLAYBOOK: &str = "platform/playbooks/apply-cloudflare-tofu.yml";
const WRAPPER: &str = "platform/playbooks/apply-cloudflare-grafana-api-tofu.yml";
const TEMPLATES: &str = "platform/semaphore/templates.yml";
const TASKS: &str =
    "plan/development/openspec/changes/o11y-signal-labeling-profiling-alerting/tasks.md";

/// Python's `" ".join(condition.split())`: whitespace runs become one space, ends trimmed.
fn normalise(text: &str) -> String {
    text.split_whitespace().collect::<Vec<_>>().join(" ")
}

fn name_of(task: &Node) -> Option<&str> {
    task.get("name").and_then(Node::as_str)
}

/// The index of the first task named `name`; panics when there is none (Python's `list.index`).
fn position_of(tasks: &[Node], name: &str) -> usize {
    tasks
        .iter()
        .position(|task| name_of(task) == Some(name))
        .unwrap_or_else(|| panic!("no task named {name:?}"))
}

/// The first task named `name` (Python's `next(t for t in tasks if t.get("name") == name)`).
fn named<'a>(tasks: &'a [Node], name: &str) -> &'a Node {
    &tasks[position_of(tasks, name)]
}

/// `task["ansible.builtin.command"]["argv"]` as strings. A non-string item is shown as a marker.
fn argv(task: &Node) -> Vec<String> {
    task.get("ansible.builtin.command")
        .and_then(|command| command.get("argv"))
        .and_then(Node::as_seq)
        .unwrap_or_else(|| panic!("{:?}: no command argv", name_of(task)))
        .iter()
        .map(|arg| arg.as_str().unwrap_or("<non-string>").to_owned())
        .collect()
}

/// Python's truthiness for the parsed values these tests check.
fn truthy(value: Option<&Node>) -> bool {
    match value {
        None | Some(Node::Null | Node::Bool(false)) => false,
        Some(Node::Str(s)) => !s.is_empty(),
        Some(Node::Seq(items)) => !items.is_empty(),
        Some(Node::Map(entries)) => !entries.is_empty(),
        Some(Node::Bool(true)) => true,
    }
}

/// The `localhost` play's tasks (Python's `next(p for p in yaml.safe_load(...) if p.get("hosts")
/// == "localhost")["tasks"]`).
fn localhost_tasks() -> Vec<Node> {
    let doc = yaml::load_one(&repo::path(PLAYBOOK)).expect("the apply playbook is empty");
    let play = doc
        .as_seq()
        .expect("the playbook is a list of plays")
        .iter()
        .find(|play| play.get("hosts").and_then(Node::as_str) == Some("localhost"))
        .expect("a localhost play");
    play.get("tasks")
        .and_then(Node::as_seq)
        .expect("the localhost play's tasks")
        .to_vec()
}

/// Every Semaphore template, in file order.
fn templates() -> Vec<Node> {
    let doc = yaml::load_one(&repo::path(TEMPLATES)).expect("templates.yml is empty");
    doc.get("templates")
        .and_then(Node::as_seq)
        .expect("a `templates` list")
        .to_vec()
}

/// The first template named `name`.
fn template<'a>(templates: &'a [Node], name: &str) -> &'a Node {
    templates
        .iter()
        .find(|t| name_of(t) == Some(name))
        .unwrap_or_else(|| panic!("no template named {name:?}"))
}

/// The names of a template's survey variables, in order.
fn survey_names(template: &Node) -> Vec<String> {
    template
        .get("survey_vars")
        .and_then(Node::as_seq)
        .expect("a survey_vars list")
        .iter()
        .map(|var| {
            var.get("name")
                .and_then(Node::as_str)
                .expect("a survey variable with a name")
                .to_owned()
        })
        .collect()
}

/// The first survey variable of `template` named `name`.
fn survey_var<'a>(template: &'a Node, name: &str) -> &'a Node {
    template
        .get("survey_vars")
        .and_then(Node::as_seq)
        .and_then(|vars| {
            vars.iter()
                .find(|var| var.get("name").and_then(Node::as_str) == Some(name))
        })
        .unwrap_or_else(|| panic!("no survey variable {name:?}"))
}

#[test]
fn apply_is_bound_to_the_saved_plan_and_digest_is_a_survey_input() {
    let play_tasks = localhost_tasks();
    let block = named(&play_tasks, "OpenTofu run");
    let tasks = block
        .get("block")
        .and_then(Node::as_seq)
        .expect("the OpenTofu run block");
    let receipt_keys = "['actions', 'digest', 'digest_matches', 'position', 'reason', 'resource', \
                        'root_field_changes', 'rule_ref', 'safe', 'status']";
    for (name, variable) in [
        ("Unpack the safe plan receipt", "_tf_plan_guard"),
        (
            "Unpack the post-apply zero-diff receipt",
            "_tf_verify_guard",
        ),
    ] {
        // unpack["block"][1]["ansible.builtin.assert"]["that"]: the second task is the field check.
        let conditions = named(tasks, name)
            .get("block")
            .and_then(Node::as_seq)
            .and_then(|inner| inner.get(1))
            .and_then(|check| check.get("ansible.builtin.assert"))
            .and_then(|assert| assert.get("that"))
            .and_then(Node::as_seq)
            .unwrap_or_else(|| panic!("{name}: no assert list at block[1]"));
        let normalised: Vec<String> = conditions
            .iter()
            .map(|condition| normalise(condition.as_str().expect("a string condition")))
            .collect();
        assert_eq!(
            normalised,
            [
                format!("{variable} is mapping"),
                format!("({variable}.keys() | list | sort) == {receipt_keys} | sort"),
            ]
        );
    }
    let gate_i = position_of(
        tasks,
        "Require the reviewed Grafana rule insertion before apply",
    );
    let apply_i = position_of(tasks, "OpenTofu apply");
    assert!(gate_i < apply_i, "the gate must come before apply");
    assert_eq!(
        argv(&tasks[apply_i]).last().map(String::as_str),
        Some(
            "{{ _tf_planfile if _cloudflare_grafana_guard | default(false) else '-auto-approve' }}"
        ),
    );
    let validate_i = position_of(tasks, "Validate the exact Grafana ruleset plan");
    assert_eq!(tasks[validate_i].get("no_log"), Some(&Node::Bool(true)));
    let validate = tasks[validate_i]
        .get("ansible.builtin.command")
        .expect("the validate task's command");
    assert!(
        truthy(validate.get("stdin")),
        "the validate task takes the plan on stdin"
    );
    assert!(
        argv(&tasks[validate_i])
            .join(" ")
            .contains("reviewed_plan_digest"),
        "the validate task does not pass the reviewed digest"
    );
    let gate = named(
        tasks,
        "Require the reviewed Grafana rule insertion before apply",
    )
    .get("ansible.builtin.assert")
    .expect("the gate's assert");
    assert_eq!(
        gate.get("that"),
        Some(&Node::Seq(vec![
            Node::Str("_tf_plan_guard.safe | default(false) | bool".to_owned()),
            Node::Str("_tf_plan_guard.status | default('') in ['insert', 'present']".to_owned()),
            Node::Str("_tf_plan_guard.digest_matches | default(false) | bool".to_owned()),
        ])),
    );
    let all = templates();
    let entry = template(&all, "Apply Grafana API Cloudflare Tofu (Dev)");
    assert_eq!(
        entry.get("repository").and_then(Node::as_str),
        Some("agent-cloud dev")
    );
    assert!(entry.get("dev_variant").is_none());
    assert!(
        !all.iter()
            .any(|t| name_of(t) == Some("Apply Grafana API Cloudflare Tofu"))
    );
    let digest = survey_var(entry, "reviewed_plan_digest");
    assert_eq!(digest.get("required"), Some(&Node::Bool(false)));
    let description = digest
        .get("description")
        .and_then(Node::as_str)
        .expect("the digest's description");
    assert!(description.to_lowercase().contains("apply") && description.contains("SHA-256"));
}

#[test]
fn generic_cloudflare_executor_stays_unrestricted_and_grafana_uses_wrapper() {
    let all = templates();
    let generic = template(&all, "Apply Cloudflare Tofu");
    let dedicated = template(&all, "Apply Grafana API Cloudflare Tofu (Dev)");
    let doc = yaml::load_one(&repo::path(WRAPPER)).expect("the wrapper playbook is empty");
    let wrapper = doc.as_seq().expect("the wrapper is a list");
    assert!(
        !survey_names(generic)
            .iter()
            .any(|n| n == "reviewed_plan_digest")
    );
    assert_eq!(
        dedicated.get("repository").and_then(Node::as_str),
        Some("agent-cloud dev")
    );
    assert!(dedicated.get("dev_variant").is_none());
    assert!(
        !all.iter()
            .any(|t| name_of(t) == Some("Apply Grafana API Cloudflare Tofu"))
    );
    assert_eq!(
        dedicated.get("playbook").and_then(Node::as_str),
        Some(WRAPPER)
    );
    assert_eq!(
        wrapper[0]
            .get("ansible.builtin.import_playbook")
            .and_then(Node::as_str),
        Some("refuse-internal-extra-vars.yml"),
    );
    // Python dict equality: exactly these two keys, both true.
    let vars = wrapper[1].get("vars").expect("the wrapper's vars");
    assert_eq!(vars.as_map().map(|entries| entries.len()), Some(2));
    assert_eq!(vars.get("_extra_var_guard_nested"), Some(&Node::Bool(true)));
    assert_eq!(
        vars.get("_cloudflare_grafana_guard"),
        Some(&Node::Bool(true))
    );
}

#[test]
fn editor_exception_is_scoped_and_capacity_gates_stay_open() {
    let text = read(TASKS);
    let mut status: HashMap<&str, bool> = HashMap::new();
    let mut lowered: HashMap<&str, String> = HashMap::new();
    for line in text.lines() {
        if line.starts_with("- [ ] ") || line.starts_with("- [x] ") {
            // line.split("] ", 1)[1].split(maxsplit=1)[0]: the task id after the checkbox.
            let id = line
                .split_once("] ")
                .and_then(|(_, rest)| rest.split_whitespace().next())
                .expect("a task id");
            status.insert(id, line.as_bytes()[3] == b'x');
            lowered.insert(id, line.to_lowercase());
        }
    }
    let got: Vec<(&str, bool)> = [
        "2.2a", "2.2b", "2.2c", "2.3", "3.3", "4.3", "5.3", "5.4", "5.5",
    ]
    .into_iter()
    .map(|id| {
        let done = *status
            .get(id)
            .unwrap_or_else(|| panic!("{id} is not a task line in {TASKS}"));
        (id, done)
    })
    .collect();
    assert_eq!(
        got,
        [
            ("2.2a", true),
            ("2.2b", true),
            ("2.2c", true),
            ("2.3", true),
            ("3.3", true),
            ("4.3", false),
            ("5.3", false),
            ("5.4", false),
            ("5.5", false),
        ]
    );
    let editor = &lowered["2.2b"];
    assert!(
        editor.contains("editor")
            && editor.contains("dedicated account")
            && editor.contains("scoped")
    );
    assert!(editor.contains("watcher") && editor.contains("viewer"));
    assert!(editor.contains("openbao") && editor.contains("source of truth"));
    assert!(editor.contains("mode 0600") && editor.contains("~/.config/mcp-grafana/prod.token"));
    let launcher = &lowered["2.2c"];
    assert!(launcher.contains("local profile") && launcher.contains("--disable-write"));
    assert!(launcher.contains("production profile"));
    assert!(launcher.contains("without `--disable-write`"));
}
