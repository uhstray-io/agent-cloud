#!/usr/bin/env bats
# Structural tests for the OPA policy engine (platform/services/opa).
# Verifies the composable shape: env-parameterized compose with a multi-arch
# pinned image, container-only deploy.sh (no secret generation), policy-as-code
# (Rego + data.json), and the recursion-safe data namespace.
#
# Run: bats platform/tests/test_service_opa.bats

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  DEPLOY_DIR="$REPO_ROOT/platform/services/opa/deployment"
  POL_DIR="$DEPLOY_DIR/policies/agentcloud"
}

@test "opa: compose env-parameterizes image + bind/port, mounts policies read-only" {
  local f="$DEPLOY_DIR/compose.yml"
  [ -f "$f" ]
  grep -qE '\$\{OPA_IMAGE' "$f"
  grep -qE '\$\{OPA_BIND' "$f"
  grep -qE '\$\{OPA_PORT' "$f"
  grep -qF './policies:/policies:ro' "$f"
}

@test "opa: prod default image is the multi-arch -static tag (arm64-safe), pinned" {
  # plain tag is amd64-only and crashes under emulation on the arm64 VM.
  grep -qE '\$\{OPA_IMAGE:-openpolicyagent/opa:[0-9]+\.[0-9]+\.[0-9]+-static\}' "$DEPLOY_DIR/compose.yml"
}

@test "opa: deploy.sh is executable, bash, sources common.sh, uses compose, no secrets" {
  local f="$DEPLOY_DIR/deploy.sh"
  [ -f "$f" ] && [ -x "$f" ]
  head -1 "$f" | grep -qE '^#!/usr/bin/env bash'
  grep -q 'common.sh' "$f"
  grep -qE '\bcompose (pull|up)' "$f"
  ! grep -qE '\b(gen_secret|put_secret|get_secret|bao_)' "$f"
}

@test "opa: local overlay adds caps/SELinux/local-dev but does NOT republish ports" {
  local f="$DEPLOY_DIR/compose.local.yml"
  [ -f "$f" ]
  grep -q 'mem_limit:' "$f"
  grep -q 'label=disable' "$f"
  grep -q 'local-dev' "$f"
  ! grep -qE '^[[:space:]]*ports:' "$f"
}

@test "opa: policy-as-code present (rules + static data + tests)" {
  [ -f "$POL_DIR/agent_actions.rego" ]
  [ -f "$POL_DIR/agent_actions_test.rego" ]
  [ -f "$POL_DIR/data.json" ]
  grep -q '^package agentcloud$' "$POL_DIR/agent_actions.rego"
  grep -q 'default allow := false' "$POL_DIR/agent_actions.rego"
}

@test "opa: static data lives under a separate namespace (recursion-safe)" {
  # data.json is mounted at data.agentcloud.catalog (dir-based), referenced as
  # such by the rules — NOT data.agentcloud[<agent>] directly (which recurses
  # into the package rules). The 'catalog' wrapper is the contract.
  grep -q '"catalog"' "$POL_DIR/data.json"
  grep -q 'data.agentcloud.catalog\[input.agent\]' "$POL_DIR/agent_actions.rego"
  ! grep -qE 'data\.agentcloud\[input\.agent\]' "$POL_DIR/agent_actions.rego"
}

@test "opa: data.json is valid JSON with the agent catalogs" {
  python3 -c "import json; d=json.load(open('$POL_DIR/data.json'))['catalog']; assert 'nemoclaw' in d and 'netclaw' in d and 'semaphore' in d"
}
