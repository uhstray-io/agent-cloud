#!/usr/bin/env bats
# Structural tests for the Caddy reverse proxy (platform/services/caddy/deployment).
# Verifies the composable conversion: env-parameterized compose, container-only
# deploy.sh, local Caddyfile template, and an overlay-safe local profile.
#
# Run: bats platform/tests/test_service_caddy.bats

load assert_helpers

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  DEPLOY_DIR="$REPO_ROOT/platform/services/caddy/deployment"
}

@test "caddy: compose env-parameterizes image, ports, and the Caddyfile source" {
  local f="$DEPLOY_DIR/compose.yml"
  [ -f "$f" ]
  grep -qE '\$\{CADDY_IMAGE' "$f"
  grep -qE '\$\{CADDY_HTTP_PORT' "$f"
  grep -qE '\$\{CADDY_HTTPS_PORT' "$f"
  grep -qE '\$\{CADDYFILE' "$f"
}

@test "caddy: prod defaults are byte-identical (cloudflare image, 80/443, committed Caddyfile)" {
  local f="$DEPLOY_DIR/compose.yml"
  grep -qE '\$\{CADDY_IMAGE:-iarekylew00t/caddy-cloudflare:2\.11\.4-alpine\}' "$f"
  grep -qE '\$\{CADDY_HTTP_PORT:-80\}:80' "$f"
  grep -qE '\$\{CADDY_HTTPS_PORT:-443\}:443' "$f"
  grep -qE '\$\{CADDYFILE:-\./Caddyfile\}' "$f"
}

@test "caddy: healthcheck uses the admin API on 127.0.0.1 (not localhost — IPv4-only bind)" {
  local f="$DEPLOY_DIR/compose.yml"
  grep -q 'healthcheck:' "$f"
  grep -q '127.0.0.1:2019/config/' "$f"
  ! grep -q 'localhost:2019' "$f"
}

@test "caddy: deploy.sh is executable, bash, sources common.sh, uses compose, no secrets" {
  local f="$DEPLOY_DIR/deploy.sh"
  [ -f "$f" ] && [ -x "$f" ]
  head -1 "$f" | grep -qE '^#!/usr/bin/env bash'
  grep -q 'common.sh' "$f"
  grep -qE '\bcompose (pull|up)' "$f"
  ! grep -qE '\b(gen_secret|put_secret|get_secret|bao_)' "$f"
}

@test "caddy: local Caddyfile template serves the step-ca cert (with local_certs fallback), no real domain" {
  local f="$DEPLOY_DIR/templates/Caddyfile.local.j2"
  [ -f "$f" ]
  # step-ca-minted wildcard when caddy_tls_cert is set; Caddy's own internal CA
  # (local_certs) only as the fallback when it isn't.
  grep -q 'caddy_tls_cert' "$f"
  grep -qF 'tls {{ _cert }} {{ caddy_tls_key }}' "$f"
  grep -q 'local_certs' "$f"
  grep -q 'reverse_proxy' "$f"
  grep -q 'caddy_routes' "$f"
  ! grep -qE 'uhstray\.io' "$f"
}

@test "caddy: forward_auth route is opt-in per-route and strips client identity headers" {
  local f="$DEPLOY_DIR/templates/Caddyfile.local.j2"
  # Only routes with a forward_auth upstream get the gated route block.
  grep -qF "r.forward_auth" "$f"
  # The embedded-outpost endpoints must be proxied to Authentik, not the app.
  grep -qF '/outpost.goauthentik.io/*' "$f"
  grep -qF 'uri /outpost.goauthentik.io/auth/caddy' "$f"
  # Identity headers are copied from the outpost AND stripped from the client
  # request first (anti-spoofing) — both must be present.
  grep -qE 'copy_headers .*X-authentik-username' "$f"
  grep -qE 'request_header -X-authentik-username' "$f"
}

