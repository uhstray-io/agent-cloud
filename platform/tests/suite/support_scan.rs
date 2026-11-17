//! The static-scan helpers the ports use (plan 24, AC-958): `repo::playbook_files`, the
//! playbook listing of `playbook_yaml.files()`; `yaml::strings`, the string walk of
//! `playbook_yaml.strings(value, keys)`; and `yaml::tasks`, the task walk of
//! `playbook_yaml.tasks(node)`.
//!
//! These tests pin each helper's own rules on fixtures and on the repository. The comparison with
//! the Python on every file is `support_scan_python` (cli tier: it starts `python3`).

use agent_cloud_tests::repo;
use agent_cloud_tests::yaml::{self, Node};

fn doc(text: &str) -> Node {
    yaml::parse(text, "inline").remove(0)
}

#[test]
fn strings_yields_every_value_string_in_document_order() {
    // Arrange
    let node = doc("a: x\nb: [y, {c: z}]\nd: true\ne: ~\nf: 'quoted'\n");

    // Act
    let found = yaml::strings(&node, false);

    // Assert
    assert_eq!(found, ["x", "y", "z", "quoted"]);
}

#[test]
fn strings_yields_mapping_keys_only_when_asked() {
    // Arrange
    let node = doc("a: x\nb: {c: z}\n");

    // Act
    let without = yaml::strings(&node, false);
    let with = yaml::strings(&node, true);

    // Assert
    assert_eq!(without, ["x", "z"]);
    assert_eq!(with, ["a", "x", "b", "c", "z"]);
}

#[test]
fn strings_skips_booleans_and_nulls_but_yields_plain_numbers() {
    // Arrange: PyYAML reads `yes`, `false` and `~` as bool and null and yields none of them; it
    // reads a plain `53` as an int and yields it neither, but a Node has no number type, so `53`
    // is a Str here and is yielded (the documented difference, `yaml::strings`).
    let node = doc("a: yes\nb: false\nc: ~\nd: 53\ne: '54'\n");

    // Act
    let found = yaml::strings(&node, false);

    // Assert
    assert_eq!(found, ["53", "54"]);
}

#[test]
fn strings_reads_through_anchors_and_merges() {
    // Arrange: an alias repeats its anchor's strings, and a `<<` merge brings the merged keys.
    let node = doc("base: &b {k: v}\nuse: *b\nmerged:\n  <<: *b\n  own: w\n");

    // Act
    let found = yaml::strings(&node, false);

    // Assert
    assert_eq!(found.iter().filter(|s| **s == "v").count(), 3);
    assert!(found.contains(&"w"));
}

#[test]
fn playbook_files_lists_yml_files_under_both_roots_in_component_order() {
    // Act
    let files = repo::playbook_files();

    // Assert
    assert!(
        files.iter().any(|f| f.starts_with("platform/playbooks/")),
        "no file under platform/playbooks"
    );
    assert!(
        files.iter().any(|f| f.starts_with("platform/semaphore/")),
        "no file under platform/semaphore"
    );
    assert!(
        files
            .iter()
            .any(|f| f.starts_with("platform/playbooks/tasks/")),
        "the walk did not descend into platform/playbooks/tasks"
    );
    for file in &files {
        assert!(file.ends_with(".yml"), "{file} is not a .yml file");
        assert!(repo::path(file).is_file(), "{file} is not a file");
    }
    let mut by_component: Vec<Vec<&str>> = files.iter().map(|f| f.split('/').collect()).collect();
    by_component.sort();
    let resorted: Vec<String> = by_component.iter().map(|p| p.join("/")).collect();
    assert_eq!(files, resorted, "not sorted by path component");
}

/// The `name` of each node `yaml::tasks` yields.
fn task_names(text: &str) -> Vec<String> {
    let node = doc(text);
    yaml::tasks(&node)
        .into_iter()
        .map(|t| {
            t.get("name")
                .and_then(Node::as_str)
                .unwrap_or("?")
                .to_owned()
        })
        .collect()
}

#[test]
fn tasks_reads_each_task_list_in_the_python_key_order() {
    // Arrange: one item holding all seven task lists, written in the reverse of playbook_yaml's
    // TASK_LISTS order, each list one task named after its key.
    let text = "- name: top\n  always: [{name: always}]\n  rescue: [{name: rescue}]\n  block: [{name: block}]\n  handlers: [{name: handlers}]\n  post_tasks: [{name: post_tasks}]\n  pre_tasks: [{name: pre_tasks}]\n  tasks: [{name: tasks}]\n";

    // Act
    let names = task_names(text);

    // Assert: the key order, not the file's order.
    assert_eq!(
        names,
        [
            "top",
            "tasks",
            "pre_tasks",
            "post_tasks",
            "handlers",
            "block",
            "rescue",
            "always"
        ]
    );
}

#[test]
fn tasks_yields_only_mappings_and_descends_depth_first() {
    // Arrange: a string item, a task list that is not a list, and a nested block.
    let text = "- just a string\n- name: a\n  tasks: \"{{ not_a_list }}\"\n- name: b\n  block:\n    - name: c\n      block: [{name: d}]\n    - plain\n- name: e\n";

    // Act
    let names = task_names(text);

    // Assert
    assert_eq!(names, ["a", "b", "c", "d", "e"]);
}

#[test]
fn tasks_of_a_document_that_is_not_a_list_is_empty() {
    // Arrange: a mapping document, such as templates.yml.
    let node = doc("templates:\n  - name: t\n    tasks: [{name: x}]\n");

    // Act
    let found = yaml::tasks(&node);

    // Assert
    assert!(found.is_empty(), "{found:?}");
}
