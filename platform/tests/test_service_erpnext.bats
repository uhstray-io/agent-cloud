#!/usr/bin/env bats
# Structural tests for the composable ERPNext service (platform/services/erpnext).
# Verifies the composable shape: env-parameterized compose with pinned,
# fully-qualified images, container-only deploy.sh (no secret generation), an
# env template that pulls every credential from OpenBao ({{ secrets.* }}, no
# literals), the slim compose.local.yml overlay, and a composable (not legacy)
# deploy playbook. Slim tier: single `queue` worker, no MinIO.
#
# Run: bats platform/tests/test_service_erpnext.bats

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  DEPLOY_DIR="$REPO_ROOT/platform/services/erpnext/deployment"
  PB_DIR="$REPO_ROOT/platform/playbooks"
}

@test "erpnext: compose has the expected top-level shape" {
  # grep-based (portable — no PyYAML dependency); yamllint gates YAML validity.
  local f="$DEPLOY_DIR/compose.yml"
  [ -f "$f" ]
  grep -qE '^name: erpnext' "$f"
  grep -qE '^services:' "$f"
  grep -qE '^\s+frontend:' "$f"
}

@test "erpnext: compose env-parameterizes images + frontend bind/port" {
  local f="$DEPLOY_DIR/compose.yml"
  grep -qE '\$\{ERPNEXT_IMAGE' "$f"
  grep -qE '\$\{ERPNEXT_VERSION' "$f"
  grep -qE '\$\{ERPNEXT_BIND' "$f"
  grep -qE '\$\{ERPNEXT_PORT' "$f"
}

@test "erpnext: images are fully-qualified and pinned (no :latest drift)" {
  local f="$DEPLOY_DIR/compose.yml"
  # frappe/erpnext pinned via the required ERPNEXT_VERSION (.env), backing
  # services pinned inline with fully-qualified docker.io/ refs.
  grep -qE '\$\{ERPNEXT_IMAGE:-docker\.io/frappe/erpnext\}:\$\{ERPNEXT_VERSION' "$f"
  grep -qE 'docker\.io/library/mariadb:10\.6' "$f"
  grep -qE 'docker\.io/library/redis:7-alpine' "$f"
  ! grep -qE ':latest' "$f"
}

@test "erpnext: compose defines the slim-tier services with explicit container_name" {
  local f="$DEPLOY_DIR/compose.yml"
  for svc in db redis-cache redis-queue configurator backend frontend websocket queue scheduler; do
    grep -q "container_name: erpnext-${svc}" "$f"
  done
}

@test "erpnext: slim tier excludes prod-only MinIO / split workers" {
  local f="$DEPLOY_DIR/compose.yml"
  ! grep -q 'container_name: erpnext-minio' "$f"
  ! grep -q 'container_name: erpnext-queue-short' "$f"
  ! grep -q 'container_name: erpnext-queue-long' "$f"
}

@test "erpnext: compose has no hardcoded credentials" {
  ! grep -E 'PASSWORD: [A-Za-z0-9]{8,}' "$DEPLOY_DIR/compose.yml"
}

@test "erpnext: compose has no RFC1918 IPs" {
  ! grep -E '192\.168\.|10\.[0-9]+\.|172\.(1[6-9]|2[0-9]|3[01])\.' "$DEPLOY_DIR/compose.yml"
}

@test "erpnext: deploy.sh is executable, bash, sources common.sh, uses compose, no secrets" {
  local f="$DEPLOY_DIR/deploy.sh"
  [ -f "$f" ] && [ -x "$f" ]
  head -1 "$f" | grep -qE '^#!/usr/bin/env bash'
  grep -q 'common.sh' "$f"
  grep -qE '\bcompose (pull|up|run)' "$f"
  ! grep -qE '\b(gen_secret|put_secret|get_secret|bao_|BAO_)' "$f"
}

@test "erpnext: deploy.sh never hardcodes a container engine" {
  ! grep -E '^\s*(docker|podman) ' "$DEPLOY_DIR/deploy.sh"
}

@test "erpnext: post-deploy.sh is executable, bash, idempotent site bootstrap, reads .env only" {
  local f="$DEPLOY_DIR/post-deploy.sh"
  [ -f "$f" ] && [ -x "$f" ]
  head -1 "$f" | grep -qE '^#!/usr/bin/env bash'
  grep -q 'bench new-site' "$f"
  grep -qE 'test -d sites/' "$f"
  ! grep -qE '\b(gen_secret|put_secret|get_secret|bao_|BAO_)' "$f"
}