@test "caddy: inference_api route allowlists /v1 (Bearer required) + /health, 404s the rest, streams" {
  local f="$DEPLOY_DIR/templates/Caddyfile.local.j2"
  # Scope every assertion to the allowlist edge, not the whole file: the forward_auth
  # branch also carries matchers and a reverse_proxy. The edge is the shared api_edge
  # macro (inference_api and mcp_api render through it) plus the inference_api call site.
  sed -n '/macro api_edge/,/endmacro/p;/r.inference_api/,/{% elif r.mcp_api/p' "$f" > "$BATS_TEST_TMPDIR/inference.j2"
  local b="$BATS_TEST_TMPDIR/inference.j2"
  [ -s "$b" ]
  # The inference route is the macro called with /v1/* and the /health handle on.
  assert_grep -qF "api_edge(r, '/v1/*', true)" "$b"
  # The API allowlist is a named matcher on /v1/* consumed by a handle block —
  # the 401 must sit INSIDE it (Caddy orders `handle` before a top-level `respond`).
  # [[:space:]] rather than \s, and $'\t' rather than '\t': neither escape is
  # portable ERE, and BSD grep on macOS reads '\t' as a literal t — which would
  # turn the refute below into one that can never match.
  assert_grep -qE '^[[:space:]]*@api path \{\{ path \}\}$' "$b"
  assert_grep -qE '^[[:space:]]*handle @api \{' "$b"
  # A regexp, not `header Authorization Bearer*`: the trailing * is a prefix
  # match, so `BearerX` would pass. The scheme, one space, a non-empty credential.
  assert_grep -qF '@noauth not header_regexp Authorization "^Bearer [^[:space:]]+$"' "$b"
  refute_grep -qE 'header Authorization Bearer\*' "$b"
  assert_grep -qE '^[[:space:]]*respond @noauth 401$' "$b"
  # ...and NOT at site level (exactly one leading tab in this template).
  refute_grep -qE $'^\trespond @noauth' "$b"
  # Liveness passes through; everything else is a 404 from the bare handle.
  assert_grep -qE '^[[:space:]]*handle /health \{' "$b"
  assert_grep -qE '^[[:space:]]*handle \{$' "$b"
  assert_grep -qE '^[[:space:]]*respond 404$' "$b"
  # Token streaming and the prompt-size cap.
  assert_grep -qE '^[[:space:]]*flush_interval -1$' "$b"
  assert_grep -qE '^[[:space:]]*max_size 16MB$' "$b"
  # The upstream comes from the route, never a literal (directly, or through the
  # upstream() macro that adds mutual TLS for an `upstream_tls` route).
  assert_grep -qE "reverse_proxy (\{\{ 'https://' if r\.upstream_tls is defined else '' \}\})?\{\{ r\.upstream \}\}" "$b"
  assert_grep -qF 'reverse_proxy {{ upstream(r,' "$b"
  refute_grep -qE 'reverse_proxy [0-9]' "$b"
}

_render_caddy() {  # renders $DEPLOY_DIR/templates/Caddyfile.local.j2 with the given routes YAML
  command -v ansible-playbook >/dev/null || skip "ansible-playbook not installed"
  cat >"$BATS_TEST_TMPDIR/render.yml" <<YML
- hosts: localhost
  gather_facts: false
  vars:
    caddy_tls_cert: /etc/caddy/certs/wildcard.crt
    caddy_tls_key: /etc/caddy/certs/wildcard.key
    caddy_routes: $1
  tasks:
    - ansible.builtin.template: {src: "$DEPLOY_DIR/templates/Caddyfile.local.j2", dest: "$BATS_TEST_TMPDIR/Caddyfile", mode: "0644"}
YML
  # The output is kept in render.log: a test that expects a refusal checks ITS message there, because a
  # nonzero status alone also comes from a broken render and would pass for the wrong reason.
  ansible-playbook -i localhost, -c local "$BATS_TEST_TMPDIR/render.yml" </dev/null >"$BATS_TEST_TMPDIR/render.log" 2>&1 \
    || { cat "$BATS_TEST_TMPDIR/render.log" >&2; return 1; }
}

