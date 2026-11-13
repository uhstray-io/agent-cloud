//! `manage-github-runner-group.yml` converges the organisation's self-hosted runner group to its
//! declared list of private repositories (security set, static tier: it is `always` in areas.yml).
//!
//! This playbook IS the control that keeps the self-hosted plane away from public repositories.
//! The organisation's pre-existing default group has visibility "all", so the failure mode is
//! concrete and close by, not hypothetical. Convergence replaces the access list, the group is
//! created selected, and a dry run is the default.
//!
//! Replaces platform/tests/test_manage_github_runner_group.bats (plan 24, Phase 4).
//!
//! Port map (every BATS `@test` once):
//!
//! | BATS @test | Rust fn |
//! |---|---|
//! | runner-group: Semaphore requires the App client id survey input | `semaphore_requires_the_app_client_id_survey_input` |
//! | runner-group: a public repository in the declaration aborts the run | `a_public_repository_in_the_declaration_aborts_the_run` |
//! | runner-group: the group is created with selected visibility, never opened first | `the_group_is_created_with_selected_visibility_never_opened_first` |
//! | runner-group: convergence REPLACES the access list, so a stray grant is removed | `convergence_replaces_the_access_list_so_a_stray_grant_is_removed` |
//! | runner-group: dry run is the DEFAULT | `dry_run_is_the_default_and_gates_every_mutation` |
//! | runner-group: no other group is ever modified or deleted | `no_other_runner_group_is_ever_modified_or_deleted` |
//! | runner-group: the exact private-repo declaration includes site-config and excludes agent-cloud | `the_private_repository_declaration_is_exact` (candidate: pins text) |
//! | runner-group: an empty declaration is refused rather than silently converged | `an_empty_declaration_is_refused_rather_than_converged` |
//! | runner-group: no_log is scoped to the credential boundary only | `no_log_is_scoped_to_the_credential_boundary_only` |
//! | runner-group: the client secret is never referenced | `the_client_secret_is_never_referenced` |
//! | runner-group: the private key comes from the secret store, not an env secret | `the_private_key_comes_from_the_secret_store` |

use agent_cloud_tests::repo;
use agent_cloud_tests::yaml::{self, Node};
use regex::Regex;

const PLAYBOOK: &str = "platform/playbooks/manage-github-runner-group.yml";
const TEMPLATES: &str = "platform/semaphore/templates.yml";

/// The file's text, read at run time (only deny-lists and pinned prose read it).
fn read(rel: &str) -> String {
    std::fs::read_to_string(repo::path(rel)).unwrap_or_else(|e| panic!("{rel}: {e}"))
}

/// The plays of a playbook, in file order.
fn plays(rel: &str) -> Vec<Node> {
    yaml::load_one(&repo::path(rel))
        .as_ref()
        .and_then(Node::as_seq)
        .map(<[Node]>::to_vec)
        .unwrap_or_default()
}

/// The templates of `templates.yml`, in file order.
fn templates() -> Vec<Node> {
    yaml::load_one(&repo::path(TEMPLATES))
        .as_ref()
        .and_then(|doc| doc.get("templates"))
        .and_then(Node::as_seq)
        .map(<[Node]>::to_vec)
        .unwrap_or_default()
}

/// The template named exactly `wanted`; a missing template is a failed test.
fn template<'a>(all: &'a [Node], wanted: &str) -> &'a Node {
    all.iter()
        .find(|t| str_at(t, "name") == Some(wanted))
        .unwrap_or_else(|| panic!("no template named {wanted:?}"))
}

fn survey(template: &Node) -> &[Node] {
    template
        .get("survey_vars")
        .and_then(Node::as_seq)
        .unwrap_or_default()
}

fn name(node: &Node) -> &str {
    str_at(node, "name").unwrap_or("")
}

fn str_at<'a>(node: &'a Node, key: &str) -> Option<&'a str> {
    node.get(key).and_then(Node::as_str)
}

fn is_true(node: Option<&Node>) -> bool {
    matches!(node, Some(Node::Bool(true)))
}

/// The text items of a `when:` or `that:` value: one string, or a list of them.
fn texts(node: Option<&Node>) -> Vec<&str> {
    match node {
        Some(Node::Str(s)) => vec![s],
        Some(Node::Seq(items)) => items.iter().filter_map(Node::as_str).collect(),
        _ => vec![],
    }
}

/// Every string in a node, keys included.
fn strings(node: &Node) -> Vec<&str> {
    fn walk<'a>(node: &'a Node, out: &mut Vec<&'a str>) {
        match node {
            Node::Str(s) => out.push(s),
            Node::Seq(items) => items.iter().for_each(|item| walk(item, out)),
            Node::Map(entries) => {
                for (key, value) in entries {
                    walk(key, out);
                    walk(value, out);
                }
            }
            Node::Null | Node::Bool(_) => {}
        }
    }
    let mut out = vec![];
    walk(node, &mut out);
    out
}

