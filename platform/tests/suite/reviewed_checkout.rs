//! The shared reviewed-checkout playbook asserts that a caller requests at most sixteen helper
//! payload paths (static tier).
//!
//! `require-reviewed-checkout.yml` asserts that a caller requests at most 16 helper payload paths.
//! This case reads the playbook and checks that assertion is there with that number. Nothing
//! compares the number with the helper's own limit, `MAX_PAYLOAD_PATHS` in
//! platform/playbooks/files/reviewed-checkout.py. The rest of test_reviewed_checkout.py imports
//! the helper in-process and stays Python.
//!
//! Replaces test_reviewed_checkout_playbook_uses_same_sixteen_path_cap in
//! platform/tests/test_reviewed_checkout.py (plan 24, Phase 4).
//!
//! Port map (the one ported Python test, once):
//!
//! | Python test | Rust fn |
//! | --- | --- |
//! | test_reviewed_checkout_playbook_uses_same_sixteen_path_cap | `the_playbook_caps_the_requested_payloads_at_sixteen` |

use agent_cloud_tests::repo;
use agent_cloud_tests::yaml::{self, Node};

const PLAYBOOK: &str = "platform/playbooks/require-reviewed-checkout.yml";
const CAP: &str = "_reviewed_checkout_requested_payloads | length <= 16";

#[test]
fn the_playbook_caps_the_requested_payloads_at_sixteen() {
    // Arrange: the playbook is a list of plays; the first play's first task is the input assert.
    let plays = yaml::load_one(&repo::path(PLAYBOOK)).expect("the playbook holds one document");
    let first_play = plays
        .as_seq()
        .and_then(|plays| plays.first())
        .expect("the playbook has a play");
    let first_task = first_play
        .get("tasks")
        .and_then(Node::as_seq)
        .and_then(|tasks| tasks.first())
        .expect("the play has a task");

    // Act
    let conditions = first_task
        .get("ansible.builtin.assert")
        .and_then(|assert| assert.get("that"))
        .and_then(Node::as_seq)
        .expect("the task asserts a list of conditions");

    // Assert
    assert!(
        conditions.iter().any(|node| node.as_str() == Some(CAP)),
        "the playbook does not assert `{CAP}`"
    );
}
