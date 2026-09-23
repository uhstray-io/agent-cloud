#!/usr/bin/env bats
# Structural tests for the agentgateway service (platform/services/agentgateway).
# Verifies the composable shape: env-parameterized compose (gateway + its own
# budget Postgres) with a pinned image and NO published admin port, container-only
# deploy.sh that probes readiness from the sibling db container (the gateway image
# has no shell), a config template
# that carries no literal credential and no `retry`/`requestTimeout` block, and
# a composable deploy playbook (manage-secrets, `existing` upstream key, no
# secret generation of its own). No hardcoded IPs/credentials.
#
# Structural only (grep/file asserts) — no live deploy.
# Run: bats platform/tests/test_service_agentgateway.bats

load assert_helpers

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  DEPLOY_DIR="$REPO_ROOT/platform/services/agentgateway/deployment"
  PLAYBOOK="$REPO_ROOT/platform/playbooks/deploy-agentgateway.yml"
  CONFIG="$DEPLOY_DIR/templates/config.yaml.j2"
}

@test "agentgateway: compose env-parameterizes image + every published bind/port" {
  local f="$DEPLOY_DIR/compose.yml"
  [ -f "$f" ]
  assert_grep -E '\$\{AGW_IMAGE' "$f"
  assert_grep -E '\$\{AGW_BIND:-127\.0\.0\.1\}:\$\{AGW_PORT' "$f"
  assert_grep -E '\$\{AGW_READY_BIND:-127\.0\.0\.1\}:\$\{AGW_READY_PORT' "$f"
  assert_grep -E '\$\{AGW_STATS_BIND:-127\.0\.0\.1\}:\$\{AGW_STATS_PORT' "$f"
}

@test "agentgateway: image is fully-qualified and pinned (no :latest drift)" {
  local f="$DEPLOY_DIR/compose.yml"
  assert_grep -E 'cr\.agentgateway\.dev/agentgateway:v[0-9]+\.[0-9]+\.[0-9]+' "$f"
  refute_grep -E 'agentgateway:latest' "$f"
}

@test "agentgateway: operator UI rides its own listener and the GATEWAY runs the OIDC login" {
  assert_grep -qE '^\s*ui:$' "$CONFIG"
  assert_grep -qE '^\s*gateways: ui$' "$CONFIG"
  # Named gateways (llm.port is deprecated); LLM routes on BOTH so the UI playground
  # calls /v1 on its own origin through Caddy (no CORS, no extra browser port).
  assert_grep -qE '^\s*default:$' "$CONFIG"
  # Conditional on agw_ui_enabled; the rendered `[default, ui]` is asserted by the
  # "UI on by default" render test below.
  assert_grep -qF "gateways: {{ '[default, ui]' if _ui else '[default]' }}" "$CONFIG"
  refute_grep -qE '^\s*port: 4000$' <(sed -n '/^llm:/,$p' "$CONFIG")
  # Auth is the gateway's own ui.policies (the UI ignores an external gate — banner
  # "UI is exposed without authentication", 2026-09-17): OIDC + admin-group rule.
  assert_grep -qE '^\s*oidc:$' "$CONFIG"
  assert_grep -qF 'clientSecret: $AGW_OIDC_CLIENT_SECRET' "$CONFIG"
  assert_grep -qF 'in jwt.groups' "$CONFIG"
  refute_grep -qE 'clientSecret:\s*\{\{' "$CONFIG"
  # 64-hex cookie key derived from a stored seed; client secret shared-read from the IdP.
  assert_grep -qF "OIDC_COOKIE_SECRET={{ secrets.agw_oidc_cookie_seed | hash('sha256') }}" "$DEPLOY_DIR/templates/env.j2"
  assert_grep -q 'agentgateway_oidc_client_secret' "$PLAYBOOK"
  assert_grep -q '_shared_reads:' "$PLAYBOOK"
  assert_grep -q 'tasks/distribute-ca-root.yml' "$PLAYBOOK"
  assert_grep -q 'SSL_CERT_FILE' "$DEPLOY_DIR/compose.local.yml"
  assert_grep -qE '\$\{AGW_UI_BIND:-127\.0\.0\.1\}:\$\{AGW_UI_PORT' "$DEPLOY_DIR/compose.yml"
  # Locally the UI publish is removed: the overlay !overrides the port list without it.
  assert_grep -qE 'ports: !override' "$DEPLOY_DIR/compose.local.yml"
  refute_grep -q 'AGW_UI_PORT' "$DEPLOY_DIR/compose.local.yml"
  # Admin-tier OIDC app in the Authentik catalog; the Caddy route is a PLAIN proxy.
  local cat="$REPO_ROOT/platform/services/authentik/deployment/app-catalog.yml"
  grep -A6 '^  agentgateway:$' "$cat" | grep -q 'type: oidc'
  grep -A6 '^  agentgateway:$' "$cat" | grep -q 'tier: admin'
  local bp="$REPO_ROOT/platform/services/authentik/deployment/blueprints/agentgateway-oidc.yaml"
  [ -f "$bp" ]
  assert_grep -q 'client_secret: !Env AGENTGATEWAY_OIDC_CLIENT_SECRET' "$bp"
  refute_grep -qiE 'client_secret:\s*[A-Za-z0-9]{8,}' "$bp"
  assert_grep -qE 'host: "admin.inference.agent-cloud.test", upstream: "agentgateway:4001" \}' "$REPO_ROOT/platform/inventory/local-dev.yml.example"
  refute_grep -qE 'admin.inference.*forward_auth' "$REPO_ROOT/platform/inventory/local-dev.yml.example"
}

