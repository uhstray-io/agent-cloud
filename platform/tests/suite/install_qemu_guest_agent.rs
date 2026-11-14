//! The static tier of the install-qemu-guest-agent pilot (plan 24).
//!
//! Replaces the two static tests of `platform/tests/test_install_qemu_guest_agent.bats`. Its two
//! refusal runs are in `install_qemu_guest_agent_playbook`.
//!
//! Port map (BATS @test → Rust fn):
//!
//! | BATS @test | Rust fn |
//! |---|---|
//! | `qga: the virtio-port check runs before the package install` | `the_virtio_port_check_runs_before_the_package_install` |
//! | `qga: an unset target can never fall back to the controller` | `an_unset_target_can_never_fall_back_to_the_controller` |

use agent_cloud_tests::repo;
use agent_cloud_tests::yaml::Node;

const PLAYBOOK: &str = "platform/playbooks/install-qemu-guest-agent.yml";

/// The tasks of a playbook in the order Ansible runs them: play by play, each play's `pre_tasks`,
/// then `tasks`, then `post_tasks`, with a block's `block`, `rescue` and `always` in place.
fn execution_order(rel: &str) -> Vec<Node> {
    fn flatten(items: &[Node], out: &mut Vec<Node>) {
        for item in items {
            out.push(item.clone());
            for section in ["block", "rescue", "always"] {
                if let Some(children) = item.get(section).and_then(Node::as_seq) {
                    flatten(children, out);
                }
            }
        }
    }
    let mut out = vec![];
    for play in repo::tasks(rel)
        .iter()
        .filter(|node| node.get("hosts").is_some())
    {
        for section in ["pre_tasks", "tasks", "post_tasks"] {
            if let Some(items) = play.get(section).and_then(Node::as_seq) {
                flatten(items, &mut out);
            }
        }
    }
    out
}

#[test]
fn the_virtio_port_check_runs_before_the_package_install() {
    // Execution order, not line or walk order: the first task that installs a package must come
    // after the task that looks for the guest-agent port. `repo::tasks` visits `tasks` before
    // `pre_tasks`, so a walk would pass with the install moved to `pre_tasks` (#1141 review).
    let tasks = execution_order(PLAYBOOK);
    let position = |matches: &dyn Fn(&Node) -> bool| {
        tasks
            .iter()
            .position(matches)
            .unwrap_or_else(|| panic!("no matching task in {PLAYBOOK}"))
    };
    let port = position(&|task| {
        task.get("name").and_then(Node::as_str) == Some("Look for the guest-agent virtio port")
    });
    let install = position(&|task| task.get("ansible.builtin.apt").is_some());
    assert!(
        port < install,
        "the virtio-port check (task {port}) must run before the package install (task {install})"
    );
}

#[test]
fn an_unset_target_can_never_fall_back_to_the_controller() {
    // The play's host pattern must not name localhost, and the file must not carry the fallback
    // spelling the BATS test refused.
    let play = repo::tasks(PLAYBOOK)
        .into_iter()
        .find(|node| node.get("hosts").is_some())
        .expect("the play declares hosts");
    let hosts = play
        .get("hosts")
        .and_then(Node::as_str)
        .expect("hosts is a string");
    assert!(
        !hosts.contains("localhost"),
        "the play's hosts is `{hosts}`"
    );

    let text = std::fs::read_to_string(repo::path(PLAYBOOK)).expect("the playbook reads");
    assert!(
        !text.contains("default('localhost'"),
        "{PLAYBOOK} falls back to localhost when target_service is unset"
    );
}
