//! The static tier of the honcho memory service (plan 24, Phase 4).
//!
//! Verifies the composable shape of `platform/services/honcho`: an env-parameterized four-container
//! compose (api, deriver, its own pgvector Postgres, redis) with dependency-safe healthchecks and
//! only the api publishing a port; a container-only `deploy.sh` (no secret generation) with the
//! local-image pull tolerance; a non-secret `env.j2` (secrets come from OpenBao); a composable deploy
//! playbook (manage-secrets, the existing Gemini key, image pre-flight, reboot linger); and a valid
//! config-as-code Authentik forward_auth blueprint for `/docs`. No hardcoded IPs or credentials.
//!
//! Replaces `platform/tests/test_service_honcho.bats`. Structural only: file reads, no live deploy.
//! The BATS absence checks (`! grep ...`) never failed under `set -e` (CONTRIBUTING.md, "Writing
//! BATS Tests"), so every check here that must not match is a real assertion.
//!
//! Port map (BATS @test → Rust fn):
//!
//! | BATS @test | Rust fn |
//! |---|---|
//! | `honcho: compose env-parameterizes image + bind/port` | `compose_env_parameterizes_image_and_bind_and_port` |
//! | `honcho: prod default image is a pinned GHCR tag (no :latest drift)` | `prod_default_image_is_a_pinned_ghcr_tag` |
//! | `honcho: four-container stack (api + deriver + pgvector db + redis)` | `compose_is_the_four_container_stack` |
//! | `honcho: only the api publishes a port (db/redis/deriver stay internal)` | `only_the_api_publishes_a_port` |
//! | `honcho: healthchecks on api + datastores, api probes /health (no curl assumption)` | `api_and_datastores_have_healthchecks_and_the_api_probes_health` |
//! | `honcho: compose has no hardcoded credentials` | `compose_has_no_hardcoded_credentials` |
//! | `honcho: compose has no RFC1918 IPs` | `compose_has_no_rfc1918_addresses` |
//! | `honcho: deploy.sh is executable, bash, sources common.sh, uses compose, no secrets` | `deploy_sh_is_executable_bash_sources_common_and_holds_no_secrets` |
//! | `honcho: deploy.sh detects the engine + tolerates a local-built image` | `deploy_sh_detects_the_engine_and_tolerates_a_local_built_image` |
//! | `honcho: env template pulls secrets from OpenBao, JWT on /v3, no literals/IPs` | `env_template_pulls_secrets_from_openbao_with_no_literals_or_addresses` |
//! | `honcho: deploy playbook is composable (manage-secrets, secret defs, existing Gemini key, env templates)` | `deploy_playbook_is_composable_with_an_existing_gemini_key` |
//! | `honcho: validate-secrets checks the operator-seeded Gemini key as well as the generated pair` | `validate_secrets_checks_the_operator_seeded_gemini_key` |
//! | `honcho: playbook gates on the pinned image + enables reboot linger (prod-gated)` | `playbook_gates_on_the_pinned_image_and_enables_reboot_linger` |
//! | `honcho: Authentik /docs forward_auth blueprint is valid config-as-code (no client secret)` | `authentik_docs_forward_auth_blueprint_is_config_as_code_with_no_client_secret` |

use std::os::unix::fs::PermissionsExt;

use agent_cloud_tests::repo;
use regex::Regex;

const COMPOSE: &str = "platform/services/honcho/deployment/compose.yml";
const DEPLOY_SH: &str = "platform/services/honcho/deployment/deploy.sh";
const ENV_TEMPLATE: &str = "platform/services/honcho/deployment/templates/env.j2";
const PLAYBOOK: &str = "platform/playbooks/deploy-honcho.yml";
const VALIDATE_SECRETS: &str = "platform/playbooks/validate-secrets.yml";
const ENABLE_LINGER: &str = "platform/playbooks/tasks/enable-linger.yml";
const BLUEPRINT: &str =
    "platform/services/authentik/deployment/blueprints/honcho-docs-forward-auth.yaml";

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

