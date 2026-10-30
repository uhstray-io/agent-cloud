//! Read-only Proxmox client for live node and guest state. Only the refresh task calls it;
//! the browser never talks to Proxmox. It authenticates with a PVEAuditor API token
//! (plan 17 D22). Errors are fixed variants: no URL, no response body and no header reaches
//! a message or a log line.
//!
//! `CONSOLE_PROXMOX_CA_FILE` adds roots for a self-signed API certificate. Certificate
//! verification is never turned off.

use std::fmt;

use serde::{Deserialize, Serialize};

use super::config::ProxmoxConfig;

/// A cluster node as `GET /api2/json/cluster/resources` reports it. Units are Proxmox's:
/// `cpu` is a fraction of `maxcpu`, memory is bytes, `uptime` is seconds.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct PveNode {
    pub name: String,
    pub status: Option<String>,
    pub cpu: Option<f64>,
    pub maxcpu: Option<f64>,
    pub mem: Option<u64>,
    pub maxmem: Option<u64>,
    pub uptime: Option<u64>,
}

/// A QEMU VM or LXC container. Joined to NetBox by `name`.
#[derive(Debug, Clone, PartialEq)]
pub struct PveGuest {
    pub name: String,
    pub vmid: Option<u64>,
    /// The node the guest runs on.
    pub node: Option<String>,
    pub status: Option<String>,
}

#[derive(Debug, Clone, Default)]
pub struct ProxmoxData {
    pub nodes: Vec<PveNode>,
    pub guests: Vec<PveGuest>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum FetchError {
    Timeout,
    Connect,
    Status(u16),
    Decode,
    Other,
}

impl fmt::Display for FetchError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            FetchError::Timeout => f.write_str("Proxmox request timed out"),
            FetchError::Connect => f.write_str("cannot connect to Proxmox"),
            FetchError::Status(code) => write!(f, "Proxmox answered HTTP {code}"),
            FetchError::Decode => f.write_str("Proxmox response could not be decoded"),
            FetchError::Other => f.write_str("Proxmox request failed"),
        }
    }
}

/// The `Authorization` value for a PVE API token: `PVEAPIToken=<user@realm!tokenid>=<secret>`.
pub fn authorization(token_id: &str, token: &str) -> String {
    format!("PVEAPIToken={token_id}={token}")
}

/// Reads the cluster's nodes and guests in one request.
pub async fn fetch(
    http: &reqwest::Client,
    config: &ProxmoxConfig,
) -> Result<ProxmoxData, FetchError> {
    let response = http
        .get(format!("{}/api2/json/cluster/resources", config.api_url))
        .header(
            reqwest::header::AUTHORIZATION,
            authorization(&config.token_id, config.token.expose()),
        )
        .header(reqwest::header::ACCEPT, "application/json")
        .send()
        .await
        .map_err(transport_error)?;
    let status = response.status();
    if !status.is_success() {
        return Err(FetchError::Status(status.as_u16()));
    }
    let body = response.bytes().await.map_err(transport_error)?;
    parse_resources(&body)
}

/// Keeps nodes and guests and drops every other resource type (storage, pools).
/// A node or guest without a name cannot be joined, so it is dropped too.
pub fn parse_resources(body: &[u8]) -> Result<ProxmoxData, FetchError> {
    let envelope: Envelope = serde_json::from_slice(body).map_err(|_| FetchError::Decode)?;
    let mut data = ProxmoxData::default();
    for item in envelope.data {
        match item.kind.as_deref() {
            Some("node") => {
                if let Some(name) = item.node {
                    data.nodes.push(PveNode {
                        name,
                        status: item.status,
                        cpu: item.cpu,
                        maxcpu: item.maxcpu,
                        mem: item.mem,
                        maxmem: item.maxmem,
                        uptime: item.uptime,
                    });
                }
            }
            Some("qemu" | "lxc") => {
                if let Some(name) = item.name {
                    data.guests.push(PveGuest {
                        name,
                        vmid: item.vmid,
                        node: item.node,
                        status: item.status,
                    });
                }
            }
            _ => {}
        }
    }
    Ok(data)
}

/// reqwest's own Display names the request URL, so it is classified, not shown.
fn transport_error(err: reqwest::Error) -> FetchError {
    if err.is_timeout() {
        FetchError::Timeout
    } else if err.is_connect() {
        FetchError::Connect
    } else {
        FetchError::Other
    }
}