@test "agentgateway: admin port 15000 is never published" {
  # The admin listener stays on the container loopback (spec: "Admin interface
  # is not exposed"). Any `ports:` line carrying 15000 is a regression.
  refute_grep -E '^\s*-\s*".*15000' "$DEPLOY_DIR/compose.yml"
  refute_grep -E '^\s*-\s*".*15000' "$DEPLOY_DIR/compose.local.yml"
  # Pinned to the container loopback explicitly, not left to the upstream default.
  assert_grep -qE '^  adminAddr: 127\.0\.0\.1:15000$' "$CONFIG"
}

@test "agentgateway: config is a read-only bind mount; the only volume is the budget db" {
  local f="$DEPLOY_DIR/compose.yml"
  assert_grep -E './config.yaml:/config.yaml:ro' "$f"
  [ "$(grep -cE '^  [a-z-]+:$' <(sed -n '/^volumes:/,$p' "$f"))" -eq 1 ]
}

@test "agentgateway: compose has no hardcoded credentials or RFC1918 IPs" {
  local f="$DEPLOY_DIR/compose.yml"
  refute_grep -iE '(password|secret|token|api_key)\s*[:=]\s*["'\''0-9A-Za-z]{8}' "$f"
  refute_grep -E '192\.168\.|10\.[0-9]+\.|172\.(1[6-9]|2[0-9]|3[01])\.' "$f"
}

@test "agentgateway: deploy.sh is executable bash, sources common.sh, container-only, no secrets" {
  local f="$DEPLOY_DIR/deploy.sh"
  [ -f "$f" ] && [ -x "$f" ]
  head -1 "$f" | grep -qE '^#!/usr/bin/env bash'
  assert_grep -q 'common.sh' "$f"
  assert_grep -qE '\bcompose (pull|up)' "$f"
  assert_grep -q 'detect_runtime' "$f"
  # Lifecycle only — no secret generation / OpenBao interaction (Deploy Rule #2).
  refute_grep -iE 'openssl rand|secret_id|vault|\bbao |gen_secret|put_secret|get_secret' "$f"
}

@test "agentgateway: deploy.sh probes readiness from the sibling db container (image has no shell)" {
  # Not the host loopback: inside the local control plane that is the wrong vantage.
  assert_grep -q 'exec agentgateway-db wget' "$DEPLOY_DIR/deploy.sh"
  assert_grep -q 'http://agentgateway:19001/healthz/ready' "$DEPLOY_DIR/deploy.sh"
  refute_grep -q '127.0.0.1' "$DEPLOY_DIR/deploy.sh"
  # The GATEWAY service block (up to the db block) carries no healthcheck; the db does.
  refute_grep -q 'healthcheck:' <(sed -n '/^  agentgateway:$/,/^  agentgateway-db:$/p' "$DEPLOY_DIR/compose.yml")
}

