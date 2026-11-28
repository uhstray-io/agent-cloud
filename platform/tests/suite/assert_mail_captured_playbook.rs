//! The playbook tier of `platform/playbooks/tasks/assert-mail-captured.yml` (AC-1123).
//!
//! - A read that finds no message to the recipient with the subject fails the run, and the failure names the recipient,
//!   the subject, the catcher's URL, the HTTP status and the count of messages read. A message list that is `null`, or
//!   a message with no `To`, is read as no match, not as a template error.
//! - A message to the recipient with the subject passes on one read. The recipient matches any `To` address, ignoring
//!   case. The subject matches as text, so a `+` in it is a plus sign, not a pattern.
//! - With `mail_captured_since` set, a message created before that second does not match, and one created at or after
//!   it does, to the second.
//! - A malformed input is refused before any read, and the refusal names the input.
//! - A message that arrives on a later read is found, within the retries.
//! - The read runs under `--check`, so a dry run also sees a message the catcher holds.
//!
//! Each test runs the task file, unchanged, through ansible-playbook on localhost against a fake Mailpit API
//! (`http::Server`). The task file is included from a wrapper play written to a temporary directory. Each test sets
//! one read and no delay, except the one that polls. The fixtures use `example.test` addresses and a run marker, and
//! no real address.

use agent_cloud_tests::http::{Mock, Server, matchers};
use agent_cloud_tests::{playbook, process, repo, seed};
use serde_json::{Value, json};
use tempfile::TempDir;

const TASKS: &str = "platform/playbooks/tasks/assert-mail-captured.yml";
const LIST: &str = "/api/v1/messages";
const RECIPIENT: &str = "x@example.test";
const SUBJECT: &str = "probe-ac1123-run-1";
const CREATED: &str = "2026-11-28T01:38:00.543Z";

/// One message as Mailpit's message list shows it (platform/services/mailpit, v1.34.0): the fields the task reads
/// and the rest of the shape.
fn message(id: &str, to: &Value, subject: &str, created: &str) -> Value {
    json!({"ID": id, "MessageID": format!("{id}@mailpit"), "Read": false,
           "From": {"Name": "", "Address": "sender@example.test"}, "To": to, "Cc": null, "Bcc": null,
           "ReplyTo": [], "Subject": subject, "Created": created, "Username": "", "Tags": [],
           "Size": 287, "Attachments": 0, "Snippet": "hi"})
}

/// A `To` list of one address.
fn to(address: &str) -> Value {
    json!([{"Name": "", "Address": address}])
}

/// The message that the default inputs look for: to RECIPIENT, with SUBJECT, created at CREATED.
fn matching(id: &str) -> Value {
    message(id, &to(RECIPIENT), SUBJECT, CREATED)
}

/// The list reply, with the counts Mailpit sends beside `messages`.
fn listing(messages: &Value) -> Value {
    let count = messages.as_array().map_or(0, Vec::len);
    json!({"total": count, "unread": count, "count": count, "messages_count": count,
           "messages_unread": count, "start": 0, "tags": [], "messages": messages})
}

/// Mounts a list reply with `messages` on every read.
fn mount_list(server: &Server, messages: &Value) {
    server.mount(
        Mock::given(matchers::method("GET"))
            .and(matchers::path(LIST))
            .respond_with(seed::reply(200, &listing(messages))),
    );
}

/// Every request as "METHOD path?query", in arrival order.
fn sent(server: &Server) -> Vec<String> {
    seed::recorded(server)
        .into_iter()
        .map(|(method, path)| format!("{method} {path}"))
        .collect()
}

/// The reads of the message list the server received.
fn reads(server: &Server) -> usize {
    seed::recorded(server)
        .iter()
        .filter(|(method, path)| method == "GET" && path.starts_with(LIST))
        .count()
}

/// The default inputs for `server`: one read and no delay, for RECIPIENT and SUBJECT.
fn inputs(server: &Server) -> Value {
    inputs_at(&server.url())
}

/// [`inputs`] for the catcher API at `url`.
fn inputs_at(url: &str) -> Value {
    json!({"mail_captured_api_url": url,
           "mail_captured_recipient": RECIPIENT,
           "mail_captured_subject": SUBJECT,
           "mail_captured_retries": 1,
           "mail_captured_delay": 0})
}

struct Run {
    ok: bool,
    out: String,
}

/// Runs the task file on localhost, included with `vars`, and adds `--check` when `check` is set.
fn run(vars: Value, check: bool) -> Run {
    let tmp = TempDir::new().unwrap();
    let inv = playbook::write(
        tmp.path(),
        "inv.json",
        &json!({"all": {"hosts": {"localhost": {"ansible_connection": "local"}}}}),
    );
    let play = json!([{
        "hosts": "localhost",
        "gather_facts": false,
        "connection": "local",
        "tasks": [{
            "name": "Assert a message reached the mail catcher",
            "ansible.builtin.include_tasks": repo::path(TASKS).display().to_string(),
            "vars": vars,
        }],
    }]);
    let path = playbook::write(tmp.path(), "play.json", &play);
    let mut cmd = process::ansible_playbook(
        tmp.path(),
        &inv.display().to_string(),
        &path.display().to_string(),
    );
    if check {
        cmd.arg("--check");
    }
    let output = process::run(&mut cmd);
    Run {
        ok: output.status.success(),
        out: process::output_text(&output),
    }
}