@test "erpnext: env template pulls every credential from OpenBao, no literals" {
  local f="$DEPLOY_DIR/templates/env.j2"
  [ -f "$f" ]
  grep -qF 'DB_PASSWORD={{ secrets.mariadb_root_password }}' "$f"
  grep -qF 'ADMIN_PASSWORD={{ secrets.admin_password }}' "$f"
  # Guard against credential literals in *value* position (an assigned secret),
  # not prose mentions of LOCAL_FAKE_ in the doc comment header.
  ! grep -qiE '=[^{]*LOCAL_FAKE|password=[A-Za-z0-9]{8}' "$f"
}

@test "erpnext: env template uses approved Jinja2 namespaces only" {
  run grep -oE '\{\{ *[a-z_.]+' "$DEPLOY_DIR/templates/env.j2"
  for var in $output; do
    cleaned="${var#\{\{ }"
    [[ "$cleaned" =~ ^(secrets\.|erpnext_|ansible_) ]]
  done
}

@test "erpnext: local overlay adds caps/SELinux/local-dev but does NOT republish ports" {
  local f="$DEPLOY_DIR/compose.local.yml"
  [ -f "$f" ]
  grep -q 'mem_limit:' "$f"
  grep -q 'label=disable' "$f"
  grep -q 'local-dev' "$f"
  ! grep -qE '^[[:space:]]*ports:' "$f"
}

@test "erpnext: local overlay puts the frontend on local-dev so Caddy reaches it by name" {
  # the frontend service block must list local-dev under its networks (grep -A
  # over the block — portable, no PyYAML).
  local f="$DEPLOY_DIR/compose.local.yml"
  grep -A6 '^\s\+frontend:' "$f" | grep -q 'local-dev'
}

@test "erpnext: deploy playbook is composable (place-monorepo + manage-secrets), not legacy" {
  local f="$PB_DIR/deploy-erpnext.yml"
  [ -f "$f" ]
  grep -q 'tasks/place-monorepo.yml' "$f"
  grep -q 'tasks/manage-secrets.yml' "$f"
  grep -q 'bash deploy.sh' "$f"
  grep -q 'bash post-deploy.sh' "$f"
  ! grep -q 'import_playbook: deploy-service.yml' "$f"
  grep -q 'name: mariadb_root_password' "$f"
}

@test "erpnext: clean-deploy playbook destroys then redeploys (local greenfield)" {
  local f="$PB_DIR/clean-deploy-erpnext.yml"
  [ -f "$f" ]
  grep -q 'tasks/clean-service.yml' "$f"
  grep -q 'import_playbook: deploy-erpnext.yml' "$f"
  grep -qi 'DANGER' "$f"
}

@test "erpnext: Social Login Key sets custom_base_url (else Frappe ignores base_url)" {
  # Without custom_base_url, Frappe resolves the relative authorize/token paths
  # against the SITE url (erp.<zone>) instead of base_url (the IdP) -> 404.
  local f="$REPO_ROOT/platform/services/erpnext/deployment/post-deploy.sh"
  grep -q 'doc.custom_base_url = 1' "$f"
}

@test "erpnext: post-deploy pre-provisions the OIDC admin (else signup-disabled 403)" {
  # Frappe matches the OIDC identity by email; if absent it self-registers, which
  # Website Settings disables -> 403. Pre-create the SSO admin as a System Manager
  # so login binds to it (signup stays disabled). Email must match Authentik.
  local f="$REPO_ROOT/platform/services/erpnext/deployment/post-deploy.sh"
  grep -q 'step_oidc_admin' "$f"
  grep -q 'ERPNEXT_OIDC_ADMIN_EMAIL' "$f"
  # full access = union of all assignable roles (System Manager alone is admin-only)
  grep -q 'user.add_roles(\*roles)' "$f"
  grep -q '"Administrator", "All", "Guest"' "$f"
  # the email is templated to match the Authentik agent-cloud-admin identity
  grep -q 'ERPNEXT_OIDC_ADMIN_EMAIL=' "$REPO_ROOT/platform/services/erpnext/deployment/templates/env.j2"
}