@test "caddy: an upstream_tls route proxies over mutual TLS with the mounted client leaf (task 6.2)" {
  _render_caddy '[{host: a.test, upstream: "gw:4001", upstream_tls: {server_name: gateway.dc1.example.internal, client_leaf: caddy}}, {host: i.test, upstream: "gw:4000", inference_api: true, upstream_tls: {server_name: gateway.dc1.example.internal, client_leaf: caddy}}]'
  local c="$BATS_TEST_TMPDIR/Caddyfile"
  # Every upstream to the gateway is https, never a plain dial (the /v1 handle, /health, the UI).
  [ "$(grep -c 'reverse_proxy https://gw:400' "$c")" -eq 3 ]
  refute_grep -qE 'reverse_proxy gw:' "$c"
  [ "$(grep -c 'tls_server_name gateway.dc1.example.internal$' "$c")" -eq 3 ]
  [ "$(grep -c 'tls_trust_pool file /etc/caddy/certs/step-ca-bundle.crt$' "$c")" -eq 3 ]
  [ "$(grep -c 'tls_client_auth /etc/caddy/certs/caddy/current/cert.pem /etc/caddy/certs/caddy/current/key.pem$' "$c")" -eq 3 ]
  # The /v1 route keeps its streaming transport settings beside the TLS ones.
  assert_grep -qE '^[[:space:]]*dial_timeout 5s$' "$c"
}

@test "caddy: a route without upstream_tls renders exactly as before (plain proxy)" {
  _render_caddy '[{host: p.test, upstream: "x:1"}]'
  local c="$BATS_TEST_TMPDIR/Caddyfile"
  # The upstream() macro adds no output of its own: the file still opens on the global block
  # (review of 53b00ad8). head -n 1, not head -c 2: $(...) drops the trailing newline.
  [ "$(head -n 1 "$c")" = "{" ]
  assert_grep -qE $'^\treverse_proxy x:1$' "$c"
  refute_grep -qE 'tls_client_auth|tls_trust_pool|https://x' "$c"
}

# The two inference sites of a render, nothing else (no global block, no trailing import).
_inference_sites() { awk '/^(i|g)\.test \{/,/^}/' "$1"; }

@test "caddy: the inference route renders byte-identical to the pre-macro template (golden)" {
  _render_caddy '[{host: i.test, upstream: "inf:8001", inference_api: true}, {host: g.test, upstream: "gw:4000", inference_api: true, upstream_tls: {server_name: gateway.dc1.example.internal, client_leaf: caddy}}]'
  _inference_sites "$BATS_TEST_TMPDIR/Caddyfile" > "$BATS_TEST_TMPDIR/inference.sites"
  # The golden was rendered by the template as it stood BEFORE the api_edge macro was factored
  # out (mcp edge, task 5.1). Any drift in the inference allowlist is a diff here.
  [ -s "$BATS_TEST_TMPDIR/inference.sites" ]
  diff "$BATS_TEST_TMPDIR/inference.sites" "$REPO_ROOT/platform/tests/fixtures/caddy-inference-sites.golden"
}

# One mcp_api site, without and with mutual TLS, extracted from the render.
_render_mcp() {
  _render_caddy '[{host: m.test, upstream: "gw:4000", mcp_api: true}, {host: t.test, upstream: "gw:4000", mcp_api: true, upstream_tls: {server_name: gateway.dc1.example.internal, client_leaf: caddy}}]'
  awk '/^m\.test \{/,/^}/' "$BATS_TEST_TMPDIR/Caddyfile" > "$BATS_TEST_TMPDIR/mcp.site"
  awk '/^t\.test \{/,/^}/' "$BATS_TEST_TMPDIR/Caddyfile" > "$BATS_TEST_TMPDIR/mcp-tls.site"
  [ -s "$BATS_TEST_TMPDIR/mcp.site" ]
  [ -s "$BATS_TEST_TMPDIR/mcp-tls.site" ]
}