@test "agentgateway: rendered files are gitignored" {
  local f="$DEPLOY_DIR/.gitignore"
  assert_grep -qE '^\.env$' "$f"
  assert_grep -qE '^config\.yaml$' "$f"
}

@test "agentgateway: config template carries no literal credential — env ref + sha256 hashes only" {
  [ -f "$CONFIG" ]
  # Upstream key is the gateway's env reference, never the secret value.
  assert_grep -qF 'apiKey: $VLLM_API_KEY' "$CONFIG"
  refute_grep -qE 'apiKey:\s*\{\{ secrets' "$CONFIG"
  # Client keys are enrolled as hashes of the OpenBao value.
  assert_grep -qF "keyHash: sha256:{{ secrets['client_' ~ c] | hash('sha256') }}" "$CONFIG"
  # A plaintext `key:` exists only inside the local-dev-only flag branch, default off.
  # ...and only together with local_mode, so a prod inventory cannot enable it.
  assert_grep -qF "{% if (agw_plaintext_keys | default(false) | bool) and (local_mode | default(false) | bool) %}" "$CONFIG"
  [ "$(grep -cE '^\s*-\s*key:\s' "$CONFIG")" -eq 1 ]
  # The deploy refuses the flag outside local_mode instead of silently ignoring it.
  assert_grep -qF "not (agw_plaintext_keys | default(false) | bool) or (local_mode | default(false) | bool)" "$REPO_ROOT/platform/playbooks/deploy-agentgateway.yml"
  # strict: an unknown key is 401 at the gateway.
  assert_grep -qE '^\s*mode: strict' "$CONFIG"
  refute_grep -E '192\.168\.|10\.[0-9]+\.' "$CONFIG"
}

@test "agentgateway: observability — identity label on metrics and logs, never prompt content" {
  assert_grep -qE '^\s*metrics:$' "$CONFIG"
  assert_grep -qE '^\s*logging:$' "$CONFIG"
  [ "$(grep -c 'identity: apiKey.name' "$CONFIG")" -eq 2 ]
  # Key form only: a comment may NAME the fields it forbids.
  refute_grep -qE ':\s*llm\.(prompt|completion)\b' "$CONFIG"
}

@test "agentgateway: config template has no retry block and no request timeouts (design §3)" {
  refute_grep -qE '^\s*retry:' "$CONFIG"
  # Key form only — the header comment is allowed to NAME the knobs it leaves unset.
  refute_grep -qE '^\s*(requestTimeout|backendRequestTimeout):' "$CONFIG"
}

@test "agentgateway: limits — one global request bucket + a per-key token budget (needs the db)" {
  # v1.5.0 rejects `key` and `conditional` under llm.policies (verified 2026-09-17);
  # budgets are the per-identity control and require config.database.
  refute_grep -qE '^\s*key: apiKey' "$CONFIG"
  refute_grep -qE '^\s*conditional:' "$CONFIG"
  assert_grep -qE '^\s*-\s*type: requests' "$CONFIG"
  assert_grep -qE '^\s*unit: Tokens' "$CONFIG"
  assert_grep -qE '^\s*onBudgetExceeded: Block' "$CONFIG"
  assert_grep -qF 'url: $AGW_DATABASE_URL' "$CONFIG"
  refute_grep -qE 'postgresql://' "$CONFIG"
}

@test "agentgateway: own Postgres is internal-only, pinned, healthchecked, and gates the gateway start" {
  local f="$DEPLOY_DIR/compose.yml"
  assert_grep -qE 'docker\.io/library/postgres:16' "$f"
  assert_grep -q 'container_name: agentgateway-db' "$f"
  assert_grep -q 'pg_isready' "$f"
  assert_grep -q 'condition: service_healthy' "$f"
  assert_grep -q 'agentgateway-pg-data:/var/lib/postgresql/data' "$f"
  # No published port for the db: every `ports:` entry belongs to the gateway.
  [ "$(grep -c '^\s*ports:' "$f")" -eq 1 ]
  assert_grep -q 'wait_for_healthy agentgateway-db' "$DEPLOY_DIR/deploy.sh"
}

