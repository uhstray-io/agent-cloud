#!/usr/bin/env bash
# deploy.sh — Deploy OpenBao (secrets backbone)
# Initializes, unseals, configures engines/policies/AppRole, seeds placeholder secrets.
# Idempotent: safe to re-run on an existing deployment.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$(dirname "$(dirname "$(dirname "$SCRIPT_DIR")")")/lib"
source "${LIB_DIR}/common.sh"

SECRETS_DIR="${SCRIPT_DIR}/secrets"
CONFIG_DIR="${SCRIPT_DIR}/config"
POLICIES_DIR="${CONFIG_DIR}/policies"
INIT_FILE="${SECRETS_DIR}/init.json"

# Listen on all interfaces when deploying to a VM (other services need to reach us)
export OPENBAO_LISTEN="${OPENBAO_LISTEN:-0.0.0.0}"

# ── bao helpers (container exec) ──────────────────────────────────────────────

bao() {
  detect_runtime
  $CONTAINER_ENGINE exec workflow-openbao bao "$@"
}

bao_auth() {
  local token="$1"; shift
  detect_runtime
  $CONTAINER_ENGINE exec -e "BAO_TOKEN=$token" workflow-openbao bao "$@"
}

# ── Step 1: Start OpenBao ─────────────────────────────────────────────────────

start_openbao() {
  info "Step 1: Starting OpenBao..."
  mkdir -p "${SCRIPT_DIR}/data/openbao"
  chmod 700 "${SCRIPT_DIR}/data/openbao"
  cd "$SCRIPT_DIR"
  compose up -d
  info "Waiting for OpenBao container..."
  local attempts=0 out
  while true; do
    out=$(bao status -tls-skip-verify 2>&1) || true
    echo "$out" | grep -qE "Initialized|Sealed" && break
    [ $attempts -ge 30 ] && error "OpenBao not reachable after 60s"
    sleep 2
    attempts=$((attempts + 1))
  done
  info "OpenBao container is reachable."
}

# ── Step 2: Initialize ────────────────────────────────────────────────────────

init_openbao() {
  local s; s=$(bao status 2>&1) || true
  if echo "$s" | grep -q "Initialized.*true"; then
    info "Step 2: OpenBao already initialized — skipping."
    return
  fi
  info "Step 2: Initializing OpenBao (1 key share, threshold 1)..."
  mkdir -p "$SECRETS_DIR"
  bao operator init -key-shares=1 -key-threshold=1 -format=json > "$INIT_FILE"
  chmod 600 "$INIT_FILE"
  info "Init complete. Keys saved to secrets/init.json"
}

# ── Step 3: Unseal ────────────────────────────────────────────────────────────

unseal_openbao() {
  local s; s=$(bao status 2>&1) || true
  if echo "$s" | grep -q "Sealed.*false"; then
    info "Step 3: OpenBao already unsealed."
    return
  fi
  [ -f "$INIT_FILE" ] || error "secrets/init.json not found"
  local unseal_key
  unseal_key=$(jq -r '.unseal_keys_b64[0]' "$INIT_FILE")
  info "Step 3: Unsealing OpenBao..."
  bao operator unseal "$unseal_key"

  # Verify unseal succeeded
  local post_status
  post_status=$(bao status 2>&1) || true
  if echo "$post_status" | grep -q "Sealed.*false"; then
    info "  Unseal successful."
  else
    error "  Unseal failed — OpenBao is still sealed."
  fi
}

# ── Step 4: Enable secrets engines ────────────────────────────────────────────

enable_secrets_engines() {
  local token="$1"
  info "Step 4: Enabling secrets engines..."
  local enabled
  enabled=$(bao_auth "$token" secrets list -format=json 2>/dev/null | jq -r 'keys[]')

  echo "$enabled" | grep -q "^secret/$" || bao_auth "$token" secrets enable -path=secret kv-v2
  echo "$enabled" | grep -q "^database/$" || bao_auth "$token" secrets enable database
  info "Secrets engines ready."
}

# ── Step 5: Write policies ────────────────────────────────────────────────────

