//! The static tier of the ERPNext composable service (plan 24, Phase 4).
//!
//! Verifies the composable shape of `platform/services/erpnext`: an env-parameterized compose with
//! pinned, fully-qualified images; a container-only `deploy.sh` (no secret generation); an env
//! template that pulls every credential from OpenBao (`{{ secrets.* }}`, no literals); the slim
//! `compose.local.yml` overlay; and a composable (not legacy) deploy playbook. Slim tier: a single
//! `queue` worker, no MinIO.
//!
//! Replaces `platform/tests/test_service_erpnext.bats`. A BATS `! grep ...` line that was not its
//! test's last command never failed under `set -e` (CONTRIBUTING.md, "Writing BATS Tests"), so
//! every check here that must not match is a real assertion. The container-name and frontend-network
//! checks read the compose files as YAML, so each is bound to its service.
//!
//! Port map (BATS @test → Rust fn):
//!
//! | BATS @test | Rust fn |
//! |---|---|
//! | `erpnext: compose has the expected top-level shape` | `compose_has_the_expected_top_level_shape` |
//! | `erpnext: compose env-parameterizes images + frontend bind/port` | `compose_env_parameterizes_images_and_the_frontend_bind_and_port` |
//! | `erpnext: images are fully-qualified and pinned (no :latest drift)` | `images_are_fully_qualified_and_pinned_with_no_latest_tag` |
//! | `erpnext: compose defines the slim-tier services with explicit container_name` | `compose_defines_the_slim_tier_services_with_explicit_container_names` |
//! | `erpnext: slim tier excludes prod-only MinIO / split workers` | `slim_tier_excludes_the_prod_only_minio_and_split_workers` |
//! | `erpnext: compose has no hardcoded credentials` | `compose_has_no_hardcoded_credentials` |
//! | `erpnext: compose has no RFC1918 IPs` | `compose_has_no_rfc1918_addresses` |
//! | `erpnext: deploy.sh is executable, bash, sources common.sh, uses compose, no secrets` | `deploy_sh_is_executable_bash_sources_common_and_holds_no_secrets` |
//! | `erpnext: deploy.sh never hardcodes a container engine` | `deploy_sh_never_hardcodes_a_container_engine` |
//! | `erpnext: post-deploy.sh is executable, bash, idempotent site bootstrap, reads .env only` | `post_deploy_sh_is_an_executable_idempotent_bootstrap_with_no_secret_handling` |
//! | `erpnext: env template pulls every credential from OpenBao, no literals` | `env_template_pulls_every_credential_from_openbao_with_no_literals` |
//! | `erpnext: env template uses approved Jinja2 namespaces only` | `env_template_uses_only_the_approved_jinja2_namespaces` |
//! | `erpnext: local overlay adds caps/SELinux/local-dev but does NOT republish ports` | `local_overlay_adds_limits_selinux_and_local_dev_but_publishes_no_ports` |
//! | `erpnext: local overlay puts the frontend on local-dev so Caddy reaches it by name` | `local_overlay_puts_the_frontend_on_local_dev` |
//! | `erpnext: deploy playbook is composable (place-monorepo + manage-secrets), not legacy` | `deploy_playbook_is_composable_and_not_legacy` |
//! | `erpnext: clean-deploy playbook destroys then redeploys (local greenfield)` | `clean_deploy_playbook_destroys_then_redeploys` |
//! | `erpnext: Social Login Key sets custom_base_url (else Frappe ignores base_url)` | `social_login_key_sets_custom_base_url` |
//! | `erpnext: post-deploy pre-provisions the OIDC admin (else signup-disabled 403)` | `post_deploy_pre_provisions_the_oidc_admin` |

use std::os::unix::fs::PermissionsExt;

use agent_cloud_tests::repo;
use agent_cloud_tests::yaml::{self, Node};
use regex::Regex;