@test "agentgateway: upstream is the custom completions provider with an inventory baseUrl" {
  assert_grep -qE '^\s*custom:' "$CONFIG"
  assert_grep -qE '^\s*-\s*type: completions' "$CONFIG"
  assert_grep -qF 'baseUrl: {{ agw_upstream_base_url }}' "$CONFIG"
}

@test "agentgateway: env template takes the upstream key from OpenBao, nothing literal" {
  local f="$DEPLOY_DIR/templates/env.j2"
  assert_grep -qF "VLLM_API_KEY={{ secrets.vllm_api_key | default('') }}" "$f"
  assert_grep -qF 'POSTGRES_PASSWORD={{ secrets.agw_db_password }}' "$f"
  refute_grep -iE '(password|secret|token|api_key)\s*[:=]\s*[A-Za-z0-9]{8,}' "$f"
  refute_grep -E '192\.168\.|10\.[0-9]+\.' "$f"
}

@test "agentgateway: deploy playbook is composable — manage-secrets, existing upstream key, renders both files" {
  [ -f "$PLAYBOOK" ]
  assert_grep -q 'tasks/place-monorepo.yml' "$PLAYBOOK"
  # A missing inventory group must fail loudly, not "succeed" with no hosts matched.
  assert_grep -q 'import_playbook: preflight-target-group.yml' "$PLAYBOOK"
  assert_grep -q 'preflight_group: agentgateway_svc' "$PLAYBOOK"
  assert_grep -q 'preflight_group_expected: agentgateway_svc' "$PLAYBOOK"
  assert_grep -q 'tasks/manage-secrets.yml' "$PLAYBOOK"
  assert_grep -q 'include_tasks: tasks/assert-bao-transport.yml' "$PLAYBOOK"
  assert_grep -q 'tasks/place-monorepo.yml' "$PLAYBOOK"  # the preamble enables linger
  # The upstream key is never generated by the deploy.
  assert_grep -qE "'name': 'vllm_api_key', 'type': 'existing'" "$PLAYBOOK"
  assert_grep -qE "'name': 'agw_db_password', 'type': 'random'" "$PLAYBOOK"
  # An empty upstream key is refused unless the inventory says the upstream takes none.
  assert_grep -q 'Refuse to deploy without the upstream key when the upstream requires one' "$PLAYBOOK"
  assert_grep -qF "or not (agw_upstream_requires_key | default(true) | bool)" "$PLAYBOOK"
  assert_grep -qE "'name': 'agw_oidc_cookie_seed', 'type': 'random'" "$PLAYBOOK"
  assert_grep -qE 'src: env.j2, dest: .env, mode: "0600"' "$PLAYBOOK"
  assert_grep -qE 'src: config.yaml.j2, dest: config.yaml' "$PLAYBOOK"
  # Nothing beyond ansible-core: no collection-only filter.
  refute_grep -qE 'community\.general\.[a-z_]+' "$PLAYBOOK"
}