#[test]
fn no_message_to_the_recipient_fails_and_names_the_recipient_subject_and_read() {
    // Arrange: an empty list, and a `null` list, which a catcher with no messages may send.
    for messages in [json!([]), Value::Null] {
        let server = Server::start();
        mount_list(&server, &messages);

        // Act
        let out = run(inputs(&server), false);

        // Assert: red, naming the recipient and subject, the URL, the status and the zero count.
        assert!(!out.ok, "{}", out.out);
        assert!(
            out.out.contains(&format!(
                "No message to {RECIPIENT} with subject containing '{SUBJECT}'"
            )),
            "{}",
            out.out
        );
        assert!(
            out.out.contains(&format!(
                "arrived in the mail catcher at {} (HTTP 200, 0 messages read)",
                server.url()
            )),
            "{}",
            out.out
        );
    }
}

#[test]
fn a_message_with_no_to_address_is_no_match_and_does_not_break_the_check() {
    // Arrange: a message whose To is null, one whose To is empty, and one to another address.
    let server = Server::start();
    let messages = json!([
        message("m0", &Value::Null, SUBJECT, CREATED),
        message("m1", &json!([]), SUBJECT, CREATED),
        message("m2", &to("other@example.test"), SUBJECT, CREATED),
    ]);
    mount_list(&server, &messages);

    // Act
    let out = run(inputs(&server), false);

    // Assert: a plain failure to find the message, not a template error.
    assert!(!out.ok, "{}", out.out);
    assert!(
        out.out.contains("No message to x@example.test"),
        "{}",
        out.out
    );
    assert!(out.out.contains("3 messages read"), "{}", out.out);
}

#[test]
fn a_message_to_the_recipient_with_the_subject_passes_on_one_read() {
    // Arrange
    let server = Server::start();
    mount_list(&server, &json!([matching("m1")]));

    // Act
    let out = run(inputs(&server), false);

    // Assert: green, on exactly one read, naming the matched message's ID.
    assert!(out.ok, "{}", out.out);
    assert_eq!(reads(&server), 1, "{:?}", sent(&server));
    assert!(out.out.contains("the first has ID m1"), "{}", out.out);
}

#[test]
fn the_recipient_matches_any_to_address_ignoring_case() {
    // Arrange: a message with two To addresses, the second written in upper case.
    let server = Server::start();
    let two = json!([{"Name": "", "Address": "other@example.test"},
                     {"Name": "", "Address": "X@EXAMPLE.test"}]);
    mount_list(&server, &json!([message("m2", &two, SUBJECT, CREATED)]));

    // Act
    let out = run(inputs(&server), false);

    // Assert
    assert!(out.ok, "{}", out.out);
    assert_eq!(reads(&server), 1, "{:?}", sent(&server));
}

#[test]
fn a_message_to_another_recipient_or_with_another_subject_is_no_match() {
    // Arrange: the right subject to another address, and the right address with the previous run's marker.
    let server = Server::start();
    mount_list(
        &server,
        &json!([
            message("m1", &to("other@example.test"), SUBJECT, CREATED),
            message("m2", &to(RECIPIENT), "probe-ac1123-run-0", CREATED),
        ]),
    );

    // Act
    let out = run(inputs(&server), false);

    // Assert
    assert!(!out.ok, "{}", out.out);
    assert!(
        out.out.contains(&format!("No message to {RECIPIENT}")),
        "{}",
        out.out
    );
}

#[test]
fn the_subject_is_matched_as_text_not_as_a_pattern() {
    // Arrange: the subject "a+b", which a pattern would read as a repeat of "a". A message "aab" does not carry it,
    // and a message "run a+b" does.
    let without = Server::start();
    mount_list(
        &without,
        &json!([message("m1", &to(RECIPIENT), "aab", CREATED)]),
    );
    let with = Server::start();
    mount_list(
        &with,
        &json!([message("m2", &to(RECIPIENT), "run a+b", CREATED)]),
    );
    let subject_vars = |url: &str| {
        let mut vars = inputs_at(url);
        vars["mail_captured_subject"] = json!("a+b");
        vars
    };

    // Act
    let no_match = run(subject_vars(&without.url()), false);
    let match_found = run(subject_vars(&with.url()), false);

    // Assert
    assert!(!no_match.ok, "{}", no_match.out);
    assert!(match_found.ok, "{}", match_found.out);
}

