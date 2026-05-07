#!/usr/bin/env bash
# deploy.sh — Deploy n8n (container lifecycle only)
#
# Secrets and env files are managed by Ansible (deploy-n8n.yml).
# This script starts containers, bootstraps the owner + API key,
# and validates the deployment.
#
# Idempotent: safe to re-run on an existing deployment.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$(dirname "$(dirname "$(dirname "$SCRIPT_DIR")")")/lib"
source "${LIB_DIR}/common.sh"

CONFIG_DIR="${SCRIPT_DIR}/config"
N8N_URL="${N8N_URL:-http://localhost:5678}"
ADMIN_EMAIL="${N8N_ADMIN_EMAIL:-admin@uhstray.io}"

# ── Step 1: Start services ────────────────────────────────────────────────────

step_start_services() {
  info "Step 1: Starting n8n services..."
  cd "$SCRIPT_DIR"
  compose up -d
  wait_for_http "${N8N_URL}/healthz" "n8n" 120
}

# ── Step 2: Bootstrap owner + API key ─────────────────────────────────────────

step_bootstrap_credentials() {
  info "Step 2: Bootstrapping n8n credentials..."

  local owner_pass
  owner_pass=$(grep '^POSTGRES_NON_ROOT_PASSWORD=' "${CONFIG_DIR}/n8n.env" 2>/dev/null | cut -d= -f2-)
  if [ -z "$owner_pass" ]; then
    warn "  No owner password found in config/n8n.env — skipping bootstrap."
    return 0
  fi

  # Try owner setup (first boot only — fails if owner already exists)
  local setup_response setup_payload
  setup_payload=$(jq -n \
    --arg email "$ADMIN_EMAIL" \
    --arg pass "$owner_pass" \
    '{"email":$email,"firstName":"Admin","lastName":"User","password":$pass}')

  setup_response=$(curl -sf -X POST "${N8N_URL}/rest/owner/setup" \
    -H "Content-Type: application/json" \
    --data-raw "$setup_payload" 2>/dev/null) || true

  if [ -n "$setup_response" ]; then
    info "  Owner account created."
  else
    info "  Owner already exists — proceeding to login."
  fi

  # Login to get session cookie
  local cookie_jar login_payload
  cookie_jar=$(mktemp)
  trap 'rm -f "$cookie_jar"' EXIT INT TERM

  login_payload=$(jq -n \
    --arg email "$ADMIN_EMAIL" \
    --arg pass "$owner_pass" \
    '{"emailOrLdapLoginId":$email,"password":$pass}')

  curl -sf -c "$cookie_jar" -X POST "${N8N_URL}/rest/login" \
    -H "Content-Type: application/json" \
    --data-raw "$login_payload" >/dev/null 2>&1 || {
    warn "  Login failed — API key creation deferred."
    return 0
  }
  info "  Logged in."

  # Create API key
  local key_response api_key
  key_response=$(curl -sf -b "$cookie_jar" -X POST "${N8N_URL}/rest/api-keys" \
    -H "Content-Type: application/json" \
    -d '{"label":"nemoclaw-agent","scopes":["workflow:read","workflow:execute","workflow:list"],"expiresAt":0}' \
    2>/dev/null) || true

  api_key=$(echo "${key_response:-}" | jq -r '.data.rawApiKey // empty' 2>/dev/null) || api_key=""

  if [ -n "$api_key" ]; then
    info "  API key created."
    echo "N8N_API_KEY=${api_key}"
    return 0
  fi

  # Fallback: direct DB insert
  info "  API endpoint unavailable — trying direct DB insert..."
  detect_runtime
  api_key=$(openssl rand -hex 20)
  if ! [[ "$api_key" =~ ^[0-9a-f]+$ ]]; then
    warn "  Generated key failed hex validation — aborting DB insert."
    return 0
  fi
  local insert_result
  insert_result=$($CONTAINER_ENGINE exec workflow-n8n-postgres \
    psql -U n8n_user -d n8n -t -A -c \
    "INSERT INTO api_key (user_id, label, api_key, created_at, updated_at)
     SELECT '1', 'nemoclaw-agent', '${api_key}', NOW(), NOW()
     WHERE NOT EXISTS (SELECT 1 FROM api_key WHERE label = 'nemoclaw-agent')
     RETURNING api_key;" 2>/dev/null) || insert_result=""

  if [ -n "$insert_result" ]; then
    info "  API key created via DB insert."
    echo "N8N_API_KEY=${api_key}"
  else
    warn "  API key creation failed — may need manual creation."
  fi
}

# ── Step 3: Validate ──────────────────────────────────────────────────────────

step_validate() {
  info "Step 3: Validating n8n deployment..."
  check_http "${N8N_URL}/healthz" "Health"
}

# ── Main ──────────────────────────────────────────────────────────────────────

main() {
  info "=== n8n Deployment ==="
  detect_runtime

  step_start_services
  step_bootstrap_credentials
  step_validate

  info "=== n8n deployment complete ==="
}

main "$@"
