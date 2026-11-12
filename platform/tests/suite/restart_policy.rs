//! Containers must come back on their own after a host reboot (static tier).
//!
//! Podman has no daemon. At boot, `podman-restart.service` (system unit for rootful, user
//! unit for rootless) runs `podman start --all --filter restart-policy=always` on podman
//! 4.9.3, so it starts ONLY `restart: always` containers. A container declared
//! `unless-stopped` stays down: production OpenBao sat stopped for three days after the
//! 2026-09-19 reboot this way (docs/MISTAKES.md 10.15). Docker honours `always` too, so one
//! policy is correct on both engines.
//!
//! Ported from `platform/tests/test_restart_policy.bats` (plan 24, Phase 1 pilot).

use std::collections::BTreeMap;

use agent_cloud_tests::repo;
use agent_cloud_tests::yaml::{self, Node};

const COMPOSE_SPECS: [&str; 4] = [
    "platform/**/*compose*.yml",
    "platform/**/*compose*.yaml",
    "agents/**/*compose*.yml",
    "agents/**/*compose*.yaml",
];

const BASE_NAMES: [&str; 4] = [
    "compose.yml",
    "compose.yaml",
    "docker-compose.yml",
    "docker-compose.yaml",
];

/// One service's effective restart policy and labels after the overlays of its directory.
#[derive(Default)]
struct Effective {
    restart: Option<Node>,
    file: String,
    /// Values kept as YAML nodes: a bare `true` is a boolean, not the string "true".
    labels: BTreeMap<String, Node>,
}

/// Compose merges a directory's overlays over its base file. A service with no `restart`
/// key keeps Compose's default ("no" restart), labels merge by key.
fn effective_services(files: &[String]) -> BTreeMap<(String, String), Effective> {
    let mut by_dir: BTreeMap<String, Vec<(bool, String, Node)>> = BTreeMap::new();
    for file in files {
        let docs = yaml::load(&repo::path(file));
        // PyYAML's yaml.load refuses a stream of several documents; so does this.
        assert!(
            docs.len() <= 1,
            "{file}: {} YAML documents, expected one",
            docs.len()
        );
        let Some(doc) = docs.into_iter().next() else {
            continue;
        };
        // A task list with "compose" in its name is not a compose file.
        let Some(services) = doc
            .get("services")
            .filter(|s| s.as_map().is_some())
            .cloned()
        else {
            continue;
        };
        let (dir, name) = file.rsplit_once('/').unwrap_or(("", file));
        let is_overlay = !BASE_NAMES.contains(&name);
        by_dir
            .entry(dir.to_owned())
            .or_default()
            .push((is_overlay, file.clone(), services));
    }

    let mut out = BTreeMap::new();
    for (dir, mut docs) in by_dir {
        docs.sort_by(|a, b| (a.0, &a.1).cmp(&(b.0, &b.1))); // base first, then overlays by name
        for (_, file, services) in docs {
            for (name, svc) in services.as_map().unwrap_or_default() {
                // A bare `yes`/`no`/`null` service name is a YAML 1.1 boolean or null, not text.
                let Some(name) = name.as_str().map(str::to_owned) else {
                    panic!("{file}: service name {name:?} is not a string");
                };
                let entry: &mut Effective = out.entry((dir.clone(), name)).or_default();
                if svc.get("restart").is_some() || entry.file.is_empty() {
                    entry.restart = svc.get("restart").cloned();
                    entry.file = file.clone();
                }
                match svc.get("labels") {
                    Some(Node::Map(pairs)) => {
                        for (k, v) in pairs {
                            if let Some(k) = k.as_str() {
                                entry.labels.insert(k.to_owned(), v.clone());
                            }
                        }
                    }
                    Some(Node::Seq(items)) => {
                        for item in items {
                            if let Some((k, v)) = item.as_str().and_then(|s| s.split_once('=')) {
                                entry.labels.insert(k.to_owned(), Node::Str(v.to_owned()));
                            }
                        }
                    }
                    _ => {}
                }
            }
        }
    }
    out
}

