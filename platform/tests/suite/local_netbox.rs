//! Local NetBox through the local Semaphore, with discovery confined to local-dev (static tier).
//!
//! Change `service-deployment-workflow`, `platform/netbox-local`, tasks 6.1 and 6.2. The properties
//! guarded: placement installs podman only on podman hosts, so a Docker NetBox host is left alone;
//! NetBox is deployed through the shared placement task and its overlays and loopback publish come
//! from inventory; a non-git copy is moved aside, never deleted; the orb agent checks the local
//! discovery scope before it writes any credential or config; the pfSense and Proxmox workers are
//! off on local-dev; containers are found by compose labels; and the local controller holds
//! production's OpenBao policy file, not an inline copy of it.
//!
//! Replaces `platform/tests/test_local_netbox.bats` (plan 24, Phase 4).
//!
//! Port map:
//!
//! | BATS @test | Rust fn |
//! |---|---|
//! | placement installs podman only on podman hosts (a Docker NetBox host is left alone) | placement_installs_podman_only_on_podman_hosts (candidate: pins text) |
//! | deploy-netbox places the monorepo through the shared task, never a bare clone | deploy_netbox_places_the_monorepo_through_the_shared_task |
//! | deploy-netbox passes the local overlay and loopback publish from inventory only | deploy_netbox_passes_the_local_overlay_and_loopback_publish_from_inventory (candidate: pins text) |
//! | the NetBox compose wrapper adds overlays after the base file, and none by default | the_netbox_compose_wrapper_adds_overlays_after_the_base_file (candidate: pins text) |
//! | deploy.sh moves a non-git netbox-docker copy aside instead of failing or deleting it | deploy_sh_moves_a_non_git_copy_aside_instead_of_deleting_it (candidate: pins text) |
//! | deploy.sh no longer requires openssl (it generates no secrets) | deploy_sh_no_longer_requires_openssl (candidate: pins text) |
//! | orb-agent: local-dev checks the discovery scope before any credential or config | orb_agent_local_dev_checks_the_discovery_scope_before_any_credential |
//! | orb-agent: the pfSense and Proxmox workers are gated, and off on local-dev | the_pfsense_and_proxmox_workers_are_gated_off_on_local_dev (candidate: pins text) |
//! | the local scope guard reads the live engine and runs under --check | the_local_scope_guard_reads_the_live_engine_under_check |
//! | verify-service-persistence selects containers by deploy dir and reports systemd-enablement | verify_service_persistence_selects_containers_by_deploy_dir |
//! | NetBox containers are found by compose labels, not a guessed name separator | netbox_containers_are_found_by_compose_labels (candidate: pins text) |
//! | the local controller's OpenBao policy is production's file, not an inline fork | the_local_openbao_policy_is_productions_file |

use agent_cloud_tests::repo;
use agent_cloud_tests::yaml::{self, Node};
use regex::Regex;

const PLACE_MONOREPO: &str = "platform/playbooks/tasks/place-monorepo.yml";
const DEPLOY_NETBOX: &str = "platform/playbooks/deploy-netbox.yml";
const DEPLOY_ORB_AGENT: &str = "platform/playbooks/deploy-orb-agent.yml";
const DISCOVERY_SCOPE: &str = "platform/playbooks/tasks/assert-local-discovery-scope.yml";
const VERIFY_PERSISTENCE: &str = "platform/playbooks/verify-service-persistence.yml";
const LIST_CONTAINERS: &str = "platform/playbooks/tasks/list-service-containers.yml";
const BOOTSTRAP_LOCAL_DEV: &str = "platform/playbooks/bootstrap-local-dev.yml";
const NETBOX_COMMON: &str = "platform/services/netbox/deployment/lib/common.sh";
const NETBOX_DEPLOY_SH: &str = "platform/services/netbox/deployment/deploy.sh";
const NETBOX_GITIGNORE: &str = "platform/services/netbox/deployment/.gitignore";
const AGENT_TEMPLATE: &str = "platform/services/netbox/deployment/templates/agent.yaml.j2";

fn doc(rel: &str) -> Node {
    yaml::load_one(&repo::path(rel)).unwrap_or_else(|| panic!("{rel} is empty"))
}

fn read(rel: &str) -> String {
    std::fs::read_to_string(repo::path(rel)).unwrap_or_else(|e| panic!("{rel}: {e}"))
}

/// The `name:` of a play or task, or "" for an item without one.
fn name_of(node: &Node) -> &str {
    node.get("name").and_then(Node::as_str).unwrap_or("")
}

