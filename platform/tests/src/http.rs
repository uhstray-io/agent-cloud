//! Fake HTTP servers for tests: the Rust side of `platform/tests/fake_http.py` (`serve()`,
//! `DrainingHandler`, `refused_port()`), built on wiremock 0.6.5.
//!
//! A [`Server`] is a fresh wiremock server on an ephemeral port, driven synchronously: the
//! caller mounts [`Mock`]s, points the code under test at [`Server::url`], and reads back
//! [`Server::requests`]. wiremock reads the whole request body before it matches, so a reply
//! never leaves bytes unread in the socket (the reset `DrainingHandler` exists to prevent).
//!
//! Not covered, because wiremock 0.6.5 has none of them: TLS (`serve(ssl_context=...)`),
//! streamed or chunk-timed response bodies, replies held until a test event, and dropped
//! connections. A test that needs one stays on the Python fixture until this grows it.

use std::io::ErrorKind;
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::time::Duration;

use tokio::runtime::Runtime;

pub use wiremock::{Mock, Request, ResponseTemplate, matchers};

/// A fake HTTP server on an ephemeral port, stopped when dropped (also during a panic).
pub struct Server {
    runtime: Runtime,
    server: Option<wiremock::MockServer>,
    url: String,
}

impl Server {
    /// A server on loopback IPv4: `bind("127.0.0.1")`.
    pub fn start() -> Server {
        Server::bind("127.0.0.1")
    }

    /// A server on an ephemeral port of `host`. Its URL names loopback for a wildcard bind
    /// ("0.0.0.0" gives 127.0.0.1, "::" gives ::1) and brackets an IPv6 host.
    pub fn bind(host: &str) -> Server {
        let listener = TcpListener::bind((host, 0))
            .unwrap_or_else(|e| panic!("cannot bind a fake HTTP server on {host}: {e}"));
        let port = listener.local_addr().unwrap().port();
        let url_host = match host {
            "0.0.0.0" => "127.0.0.1",
            "::" => "::1",
            other => other,
        };
        let url = if url_host.contains(':') {
            format!("http://[{url_host}]:{port}")
        } else {
            format!("http://{url_host}:{port}")
        };
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_time()
            .build()
            .expect("a tokio runtime for the fake HTTP server");
        // The builder, not MockServer::start(): start() hands out a pooled server that
        // outlives this one and may carry another test's mocks.
        let server = runtime.block_on(wiremock::MockServer::builder().listener(listener).start());
        Server {
            runtime,
            server: Some(server),
            url,
        }
    }

    /// The server's base URL, "http://<host>:<port>" with no trailing slash.
    pub fn url(&self) -> &str {
        &self.url
    }

    /// Mounts `mock`: requests it matches get its reply from now on.
    pub fn mount(&self, mock: Mock) {
        self.runtime.block_on(mock.mount(self.server()));
    }

    /// Every request received so far, matched or not, in arrival order.
    pub fn requests(&self) -> Vec<Request> {
        self.runtime
            .block_on(self.server().received_requests())
            .expect("request recording is on")
    }

    fn server(&self) -> &wiremock::MockServer {
        self.server.as_ref().expect("the server is running")
    }
}

impl Drop for Server {
    /// Stops the server and waits until its port refuses connections. Dropping the MockServer
    /// only signals its accept loop, which runs on a detached thread (wiremock
    /// `bare_server.rs`), so the listener closes a moment later; the wait makes "dropped" mean
    /// "stopped". Bounded at 2 s: a parallel test handed the freed port would keep it open.
    fn drop(&mut self) {
        let Some(server) = self.server.take() else {
            return;
        };
        let address = *server.address();
        drop(server);
        for _ in 0..200 {
            match TcpStream::connect_timeout(&address, Duration::from_millis(50)) {
                Err(e) if e.kind() == ErrorKind::ConnectionRefused => return,
                _ => std::thread::sleep(Duration::from_millis(10)),
            }
        }
    }
}

/// The port aimed at when a test needs "nothing listens here": 1, below every ephemeral range,
/// so a bind to port 0 never hands it out. A port freed by stopping a server is not that: a
/// parallel test can be handed it (`fake_http.py`, `docs/MISTAKES.md` 2.33). Panics, naming the
/// address, when 127.0.0.1 or ::1 does not refuse a connection to it; ::1 is skipped on a
/// machine with no IPv6 loopback.
pub fn refused_port() -> u16 {
    const PORT: u16 = 1;
    for (address, v6) in [("127.0.0.1", false), ("::1", true)] {
        let addr = SocketAddr::new(address.parse().unwrap(), PORT);
        match TcpStream::connect_timeout(&addr, Duration::from_secs(2)) {
            Ok(_) => {
                panic!("something listens on [{address}]:{PORT}; refused_port() needs it closed")
            }
            Err(e) if e.kind() == ErrorKind::ConnectionRefused => {}
            Err(e)
                if v6
                    && (matches!(
                        e.kind(),
                        ErrorKind::AddrNotAvailable | ErrorKind::NetworkUnreachable
                    ) || e.raw_os_error() == Some(libc_eafnosupport())) => {}
            Err(e) => panic!("[{address}]:{PORT} did not refuse a connection: {e}"),
        }
    }
    PORT
}

/// EAFNOSUPPORT, the "address family not supported" errno: 47 on macOS, 97 on Linux.
fn libc_eafnosupport() -> i32 {
    if cfg!(target_os = "macos") { 47 } else { 97 }
}
