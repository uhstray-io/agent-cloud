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
//! connections. A test that needs one stays on the Python fixture until this grows it (AC-956).

use std::io::ErrorKind;
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr, TcpListener, TcpStream};
use std::time::{Duration, Instant};

use tokio::runtime::Runtime;

pub use wiremock::{Mock, Request, ResponseTemplate, matchers};

/// A fake HTTP server on an ephemeral port, stopped when dropped (also during a panic).
pub struct Server {
    runtime: Runtime,
    server: Option<wiremock::MockServer>,
    addr: SocketAddr,
}

impl Server {
    /// A server on loopback IPv4: `bind("127.0.0.1")`.
    pub fn start() -> Server {
        Server::bind("127.0.0.1")
    }

    /// A server on an ephemeral port of `host`. A wildcard bind ("0.0.0.0", "::") is reached on
    /// loopback, so its address and URL name loopback.
    pub fn bind(host: &str) -> Server {
        let listener = TcpListener::bind((host, 0))
            .unwrap_or_else(|e| panic!("cannot bind a fake HTTP server on {host}: {e}"));
        let mut addr = listener
            .local_addr()
            .expect("a bound listener has an address");
        if addr.ip().is_unspecified() {
            addr.set_ip(if addr.is_ipv6() {
                IpAddr::V6(Ipv6Addr::LOCALHOST)
            } else {
                IpAddr::V4(Ipv4Addr::LOCALHOST)
            });
        }
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
            addr,
        }
    }

    /// The address a client connects to.
    pub fn addr(&self) -> SocketAddr {
        self.addr
    }

    /// The server's base URL, "http://<host>:<port>" with no trailing slash; an IPv6 host is
    /// bracketed.
    pub fn url(&self) -> String {
        format!("http://{}", self.addr)
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
    /// Stops the server and waits, about 2 s at most, until its port refuses connections:
    /// dropping the MockServer only signals its accept loop, which runs on a detached thread
    /// (wiremock `bare_server.rs`), so the listener closes a moment later. Running out of time is
    /// not an error: a parallel test can be handed the freed port and keep it open.
    ///
    /// wiremock's own drop panics when a `Mock::expect` is unmet, and that panic prints every
    /// recorded request with its headers, unredacted (wiremock `exposed_server.rs`, `request.rs`).
    /// A fixture must therefore never send a real credential to this server.
    fn drop(&mut self) {
        let Some(server) = self.server.take() else {
            return;
        };
        drop(server);
        let deadline = Instant::now() + Duration::from_secs(2);
        while !refuses(self.addr) && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(10));
        }
    }
}

/// Whether a TCP connection to `addr` is refused; an attempt that neither connects nor is refused
/// within 50 ms counts as not refused.
pub fn refuses(addr: SocketAddr) -> bool {
    matches!(
        TcpStream::connect_timeout(&addr, Duration::from_millis(50)),
        Err(e) if e.kind() == ErrorKind::ConnectionRefused
    )
}

/// The port aimed at when a test needs "nothing listens here": 1, below every ephemeral range,
/// so a bind to port 0 never hands it out. A port freed by stopping a server is not that: a
/// parallel test can be handed it (`fake_http.py`, `docs/MISTAKES.md` 2.33). Panics, naming the
/// address, unless 127.0.0.1, and ::1 where this machine has an IPv6 loopback, refuse it.
pub fn refused_port() -> u16 {
    const PORT: u16 = 1;
    let mut loopbacks = vec![IpAddr::V4(Ipv4Addr::LOCALHOST)];
    if TcpListener::bind((Ipv6Addr::LOCALHOST, 0)).is_ok() {
        loopbacks.push(IpAddr::V6(Ipv6Addr::LOCALHOST));
    }
    for ip in loopbacks {
        let addr = SocketAddr::new(ip, PORT);
        match TcpStream::connect_timeout(&addr, Duration::from_secs(2)) {
            Err(e) if e.kind() == ErrorKind::ConnectionRefused => {}
            Ok(_) => panic!("something listens on {addr}; refused_port() needs it closed"),
            Err(e) => panic!("{addr} did not refuse a connection: {e}"),
        }
    }
    PORT
}