@test "agentgateway: playbook verifies over the compose network, asserts the 401, puts no key on an argv" {
  # The client key is a `uri` header in exactly two no_log tasks, and no command or shell task
  # ever sees it: a shell probe put it on wget's argv (PR 221 Codex review).
  python3 - "$PLAYBOOK" <<'PY'
import sys, yaml
def walk(tasks):
    for t in tasks or []:
        yield t
        for k in ("block", "rescue", "always"):
            yield from walk(t.get(k))
keyed = []
for play in yaml.safe_load(open(sys.argv[1])):
    for t in walk(play.get("tasks")):
        body = {k: v for k, v in t.items() if k not in ("name", "when")}
        if "_resolved['client_" in str(body):
            keyed.append(t)
assert len(keyed) == 2, [t.get("name") for t in keyed]
for t in keyed:
    assert "ansible.builtin.uri" in t and t.get("no_log") is True, t.get("name")
    assert "_resolved['client_" in t["ansible.builtin.uri"]["headers"]["Authorization"], t.get("name")
    assert "_resolved['client_" not in str({k: v for k, v in t["ansible.builtin.uri"].items() if k != "headers"})
PY
  [ "$(grep -c 'no_log: true' "$PLAYBOOK")" -eq 2 ]
  refute_grep -qE "secrets\['client_" "$PLAYBOOK"
  refute_grep -qF 'read -r k' "$PLAYBOOK"
  # The identity is checked against, and asks for, only the models it may use; a 429 from the
  # gateway's own policy is reported as unproven, not failed as a routing fault.
  assert_grep -qF '.allowed_models' "$PLAYBOOK"
  assert_grep -qF 'model: "{{ _verify_models[0] }}"' "$PLAYBOOK"
  assert_grep -qF '(_keyed_models.status | default(-1)) == 429' "$PLAYBOOK"
  # Only the gateway's own refusal body counts; a 429 relayed from the upstream fails.
  [ "$(grep -cF "| trim) == 'rate limit exceeded'" "$PLAYBOOK")" -eq 2 ]
  assert_grep -qF 'round-trip was NOT proven on this run' "$PLAYBOOK"
  # Local-dev's verify runs in the Semaphore container, on the gateway's network.
  assert_grep -qF 'agw_verify_base_url=http://agentgateway:4000' "$REPO_ROOT/platform/playbooks/bootstrap-local-dev.yml"
  assert_grep -q 'exec agentgateway-db wget' "$PLAYBOOK"
  assert_grep -q 'http://agentgateway:19001/healthz/ready' "$PLAYBOOK"
  assert_grep -qF "'401' not in _noauth.stderr" "$PLAYBOOK"
  assert_grep -qE 'mode: "0644"' "$PLAYBOOK"
}

@test "agentgateway: clean-deploy composes clean-service then the deploy playbook" {
  local f="$REPO_ROOT/platform/playbooks/clean-deploy-agentgateway.yml"
  [ -f "$f" ]
  assert_grep -q 'tasks/clean-service.yml' "$f"
  assert_grep -q 'import_playbook: deploy-agentgateway.yml' "$f"
}

@test "agentgateway: Semaphore templates exist as code for deploy and clean deploy" {
  local f="$REPO_ROOT/platform/semaphore/templates.yml"
  assert_grep -q 'playbook: platform/playbooks/deploy-agentgateway.yml' "$f"
  assert_grep -q 'playbook: platform/playbooks/clean-deploy-agentgateway.yml' "$f"
  # And the worktree-bound local catalog, so `make local-deploy-agentgateway` runs the working tree.
  local l="$REPO_ROOT/platform/semaphore/templates-local.yml"
  assert_grep -q 'playbook: platform/playbooks/deploy-agentgateway.yml' "$l"
  assert_grep -q 'playbook: platform/playbooks/clean-deploy-agentgateway.yml' "$l"
}

@test "agentgateway: virtual-key management is code — rotate/revoke playbook, inventory-gated, no values printed" {
  local f="$REPO_ROOT/platform/playbooks/manage-agentgateway-client-key.yml"
  [ -f "$f" ]
  assert_grep -q 'import_playbook: preflight-target-group.yml' "$f"
  assert_grep -q 'include_tasks: tasks/assert-bao-transport.yml' "$f"
  # Store writes go through the shared merge task; revoke is a merge-patch null.
  assert_grep -q 'include_tasks: tasks/bao-merge-keys.yml' "$f"
  # Inventory is the source of who exists: rotate needs the name declared, revoke needs it gone.
  assert_grep -q '_client in _declared' "$f"
  assert_grep -q '_client not in _declared' "$f"
  # Always ends by re-rendering + reloading through the deploy playbook.
  assert_grep -q 'import_playbook: deploy-agentgateway.yml' "$f"
  # Handout is the existing site-config channel, never stdout: the report task names
  # the channel and carries no key value (scoped to that task, not the whole file).
  local report
  report=$(sed -n '/name: "Report (no values)"/,/^- name:/p' "$f")
  assert_contains "$report" 'backup-credentials-to-site-config.yml'
  refute_contains "$report" '_new_value'
  refute_contains "$report" "secrets["
  # Revoke goes through the shared merge task (no hand-rolled merge-patch here).
  assert_grep -q '_bm_remove:' "$f"
  refute_grep -q 'application/merge-patch+json' "$f"
  assert_grep -q 'playbook: platform/playbooks/manage-agentgateway-client-key.yml' "$REPO_ROOT/platform/semaphore/templates.yml"
  assert_grep -q 'playbook: platform/playbooks/manage-agentgateway-client-key.yml' "$REPO_ROOT/platform/semaphore/templates-local.yml"
}