/// Every string in `node`, keys and values, depth first.
fn strings<'a>(node: &'a Node, out: &mut Vec<&'a str>) {
    match node {
        Node::Str(s) => out.push(s),
        Node::Seq(items) => items.iter().for_each(|item| strings(item, out)),
        Node::Map(entries) => {
            for (key, value) in entries {
                strings(key, out);
                strings(value, out);
            }
        }
        Node::Null | Node::Bool(_) => {}
    }
}

/// The text of a node: its strings, one per line. Scoped by the caller to one task or file.
fn text(node: &Node) -> String {
    let mut out = vec![];
    strings(node, &mut out);
    out.join("\n")
}

/// A play, an import or a block: a node that holds other tasks, not one task's own text.
fn is_task(node: &Node) -> bool {
    ["hosts", "block", "tasks"]
        .iter()
        .all(|key| node.get(key).is_none())
}

/// True when a map anywhere in `node` has `key`.
fn has_key(node: &Node, key: &str) -> bool {
    match node {
        Node::Map(entries) => entries
            .iter()
            .any(|(k, v)| k.as_str() == Some(key) || has_key(v, key)),
        Node::Seq(items) => items.iter().any(|item| has_key(item, key)),
        Node::Str(_) | Node::Bool(_) | Node::Null => false,
    }
}

/// True when a map anywhere in `node` holds exactly `key: value`.
fn has_entry(node: &Node, key: &str, value: &Node) -> bool {
    match node {
        Node::Map(entries) => entries
            .iter()
            .any(|(k, v)| (k.as_str() == Some(key) && v == value) || has_entry(v, key, value)),
        Node::Seq(items) => items.iter().any(|item| has_entry(item, key, value)),
        Node::Str(_) | Node::Bool(_) | Node::Null => false,
    }
}

/// The first task whose name contains `needle`. The name is the selector: a task is named.
fn task<'a>(tasks: &'a [Node], needle: &str) -> &'a Node {
    tasks
        .iter()
        .find(|t| name_of(t).contains(needle))
        .unwrap_or_else(|| panic!("no task named like {needle:?}"))
}

/// Index of the first item whose name contains `needle`; the order of the file is the order.
fn position(tasks: &[Node], needle: &str) -> usize {
    tasks
        .iter()
        .position(|t| name_of(t).contains(needle))
        .unwrap_or_else(|| panic!("no task named like {needle:?}"))
}

/// Index of the first task that includes `path`.
fn include_position(tasks: &[Node], path: &str) -> usize {
    tasks
        .iter()
        .position(|t| includes(t) == Some(path))
        .unwrap_or_else(|| panic!("no task includes {path:?}"))
}

/// The module arguments of a task, under the module's fully qualified name.
fn module<'a>(task: &'a Node, fq: &str) -> &'a Node {
    task.get(fq)
        .unwrap_or_else(|| panic!("task {:?} has no {fq}", name_of(task)))
}

/// The file an `include_tasks` task includes, under either spelling of the module.
fn includes(task: &Node) -> Option<&str> {
    ["ansible.builtin.include_tasks", "include_tasks"]
        .iter()
        .find_map(|m| task.get(m).and_then(Node::as_str))
}

/// The `when:` conditions of a task, whether one string or a list.
fn when_list(task: &Node) -> Vec<&str> {
    match task.get("when") {
        Some(Node::Str(s)) => vec![s],
        Some(Node::Seq(items)) => items.iter().filter_map(Node::as_str).collect(),
        _ => vec![],
    }
}

/// The lines of the shell function that `header` opens, through its closing `}` at column 0.
/// The same selection as `sed -n '/^<header>/,/^}/p'`.
fn function_body(script: &str, header: &str) -> String {
    let mut body = vec![];
    let mut inside = false;
    for line in script.lines() {
        if !inside {
            if line.starts_with(header) {
                inside = true;
                body.push(line);
            }
            continue;
        }
        body.push(line);
        if line.starts_with('}') {
            break;
        }
    }
    body.join("\n")
}

#[test]
fn placement_installs_podman_only_on_podman_hosts() {
    // Arrange: a Docker host (NetBox in production) must not get podman installed beside it.
    let tasks = repo::tasks(PLACE_MONOREPO);

    // Act
    let ensure = task(&tasks, "Ensure podman + compose entrypoint");

    // Assert
    assert_eq!(
        ensure.get("when").and_then(Node::as_str),
        Some("(container_engine | default('podman')) == 'podman'"),
        "the podman entrypoint is not gated on the podman engine"
    );
}

