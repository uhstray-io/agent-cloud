//! `install-podman.yml`, the removed speculation and the opt-in Docker compatibility (static tier).
//!
//! Three concerns. A removal recorded in `docs/MISTAKES.md`: an earlier version configured a
//! rootless API socket and relied on `systemctl --machine`, both built from an assumption about
//! how a consumer invokes podman, and nothing used either. The opt-in Docker CLI shim, which the
//! GitHub Actions runner container hooks need because they shell out to a `docker` binary; both
//! options default OFF, so a host that already runs this playbook installs nothing new. And the
//! opt-in Docker CLI client, pinned, client-only, which replaces the shim for the runner
//! (`docs/MISTAKES.md` 10.29): the two are mutually exclusive, and nothing installs a Docker engine.
//!
//! Replaces `platform/tests/test_install_podman.bats` (plan 24, Phase 4).
//!
//! Port map:
//!
//! | BATS @test | Rust fn |
//! |---|---|
//! | install-podman: exists and is a playbook | exists_and_is_a_playbook |
//! | install-podman: configures no podman API socket | configures_no_podman_api_socket |
//! | install-podman: does not depend on systemctl --machine | does_not_depend_on_systemctl_machine |
//! | install-podman: the Docker CLI shim and the Docker CLI client are opt-in and default OFF | the_docker_options_are_opt_in_and_default_off |
//! | install-podman: the shim package and its banner marker are both handled | the_shim_package_and_its_banner_marker_are_both_handled |
//! | install-podman: the banner marker touch is idempotent | the_banner_marker_touch_is_idempotent |
//! | install-podman: verification checks presence, not a rootful engine call | verification_checks_presence_not_a_rootful_engine_call |
//! | install-podman: waits for a freshly-booted host before installing | waits_for_a_freshly_booted_host_before_installing |
//! | enable-linger: accepts an explicit linger_user, defaulting to ansible_user | enable_linger_accepts_an_explicit_linger_user (candidate: pins text) |
//! | install-podman: installs catatonit, the init podman bind-mounts for a container with init: true | installs_catatonit_and_uidmap_which_podman_only_recommends |
//! | install-podman: installs uidmap, without which rootless podman cannot start a container | installs_catatonit_and_uidmap_which_podman_only_recommends |
//! | install-podman: the shim and the Docker CLI client are refused together | the_shim_and_the_docker_cli_client_are_refused_together |
//! | install-podman: the Docker CLI client is pinned, client-only, and replaces the shim | the_docker_cli_client_is_pinned_client_only_and_replaces_the_shim |
//! | docker apt repository: one shared task, used by both install playbooks | the_docker_apt_repository_is_one_shared_task_used_by_both_playbooks |

use agent_cloud_tests::repo;
use agent_cloud_tests::yaml::{self, Node};
use regex::Regex;

const PB: &str = "platform/playbooks/install-podman.yml";
const INSTALL_DOCKER: &str = "platform/playbooks/install-docker.yml";
const ADD_DOCKER_REPO: &str = "platform/playbooks/tasks/add-docker-apt-repo.yml";
const LINGER: &str = "platform/playbooks/tasks/enable-linger.yml";
const WAIT_FOR_APT: &str = "platform/playbooks/tasks/wait-for-apt.yml";

fn doc(rel: &str) -> Node {
    yaml::load_one(&repo::path(rel)).unwrap_or_else(|| panic!("{rel} is empty"))
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

/// One field of a task's module arguments, as text.
fn field<'a>(task: &'a Node, fq: &str, key: &str) -> Option<&'a str> {
    task.get(fq)?.get(key)?.as_str()
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

/// The `that:` conditions of an assert task.
fn that_list(task: &Node) -> Vec<&str> {
    match module(task, "ansible.builtin.assert").get("that") {
        Some(Node::Str(s)) => vec![s],
        Some(Node::Seq(items)) => items.iter().filter_map(Node::as_str).collect(),
        _ => vec![],
    }
}

/// The package names of an apt task: its `name:` as one string or a list.
fn apt_names(task: &Node) -> Vec<&str> {
    let Some(apt) = task.get("ansible.builtin.apt") else {
        return vec![];
    };
    match apt.get("name") {
        Some(Node::Str(s)) => vec![s],
        Some(Node::Seq(items)) => items.iter().filter_map(Node::as_str).collect(),
        _ => vec![],
    }
}

/// The command a `command:` task runs, in its short or its long form.
fn command_line(task: &Node) -> Option<&str> {
    match task.get("ansible.builtin.command")? {
        Node::Str(s) => Some(s.as_str()),
        long => long.get("cmd").and_then(Node::as_str),
    }
}