@test "caddy: an mcp_api route proxies ONLY the exact path /mcp; /v1, /sse, /metrics, /health fall to the 404" {
  _render_mcp
  local m="$BATS_TEST_TMPDIR/mcp.site"
  # The one matcher is the exact path: no wildcard, so /mcp/ and /mcp/x are not matched.
  assert_grep -qE '^[[:space:]]*@api path /mcp$' "$m"
  [ "$(grep -c '@api path' "$m")" -eq 1 ]
  # Exactly two handle blocks: the gated /mcp one and the bare fallthrough. Any further handle
  # (a /health liveness, a /v1 allowlist) would open another path.
  [ "$(grep -cE '^[[:space:]]*handle ' "$m")" -eq 2 ]
  assert_grep -qE '^[[:space:]]*handle @api \{$' "$m"
  assert_grep -qE '^[[:space:]]*handle \{$' "$m"
  refute_grep -qE 'handle /' "$m"
  # No path other than /mcp appears in a directive (comments, which name /sse, are dropped).
  sed '/^[[:space:]]*#/d' "$m" > "$BATS_TEST_TMPDIR/mcp.nocomment"
  refute_grep -qE '/v1|/sse|/metrics|/health' "$BATS_TEST_TMPDIR/mcp.nocomment"
  # The bare handle is the fallthrough and answers 404: its whole block, selected by construct.
  sed -n '/^[[:space:]]*handle {$/,/^[[:space:]]*}$/p' "$m" > "$BATS_TEST_TMPDIR/mcp.fallthrough"
  [ "$(tr -d '\t' < "$BATS_TEST_TMPDIR/mcp.fallthrough" | paste -sd'|' -)" = "handle {|respond 404|}" ]
}

@test "caddy: an mcp_api route answers 401 for a missing or malformed Bearer INSIDE the /mcp handle" {
  _render_mcp
  local m="$BATS_TEST_TMPDIR/mcp.site"
  # The scheme, one space, a non-empty credential: Bearer alone, BearerX, Basic and an empty
  # header all fail this regexp and hit the 401. Not the prefix form `Bearer*`.
  assert_grep -qF '@noauth not header_regexp Authorization "^Bearer [^[:space:]]+$"' "$m"
  refute_grep -qE 'header Authorization Bearer\*' "$m"
  assert_grep -qE '^[[:space:]]*respond @noauth 401$' "$m"
  # Inside the handle block (two tabs), not at site level (one tab): Caddy orders `handle`
  # before a top-level `respond`, which would never run for /mcp.
  assert_grep -qE $'^\t\trespond @noauth 401$' "$m"
  refute_grep -qE $'^\trespond @noauth' "$m"
  # The 401 precedes the proxy it guards.
  [ "$(grep -n 'respond @noauth 401' "$m" | head -n 1 | cut -d: -f1)" -lt "$(grep -n 'reverse_proxy' "$m" | head -n 1 | cut -d: -f1)" ]
}

@test "caddy: an mcp_api route streams and takes its upstream (and mutual TLS) from the route" {
  _render_mcp
  local m="$BATS_TEST_TMPDIR/mcp.site" t="$BATS_TEST_TMPDIR/mcp-tls.site"
  assert_grep -qE '^[[:space:]]*flush_interval -1$' "$m"
  assert_grep -qE $'^\t\treverse_proxy gw:4000 \\{$' "$m"
  refute_grep -qE 'tls_client_auth|tls_trust_pool|https://' "$m"
  # upstream_tls: https, the verified server name, the mounted client leaf.
  assert_grep -qE $'^\t\treverse_proxy https://gw:4000 \\{$' "$t"
  assert_grep -qE '^[[:space:]]*tls_server_name gateway\.dc1\.example\.internal$' "$t"
  assert_grep -qE '^[[:space:]]*tls_client_auth /etc/caddy/certs/caddy/current/cert\.pem /etc/caddy/certs/caddy/current/key\.pem$' "$t"
  assert_grep -qE '^[[:space:]]*flush_interval -1$' "$t"
}

