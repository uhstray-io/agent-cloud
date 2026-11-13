//! The committed local-dev inventory template (static tier, in the security set).
//!
//! `platform/inventory/local-dev.yml.example` is the source `make local-init` copies into the
//! working inventory. It must parse, be localhost-only by construction, define every service
//! group and per-host field, and carry no credential or routable address. The route checks hold
//! the template's Caddy route table to the copy `bootstrap-local-dev.yml` emits into the local
//! Semaphore inventory: a route changed in one copy only is the silent drift docs/MISTAKES.md 1.8
//! records (an encoding checked against a convenient sample, not the parser's rule).
//!
//! Replaces platform/tests/test_local_dev_inventory.bats (plan 24, Phase 4). Structure is read
//! from the parsed YAML; the credential, address and placeholder checks are deny-lists over the
//! file's text, which is the one form a text search is allowed to take (03 Part 1).
//!
//! Port map:
//!
//! | BATS @test | Rust fn |
//! |---|---|
//! | inventory example: file exists | `inventory_example_file_exists` |
//! | inventory example: valid YAML (python loader) | `inventory_example_parses_as_yaml` |
//! | inventory example: all ansible_host values are 127.0.0.1 (localhost-only) | `every_ansible_host_is_loopback` |
//! | inventory example: openbao_addr is localhost-only | `openbao_addr_is_loopback_everywhere` |
//! | inventory example: local_mode is true | `local_mode_is_true_everywhere` |
//! | inventory example: no real credentials (no values that look like passwords or tokens) | `inventory_holds_no_secret_looking_value` |
//! | inventory example: no public/routable IP addresses (RFC 1918 + loopback only) | `inventory_names_no_routable_address_outside_the_documented_exceptions` |
//! | inventory example: defines dns_svc group | `defines_every_required_service_group` (case `dns_svc`) |
//! | inventory example: defines step_ca_svc group | `defines_every_required_service_group` (case `step_ca_svc`) |
//! | inventory example: defines caddy_svc group | `defines_every_required_service_group` (case `caddy_svc`) |
//! | inventory example: defines authentik_svc group | `defines_every_required_service_group` (case `authentik_svc`) |
//! | inventory example: defines o11y_svc group | `defines_every_required_service_group` (case `o11y_svc`) |
//! | inventory example: defines opa_svc group | `defines_every_required_service_group` (case `opa_svc`) |
//! | inventory example: defines erpnext_svc group | `defines_every_required_service_group` (case `erpnext_svc`) |
//! | inventory example: every host has ansible_connection: local | `every_host_connects_locally` |
//! | inventory example: all service hosts declare service_name | `every_service_host_declares_service_name` |
//! | inventory example: all service hosts declare monorepo_deploy_path | `every_service_host_declares_monorepo_deploy_path` |
//! | inventory example: all service hosts declare container_engine | `every_service_host_declares_container_engine` |
//! | inventory example: container_engine values are podman or docker only | `container_engine_is_podman_or_docker` |
//! | inventory example: contains __REPO_DIR__ placeholder for local_workspace_dir | `local_workspace_dir_is_the_repo_placeholder` |
//! | inventory example: contains __GENESIS_DIR__ placeholder for local_monorepo_dir | `local_monorepo_dir_is_the_genesis_placeholder` |
//! | inventory example: caddy_routes includes semaphore and openbao entries | `caddy_routes_include_semaphore_and_openbao` |
//! | inventory example: caddy_routes forward_auth gates use authentik-server:9000 | `caddy_forward_auth_gates_use_the_authentik_outpost` |
//! | inventory example: caddy_routes auth entry exposes internal_https_port 8443 | `auth_route_exposes_internal_https_port_8443` |
//! | inventory example: caddy_routes inference entry opts into the inference_api edge, same list as the bootstrap INI | `inference_route_opts_into_the_inference_edge_in_both_copies` |
//! | inventory example and bootstrap INI: inference route fronts the agentgateway /v1 listener, not the Mac-host stand-in | `inference_route_fronts_agentgateway_not_the_mac_stand_in` |
//! | inventory example and bootstrap INI: agentgateway upstream is the public edge, key required, qwen3.8-flash-next served | `agentgateway_upstream_is_the_public_edge_with_a_key_and_the_served_model` |
//! | inventory example: caddy_routes MCP entry opts into mcp_api on the default listener, same list as the bootstrap INI | `mcp_route_opts_into_mcp_api_in_both_copies` |
//! | inventory example and bootstrap INI: OPA is served over HTTPS by a plain Caddy proxy, no edge gate | `opa_route_is_a_plain_https_proxy_with_no_edge_gate` |
//! | bootstrap: the INI route table is emitted as a Python literal, not JSON | `bootstrap_emits_the_route_table_as_a_python_literal` |
//! | inventory example: dns zone is agent-cloud.test | `dns_zone_is_agent_cloud_test` |
//! | inventory example: dns_port is 5300 (mDNS/VPN-safe) | `dns_port_is_5300` |
//! | inventory example: dns_upstreams contains public resolvers | `dns_upstreams_contain_public_resolvers` |
//! | inventory example: step-ca name identifies it as internal CA | `stepca_name_identifies_the_internal_ca` |
//! | inventory example: stepca_dns_names includes step-ca and localhost | `stepca_dns_names_include_step_ca_and_localhost` |