/// The play whose name starts with `prefix`, as parsed (its `vars:` and its `tasks:`).
fn play(prefix: &str) -> Node {
    repo::tasks(PB)
        .into_iter()
        .find(|n| n.get("hosts").is_some() && name_of(n).starts_with(prefix))
        .unwrap_or_else(|| panic!("no play named {prefix:?}"))
}

/// The Docker-compatibility tasks that touch the shim or the CLI client, and how many there are,
/// with the names of those that have no opt-in as their first condition. Assert and debug tasks
/// change nothing on the host and need no gate.
fn shim_tasks() -> (usize, Vec<String>) {
    let touches = Regex::new(
        r"podman-docker|/etc/containers/nodocker|/usr/bin/docker|docker-ce-cli|add-docker-apt-repo",
    )
    .unwrap();
    let opt_in = Regex::new(r"^_docker_(cli|api_client)\b").unwrap();
    let mut touched = 0;
    let mut ungated = vec![];
    for task in repo::tasks(PB).iter().filter(|t| is_task(t)) {
        if task.get("ansible.builtin.assert").is_some()
            || task.get("ansible.builtin.debug").is_some()
        {
            continue;
        }
        if !touches.is_match(&text(task)) {
            continue;
        }
        touched += 1;
        if !when_list(task).first().is_some_and(|c| opt_in.is_match(c)) {
            ungated.push(name_of(task).to_owned());
        }
    }
    (touched, ungated)
}

#[test]
fn exists_and_is_a_playbook() {
    // Arrange / Act
    let playbook = doc(PB);

    // Assert
    let plays = playbook
        .as_seq()
        .expect("the playbook is a sequence of plays");
    assert!(
        plays.iter().any(|p| p.get("name").is_some()),
        "no play in {PB} has a name"
    );
}

#[test]
fn configures_no_podman_api_socket() {
    // Arrange: the socket was added speculatively, and enabling it puts a listener on every host.
    let all = text(&doc(PB));

    // Act
    let socket = Regex::new(r"podman\.socket|podman\.sock|podman[- ]system[- ]service").unwrap();

    // Assert
    assert!(
        !socket.is_match(&all),
        "the playbook configures a podman API socket"
    );
}

#[test]
fn does_not_depend_on_systemctl_machine() {
    // Arrange: `systemctl --machine` needs a user D-Bus, which a freshly provisioned host lacks.
    let all = text(&doc(PB));

    // Act
    let machine = Regex::new(r"systemctl .*--machine").unwrap();

    // Assert
    assert!(
        !machine.is_match(&all),
        "the playbook depends on systemctl --machine"
    );
}

#[test]
fn the_docker_options_are_opt_in_and_default_off() {
    // Arrange
    let install = play("Install Podman");
    let vars = install.get("vars");

    // Act
    let shim = vars
        .and_then(|v| v.get("_docker_cli"))
        .and_then(Node::as_str);
    let client = vars
        .and_then(|v| v.get("_docker_api_client"))
        .and_then(Node::as_str);
    let (touched, ungated) = shim_tasks();

    // Assert
    assert_eq!(
        shim,
        Some("{{ podman_docker_cli | default(false) | bool }}"),
        "the shim is not opt-in"
    );
    assert_eq!(
        client,
        Some("{{ podman_docker_api_client | default(false) | bool }}"),
        "the CLI client is not opt-in"
    );
    // Per task, not a count: a gated task elsewhere must not stand in for an ungated one.
    assert!(
        touched > 0,
        "no task touches the Docker shim, so the gate checks nothing"
    );
    assert!(
        ungated.is_empty(),
        "Docker compatibility tasks with no opt-in as their condition: {ungated:?}"
    );
}

#[test]
fn the_shim_package_and_its_banner_marker_are_both_handled() {
    // Arrange
    let tasks = repo::tasks(PB);

    // Act
    let package = tasks
        .iter()
        .any(|t| apt_names(t).contains(&"podman-docker"));
    let marker = tasks
        .iter()
        .any(|t| field(t, "ansible.builtin.file", "path") == Some("/etc/containers/nodocker"));

    // Assert
    assert!(package, "no task installs the podman-docker shim");
    assert!(
        marker,
        "no task manages the /etc/containers/nodocker banner marker"
    );
}