const COMPOSE: &str = "platform/services/erpnext/deployment/compose.yml";
const COMPOSE_LOCAL: &str = "platform/services/erpnext/deployment/compose.local.yml";
const DEPLOY_SH: &str = "platform/services/erpnext/deployment/deploy.sh";
const POST_DEPLOY_SH: &str = "platform/services/erpnext/deployment/post-deploy.sh";
const ENV_TEMPLATE: &str = "platform/services/erpnext/deployment/templates/env.j2";
const DEPLOY_PLAYBOOK: &str = "platform/playbooks/deploy-erpnext.yml";
const CLEAN_PLAYBOOK: &str = "platform/playbooks/clean-deploy-erpnext.yml";

/// The slim tier's services; each carries an explicit `container_name: erpnext-<service>`.
const SLIM_SERVICES: [&str; 9] = [
    "db",
    "redis-cache",
    "redis-queue",
    "configurator",
    "backend",
    "frontend",
    "websocket",
    "queue",
    "scheduler",
];

/// The file at `rel`, read as text. A missing file panics with its path.
fn read(rel: &str) -> String {
    std::fs::read_to_string(repo::path(rel)).unwrap_or_else(|e| panic!("{rel}: {e}"))
}

/// `grep -E`: true when some line of `text` matches the ERE `pattern`. The regex crate reads the
/// constructs these patterns use as grep does, and `^` and `$` anchor each line.
fn any_line(text: &str, pattern: &str) -> bool {
    let re = Regex::new(pattern).expect("a valid pattern");
    text.lines().any(|line| re.is_match(line))
}

/// `grep -qE pattern file` must succeed.
fn assert_line(rel: &str, pattern: &str) {
    assert!(
        any_line(&read(rel), pattern),
        "{rel}: no line matches `{pattern}`"
    );
}

/// `! grep -qE pattern file`: no line of the file may match.
fn refute_line(rel: &str, pattern: &str) {
    assert!(
        !any_line(&read(rel), pattern),
        "{rel}: a line matches the forbidden `{pattern}`"
    );
}

/// `grep -qF literal file` must succeed.
fn assert_text(rel: &str, literal: &str) {
    assert!(read(rel).contains(literal), "{rel}: `{literal}` is missing");
}

/// `! grep -qF literal file`: the literal must be absent.
fn refute_text(rel: &str, literal: &str) {
    assert!(
        !read(rel).contains(literal),
        "{rel}: the forbidden `{literal}` is present"
    );
}

/// The script at `rel` is executable and starts `#!/usr/bin/env bash`.
fn bash_script(rel: &str) {
    let meta = std::fs::metadata(repo::path(rel)).unwrap_or_else(|e| panic!("{rel}: {e}"));
    assert!(
        meta.permissions().mode() & 0o111 != 0,
        "{rel} is not executable"
    );
    assert!(
        read(rel).starts_with("#!/usr/bin/env bash"),
        "{rel}: the first line is not `#!/usr/bin/env bash`"
    );
}

/// One service of the compose file at `rel`.
fn service(rel: &str, name: &str) -> Node {
    let doc = yaml::load_one(&repo::path(rel)).unwrap_or_else(|| panic!("{rel} is empty"));
    doc.get("services")
        .and_then(|services| services.get(name))
        .cloned()
        .unwrap_or_else(|| panic!("{rel}: no `{name}` service"))
}

#[test]
fn compose_has_the_expected_top_level_shape() {
    assert_line(COMPOSE, r"^name: erpnext");
    assert_line(COMPOSE, r"^services:");
    assert_line(COMPOSE, r"^\s+frontend:");
}

#[test]
fn compose_env_parameterizes_images_and_the_frontend_bind_and_port() {
    assert_line(COMPOSE, r"\$\{ERPNEXT_IMAGE");
    assert_line(COMPOSE, r"\$\{ERPNEXT_VERSION");
    assert_line(COMPOSE, r"\$\{ERPNEXT_BIND");
    assert_line(COMPOSE, r"\$\{ERPNEXT_PORT");
}