#[derive(Deserialize)]
struct Envelope {
    data: Vec<Resource>,
}

/// One `cluster/resources` entry. Every field is optional on the wire, because the same
/// list carries nodes, guests and storage with different keys.
#[derive(Deserialize)]
struct Resource {
    #[serde(rename = "type")]
    kind: Option<String>,
    node: Option<String>,
    name: Option<String>,
    vmid: Option<u64>,
    status: Option<String>,
    cpu: Option<f64>,
    maxcpu: Option<f64>,
    mem: Option<u64>,
    maxmem: Option<u64>,
    uptime: Option<u64>,
}

#[cfg(test)]
pub mod fixtures {
    //! A Proxmox stand-in on loopback. Answers only the PVE token it expects, and can be
    //! switched to fail every request.

    use std::net::SocketAddr;
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::Arc;

    use dioxus::server::axum::extract::State;
    use dioxus::server::axum::http::{header, HeaderMap, StatusCode};
    use dioxus::server::axum::response::{IntoResponse, Response};
    use dioxus::server::axum::routing::get;
    use dioxus::server::axum::Router;

    pub const TOKEN_ID: &str = "console@pve!ro";
    pub const TOKEN: &str = "pve-secret-value";
    pub const EXPECTED_AUTH: &str = "PVEAPIToken=console@pve!ro=pve-secret-value";

