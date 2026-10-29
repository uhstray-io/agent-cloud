#!/usr/bin/env bats
# Structural tests for the Dispatch Console (platform/services/console). Grouped by
# owner: the Authentik side (the OIDC client declared as config-as-code: secret
# declaration, worker env line, app-catalog entry, blueprint) comes first; the
# console's own deploy-side tests are appended below their own marker.
#
# Structural (grep / YAML asserts) — no live Authentik, Semaphore or OpenBao. The
# guard and project-resolution tests at the end also run the real assert and
# set_fact tasks under a local ansible-playbook against a scratch inventory, so
# they touch no service. Assertions use the helpers in assert_helpers.bash
# (refute_grep / assert_grep), never `! grep` or `|| true`, so each one can
# actually fail (CONTRIBUTING.md "Writing BATS Tests").
#
# Run: bats platform/tests/test_service_console.bats

load assert_helpers

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  AUTHENTIK_DIR="$REPO_ROOT/platform/services/authentik/deployment"
  DEPLOY_DIR="$REPO_ROOT/platform/services/console/deployment"
  PLAYBOOK="$REPO_ROOT/platform/playbooks/deploy-console.yml"
  CLEAN_PLAYBOOK="$REPO_ROOT/platform/playbooks/clean-deploy-console.yml"
}

# --- Authentik ---

@test "console: OIDC blueprint is a valid config-as-code blueprint (version 1, provider + app)" {
  local f="$AUTHENTIK_DIR/blueprints/console-oidc.yaml"
  [ -f "$f" ]
  assert_grep -qE '^version: 1' "$f"
  assert_grep -qF 'authentik_providers_oauth2.oauth2provider' "$f"
  assert_grep -qF 'authentik_core.application' "$f"
}

@test "console: OAuth2 provider is confidential, client_id console, secret injected from env" {
  local f="$AUTHENTIK_DIR/blueprints/console-oidc.yaml"
  local prov="$BATS_TEST_TMPDIR/provider.yaml"
  # Scope every assertion to the provider entry, not the whole file.
  sed -n '/id: provider-console/,/model: authentik_core.application/p' "$f" > "$prov"
  assert_grep -qE '^      name: console$' "$prov"
  assert_grep -qE '^      client_type: confidential$' "$prov"
  assert_grep -qE '^      client_id: console$' "$prov"
  assert_grep -qxF '      client_secret: !Env CONSOLE_OIDC_CLIENT_SECRET' "$prov"
  # The secret is never a literal in git.
  refute_grep -qiE 'client_secret:[[:space:]]+[A-Za-z0-9]{8,}' "$f"
}

@test "console: redirect is strict and defaults to the console's /auth/callback route" {
  local f="$AUTHENTIK_DIR/blueprints/console-oidc.yaml"
  local prov="$BATS_TEST_TMPDIR/provider.yaml"
  sed -n '/id: provider-console/,/model: authentik_core.application/p' "$f" > "$prov"
  assert_grep -qE '^        - matching_mode: strict$' "$prov"
  assert_grep -qF '          url: !Context console_redirect' "$prov"
  # Context default (local-dev): BASE_URL + /auth/callback, rendered from inventory in prod.
  assert_grep -qE "^  console_redirect: .*default\('https://[^']*/auth/callback'\)" "$f"
}

@test "console: application slug console is bound to the console provider and launches the console" {
  local f="$AUTHENTIK_DIR/blueprints/console-oidc.yaml"
  local app="$BATS_TEST_TMPDIR/app.yaml"
  sed -n '/model: authentik_core.application/,$p' "$f" > "$app"
  assert_grep -qE '^      slug: console$' "$app"
  assert_grep -qE '^      name: Dispatch Console$' "$app"
  assert_grep -qF '      provider: !KeyOf provider-console' "$app"
  assert_grep -qF '      meta_launch_url: !Context console_launch' "$app"
}

@test "console: app catalog declares a member-tier OIDC app whose blueprint file exists" {
  local c="$AUTHENTIK_DIR/app-catalog.yml"
  [ -f "$c" ]
  python3 - "$c" "$AUTHENTIK_DIR" <<'PY'
import os, sys, yaml
cat = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))["authentik_app_catalog"]
e = cat["console"]
assert e["type"] == "oidc", e
assert e["tier"] == "member", e
assert e["provider"] == "console", e
assert e["file"] == "console-oidc.yaml", e
assert e["prod_required"] == ["console_redirect_uri", "console_launch_url"], e
assert e["verify_redirect"] == "console_redirect_uri", e
assert os.path.isfile(os.path.join(sys.argv[2], "blueprints", e["file"])), e
PY
}

@test "console: OIDC client secret is declared once in the Authentik secret contract (random, 40)" {
  local d="$REPO_ROOT/platform/playbooks/vars/secret-declarations/authentik.yml"
  [ -f "$d" ]
  python3 - "$d" <<'PY'
import sys, yaml
decl = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))["_static_secret_definitions"]
hits = [e for e in decl if e.get("name") == "console_oidc_client_secret"]
assert hits == [{"name": "console_oidc_client_secret", "type": "random", "length": 40}], hits
PY
}

@test "console: worker env template injects CONSOLE_OIDC_CLIENT_SECRET from OpenBao, no literal" {
  local f="$AUTHENTIK_DIR/templates/env.j2"
  [ -f "$f" ]
  assert_grep -qxF 'CONSOLE_OIDC_CLIENT_SECRET={{ secrets.console_oidc_client_secret }}' "$f"
  refute_grep -qE '^CONSOLE_OIDC_CLIENT_SECRET=[^{]' "$f"
}

# --- Deploy ---

@test "console: compose env-parameterizes image + bind/port" {
  local f="$DEPLOY_DIR/compose.yml"
  [ -f "$f" ]
  grep -qE '\$\{CONSOLE_IMAGE' "$f"
  grep -qE '\$\{CONSOLE_BIND' "$f"
  grep -qE '\$\{CONSOLE_PORT' "$f"
}

