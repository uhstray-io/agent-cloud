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
