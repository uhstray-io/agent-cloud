#!/usr/bin/env bats
# Structural tests for the honcho service (platform/services/honcho). Verifies
# the composable shape: env-parameterized four-container compose (api + deriver
# + own pgvector Postgres + redis), dependency-safe healthchecks, only the api
# publishing a port, container-only deploy.sh (no secret generation) with the
# local-image pull tolerance, a non-secret env.j2 (secrets from OpenBao), a
# composable deploy playbook (manage-secrets + shared-read + image pre-flight +
# reboot linger), and a valid config-as-code Authentik forward_auth blueprint
# for /docs. No hardcoded IPs/credentials.
#
# Structural only (grep/file asserts) — no live deploy.
# Run: bats platform/tests/test_service_honcho.bats

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  DEPLOY_DIR="$REPO_ROOT/platform/services/honcho/deployment"
  PLAYBOOK="$REPO_ROOT/platform/playbooks/deploy-honcho.yml"
}

@test "honcho: compose env-parameterizes image + bind/port" {
  local f="$DEPLOY_DIR/compose.yml"
  [ -f "$f" ]
  grep -qE '\$\{HONCHO_IMAGE' "$f"
  grep -qE '\$\{HONCHO_BIND' "$f"
  grep -qE '\$\{HONCHO_PORT' "$f"
}

@test "honcho: prod default image is a pinned GHCR tag (no :latest drift)" {
  local f="$DEPLOY_DIR/compose.yml"
  grep -qE '\$\{HONCHO_IMAGE:-ghcr\.io/uhstray-io/honcho:v[0-9]' "$f"
  ! grep -qE 'honcho:latest' "$f"
}

@test "honcho: four-container stack (api + deriver + pgvector db + redis)" {
  local f="$DEPLOY_DIR/compose.yml"
  grep -qE '^\s+honcho-api:' "$f"
  grep -qE '^\s+honcho-deriver:' "$f"
  grep -qE '^\s+honcho-db:' "$f"
  grep -qE '^\s+honcho-redis:' "$f"
  # db is honcho's OWN pgvector Postgres, not the platform Postgres.
  grep -qE 'pgvector/pgvector:pg' "$f"
}

@test "honcho: only the api publishes a port (db/redis/deriver stay internal)" {
  local f="$DEPLOY_DIR/compose.yml"
  # A single ports: stanza — the api's. A stray db/redis publish would add more.
  [ "$(grep -cE '^\s+ports:' "$f")" -eq 1 ]
}

@test "honcho: healthchecks on api + datastores, api probes /health (no curl assumption)" {
  local f="$DEPLOY_DIR/compose.yml"
  # api (python urlopen), db (pg_isready), redis (redis-cli) each healthchecked.
  [ "$(grep -c 'healthcheck:' "$f")" -ge 3 ]
  grep -qE 'CMD-SHELL.*python' "$f"
  grep -q '/health' "$f"
  grep -q 'pg_isready' "$f"
}

@test "honcho: compose has no hardcoded credentials" {
  local f="$DEPLOY_DIR/compose.yml"
  ! grep -qiE '(password|secret|token|api_key)\s*[:=]\s*["'\''0-9A-Za-z]{8}' "$f"
}

@test "honcho: compose has no RFC1918 IPs" {
  local f="$DEPLOY_DIR/compose.yml"
  ! grep -qE '192\.168\.|10\.[0-9]+\.|172\.(1[6-9]|2[0-9]|3[01])\.' "$f"
}

@test "honcho: deploy.sh is executable, bash, sources common.sh, uses compose, no secrets" {
  local f="$DEPLOY_DIR/deploy.sh"
  [ -f "$f" ] && [ -x "$f" ]
  head -1 "$f" | grep -qE '^#!/usr/bin/env bash'
  grep -q 'common.sh' "$f"
  grep -qE '\bcompose (pull|up)' "$f"
  # Lifecycle only — no secret generation / OpenBao interaction (Deploy Rule #2).
  ! grep -qiE 'openssl rand|secret_id|vault|\bbao |gen_secret|put_secret|get_secret' "$f"
}

@test "honcho: deploy.sh detects the engine + tolerates a local-built image" {
  local f="$DEPLOY_DIR/deploy.sh"
  grep -q 'detect_runtime' "$f"
  # Pull failure is tolerated ONLY when the image is already in the local store.
  grep -q 'image exists' "$f"
  grep -q 'wait_for_healthy honcho-api' "$f"
}

@test "honcho: env template pulls secrets from OpenBao, JWT on /v3, no literals/IPs" {
  local f="$DEPLOY_DIR/templates/env.j2"
  [ -f "$f" ]
  grep -qF 'AUTH_JWT_SECRET={{ secrets.honcho_jwt_secret }}' "$f"
  grep -qF 'POSTGRES_PASSWORD={{ secrets.honcho_db_password }}' "$f"
  grep -qF 'LLM_GEMINI_API_KEY={{ secrets.gemini_api_key }}' "$f"
  grep -qE 'AUTH_USE_AUTH=true' "$f"
  ! grep -qE '192\.168\.|10\.[0-9]+\.' "$f"
  ! grep -qiE '(password|secret|token|api_key)\s*[:=]\s*[A-Za-z0-9]{8,}' "$f"
}

@test "honcho: deploy playbook is composable (manage-secrets, secret defs, shared-read, env templates)" {
  [ -f "$PLAYBOOK" ]
  grep -q 'tasks/manage-secrets.yml' "$PLAYBOOK"
  grep -q '_secret_definitions:' "$PLAYBOOK"
  grep -q 'honcho_jwt_secret' "$PLAYBOOK"
  grep -q 'honcho_db_password' "$PLAYBOOK"
  # Gemini key is shared-read from nemoclaw (which owns it).
  grep -q '_shared_reads:' "$PLAYBOOK"
  grep -q 'gemini_api_key' "$PLAYBOOK"
  grep -q '_env_templates:' "$PLAYBOOK"
}

@test "honcho: playbook gates on the pinned image + enables reboot linger (prod-gated)" {
  # Pre-flight fails fast on a fresh prod VM if build-honcho.yml never published.
  grep -q '_honcho_image' "$PLAYBOOK"
  grep -q 'build-honcho' "$PLAYBOOK"
  # Rootless podman reboot persistence, gated to non-local.
  grep -q 'tasks/enable-linger.yml' "$PLAYBOOK"
  grep -qE 'not \(local_mode \| default\(false\) \| bool\)' "$PLAYBOOK"
}

@test "honcho: Authentik /docs forward_auth blueprint is valid config-as-code (no client secret)" {
  local f="$REPO_ROOT/platform/services/authentik/deployment/blueprints/honcho-docs-forward-auth.yaml"
  [ -f "$f" ]
  grep -qE '^version: 1' "$f"
  grep -q 'authentik_providers_proxy.proxyprovider' "$f"
  grep -q 'mode: forward_single' "$f"
  # Proxy provider self-manages its tokens — no client secret in git.
  ! grep -qi 'client_secret' "$f"
}