@test "console: single container persists preferences on the console-data named volume" {
  local f="$DEPLOY_DIR/compose.yml"
  grep -qE '^[[:space:]]+console:' "$f"
  grep -qE 'console-data:/data' "$f"
  grep -qE '^volumes:' "$f"
  grep -qE '^[[:space:]]+console-data:' "$f"
  grep -qE 'restart: always' "$f"
}

@test "console: compose declares no healthcheck (podman-compose 1.0.6 wraps it in /bin/sh; the image has no shell)" {
  # Comment lines may name /bin/sh and CMD-SHELL to explain the decision; only a live key counts.
  local code="$BATS_TEST_TMPDIR/compose-code.yml"
  sed '/^[[:space:]]*#/d' "$DEPLOY_DIR/compose.yml" > "$code"
  [ -s "$code" ]
  refute_grep -qE '^[[:space:]]+healthcheck:' "$code"
  refute_grep -qE 'CMD-SHELL|/bin/sh' "$code"
}

@test "console: compose has no hardcoded credentials" {
  local f="$DEPLOY_DIR/compose.yml"
  refute_grep -qiE '(password|secret|token|api_key)[[:space:]]*[:=][[:space:]]*["'\''0-9A-Za-z]{8}' "$f"
}

@test "console: compose has no RFC1918 IPs" {
  local f="$DEPLOY_DIR/compose.yml"
  refute_grep -qE '192\.168\.|10\.[0-9]+\.|172\.(1[6-9]|2[0-9]|3[01])\.' "$f"
}

@test "console: deploy.sh is executable bash, sources common.sh, uses compose_up_if_changed, no secrets" {
  local f="$DEPLOY_DIR/deploy.sh"
  [ -f "$f" ] && [ -x "$f" ]
  head -1 "$f" | grep -qE '^#!/usr/bin/env bash'
  grep -q 'common.sh' "$f"
  grep -qE 'compose_up_if_changed' "$f"
  grep -qF 'exec console /usr/local/app/server probe /healthz 200' "$f"
  # Lifecycle plus a host build only: no secret generation, no OpenBao interaction (Deploy Rule #2).
  refute_grep -qiE 'openssl rand|secret_id|vault|\bbao |gen_secret|put_secret|get_secret' "$f"
}

@test "console: deploy.sh never forces a recreate on every run" {
  # Comment lines may name the flag; only a live use counts.
  local code="$BATS_TEST_TMPDIR/deploy-code.sh"
  sed '/^[[:space:]]*#/d' "$DEPLOY_DIR/deploy.sh" > "$code"
  [ -s "$code" ]
  refute_grep -q -- '--force-recreate' "$code"
}

# _run_step4 <pass_at> <bound: timeout|none> [stall]
# Runs deploy.sh's _console_probe_once and step_wait_healthy, both extracted as written, against
# a stub engine that logs each `exec` and succeeds from the pass_at-th one on (never when 0).
#   timeout : a `timeout` shim is first on PATH; it logs its arguments, then runs the command.
#   none    : PATH holds only bash, date and grep, so `command -v timeout` finds nothing.
#   stall   : the probe's bound is a function that costs 15 s of wall clock and returns 124, the
#             status coreutils timeout reports. It models a stalled `podman exec`.
# sleep logs its argument and advances SECONDS by it, so the step's wall-clock deadline is real
# inside the test. A loop with no bound stops after 200 sleeps with status 99: a named failure,
# not a hung run. Sets $status, $output and the logs EXEC_LOG, SLEEP_LOG, TIMEOUT_LOG.
_run_step4() {
  local pass_at="$1" bound="$2" stall="${3:-}"
  local step="$BATS_TEST_TMPDIR/step4.sh" driver="$BATS_TEST_TMPDIR/driver.sh"
  local shim="$BATS_TEST_TMPDIR/shim" minbin="$BATS_TEST_TMPDIR/minbin" path
  EXEC_LOG="$BATS_TEST_TMPDIR/exec.log"
  SLEEP_LOG="$BATS_TEST_TMPDIR/sleep.log"
  TIMEOUT_LOG="$BATS_TEST_TMPDIR/timeout.log"
  {
    sed -n '/^_console_probe_once()/,/^}/p' "$DEPLOY_DIR/deploy.sh"
    sed -n '/^step_wait_healthy()/,/^}/p' "$DEPLOY_DIR/deploy.sh"
  } > "$step"
  assert_grep -q '^_console_probe_once()' "$step"
  assert_grep -q '^step_wait_healthy()' "$step"
  cat > "$BATS_TEST_TMPDIR/engine" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$EXEC_LOG"
n=$(grep -c '' "$EXEC_LOG")
if [ "${PASS_AT:-0}" -gt 0 ] && [ "$n" -ge "$PASS_AT" ]; then exit 0; fi
exit 1
STUB
  chmod +x "$BATS_TEST_TMPDIR/engine"
  mkdir -p "$shim" "$minbin"
  cat > "$shim/timeout" <<'SHIM'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$TIMEOUT_LOG"
while [ "$1" = "-k" ]; do shift 2; done
shift
exec "$@"
SHIM
  chmod +x "$shim/timeout"
  local tool
  for tool in bash date grep; do ln -sf "$(command -v "$tool")" "$minbin/$tool"; done
  if [ "$bound" = timeout ]; then path="$shim:$PATH"; else path="$minbin"; fi
  cat > "$driver" <<'DRIVER'
source "$COMMON"
CONTAINER_ENGINE="$ENGINE"
dump_container_diagnostics() { echo "DIAGNOSTICS-DUMPED for $1"; }
sleep() {
  printf '%s\n' "$1" >> "$SLEEP_LOG"
  SLEEPS=$(( ${SLEEPS:-0} + 1 ))
  [ "$SLEEPS" -lt 200 ] || { echo "RUNAWAY: the probe loop has no bound" >&2; exit 99; }
  SECONDS=$(( SECONDS + $1 ))
}
if [ "${STALL:-}" = stall ]; then
  timeout() { printf '%s\n' "$*" >> "$TIMEOUT_LOG"; SECONDS=$(( SECONDS + 15 )); return 124; }
fi
source "$STEP"
step_wait_healthy
DRIVER
  run env PATH="$path" COMMON="$REPO_ROOT/platform/lib/common.sh" ENGINE="$BATS_TEST_TMPDIR/engine" \
    STEP="$step" EXEC_LOG="$EXEC_LOG" SLEEP_LOG="$SLEEP_LOG" TIMEOUT_LOG="$TIMEOUT_LOG" \
    PASS_AT="$pass_at" STALL="$stall" "$BASH" "$driver"
}