@test "caddy: a route that sets both edge flags, or mcp_api with forward_auth, is refused at render time" {
  # Without the refusal the if/elif chain lets the first flag win silently, and mcp_api would
  # drop the forward_auth gate the route asked for.
  run _render_caddy '[{host: b.test, upstream: "x:1", inference_api: true, mcp_api: true}]'
  [ "$status" -ne 0 ]
  assert_grep -qF 'b.test sets both inference_api and mcp_api' "$BATS_TEST_TMPDIR/render.log"
  run _render_caddy '[{host: f.test, upstream: "x:1", mcp_api: true, forward_auth: "a:1"}]'
  [ "$status" -ne 0 ]
  assert_grep -qF 'f.test sets mcp_api with forward_auth' "$BATS_TEST_TMPDIR/render.log"
  # A route that sets one edge flag still renders.
  _render_caddy '[{host: ok.test, upstream: "x:1", mcp_api: true}, {host: ok2.test, upstream: "x:1", inference_api: true}]'
  assert_grep -qF 'ok.test {' "$BATS_TEST_TMPDIR/Caddyfile"
}

@test "caddy: env template prod defaults match the compose defaults" {
  local f="$DEPLOY_DIR/templates/env.j2"
  [ -f "$f" ]
  grep -qE "caddy_image \| default\('iarekylew00t/caddy-cloudflare:2\.11\.4-alpine'\)" "$f"
  grep -qE "caddy_file \| default\('\./Caddyfile'\)" "$f"
}

@test "caddy: local overlay adds caps/SELinux/network but does NOT republish ports" {
  local f="$DEPLOY_DIR/compose.local.yml"
  [ -f "$f" ]
  grep -q 'mem_limit:' "$f"
  grep -q 'label=disable' "$f"
  grep -q 'local-dev' "$f"
  # Ports/image/Caddyfile are env-param in the base — an overlay ports list
  # would APPEND (not replace), so it must not appear here.
  ! grep -qE '^[[:space:]]*ports:' "$f"
}

@test "caddy: reviewed revision gates the controller and production receiver" {
  local pb="$BATS_TEST_DIRNAME/../playbooks/deploy-caddy.yml"
  assert_grep -qF 'argv: [git, status, --porcelain, --untracked-files=all]' "$pb"
  assert_precedes "$pb" 'Refuse a receiver checkout that differs from the reviewed candidate' 'Render compose .env'
  python3 - "$pb" <<'PY'
import sys, yaml
plays = yaml.safe_load(open(sys.argv[1]))
tasks = {task['name']: task for play in plays for task in play.get('tasks') or []}
for name in ('Read the placed revision when a candidate SHA is required',
             'Refuse a receiver checkout that differs from the reviewed candidate'):
    assert 'expected_repository_sha is defined' in tasks[name]['when']
    assert 'not (local_mode | default(false) | bool)' in tasks[name]['when']
PY
}