use regex::Regex;

use agent_cloud_tests::repo;
use agent_cloud_tests::yaml::{self, Node};

const INVENTORY: &str = "platform/inventory/local-dev.yml.example";
const BOOTSTRAP: &str = "platform/playbooks/bootstrap-local-dev.yml";
const AGENTGATEWAY_ROLES: [&str; 6] = [
    "infra-agent",
    "security-agent",
    "o11y-agent",
    "service-agent",
    "network-agent",
    "skynet-eval",
];

fn text(rel: &str) -> String {
    std::fs::read_to_string(repo::path(rel)).unwrap_or_else(|e| panic!("{rel}: {e}"))
}

fn document(rel: &str) -> Node {
    yaml::load_one(&repo::path(rel)).unwrap_or_else(|| panic!("{rel}: empty document"))
}

fn s(value: &str) -> Node {
    Node::Str(value.to_owned())
}

fn seq(items: &[&str]) -> Node {
    Node::Seq(items.iter().map(|i| s(i)).collect())
}

/// Every value stored under `key` anywhere in `node`, depth first.
fn values_of<'a>(node: &'a Node, key: &str) -> Vec<&'a Node> {
    let mut out = vec![];
    collect_values(node, key, &mut out);
    out
}

fn collect_values<'a>(node: &'a Node, key: &str, out: &mut Vec<&'a Node>) {
    match node {
        Node::Map(entries) => {
            for (k, v) in entries {
                if k.as_str() == Some(key) {
                    out.push(v);
                }
                collect_values(v, key, out);
            }
        }
        Node::Seq(items) => {
            for item in items {
                collect_values(item, key, out);
            }
        }
        _ => {}
    }
}

/// The variables of `all.vars`.
fn all_vars(doc: &Node) -> &Node {
    doc.get("all")
        .and_then(|a| a.get("vars"))
        .unwrap_or_else(|| panic!("no all.vars"))
}

/// The variables of every host of one group: `all.children.<group>.hosts.<host>`.
fn hosts_of<'a>(doc: &'a Node, group: &str) -> Vec<&'a Node> {
    doc.get("all")
        .and_then(|a| a.get("children"))
        .and_then(|c| c.get(group))
        .and_then(|g| g.get("hosts"))
        .and_then(Node::as_map)
        .map(|hosts| hosts.iter().map(|(_, vars)| vars).collect())
        .unwrap_or_default()
}

/// The variables of one named host of one group.
fn host<'a>(doc: &'a Node, group: &str, name: &str) -> &'a Node {
    doc.get("all")
        .and_then(|a| a.get("children"))
        .and_then(|c| c.get(group))
        .and_then(|g| g.get("hosts"))
        .and_then(|h| h.get(name))
        .unwrap_or_else(|| panic!("no host {name} in group {group}"))
}

