#!/usr/bin/env bats
# Structural tests for the WisBot agent deployment (agents/wisbot/deployment).
# Verifies compose, deploy.sh, and the env template follow agent-cloud conventions.
#
# Run: bats platform/tests/test_service_wisbot.bats

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  DEPLOY_DIR="$REPO_ROOT/agents/wisbot/deployment"
}

@test "wisbot: compose.yml pulls the prebuilt GHCR image (no local build)" {
  local f="$DEPLOY_DIR/compose.yml"
  [ -f "$f" ]
  grep -qE 'image:[[:space:]]*ghcr\.io/uhstray-io/wisbot' "$f"
  ! grep -qE '^[[:space:]]*build:' "$f"
}

@test "wisbot: compose.yml defines a healthcheck and named volumes" {
  local f="$DEPLOY_DIR/compose.yml"
  grep -q 'healthcheck:' "$f"
  grep -q '/health' "$f"
  grep -qE '^[[:space:]]*wisbot_data:' "$f"
}

@test "wisbot: deploy.sh is executable with a bash shebang" {
  local f="$DEPLOY_DIR/deploy.sh"
  [ -f "$f" ]
  [ -x "$f" ]
  head -1 "$f" | grep -qE '^#!/usr/bin/env bash'
}

@test "wisbot: deploy.sh sources common.sh and uses the compose helper" {
  local f="$DEPLOY_DIR/deploy.sh"
  grep -q 'common.sh' "$f"
  grep -qE '\bcompose (pull|up)' "$f"
}

@test "wisbot: deploy.sh is container-only (no secret generation or OpenBao)" {
  local f="$DEPLOY_DIR/deploy.sh"
  ! grep -qE '\b(gen_secret|put_secret|get_secret|bao_)' "$f"
}

@test "wisbot: deploy.sh exits non-zero when config/wisbot.env is missing" {
  # The runtime env file is templated by Ansible and never committed.
  [ ! -f "$DEPLOY_DIR/config/wisbot.env" ]
  # CONTAINER_ENGINE set so detect_runtime returns before any real engine call;
  # deploy.sh must abort at the env-file check (before pulling images).
  run env CONTAINER_ENGINE=docker bash "$DEPLOY_DIR/deploy.sh"
  [ "$status" -ne 0 ]
  [[ "$output" == *"config/wisbot.env"* ]]
}

@test "wisbot: env template references the token via secrets.* (no literal value)" {
  local f="$DEPLOY_DIR/templates/wisbot.env.j2"
  [ -f "$f" ]
  grep -q 'secrets.discord_token' "$f"
  ! grep -qiE 'DISCORD_TOKEN_WISBOT=[A-Za-z0-9._-]{20,}' "$f"
}

@test "wisbot: env template has no hardcoded RFC1918 IPs" {
  local f="$DEPLOY_DIR/templates/wisbot.env.j2"
  ! grep -qE '(192\.168\.[0-9]+\.[0-9]+|10\.[0-9]+\.[0-9]+\.[0-9]+|172\.(1[6-9]|2[0-9]|3[01])\.[0-9]+\.[0-9]+)' "$f"
}

@test "wisbot: .env.example uses a placeholder guild id (no real id)" {
  local f="$DEPLOY_DIR/.env.example"
  [ -f "$f" ]
  ! grep -qE 'WISBOT_GUILD_ID=[1-9][0-9]{15,}' "$f"
}
