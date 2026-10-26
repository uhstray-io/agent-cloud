//! `console probe <path> <expected_status>`: the container healthcheck and the
//! playbook verify call this inside the running container. It speaks plain
//! HTTP/1.0 over std sockets, so the distroless image needs no curl or wget.

use std::io::{self, Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::time::Duration;

const TIMEOUT: Duration = Duration::from_secs(5);
const DEFAULT_PORT: u16 = 8080;

/// Runs the probe and returns the process exit code: 0 when the status equals
/// the expected one, 1 otherwise (including every transport error).
pub fn run_cli(args: &[String]) -> i32 {
    let [path, expected] = args else {
        eprintln!("usage: console probe <path> <expected_status>");
        return 1;
    };
    let Ok(expected) = expected.parse::<u16>() else {
        eprintln!("probe: expected status must be a number");
        return 1;
    };
    let port = match std::env::var("PORT") {
        Ok(text) => match text.parse::<u16>() {
            Ok(port) => port,
            Err(_) => {
                eprintln!("probe: PORT is not a valid port");
                return 1;
            }
        },
        Err(_) => DEFAULT_PORT,
    };
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    let result = fetch_status(addr, path, TIMEOUT);
    if let Ok(code) = &result {
        println!("{code}");
    }
    if let Err(err) = &result {
        eprintln!("probe: GET {path} on 127.0.0.1:{port} failed: {err}");
    }
    exit_code(result, expected)
}

/// 0 only when the probe got a status and it equals `expected`; 1 otherwise.
pub fn exit_code(result: io::Result<u16>, expected: u16) -> i32 {
    match result {
        Ok(code) if code == expected => 0,
        _ => 1,
    }
}

/// Status code of one GET, or an error when the request or status line fails.
pub fn fetch_status(addr: SocketAddr, path: &str, timeout: Duration) -> io::Result<u16> {
    let text = http_get_raw(addr, path, timeout)?;
    let line = text.lines().next().unwrap_or("");
    parse_status_line(line)
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "no HTTP status line"))
}

/// Whole response (status line, headers, body) of one GET. Used by the probe
/// and by the server tests, which hit a live listener without a client crate.
pub fn http_get_raw(addr: SocketAddr, path: &str, timeout: Duration) -> io::Result<String> {
    if !path.starts_with('/') {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "path must start with /",
        ));
    }
    let mut stream = TcpStream::connect_timeout(&addr, timeout)?;
    stream.set_read_timeout(Some(timeout))?;
    stream.set_write_timeout(Some(timeout))?;
    // One write_all, not write!: write! issues one syscall per piece, and a peer
    // that reads only the first piece then closes resets the connection.
    let request = format!("GET {path} HTTP/1.0\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n");
    stream.write_all(request.as_bytes())?;
    let mut buf = Vec::new();
    stream.read_to_end(&mut buf)?;
    Ok(String::from_utf8_lossy(&buf).into_owned())
}

/// `HTTP/1.1 200 OK` -> `Some(200)`. Anything else is `None`.
pub fn parse_status_line(line: &str) -> Option<u16> {
    let mut parts = line.split_whitespace();
    if !parts.next()?.starts_with("HTTP/") {
        return None;
    }
    parts.next()?.parse().ok()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;
    use std::thread;

    #[test]
    fn parses_status_line() {
        assert_eq!(parse_status_line("HTTP/1.1 200 OK"), Some(200));
        assert_eq!(parse_status_line("HTTP/1.0 401 Unauthorized"), Some(401));
        assert_eq!(parse_status_line("garbage 200 OK"), None);
        assert_eq!(parse_status_line("HTTP/1.1 twohundred"), None);
        assert_eq!(parse_status_line(""), None);
    }

    #[test]
    fn fetch_status_reads_status_from_a_live_socket() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind ephemeral port");
        let addr = listener.local_addr().expect("local addr");
        let server = thread::spawn(move || {
            let (mut conn, _) = listener.accept().expect("accept");
            // Read until the blank line that ends the request head.
            let mut seen = Vec::new();
            let mut chunk = [0u8; 256];
            while !seen.windows(4).any(|w| w == b"\r\n\r\n") {
                let n = conn.read(&mut chunk).expect("read request");
                if n == 0 {
                    break;
                }
                seen.extend_from_slice(&chunk[..n]);
            }
            conn.write_all(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n")
                .expect("write");
        });
        let code = fetch_status(addr, "/healthz", Duration::from_secs(5)).expect("probe");
        server.join().expect("server thread");
        assert_eq!(code, 503);
    }

    #[test]
    fn rejects_relative_path_without_slash() {
        let addr = SocketAddr::from(([127, 0, 0, 1], 9));
        let err = fetch_status(addr, "healthz", Duration::from_millis(50)).unwrap_err();
        assert_eq!(err.kind(), io::ErrorKind::InvalidInput);
    }

    #[test]
    fn exit_code_is_zero_only_on_the_expected_status() {
        assert_eq!(exit_code(Ok(200), 200), 0);
        assert_eq!(exit_code(Ok(401), 200), 1);
        assert_eq!(exit_code(Err(io::Error::other("refused")), 200), 1);
    }

    #[test]
    fn run_cli_exits_one_on_bad_arguments() {
        assert_eq!(run_cli(&[]), 1);
        assert_eq!(run_cli(&["/healthz".into(), "abc".into()]), 1);
    }
}
