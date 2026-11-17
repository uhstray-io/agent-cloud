//! The seed harness's helpers (`src/seed.rs`), the port of `seed_harness.py` (AC-950), checked
//! against a fake server. The seed playbook runs are in `support_seed_playbook` (playbook tier).

use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::panic::{AssertUnwindSafe, catch_unwind};
use std::time::Duration;

use agent_cloud_tests::http::{Mock, Server, matchers};
use agent_cloud_tests::seed::{self, LOGIN, NEVER_PRINTED, ROLE_ID, ROLE_SECRET};
use serde_json::{Value, json};

/// One raw HTTP/1.1 request with `Connection: close`; returns the whole response text.
fn send(addr: SocketAddr, method: &str, target: &str, body: &[u8]) -> String {
    let mut stream = TcpStream::connect_timeout(&addr, Duration::from_secs(5)).unwrap();
    stream
        .set_read_timeout(Some(Duration::from_secs(10)))
        .unwrap();
    let head = format!(
        "{method} {target} HTTP/1.1\r\nHost: test\r\nConnection: close\r\nContent-Length: {}\r\n\r\n",
        body.len()
    );
    stream.write_all(head.as_bytes()).unwrap();
    stream.write_all(body).unwrap();
    let mut response = Vec::new();
    stream.read_to_end(&mut response).unwrap();
    String::from_utf8_lossy(&response).into_owned()
}

/// The message of a caught panic.
fn panic_message(payload: &(dyn std::any::Any + Send)) -> String {
    payload
        .downcast_ref::<String>()
        .cloned()
        .or_else(|| payload.downcast_ref::<&str>().map(|s| (*s).to_owned()))
        .unwrap_or_default()
}

#[test]
fn record_keeps_the_method_and_the_path_with_its_query_in_arrival_order() {
    let server = Server::start();

    send(server.addr(), "GET", "/v1/secret/data/x?version=2", b"");
    send(server.addr(), "PATCH", "/v1/secret/metadata/x", b"{}");
    send(server.addr(), "POST", "/v1/auth/approle/login", b"{}");

    assert_eq!(
        seed::recorded(&server),
        vec![
            ("GET".to_owned(), "/v1/secret/data/x?version=2".to_owned()),
            ("PATCH".to_owned(), "/v1/secret/metadata/x".to_owned()),
            ("POST".to_owned(), "/v1/auth/approle/login".to_owned()),
        ]
    );
}

#[test]
fn writes_counts_a_patch_a_put_and_a_post_under_secret_and_nothing_else() {
    // (method, path, whether `writes` counts it): the seed_harness.writes rule.
    let cases = [
        ("PATCH", "/v1/secret/metadata/x", true),
        ("PATCH", "/v1/anything", true),
        ("PUT", "/v1/sys/policies/acl/x", true),
        ("POST", "/v1/secret/data/x", true),
        ("POST", "/v1/secret/data/x?cas=1", true),
        ("POST", "/v1/auth/approle/login", false),
        ("GET", "/v1/secret/data/x", false),
        ("DELETE", "/v1/secret/data/x", false),
    ];
    for (method, path, expected) in cases {
        let counted = !seed::writes(&[(method.to_owned(), path.to_owned())]).is_empty();
        assert_eq!(counted, expected, "writes() on {method} {path}");
    }
}

#[test]
fn json_body_is_empty_for_no_body_and_the_object_otherwise() {
    let server = Server::start();
    send(server.addr(), "GET", "/none", b"");
    send(
        server.addr(),
        "POST",
        "/obj",
        format!(r#"{{"role_id":"{ROLE_ID}","n":2}}"#).as_bytes(),
    );

    let requests = server.requests();
    assert_eq!(seed::json_body(&requests[0]), json!({}));
    assert_eq!(
        seed::json_body(&requests[1]),
        json!({"role_id": ROLE_ID, "n": 2})
    );
}

#[test]
fn json_body_refuses_a_body_that_is_not_json_and_never_echoes_it() {
    let server = Server::start();
    send(server.addr(), "POST", "/bad", b"not-json-synthetic-body");

    let requests = server.requests();
    let outcome = catch_unwind(AssertUnwindSafe(|| seed::json_body(&requests[0])));

    let message = panic_message(
        outcome
            .expect_err("a body that is not JSON panics")
            .as_ref(),
    );
    assert!(message.contains("POST /bad"), "{message}");
    assert!(
        !message.contains("not-json-synthetic-body"),
        "the panic echoed the body: {message}"
    );
}

#[test]
fn reply_sets_the_status_the_json_body_and_the_json_content_type() {
    let server = Server::start();
    let issued = json!({"auth": {"client_token": LOGIN}});
    server.mount(
        Mock::given(matchers::path("/v1/auth/approle/login"))
            .respond_with(seed::reply(201, &issued)),
    );

    let response = send(server.addr(), "POST", "/v1/auth/approle/login", b"{}");

    let (head, body) = response
        .split_once("\r\n\r\n")
        .expect("a response with a head");
    assert!(head.starts_with("HTTP/1.1 201 "), "{head}");
    assert!(
        head.lines()
            .any(|line| line.eq_ignore_ascii_case("content-type: application/json")),
        "{head}"
    );
    assert_eq!(serde_json::from_str::<Value>(body).unwrap(), issued);
}

#[test]
fn the_role_and_the_never_printed_values_are_the_python_ones() {
    let role = seed::role();
    assert_eq!(role.len(), 2);
    assert_eq!(role["bao_role_id"], json!("synthetic-role"));
    assert_eq!(role["bao_secret_id"], json!("synthetic-role-secret"));

    assert_eq!(ROLE_ID, "synthetic-role");
    assert_eq!(ROLE_SECRET, "synthetic-role-secret");
    assert_eq!(LOGIN, "synthetic-login-value");
    assert_eq!(
        NEVER_PRINTED,
        [
            "synthetic-role",
            "synthetic-role-secret",
            "synthetic-login-value",
            "synthetic-leftover-value",
        ]
    );
}

#[test]
fn assert_never_printed_names_the_entry_that_leaked_and_never_the_value() {
    let output = format!("debug: {ROLE_SECRET} and more text");

    let outcome = catch_unwind(AssertUnwindSafe(|| {
        seed::assert_never_printed(&output, &[])
    }));

    let message = panic_message(outcome.expect_err("a printed secret panics").as_ref());
    assert!(message.contains("NEVER_PRINTED[1]"), "{message}");
    assert!(!message.contains(ROLE_SECRET), "{message}");
    assert!(
        !message.contains("more text"),
        "the panic echoed the output: {message}"
    );
}

#[test]
fn assert_never_printed_names_a_named_value_that_leaked_by_its_name() {
    let outcome = catch_unwind(AssertUnwindSafe(|| {
        seed::assert_never_printed(
            "prints synthetic-extra-value",
            &[("SEED_EXTRA", "synthetic-extra-value")],
        )
    }));

    let message = panic_message(outcome.expect_err("a printed input panics").as_ref());
    assert!(message.contains("`SEED_EXTRA`"), "{message}");
    assert!(!message.contains("synthetic-extra-value"), "{message}");
}

#[test]
fn assert_never_printed_passes_output_that_holds_no_value() {
    seed::assert_never_printed(
        "nothing secret is printed here",
        &[("SEED_EXTRA", "synthetic-extra-value")],
    );
}