write_policies() {
  local token="$1"
  info "Step 5: Writing policies..."
  for policy_file in "${POLICIES_DIR}"/*.hcl; do
    local name
    name=$(basename "$policy_file" .hcl)
    $CONTAINER_ENGINE exec -i -e "BAO_TOKEN=$token" workflow-openbao bao policy write "$name" - < "$policy_file"
    info "  Policy: ${name}"
  done
}

# ── Step 6: Enable AppRole auth + create per-service roles ────────────────────

setup_approle() {
  local token="$1"
  info "Step 6: Setting up AppRole auth..."

  if ! bao_auth "$token" auth list -format=json 2>/dev/null | jq -r 'keys[]' | grep -q "^approle/$"; then
    bao_auth "$token" auth enable approle
  fi

  # NemoClaw role — read-only across all service secrets
  bao_auth "$token" write auth/approle/role/nemoclaw \
    secret_id_ttl=0 token_num_uses=0 token_ttl=30m token_max_ttl=2h \
    token_policies=nemoclaw-read

  local role_id secret_id
  role_id=$(bao_auth "$token" read -format=json auth/approle/role/nemoclaw/role-id | jq -r '.data.role_id')
  secret_id=$(bao_auth "$token" write -format=json -f auth/approle/role/nemoclaw/secret-id | jq -r '.data.secret_id')
  put_secret "$SECRETS_DIR" nemoclaw-role-id "$role_id"
  put_secret "$SECRETS_DIR" nemoclaw-secret-id "$secret_id"
  info "  NemoClaw AppRole created."

  # Per-service write roles — each service can only write to its own path
  local svc
  for svc in nocodb n8n semaphore; do
    bao_auth "$token" write "auth/approle/role/${svc}" \
      secret_id_ttl=0 token_num_uses=0 token_ttl=30m token_max_ttl=2h \
      "token_policies=${svc}-write"

    role_id=$(bao_auth "$token" read -format=json "auth/approle/role/${svc}/role-id" | jq -r '.data.role_id')
    secret_id=$(bao_auth "$token" write -format=json -f "auth/approle/role/${svc}/secret-id" | jq -r '.data.secret_id')
    put_secret "$SECRETS_DIR" "${svc}-role-id" "$role_id"
    put_secret "$SECRETS_DIR" "${svc}-secret-id" "$secret_id"
    info "  ${svc} AppRole created."
  done
}

# ── Step 7: Seed placeholder secrets ──────────────────────────────────────────

seed_if_absent() {
  local token="$1" path="$2"; shift 2
  bao_auth "$token" kv get "$path" &>/dev/null || bao_auth "$token" kv put "$path" "$@"
}

seed_secrets() {
  local token="$1"
  info "Step 7: Seeding placeholder secrets..."
  seed_if_absent "$token" secret/services/nocodb    url="${NOCODB_URL:-REPLACE_WITH_NOCODB_URL}"       api_token="REPLACE_WITH_NOCODB_API_TOKEN"
  seed_if_absent "$token" secret/services/github    pat="REPLACE_WITH_GITHUB_PAT"                     deploy_key="REPLACE_WITH_GITHUB_DEPLOY_KEY"
  seed_if_absent "$token" secret/services/discord   bot_token="REPLACE_WITH_DISCORD_BOT_TOKEN"
  seed_if_absent "$token" secret/services/proxmox   url="${PROXMOX_URL:-REPLACE_WITH_PROXMOX_URL}"    api_token="REPLACE_WITH_PROXMOX_API_TOKEN" token_id="${PROXMOX_TOKEN_ID:-REPLACE_WITH_PROXMOX_TOKEN_ID}"
  seed_if_absent "$token" secret/services/n8n       url="${N8N_URL:-REPLACE_WITH_N8N_URL}"             api_key="REPLACE_WITH_N8N_API_KEY"
  seed_if_absent "$token" secret/services/semaphore url="${SEMAPHORE_URL:-REPLACE_WITH_SEMAPHORE_URL}" api_token="REPLACE_WITH_SEMAPHORE_API_TOKEN"
  info "Placeholder secrets in place."
}

# ── Main ──────────────────────────────────────────────────────────────────────

main() {
  info "=== OpenBao Deployment ==="
  detect_runtime

  start_openbao
  init_openbao
  unseal_openbao

  local root_token
  root_token=$(jq -r '.root_token' "$INIT_FILE")

  enable_secrets_engines "$root_token"
  write_policies "$root_token"
  setup_approle "$root_token"
  seed_secrets "$root_token"

  info ""
  info "=== OpenBao deployment complete ==="
  info "  UI: http://localhost:8200/ui"
  info "  Root token: secrets/init.json"
}

main "$@"