    /// Two nodes; guests named like the NetBox fixture, plus a storage entry that must be ignored.
    pub const RESOURCES: &str = r#"{"data":[
        {"id":"node/pve-01","type":"node","node":"pve-01","status":"online","cpu":0.12,"maxcpu":16,
         "mem":8589934592,"maxmem":68719476736,"uptime":123456},
        {"id":"node/pve-02","type":"node","node":"pve-02","status":"online","cpu":0.05,"maxcpu":8,
         "mem":2147483648,"maxmem":34359738368,"uptime":99999},
        {"id":"qemu/101","type":"qemu","node":"pve-01","vmid":101,"name":"tududi","status":"running",
         "cpu":0.02,"maxcpu":2,"mem":1073741824,"maxmem":4294967296,"uptime":3600,"disk":0,"maxdisk":42949672960},
        {"id":"qemu/102","type":"qemu","node":"pve-01","vmid":102,"name":"gateway","status":"stopped",
         "maxcpu":1,"maxmem":2147483648,"uptime":0},
        {"id":"lxc/200","type":"lxc","node":"pve-01","vmid":200,"name":"planned-vm","status":"running",
         "cpu":0.01,"maxcpu":1,"mem":268435456,"maxmem":1073741824,"uptime":500},
        {"id":"qemu/104","type":"qemu","node":"pve-02","vmid":104,"name":"wiki","status":"running",
         "cpu":0.3,"maxcpu":2,"mem":2147483648,"maxmem":4294967296,"uptime":7200},
        {"id":"storage/pve-01/local","type":"storage","node":"pve-01","storage":"local","status":"available"}
    ]}"#;

    #[derive(Clone, Default)]
    pub struct Mock {
        pub fail: Arc<AtomicBool>,
    }

    async fn resources(State(mock): State<Mock>, headers: HeaderMap) -> Response {
        let auth = headers
            .get(header::AUTHORIZATION)
            .and_then(|value| value.to_str().ok())
            .unwrap_or("")
            .to_string();
        if mock.fail.load(Ordering::SeqCst) {
            return (StatusCode::INTERNAL_SERVER_ERROR, format!("boom {auth}")).into_response();
        }
        if auth != EXPECTED_AUTH {
            return StatusCode::UNAUTHORIZED.into_response();
        }
        (StatusCode::OK, RESOURCES).into_response()
    }

    pub fn router(mock: Mock) -> Router {
        Router::new()
            .route("/api2/json/cluster/resources", get(resources))
            .with_state(mock)
    }

    /// Serves `router` on loopback and returns its address.
    pub async fn serve(app: Router) -> SocketAddr {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind");
        let addr = listener.local_addr().expect("addr");
        tokio::spawn(async move {
            dioxus::server::axum::serve(listener, app)
                .await
                .expect("serve");
        });
        addr
    }

    pub fn config_for(addr: SocketAddr) -> super::super::config::ProxmoxConfig {
        super::super::config::ProxmoxConfig {
            api_url: format!("http://{addr}"),
            token_id: TOKEN_ID.to_string(),
            token: super::super::config::Secret::new(TOKEN),
            ca_file: None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::fixtures::{config_for, router, serve, Mock, EXPECTED_AUTH, TOKEN};
    use super::*;

    #[test]
    fn api_token_header_is_pveapitoken_id_equals_secret() {
        assert_eq!(
            authorization("console@pve!ro", "abc-123"),
            "PVEAPIToken=console@pve!ro=abc-123"
        );
    }

    #[test]
    fn parse_keeps_nodes_and_guests_and_drops_storage() {
        let data = parse_resources(fixtures::RESOURCES.as_bytes()).expect("parse");
        let nodes: Vec<&str> = data.nodes.iter().map(|n| n.name.as_str()).collect();
        assert_eq!(nodes, ["pve-01", "pve-02"]);
        assert_eq!(data.nodes[0].status.as_deref(), Some("online"));
        assert_eq!(data.nodes[0].maxcpu, Some(16.0));
        let guests: Vec<&str> = data.guests.iter().map(|g| g.name.as_str()).collect();
        assert_eq!(guests, ["tududi", "gateway", "planned-vm", "wiki"]);
        assert_eq!(data.guests[0].vmid, Some(101));
        assert_eq!(data.guests[0].node.as_deref(), Some("pve-01"));
        assert_eq!(data.guests[1].status.as_deref(), Some("stopped"));
    }

    #[test]
    fn unnamed_guests_and_nodes_are_dropped() {
        let body = br#"{"data":[
            {"type":"qemu","node":"pve-01","vmid":9},
            {"type":"node","status":"online"}
        ]}"#;
        let data = parse_resources(body).expect("parse");
        assert!(data.guests.is_empty());
        assert!(data.nodes.is_empty());
    }

    #[test]
    fn undecodable_body_is_a_decode_error_without_body_text() {
        let err = parse_resources(b"{\"data\": not json secret=leaked").expect_err("must fail");
        assert_eq!(err, FetchError::Decode);
        assert!(!err.to_string().contains("leaked"));
    }

    #[test]
    fn every_failure_text_is_fixed_and_names_no_token() {
        for err in [
            FetchError::Timeout,
            FetchError::Connect,
            FetchError::Status(401),
            FetchError::Decode,
            FetchError::Other,
        ] {
            let text = err.to_string();
            assert!(text.contains("Proxmox"), "{text}");
            assert!(
                !text.contains(TOKEN) && !text.contains("PVEAPIToken"),
                "{text}"
            );
        }
    }

    #[tokio::test]
    async fn fetch_sends_the_pve_token_and_decodes_the_resources() {
        let addr = serve(router(Mock::default())).await;
        let data = fetch(&reqwest::Client::new(), &config_for(addr))
            .await
            .expect("fetch");
        assert_eq!(data.nodes.len(), 2);
        assert_eq!(data.guests.len(), 4);
    }

    #[tokio::test]
    async fn a_refused_token_is_a_status_error_without_the_token() {
        let addr = serve(router(Mock::default())).await;
        let mut config = config_for(addr);
        config.token = super::super::config::Secret::new("wrong-secret");
        let err = fetch(&reqwest::Client::new(), &config)
            .await
            .expect_err("must fail");
        assert_eq!(err, FetchError::Status(401));
        assert!(!err.to_string().contains("wrong-secret"));
    }

    #[tokio::test]
    async fn a_failing_server_keeps_its_body_out_of_the_error() {
        let mock = Mock::default();
        mock.fail.store(true, std::sync::atomic::Ordering::SeqCst);
        let addr = serve(router(mock)).await;
        let err = fetch(&reqwest::Client::new(), &config_for(addr))
            .await
            .expect_err("must fail");
        assert_eq!(err, FetchError::Status(500));
        let text = err.to_string();
        assert!(!text.contains("boom"), "{text}");
        assert!(!text.contains(EXPECTED_AUTH), "{text}");
    }

    #[tokio::test]
    async fn connection_refused_is_a_connect_error() {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").expect("bind");
        let addr = listener.local_addr().expect("addr");
        drop(listener);
        let err = fetch(&reqwest::Client::new(), &config_for(addr))
            .await
            .expect_err("must fail");
        assert_eq!(err, FetchError::Connect);
    }
}