#[test]
fn every_services_effective_policy_is_always_or_a_labelled_one_shot_no() {
    // Arrange
    let files = repo::ls_files(&COMPOSE_SPECS);
    assert!(
        files.len() > 10,
        "found only {} compose files; the glob is wrong",
        files.len()
    );
    let services = effective_services(&files);

    // Act
    let bad: Vec<String> = services
        .iter()
        .filter_map(|((_, name), eff)| {
            // Exactly the string "true", as the BATS original required (`str(v) != 'true'`): a
            // bare `true` is a YAML 1.1 boolean and is refused, like a bare `no` below.
            let labelled =
                eff.labels.get("agent-cloud.one-shot") == Some(&Node::Str("true".into()));
            match &eff.restart {
                Some(Node::Str(p)) if p == "always" => None,
                // "no" is for one-shot init containers, which must declare themselves: the
                // persistence check accepts "no" only from a labelled container that exited 0.
                Some(Node::Str(p)) if p == "no" && labelled => None,
                Some(Node::Str(p)) if p == "no" => Some(format!(
                    "{}: service {name}: restart 'no' without label agent-cloud.one-shot: \"true\"",
                    eff.file
                )),
                // A bare `no` is YAML 1.1 false to PyYAML and podman-compose, not the policy "no".
                other => Some(format!("{}: service {name}: restart={other:?}", eff.file)),
            }
        })
        .collect();

    // Assert
    assert!(
        bad.is_empty(),
        "services podman will not start at boot:\n{}",
        bad.join("\n")
    );
}

#[test]
fn no_container_is_started_with_restart_unless_stopped() {
    // This file and the BATS original name the flag, so both exclude themselves.
    let hits = repo::grep(
        "--restart[= ]+unless-stopped",
        &[
            "platform",
            "agents",
            "scripts",
            ":!platform/tests/test_restart_policy.bats",
            ":!platform/tests/suite/restart_policy.rs",
        ],
    );
    assert!(hits.is_empty(), "{}", hits.join("\n"));
}

#[test]
fn the_shared_preamble_includes_the_linger_task() {
    let tasks = repo::tasks("platform/playbooks/tasks/place-monorepo.yml");

    let includes_linger = tasks.iter().any(|t| {
        ["ansible.builtin.include_tasks", "include_tasks"]
            .iter()
            .any(|m| t.get(m).and_then(Node::as_str) == Some("enable-linger.yml"))
    });

    assert!(
        includes_linger,
        "place-monorepo.yml does not include_tasks enable-linger.yml"
    );
}

#[test]
fn the_linger_task_links_podmans_user_boot_unit_as_the_container_user() {
    // Linger alone starts an empty user manager: the unit ships disabled.
    let tasks = repo::tasks("platform/playbooks/tasks/enable-linger.yml");
    let link = tasks
        .iter()
        .find(|t| {
            t.get("ansible.builtin.file")
                .and_then(|f| f.get("src"))
                .and_then(Node::as_str)
                == Some("/usr/lib/systemd/user/podman-restart.service")
        })
        .expect("a file task linking /usr/lib/systemd/user/podman-restart.service");
    let block = tasks
        .iter()
        .find(|t| {
            t.get("block")
                .and_then(Node::as_seq)
                .is_some_and(|b| b.contains(link))
        })
        .expect("the link task sits in a block that sets the user and the wants directory");

    let file = link.get("ansible.builtin.file").unwrap();
    assert_eq!(file.get("state").and_then(Node::as_str), Some("link"));
    assert_eq!(
        file.get("dest").and_then(Node::as_str),
        Some("{{ _unit_wants_dir }}/podman-restart.service")
    );
    let wants = block
        .get("vars")
        .and_then(|v| v.get("_unit_wants_dir"))
        .and_then(Node::as_str)
        .unwrap_or_default();
    assert!(
        wants.ends_with("/.config/systemd/user/default.target.wants"),
        "_unit_wants_dir = {wants:?}"
    );
    // Owned by the container user, not root, so their systemd reads it.
    assert_eq!(
        block.get("become_user").and_then(Node::as_str),
        Some("{{ linger_user | default(ansible_user) }}")
    );
}
