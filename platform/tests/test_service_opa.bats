#!/usr/bin/env bats
# Structural tests for the OPA policy engine (platform/services/opa).
# Verifies the composable shape: env-parameterized compose with a multi-arch
# pinned image, container-only deploy.sh (no secret generation), policy-as-code
# (Rego + data.json), and the recursion-safe data namespace.
#
# Run: bats platform/tests/test_service_opa.bats

load assert_helpers

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

# ── Listener TLS (opa_tls, production) ──────────────────────────────────────────

# The "- item" lines under a service's `command:` key, without the dash.
command_args() {
  awk '/^    command:/{f=1; next} f && /^    [a-z_]+:/{f=0} f && /^      - /{sub(/^      - /, ""); print}' "$1"
}

# The whole Ansible task (a "    - name:" block) of a playbook whose text matches the pattern.
task_with() {
  awk -v pat="$2" '
    function flush() { if (!done && buf ~ pat) { printf "%s", buf; done = 1 } }
    /^    - name:/ { flush(); buf = "" }
    { buf = buf $0 "\n" }
    END { flush() }
  ' "$1"
}

@test "opa tls overlay: keep-id to uid 1000, ./certs mounted read-only as a directory" {
  local f="$DEPLOY_DIR/compose.tls.yml"
  [ -f "$f" ]
  assert_grep -qxF '    userns_mode: "keep-id:uid=1000,gid=1000"' "$f"
  assert_grep -qxF '      - ./certs:/certs:ro' "$f"
}

@test "opa tls overlay: command repeats every compose.yml argument, adds the three TLS flags before /policies" {
  local base="$DEPLOY_DIR/compose.yml" over="$DEPLOY_DIR/compose.tls.yml" arg
  [ -n "$(command_args "$base")" ]
  while IFS= read -r arg; do
    assert_grep -qxF -- "      - $arg" "$over"
  done < <(command_args "$base")
  [ "$(command_args "$over" | wc -l | tr -d ' ')" -eq "$(( $(command_args "$base" | wc -l | tr -d ' ') + 3 ))" ]
  assert_grep -qxF -- '      - --tls-cert-file=/certs/${OPA_SERVER_LEAF:-opa-server}/current/cert.pem' "$over"
  assert_grep -qxF -- '      - --tls-private-key-file=/certs/${OPA_SERVER_LEAF:-opa-server}/current/key.pem' "$over"
  assert_grep -qxF -- '      - --tls-cert-refresh-period=30s' "$over"
  [ "$(command_args "$over" | tail -n 1)" = "/policies" ]
}

@test "opa: certs/ is gitignored, so the issued leaf and bundle are neither committed nor deleted by the rsync" {
  assert_grep -qxF 'certs/' "$DEPLOY_DIR/.gitignore"
}

@test "opa tls: the server leaf is issued from step-ca after the repo is placed, only under opa_tls" {
  local pb="$REPO_ROOT/platform/playbooks/deploy-opa.yml" place mint
  place=$(grep -n 'tasks/place-monorepo.yml' "$pb" | head -n 1 | cut -d: -f1)
  mint=$(grep -n 'tasks/mint-internal-cert.yml' "$pb" | head -n 1 | cut -d: -f1)
  [ -n "$place" ]
  [ -n "$mint" ]
  [ "$mint" -gt "$place" ]
  task_with "$pb" 'tasks/mint-internal-cert.yml' > "$BATS_TEST_TMPDIR/mint.yml"
  assert_grep -qx '      when: _opa_tls' "$BATS_TEST_TMPDIR/mint.yml"
  assert_grep -qF '_mint_name: "{{ _opa_server_leaf }}"' "$BATS_TEST_TMPDIR/mint.yml"
  assert_grep -qF "_mint_ca_host: \"{{ groups['step_ca_svc'] | first }}\"" "$BATS_TEST_TMPDIR/mint.yml"
  assert_grep -qF '_mint_reissue: false' "$BATS_TEST_TMPDIR/mint.yml"
}

@test "opa tls: the step-ca trust bundle is placed in ./certs, only under opa_tls" {
  local pb="$REPO_ROOT/platform/playbooks/deploy-opa.yml"
  task_with "$pb" 'tasks/distribute-ca-root.yml' > "$BATS_TEST_TMPDIR/bundle.yml"
  assert_grep -qx '      when: _opa_tls' "$BATS_TEST_TMPDIR/bundle.yml"
  assert_grep -qF '_ca_root_dest_dir: "{{ _deploy_dir }}"' "$BATS_TEST_TMPDIR/bundle.yml"
  assert_grep -qF "_ca_host: \"{{ groups['step_ca_svc'] | first }}\"" "$BATS_TEST_TMPDIR/bundle.yml"
}

@test "opa tls: deploy.sh gets compose.tls.yml and the leaf name only under opa_tls" {
  local pb="$REPO_ROOT/platform/playbooks/deploy-opa.yml"
  task_with "$pb" 'Run deploy.sh' > "$BATS_TEST_TMPDIR/deploy.yml"
  assert_grep -qF "COMPOSE_OVERLAYS: \"{{ 'compose.tls.yml' if _opa_tls else '' }}\"" "$BATS_TEST_TMPDIR/deploy.yml"
  assert_grep -qF "OPA_SERVER_LEAF: \"{{ opa_server_leaf | default('opa-server') }}\"" "$BATS_TEST_TMPDIR/deploy.yml"
}

@test "opa tls: the declaration must name the leaf, its profile, dir, reload and the verify URL" {
  local pb="$REPO_ROOT/platform/playbooks/deploy-opa.yml"
  task_with "$pb" 'Refuse opa_tls without its server leaf' > "$BATS_TEST_TMPDIR/decl.yml"
  assert_grep -qF "_opa_leaf_decl[0].profile == 'server'" "$BATS_TEST_TMPDIR/decl.yml"
  assert_grep -qF "_opa_leaf_decl[0].dir == _deploy_dir ~ '/certs/' ~ _opa_server_leaf" "$BATS_TEST_TMPDIR/decl.yml"
  assert_grep -qF "_opa_leaf_decl[0].reload == 'none'" "$BATS_TEST_TMPDIR/decl.yml"
  assert_grep -qF "_opa_tls_name in (_opa_leaf_decl[0].sans" "$BATS_TEST_TMPDIR/decl.yml"
  assert_grep -qF "opa_internal_url | default('', true) | string) == 'https://' ~ _opa_tls_name" "$BATS_TEST_TMPDIR/decl.yml"
  assert_grep -qF "_opa_server_leaf is match(" "$BATS_TEST_TMPDIR/decl.yml"
}

@test "opa tls: the verify probes HTTPS through curl against the step-ca bundle, and refuses a plain-HTTP 200" {
  local pb="$REPO_ROOT/platform/playbooks/deploy-opa.yml"
  sed -n '/^    _opa_tls_args:/,/^  tasks:/p' "$pb" > "$BATS_TEST_TMPDIR/args.yml"
  assert_grep -qx -- '      - --cacert' "$BATS_TEST_TMPDIR/args.yml"
  assert_grep -qF -- '"{{ _deploy_dir }}/certs/step-ca-bundle.crt"' "$BATS_TEST_TMPDIR/args.yml"
  assert_grep -qx -- '      - --resolve' "$BATS_TEST_TMPDIR/args.yml"
  task_with "$pb" 'OPA health over TLS' > "$BATS_TEST_TMPDIR/health.yml"
  assert_grep -qF '_opa_tls_args' "$BATS_TEST_TMPDIR/health.yml"
  assert_grep -qx '      when: _opa_tls' "$BATS_TEST_TMPDIR/health.yml"
  task_with "$pb" 'Plain HTTP to the TLS listener' > "$BATS_TEST_TMPDIR/plain.yml"
  assert_grep -qF 'failed_when: false' "$BATS_TEST_TMPDIR/plain.yml"
  task_with "$pb" 'Refuse a plain-HTTP 200' > "$BATS_TEST_TMPDIR/refuse.yml"
  assert_grep -qF "trim != '200'" "$BATS_TEST_TMPDIR/refuse.yml"
}

@test "opa tls: the TLS decisions are gated on opa_tls and the plain ones on its absence" {
  local pb="$REPO_ROOT/platform/playbooks/deploy-opa.yml"
  task_with "$pb" 'ansible.builtin.uri:' > "$BATS_TEST_TMPDIR/uri-health.yml"
  assert_grep -qx '      when: not _opa_tls' "$BATS_TEST_TMPDIR/uri-health.yml"
  task_with "$pb" 'Sample decision: nemoclaw read nocodb -> allow' > "$BATS_TEST_TMPDIR/plain-allow.yml"
  assert_grep -qx '      when: not ansible_check_mode and not _opa_tls' "$BATS_TEST_TMPDIR/plain-allow.yml"
  task_with "$pb" 'Sample decision: unknown agent -> deny' > "$BATS_TEST_TMPDIR/plain-deny.yml"
  assert_grep -qx '      when: not ansible_check_mode and not _opa_tls' "$BATS_TEST_TMPDIR/plain-deny.yml"
  task_with "$pb" 'Sample decision over TLS: nemoclaw' > "$BATS_TEST_TMPDIR/tls-allow.yml"
  assert_grep -qx '      when: _opa_tls and not ansible_check_mode' "$BATS_TEST_TMPDIR/tls-allow.yml"
  task_with "$pb" 'Sample decision over TLS: unknown agent' > "$BATS_TEST_TMPDIR/tls-deny.yml"
  assert_grep -qx '      when: _opa_tls and not ansible_check_mode' "$BATS_TEST_TMPDIR/tls-deny.yml"
}
