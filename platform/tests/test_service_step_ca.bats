#!/usr/bin/env bats
# Structural tests for the step-ca internal CA (platform/services/step-ca/deployment).
# Verifies the composable shape: env-parameterized compose, auto-init via
# DOCKER_STEPCA_INIT_*, chain-verified healthcheck, container-only deploy.sh
# (no key/secret generation), and an overlay-safe local profile that joins
# local-dev so Caddy can reach the CA by name.
#
# Run: bats platform/tests/test_service_step_ca.bats

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  DEPLOY_DIR="$REPO_ROOT/platform/services/step-ca/deployment"
}

@test "step-ca: compose env-parameterizes image, bind/port, and init knobs" {
  local f="$DEPLOY_DIR/compose.yml"
  [ -f "$f" ]
  grep -qE '\$\{STEPCA_IMAGE' "$f"
  grep -qE '\$\{STEPCA_BIND' "$f"
  grep -qE '\$\{STEPCA_PORT' "$f"
  grep -qE '\$\{STEPCA_INIT_PASSWORD\}' "$f"
}

@test "step-ca: prod default image is the pinned upstream tag (no :latest drift)" {
  local f="$DEPLOY_DIR/compose.yml"
  grep -qE '\$\{STEPCA_IMAGE:-docker\.io/smallstep/step-ca:[0-9]+\.[0-9]+\.[0-9]+\}' "$f"
}

@test "step-ca: auto-init via DOCKER_STEPCA_INIT_* (no manual key generation)" {
  local f="$DEPLOY_DIR/compose.yml"
  grep -q 'DOCKER_STEPCA_INIT_NAME:' "$f"
  grep -q 'DOCKER_STEPCA_INIT_DNS_NAMES:' "$f"
  grep -q 'DOCKER_STEPCA_INIT_PASSWORD:' "$f"
}

@test "step-ca: keys persist in a named volume (stable root across redeploys)" {
  local f="$DEPLOY_DIR/compose.yml"
  grep -qE 'step-ca-data:/home/step' "$f"
}

@test "step-ca: healthcheck is chain-verified (root cert, no --insecure)" {
  local f="$DEPLOY_DIR/compose.yml"
  grep -q 'healthcheck:' "$f"
  # Isolate the actual health command line, then assert it verifies against the
  # in-volume root and does NOT fall back to --insecure (a comment may mention
  # the flag, so check the command line itself, not the whole file).
  run grep 'step ca health' "$f"
  [ "$status" -eq 0 ]
  [[ "$output" == *"--root /home/step/certs/root_ca.crt"* ]]
  [[ "$output" != *"--insecure"* ]]
}

@test "step-ca: deploy.sh is executable, bash, sources common.sh, uses compose, no secrets" {
  local f="$DEPLOY_DIR/deploy.sh"
  [ -f "$f" ] && [ -x "$f" ]
  head -1 "$f" | grep -qE '^#!/usr/bin/env bash'
  grep -q 'common.sh' "$f"
  grep -qE '\bcompose (pull|up)' "$f"
  ! grep -qE '\b(gen_secret|put_secret|get_secret|bao_)' "$f"
}

@test "step-ca: env template default image matches the compose default" {
  local f="$DEPLOY_DIR/templates/env.j2"
  [ -f "$f" ]
  grep -qE "stepca_image \| default\('docker\.io/smallstep/step-ca:[0-9]+\.[0-9]+\.[0-9]+'\)" "$f"
  grep -qE 'STEPCA_INIT_PASSWORD=\{\{ secrets\.init_password \}\}' "$f"
}

@test "step-ca: local overlay adds caps/SELinux/local-dev network but does NOT republish ports" {
  local f="$DEPLOY_DIR/compose.local.yml"
  [ -f "$f" ]
  grep -q 'mem_limit:' "$f"
  grep -q 'label=disable' "$f"
  grep -q 'local-dev' "$f"
  # The published port is env-param in the base; an overlay ports list would
  # APPEND (not replace), so it must not appear here.
  ! grep -qE '^[[:space:]]*ports:' "$f"
}