@test "console: step 4 probes under timeout 15 and returns at the first passing probe" {
  _run_step4 3 timeout
  [ "$status" -eq 0 ]
  assert_contains "$output" "console is ready"
  refute_contains "$output" "DIAGNOSTICS-DUMPED"
  # Three attempts, each one bounded on the host by `timeout -k 5 15` and each one the probe.
  [ "$(grep -c '' "$TIMEOUT_LOG")" -eq 3 ]
  [ "$(grep -cxF -- "-k 5 15 $BATS_TEST_TMPDIR/engine exec console /usr/local/app/server probe /healthz 200" "$TIMEOUT_LOG")" -eq 3 ]
  [ "$(grep -c '' "$EXEC_LOG")" -eq 3 ]
  # Every logged exec is the probe: the matching-line count equals the total line count.
  [ "$(grep -cxF 'exec console /usr/local/app/server probe /healthz 200' "$EXEC_LOG")" -eq 3 ]
  # Two sleeps between the three attempts, each one 5 s.
  [ "$(grep -c '' "$SLEEP_LOG")" -eq 2 ]
  [ "$(grep -cxF 5 "$SLEEP_LOG")" -eq 2 ]
}

@test "console: without coreutils timeout on the host, step 4 still probes and returns on the first pass" {
  _run_step4 2 none
  [ "$status" -eq 0 ]
  assert_contains "$output" "console is ready"
  # The operator is told the probes are unbounded, so a later hang is explainable.
  assert_contains "$output" "each readiness probe runs unbounded"
  [ "$(grep -c '' "$EXEC_LOG")" -eq 2 ]
  [ "$(grep -cxF 5 "$SLEEP_LOG")" -eq 1 ]
  # Nothing was bounded: no timeout shim was on this PATH.
  [ ! -e "$TIMEOUT_LOG" ]
  # The precondition this test rests on: the PATH it ran under has no timeout at all.
  run env PATH="$BATS_TEST_TMPDIR/minbin" "$BASH" -c 'command -v timeout'
  [ -z "$output" ]
}

@test "console: step 4 ends at the 300 s wall-clock deadline, probing every 5 s; on timeout it dumps diagnostics and fails" {
  _run_step4 0 timeout
  [ "$status" -eq 1 ]
  refute_contains "$output" "RUNAWAY"
  # Each failed attempt is followed by a 5 s sleep, so attempts and sleeps match, and every
  # attempt is bounded. About 60 attempts span the 300 s deadline; real seconds spent in the
  # test shorten that count a little, so the range allows for it.
  local n
  n=$(grep -c '' "$EXEC_LOG")
  [ "$n" -ge 55 ]
  [ "$n" -le 60 ]
  [ "$(grep -c '' "$SLEEP_LOG")" -eq "$n" ]
  [ "$(grep -cxF 5 "$SLEEP_LOG")" -eq "$n" ]
  [ "$(grep -c '' "$TIMEOUT_LOG")" -eq "$n" ]
  assert_contains "$output" "DIAGNOSTICS-DUMPED for console"
  assert_contains "$output" "console did not become ready within 300s"
}

@test "console: a stalled probe spends real time against the deadline, so the loop ends after about 15 attempts" {
  _run_step4 0 timeout stall
  [ "$status" -eq 1 ]
  refute_contains "$output" "RUNAWAY"
  # A stalled probe costs 15 s and the sleep after it 5 s: 20 s per attempt, so 15 attempts reach
  # the 300 s deadline. A loop that counted only its sleeps would run 60 attempts.
  [ "$(grep -c '' "$TIMEOUT_LOG")" -eq 15 ]
  [ ! -s "$EXEC_LOG" ]
  assert_contains "$output" "DIAGNOSTICS-DUMPED for console"
  assert_contains "$output" "console did not become ready within 300s"
}

@test "console: deploy.sh builds the image on the host, labelled with its inputs digest" {
  local f="$DEPLOY_DIR/deploy.sh"
  grep -q 'build_console_image()' "$f"
  grep -q 'io.agent-cloud.console.build-inputs-sha256' "$f"
  grep -qE '\$CONTAINER_ENGINE build ' "$f"
  grep -q -- '--label "${CONSOLE_BUILD_LABEL}=${want}"' "$f"
  grep -q -- '-f Containerfile' "$f"
  # The inputs are the app crate (minus its target/) and the Containerfile.
  grep -q 'find ../app' "$f"
  grep -q -- '-path ../app/target -prune' "$f"
  grep -q 'echo Containerfile' "$f"
}

@test "console: deploy.sh skips the build only when the image label matches the inputs" {
  local f="$DEPLOY_DIR/deploy.sh"
  # The skip is a positive match, never a mismatch, and the force lever is the literal true only.
  grep -qF '[ "${CONSOLE_FORCE_REBUILD:-}" != "true" ] && [ -n "$have" ] && [ "$have" = "$want" ]' "$f"
  grep -q 'CONSOLE_FORCE_REBUILD=true rebuilds' "$f"
}

@test "console: deploy.sh passes the local seccomp profile to the build only when set" {
  local f="$DEPLOY_DIR/deploy.sh"
  grep -qF 'sec=(--security-opt "seccomp=${CONSOLE_BUILD_SECCOMP}")' "$f"
  grep -qF '${sec[@]+"${sec[@]}"}' "$f"
}

