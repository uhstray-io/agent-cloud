//! The suite's fake HTTP server (`src/http.rs`), the port of `fake_http.py`.

use std::io::{ErrorKind, Read, Write};
use std::net::{SocketAddr, TcpStream, ToSocketAddrs};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::Duration;

use agent_cloud_tests::http::{Mock, ResponseTemplate, Server, matchers, refused_port};
use agent_cloud_tests::process;

/// The address a server URL names ("http://host:port" or "http://[v6]:port").
fn addr(url: &str) -> SocketAddr {
    let authority = url.strip_prefix("http://").unwrap();
    authority.to_socket_addrs().unwrap().next().unwrap()
}

/// One raw HTTP/1.1 request with `Connection: close`; returns the whole response text.
fn send(url: &str, method: &str, path: &str, headers: &[(&str, &str)], body: &[u8]) -> String {
    let mut stream = TcpStream::connect_timeout(&addr(url), Duration::from_secs(5)).unwrap();
    stream
        .set_read_timeout(Some(Duration::from_secs(10)))
        .unwrap();
    let mut head = format!(
        "{method} {path} HTTP/1.1\r\nHost: test\r\nConnection: close\r\nContent-Length: {}\r\n",
        body.len()
    );
    for (name, value) in headers {
        head.push_str(&format!("{name}: {value}\r\n"));
    }
    head.push_str("\r\n");
    stream.write_all(head.as_bytes()).unwrap();
    stream.write_all(body).unwrap();
    let mut response = Vec::new();
    stream.read_to_end(&mut response).unwrap();
    String::from_utf8_lossy(&response).into_owned()
}

fn status(response: &str) -> &str {
    response.split(' ').nth(1).unwrap()
}

fn body(response: &str) -> &str {
    response.split_once("\r\n\r\n").unwrap().1
}

fn reply_on(server: &Server, path: &str, code: u16, text: &str) {
    server.mount(
        Mock::given(matchers::path(path))
            .respond_with(ResponseTemplate::new(code).set_body_string(text)),
    );
}

#[test]
fn a_mounted_mock_answers_its_method_and_path_and_records_the_request() {
    let server = Server::start();
    server.mount(
        Mock::given(matchers::method("POST"))
            .and(matchers::path("/x"))
            .respond_with(ResponseTemplate::new(201).set_body_string("made")),
    );

    let response = send(
        server.url(),
        "POST",
        "/x",
        &[("X-Probe", "one")],
        b"payload",
    );

    assert_eq!(status(&response), "201");
    assert_eq!(body(&response), "made");
    let requests = server.requests();
    assert_eq!(requests.len(), 1);
    assert_eq!(requests[0].method.as_str(), "POST");
    assert_eq!(requests[0].url.path(), "/x");
    assert_eq!(requests[0].headers.get("x-probe").unwrap(), "one");
    assert_eq!(requests[0].body, b"payload");
}

#[test]
fn a_request_no_mock_matches_gets_404_and_is_recorded() {
    let server = Server::start();

    let response = send(server.url(), "GET", "/nothing", &[], b"");

    assert_eq!(status(&response), "404");
    let requests = server.requests();
    assert_eq!(requests.len(), 1);
    assert_eq!(requests[0].url.path(), "/nothing");
}

#[test]
fn a_closure_responder_can_change_its_reply_between_calls() {
    let server = Server::start();
    let calls = AtomicUsize::new(0);
    server.mount(Mock::given(matchers::path("/flaky")).respond_with(
        move |_: &agent_cloud_tests::http::Request| {
            if calls.fetch_add(1, Ordering::SeqCst) == 0 {
                ResponseTemplate::new(500)
            } else {
                ResponseTemplate::new(200)
            }
        },
    ));

    let first = send(server.url(), "GET", "/flaky", &[], b"");
    let second = send(server.url(), "GET", "/flaky", &[], b"");

    assert_eq!(status(&first), "500");
    assert_eq!(status(&second), "200");
}

#[test]
fn a_large_request_body_is_read_before_the_reply() {
    // fake_http.py's DrainingHandler: a reply sent over an unread body ends in a reset.
    let server = Server::start();
    reply_on(&server, "/auth", 401, "denied");
    let large = vec![b'a'; 4 * 1024 * 1024];

    let response = send(server.url(), "POST", "/auth", &[], &large);

    assert_eq!(status(&response), "401");
    assert_eq!(body(&response), "denied");
    assert_eq!(server.requests()[0].body.len(), 4 * 1024 * 1024);
}

#[test]
fn a_wildcard_bind_reports_a_loopback_url() {
    let server = Server::bind("0.0.0.0");
    reply_on(&server, "/", 200, "here");

    let response = send(server.url(), "GET", "/", &[], b"");

    assert!(
        server.url().starts_with("http://127.0.0.1:"),
        "{}",
        server.url()
    );
    assert_eq!(body(&response), "here");
}

#[test]
fn an_ipv6_bind_reports_a_bracketed_url() {
    let server = Server::bind("::1");
    reply_on(&server, "/", 200, "here");

    let response = send(server.url(), "GET", "/", &[], b"");

    assert!(
        server.url().starts_with("http://[::1]:"),
        "{}",
        server.url()
    );
    assert_eq!(body(&response), "here");
}

fn refused(address: SocketAddr) -> bool {
    // A small race: once the port is free, a parallel test's bind to 0 could be handed it.
    // The check runs at once after the drop, so that window is tiny.
    matches!(
        TcpStream::connect_timeout(&address, Duration::from_secs(2)),
        Err(e) if e.kind() == ErrorKind::ConnectionRefused
    )
}

#[test]
fn dropping_the_server_stops_it() {
    let server = Server::start();
    reply_on(&server, "/", 200, "up");
    let address = addr(server.url());
    assert_eq!(body(&send(server.url(), "GET", "/", &[], b"")), "up");

    drop(server);

    assert!(refused(address), "{address} still accepts after the drop");
}

#[test]
fn a_panic_while_serving_still_stops_it() {
    let mut address = None;

    let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        let server = Server::start();
        reply_on(&server, "/", 200, "up");
        address = Some(addr(server.url()));
        assert_eq!(body(&send(server.url(), "GET", "/", &[], b"")), "up");
        panic!("the test body failed");
    }));

    assert!(outcome.is_err());
    let address = address.unwrap();
    assert!(refused(address), "{address} still accepts after the panic");
}

#[test]
fn the_refused_port_refuses_a_connection() {
    let address = SocketAddr::from(([127, 0, 0, 1], refused_port()));

    let result = TcpStream::connect_timeout(&address, Duration::from_secs(2));

    assert_eq!(result.unwrap_err().kind(), ErrorKind::ConnectionRefused);
}

#[test]
fn a_child_process_reaches_the_server() {
    let server = Server::start();
    reply_on(&server, "/child", 200, "from the fake");
    let home = tempfile::tempdir().unwrap();

    let output = process::run_for(
        process::scrubbed("python3", home.path()).args([
            "-I",
            "-c",
            "import sys, urllib.request; print(urllib.request.urlopen(sys.argv[1]).read().decode())",
            &format!("{}/child", server.url()),
        ]),
        Duration::from_secs(30),
    );

    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(
        String::from_utf8_lossy(&output.stdout).trim(),
        "from the fake"
    );
}
