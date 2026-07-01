#!/usr/bin/env bats
# Structural tests for the OpenHands (Agent Canvas) service
# (platform/services/openhands). Verifies the composable shape: env-parameterized
# compose, the Docker-socket runtime requirement, a dependency-safe healthcheck,
# container-only deploy.sh (no secret generation), non-secret env.j2, and a
# forward_auth + tls-internal Caddy fragment. No hardcoded IPs/credentials.
#
# Run: bats platform/tests/test_service_openhands.bats

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  DEPLOY_DIR="$REPO_ROOT/platform/services/openhands/deployment"
}

@test "openhands: compose env-parameterizes image + bind/port" {
  local f="$DEPLOY_DIR/compose.yml"
  [ -f "$f" ]
  grep -qE '\$\{OPENHANDS_IMAGE' "$f"
  grep -qE '\$\{OPENHANDS_BIND' "$f"
  grep -qE '\$\{OPENHANDS_PORT' "$f"
}

@test "openhands: image is fully-qualified and pinned (no :latest drift)" {
  local f="$DEPLOY_DIR/compose.yml"
  grep -qE 'docker\.openhands\.dev/openhands/openhands:[0-9]' "$f"
  ! grep -qE 'openhands:latest' "$f"
}

@test "openhands: mounts the host Docker socket + state volume + host-gateway" {
  local f="$DEPLOY_DIR/compose.yml"
  grep -q '/var/run/docker.sock:/var/run/docker.sock' "$f"
  grep -qE 'openhands-state:/\.openhands' "$f"
  grep -q 'host.docker.internal:host-gateway' "$f"
}

@test "openhands: healthcheck does not assume curl (python socket check)" {
  local f="$DEPLOY_DIR/compose.yml"
  grep -q 'healthcheck:' "$f"
  grep -qE 'CMD-SHELL.*python3' "$f"
}

@test "openhands: compose has no hardcoded credentials" {
  local f="$DEPLOY_DIR/compose.yml"
  ! grep -qiE '(password|secret|token|api_key)\s*[:=]\s*["'\''0-9A-Za-z]{8}' "$f"
}

@test "openhands: compose has no RFC1918 IPs" {
  local f="$DEPLOY_DIR/compose.yml"
  ! grep -qE '192\.168\.|10\.[0-9]+\.|172\.(1[6-9]|2[0-9]|3[01])\.' "$f"
}

@test "openhands: deploy.sh is executable, bash, sources common.sh, uses compose, no secrets" {
  local f="$DEPLOY_DIR/deploy.sh"
  [ -f "$f" ] && [ -x "$f" ]
  head -1 "$f" | grep -qE '^#!/usr/bin/env bash'
  grep -q 'common.sh' "$f"
  grep -qE '\bcompose\b' "$f"
  ! grep -qiE 'openssl rand|secret_id|vault|bao ' "$f"
}

@test "openhands: deploy.sh never hardcodes a container engine" {
  local f="$DEPLOY_DIR/deploy.sh"
  grep -q 'detect_runtime' "$f"
}

@test "openhands: env template is non-secret config (image pins, no literals/IPs)" {
  local f="$DEPLOY_DIR/templates/env.j2"
  [ -f "$f" ]
  grep -qE 'AGENT_SERVER_IMAGE_(REPOSITORY|TAG)' "$f"
  ! grep -qE '192\.168\.|10\.[0-9]+\.' "$f"
  ! grep -qiE '(password|secret|token)=' "$f"
}

@test "openhands: caddy fragment gates via forward_auth + tls internal + SSE flush" {
  local f="$DEPLOY_DIR/templates/caddy-site.j2"
  [ -f "$f" ]
  grep -q 'tls internal' "$f"
  grep -q 'forward_auth' "$f"
  grep -q '/outpost.goauthentik.io/auth/caddy' "$f"
  grep -q 'flush_interval -1' "$f"
  # strips client-supplied identity headers (anti-spoofing)
  grep -q 'request_header -X-authentik-username' "$f"
}

@test "openhands: deploy playbook is composable (clone + env, container-only deploy)" {
  local f="$REPO_ROOT/platform/playbooks/deploy-openhands.yml"
  [ -f "$f" ]
  grep -q 'tasks/distribute-caddy-site.yml' "$f"
  grep -q 'caddy_composable' "$f"
}