#[test]
fn images_are_fully_qualified_and_pinned_with_no_latest_tag() {
    // frappe/erpnext is pinned through the required ERPNEXT_VERSION (.env); the backing services
    // are pinned inline with fully-qualified docker.io/ references.
    assert_line(
        COMPOSE,
        r"\$\{ERPNEXT_IMAGE:-docker\.io/frappe/erpnext\}:\$\{ERPNEXT_VERSION",
    );
    assert_line(COMPOSE, r"docker\.io/library/mariadb:10\.6");
    assert_line(COMPOSE, r"docker\.io/library/redis:7-alpine");
    refute_line(COMPOSE, r":latest");
}

#[test]
fn compose_defines_the_slim_tier_services_with_explicit_container_names() {
    for svc in SLIM_SERVICES {
        let want = format!("erpnext-{svc}");
        let found = service(COMPOSE, svc);
        let name = found.get("container_name").and_then(Node::as_str);
        assert_eq!(
            name,
            Some(want.as_str()),
            "{COMPOSE}: `{svc}` must set `container_name: {want}`"
        );
    }
}

#[test]
fn slim_tier_excludes_the_prod_only_minio_and_split_workers() {
    refute_text(COMPOSE, "container_name: erpnext-minio");
    refute_text(COMPOSE, "container_name: erpnext-queue-short");
    refute_text(COMPOSE, "container_name: erpnext-queue-long");
}

#[test]
fn compose_has_no_hardcoded_credentials() {
    refute_line(COMPOSE, r"PASSWORD: [A-Za-z0-9]{8,}");
}

#[test]
fn compose_has_no_rfc1918_addresses() {
    refute_line(
        COMPOSE,
        r"192\.168\.|10\.[0-9]+\.|172\.(1[6-9]|2[0-9]|3[01])\.",
    );
}

#[test]
fn deploy_sh_is_executable_bash_sources_common_and_holds_no_secrets() {
    bash_script(DEPLOY_SH);
    assert_text(DEPLOY_SH, "common.sh");
    assert_line(DEPLOY_SH, r"\bcompose (pull|up|run)");
    refute_line(DEPLOY_SH, r"\b(gen_secret|put_secret|get_secret|bao_|BAO_)");
}

#[test]
fn deploy_sh_never_hardcodes_a_container_engine() {
    refute_line(DEPLOY_SH, r"^\s*(docker|podman) ");
}

#[test]
fn post_deploy_sh_is_an_executable_idempotent_bootstrap_with_no_secret_handling() {
    bash_script(POST_DEPLOY_SH);
    assert_text(POST_DEPLOY_SH, "bench new-site");
    assert_line(POST_DEPLOY_SH, r"test -d sites/");
    refute_line(
        POST_DEPLOY_SH,
        r"\b(gen_secret|put_secret|get_secret|bao_|BAO_)",
    );
}

#[test]
fn env_template_pulls_every_credential_from_openbao_with_no_literals() {
    assert_text(
        ENV_TEMPLATE,
        "DB_PASSWORD={{ secrets.mariadb_root_password }}",
    );
    assert_text(ENV_TEMPLATE, "ADMIN_PASSWORD={{ secrets.admin_password }}");
    // The guard reads credential literals in value position (an assigned secret), not prose
    // mentions of LOCAL_FAKE_ in the header comment.
    refute_line(
        ENV_TEMPLATE,
        r"(?i)=[^{]*LOCAL_FAKE|password=[A-Za-z0-9]{8}",
    );
}

