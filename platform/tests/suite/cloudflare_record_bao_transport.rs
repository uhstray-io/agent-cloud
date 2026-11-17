//! The Cloudflare record task checks the OpenBao transport before it sends the AppRole
//! credentials (static tier).
//!
//! `platform/playbooks/tasks/manage-cloudflare-record.yml` logs in to OpenBao with the AppRole
//! `role_id` and `secret_id`, then reads `secret/services/caddy` with the client token. Until
//! AC-936 neither it nor its caller included `tasks/assert-bao-transport.yml`, so nothing
//! refused a public `http://` OpenBao address before the credentials crossed it. The guard now
//! sits in the shared task file, so every caller is covered.
//!
//! A request to OpenBao is selected by its shape, not by a variable name: a `uri` task whose URL
//! holds `/v1/auth/` (a login) or whose headers carry `X-Vault-Token`.

use agent_cloud_tests::repo;
use agent_cloud_tests::yaml::Node;

const TASK_FILE: &str = "platform/playbooks/tasks/manage-cloudflare-record.yml";
const GUARD: &str = "assert-bao-transport.yml";

/// The `uri` module arguments of a task, under either module name.
fn uri(task: &Node) -> Option<&Node> {
    task.get("ansible.builtin.uri").or_else(|| task.get("uri"))
}

/// A task that sends a request to OpenBao: a login, or any request carrying a client token.
fn sends_to_openbao(task: &Node) -> bool {
    let Some(args) = uri(task) else {
        return false;
    };
    let url = args.get("url").and_then(Node::as_str).unwrap_or_default();
    let token_header = args
        .get("headers")
        .and_then(Node::as_map)
        .is_some_and(|headers| {
            headers
                .iter()
                .any(|(k, _)| k.as_str() == Some("X-Vault-Token"))
        });
    url.contains("/v1/auth/") || token_header
}

/// A task that includes the transport guard.
fn is_guard(task: &Node) -> bool {
    [
        "ansible.builtin.include_tasks",
        "include_tasks",
        "ansible.builtin.import_tasks",
        "import_tasks",
    ]
    .iter()
    .filter_map(|key| task.get(key).and_then(Node::as_str))
    .any(|file| file.trim_end().ends_with(GUARD))
}

/// The variable name inside a whole-value `{{ name }}` expression.
fn bare_variable(value: &str) -> Option<&str> {
    let inner = value.trim().strip_prefix("{{")?.strip_suffix("}}")?.trim();
    (!inner.is_empty() && inner.chars().all(|c| c.is_ascii_alphanumeric() || c == '_'))
        .then_some(inner)
}

fn name(task: &Node) -> &str {
    task.get("name")
        .and_then(Node::as_str)
        .unwrap_or("<unnamed>")
}

#[test]
fn the_transport_guard_runs_before_every_openbao_request() {
    // Arrange
    let tasks = repo::tasks(TASK_FILE);

    // Act
    let guard = tasks.iter().position(is_guard);
    let requests: Vec<(usize, &Node)> = tasks
        .iter()
        .enumerate()
        .filter(|(_, task)| sends_to_openbao(task))
        .collect();

    // Assert
    assert!(!requests.is_empty(), "{TASK_FILE} sends no OpenBao request");
    let guard = guard.unwrap_or_else(|| panic!("{TASK_FILE} does not include {GUARD}"));
    let early: Vec<&str> = requests
        .iter()
        .filter(|(index, _)| *index < guard)
        .map(|(_, task)| name(task))
        .collect();
    assert!(
        early.is_empty(),
        "these OpenBao requests run before the transport guard: {early:?}"
    );
}

#[test]
fn every_openbao_request_uses_the_url_the_guard_checked() {
    // Arrange
    let tasks = repo::tasks(TASK_FILE);
    let guard = tasks
        .iter()
        .find(|task| is_guard(task))
        .unwrap_or_else(|| panic!("{TASK_FILE} does not include {GUARD}"));

    // Act
    let checked = guard
        .get("vars")
        .and_then(|vars| vars.get("_assert_bao_url"))
        .and_then(Node::as_str)
        .and_then(bare_variable)
        .unwrap_or_else(|| panic!("the guard's _assert_bao_url is not one `{{{{ variable }}}}`"));
    let prefix = format!("{{{{ {checked} }}}}/");
    let elsewhere: Vec<&str> = tasks
        .iter()
        .filter(|task| sends_to_openbao(task))
        .filter(|task| {
            !uri(task)
                .and_then(|args| args.get("url"))
                .and_then(Node::as_str)
                .is_some_and(|url| url.starts_with(&prefix))
        })
        .map(name)
        .collect();

    // Assert
    assert!(
        elsewhere.is_empty(),
        "these OpenBao requests do not use the URL the guard checked ({prefix}...): {elsewhere:?}"
    );
}

#[test]
fn the_guard_runs_whenever_an_openbao_request_runs() {
    // Arrange: a guard skipped by its own `when` while a request runs would check nothing.
    let tasks = repo::tasks(TASK_FILE);
    let guard = tasks
        .iter()
        .find(|task| is_guard(task))
        .unwrap_or_else(|| panic!("{TASK_FILE} does not include {GUARD}"));
    let guard_when = guard.get("when");

    // Act
    let unmatched: Vec<&str> = tasks
        .iter()
        .filter(|task| sends_to_openbao(task) && task.get("when") != guard_when)
        .map(name)
        .collect();

    // Assert
    assert!(
        unmatched.is_empty(),
        "these OpenBao requests run under a different `when` than the guard: {unmatched:?}"
    );
}

#[test]
fn a_bare_variable_is_read_and_an_expression_is_not() {
    assert_eq!(bare_variable("{{ _cf_bao_url }}"), Some("_cf_bao_url"));
    assert_eq!(bare_variable("{{_cf_bao_url}}"), Some("_cf_bao_url"));
    assert_eq!(bare_variable("{{ openbao_addr | default('') }}"), None);
    assert_eq!(bare_variable("_cf_bao_url"), None);
}