#[test]
fn deploy_netbox_places_the_monorepo_through_the_shared_task() {
    // Arrange
    let tasks = repo::tasks(DEPLOY_NETBOX);

    // Act
    let shared = tasks
        .iter()
        .any(|t| includes(t) == Some("tasks/place-monorepo.yml"));
    let bare_clone = has_key(&doc(DEPLOY_NETBOX), "ansible.builtin.git");

    // Assert
    assert!(
        shared,
        "deploy-netbox does not place the monorepo through tasks/place-monorepo.yml"
    );
    assert!(
        !bare_clone,
        "deploy-netbox clones the repository with a bare git task"
    );
}

#[test]
fn deploy_netbox_passes_the_local_overlay_and_loopback_publish_from_inventory() {
    // Arrange
    let netbox = doc(DEPLOY_NETBOX);

    // Act
    let overlays = has_entry(
        &netbox,
        "COMPOSE_OVERLAYS",
        &Node::Str("{{ netbox_compose_overlays | default([]) | join(' ') }}".to_owned()),
    );
    let host_ip = has_entry(
        &netbox,
        "NETBOX_HOST_IP",
        &Node::Str("{{ netbox_host_ip | default('') }}".to_owned()),
    );

    // Assert
    assert!(overlays, "COMPOSE_OVERLAYS is not read from inventory");
    assert!(host_ip, "NETBOX_HOST_IP is not read from inventory");
}

