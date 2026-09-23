#!/usr/bin/env bash
# deploy.sh — Deploy Semaphore with programmatic API token creation
# Idempotent: safe to re-run on an existing deployment.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$(dirname "$(dirname "$(dirname "$SCRIPT_DIR")")")/lib"
source "${LIB_DIR}/common.sh"
source "${LIB_DIR}/bao-client.sh"

SECRETS_DIR="${SCRIPT_DIR}/secrets"
CONFIG_DIR="${SCRIPT_DIR}/config"
SEMAPHORE_URL="${SEMAPHORE_URL:-http://localhost:${SEMAPHORE_PORT:-3000}}"
OPENBAO_ADDR="${OPENBAO_ADDR:-http://127.0.0.1:8200}"

# ── Step 1: Generate secrets & env file ───────────────────────────────────────

step_generate_secrets() {
  info "Step 1: Generating Semaphore secrets..."
  mkdir -p "$SECRETS_DIR"
  generate_semaphore_env "${CONFIG_DIR}/semaphore.env" "$SECRETS_DIR"
}

# ── Step 2: Start services ────────────────────────────────────────────────────

# The image this deploy would start: SEMAPHORE_IMAGE, else the pin compose.yml declares.
pinned_image() {
  local default
  default=$(sed -nE 's/.*image: \$\{SEMAPHORE_IMAGE:-([^}]+)\}.*/\1/p' "${SCRIPT_DIR}/compose.yml" | head -n 1)
  printf '%s\n' "${SEMAPHORE_IMAGE:-$default}"
}

# assert_no_downgrade — refuse to start a pinned Semaphore older than the existing controller.
# Semaphore migrates its database forward at start and has no downward migration, so an
# older binary over a newer schema is a broken controller. The pin in compose.yml was chosen
# without reading production's version (PR 203 review; change task 0.3), so the deploy reads
# it: `semaphore version` in the running container prints e.g. v2.18.12^0-8a4dcf0-1780941924;
# a stopped container still names the image it was created from. An existing controller whose
# version neither answers is refused, not waved through (PR 203 Codex review). A pin that is
# not a version (latest) or no controller container has nothing to compare.
# ponytail: reads the container, not the database; a data volume whose container was removed
# is not compared. Read the schema's migration table if that case needs covering.
assert_no_downgrade() {
  local running="" pinned image=""
  pinned=$(pinned_image | sed -nE 's/.*:v?([0-9]+\.[0-9]+\.[0-9]+).*/\1/p')
  [ -n "$pinned" ] || return 0
  $CONTAINER_ENGINE container inspect workflow-semaphore >/dev/null 2>&1 || return 0
  running=$($CONTAINER_ENGINE exec workflow-semaphore semaphore version 2>/dev/null \
    | grep -oE '^v?[0-9]+\.[0-9]+\.[0-9]+' | tr -d v) || running=""
  if [ -z "$running" ]; then
    image=$($CONTAINER_ENGINE container inspect --format '{{.Config.Image}}' workflow-semaphore 2>/dev/null) || image=""
    running=$(printf '%s\n' "$image" | sed -nE 's/.*:v?([0-9]+\.[0-9]+\.[0-9]+).*/\1/p')
  fi
  [ -n "$running" ] || error "Refusing to deploy Semaphore: workflow-semaphore exists but its version cannot be read (not answering, image '${image:-unknown}' has no version tag). Start it so 'semaphore version' answers, or remove it deliberately."
  if [ "$(printf '%s\n%s\n' "$pinned" "$running" | sort -V | head -n 1)" != "$running" ]; then
    error "Refusing to downgrade Semaphore: v${running} is running, the pin is v${pinned}. Raise the pin in compose.yml (or SEMAPHORE_IMAGE) to v${running} or later."
  fi
  info "  Semaphore version check: existing v${running}, pin v${pinned}."
}