@test "console: deploy.sh never sources .env into the shell environment" {
  # Only CONSOLE_IMAGE is read from .env, by key. Sourcing it would export the OIDC secret.
  local code="$BATS_TEST_TMPDIR/deploy-code.sh"
  sed '/^[[:space:]]*#/d' "$DEPLOY_DIR/deploy.sh" > "$code"
  [ -s "$code" ]
  refute_grep -qE '^[[:space:]]*(\.|source) .*\.env' "$code"
  refute_grep -qE 'set -a' "$code"
}

@test "console: env template is non-secret config plus the shared OIDC secret, no literals" {
  local f="$DEPLOY_DIR/templates/env.j2"
  [ -f "$f" ]
  # The client secret is a Jinja ref into the secrets.* namespace, never a literal.
  grep -qF 'CONSOLE_OIDC_CLIENT_SECRET={{ secrets.console_oidc_client_secret }}' "$f"
  grep -qF 'CONSOLE_OIDC_CLIENT_ID=console' "$f"
  grep -qF 'CONSOLE_SCOPE_FILE=/etc/console/scope.json' "$f"
  refute_grep -qE '192\.168\.|10\.[0-9]+\.' "$f"
  refute_grep -qiE '(password|secret|token|api_key)[[:space:]]*[:=][[:space:]]*[A-Za-z0-9]{8,}' "$f"
}

@test "console: scope template renders the inventory's tenants as JSON" {
  local f="$DEPLOY_DIR/templates/scope.json.j2"
  [ -f "$f" ]
  grep -qF 'console_tenants' "$f"
  grep -qF 'console_scope_is_example' "$f"
  grep -qF 'to_nice_json' "$f"
}

@test "console: deploy playbook refuses cleartext OpenBao before it places the repo or writes a secret" {
  local guard place secrets
  guard=$(grep -n 'tasks/assert-bao-transport.yml' "$PLAYBOOK" | head -1 | cut -d: -f1)
  place=$(grep -n 'tasks/place-monorepo.yml' "$PLAYBOOK" | head -1 | cut -d: -f1)
  secrets=$(grep -n 'tasks/manage-secrets.yml' "$PLAYBOOK" | head -1 | cut -d: -f1)
  [ -n "$guard" ]
  [ -n "$place" ]
  [ -n "$secrets" ]
  [ "$guard" -lt "$place" ]
  [ "$place" -lt "$secrets" ]
}

@test "console: deploy playbook is composable (manage-secrets, shared OIDC read, env + scope templates)" {
  [ -f "$PLAYBOOK" ]
  grep -q 'tasks/manage-secrets.yml' "$PLAYBOOK"
  grep -q '_secret_definitions:' "$PLAYBOOK"
  grep -q '_shared_reads:' "$PLAYBOOK"
  grep -q 'from_service: authentik' "$PLAYBOOK"
  grep -q 'console_oidc_client_secret' "$PLAYBOOK"
  grep -q '_env_templates:' "$PLAYBOOK"
  grep -q 'src: env.j2, dest: .env' "$PLAYBOOK"
  grep -q 'src: scope.json.j2, dest: config/scope.json' "$PLAYBOOK"
  grep -q 'tasks/place-monorepo.yml' "$PLAYBOOK"
}

@test "console: deploy playbook creates config/ before manage-secrets templates into it" {
  local mkdir_line secrets_line
  mkdir_line=$(grep -n 'path: "{{ _deploy_dir }}/config"' "$PLAYBOOK" | head -1 | cut -d: -f1)
  secrets_line=$(grep -n 'tasks/manage-secrets.yml' "$PLAYBOOK" | head -1 | cut -d: -f1)
  [ -n "$mkdir_line" ]
  [ -n "$secrets_line" ]
  [ "$mkdir_line" -lt "$secrets_line" ]
}

@test "console: deploy playbook gives the build levers to deploy.sh" {
  grep -q "CONSOLE_FORCE_REBUILD:" "$PLAYBOOK"
  grep -q "CONSOLE_BUILD_SECCOMP:" "$PLAYBOOK"
  grep -q "console_force_rebuild" "$PLAYBOOK"
  grep -q "console_build_seccomp" "$PLAYBOOK"
}

@test "console: deploy playbook verifies the probes and records the result last" {
  grep -qF 'exec console /usr/local/app/server probe /healthz 200' "$PLAYBOOK"
  grep -qF 'exec console /usr/local/app/server probe /v1/me 401' "$PLAYBOOK"
  local last
  last=$(grep -n 'tasks/emit-service-deploy-result.yml' "$PLAYBOOK" | tail -1 | cut -d: -f1)
  [ -n "$last" ]
  # The record is the playbook's final include: nothing after it may run.
  [ "$(awk -v n="$last" 'NR>n && /^[[:space:]]*- name:/' "$PLAYBOOK" | wc -l)" -eq 0 ]
}

@test "console: clean-deploy confirms the reset, then runs the deploy" {
  [ -f "$CLEAN_PLAYBOOK" ]
  grep -q 'tasks/clean-service.yml' "$CLEAN_PLAYBOOK"
  grep -q 'import_playbook: deploy-console.yml' "$CLEAN_PLAYBOOK"
  grep -q '_extra_var_guard_nested: true' "$CLEAN_PLAYBOOK"
  grep -q 'refuse-internal-extra-vars.yml' "$CLEAN_PLAYBOOK"
}

@test "console: Semaphore templates expose the deploy and clean-deploy, the rebuild lever included" {
  local t="$REPO_ROOT/platform/semaphore/templates.yml"
  grep -q 'playbook: platform/playbooks/deploy-console.yml' "$t"
  grep -q 'playbook: platform/playbooks/clean-deploy-console.yml' "$t"
  grep -q 'name: console_force_rebuild' "$t"
  local l="$REPO_ROOT/platform/semaphore/templates-local.yml"
  grep -q 'playbook: platform/playbooks/deploy-console.yml' "$l"
  grep -q 'playbook: platform/playbooks/clean-deploy-console.yml' "$l"
}