/// The first task whose name contains `needle`. A missing step is a failed test.
fn named<'a>(items: &'a [Node], needle: &str) -> &'a Node {
    items
        .iter()
        .find(|item| name(item).contains(needle))
        .unwrap_or_else(|| panic!("no task named like {needle:?}"))
}

/// The arguments of module `short` in a task, under any spelling Ansible accepts for it: the
/// short name, `ansible.builtin.<short>` or `ansible.legacy.<short>`. A task written under one
/// spelling is the same module, so a check that reads only one spelling can pass on a task it
/// never saw.
fn module<'a>(task: &'a Node, short: &str) -> Option<&'a Node> {
    [
        short.to_owned(),
        format!("ansible.builtin.{short}"),
        format!("ansible.legacy.{short}"),
    ]
    .iter()
    .find_map(|key| task.get(key))
}

/// The `uri` arguments of a task, when it makes an HTTP request.
fn uri(task: &Node) -> Option<&Node> {
    module(task, "uri")
}

/// The `assert` `that:` items of a task.
fn asserts(task: &Node) -> Vec<&str> {
    texts(module(task, "assert").and_then(|check| check.get("that")))
}

#[test]
fn semaphore_requires_the_app_client_id_survey_input() {
    // Arrange
    let all = templates();
    let group = template(&all, "Manage GitHub Runner Group");

    // Act
    let client_id = survey(group)
        .iter()
        .find(|field| str_at(field, "name") == Some("github_app_client_id"));

    // Assert
    assert!(
        client_id.is_some_and(|field| is_true(field.get("required"))),
        "the runner-group App client id survey is missing or optional"
    );
}

#[test]
fn a_public_repository_in_the_declaration_aborts_the_run() {
    // Arrange
    let tasks = repo::tasks(PLAYBOOK);
    let abort = named(&tasks, "Abort if any declared repository is public");

    // Act
    let checks = asserts(abort);
    let message = module(abort, "assert")
        .and_then(|check| str_at(check, "fail_msg"))
        .unwrap_or_default();

    // Assert
    assert!(
        checks
            .iter()
            .any(|c| c.contains("selectattr('json.private', 'equalto', false)")),
        "the abort does not read each declared repository's visibility: {checks:?}"
    );
    assert!(
        message.contains("must not be granted self-hosted"),
        "the abort does not say a public repository is refused: {message}"
    );
}

#[test]
fn the_group_is_created_with_selected_visibility_never_opened_first() {
    // Arrange
    let tasks = repo::tasks(PLAYBOOK);
    let create = named(&tasks, "Create the group with selected visibility");
    let body = uri(create).and_then(|request| request.get("body"));

    // Act
    let visibility = body.and_then(|b| str_at(b, "visibility"));
    let public_repositories = body.and_then(|b| b.get("allows_public_repositories"));
    let opened_first = read(PLAYBOOK).contains("visibility: \"all\"");

    // Assert
    assert_eq!(
        visibility,
        Some("selected"),
        "the group is not created selected"
    );
    assert!(
        matches!(public_repositories, Some(Node::Bool(false))),
        "the group may admit public repositories: {public_repositories:?}"
    );
    assert!(
        !opened_first,
        "the playbook creates a group visible to every repository"
    );
}

#[test]
fn convergence_replaces_the_access_list_so_a_stray_grant_is_removed() {
    // Arrange
    let tasks = repo::tasks(PLAYBOOK);
    let replace = named(&tasks, "Replace the group's repository access list");

    // Act
    let method = uri(replace).and_then(|request| str_at(request, "method"));

    // Assert
    assert_eq!(
        method,
        Some("PUT"),
        "convergence adds grants and never removes one"
    );
}

#[test]
fn dry_run_is_the_default_and_gates_every_mutation() {
    // Arrange
    let plays = plays(PLAYBOOK);
    let tasks = repo::tasks(PLAYBOOK);

    // Act
    let dry_run = plays
        .iter()
        .filter_map(|play| play.get("vars"))
        .find_map(|vars| str_at(vars, "_dry_run"));
    let mutating: Vec<&Node> = tasks
        .iter()
        .filter(|task| {
            let method = uri(task).and_then(|request| str_at(request, "method"));
            matches!(method, Some("POST" | "PATCH" | "PUT"))
                && uri(task)
                    .and_then(|request| str_at(request, "url"))
                    .is_some_and(|url| url.contains("actions/runner-groups"))
        })
        .collect();
    let ungated: Vec<&str> = mutating
        .iter()
        .filter(|task| {
            !texts(task.get("when"))
                .iter()
                .any(|w| w.contains("not _dry_run"))
        })
        .map(|task| name(task))
        .collect();

    // Assert
    assert_eq!(
        dry_run,
        Some("{{ (dry_run | default(true) | bool) or ansible_check_mode }}"),
        "the playbook is not a dry run unless told otherwise"
    );
    assert!(!mutating.is_empty(), "no mutating task found");
    assert!(
        ungated.is_empty(),
        "mutations that run on a dry run: {ungated:?}"
    );
}