step_start_services() {
  info "Step 2: Starting Semaphore services..."
  cd "$SCRIPT_DIR"
  assert_no_downgrade
  compose up -d
  wait_for_http "${SEMAPHORE_URL}/api/ping" "Semaphore" 120
}

# ── Step 3: Bootstrap API token ───────────────────────────────────────────────

step_bootstrap_credentials() {
  info "Step 3: Bootstrapping Semaphore credentials..."
  local api_token admin_pass
  api_token=$(get_secret "$SECRETS_DIR" semaphore_api_token)
  if ! needs_gen "$api_token"; then
    info "  API token already exists — skipping bootstrap."
    return 0
  fi

  admin_pass=$(get_secret "$SECRETS_DIR" semaphore_admin_password)
  if needs_gen "$admin_pass"; then
    warn "  No admin password found — skipping. Run deploy again after config generation."
    return 0
  fi

  # Login to get session cookie
  local cookie_jar _login_response
  cookie_jar=$(mktemp)
  trap 'rm -f "$cookie_jar"' EXIT INT TERM

  local login_payload
  login_payload=$(jq -n --arg pass "$admin_pass" '{"auth":"admin","password":$pass}')
  _login_response=$(curl -sf -c "$cookie_jar" -X POST "${SEMAPHORE_URL}/api/auth/login" \
    -H "Content-Type: application/json" \
    --data-raw "$login_payload" 2>/dev/null) || {
    warn "  Login failed — API token creation deferred."
    return 0
  }
  info "  Logged in."

  # Check if token already exists
  local existing_tokens
  existing_tokens=$(curl -sf -b "$cookie_jar" "${SEMAPHORE_URL}/api/user/tokens" 2>/dev/null) || existing_tokens="[]"
  local token_count
  token_count=$(echo "$existing_tokens" | jq 'length' 2>/dev/null) || token_count=0

  if [ "$token_count" -gt 0 ]; then
    api_token=$(echo "$existing_tokens" | jq -r '.[0].id' 2>/dev/null) || api_token=""
    if [ -n "$api_token" ]; then
      put_secret "$SECRETS_DIR" semaphore_api_token "$api_token"
      info "  Reusing existing API token."
      return 0
    fi
  fi

  # Create new token
  local token_response
  token_response=$(curl -sf -b "$cookie_jar" -X POST "${SEMAPHORE_URL}/api/user/tokens" \
    -H "Content-Type: application/json" 2>/dev/null) || true

  api_token=$(echo "${token_response:-}" | jq -r '.id // empty' 2>/dev/null) || api_token=""

  if [ -n "$api_token" ]; then
    put_secret "$SECRETS_DIR" semaphore_api_token "$api_token"
    info "  API token created and saved."
  else
    warn "  API token creation failed — may need manual creation."
  fi
}

# ── Step 4: Store token in OpenBao ────────────────────────────────────────────

step_store_in_openbao() {
  info "Step 4: Storing Semaphore token in OpenBao..."
  store_token_in_openbao "$SECRETS_DIR" semaphore_api_token "services/semaphore" api_token
}

# ── Step 5: Validate ──────────────────────────────────────────────────────────

step_validate() {
  info "Step 5: Validating Semaphore deployment..."
  local api_token

  check_http "${SEMAPHORE_URL}/api/ping" "Health"

  api_token=$(get_secret "$SECRETS_DIR" semaphore_api_token)
  if ! needs_gen "$api_token"; then
    check_http "${SEMAPHORE_URL}/api/projects" "API token" "Authorization" "Bearer ${api_token}"
  else
    warn "  API token: not yet created"
  fi
}

# ── Main ──────────────────────────────────────────────────────────────────────

main() {
  info "=== Semaphore Deployment ==="
  detect_runtime

  step_generate_secrets
  step_start_services
  step_bootstrap_credentials
  step_store_in_openbao
  step_validate

  info "=== Semaphore deployment complete ==="
}

# Sourced by its tests for the functions above; run as a script, it deploys.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi
