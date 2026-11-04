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
  grep -qE '\$\{OPA_IMAGE:-docker\.io/openpolicyagent/opa:[0-9]+\.[0-9]+\.[0-9]+-static\}' "$DEPLOY_DIR/compose.yml"
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

@test "opa: a production deploy requires listener TLS and an https verify URL" {
  local pb="$REPO_ROOT/platform/playbooks/deploy-opa.yml"
  task_with "$pb" 'Refuse a production deploy without its declared bind' > "$BATS_TEST_TMPDIR/prod.yml"
  assert_grep -qF -e "- opa_tls | default(false) | bool" "$BATS_TEST_TMPDIR/prod.yml"
  assert_grep -qF "is match('^https://')" "$BATS_TEST_TMPDIR/prod.yml"
  refute_grep -qF "https?://" "$BATS_TEST_TMPDIR/prod.yml"
  assert_grep -qF "when: not (local_mode | default(false) | bool)" "$BATS_TEST_TMPDIR/prod.yml"
}

# ── Read-only policy (bundle mode) ──────────────────────────────────────────────

@test "opa: the verify runs the Rego tests as a bundle, the way the server loads them" {
  # Without --bundle, PUT /v1/data and /v1/policies (and DELETE /v1/data/agentcloud) succeed for
  # any caller the firewall admits; the :ro mount protects the files, not the running policy.
  # Both command lists ending in "--bundle /policies" is asserted by the --authorization test
  # below, whose anchored suffix includes it. The Rego tests load the tree the way the server does.
  assert_grep -qF '/opa test --bundle /policies"' "$REPO_ROOT/platform/playbooks/deploy-opa.yml"
}

@test "opa: every data file in the policy tree is one bundle mode loads (data.json / data.yaml)" {
  # A plain load reads any .json/.yaml as data; --bundle reads only data.json/data.yaml. CI's
  # opa test still loads the tree plainly, so a stray data file would pass CI and be dropped at
  # runtime. Name it data.json in its own directory instead.
  local stray
  stray="$(find "$DEPLOY_DIR/policies" -type f \( -name '*.json' -o -name '*.yaml' -o -name '*.yml' \) \
    -not -name data.json -not -name data.yaml)"
  [ -z "$stray" ]
}

@test "opa: the verify probes one list of three refusals (policy write, data read, decision with explain), each with its method, path and body" {
  # The list is a play var, so it is read by range rather than by task. The write probe's path sits
  # outside every package a decision reads; only the explain probe reads a decision.
  local pb="$REPO_ROOT/platform/playbooks/deploy-opa.yml" probes
  probes="$BATS_TEST_TMPDIR/probes.yml"
  sed -n '/^    _opa_refusal_probes:/,/^    _opa_tls_probe_args:/p' "$pb" > "$probes"
  [ "$(grep -c '^      - what: ' "$probes")" -eq 3 ]
  assert_grep -qxF '      - what: policy write' "$probes"
  assert_grep -qxF '        method: PUT' "$probes"
  assert_grep -qxF '        path: /v1/data/agentcloud_write_probe' "$probes"
  assert_grep -qxF '        body: {}' "$probes"
  assert_grep -qxF '      - what: data read' "$probes"
  assert_grep -qxF '        method: GET' "$probes"
  assert_grep -qxF '        path: /v1/data' "$probes"
  assert_grep -qxF '      - what: decision with explain' "$probes"
  assert_grep -qxF '        method: POST' "$probes"
  assert_grep -qxF '        path: /v1/data/agentcloud/decision?explain=full' "$probes"
  assert_grep -qxF '          input: {agent: nemoclaw, action: read, service: nocodb}' "$probes"
  # The write probe never writes under a package a decision reads, nor under the policy API.
  sed -n '/what: policy write/,/what: data read/p' "$probes" > "$BATS_TEST_TMPDIR/write-entry.yml"
  refute_grep -qE '/v1/data/agentcloud/|/v1/policies' "$BATS_TEST_TMPDIR/write-entry.yml"
  refute_grep -qE '/v1/policies' "$probes"
}