@test "agentgateway: per-identity policy overrides render from inventory (allowedModels, budget)" {
  assert_grep -qF 'agw_client_policies' "$CONFIG"
  assert_grep -qF 'allowedModels: {{ _pol.allowed_models | to_json }}' "$CONFIG"
  assert_grep -qF '_pol.tokens_per_hour | default(agw_rate_tokens_per_hour' "$CONFIG"
}

# ── agw_ui_enabled: /v1 can ship before the Authentik client exists ─────────
# v1.5.0 fetches the OIDC discovery document when it loads config
# (`--validate-only` on a UI-on render fails "failed to decode oidc discovery
# response", 2026-09-22), so a UI-on gateway cannot even start before its
# Authentik provider exists. The flag renders the gateway without any of it.
_render_ui() {  # $1 = true|false|unset ; renders into $BATS_TEST_TMPDIR/<name>
  command -v ansible-playbook >/dev/null || skip "ansible-playbook not installed"
  local flag="" play="$BATS_TEST_TMPDIR/render.yml"
  [ "$1" != unset ] && flag="agw_ui_enabled: $1"
  cat >"$play" <<YML
- hosts: localhost
  gather_facts: false
  vars:
    $flag
    agw_clients: [stray]
    agw_models: [{name: m}]
    agw_upstream_base_url: "http://upstream.invalid:8000/v1"
    secrets: {client_stray: k, vllm_api_key: v, agw_db_password: p, agw_oidc_cookie_seed: s, agentgateway_oidc_client_secret: c}
  tasks:
    - ansible.builtin.template: {src: "$DEPLOY_DIR/templates/config.yaml.j2", dest: "$BATS_TEST_TMPDIR/config.yaml", mode: "0644"}
    - ansible.builtin.template: {src: "$DEPLOY_DIR/templates/env.j2", dest: "$BATS_TEST_TMPDIR/env", mode: "0600"}
YML
  ansible-playbook -i localhost, -c local "$play" >/dev/null
}

@test "agentgateway: UI on by default — listener, OIDC policy, playground route, OIDC env" {
  _render_ui unset
  assert_grep -qE '^    port: 4001$' "$BATS_TEST_TMPDIR/config.yaml"
  assert_grep -qE '^    oidc:$' "$BATS_TEST_TMPDIR/config.yaml"
  assert_grep -qF 'gateways: [default, ui]' "$BATS_TEST_TMPDIR/config.yaml"
  assert_grep -q '^AGW_OIDC_CLIENT_SECRET=' "$BATS_TEST_TMPDIR/env"
  assert_grep -q '^OIDC_COOKIE_SECRET=' "$BATS_TEST_TMPDIR/env"
}

@test "agentgateway: agw_ui_enabled=false renders no UI listener, no OIDC, /v1 on default only" {
  _render_ui false
  refute_grep -q 'port: 4001' "$BATS_TEST_TMPDIR/config.yaml"
  refute_grep -qE '^ui:|oidc:|jwt\.groups' "$BATS_TEST_TMPDIR/config.yaml"
  assert_grep -qF 'gateways: [default]' "$BATS_TEST_TMPDIR/config.yaml"
  # The API itself is untouched: still strict apiKey in front of the upstream.
  assert_grep -qF 'mode: strict' "$BATS_TEST_TMPDIR/config.yaml"
  assert_grep -qF 'baseUrl: http://upstream.invalid:8000/v1' "$BATS_TEST_TMPDIR/config.yaml"
  refute_grep -q 'OIDC' "$BATS_TEST_TMPDIR/env"
}

@test "agentgateway: agw_ui_enabled=false drops the Authentik shared read from the deploy" {
  local blk
  blk=$(sed -n '/_shared_reads: >-/,/_env_templates:/p' "$PLAYBOOK")
  assert_grep -qF "if (agw_ui_enabled | default(true) | bool) else []" <<<"$blk"
  assert_grep -qF "'read_keys': ['agentgateway_oidc_client_secret']" <<<"$blk"
}