#[test]
fn the_netbox_compose_wrapper_adds_overlays_after_the_base_file() {
    // Arrange: the wrapper is a shell function, so its text is selected as one function.
    let body = function_body(&read(NETBOX_COMMON), "compose() {");

    // Act / Assert
    assert!(
        !body.is_empty(),
        "compose() is not defined in {NETBOX_COMMON}"
    );
    assert!(
        body.contains(r#"files=(-f "${compose_dir}/docker-compose.yml")"#),
        "the base compose file is not the first file"
    );
    assert!(
        body.contains("for overlay in ${COMPOSE_OVERLAYS:-}; do"),
        "overlays are not added after the base file, or none is the default"
    );
    assert!(
        body.contains("which does not exist in"),
        "a missing overlay is not refused"
    );
}

#[test]
fn deploy_sh_moves_a_non_git_copy_aside_instead_of_deleting_it() {
    // Arrange
    let script = read(NETBOX_DEPLOY_SH);
    let gitignore = read(NETBOX_GITIGNORE);

    // Act
    let deleted = Regex::new(r#"rm -rf "?\$\{?NETBOX_DOCKER_DIR"#)
        .unwrap()
        .is_match(&script);

    // Assert
    assert!(
        script.contains(r#"mv "${NETBOX_DOCKER_DIR}" "${stale}""#),
        "deploy.sh does not move a non-git copy aside"
    );
    assert!(!deleted, "deploy.sh deletes the netbox-docker copy");
    assert!(
        gitignore.contains("netbox-docker.stale-*/"),
        "the set-aside copies are not ignored by git"
    );
}

#[test]
fn deploy_sh_no_longer_requires_openssl() {
    // Arrange / Act
    let script = read(NETBOX_DEPLOY_SH);

    // Assert: it generates no secrets, so it has no reason to need openssl.
    assert!(
        !script.lines().any(|l| l.starts_with("command -v openssl")),
        "deploy.sh still requires openssl"
    );
}

#[test]
fn orb_agent_local_dev_checks_the_discovery_scope_before_any_credential() {
    // Arrange
    let tasks = repo::tasks(DEPLOY_ORB_AGENT);
    let gate = "not (_local_discovery_enabled | default(false) | bool)";

    // Act
    let scope = include_position(&tasks, "tasks/assert-local-discovery-scope.yml");
    let creds = include_position(&tasks, "tasks/manage-diode-credentials.yml");
    let gated = tasks
        .iter()
        .flat_map(when_list)
        .filter(|c| *c == gate)
        .count();
    let removes = text(module(
        task(&tasks, "remove a previously started orb agent"),
        "ansible.builtin.shell",
    ));

    // Assert
    assert!(
        scope < creds,
        "the scope check runs after the Diode credentials are minted"
    );
    // Three uses: remove the agent a prior run started, stop when discovery is off, and the
    // same stop in play 2.
    assert_eq!(
        gated, 3,
        "the discovery-disabled condition is not on all three gates"
    );
    assert!(
        position(&tasks, "remove a previously started orb agent")
            < position(
                &tasks,
                "stop here when no local discovery target is declared"
            ),
        "the stop runs before a previously started agent is removed"
    );
    assert!(
        removes.contains("rm -f netbox-orb-agent"),
        "the previously started agent is not removed by name"
    );
    assert!(
        !has_key(&doc(DEPLOY_ORB_AGENT), "ansible.builtin.git"),
        "the orb-agent playbook clones the repository with a bare git task"
    );
}

#[test]
fn the_pfsense_and_proxmox_workers_are_gated_off_on_local_dev() {
    // Arrange
    let template = read(AGENT_TEMPLATE);

    // Act / Assert: the workers render only when the inventory enables them.
    assert!(
        template.contains("{% if discovery_workers_enabled | default(true) | bool %}"),
        "the workers are not gated on discovery_workers_enabled"
    );
    assert!(
        template.lines().any(|l| l == "{% endif %}"),
        "the worker gate has no closing endif"
    );
}

#[test]
fn the_local_scope_guard_reads_the_live_engine_under_check() {
    // Arrange: the guard is read-only, so every task that reads the engine or runs the scope script
    // runs under --check.
    let tasks = repo::tasks(DISCOVERY_SCOPE);

    // Act
    let engine = text(module(
        task(&tasks, "read the local engine's network subnets"),
        "ansible.builtin.shell",
    ));
    let scope_script = text(task(&tasks, "refuse any target outside local-dev"));
    let check_mode_off = tasks
        .iter()
        .filter(|t| t.get("check_mode") == Some(&Node::Bool(false)))
        .count();

    // Assert
    assert!(
        engine.contains("network ls -q"),
        "the allowlist is not read from the live engine"
    );
    assert!(
        scope_script.contains("discovery_scope.py"),
        "the targets are not checked by discovery_scope.py"
    );
    assert_eq!(
        check_mode_off, 2,
        "the engine read and the scope check do not both run under --check"
    );
}

#[test]
fn verify_service_persistence_selects_containers_by_deploy_dir() {
    // Arrange
    let list = repo::tasks(LIST_CONTAINERS);
    let verify = repo::tasks(VERIFY_PERSISTENCE);
    let persistence = doc(VERIFY_PERSISTENCE);

    // Act
    let selector = list
        .iter()
        .any(|t| text(t).contains("'label=com.docker.compose.project.working_dir=' ~ _deploy_dir"));
    let readers = [
        "platform/playbooks/verify-service-persistence.yml",
        "platform/playbooks/snapshot-firewall.yml",
        "platform/playbooks/snapshot-service-assessment.yml",
    ]
    .map(|rel| {
        repo::tasks(rel)
            .iter()
            .any(|t| includes(t) == Some("tasks/list-service-containers.yml"))
    });
    let allowed_policy = has_entry(
        &persistence,
        "_restart_ok",
        &Node::Seq(vec![Node::Str("always".to_owned())]),
    );
    let step = verify.iter().any(|t| {
        includes(t) == Some("tasks/emit-step-result.yml")
            && has_entry(
                t,
                "step_result_step",
                &Node::Str("systemd-enablement".to_owned()),
            )
    });
    let empty_selection = verify.iter().any(|t| {
        is_task(t) && text(t).contains("no containers carry the compose working_dir label")
    });

    // Assert
    assert!(
        selector,
        "the container selector does not match the deploy directory label"
    );
    assert!(
        readers.iter().all(|reader| *reader),
        "a snapshot or the persistence check does not select containers through the shared task: {readers:?}"
    );
    assert!(
        allowed_policy,
        "the persistence check does not accept only restart: always"
    );
    assert!(
        step,
        "the persistence result is not recorded as systemd-enablement"
    );
    assert!(
        empty_selection,
        "an empty container selection is not reported as a failure"
    );
}

#[test]
fn netbox_containers_are_found_by_compose_labels_not_a_guessed_name_separator() {
    // Arrange: a shell library, so the selection is by text.
    let lib = read(NETBOX_COMMON);

    // Act / Assert
    assert!(
        !lib.contains("CONTAINER_SEP"),
        "the container lookup still guesses a name separator"
    );
    assert!(
        lib.contains("label=com.docker.compose.service=$1"),
        "containers are not found by their compose service label"
    );
    assert!(
        lib.contains(r#"NETBOX_PG_VOLUME="netbox_netbox-postgres""#),
        "the Postgres volume name is not the compose-derived one"
    );
}

#[test]
fn the_local_openbao_policy_is_productions_file() {
    // Arrange
    let bootstrap = repo::tasks(BOOTSTRAP_LOCAL_DEV);

    // Act
    let policy = text(task(&bootstrap, "Write local-semaphore policy"));

    // Assert: the same policy file production's Semaphore holds, not an inline copy that drifts.
    assert!(
        policy.contains("config/policies/semaphore-read.hcl"),
        "the local policy is not read from production's policy file"
    );
    assert!(
        !policy.contains(r#"path "secret/data/services/*""#),
        "the local policy is an inline fork of production's policy"
    );
}
