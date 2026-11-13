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

/// The URL of a uri task, if the task is one.
fn url_of(task: &Node) -> Option<&str> {
    task.get("ansible.builtin.uri")?.get("url")?.as_str()
}

/// The HTTP method of a uri task. The module defaults to GET.
fn method_of(task: &Node) -> &str {
    task.get("ansible.builtin.uri")
        .and_then(|uri| uri.get("method"))
        .and_then(Node::as_str)
        .unwrap_or("GET")
}

#[test]
fn the_store_back_goes_through_the_shared_merge_task() {
    // Arrange
    let tasks = repo::tasks(MANAGE);

    // Act
    let includes_merge = tasks.iter().any(|task| {
        ["include_tasks", "ansible.builtin.include_tasks"]
            .iter()
            .any(|key| task.get(key).and_then(Node::as_str) == Some(MERGE_TASK))
    });

    // Assert
    assert!(
        includes_merge,
        "{MANAGE} must store secrets back through include_tasks: {MERGE_TASK}"
    );
}

#[test]
fn no_task_writes_to_the_service_secret_path_directly() {
    // Arrange: the tasks that read or write the KV-v2 data path.
    let tasks = repo::tasks(MANAGE);
    let on_secret_path: Vec<&Node> = tasks
        .iter()
        .filter(|task| url_of(task).is_some_and(|url| url.contains(SERVICE_PATH)))
        .collect();
    assert!(
        !on_secret_path.is_empty(),
        "{MANAGE} has no task on {SERVICE_PATH}; the check would pass for nothing"
    );

    // Act / Assert: every such task is a GET (fetch existing, shared reads). A write goes through
    // the merge task.
    for task in on_secret_path {
        let method = method_of(task);
        assert_eq!(
            method,
            "GET",
            "a task on {SERVICE_PATH} writes with {method}: {}",
            url_of(task).unwrap_or_default()
        );
    }
}
