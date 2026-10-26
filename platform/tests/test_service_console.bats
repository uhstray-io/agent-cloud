#!/usr/bin/env bats
# Structural tests for the Dispatch Console (platform/services/console). Grouped by
# owner: the Authentik side (the OIDC client declared as config-as-code: secret
# declaration, worker env line, app-catalog entry, blueprint) comes first; the
# console's own deploy-side tests are appended below their own marker.
#
# Structural only (grep / YAML asserts) — no live Authentik. Assertions use the
# helpers in assert_helpers.bash (refute_grep / assert_grep), never `! grep` or
# `|| true`, so each one can actually fail (CONTRIBUTING.md "Writing BATS Tests").
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

@test "console: healthcheck is the exec-form probe (distroless image has no shell)" {
  local f="$DEPLOY_DIR/compose.yml"
  grep -qF 'test: ["CMD", "/usr/local/app/server", "probe", "/healthz", "200"]' "$f"
  ! grep -qE '^[[:space:]]*test:.*CMD-SHELL' "$f"
}

@test "console: compose has no hardcoded credentials" {
  local f="$DEPLOY_DIR/compose.yml"
  ! grep -qiE '(password|secret|token|api_key)[[:space:]]*[:=][[:space:]]*["'\''0-9A-Za-z]{8}' "$f"
}

@test "console: compose has no RFC1918 IPs" {
  local f="$DEPLOY_DIR/compose.yml"
  ! grep -qE '192\.168\.|10\.[0-9]+\.|172\.(1[6-9]|2[0-9]|3[01])\.' "$f"
}

@test "console: deploy.sh is executable bash, sources common.sh, uses compose_up_if_changed, no secrets" {
  local f="$DEPLOY_DIR/deploy.sh"
  [ -f "$f" ] && [ -x "$f" ]
  head -1 "$f" | grep -qE '^#!/usr/bin/env bash'
  grep -q 'common.sh' "$f"
  grep -qE 'compose_up_if_changed' "$f"
  grep -q 'wait_for_healthy console' "$f"
  # Lifecycle plus a host build only: no secret generation, no OpenBao interaction (Deploy Rule #2).
  ! grep -qiE 'openssl rand|secret_id|vault|\bbao |gen_secret|put_secret|get_secret' "$f"
}

@test "console: deploy.sh never forces a recreate on every run" {
  # Comment lines may name the flag; only a live use counts.
  ! grep -v '^[[:space:]]*#' "$DEPLOY_DIR/deploy.sh" | grep -q -- '--force-recreate'
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
  ! grep -v '^[[:space:]]*#' "$DEPLOY_DIR/deploy.sh" | grep -qE '^[[:space:]]*(\.|source) .*\.env'
  ! grep -v '^[[:space:]]*#' "$DEPLOY_DIR/deploy.sh" | grep -qE 'set -a'
}

@test "console: env template is non-secret config plus the shared OIDC secret, no literals" {
  local f="$DEPLOY_DIR/templates/env.j2"
  [ -f "$f" ]
  # The client secret is a Jinja ref into the secrets.* namespace, never a literal.
  grep -qF 'CONSOLE_OIDC_CLIENT_SECRET={{ secrets.console_oidc_client_secret }}' "$f"
  grep -qF 'CONSOLE_OIDC_CLIENT_ID=console' "$f"
  grep -qF 'CONSOLE_SCOPE_FILE=/etc/console/scope.json' "$f"
  ! grep -qE '192\.168\.|10\.[0-9]+\.' "$f"
  ! grep -qiE '(password|secret|token|api_key)[[:space:]]*[:=][[:space:]]*[A-Za-z0-9]{8,}' "$f"
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