/// Every route of every `key` list in the document, flattened.
fn routes_under<'a>(doc: &'a Node, key: &str) -> Vec<&'a Node> {
    values_of(doc, key)
        .into_iter()
        .filter_map(Node::as_seq)
        .flatten()
        .collect()
}

fn route<'a>(routes: &[&'a Node], host: &str) -> Option<&'a Node> {
    routes
        .iter()
        .copied()
        .find(|r| r.get("host").and_then(Node::as_str) == Some(host))
}

fn route_or_panic<'a>(routes: &[&'a Node], host: &str) -> &'a Node {
    route(routes, host).unwrap_or_else(|| panic!("no route for {host}"))
}

/// The one `_inv_ini` block scalar the bootstrap writes, as text.
fn inventory_ini(doc: &Node) -> String {
    let found = values_of(doc, "_inv_ini");
    assert_eq!(found.len(), 1, "one _inv_ini definition expected");
    found[0].as_str().expect("_inv_ini is a string").to_owned()
}

/// The value of `key` in an INI text, from every `key=value` line. ansible reads a key that
/// appears twice from one of them, so the tests require exactly one.
fn ini_values<'a>(ini: &'a str, key: &str) -> Vec<&'a str> {
    ini.lines()
        .filter_map(|line| {
            let (k, v) = line.trim().split_once('=')?;
            (k.trim_end() == key).then_some(v)
        })
        .collect()
}

fn single<'a>(ini: &'a str, key: &str) -> &'a str {
    let found = ini_values(ini, key);
    assert_eq!(found.len(), 1, "{key} must be set exactly once: {found:?}");
    found[0]
}

/// A Python literal (as `ansible-core` reads it through `ast.literal_eval`) as a node. Every
/// literal the INI carries is also valid YAML flow syntax.
fn literal(value: &str) -> Node {
    yaml::parse(value, "ini literal")
        .pop()
        .unwrap_or_else(|| panic!("empty literal {value:?}"))
}

#[test]
fn inventory_example_file_exists() {
    // Arrange, Act
    let exists = repo::path(INVENTORY).is_file();

    // Assert
    assert!(exists, "{INVENTORY} is missing");
}

#[test]
fn inventory_example_parses_as_yaml() {
    // Arrange, Act: a parse error panics in the loader, never skips
    let doc = document(INVENTORY);

    // Assert
    assert!(doc.get("all").is_some(), "no top-level `all` key");
}

#[test]
fn every_ansible_host_is_loopback() {
    // Arrange
    let doc = document(INVENTORY);

    // Act
    let hosts = values_of(&doc, "ansible_host");

    // Assert
    assert!(!hosts.is_empty(), "no ansible_host values");
    let bad: Vec<&Node> = hosts
        .into_iter()
        .filter(|h| h.as_str() != Some("127.0.0.1"))
        .collect();
    assert!(bad.is_empty(), "non-localhost ansible_host: {bad:?}");
}

#[test]
fn openbao_addr_is_loopback_everywhere() {
    // Arrange
    let doc = document(INVENTORY);

    // Act: every value, not an aggregate that one loopback entry could satisfy
    let addrs = values_of(&doc, "openbao_addr");

    // Assert
    assert!(!addrs.is_empty(), "no openbao_addr values");
    let bad: Vec<&Node> = addrs
        .into_iter()
        .filter(|a| !a.as_str().is_some_and(|v| v.contains("127.0.0.1")))
        .collect();
    assert!(bad.is_empty(), "non-localhost openbao_addr: {bad:?}");
}

#[test]
fn local_mode_is_true_everywhere() {
    // Arrange
    let doc = document(INVENTORY);

    // Act
    let flags = values_of(&doc, "local_mode");

    // Assert: every entry is explicitly true, not merely present
    assert!(!flags.is_empty(), "no local_mode values");
    let bad: Vec<&Node> = flags
        .into_iter()
        .filter(|f| **f != Node::Bool(true))
        .collect();
    assert!(bad.is_empty(), "local_mode not true: {bad:?}");
}