/// The first line of `text`, as `head -1` prints it.
fn first_line(text: &str) -> &str {
    text.lines().next().unwrap_or("")
}

/// `[ -x file ]`, read as the file's execute bits.
fn is_executable(rel: &str) -> bool {
    let meta = std::fs::metadata(repo::path(rel)).unwrap_or_else(|e| panic!("{rel}: {e}"));
    meta.permissions().mode() & 0o111 != 0
}

#[test]
fn compose_env_parameterizes_image_and_bind_and_port() {
    assert!(repo::path(COMPOSE).is_file(), "{COMPOSE} does not exist");
    assert_line(COMPOSE, r"\$\{HONCHO_IMAGE");
    assert_line(COMPOSE, r"\$\{HONCHO_BIND");
    assert_line(COMPOSE, r"\$\{HONCHO_PORT");
}

#[test]
fn prod_default_image_is_a_pinned_ghcr_tag() {
    assert_line(
        COMPOSE,
        r"\$\{HONCHO_IMAGE:-ghcr\.io/uhstray-io/honcho:v[0-9]",
    );
    refute_line(COMPOSE, r"honcho:latest");
}

#[test]
fn compose_is_the_four_container_stack() {
    assert_line(COMPOSE, r"^\s+honcho-api:");
    assert_line(COMPOSE, r"^\s+honcho-deriver:");
    assert_line(COMPOSE, r"^\s+honcho-db:");
    assert_line(COMPOSE, r"^\s+honcho-redis:");
    // The db is honcho's OWN pgvector Postgres, not the platform Postgres.
    assert_line(COMPOSE, r"pgvector/pgvector:pg");
}

#[test]
fn only_the_api_publishes_a_port() {
    // A single ports: stanza, the api's. A stray db or redis publish would add a second one.
    let port_key = Regex::new(r"^\s+ports:").expect("a valid pattern");
    let stanzas = read(COMPOSE)
        .lines()
        .filter(|line| port_key.is_match(line))
        .count();
    assert_eq!(
        stanzas, 1,
        "{COMPOSE}: expected exactly one `ports:` stanza (the api's), found {stanzas}"
    );
}

#[test]
fn api_and_datastores_have_healthchecks_and_the_api_probes_health() {
    // api (python urlopen), db (pg_isready) and redis (redis-cli) are each healthchecked.
    let healthchecks = read(COMPOSE)
        .lines()
        .filter(|line| line.contains("healthcheck:"))
        .count();
    assert!(
        healthchecks >= 3,
        "{COMPOSE}: expected at least 3 `healthcheck:` keys, found {healthchecks}"
    );
    assert_line(COMPOSE, r"CMD-SHELL.*python");
    assert_text(COMPOSE, "/health");
    assert_text(COMPOSE, "pg_isready");
}

#[test]
fn compose_has_no_hardcoded_credentials() {
    refute_line(
        COMPOSE,
        r#"(?i)(password|secret|token|api_key)\s*[:=]\s*["'0-9A-Za-z]{8}"#,
    );
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
    assert!(
        repo::path(DEPLOY_SH).is_file(),
        "{DEPLOY_SH} does not exist"
    );
    assert!(is_executable(DEPLOY_SH), "{DEPLOY_SH} is not executable");
    let text = read(DEPLOY_SH);
    assert!(
        first_line(&text).starts_with("#!/usr/bin/env bash"),
        "{DEPLOY_SH}: the first line is not `#!/usr/bin/env bash`"
    );
    assert_text(DEPLOY_SH, "common.sh");
    assert_line(DEPLOY_SH, r"\bcompose (pull|up)");
    // Lifecycle only: no secret generation and no OpenBao interaction (Deploy Rule #2).
    refute_line(
        DEPLOY_SH,
        r"(?i)openssl rand|secret_id|vault|\bbao |gen_secret|put_secret|get_secret",
    );
}