@test "console: local-dev routes console.<zone> to the container and declares its inventory group" {
  grep -qF "{'host': 'console.{{ _dev_zone }}', 'upstream': 'console:8080'}" "$REPO_ROOT/platform/playbooks/bootstrap-local-dev.yml"
  grep -qF '[console_svc]' "$REPO_ROOT/platform/playbooks/bootstrap-local-dev.yml"
  grep -qF 'console-local ansible_host=127.0.0.1' "$REPO_ROOT/platform/playbooks/bootstrap-local-dev.yml"
  grep -qF 'host: "console.agent-cloud.test", upstream: "console:8080"' "$REPO_ROOT/platform/inventory/local-dev.yml.example"
  grep -qE '^[[:space:]]+console_svc:' "$REPO_ROOT/platform/inventory/local-dev.yml.example"
}

@test "console: validate-all probes the console inside its container" {
  local v="$REPO_ROOT/platform/playbooks/validate-all.yml"
  grep -q 'name: "Validate console"' "$v"
  grep -qF 'exec console /usr/local/app/server probe /healthz 200' "$v"
}

@test "console: deployment has its .gitignore for rendered files and an AGENTS.md link" {
  local g="$DEPLOY_DIR/.gitignore"
  grep -qx '.env' "$g"
  grep -qx 'config/' "$g"
  grep -qx 'certs/' "$g"
  [ -L "$DEPLOY_DIR/AGENTS.md" ]
  [ "$(readlink "$DEPLOY_DIR/AGENTS.md")" = "CLAUDE.md" ]
}

@test "console: build-context ignore file keeps .env, config/, certs/ and target/ out of the image build" {
  # The build context is the service directory, so the OIDC secret in deployment/.env is in the
  # context unless this file excludes it. Podman reads <Containerfile>.dockerignore beside the file.
  local f="$DEPLOY_DIR/Containerfile.dockerignore"
  [ -f "$f" ]
  grep -qx 'deployment/.env' "$f"
  grep -qx 'deployment/config' "$f"
  grep -qx 'deployment/certs' "$f"
  grep -qx 'app/target' "$f"
}

# --- Deploy: Semaphore access (Phase 2, plan 17 D17-D20) ---
# The console's read-only Semaphore identity is provisioned by its own play (svc-console, guest), and
# deploy-console only READS the token it stores. These tests pin the properties that make the identity
# read-only and the probes honest. Structural (grep / YAML), like the rest of this file.

@test "console: compose binds config/workflow.json read-only at /etc/console/workflow.json" {
  assert_grep -qF '      - ./config/workflow.json:/etc/console/workflow.json:ro' "$DEPLOY_DIR/compose.yml"
}

@test "console: env carries the Semaphore URLs, project and token by reference, and the workflow path" {
  local f="$DEPLOY_DIR/templates/env.j2"
  assert_grep -qF 'CONSOLE_SEMAPHORE_API_URL={{ console_semaphore_api_url | default(' "$f"
  assert_grep -qF 'CONSOLE_SEMAPHORE_UI_URL={{ console_semaphore_ui_url | default(' "$f"
  assert_grep -qF 'CONSOLE_SEMAPHORE_PROJECT={{ console_semaphore_project | default(' "$f"
  # The token is a Jinja reference into the secrets namespace, never a literal.
  assert_grep -qxF 'CONSOLE_SEMAPHORE_TOKEN={{ secrets.semaphore_api_token }}' "$f"
  refute_grep -qE '^CONSOLE_SEMAPHORE_TOKEN=[^{]' "$f"
  assert_grep -qxF 'CONSOLE_WORKFLOW_FILE=/etc/console/workflow.json' "$f"
}

@test "console: workflow template renders the registry steps as id, order, owner, label, sorted by order" {
  local f="$DEPLOY_DIR/templates/workflow.json.j2"
  [ -f "$f" ]
  assert_grep -qF "sort(attribute='order')" "$f"
  assert_grep -qF "'label': step.diagram_label" "$f"
  assert_grep -qF "'owner': step.owner" "$f"
  assert_grep -qF '| to_nice_json' "$f"
}

@test "console: deploy playbook reads the Semaphore token as existing, never generates it, and has no URL-only workaround" {
  python3 - "$PLAYBOOK" <<'PY'
import sys, yaml
# Scoped to the Phase 1 play's vars: the play that declares _secret_definitions.
plays = [p for p in yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
         if isinstance(p, dict) and "_secret_definitions" in (p.get("vars") or {})]
assert len(plays) == 1, len(plays)
v = plays[0]["vars"]
assert {"name": "semaphore_api_token", "type": "existing"} in v["_secret_definitions"], v["_secret_definitions"]
assert v["_required_existing_secrets"] == ["semaphore_api_token"], v["_required_existing_secrets"]
generated = [e for e in v["_secret_definitions"]
             if e.get("name") == "semaphore_api_token" and e.get("type") in ("random", "django")]
assert not generated, generated
# The store step had nothing to merge before the token existed; _service_url is gone now.
assert "_service_url" not in v, sorted(v)
PY
}

@test "console: deploy playbook renders config/workflow.json from the service-onboarding registry on the controller" {
  assert_grep -qF 'src: workflow.json.j2, dest: config/workflow.json' "$PLAYBOOK"
  assert_grep -qF "playbook_dir ~ '/../workflows/service-onboarding/registry.yml'" "$PLAYBOOK"
  [ -f "$REPO_ROOT/platform/workflows/service-onboarding/registry.yml" ]
}

@test "console: deploy.sh refuses to start without config/workflow.json and hashes it into both up branches" {
  local f="$DEPLOY_DIR/deploy.sh"
  assert_grep -qF 'config/workflow.json not found. Run Ansible deploy-console.yml first.' "$f"
  # The local overlay branch and the base branch both pass the file as a recreate input.
  [ "$(grep -cF 'compose_up_if_changed config/scope.json config/workflow.json' "$f")" -eq 2 ]
}

