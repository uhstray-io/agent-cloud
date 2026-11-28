//! The static tier of the local-dev mail catcher (`platform/services/mailpit`, AC-1123).
//!
//! - The shared Semaphore catalog holds no mailpit template: the catcher exists on local-dev only.
//! - The local catalog holds `Deploy mailpit (Local)`, and its playbook is `deploy-mailpit.yml`, which exists.
//! - The mailpit route is one host and one upstream in both copies: bootstrap-local-dev.yml's `_caddy_routes` and
//!   local-dev.yml.example. The host is `mailpit.<zone>`, the zone is the one bootstrap's `_dev_zone` defaults to, and
//!   the upstream is `mailpit:8025`. The two copies are written by hand, and no other suite module compares them.
//! - The `mailpit_svc` group holds one host, `mailpit-local`, on the local connection, in both copies.
//! - The shared task `assert-mail-captured.yml` reads the catcher with `check_mode: false`, sets no `no_log`, and
//!   names every register and set_fact with a leading `_`.
//! - `compose.yml` pins its image: fully qualified, with a sha256 digest, and it runs as `65534:65534`.
//!
//! Structural only: the files are read as text and as YAML. No container starts and no playbook runs here; the
//! playbook tier (`assert_mail_captured_playbook.rs`) runs the shared task.

use agent_cloud_tests::{playbook, repo, text};
use regex::Regex;
use serde_json::{Value, json};

const TEMPLATES: &str = "platform/semaphore/templates.yml";
const TEMPLATES_LOCAL: &str = "platform/semaphore/templates-local.yml";
const BOOTSTRAP: &str = "platform/playbooks/bootstrap-local-dev.yml";
const EXAMPLE: &str = "platform/inventory/local-dev.yml.example";
const DEPLOY: &str = "platform/playbooks/deploy-mailpit.yml";
const COMPOSE: &str = "platform/services/mailpit/deployment/compose.yml";
const TASKS: &str = "platform/playbooks/tasks/assert-mail-captured.yml";
const UPSTREAM: &str = "mailpit:8025";

/// A YAML file as a JSON value.
fn doc(rel: &str) -> Value {
    playbook::loads(&text::read(rel))
}

/// The first value under `key` anywhere in `v`, searched depth first.
fn find_key<'a>(v: &'a Value, key: &str) -> Option<&'a Value> {
    match v {
        Value::Object(map) => map
            .get(key)
            .or_else(|| map.values().find_map(|child| find_key(child, key))),
        Value::Array(items) => items.iter().find_map(|child| find_key(child, key)),
        _ => None,
    }
}

/// The (host, upstream) pair of every route object under `v` whose host begins `mailpit.`.
fn mailpit_routes(v: &Value) -> Vec<(String, String)> {
    let mut found = Vec::new();
    collect_mailpit_routes(v, &mut found);
    found
}

fn collect_mailpit_routes(v: &Value, found: &mut Vec<(String, String)>) {
    match v {
        Value::Object(map) => {
            let host = map.get("host").and_then(Value::as_str).unwrap_or_default();
            if host.starts_with("mailpit.") {
                let upstream = map
                    .get("upstream")
                    .and_then(Value::as_str)
                    .unwrap_or_default();
                found.push((host.to_owned(), upstream.to_owned()));
            }
            for child in map.values() {
                collect_mailpit_routes(child, found);
            }
        }
        Value::Array(items) => {
            for child in items {
                collect_mailpit_routes(child, found);
            }
        }
        _ => {}
    }
}

/// The arguments of `task`'s module `short`, under its `ansible.builtin.` or short name, or None when it is another module.
fn module<'a>(task: &'a Value, short: &str) -> Option<&'a Value> {
    task.get(format!("ansible.builtin.{short}"))
        .or_else(|| task.get(short))
}

#[test]
fn the_shared_catalog_holds_no_mailpit_template() {
    // Assert: the catcher is local-dev only, so no template for it may sit in the shared catalog.
    let shared = text::read(TEMPLATES).to_lowercase();
    assert!(
        !shared.contains("mailpit"),
        "{TEMPLATES} names mailpit; the catcher is local-dev only"
    );
}

#[test]
fn the_local_catalog_deploys_mailpit_with_its_playbook() {
    // Arrange
    let catalog = doc(TEMPLATES_LOCAL);
    let templates = catalog["templates"].as_array().expect("a templates list");

    // Act
    let deploy: Vec<&Value> = templates
        .iter()
        .filter(|t| t["name"] == "Deploy mailpit (Local)")
        .collect();

    // Assert: one such template, running the deploy playbook, and that playbook exists.
    assert_eq!(deploy.len(), 1, "the deploy template, once: {deploy:?}");
    assert_eq!(deploy[0]["playbook"], DEPLOY);
    assert!(repo::path(DEPLOY).is_file(), "{DEPLOY} does not exist");
}