#[test]
fn deploy_sh_detects_the_engine_and_tolerates_a_local_built_image() {
    assert_text(DEPLOY_SH, "detect_runtime");
    // A pull failure is tolerated ONLY when the image is already in the local store.
    assert_text(DEPLOY_SH, "image exists");
    assert_text(DEPLOY_SH, "wait_for_healthy honcho-api");
}

#[test]
fn env_template_pulls_secrets_from_openbao_with_no_literals_or_addresses() {
    assert!(
        repo::path(ENV_TEMPLATE).is_file(),
        "{ENV_TEMPLATE} does not exist"
    );
    assert_text(
        ENV_TEMPLATE,
        "AUTH_JWT_SECRET={{ secrets.honcho_jwt_secret }}",
    );
    assert_text(
        ENV_TEMPLATE,
        "POSTGRES_PASSWORD={{ secrets.honcho_db_password }}",
    );
    assert_text(
        ENV_TEMPLATE,
        "LLM_GEMINI_API_KEY={{ secrets.gemini_api_key }}",
    );
    assert_line(ENV_TEMPLATE, r"AUTH_USE_AUTH=true");
    refute_line(ENV_TEMPLATE, r"192\.168\.|10\.[0-9]+\.");
    refute_line(
        ENV_TEMPLATE,
        r"(?i)(password|secret|token|api_key)\s*[:=]\s*[A-Za-z0-9]{8,}",
    );
}

#[test]
fn deploy_playbook_is_composable_with_an_existing_gemini_key() {
    assert!(repo::path(PLAYBOOK).is_file(), "{PLAYBOOK} does not exist");
    assert_text(PLAYBOOK, "tasks/manage-secrets.yml");
    assert_text(PLAYBOOK, "_secret_definitions:");
    assert_text(PLAYBOOK, "honcho_jwt_secret");
    assert_text(PLAYBOOK, "honcho_db_password");
    // The Gemini key is honcho's own: declared `existing` (operator-seeded, never generated) and
    // required before any write, so a missing key fails the deploy up front.
    assert_text(PLAYBOOK, "{ name: gemini_api_key, type: existing }");
    assert_text(PLAYBOOK, "_required_existing_secrets: [gemini_api_key]");
    // No shared read from another platform's path.
    refute_text(PLAYBOOK, "_shared_reads:");
    assert!(
        !read(PLAYBOOK).to_lowercase().contains("nemoclaw"),
        "{PLAYBOOK}: the forbidden `nemoclaw` is present (case-insensitive)"
    );
    assert_text(PLAYBOOK, "_env_templates:");
}

#[test]
fn validate_secrets_checks_the_operator_seeded_gemini_key() {
    assert_text(VALIDATE_SECRETS, "_secrets.gemini_api_key is defined");
}

#[test]
fn playbook_gates_on_the_pinned_image_and_enables_reboot_linger() {
    // Pre-flight fails fast on a fresh prod VM if build-honcho.yml never published.
    assert_text(PLAYBOOK, "_honcho_image");
    assert_text(PLAYBOOK, "build-honcho");
    // Rootless podman reboot persistence: the place-monorepo preamble enables linger.
    assert_text(PLAYBOOK, "tasks/place-monorepo.yml");
    // The prod gate lives inside the task (self-gating). The contract is asserted, not the exact
    // expression: enable-linger reads local_mode.
    assert_text(ENABLE_LINGER, "local_mode");
}

#[test]
fn authentik_docs_forward_auth_blueprint_is_config_as_code_with_no_client_secret() {
    assert!(
        repo::path(BLUEPRINT).is_file(),
        "{BLUEPRINT} does not exist"
    );
    assert_line(BLUEPRINT, r"^version: 1");
    assert_text(BLUEPRINT, "authentik_providers_proxy.proxyprovider");
    assert_text(BLUEPRINT, "mode: forward_single");
    // The proxy provider manages its own tokens, so no client secret is committed to git.
    assert!(
        !read(BLUEPRINT).to_lowercase().contains("client_secret"),
        "{BLUEPRINT}: the forbidden `client_secret` is present (case-insensitive)"
    );
}