#[test]
fn inventory_holds_no_secret_looking_value() {
    // Arrange: a deny-list. Placeholders such as __REPO_DIR__ or LOCAL_FAKE_ values are fine, a
    // value that looks like a real secret is not. A quoted or templated value is never a secret.
    let text = text(INVENTORY);
    let patterns = [
        r#"(?i)password\s*:\s*[^{"'][a-zA-Z0-9._-]{8,}"#,
        r#"(?i)secret\s*:\s*[^{"'][a-zA-Z0-9._-]{16,}"#,
        r#"(?i)token\s*:\s*[^{"'][a-zA-Z0-9._-]{16,}"#,
    ];

    // Act, Assert
    for pattern in patterns {
        let re = Regex::new(pattern).expect("valid pattern");
        let hits: Vec<&str> = text.lines().filter(|l| re.is_match(l)).collect();
        assert!(
            hits.is_empty(),
            "a secret-looking value ({pattern}): {hits:?}"
        );
    }
}

#[test]
fn inventory_names_no_routable_address_outside_the_documented_exceptions() {
    // Arrange: comments are skipped, and so is the dns_upstreams line, which names PUBLIC
    // resolvers on purpose (asserted by dns_upstreams_contain_public_resolvers). Loopback, RFC
    // 1918 and the bind-all 0.0.0.0 are allowed.
    let text = text(INVENTORY);
    assert!(!text.is_empty(), "{INVENTORY} is empty");
    let ipv4 = Regex::new(r"([0-9]{1,3}\.){3}[0-9]{1,3}").expect("valid pattern");
    let allowed = Regex::new(r"^(127\.|10\.|172\.(1[6-9]|2[0-9]|3[01])\.|192\.168\.|0\.0\.0\.0$)")
        .expect("valid pattern");

    // Act
    let bad: Vec<&str> = text
        .lines()
        .filter(|l| !l.trim_start().starts_with('#') && !l.contains("dns_upstreams"))
        .flat_map(|l| ipv4.find_iter(l).map(|m| m.as_str()))
        .filter(|ip| !allowed.is_match(ip))
        .collect();

    // Assert
    assert!(bad.is_empty(), "unexpected routable address: {bad:?}");
}

#[test]
fn defines_every_required_service_group() {
    // Arrange: one case per group the composable deploys need
    let doc = document(INVENTORY);
    let cases = [
        "dns_svc",
        "step_ca_svc",
        "caddy_svc",
        "authentik_svc",
        "o11y_svc",
        "opa_svc",
        "erpnext_svc",
    ];

    // Act + Assert: each case names its own failure
    for group in cases {
        assert!(
            doc.get("all")
                .and_then(|a| a.get("children"))
                .and_then(|c| c.get(group))
                .is_some(),
            "the inventory defines no {group} group"
        );
    }
}

#[test]
fn every_host_connects_locally() {
    // Arrange
    let doc = document(INVENTORY);

    // Act
    let connections = values_of(&doc, "ansible_connection");

    // Assert
    assert!(!connections.is_empty(), "no ansible_connection values");
    let bad: Vec<&Node> = connections
        .into_iter()
        .filter(|c| c.as_str() != Some("local"))
        .collect();
    assert!(bad.is_empty(), "non-local ansible_connection: {bad:?}");
}

#[test]
fn every_service_host_declares_service_name() {
    // Arrange
    let doc = document(INVENTORY);

    // Act
    let count = values_of(&doc, "service_name").len();

    // Assert: at least eight concrete service hosts
    assert!(
        count >= 8,
        "{count} service_name declarations, expected at least 8"
    );
}

#[test]
fn every_service_host_declares_monorepo_deploy_path() {
    // Arrange
    let doc = document(INVENTORY);

    // Act
    let count = values_of(&doc, "monorepo_deploy_path").len();

    // Assert
    assert!(
        count >= 8,
        "{count} monorepo_deploy_path declarations, expected at least 8"
    );
}

#[test]
fn every_service_host_declares_container_engine() {
    // Arrange
    let doc = document(INVENTORY);

    // Act
    let count = values_of(&doc, "container_engine").len();

    // Assert
    assert!(
        count >= 8,
        "{count} container_engine declarations, expected at least 8"
    );
}

