#!/usr/bin/env bats
# Structural tests for the tududi service (platform/services/tududi). Verifies
# the composable shape: env-parameterized single-container compose (SQLite), a
# dependency-safe healthcheck, container-only deploy.sh (no secret generation),
# a non-secret env.j2 (SSO-only + secrets from OpenBao), a composable deploy
# playbook (manage-secrets + reboot linger, no secret-gen), and a valid
# config-as-code Authentik OIDC blueprint. No hardcoded IPs/credentials.
#
# Structural only (grep/file asserts) — no live deploy.
# Run: bats platform/tests/test_service_tududi.bats

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  DEPLOY_DIR="$REPO_ROOT/platform/services/tududi/deployment"
  PLAYBOOK="$REPO_ROOT/platform/playbooks/deploy-tududi.yml"
}

@test "tududi: compose env-parameterizes image + bind/port" {
  local f="$DEPLOY_DIR/compose.yml"
  [ -f "$f" ]
  grep -qE '\$\{TUDUDI_IMAGE' "$f"
  grep -qE '\$\{TUDUDI_BIND' "$f"
  grep -qE '\$\{TUDUDI_PORT' "$f"
}

@test "tududi: image is fully-qualified and pinned (no :latest drift)" {
  local f="$DEPLOY_DIR/compose.yml"
  # Pinned upstream tag — 1.2.0/latest has a broken first-boot migration (env.j2).
  grep -qE 'docker\.io/chrisvel/tududi:[0-9]' "$f"
  ! grep -qE 'tududi:latest' "$f"
}

@test "tududi: single container persists SQLite + uploads on named volumes" {
  local f="$DEPLOY_DIR/compose.yml"
  grep -qE '^\s+tududi:' "$f"
  grep -qE 'tududi-db:/app/backend/db' "$f"
  grep -qE 'tududi-uploads:/app/backend/uploads' "$f"
}

@test "tududi: healthcheck does not assume curl (node /api/health check)" {
  local f="$DEPLOY_DIR/compose.yml"
  grep -q 'healthcheck:' "$f"
  # CMD-SHELL form (podman-compose mis-quotes the ["CMD",...] exec list).
  grep -qE 'CMD-SHELL.*node' "$f"
  grep -q '/api/health' "$f"
}

@test "tududi: compose has no hardcoded credentials" {
  local f="$DEPLOY_DIR/compose.yml"
  ! grep -qiE '(password|secret|token|api_key)\s*[:=]\s*["'\''0-9A-Za-z]{8}' "$f"
}

@test "tududi: compose has no RFC1918 IPs" {
  local f="$DEPLOY_DIR/compose.yml"
  ! grep -qE '192\.168\.|10\.[0-9]+\.|172\.(1[6-9]|2[0-9]|3[01])\.' "$f"
}

@test "tududi: deploy.sh is executable, bash, sources common.sh, uses compose, no secrets" {
  local f="$DEPLOY_DIR/deploy.sh"
  [ -f "$f" ] && [ -x "$f" ]
  head -1 "$f" | grep -qE '^#!/usr/bin/env bash'
  grep -q 'common.sh' "$f"
  grep -qE '\bcompose (pull|up)' "$f"
  # Lifecycle only — no secret generation / OpenBao interaction (Deploy Rule #2).
  ! grep -qiE 'openssl rand|secret_id|vault|\bbao |gen_secret|put_secret|get_secret' "$f"
}

@test "tududi: deploy.sh never hardcodes a container engine" {
  grep -q 'detect_runtime' "$DEPLOY_DIR/deploy.sh"
}

@test "tududi: env template is SSO-only, secrets from OpenBao, no literals/IPs" {
  local f="$DEPLOY_DIR/templates/env.j2"
  [ -f "$f" ]
  # Secrets are Jinja refs into the secrets.* namespace, never literals.
  grep -qF 'TUDUDI_SESSION_SECRET={{ secrets.tududi_session_secret }}' "$f"
  grep -qF 'TUDUDI_USER_PASSWORD={{ secrets.tududi_admin_password }}' "$f"
  grep -qF 'OIDC_CLIENT_SECRET={{ secrets.tududi_oidc_client_secret }}' "$f"
  # SSO-only with the break-glass admin as fallback; trust proxy behind Caddy.
  grep -qE 'PASSWORD_AUTH_ENABLED=false' "$f"
  grep -qE 'TUDUDI_TRUST_PROXY=true' "$f"
  ! grep -qE '192\.168\.|10\.[0-9]+\.' "$f"
  ! grep -qiE '(password|secret|token|api_key)\s*[:=]\s*[A-Za-z0-9]{8,}' "$f"
}

@test "tududi: deploy playbook is composable (manage-secrets, secret defs + env templates)" {
  [ -f "$PLAYBOOK" ]
  grep -q 'tasks/manage-secrets.yml' "$PLAYBOOK"
  grep -q '_secret_definitions:' "$PLAYBOOK"
  grep -q 'tududi_session_secret' "$PLAYBOOK"
  grep -q 'tududi_admin_password' "$PLAYBOOK"
  # OIDC client secret is shared-read from authentik (the IdP owns it).
  grep -q '_shared_reads:' "$PLAYBOOK"
  grep -q 'tududi_oidc_client_secret' "$PLAYBOOK"
  grep -q '_env_templates:' "$PLAYBOOK"
}

@test "tududi: playbook enables reboot linger for rootless podman (prod-gated)" {
  # Rootless podman is daemonless — linger auto-starts the container after a
  # reboot. Gated to non-local so local-dev (podman machine) skips it.
  grep -q 'tasks/enable-linger.yml' "$PLAYBOOK"
  grep -qE 'not \(local_mode \| default\(false\) \| bool\)' "$PLAYBOOK"
}

@test "tududi: Authentik OIDC blueprint is valid config-as-code (no committed secret)" {
  local f="$REPO_ROOT/platform/services/authentik/deployment/blueprints/tududi-oidc.yaml"
  [ -f "$f" ]
  grep -qE '^version: 1' "$f"
  grep -q 'authentik_providers_oauth2.oauth2provider' "$f"
  grep -q 'client_id: tududi' "$f"
  # The client secret is injected from env via !Env — never a literal in git.
  grep -q 'client_secret: !Env' "$f"
  ! grep -qiE 'client_secret:\s*[A-Za-z0-9]{8,}' "$f"
}