@test "opa: both refusal tasks loop over the one probe list, and the six per-probe tasks are gone" {
  local pb="$REPO_ROOT/platform/playbooks/deploy-opa.yml"
  task_with "$pb" 'Authorization refusals: policy write' > "$BATS_TEST_TMPDIR/plain.yml"
  assert_grep -qxF '      loop: "{{ _opa_refusal_probes }}"' "$BATS_TEST_TMPDIR/plain.yml"
  task_with "$pb" 'Authorization refusals over TLS' > "$BATS_TEST_TMPDIR/tls.yml"
  assert_grep -qxF '      loop: "{{ _opa_refusal_probes }}"' "$BATS_TEST_TMPDIR/tls.yml"
  [ "$(grep -c '^    - name: "Authorization refusals' "$pb")" -eq 2 ]
  refute_grep -qE '^    - name: "(Policy writes|Data reads|Decision with explain)' "$pb"
}

@test "opa: the plain refusal task fails on any status but 401 or any code but unauthorized, and sends no body on a GET" {
  local pb="$REPO_ROOT/platform/playbooks/deploy-opa.yml" plain
  plain="$BATS_TEST_TMPDIR/plain.yml"
  task_with "$pb" 'Authorization refusals: policy write' > "$plain"
  assert_grep -qxF '        url: "{{ _opa_url }}{{ item.path }}"' "$plain"
  assert_grep -qxF '        method: "{{ item.method }}"' "$plain"
  assert_grep -qxF '        status_code: [401]' "$plain"
  assert_grep -qF "failed_when: _opa_refusal.status != 401 or (_opa_refusal.json.code | default('')) != 'unauthorized'" "$plain"
  # uri would JSON-encode an absent body as null, so json is set only where a probe has a body.
  assert_grep -qxF "        body_format: \"{{ 'json' if item.body is defined else omit }}\"" "$plain"
  assert_grep -qxF '        body: "{{ item.body | default(omit) }}"' "$plain"
  assert_grep -qxF '      when: not ansible_check_mode and not _opa_tls' "$plain"
  refute_grep -qF 'status_code: [400]' "$plain"
}

@test "opa tls: the TLS refusal task drops --fail through the probe args and passes only on a last-line 401 with code unauthorized" {
  local pb="$REPO_ROOT/platform/playbooks/deploy-opa.yml" tls
  tls="$BATS_TEST_TMPDIR/tls.yml"
  task_with "$pb" 'Authorization refusals over TLS' > "$tls"
  assert_grep -qF '{{ _opa_tls_probe_args' "$tls"
  # The probe args are the TLS args without --fail, so the expected 401 exits 0.
  assert_grep -qxF "    _opa_tls_probe_args: \"{{ _opa_tls_args | reject('equalto', '--fail') | list }}\"" "$pb"
  refute_grep -qF '_opa_tls_args' "$tls"
  refute_grep -qF -- '--fail' "$tls"
  assert_grep -qF "'--request', item.method" "$tls"
  assert_grep -qF "'--write-out', '\n%{http_code}'" "$tls"
  assert_grep -qF "_opa_refusal_tls.rc != 0" "$tls"
  assert_grep -qF "(_opa_refusal_tls.stdout_lines | last) != '401'" "$tls"
  assert_grep -qF "(_opa_refusal_tls.stdout_lines[:-1] | join('\n') | from_json).code | default('')) != 'unauthorized'" "$tls"
  assert_grep -qxF '      when: _opa_tls and not ansible_check_mode' "$tls"
}

@test "opa authz: a decision with query parameters is refused, explain and instrument alike (the rule and its unit tests)" {
  # ?explain=full returns the evaluation trace of the policy and its data, so the authorization
  # policy admits a decision only with no query parameters. Each refusal has a unit test.
  local authz="$DEPLOY_DIR/policies/system/authz.rego" tests="$DEPLOY_DIR/policies/system/authz_test.rego"
  assert_grep -qF 'count(object.get(input, "params", {})) == 0' "$authz"
  assert_grep -qE '^test_deny_decision_explain if' "$tests"
  assert_grep -qE '^test_deny_decision_instrument if' "$tests"
}

@test "opa authz: the policy admits exactly the decision POST and the health GET, and its tests exist" {
  # Deny-by-default: the only two allow rules are the decision API and /health. Adding a third
  # changes what an admitted caller may reach, so it must be an explicit, reviewed change here.
  local authz="$DEPLOY_DIR/policies/system/authz.rego" tests="$DEPLOY_DIR/policies/system/authz_test.rego"
  [ -f "$authz" ]
  [ -f "$tests" ]
  assert_grep -qx 'package system.authz' "$authz"
  assert_grep -qx 'default allow := false' "$authz"
  [ "$(grep -c '^allow if' "$authz")" -eq 2 ]
  assert_grep -qF 'input.method == "POST"' "$authz"
  assert_grep -qF 'input.path == ["v1", "data", "agentcloud", "decision"]' "$authz"
  assert_grep -qF 'input.method == "GET"' "$authz"
  assert_grep -qF 'input.path == ["health"]' "$authz"
  # The unit tests cover the admitted requests and the refused ones.
  assert_grep -qE '^test_allow_decision_post if' "$tests"
  assert_grep -qE '^test_allow_health_get if' "$tests"
  assert_grep -qE '^test_deny_policy_write_probe_put if' "$tests"
  assert_grep -qE '^test_deny_data_root_get if' "$tests"
  assert_grep -qE '^test_deny_metrics_get if' "$tests"
}

@test "opa: both command lists pass --authorization=basic, directly before --bundle" {
  # Without the flag the policy is never asked, and every request reaches the handlers.
  local f cmd args
  for f in "$DEPLOY_DIR/compose.yml" "$DEPLOY_DIR/compose.tls.yml"; do
    cmd="$(command_args "$f")"
    [ "$(grep -cxF -- '--authorization=basic' <<<"$cmd")" -eq 1 ]
    # The command ends with the flag, then the bundle it guards (anchored on the whole list).
    args="$(tr '\n' ' ' <<<"$cmd")"
    # `[ ]`, not `[[ ]]`: a false `[[ ]]` mid-body does not fail a Bats test (test_assertions_are_real.bats).
    [ "${args%" --authorization=basic --bundle /policies "}" != "$args" ]
  done
}
