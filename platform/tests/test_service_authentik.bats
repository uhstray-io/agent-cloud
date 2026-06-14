#!/usr/bin/env bats
# Structural tests for the Authentik IdP service (platform/services/authentik).
# Verifies the composable shape: env-parameterized compose, four-service stack,
# `ak healthcheck`, container-only deploy.sh (no secret generation), env.j2
# serves plain HTTP behind Caddy + shares the DB password, an overlay-safe
# local profile, and a valid seed blueprint (config-as-code).
#
# Run: bats platform/tests/test_service_authentik.bats

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  DEPLOY_DIR="$REPO_ROOT/platform/services/authentik/deployment"
}

@test "authentik: compose env-parameterizes image + bind/port" {
  local f="$DEPLOY_DIR/compose.yml"
  [ -f "$f" ]
  grep -qE '\$\{AUTHENTIK_IMAGE' "$f"
  grep -qE '\$\{AUTHENTIK_BIND' "$f"
  grep -qE '\$\{AUTHENTIK_PORT' "$f"
}

@test "authentik: prod default image is a pinned upstream tag (no :latest drift)" {
  local f="$DEPLOY_DIR/compose.yml"
  grep -qE '\$\{AUTHENTIK_IMAGE:-ghcr\.io/goauthentik/server:[0-9]{4}\.[0-9]+\.[0-9]+\}' "$f"
}

@test "authentik: four-service stack (server + worker + postgres + redis)" {
  local f="$DEPLOY_DIR/compose.yml"
  grep -qE '^\s+server:' "$f"
  grep -qE '^\s+worker:' "$f"
  grep -qE '^\s+postgresql:' "$f"
  grep -qE '^\s+redis:' "$f"
}

@test "authentik: healthcheck uses ak healthcheck" {
  local f="$DEPLOY_DIR/compose.yml"
  grep -q 'healthcheck:' "$f"
  grep -q '"ak", "healthcheck"' "$f"
}

@test "authentik: deploy.sh is executable, bash, sources common.sh, uses compose, no secrets" {
  local f="$DEPLOY_DIR/deploy.sh"
  [ -f "$f" ] && [ -x "$f" ]
  head -1 "$f" | grep -qE '^#!/usr/bin/env bash'
  grep -q 'common.sh' "$f"
  grep -qE '\bcompose (pull|up)' "$f"
  ! grep -qE '\b(gen_secret|put_secret|get_secret|bao_)' "$f"
}

@test "authentik: env template serves HTTP behind Caddy, shares the DB password, secrets from OpenBao" {
  local f="$DEPLOY_DIR/templates/env.j2"
  [ -f "$f" ]
  grep -qE 'AUTHENTIK_LISTEN__HTTP=0\.0\.0\.0:9000' "$f"
  grep -qF 'AUTHENTIK_SECRET_KEY={{ secrets.secret_key }}' "$f"
  # postgres container password and authentik's DB password are the SAME secret.
  grep -qF 'AUTHENTIK_POSTGRES_PASSWORD={{ secrets.db_password }}' "$f"
  grep -qF 'AUTHENTIK_POSTGRESQL__PASSWORD={{ secrets.db_password }}' "$f"
}

@test "authentik: local overlay adds caps/SELinux/local-dev but does NOT republish ports" {
  local f="$DEPLOY_DIR/compose.local.yml"
  [ -f "$f" ]
  grep -q 'mem_limit:' "$f"
  grep -q 'label=disable' "$f"
  grep -q 'local-dev' "$f"
  # Ports are env-param in the base; an overlay ports list would APPEND.
  ! grep -qE '^[[:space:]]*ports:' "$f"
}

@test "authentik: seed blueprint is a valid config-as-code blueprint" {
  local f="$DEPLOY_DIR/blueprints/agent-cloud.yaml"
  [ -f "$f" ]
  grep -qE '^version: 1' "$f"
  grep -qE '^entries:' "$f"
  grep -q 'authentik_core.group' "$f"
}

@test "authentik: platform RBAC groups are config-as-code (admins is_superuser)" {
  local f="$DEPLOY_DIR/blueprints/platform-groups.yaml"
  [ -f "$f" ]
  for g in platform-admins platform-developers platform-user; do grep -q "name: $g" "$f"; done
  # admins carries is_superuser: true (portable: check the lines after its name).
  grep -A3 'name: platform-admins' "$f" | grep -q 'is_superuser: true'
}

@test "authentik: netbox + openbao are forward_auth proxy providers (no client secret)" {
  for svc in netbox openbao; do
    local f="$DEPLOY_DIR/blueprints/${svc}-forward-auth.yaml"
    [ -f "$f" ]
    grep -q 'authentik_providers_proxy.proxyprovider' "$f"
    grep -q 'mode: forward_single' "$f"
    ! grep -qi 'client_secret' "$f"
  done
}

@test "authentik: shared bindings own the outpost (both providers) + the access gate" {
  local f="$DEPLOY_DIR/blueprints/zz-sso-bindings.yaml"
  [ -f "$f" ]
  # zz- so it applies last (resolves !Find against already-created providers).
  grep -q 'authentik_outposts.outpost' "$f"
  grep -q '\[name, netbox\]' "$f"
  grep -q '\[name, openbao\]' "$f"
  # gate's allowed set has admins+developers (quoted), NOT the no-access tier;
  # superuser break-glass. (platform-user may appear in a comment, so match the
  # quoted set membership, not bare presence.)
  grep -q '"platform-admins"' "$f" && grep -q '"platform-developers"' "$f"
  ! grep -q '"platform-user"' "$f"
  grep -q 'is_superuser' "$f"
  grep -q 'authentik_policies.policybinding' "$f"
}