#[test]
fn the_banner_marker_touch_is_idempotent() {
    // Arrange
    let tasks = repo::tasks(PB);

    // Act
    let touch = tasks
        .iter()
        .find(|t| field(t, "ansible.builtin.file", "state") == Some("touch"))
        .expect("no task touches the banner marker");
    let file = module(touch, "ansible.builtin.file");

    // Assert: a bare touch reports changed on every run.
    assert_eq!(
        file.get("modification_time").and_then(Node::as_str),
        Some("preserve"),
        "the banner touch changes the marker's modification time on every run"
    );
    assert_eq!(
        file.get("access_time").and_then(Node::as_str),
        Some("preserve"),
        "the banner touch changes the marker's access time on every run"
    );
}

#[test]
fn verification_checks_presence_not_a_rootful_engine_call() {
    // Arrange
    let tasks = repo::tasks(PB);

    // Act
    let presence = tasks
        .iter()
        .any(|t| field(t, "ansible.builtin.stat", "path") == Some("/usr/bin/docker"));
    let rootful_call = tasks.iter().any(|t| command_line(t) == Some("docker ps"));

    // Assert: `docker ps` as root initialises rootful podman state and proves nothing about the caller.
    assert!(
        presence,
        "the docker shim is not verified by presence at /usr/bin/docker"
    );
    assert!(
        !rootful_call,
        "the verification runs `docker ps`, which initialises rootful podman state"
    );
}

#[test]
fn waits_for_a_freshly_booted_host_before_installing() {
    // Arrange
    let tasks = repo::tasks(PB);

    // Act
    let wait = include_position(&tasks, "tasks/wait-for-apt.yml");
    let install = tasks
        .iter()
        .position(|t| {
            is_task(t)
                && apt_names(t)
                    .iter()
                    .any(|n| *n == "podman" || *n == "podman-docker")
        })
        .expect("no task installs podman");
    let wait_tasks = repo::tasks(WAIT_FOR_APT);
    let wait_text = text(&doc(WAIT_FOR_APT));

    // Assert
    assert!(
        wait < install,
        "podman is installed (task {install}) before the apt wait (task {wait})"
    );
    // The wait depends on no package that may be absent: apt-get check is its only probe.
    assert!(
        wait_tasks
            .iter()
            .any(|t| command_line(t) == Some("apt-get check")),
        "the apt wait does not run apt-get check"
    );
    assert!(
        !Regex::new("fuser|lsof").unwrap().is_match(&wait_text),
        "the apt wait relies on fuser or lsof, which a fresh host may lack"
    );
}

#[test]
fn enable_linger_accepts_an_explicit_linger_user_defaulting_to_ansible_user() {
    // Arrange: a CI runner lingers a dedicated account, so the user is a parameter.
    let linger = text(&doc(LINGER));

    // Act
    let machine_default = Regex::new(r"loginctl (show-user|enable-linger) \{\{ ansible_user \}\}")
        .unwrap()
        .is_match(&linger);

    // Assert
    assert!(
        linger.contains("linger_user | default(ansible_user)"),
        "enable-linger does not take linger_user, defaulting to ansible_user"
    );
    assert!(
        !machine_default,
        "enable-linger hard-wires ansible_user into a loginctl command"
    );
}

#[test]
fn installs_catatonit_and_uidmap_which_podman_only_recommends() {
    // Arrange: podman only RECOMMENDS these two. Without catatonit a container with init: true
    // will not start; without uidmap rootless podman cannot map users, so every container fails.
    let tasks = repo::tasks(PB);
    let cases = [
        (
            "catatonit",
            "the init podman bind-mounts for a container with init: true",
        ),
        (
            "uidmap",
            "newuidmap and newgidmap, which rootless podman needs",
        ),
    ];

    for (package, why) in cases {
        // Act
        let installed = tasks
            .iter()
            .any(|t| is_task(t) && apt_names(t).contains(&package));

        // Assert
        assert!(installed, "{package} is not installed: {why}");
    }
}

#[test]
fn the_shim_and_the_docker_cli_client_are_refused_together() {
    // Arrange: both provide /usr/bin/docker and their packages conflict.
    let tasks = repo::tasks(PB);
    let refuse = task(&tasks, "Refuse both Docker options at once");

    // Act
    let conditions = that_list(refuse);

    // Assert
    assert!(
        conditions
            .iter()
            .any(|c| c.contains("not (_docker_cli and _docker_api_client)")),
        "the refusal does not name both options: {conditions:?}"
    );
    assert!(
        refuse.get("when").is_none(),
        "the refusal is conditional, so a run can skip it"
    );
    assert!(
        position(&tasks, "Refuse both Docker options at once")
            < include_position(&tasks, "tasks/wait-for-apt.yml"),
        "the refusal runs after an install could begin"
    );
}

