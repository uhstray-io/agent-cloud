//! The shared secret lifecycle writes the service's secrets only through the merge task (static
//! tier).
//!
//! Every composable deploy includes tasks/manage-secrets.yml. A KV-v2 POST of `_resolved` writes
//! the FULL document and silently deletes every key at the path that `_secret_definitions` does not
//! declare. Measured: a deploy re-run erased a service's post-deploy API key from the store
//! (docs/MISTAKES.md 10.11). Post-deploy keys, such as captured API keys and seeded operator
//! credentials, legitimately live beside the deploy-managed ones. So the store-back goes through the
//! shared merge-patch implementation, and no task writes to the service path directly.
//!
//! Replaces platform/tests/test_manage_secrets.bats (plan 24, Phase 4).
//!
//! ## Port map
//!
//! | BATS @test | Rust fn |
//! |---|---|
//! | manage-secrets: the store-back is a sibling-preserving merge, never a whole-document write | `the_store_back_goes_through_the_shared_merge_task`, with `no_task_writes_to_the_service_secret_path_directly` |

use agent_cloud_tests::repo;
use agent_cloud_tests::yaml::Node;

const MANAGE: &str = "platform/playbooks/tasks/manage-secrets.yml";
const MERGE_TASK: &str = "bao-merge-keys.yml";
const SERVICE_PATH: &str = "secret/data";

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

/// A string at any depth, in a key or a value: a task names a path wherever it does so.
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

#[test]
fn the_store_back_goes_through_the_shared_merge_task() {
    // Arrange
    let tasks = repo::tasks(MANAGE);

    // Act
    let includes_merge = tasks
        .iter()
        .any(|task| module(task, "include_tasks").and_then(Node::as_str) == Some(MERGE_TASK));

    // Assert
    assert!(
        includes_merge,
        "{MANAGE} must store secrets back through include_tasks: {MERGE_TASK}"
    );
}

#[test]
fn no_task_writes_to_the_service_secret_path_directly() {
    // Arrange: a task names the KV-v2 data path wherever it does so, in its url, its vars, or any
    // other scalar. Selecting on the url alone let a write through with the path in a task var.
    let tasks = repo::tasks(MANAGE);
    let on_secret_path: Vec<&Node> = tasks
        .iter()
        .filter(|task| any_string(task, &|s: &str| s.contains(SERVICE_PATH)))
        .collect();
    assert!(
        !on_secret_path.is_empty(),
        "{MANAGE} has no task on {SERVICE_PATH}; the check would pass for nothing"
    );

    // Act / Assert: every such task is a uri GET (fetch existing, shared reads), under whichever
    // spelling of the uri module it uses. A method left out is GET, as the module defaults.
    for task in on_secret_path {
        let name = task
            .get("name")
            .and_then(Node::as_str)
            .unwrap_or("(unnamed)");
        let Some(uri) = module(task, "uri") else {
            panic!("{MANAGE}: task {name:?} names {SERVICE_PATH} but is not a uri read");
        };
        let method = uri.get("method").and_then(Node::as_str).unwrap_or("GET");
        assert!(
            method.eq_ignore_ascii_case("GET"),
            "{MANAGE}: task {name:?} reads {SERVICE_PATH} with method {method}, not GET"
        );
    }
}