@test "caddy: the internal cert gains a *.<parent> SAN for every nested route host" {
  # A TLS wildcard matches ONE label: *.zone does not cover admin.inference.zone.
  # EVALUATES the expression deploy-caddy.yml actually carries (extracted from the
  # file, not re-typed here) against sample routes, so a broken regex fails.
  command -v ansible-playbook >/dev/null 2>&1 || skip "ansible-playbook not available"
  local pb="$BATS_TEST_DIRNAME/../playbooks/deploy-caddy.yml"
  local mint="$BATS_TEST_DIRNAME/../playbooks/tasks/mint-internal-cert.yml"
  python3 - "$pb" "$BATS_TEST_TMPDIR/sans.yml" <<'PY2'
import sys, yaml
plays = yaml.safe_load(open(sys.argv[1]))
expr = None
for play in plays:
    for t in play.get('tasks', []) or []:
        v = (t.get('vars') or {}).get('_mint_extra_sans')
        if v: expr = v
assert expr, "no _mint_extra_sans in deploy-caddy.yml"
out = [{'hosts': 'localhost', 'connection': 'local', 'gather_facts': False,
        'vars': {'_mint_zone': 'z.test', 'caddy_routes': [
            {'host': 'semaphore.z.test'}, {'host': 'admin.inference.z.test'},
            {'host': 'x.inference.z.test'}, {'host': 'a.b.c.z.test'}, {'host': 'z.test'}],
            '_sans': expr},
        'tasks': [{'ansible.builtin.copy': {'content': '{{ _sans | to_json }}', 'dest': sys.argv[2] + '.out'}}]}]
yaml.safe_dump(out, open(sys.argv[2], 'w'))
PY2
  ansible-playbook "$BATS_TEST_TMPDIR/sans.yml" >/dev/null 2>&1
  [ "$(cat "$BATS_TEST_TMPDIR/sans.yml.out")" = '["*.inference.z.test", "*.b.c.z.test"]' ]
  # The mint task emits each SAN and refuses non-hostname characters first. Evaluate
  # that refusal too: the derived wildcard passes, a shell metacharacter does not.
  assert_grep -qF -- '--san "{{ san }}"' "$mint"
  python3 - "$mint" "$BATS_TEST_TMPDIR/sancheck.yml" <<'PY2'
import sys, yaml
top = yaml.safe_load(open(sys.argv[1]))
# The wildcard tasks sit in a block (a declared leaf goes to tasks/issue-internal-leaf.yml).
tasks = top + [c for x in top for c in (x.get('block') or [])]
t = [x for x in tasks if x.get('name') == 'Refuse an extra SAN outside hostname characters'][0]
that = t['ansible.builtin.assert']['that']
play = [{'hosts': 'localhost', 'connection': 'local', 'gather_facts': False,
         'tasks': [{'ansible.builtin.assert': {'that': that}}]}]
yaml.safe_dump(play, open(sys.argv[2], 'w'))
PY2
  ansible-playbook "$BATS_TEST_TMPDIR/sancheck.yml" -e '{"_mint_extra_sans":["*.inference.z.test","plain.z.test"]}' >/dev/null 2>&1
  run ansible-playbook "$BATS_TEST_TMPDIR/sancheck.yml" -e '{"_mint_extra_sans":["x$(id).z.test"]}'
  [ "$status" -ne 0 ]
}