#[test]
fn container_engine_is_podman_or_docker() {
    // Arrange
    let doc = document(INVENTORY);

    // Act
    let engines = values_of(&doc, "container_engine");

    // Assert
    let bad: Vec<&Node> = engines
        .into_iter()
        .filter(|e| !matches!(e.as_str(), Some("podman" | "docker")))
        .collect();
    assert!(bad.is_empty(), "unexpected container_engine: {bad:?}");
}

#[test]
fn local_workspace_dir_is_the_repo_placeholder() {
    // Arrange
    let doc = document(INVENTORY);

    // Act
    let dir = all_vars(&doc)
        .get("local_workspace_dir")
        .and_then(Node::as_str);

    // Assert: `make local-init` substitutes the working tree for this placeholder
    assert!(
        dir.is_some_and(|d| d.contains("__REPO_DIR__")),
        "local_workspace_dir is not the __REPO_DIR__ placeholder: {dir:?}"
    );
}

#[test]
fn local_monorepo_dir_is_the_genesis_placeholder() {
    // Arrange
    let doc = document(INVENTORY);

    // Act
    let dir = all_vars(&doc)
        .get("local_monorepo_dir")
        .and_then(Node::as_str);

    // Assert
    assert!(
        dir.is_some_and(|d| d.contains("__GENESIS_DIR__")),
        "local_monorepo_dir is not the __GENESIS_DIR__ placeholder: {dir:?}"
    );
}

#[test]
fn caddy_routes_include_semaphore_and_openbao() {
    // Arrange
    let doc = document(INVENTORY);
    let routes = routes_under(&doc, "caddy_routes");

    // Act, Assert
    assert!(
        route(&routes, "semaphore.agent-cloud.test").is_some(),
        "no semaphore route"
    );
    assert!(
        route(&routes, "openbao.agent-cloud.test").is_some(),
        "no openbao route"
    );
}

#[test]
fn caddy_forward_auth_gates_use_the_authentik_outpost() {
    // Arrange
    let doc = document(INVENTORY);
    let routes = routes_under(&doc, "caddy_routes");

    // Act
    let gated = routes
        .iter()
        .filter(|r| {
            r.get("forward_auth")
                .and_then(Node::as_str)
                .is_some_and(|v| v.contains("authentik-server:9000"))
        })
        .count();

    // Assert: at least OpenBao and NetBox
    assert!(
        gated >= 2,
        "{gated} forward_auth gates on authentik-server:9000, expected at least 2"
    );
}

#[test]
fn auth_route_exposes_internal_https_port_8443() {
    // Arrange
    let doc = document(INVENTORY);
    let routes = routes_under(&doc, "caddy_routes");

    // Act
    let auth = route_or_panic(&routes, "auth.agent-cloud.test");

    // Assert: OIDC clients reach the IdP on the same host:port the browser uses
    assert_eq!(
        auth.get("internal_https_port").and_then(Node::as_str),
        Some("8443")
    );
}

#[test]
fn inference_route_opts_into_the_inference_edge_in_both_copies() {
    // Arrange: a plain reverse_proxy would expose the upstream's unauthenticated paths
    let example = document(INVENTORY);
    let bootstrap = document(BOOTSTRAP);
    let example_routes = routes_under(&example, "caddy_routes");
    let bootstrap_routes = routes_under(&bootstrap, "_caddy_routes");

    // Act
    let example_flag =
        route_or_panic(&example_routes, "inference.agent-cloud.test").get("inference_api");
    let bootstrap_flag =
        route_or_panic(&bootstrap_routes, "inference.{{ _dev_zone }}").get("inference_api");

    // Assert: the bootstrap INI emits its own copy of the table, and the two drift silently
    assert_eq!(
        example_flag,
        Some(&Node::Bool(true)),
        "example inference route"
    );
    assert_eq!(
        bootstrap_flag,
        Some(&Node::Bool(true)),
        "bootstrap inference route"
    );
}