#[test]
fn env_template_uses_only_the_approved_jinja2_namespaces() {
    // The variable name is the identifier after each `{{` and its spaces.
    let text = read(ENV_TEMPLATE);
    let variable = Regex::new(r"\{\{ *([a-z_.]+)").expect("a valid pattern");
    let tokens: Vec<&str> = variable
        .captures_iter(&text)
        .map(|caps| caps.get(1).expect("group 1 is set").as_str())
        .collect();
    assert!(
        !tokens.is_empty(),
        "{ENV_TEMPLATE}: no `{{{{ variable }}}}` found, so no namespace was checked"
    );
    for token in tokens {
        assert!(
            ["secrets.", "erpnext_", "ansible_"]
                .iter()
                .any(|namespace| token.starts_with(namespace)),
            "{ENV_TEMPLATE}: unapproved Jinja2 namespace `{token}`"
        );
    }
}

#[test]
fn local_overlay_adds_limits_selinux_and_local_dev_but_publishes_no_ports() {
    assert_text(COMPOSE_LOCAL, "mem_limit:");
    assert_text(COMPOSE_LOCAL, "label=disable");
    assert_text(COMPOSE_LOCAL, "local-dev");
    refute_line(COMPOSE_LOCAL, r"^\s*ports:");
}

#[test]
fn local_overlay_puts_the_frontend_on_local_dev() {
    // The frontend service's own networks, in list or mapping form, so another service joining
    // local-dev cannot stand in for it.
    let frontend = service(COMPOSE_LOCAL, "frontend");
    let networks: Vec<&str> = match frontend.get("networks") {
        Some(Node::Seq(items)) => items.iter().filter_map(Node::as_str).collect(),
        Some(Node::Map(entries)) => entries.iter().filter_map(|(k, _)| k.as_str()).collect(),
        _ => vec![],
    };
    assert!(
        networks.contains(&"local-dev"),
        "{COMPOSE_LOCAL}: the frontend does not join the `local-dev` network (networks: {networks:?})"
    );
}

#[test]
fn deploy_playbook_is_composable_and_not_legacy() {
    assert_text(DEPLOY_PLAYBOOK, "tasks/place-monorepo.yml");
    assert_text(DEPLOY_PLAYBOOK, "tasks/manage-secrets.yml");
    assert_text(DEPLOY_PLAYBOOK, "bash deploy.sh");
    assert_text(DEPLOY_PLAYBOOK, "bash post-deploy.sh");
    refute_text(DEPLOY_PLAYBOOK, "import_playbook: deploy-service.yml");
    assert_text(DEPLOY_PLAYBOOK, "name: mariadb_root_password");
}

#[test]
fn clean_deploy_playbook_destroys_then_redeploys() {
    assert_text(CLEAN_PLAYBOOK, "tasks/clean-service.yml");
    assert_text(CLEAN_PLAYBOOK, "import_playbook: deploy-erpnext.yml");
    assert_line(CLEAN_PLAYBOOK, r"(?i)danger");
}

#[test]
fn social_login_key_sets_custom_base_url() {
    // Without custom_base_url, Frappe resolves the relative authorize and token paths against
    // the SITE url (erp.<zone>) instead of base_url (the IdP), which is a 404.
    assert_text(POST_DEPLOY_SH, "doc.custom_base_url = 1");
}

#[test]
fn post_deploy_pre_provisions_the_oidc_admin() {
    // Frappe matches the OIDC identity by email. If none exists it self-registers, which Website
    // Settings disables (a 403). So the SSO admin is created as a System Manager ahead of login,
    // and the email must match the Authentik identity.
    assert_text(POST_DEPLOY_SH, "step_oidc_admin");
    assert_text(POST_DEPLOY_SH, "ERPNEXT_OIDC_ADMIN_EMAIL");
    // Full access is the union of every assignable role (System Manager alone is admin-only).
    assert_text(POST_DEPLOY_SH, "user.add_roles(*roles)");
    assert_text(POST_DEPLOY_SH, r#""Administrator", "All", "Guest""#);
    // The email is templated to match the Authentik agent-cloud-admin identity.
    assert_text(ENV_TEMPLATE, "ERPNEXT_OIDC_ADMIN_EMAIL=");
}
