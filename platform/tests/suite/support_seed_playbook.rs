//! The seed harness's runner (`src/seed.rs`, `run_seed`), checked by running seed playbooks
//! against a fake OpenBao: the playbook tier of `support_seed`. Each test here is an
//! ansible-playbook run, so the module keeps to three runs (plan 21, F1).

use std::panic::{AssertUnwindSafe, catch_unwind};

use agent_cloud_tests::http::{Mock, Server, matchers};
use agent_cloud_tests::playbook;
use agent_cloud_tests::seed::{self, LOGIN, ROLE_ID, ROLE_SECRET, RunOpts};
use serde_json::{Value, json};
use tempfile::TempDir;

/// One play on `localhost` with `tasks`, as the JSON a seed playbook is written in.
fn play(tasks: Value) -> Value {
    json!([{"hosts": "localhost", "gather_facts": false, "tasks": tasks}])
}

/// The message of a caught panic.
fn panic_message(payload: &(dyn std::any::Any + Send)) -> String {
    payload
        .downcast_ref::<String>()
        .cloned()
        .or_else(|| payload.downcast_ref::<&str>().map(|s| (*s).to_owned()))
        .unwrap_or_default()
}

/// A debug task that prints `msg`.
fn debug(msg: &str) -> Value {
    json!({"ansible.builtin.debug": {"msg": msg}})
}

#[test]
fn a_seed_run_logs_in_with_the_role_and_shows_the_inputs_and_the_inventory() {
    let server = Server::start();
    server.mount(
        Mock::given(matchers::method("POST"))
            .and(matchers::path("/v1/auth/approle/login"))
            .respond_with(seed::reply(200, &json!({"auth": {"client_token": LOGIN}}))),
    );
    let dir = TempDir::new().unwrap();
    let login = json!({
        "name": "log in to OpenBao",
        "ansible.builtin.uri": {
            "url": "{{ openbao_addr }}/v1/auth/approle/login",
            "method": "POST",
            "body_format": "json",
            "body": {"role_id": "{{ bao_role_id }}", "secret_id": "{{ bao_secret_id }}"},
        },
        "no_log": true,
    });
    let path = playbook::write(
        dir.path(),
        "login.json",
        &play(json!([
            login,
            debug("seed input length: {{ lookup('env', 'SEED_X') | length }}"),
            debug("marker: {{ seed_marker }}"),
        ])),
    );
    let opts = RunOpts {
        inputs: vec![("SEED_X".to_owned(), "synthetic-seed-input".to_owned())],
        inventory_vars: json!({"seed_marker": "synthetic-inventory-marker"}),
        ..RunOpts::default()
    };

    let (code, output) = seed::run_seed(dir.path(), &path, &server.url(), &opts);

    assert_eq!(code, 0, "{output}");
    assert_eq!(
        seed::recorded(&server),
        vec![("POST".to_owned(), "/v1/auth/approle/login".to_owned())]
    );
    let requests = server.requests();
    assert_eq!(
        seed::json_body(&requests[0]),
        json!({"role_id": ROLE_ID, "secret_id": ROLE_SECRET})
    );
    let length = "synthetic-seed-input".len();
    assert!(
        output.contains(&format!("seed input length: {length}")),
        "{output}"
    );
    assert!(
        output.contains("marker: synthetic-inventory-marker"),
        "{output}"
    );
}

#[test]
fn a_run_that_prints_a_secret_panics_naming_it_and_never_its_value() {
    let dir = TempDir::new().unwrap();
    let path = playbook::write(
        dir.path(),
        "leak.json",
        &play(json!([
            debug("{{ bao_secret_id }}"),
            debug("{{ lookup('env', 'SEED_LEAK') }}"),
        ])),
    );
    let opts = RunOpts {
        inputs: vec![(
            "SEED_LEAK".to_owned(),
            "synthetic-seed-leak-value".to_owned(),
        )],
        ..RunOpts::default()
    };

    // The play makes no request, so the address is never dialled and no server is needed.
    let outcome = catch_unwind(AssertUnwindSafe(|| {
        seed::run_seed(dir.path(), &path, "http://127.0.0.1:9", &opts)
    }));

    let message = panic_message(outcome.expect_err("a printed secret must panic").as_ref());
    // ROLE_ID is a substring of ROLE_SECRET, so both entries are named.
    assert!(message.contains("NEVER_PRINTED[1]"), "{message}");
    assert!(message.contains("input `SEED_LEAK`"), "{message}");
    assert!(!message.contains(ROLE_SECRET), "{message}");
    assert!(!message.contains("synthetic-seed-leak-value"), "{message}");
}

#[test]
fn an_undeclared_run_leaves_openbao_addr_undefined_and_takes_the_environment_address() {
    let dir = TempDir::new().unwrap();
    let path = playbook::write(
        dir.path(),
        "undeclared.json",
        &play(json!([
            debug("declared: {{ openbao_addr | default('UNDEFINED') }}"),
            debug("environment: {{ lookup('env', 'OPENBAO_ADDR') }}"),
        ])),
    );
    // The address is not used: an undeclared inventory declares none.
    let opts = RunOpts {
        undeclared: true,
        env_addr: Some("synthetic-env-address".to_owned()),
        ..RunOpts::default()
    };

    let (code, output) = seed::run_seed(dir.path(), &path, "synthetic-unused-address", &opts);

    assert_eq!(code, 0, "{output}");
    assert!(output.contains("declared: UNDEFINED"), "{output}");
    assert!(
        output.contains("environment: synthetic-env-address"),
        "{output}"
    );
}