#[test]
fn inference_route_fronts_agentgateway_not_the_mac_stand_in() {
    // Arrange
    let example_text = text(INVENTORY);
    let bootstrap_text = text(BOOTSTRAP);
    let example = document(INVENTORY);
    let bootstrap = document(BOOTSTRAP);
    let example_routes = routes_under(&example, "caddy_routes");
    let bootstrap_routes = routes_under(&bootstrap, "_caddy_routes");

    // Act, Assert: the gateway's /v1 listener, in both copies
    for (routes, host) in [
        (&example_routes, "inference.agent-cloud.test"),
        (&bootstrap_routes, "inference.{{ _dev_zone }}"),
    ] {
        let r = route_or_panic(routes, host);
        assert_eq!(
            r.get("upstream").and_then(Node::as_str),
            Some("agentgateway:4000"),
            "{host} does not front the gateway"
        );
        assert_eq!(r.get("inference_api"), Some(&Node::Bool(true)));
    }
    // The stand-in port must not come back in either copy (deny-list).
    assert!(
        !example_text.contains("host.containers.internal:8001"),
        "the Mac-host stand-in is back in the example"
    );
    assert!(
        !bootstrap_text.contains("host.containers.internal:8001"),
        "the Mac-host stand-in is back in the bootstrap"
    );
}

#[test]
fn agentgateway_upstream_is_the_public_edge_with_a_key_and_the_served_model() {
    // Arrange
    const EDGE: &str = "https://inference.uhstray.io/v1";
    let models = Node::Seq(vec![Node::Map(vec![(s("name"), s("qwen3.8-flash-next"))])]);
    let example = document(INVENTORY);
    let gateway = host(&example, "agentgateway_svc", "agentgateway-local");
    let bootstrap = document(BOOTSTRAP);
    let ini = inventory_ini(&bootstrap);

    // Act + Assert: the example
    assert_eq!(
        gateway.get("agw_upstream_base_url").and_then(Node::as_str),
        Some(EDGE)
    );
    assert_eq!(
        gateway.get("agw_upstream_requires_key"),
        Some(&Node::Bool(true))
    );
    assert_eq!(gateway.get("agw_models"), Some(&models));
    let clients: Vec<&str> = gateway
        .get("agw_clients")
        .and_then(Node::as_seq)
        .unwrap_or_default()
        .iter()
        .filter_map(Node::as_str)
        .collect();
    assert_eq!(clients.first(), Some(&"dev-local"));
    for role in AGENTGATEWAY_ROLES {
        assert!(clients.contains(&role), "the example enrolls no {role}");
    }

    // Act + Assert: the bootstrap INI. Its isolation probe and mcp-self drift from the example
    // is known and is not pinned here.
    assert_eq!(single(&ini, "agw_upstream_base_url"), EDGE);
    assert_eq!(single(&ini, "agw_upstream_requires_key"), "true");
    assert_eq!(literal(single(&ini, "agw_models")), models);
    let mut enrolled = vec!["dev-local"];
    enrolled.extend(AGENTGATEWAY_ROLES);
    assert_eq!(literal(single(&ini, "agw_clients")), seq(&enrolled));
}

#[test]
fn mcp_route_opts_into_mcp_api_in_both_copies() {
    // Arrange: mcp_api renders the exact-/mcp allowlist, not every path the listener serves
    let example = document(INVENTORY);
    let bootstrap = document(BOOTSTRAP);
    let example_routes = routes_under(&example, "caddy_routes");
    let bootstrap_routes = routes_under(&bootstrap, "_caddy_routes");

    // Act, Assert
    for (routes, host) in [
        (&example_routes, "mcp.agent-cloud.test"),
        (&bootstrap_routes, "mcp.{{ _dev_zone }}"),
    ] {
        let r = route_or_panic(routes, host);
        assert_eq!(
            r.get("upstream").and_then(Node::as_str),
            Some("agentgateway:4000"),
            "{host} upstream"
        );
        assert_eq!(r.get("mcp_api"), Some(&Node::Bool(true)), "{host} mcp_api");
    }
}