#[test]
fn the_mailpit_route_is_the_same_host_and_upstream_in_both_copies() {
    // Arrange: bootstrap's copy is the `_caddy_routes` list; the example's copy is its `caddy_routes` list.
    let bootstrap_doc = doc(BOOTSTRAP);
    let bootstrap = find_key(&bootstrap_doc, "_caddy_routes").expect("bootstrap _caddy_routes");
    let example_doc = doc(EXAMPLE);
    let example = find_key(&example_doc, "caddy_routes").expect("example caddy_routes");

    // The zone bootstrap's `_dev_zone` defaults to: the host it builds resolves under this zone.
    let zone = Regex::new(r#"_dev_zone: "\{\{ dev_zone \| default\('([^']+)'\) \}\}""#)
        .expect("the zone pattern compiles")
        .captures(&text::read(BOOTSTRAP))
        .expect("bootstrap's _dev_zone default")[1]
        .to_owned();

    // Act
    let bootstrap_routes = mailpit_routes(bootstrap);
    let example_routes = mailpit_routes(example);

    // Assert: one route in each copy, with the same label, the same zone and the same upstream.
    assert_eq!(
        bootstrap_routes,
        vec![("mailpit.{{ _dev_zone }}".to_owned(), UPSTREAM.to_owned())],
        "bootstrap-local-dev.yml _caddy_routes"
    );
    assert_eq!(
        example_routes,
        vec![(format!("mailpit.{zone}"), UPSTREAM.to_owned())],
        "local-dev.yml.example caddy_routes (zone {zone})"
    );
}

#[test]
fn the_mailpit_group_holds_one_local_host_in_both_copies() {
    // The example: the group's host map holds exactly mailpit-local, on the local connection.
    let example_doc = doc(EXAMPLE);
    let group = find_key(&example_doc, "mailpit_svc").expect("example mailpit_svc group");
    let hosts = group["hosts"].as_object().expect("the group's hosts");
    assert_eq!(
        hosts.keys().map(String::as_str).collect::<Vec<_>>(),
        vec!["mailpit-local"],
        "example mailpit_svc hosts"
    );
    assert_eq!(hosts["mailpit-local"]["ansible_connection"], "local");

    // Bootstrap's copy is an INI block inside its inventory text: one header, and one host line under it.
    let bootstrap_text = text::read(BOOTSTRAP);
    let lines: Vec<&str> = bootstrap_text.lines().map(str::trim).collect();
    assert_eq!(
        lines.iter().filter(|l| **l == "[mailpit_svc]").count(),
        1,
        "bootstrap: one [mailpit_svc] header"
    );
    let start = lines
        .iter()
        .position(|l| *l == "[mailpit_svc]")
        .expect("the header");
    let hosts: Vec<&str> = lines[start + 1..]
        .iter()
        .take_while(|l| !l.is_empty() && !l.starts_with('['))
        .filter(|l| !l.starts_with('#'))
        .copied()
        .collect();
    assert_eq!(
        hosts.len(),
        1,
        "bootstrap: one host under [mailpit_svc]: {hosts:?}"
    );
    let mut fields = hosts[0].split_whitespace();
    assert_eq!(fields.next(), Some("mailpit-local"), "bootstrap host name");
    assert!(
        hosts[0]
            .split_whitespace()
            .any(|f| f == "ansible_connection=local"),
        "bootstrap host is not on the local connection: {}",
        hosts[0]
    );
}

#[test]
fn the_shared_task_reads_with_check_mode_false_and_never_logs_and_prefixes_its_internals() {
    // Arrange
    let doc = playbook::loads(&text::read(TASKS));
    let top = doc.as_array().expect("a task list");
    // A block's own vars are internals too; its tasks are checked like top-level ones.
    let mut tasks: Vec<&Value> = Vec::new();
    for task in top {
        tasks.push(task);
        if let Some(inner) = task.get("block").and_then(Value::as_array) {
            for key in task["vars"]
                .as_object()
                .into_iter()
                .flat_map(|vars| vars.keys())
            {
                assert!(key.starts_with('_'), "block var {key} is not `_`-prefixed");
            }
            tasks.extend(inner);
        }
    }

    // Act + Assert: the one read sets check_mode false; no task sets no_log; each register and set_fact key is `_`-prefixed.
    let mut reads = 0;
    for task in tasks {
        let name = task["name"].as_str().unwrap_or("<unnamed>");
        assert!(
            task.get("no_log").is_none(),
            "{name}: sets no_log; only a credential task does"
        );
        if module(task, "uri").is_some() {
            reads += 1;
            assert_eq!(
                task["check_mode"],
                json!(false),
                "{name}: a uri read must set check_mode: false"
            );
        }
        if let Some(args) = module(task, "set_fact") {
            for key in args.as_object().expect("set_fact arguments").keys() {
                assert!(
                    key.starts_with('_'),
                    "{name}: set_fact {key} is not `_`-prefixed"
                );
            }
        }
        if let Some(register) = task.get("register").and_then(Value::as_str) {
            assert!(
                register.starts_with('_'),
                "{name}: register {register} is not `_`-prefixed"
            );
        }
    }
    assert_eq!(reads, 1, "the task holds one uri read of the catcher");
}

#[test]
fn compose_pins_a_fully_qualified_digest_and_runs_as_nobody() {
    // Arrange
    let compose = doc(COMPOSE);
    let service = &compose["services"]["mailpit"];
    let image = service["image"].as_str().expect("the mailpit image");

    // Act: the image default is a registry host (with a dot), a path, a tag, and a sha256 digest.
    let pinned = Regex::new(
        r"^\$\{MAILPIT_IMAGE:-[a-z0-9.-]+\.[a-z]+/[a-z0-9._/-]+:[A-Za-z0-9._-]+@sha256:[0-9a-f]{64}\}$",
    )
    .expect("the image pattern compiles");

    // Assert
    assert!(
        pinned.is_match(image),
        "the image is not fully qualified and digest-pinned: {image}"
    );
    assert_eq!(service["user"], "65534:65534", "the container user");
}