@test "console: provision play is a separate localhost play that refuses extra vars setting internal names" {
  local p="$REPO_ROOT/platform/playbooks/provision-console-semaphore-access.yml"
  [ -f "$p" ]
  assert_grep -qF 'import_playbook: refuse-internal-extra-vars.yml' "$p"
  assert_grep -qE '^  hosts: localhost$' "$p"
}

@test "console: provision play guards the OpenBao transport before its first OpenBao request" {
  assert_guard_precedes_first_uri "$REPO_ROOT/platform/playbooks/provision-console-semaphore-access.yml"
}

@test "console: provision play talks only to the controller's loopback Semaphore API, override refused" {
  local p="$REPO_ROOT/platform/playbooks/provision-console-semaphore-access.yml"
  assert_grep -qF '_semaphore_url: "http://127.0.0.1:3000"' "$p"
  assert_grep -qF "(semaphore_url | default(_semaphore_url)) == 'http://127.0.0.1:3000'" "$p"
}

@test "console: svc-console is a non-admin, non-external account at project role guest" {
  local p="$REPO_ROOT/platform/playbooks/provision-console-semaphore-access.yml"
  python3 - "$p" <<'PY'
import sys, yaml
play = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))[1]
vars_ = play["vars"]
assert vars_["_svc_user"] == "svc-console", vars_["_svc_user"]
assert vars_["_svc_role"] == "guest", vars_["_svc_role"]
def tasks(seq):
    for t in seq or []:
        if isinstance(t, dict):
            yield t
            for sec in ("block", "rescue", "always"):
                yield from tasks(t.get(sec))
hits = [t for t in tasks(play["tasks"]) if t.get("name") == "Create the svc-console account (non-admin, not external: it logs in to mint its token)"]
assert len(hits) == 1, len(hits)
body = hits[0]["ansible.builtin.uri"]["body"]
assert body["admin"] is False and body["external"] is False, body
assert body["username"] == "{{ _svc_user }}", body
assert body["password"] == "{{ _svc_password }}", body
PY
}

@test "console: every header-bearing or password-bearing uri and every secret set_fact in the provision play is no_log" {
  local p="$REPO_ROOT/platform/playbooks/provision-console-semaphore-access.yml"
  python3 - "$p" <<'PY'
import sys, yaml
play = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))[1]
AUTH = ("Authorization", "X-Vault-Token", "Cookie")
SECRET_FACTS = ("_svc_password", "_minted_token", "_stored_token", "_stored_password", "_svc_token", "_bm_token")
def tasks(seq):
    for t in seq or []:
        if isinstance(t, dict):
            yield t
            for sec in ("block", "rescue", "always"):
                yield from tasks(t.get(sec))
bad = []
checked = 0
for t in tasks(play["tasks"]):
    name = t.get("name", "?")
    if "ansible.builtin.uri" in t:
        args = t["ansible.builtin.uri"]
        headers = args.get("headers") or {}
        if any(h in headers for h in AUTH) or "password" in str(args.get("body", "")):
            checked += 1
            if t.get("no_log") is not True:
                bad.append(name)
    if "ansible.builtin.set_fact" in t:
        text = str(t["ansible.builtin.set_fact"])
        if any(v in text for v in SECRET_FACTS):
            checked += 1
            if t.get("no_log") is not True:
                bad.append(name)
assert checked >= 8, checked
assert not bad, bad
PY
}

@test "console: the token mint is POST-only, and no task lists tokens to reuse one" {
  local p="$REPO_ROOT/platform/playbooks/provision-console-semaphore-access.yml"
  python3 - "$p" <<'PY'
import sys, yaml
play = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))[1]
def tasks(seq):
    for t in seq or []:
        if isinstance(t, dict):
            yield t
            for sec in ("block", "rescue", "always"):
                yield from tasks(t.get(sec))
token_calls = [t["ansible.builtin.uri"] for t in tasks(play["tasks"])
               if "ansible.builtin.uri" in t and "api/user/tokens" in str(t["ansible.builtin.uri"].get("url", ""))]
assert token_calls, "no token call"
for c in token_calls:
    # Listing ids is refused: GET /api/user/tokens cuts each id to 8 characters (api/user.go:62).
    assert c.get("method") == "POST", c
PY
}

@test "console: the read-only proof reads tasks/last (200) and proves refused launch with template -1 (403)" {
  local p="$REPO_ROOT/platform/playbooks/provision-console-semaphore-access.yml"
  python3 - "$p" <<'PY'
import sys, yaml
play = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))[1]
def tasks(seq):
    for t in seq or []:
        if isinstance(t, dict):
            yield t
            for sec in ("block", "rescue", "always"):
                yield from tasks(t.get(sec))
all_tasks = list(tasks(play["tasks"]))
def one(name):
    hits = [t for t in all_tasks if t.get("name") == name]
    assert len(hits) == 1, (name, len(hits))
    return hits[0]
def conditions(t):
    x = t["ansible.builtin.assert"]["that"]
    return x if isinstance(x, list) else [x]
# Each status is checked by its own assert task, scoped by name.
read = one("Require the token to read the project's tasks (200)")
assert any("_probe_read.status == 200" in c for c in conditions(read)), conditions(read)
refused = one("Require the launch probe to answer 403 (read-only: guest cannot run tasks)")
assert any("_probe_launch.status == 403" in c for c in conditions(refused)), conditions(refused)
# The launch probe sends template -1, and only that task does.
launch = one("Probe the token cannot launch: POST /api/project/{p}/tasks with template -1")
assert launch["ansible.builtin.uri"]["body"]["template_id"] == -1, launch["ansible.builtin.uri"]["body"]
hits = [t for t in all_tasks if "ansible.builtin.uri" in t
        and t["ansible.builtin.uri"].get("method") == "POST"
        and str(t["ansible.builtin.uri"].get("url", "")).endswith("/tasks")]