#[test]
fn opa_route_is_a_plain_https_proxy_with_no_edge_gate() {
    // Arrange: skynet trusts OPA's decisions, so the route carries no forward_auth, no api
    // allowlist flag and no other gate. The example route has exactly host and upstream.
    let example = document(INVENTORY);
    let bootstrap = document(BOOTSTRAP);
    let example_routes = routes_under(&example, "caddy_routes");
    let bootstrap_routes = routes_under(&bootstrap, "_caddy_routes");

    // Act
    let example_opa = route_or_panic(&example_routes, "opa.agent-cloud.test");
    let bootstrap_opa = route_or_panic(&bootstrap_routes, "opa.{{ _dev_zone }}");

    // Assert
    assert_eq!(
        example_opa.as_map().map(<[_]>::len),
        Some(2),
        "extra keys on the OPA route"
    );
    assert_eq!(
        example_opa.get("upstream").and_then(Node::as_str),
        Some("opa:8181")
    );
    assert_eq!(
        bootstrap_opa,
        &Node::Map(vec![
            (s("host"), s("opa.{{ _dev_zone }}")),
            (s("upstream"), s("opa:8181")),
        ]),
        "the bootstrap OPA route has more than host and upstream"
    );
}

#[test]
fn bootstrap_emits_the_route_table_as_a_python_literal() {
    // Arrange: ansible's ini reader calls ast.literal_eval, which reads JSON only while the value
    // has no boolean or null. The first `true` made caddy_routes a string in the past.
    let bootstrap = document(BOOTSTRAP);
    let ini = inventory_ini(&bootstrap);
    let source = text(BOOTSTRAP);

    // Act, Assert
    assert_eq!(
        ini_values(&ini, "caddy_routes"),
        vec!["{{ _caddy_routes | string }}"],
        "the INI does not emit the route table with `| string`"
    );
    assert_eq!(
        source
            .matches("caddy_routes={{ _caddy_routes | to_json }}")
            .count(),
        0,
        "the route table is emitted as JSON again"
    );
}

#[test]
fn dns_zone_is_agent_cloud_test() {
    // Arrange
    let doc = document(INVENTORY);

    // Act
    let zones: Vec<&Node> = hosts_of(&doc, "dns_svc")
        .into_iter()
        .filter_map(|h| h.get("dns_zone"))
        .collect();

    // Assert
    assert_eq!(zones, vec![&s("agent-cloud.test")]);
}

#[test]
fn dns_port_is_5300() {
    // Arrange
    let doc = document(INVENTORY);

    // Act
    let ports: Vec<&Node> = hosts_of(&doc, "dns_svc")
        .into_iter()
        .filter_map(|h| h.get("dns_port"))
        .collect();

    // Assert: mDNS and VPN safe on macOS
    assert_eq!(ports, vec![&s("5300")]);
}

#[test]
fn dns_upstreams_contain_public_resolvers() {
    // Arrange
    let doc = document(INVENTORY);

    // Act
    let upstreams: Vec<&str> = hosts_of(&doc, "dns_svc")
        .into_iter()
        .filter_map(|h| h.get("dns_upstreams"))
        .filter_map(Node::as_seq)
        .flatten()
        .filter_map(Node::as_str)
        .collect();

    // Assert: required forwarding config, not a leaked address
    assert!(
        upstreams.contains(&"1.1.1.1"),
        "no 1.1.1.1 upstream: {upstreams:?}"
    );
}

#[test]
fn stepca_name_identifies_the_internal_ca() {
    // Arrange
    let doc = document(INVENTORY);

    // Act
    let names: Vec<&str> = hosts_of(&doc, "step_ca_svc")
        .into_iter()
        .filter_map(|h| h.get("stepca_name"))
        .filter_map(Node::as_str)
        .collect();

    // Assert
    assert!(
        names.iter().any(|n| n.to_lowercase().contains("internal")),
        "stepca_name does not say internal: {names:?}"
    );
}

#[test]
fn stepca_dns_names_include_step_ca_and_localhost() {
    // Arrange
    let doc = document(INVENTORY);

    // Act
    let names: Vec<&str> = hosts_of(&doc, "step_ca_svc")
        .into_iter()
        .filter_map(|h| h.get("stepca_dns_names"))
        .filter_map(Node::as_str)
        .collect();

    // Assert
    assert!(
        names
            .iter()
            .any(|n| n.contains("step-ca") && n.contains("localhost")),
        "stepca_dns_names lacks step-ca or localhost: {names:?}"
    );
}