#[test]
fn the_docker_cli_client_is_pinned_client_only_and_replaces_the_shim() {
    // Arrange
    let tasks = repo::tasks(PB);
    let install = play("Install Podman");
    let remove = task(
        &tasks,
        "Remove the podman-docker shim, which the Docker CLI client replaces",
    );
    let pinned = task(
        &tasks,
        "Install the pinned Docker CLI client, and nothing of the Docker engine",
    );
    let verify = task(
        &tasks,
        "Require Docker's CLI client at the pinned version, and no Docker engine",
    );

    // Act
    let removed = field(remove, "ansible.builtin.apt", "name");
    let removed_state = field(remove, "ansible.builtin.apt", "state");
    let removed_when = remove.get("when").and_then(Node::as_str);
    let pin_name = field(pinned, "ansible.builtin.apt", "name");
    let downgrade = module(pinned, "ansible.builtin.apt").get("allow_downgrade");
    let recommends = module(pinned, "ansible.builtin.apt").get("install_recommends");
    let version_var = install
        .get("vars")
        .and_then(|v| v.get("_docker_api_client_version"))
        .and_then(Node::as_str)
        .unwrap_or_default();
    let conditions = that_list(verify);

    // Assert
    assert_eq!(
        removed,
        Some("podman-docker"),
        "the shim is not removed by name"
    );
    assert_eq!(
        removed_state,
        Some("absent"),
        "the shim removal is not state: absent"
    );
    assert_eq!(
        removed_when,
        Some("_docker_api_client"),
        "the shim removal is not gated on the client opt-in"
    );
    assert_eq!(
        pin_name,
        Some("docker-ce-cli=5:{{ _docker_api_client_version }}-*"),
        "the client is not pinned to the declared version"
    );
    assert_eq!(
        downgrade,
        Some(&Node::Bool(true)),
        "a newer client is not moved back to the pin"
    );
    assert_eq!(
        recommends,
        Some(&Node::Bool(false)),
        "the client pulls its Recommends"
    );
    assert!(
        version_var.starts_with("{{ podman_docker_api_client_version | default('"),
        "the client version is not a declared default: {version_var:?}"
    );
    // No Docker engine: no leaf task installs docker-ce, containerd.io or docker.io.
    for installer in tasks.iter().filter(|t| is_task(t)) {
        for package in apt_names(installer) {
            assert!(
                !matches!(package, "docker-ce" | "containerd.io" | "docker.io"),
                "{:?} installs the Docker engine package {package}",
                name_of(installer)
            );
        }
    }
    assert!(
        conditions
            .iter()
            .any(|c| c.contains("'owner=docker-ce-cli' in _docker_api_client_state.stdout_lines")),
        "the verify does not read the owner of /usr/bin/docker: {conditions:?}"
    );
    assert!(
        conditions
            .iter()
            .any(|c| c.contains("select('match', '^engine=')")),
        "the verify does not reject a Docker engine: {conditions:?}"
    );
    // The removal comes before the install, so the swap does not rest on apt's conflict resolution.
    assert!(
        position(&tasks, "Remove the podman-docker shim")
            < position(&tasks, "Install the pinned Docker CLI client"),
        "the client installs before the shim is removed"
    );
}

#[test]
fn the_docker_apt_repository_is_one_shared_task_used_by_both_playbooks() {
    // Arrange
    let repository = repo::tasks(ADD_DOCKER_REPO);
    let install_podman = repo::tasks(PB);
    let install_docker = repo::tasks(INSTALL_DOCKER);

    // Act
    let signed = repository.iter().any(|t| {
        field(t, "ansible.builtin.apt_repository", "repo")
            .is_some_and(|r| r.contains("signed-by=/etc/apt/keyrings/docker.asc"))
    });
    let key = repository.iter().any(|t| {
        field(t, "ansible.builtin.get_url", "url")
            == Some("https://download.docker.com/linux/ubuntu/gpg")
    });

    // Assert
    assert!(
        signed,
        "the shared repository is not signed by Docker's key"
    );
    assert!(
        key,
        "the shared repository does not fetch Docker's published key"
    );
    assert!(
        install_podman
            .iter()
            .any(|t| includes(t) == Some("tasks/add-docker-apt-repo.yml")),
        "install-podman does not include the shared repository task"
    );
    assert!(
        install_docker
            .iter()
            .any(|t| includes(t) == Some("tasks/add-docker-apt-repo.yml")),
        "install-docker does not include the shared repository task"
    );
    // Declared once: neither playbook carries its own copy of the source.
    assert!(
        !text(&doc(PB)).contains("download.docker.com"),
        "install-podman carries its own copy of the Docker repository"
    );
    assert!(
        !text(&doc(INSTALL_DOCKER)).contains("download.docker.com"),
        "install-docker carries its own copy of the Docker repository"
    );
}