# Phase 3 of deploy-caddy.yml, EXTRACTED from the file and run against a fake container
# engine, so the tasks under test are the ones the playbook carries. The fake answers the
# admin-API read with a running config that holds only `loaded.z.test`, and answers the
# :2021 metrics probe with $FAKE_METRICS_RC.
_run_caddy_phase3() {
  command -v ansible-playbook >/dev/null 2>&1 || skip "ansible-playbook not available"
  local pb="$BATS_TEST_DIRNAME/../playbooks/deploy-caddy.yml"
  local fake="$BATS_TEST_TMPDIR/fake-engine"
  cat >"$fake" <<'SH'
#!/bin/sh
case "$*" in
  *2021/metrics*) exit "${FAKE_METRICS_RC:-0}" ;;
  *2019/config/*)
    if [ -n "${FAKE_CONFIG:-}" ]; then printf '%s\n' "$FAKE_CONFIG"
    else echo '{"apps":{"http":{"servers":{"srv0":{"routes":[{"match":[{"host":["loaded.z.test"]}]}]}}}}}'; fi ;;
esac
SH
  chmod +x "$fake"
  python3 - "$pb" "$BATS_TEST_TMPDIR/phase3.yml" <<'PY'
import sys, yaml
plays = yaml.safe_load(open(sys.argv[1]))
play = next(p for p in plays if p.get('name') == 'Phase 3: Verify Caddy config')
play['hosts'] = 'localhost'
play['connection'] = 'local'
yaml.safe_dump([play], open(sys.argv[2], 'w'))
PY
  local routes='{"caddy_routes":[{"host":"loaded.z.test","upstream":"a:1"},{"host":"new.z.test","upstream":"b:2"}]}'
  run ansible-playbook "$BATS_TEST_TMPDIR/phase3.yml" -e "container_engine=$fake" -e "$routes" "$@"
}

@test "caddy: dry run with a declared route not yet loaded passes and names it" {
  _run_caddy_phase3 --check
  [ "$status" -eq 0 ]
  assert_contains "$output" "would be added on a real run: new.z.test"
  assert_contains "$output" "already loaded: loaded.z.test"
}

@test "caddy: real run with a declared route not yet loaded fails with the route message" {
  _run_caddy_phase3
  [ "$status" -ne 0 ]
  assert_contains "$output" "route new.z.test not found in Caddy's running config"
  refute_contains "$output" "would be added on a real run"
}

@test "caddy: dry run does not fail on a local metrics listener that is not loaded yet" {
  FAKE_METRICS_RC=1 _run_caddy_phase3 --check -e local_mode=true
  [ "$status" -eq 0 ]
}

@test "caddy: real run still fails on a missing local metrics listener" {
  FAKE_METRICS_RC=1 _run_caddy_phase3 -e local_mode=true -e '{"caddy_routes":[]}'
  [ "$status" -ne 0 ]
  assert_contains "$output" "Require the private local metrics listener"
}

# A hostname is a loaded route only when a HOST MATCHER carries it; the old substring test accepted a host that
# merely appears in the config (an upstream dial, here) and so passed a route Caddy never loaded.
_UPSTREAM_ONLY='{"apps":{"http":{"servers":{"srv0":{"routes":[{"match":[{"host":["loaded.z.test"]}],"handle":[{"handler":"reverse_proxy","upstreams":[{"dial":"new.z.test:80"}]}]}]}}}}}'
_BOTH_MATCHED='{"apps":{"http":{"servers":{"srv0":{"routes":[{"match":[{"host":["Loaded.Z.test"]}]}]},"srv1":{"routes":[{"match":[{"path":["/x"]},{"host":["new.z.test"]}]},{"handle":[]}]}}}}}'

@test "caddy: a host that only appears in an upstream is not a loaded route (dry run names it pending)" {
  FAKE_CONFIG="$_UPSTREAM_ONLY" _run_caddy_phase3 --check
  [ "$status" -eq 0 ]
  assert_contains "$output" "would be added on a real run: new.z.test"
  assert_contains "$output" "already loaded: loaded.z.test"
}

@test "caddy: a host that only appears in an upstream is not a loaded route (real run fails)" {
  FAKE_CONFIG="$_UPSTREAM_ONLY" _run_caddy_phase3
  [ "$status" -ne 0 ]
  assert_contains "$output" "route new.z.test not found in Caddy's running config"
}

@test "caddy: host matchers are read from every server, any case, and routes without a host match are tolerated" {
  FAKE_CONFIG="$_BOTH_MATCHED" _run_caddy_phase3
  [ "$status" -eq 0 ]
  assert_contains "$output" "Caddy healthy with 2 route(s)"
}

@test "caddy: a dry run reports counts and names, and never claims Caddy is healthy" {
  _run_caddy_phase3 --check
  [ "$status" -eq 0 ]
  assert_contains "$output" "dry run: 2 declared, 1 loaded, 1 would be added (new.z.test)"
  refute_contains "$output" "Caddy healthy"
}