assert len(hits) == 1, len(hits)
# A write attempt, even a refused one, is never sent under --check.
assert "not ansible_check_mode" in str(hits[0].get("when")), hits[0].get("when")
PY
}

@test "console: a stored token is reused only when GET /api/user proves svc-console, non-admin" {
  local p="$REPO_ROOT/platform/playbooks/provision-console-semaphore-access.yml"
  python3 - "$p" <<'PY'
import sys, yaml
play = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))[1]
def tasks(seq):
    for t in seq or []:
        if isinstance(t, dict):
            yield t
            for sec in ("block", "rescue", "always"):
                yield from tasks(t.get(sec))
hits = [t for t in tasks(play["tasks"])
        if t.get("name") == "Hold whether the stored token is the svc-console token"]
assert len(hits) == 1, len(hits)
# Scoped to the set_fact that decides the reuse: the value is a folded string, and each check sits on one source line.
expr = str(hits[0]["ansible.builtin.set_fact"]["_stored_ok"])
assert "(_stored_proof.json | default({})).get('username', '') == _svc_user" in expr, expr
assert "(_stored_proof.json | default({})).get('admin', true) | bool) == false" in expr, expr
PY
}

@test "console: the provision template is registered as a Dev twin in templates.yml and as a Local one" {
  python3 - "$REPO_ROOT/platform/semaphore/templates.yml" "$REPO_ROOT/platform/semaphore/templates-local.yml" <<'PY'
import sys, yaml
shared = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
local = yaml.safe_load(open(sys.argv[2], encoding="utf-8"))
def items(doc):
    return doc["templates"] if isinstance(doc, dict) else doc
play = "platform/playbooks/provision-console-semaphore-access.yml"
hit = [t for t in items(shared) if t.get("playbook") == play]
assert len(hit) == 1, hit
assert hit[0].get("dev_variant") is True, hit[0]
loc = [t for t in items(local) if t.get("playbook") == play]
assert len(loc) == 1, loc
PY
}

@test "console: the provision play is listed in AGENTS.md Independent Workflows" {
  assert_grep -qF '| Provision Console Semaphore Access | `provision-console-semaphore-access.yml` |' "$REPO_ROOT/AGENTS.md"
}

@test "console: local inventory and bootstrap declare the console's Semaphore URLs and project" {
  assert_grep -qF 'console_semaphore_api_url: https://semaphore.agent-cloud.test' "$REPO_ROOT/platform/inventory/local-dev.yml.example"
  assert_grep -qF 'console_semaphore_project: 1' "$REPO_ROOT/platform/inventory/local-dev.yml.example"
  assert_grep -qF 'console_semaphore_ui_url=https://semaphore.{{ _dev_zone }}:{{ _edge_https_port }}' "$REPO_ROOT/platform/playbooks/bootstrap-local-dev.yml"
  assert_grep -qF 'console_semaphore_project=1' "$REPO_ROOT/platform/playbooks/bootstrap-local-dev.yml"
}

# --- Review of PR #747: production guard, and the project the provision play binds ---

# Write a runnable play to $1: the play of $2 whose vars hold key $3, with only the tasks named in $5.. (kept in
# playbook order, each exactly once), run against hosts $4. The play's own vars are copied, so the kept tasks
# evaluate as they do in the real playbook. A debug task prints _pid, so the caller can read the resolved project.
_wrap_play() {
  python3 - "$@" <<'PY'
import sys, yaml
out, pb, key, hosts, *names = sys.argv[1:]
plays = [p for p in yaml.safe_load(open(pb, encoding="utf-8"))
         if isinstance(p, dict) and key in (p.get("vars") or {})]
assert len(plays) == 1, len(plays)
play = plays[0]
kept = [t for t in play["tasks"] if isinstance(t, dict) and t.get("name") in names]
assert sorted(t["name"] for t in kept) == sorted(names), [t.get("name") for t in kept]
kept.append({"name": "Show the resolved project",
             "ansible.builtin.debug": {"msg": "PID={{ _pid | default('unset') }}"}})
wrap = {"hosts": hosts, "connection": "local", "gather_facts": False, "vars": play["vars"], "tasks": kept}
with open(out, "w", encoding="utf-8") as f:
    yaml.safe_dump([wrap], f, sort_keys=False)
PY
}

@test "console: production guard is one assert, first in Phase 1, skipped under local_mode, naming each console_svc var" {
  python3 - "$PLAYBOOK" <<'PY'
import sys, yaml
plays = [p for p in yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
         if isinstance(p, dict) and "_console_prod_required" in (p.get("vars") or {})]
assert len(plays) == 1, len(plays)
play = plays[0]
# The five host vars a production console cannot run without.
want = ["console_base_url", "console_oidc_issuer", "console_semaphore_api_url",
        "console_semaphore_ui_url", "console_semaphore_project"]
assert sorted(play["vars"]["_console_prod_required"]) == sorted(want), play["vars"]["_console_prod_required"]
tasks = [t for t in play["tasks"] if isinstance(t, dict)]
names = [t.get("name") for t in tasks]
guard_name = "Production guard: the console's host vars are set and not local-dev defaults"
assert names.count(guard_name) == 1, names
guard = tasks[names.index(guard_name)]
assert "ansible.builtin.assert" in guard, sorted(guard)
assert guard.get("when") == "not (local_mode | default(false) | bool)", guard.get("when")
assert guard.get("loop") == "{{ _console_prod_required }}", guard.get("loop")
assert "{{ item }}" in guard["ansible.builtin.assert"]["fail_msg"], guard["ansible.builtin.assert"]["fail_msg"]
conditions = guard["ansible.builtin.assert"]["that"]
assert any("'agent-cloud.test' not in" in c for c in conditions), conditions
assert any("length > 0" in c for c in conditions), conditions
# First task of the play: before the repo is placed and before any secret is written.
assert names.index(guard_name) == 0, names[:3]
PY
}