#[test]
fn a_message_before_the_since_time_is_no_match_and_one_at_or_after_it_passes() {
    // Arrange: the message is created at 01:38:00.543, and the run began at 01:38:01 (after it).
    let server = Server::start();
    mount_list(&server, &json!([matching("m1")]));
    let mut vars = inputs(&server);
    vars["mail_captured_since"] = json!("2026-11-28T01:38:01Z");

    // Act
    let before = run(vars.clone(), false);

    // Arrange: the run began at 01:38:00, and a message created at that same second, with no milliseconds, counts.
    let at_second = Server::start();
    mount_list(
        &at_second,
        &json!([message(
            "m2",
            &to(RECIPIENT),
            SUBJECT,
            "2026-11-28T01:38:00Z"
        )]),
    );
    let mut at_vars = inputs(&at_second);
    at_vars["mail_captured_since"] = json!("2026-11-28T01:38:00Z");

    // Act
    let at = run(at_vars, false);

    // Assert
    assert!(!before.ok, "{}", before.out);
    assert!(
        before.out.contains("since 2026-11-28T01:38:01Z"),
        "{}",
        before.out
    );
    assert!(at.ok, "{}", at.out);
}

#[test]
fn a_malformed_input_is_refused_before_any_read_and_the_refusal_names_it() {
    // Arrange: each row sets one input to a value outside its rule. The fake API holds a matching message, so a missing
    // refusal would let the run pass.
    let rows = [
        (
            "mail_captured_api_url",
            json!("ftp://mail.example.test"),
            "mail_captured_api_url must be",
        ),
        (
            "mail_captured_api_url",
            json!(""),
            "mail_captured_api_url must be",
        ),
        (
            "mail_captured_recipient",
            json!(""),
            "mail_captured_recipient must be a non-empty address",
        ),
        (
            "mail_captured_subject",
            json!(""),
            "mail_captured_subject must be non-empty",
        ),
        (
            "mail_captured_since",
            json!("2026-11-28 01:38:00"),
            "mail_captured_since must be an ISO 8601 UTC time",
        ),
        (
            "mail_captured_since",
            json!("2026-11-28T01:38:00Z\n"),
            "mail_captured_since must be an ISO 8601 UTC time",
        ),
    ];

    for (key, value, part) in rows {
        let server = Server::start();
        mount_list(&server, &json!([matching("m1")]));
        let mut vars = inputs(&server);
        vars[key] = value;

        // Act
        let out = run(vars, false);

        // Assert: red, naming the input, and the fake API received no request.
        assert!(!out.ok, "{key}: {}", out.out);
        assert!(
            out.out.contains(part),
            "{key}: missing {part:?}: {}",
            out.out
        );
        assert!(sent(&server).is_empty(), "{key}: {:?}", sent(&server));
    }
}

#[test]
fn a_message_that_arrives_on_a_later_read_is_found_within_the_retries() {
    // Arrange: the first read is empty, and every later read holds the message.
    let server = Server::start();
    server.mount(
        Mock::given(matchers::method("GET"))
            .and(matchers::path(LIST))
            .respond_with(seed::reply(200, &listing(&json!([]))))
            .up_to_n_times(1)
            .with_priority(1),
    );
    mount_list(&server, &json!([matching("m1")]));
    let mut vars = inputs(&server);
    vars["mail_captured_retries"] = json!(3);

    // Act
    let out = run(vars, false);

    // Assert: green, on the second read: the until stops the poll at the first match.
    assert!(out.ok, "{}", out.out);
    assert_eq!(reads(&server), 2, "{:?}", sent(&server));
}

#[test]
fn a_read_that_is_not_200_is_no_match_even_when_its_body_lists_the_message() {
    // Arrange: every read answers 503 with a list that holds the message. Only a 200 read counts.
    let server = Server::start();
    server.mount(
        Mock::given(matchers::method("GET"))
            .and(matchers::path(LIST))
            .respond_with(seed::reply(503, &listing(&json!([matching("m1")])))),
    );
    let mut vars = inputs(&server);
    vars["mail_captured_retries"] = json!(3);

    // Act
    let out = run(vars, false);

    // Assert: red, with the status named, and the read was retried rather than taken as a match.
    assert!(!out.ok, "{}", out.out);
    assert!(out.out.contains("(HTTP 503,"), "{}", out.out);
    assert!(reads(&server) > 1, "{:?}", sent(&server));
}

#[test]
fn under_check_mode_the_catcher_is_read_and_a_matching_message_passes() {
    // Arrange
    let server = Server::start();
    mount_list(&server, &json!([matching("m1")]));

    // Act: a dry run. The read sets check_mode: false, so it reaches the catcher.
    let out = run(inputs(&server), true);

    // Assert
    assert!(out.ok, "{}", out.out);
    assert_eq!(reads(&server), 1, "{:?}", sent(&server));
}