#[test]
fn no_other_runner_group_is_ever_modified_or_deleted() {
    // Arrange
    let text = read(PLAYBOOK);
    let tasks = repo::tasks(PLAYBOOK);
    let literal_id = Regex::new(r"runner-groups/[0-9]+").unwrap();

    // Act
    let deletes = text.contains("method: DELETE");
    let targets_own_group = tasks
        .iter()
        .flat_map(strings)
        .any(|s| s.contains("_existing.id"));
    let literal_group = literal_id.is_match(&text);

    // Assert
    assert!(!deletes, "the playbook deletes a runner group");
    assert!(
        targets_own_group,
        "no mutating URL names the group this run resolved"
    );
    assert!(
        !literal_group,
        "a mutating URL names a literal runner-group id"
    );
}

#[test]
fn the_private_repository_declaration_is_exact() {
    // Arrange
    let text = read(PLAYBOOK);
    let declared = plays(PLAYBOOK)
        .iter()
        .filter_map(|play| play.get("vars"))
        .find_map(|vars| str_at(vars, "_repos"))
        .unwrap_or_default()
        .to_owned();
    let agent_cloud_default = Regex::new(r"default\(\['.*agent-cloud").unwrap();

    // Act
    let admits_agent_cloud = text.lines().any(|line| agent_cloud_default.is_match(line));

    // Assert
    assert!(
        declared.contains(
            "github_runner_repos | default(['zerds', 'atlas', 'zerds-website', 'weft', 'scientific-business', 'site-config', 'skynet'])"
        ),
        "the declared private repositories changed: {declared}"
    );
    assert!(
        !admits_agent_cloud,
        "agent-cloud is in the default declaration; it is public"
    );
}

#[test]
fn an_empty_declaration_is_refused_rather_than_converged() {
    // Arrange
    let tasks = repo::tasks(PLAYBOOK);
    let validate = named(&tasks, "Validate the declaration");

    // Act
    let refuses_empty = asserts(validate)
        .iter()
        .any(|c| c.contains("_repos | length > 0"));

    // Assert
    assert!(
        refuses_empty,
        "an empty repository list converges the group to admitting nothing"
    );
}

#[test]
fn no_log_is_scoped_to_the_credential_boundary_only() {
    // Arrange
    let tasks = repo::tasks(PLAYBOOK);
    let report = named(&tasks, "Report the converged access list");

    // Act
    let hidden = tasks
        .iter()
        .filter(|task| is_true(task.get("no_log")))
        .count();
    let report_hidden = is_true(report.get("no_log"));

    // Assert
    assert_eq!(
        hidden, 5,
        "no_log must cover the five credential steps and nothing else"
    );
    assert!(
        !report_hidden,
        "the convergence report is hidden: it is the audit record"
    );
}

#[test]
fn the_client_secret_is_never_referenced() {
    // Arrange
    let secret = Regex::new(r"(?i)client_secret|GITHUB_APP_SECRET").unwrap();

    // Act
    let referenced = read(PLAYBOOK).lines().any(|line| secret.is_match(line));

    // Assert
    assert!(!referenced, "the playbook references the App client secret");
}

#[test]
fn the_private_key_comes_from_the_secret_store() {
    // Arrange
    let text = read(PLAYBOOK);
    let tasks = repo::tasks(PLAYBOOK);

    // Act
    let env_key = text.contains("GITHUB_APP_PEM");
    let names_the_key = tasks
        .iter()
        .flat_map(strings)
        .any(|s| s.contains("app_private_key"));
    let includes_guard = tasks.iter().any(|task| {
        module(task, "include_tasks").and_then(Node::as_str)
            == Some("tasks/assert-bao-transport.yml")
    });
    let readers: Vec<&Node> = tasks
        .iter()
        .filter(|task| {
            uri(task)
                .and_then(|request| str_at(request, "url"))
                .is_some_and(|url| url.contains("/v1/secret/data/services/github-runner"))
        })
        .collect();
    let read_with_token = readers.iter().any(|task| {
        uri(task)
            .and_then(|request| request.get("headers"))
            .and_then(|headers| headers.get("X-Vault-Token"))
            .is_some()
    });

    // Assert
    assert!(
        !env_key,
        "the App key arrives from a Semaphore environment secret"
    );
    assert!(names_the_key, "the playbook does not read app_private_key");
    assert!(
        includes_guard,
        "the key read is not behind the cleartext guard"
    );
    assert!(
        !readers.is_empty(),
        "no task reads the App key from the secret store"
    );
    assert!(
        read_with_token,
        "the secret-store read carries no vault token"
    );
}