@test "console: production guard refuses an agent-cloud.test value and names it; local_mode skips it" {
  command -v ansible-playbook >/dev/null 2>&1 || skip "ansible-playbook not available"
  local wrap="$BATS_TEST_TMPDIR/guard.yml" inv="$BATS_TEST_TMPDIR/hosts.ini"
  _wrap_play "$wrap" "$PLAYBOOK" _console_prod_required console_svc \
    "Production guard: the console's host vars are set and not local-dev defaults"
  printf '[console_svc]\nconsole-test ansible_connection=local console_base_url=https://console.agent-cloud.test:8443 console_oidc_issuer=https://auth.example.org/application/o/console/ console_semaphore_api_url=https://semaphore.example.org console_semaphore_ui_url=https://semaphore.example.org:8443 console_semaphore_project=7\n' > "$inv"
  run ansible-playbook -i "$inv" "$wrap" -e local_mode=false
  [ "$status" -ne 0 ]
  assert_contains "$output" "console_base_url"
  run ansible-playbook -i "$inv" "$wrap" -e local_mode=true
  [ "$status" -eq 0 ]
}

@test "console: production guard passes when every var is set, and refuses a console_svc with no Semaphore project" {
  command -v ansible-playbook >/dev/null 2>&1 || skip "ansible-playbook not available"
  local wrap="$BATS_TEST_TMPDIR/guard.yml" inv="$BATS_TEST_TMPDIR/hosts.ini"
  _wrap_play "$wrap" "$PLAYBOOK" _console_prod_required console_svc \
    "Production guard: the console's host vars are set and not local-dev defaults"
  printf '[console_svc]\nconsole-test ansible_connection=local console_base_url=https://console.example.org console_oidc_issuer=https://auth.example.org/application/o/console/ console_semaphore_api_url=https://semaphore.example.org console_semaphore_ui_url=https://semaphore.example.org:8443 console_semaphore_project=7\n' > "$inv"
  run ansible-playbook -i "$inv" "$wrap" -e local_mode=false
  [ "$status" -eq 0 ]
  printf '[console_svc]\nconsole-test ansible_connection=local console_base_url=https://console.example.org console_oidc_issuer=https://auth.example.org/application/o/console/ console_semaphore_api_url=https://semaphore.example.org console_semaphore_ui_url=https://semaphore.example.org:8443\n' > "$inv"
  run ansible-playbook -i "$inv" "$wrap" -e local_mode=false
  [ "$status" -ne 0 ]
  assert_contains "$output" "console_semaphore_project"
}

@test "console: provision play resolves the project before its first Semaphore or OpenBao request" {
  local p="$REPO_ROOT/platform/playbooks/provision-console-semaphore-access.yml"
  python3 - "$p" <<'PY'
import sys, yaml
play = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))[1]
# _pid is no longer a play var: the resolve task is its only source.
assert "_pid" not in play["vars"], sorted(play["vars"])
names = [t.get("name") for t in play["tasks"] if isinstance(t, dict)]
refuse = names.index("Refuse a production run with no declared Semaphore project for the console")
resolve = names.index("Resolve the Semaphore project the console is bound to")
first_uri = [i for i, t in enumerate(play["tasks"])
             if isinstance(t, dict) and "ansible.builtin.uri" in t][0]
assert refuse < resolve < first_uri, (refuse, resolve, first_uri)
guard = play["tasks"][refuse]
assert guard.get("when") == "not (local_mode | default(false) | bool)", guard.get("when")
assert play["tasks"][resolve]["ansible.builtin.set_fact"]["_pid"], play["tasks"][resolve]
PY
}

@test "console: provision play reads the project the console_svc host declares when no extra var is passed" {
  command -v ansible-playbook >/dev/null 2>&1 || skip "ansible-playbook not available"
  local p="$REPO_ROOT/platform/playbooks/provision-console-semaphore-access.yml"
  local wrap="$BATS_TEST_TMPDIR/provision.yml" inv="$BATS_TEST_TMPDIR/hosts.ini"
  _wrap_play "$wrap" "$p" _console_svc_project localhost \
    "Refuse a production run with no declared Semaphore project for the console" \
    "Resolve the Semaphore project the console is bound to"
  printf 'localhost ansible_connection=local\n[console_svc]\nconsole-test ansible_connection=local console_semaphore_project=7\n' > "$inv"
  run ansible-playbook -i "$inv" "$wrap" -e local_mode=false
  [ "$status" -eq 0 ]
  assert_contains "$output" "PID=7"
  refute_contains "$output" "PID=unset"
}

@test "console: provision play falls back to the extra var only when no console_svc host declares a project" {
  command -v ansible-playbook >/dev/null 2>&1 || skip "ansible-playbook not available"
  local p="$REPO_ROOT/platform/playbooks/provision-console-semaphore-access.yml"
  local wrap="$BATS_TEST_TMPDIR/provision.yml" inv="$BATS_TEST_TMPDIR/hosts.ini"
  _wrap_play "$wrap" "$p" _console_svc_project localhost \
    "Refuse a production run with no declared Semaphore project for the console" \
    "Resolve the Semaphore project the console is bound to"
  # A console_svc host with no project, and a production run: the extra var is the only declaration.
  printf 'localhost ansible_connection=local\n[console_svc]\nconsole-test ansible_connection=local\n' > "$inv"
  run ansible-playbook -i "$inv" "$wrap" -e local_mode=false -e console_semaphore_project=9
  [ "$status" -eq 0 ]
  assert_contains "$output" "PID=9"
  # Neither declared, production: refused before any login, never bound to project 1.
  run ansible-playbook -i "$inv" "$wrap" -e local_mode=false
  [ "$status" -ne 0 ]
  assert_contains "$output" "No Semaphore project is declared"
  # Local-dev with nothing declared keeps the local default, project 1.
  printf 'localhost ansible_connection=local\n' > "$inv"
  run ansible-playbook -i "$inv" "$wrap" -e local_mode=true
  [ "$status" -eq 0 ]
  assert_contains "$output" "PID=1"
}
