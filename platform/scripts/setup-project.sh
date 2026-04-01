#!/usr/bin/env bash
# setup-project.sh — Programmatically create Semaphore project, inventory,
# repositories, and task templates for agent-cloud deployment.
# Run AFTER Semaphore is deployed and API token is available.
# Idempotent: checks for existing resources before creating.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
LIB_DIR="${PROJECT_ROOT}/lib"
source "${LIB_DIR}/common.sh"

SEMAPHORE_URL="${SEMAPHORE_URL:-http://localhost:${SEMAPHORE_PORT:-3000}}"
SECRETS_DIR="${PROJECT_ROOT}/services/semaphore/deployment/secrets"
API_TOKEN=""

# ── Helpers ───────────────────────────────────────────────────────────────────

sem_api() {
  local method="$1" path="$2"; shift 2
  curl -sf -X "$method" \
    -H "Authorization: Bearer ${API_TOKEN}" \
    -H "Content-Type: application/json" \
    "${SEMAPHORE_URL}/api${path}" \
    "$@"
}

sem_api_check() {
  local method="$1" path="$2"; shift 2
  local code
  code=$(curl -s -o /dev/null -w "%{http_code}" \
    -X "$method" \
    -H "Authorization: Bearer ${API_TOKEN}" \
    -H "Content-Type: application/json" \
    "${SEMAPHORE_URL}/api${path}" \
    "$@") || code="000"
  echo "$code"
}

# ── Step 1: Get or validate API token ─────────────────────────────────────────

get_api_token() {
  API_TOKEN=$(get_secret "$SECRETS_DIR" semaphore_api_token)
  if needs_gen "$API_TOKEN"; then
    error "No Semaphore API token found. Run vms/semaphore/deploy.sh first."
  fi

  local code
  code=$(sem_api_check GET "/projects")
  if [ "$code" != "200" ]; then
    error "API token invalid (HTTP ${code}). Re-run vms/semaphore/deploy.sh."
  fi
  info "API token validated."
}

# ── Step 2: Create project ────────────────────────────────────────────────────

create_project() {
  info "Step 2: Creating project..."

  # Check if project already exists
  local projects
  projects=$(sem_api GET "/projects" 2>/dev/null) || projects="[]"
  local existing
  existing=$(echo "$projects" | jq -r '.[] | select(.name == "agent-cloud") | .id' 2>/dev/null) || existing=""

  if [ -n "$existing" ]; then
    PROJECT_ID="$existing"
    info "  Project already exists (id=${PROJECT_ID})."
    return 0
  fi

  local response
  response=$(sem_api POST "/projects" -d '{
    "name": "agent-cloud",
    "alert": false
  }')

  PROJECT_ID=$(echo "$response" | jq -r '.id')
  info "  Project created (id=${PROJECT_ID})."
}

# ── Step 3: Create key store (None key for local execution) ───────────────────

create_keys() {
  info "Step 3: Creating key store entries..."
  local existing
  existing=$(sem_api GET "/project/${PROJECT_ID}/keys" 2>/dev/null) || existing="[]"

  # None key (for local connections)
  local none_key_id
  none_key_id=$(echo "$existing" | jq -r '.[] | select(.name == "local-none") | .id' 2>/dev/null) || none_key_id=""
  if [ -z "$none_key_id" ]; then
    local resp
    resp=$(sem_api POST "/project/${PROJECT_ID}/keys" -d '{
      "name": "local-none",
      "type": "none",
      "project_id": '"${PROJECT_ID}"'
    }')
    none_key_id=$(echo "$resp" | jq -r '.id')
    info "  Created 'local-none' key (id=${none_key_id})."
  else
    info "  'local-none' key already exists (id=${none_key_id})."
  fi
  NONE_KEY_ID="$none_key_id"
}

# ── Step 4: Create inventory ──────────────────────────────────────────────────

create_inventory() {
  info "Step 4: Creating inventory..."
  local existing
  existing=$(sem_api GET "/project/${PROJECT_ID}/inventory" 2>/dev/null) || existing="[]"

  local inv_id
  inv_id=$(echo "$existing" | jq -r '.[] | select(.name == "local") | .id' 2>/dev/null) || inv_id=""
  if [ -z "$inv_id" ]; then
    local inv_content
    inv_content=$(cat "${PROJECT_ROOT}/inventory/local.yml")
    local resp
    resp=$(sem_api POST "/project/${PROJECT_ID}/inventory" -d "$(jq -n \
      --arg name "local" \
      --argjson pid "$PROJECT_ID" \
      --argjson kid "$NONE_KEY_ID" \
      --arg inv "$inv_content" \
      '{name: $name, project_id: $pid, inventory: $inv, ssh_key_id: $kid, type: "static-yaml"}')")
    inv_id=$(echo "$resp" | jq -r '.id')
    info "  Created 'local' inventory (id=${inv_id})."
  else
    info "  'local' inventory already exists (id=${inv_id})."
  fi
  LOCAL_INV_ID="$inv_id"
}

# ── Step 5: Create repository (local path) ────────────────────────────────────

create_repository() {
  info "Step 5: Creating repository..."
  local existing
  existing=$(sem_api GET "/project/${PROJECT_ID}/repositories" 2>/dev/null) || existing="[]"

  local repo_id
  repo_id=$(echo "$existing" | jq -r '.[] | select(.name == "agent-cloud-local") | .id' 2>/dev/null) || repo_id=""
  if [ -z "$repo_id" ]; then
    local resp
    resp=$(sem_api POST "/project/${PROJECT_ID}/repositories" -d "$(jq -n \
      --arg name "agent-cloud-local" \
      --argjson pid "$PROJECT_ID" \
      --argjson kid "$NONE_KEY_ID" \
      --arg git_url "${PROJECT_ROOT}" \
      '{name: $name, project_id: $pid, ssh_key_id: $kid, git_url: $git_url, git_branch: "main"}')")
    repo_id=$(echo "$resp" | jq -r '.id')
    info "  Created repository (id=${repo_id})."
  else
    info "  Repository already exists (id=${repo_id})."
  fi
  REPO_ID="$repo_id"
}

# ── Step 6: Create environments ───────────────────────────────────────────────

create_environments() {
  info "Step 6: Creating environments..."
  local existing
  existing=$(sem_api GET "/project/${PROJECT_ID}/environment" 2>/dev/null) || existing="[]"

  local env_id
  env_id=$(echo "$existing" | jq -r '.[] | select(.name == "local-dev") | .id' 2>/dev/null) || env_id=""
  if [ -z "$env_id" ]; then
    local resp
    resp=$(sem_api POST "/project/${PROJECT_ID}/environment" -d "$(jq -n \
      --arg name "local-dev" \
      --argjson pid "$PROJECT_ID" \
      --arg root "$PROJECT_ROOT" \
      '{name: $name, project_id: $pid, json: "{}", env: "", extra_vars: ({project_root: $root} | @json)}')")
    env_id=$(echo "$resp" | jq -r '.id')
    info "  Created 'local-dev' environment (id=${env_id})."
  else
    info "  'local-dev' environment already exists (id=${env_id})."
  fi
  ENV_ID="$env_id"
}

# ── Step 7: Create task templates ─────────────────────────────────────────────

create_template() {
  local name="$1" playbook="$2" extra_vars="${3:-}"

  local tmpl_id
  tmpl_id=$(echo "$ALL_TEMPLATES" | jq -r --arg n "$name" '.[] | select(.name == $n) | .id' 2>/dev/null) || tmpl_id=""
  if [ -n "$tmpl_id" ]; then
    info "  Template '${name}' already exists (id=${tmpl_id})."
    return 0
  fi

  local payload
  payload=$(jq -n \
    --arg name "$name" \
    --argjson pid "$PROJECT_ID" \
    --argjson iid "$LOCAL_INV_ID" \
    --argjson rid "$REPO_ID" \
    --argjson eid "$ENV_ID" \
    --arg playbook "$playbook" \
    --arg extra "$extra_vars" \
    '{
      name: $name,
      project_id: $pid,
      inventory_id: $iid,
      repository_id: $rid,
      environment_id: $eid,
      playbook: $playbook,
      app: "ansible",
      start_version: "",
      autorun: false,
      survey_vars: [],
      suppress_success_alerts: true
    } + (if $extra != "" then {extra_cli_arguments: $extra} else {} end)')

  local resp
  resp=$(sem_api POST "/project/${PROJECT_ID}/templates" -d "$payload")
  tmpl_id=$(echo "$resp" | jq -r '.id')
  info "  Created template '${name}' (id=${tmpl_id})."
}

create_task_templates() {
  info "Step 7: Creating task templates..."
  ALL_TEMPLATES=$(sem_api GET "/project/${PROJECT_ID}/templates" 2>/dev/null) || ALL_TEMPLATES="[]"

  create_template "Deploy All Services" \
    "playbooks/deploy-all.yml"

  create_template "Validate All Services" \
    "playbooks/validate-all.yml"

  create_template "Deploy OpenBao" \
    "playbooks/deploy-service.yml" \
    "-e target_service=openbao_svc"

  create_template "Deploy NocoDB" \
    "playbooks/deploy-service.yml" \
    "-e target_service=nocodb_svc"

  create_template "Deploy n8n" \
    "playbooks/deploy-service.yml" \
    "-e target_service=n8n_svc"

  create_template "Deploy Semaphore" \
    "playbooks/deploy-service.yml" \
    "-e target_service=semaphore_svc"

  create_template "Deploy NetBox" \
    "playbooks/deploy-service.yml" \
    "-e target_service=netbox_svc"

  create_template "Deploy NemoClaw" \
    "playbooks/deploy-service.yml" \
    "-e target_service=nemoclaw_svc"

  create_template "Update NocoDB" \
    "playbooks/update-service.yml" \
    "-e target_service=nocodb_svc"

  create_template "Update n8n" \
    "playbooks/update-service.yml" \
    "-e target_service=n8n_svc"

  create_template "Update Semaphore" \
    "playbooks/update-service.yml" \
    "-e target_service=semaphore_svc"

  create_template "Update NetBox" \
    "playbooks/update-service.yml" \
    "-e target_service=netbox_svc"

  # Proxmox provisioning templates
  create_template "Validate Proxmox Cluster" \
    "playbooks/proxmox-validate.yml"

  create_template "Create VM Template" \
    "playbooks/provision-template.yml"

  create_template "Provision OpenBao VM" \
    "playbooks/provision-vm.yml" \
    "-e target_service=openbao"

  create_template "Provision NocoDB VM" \
    "playbooks/provision-vm.yml" \
    "-e target_service=nocodb"

  create_template "Provision n8n VM" \
    "playbooks/provision-vm.yml" \
    "-e target_service=n8n"

  create_template "Provision Semaphore VM" \
    "playbooks/provision-vm.yml" \
    "-e target_service=semaphore"

  create_template "Provision NemoClaw VM" \
    "playbooks/provision-vm.yml" \
    "-e target_service=nemoclaw"

  create_template "Provision NetBox VM" \
    "playbooks/provision-vm.yml" \
    "-e target_service=netbox"
}

# ── Main ──────────────────────────────────────────────────────────────────────

main() {
  info "=== Semaphore Project Setup ==="

  get_api_token
  create_project
  create_keys
  create_inventory
  create_repository
  create_environments
  create_task_templates

  info ""
  info "=== Semaphore project setup complete ==="
  info "  UI: ${SEMAPHORE_URL}"
  info "  Project: agent-cloud (id=${PROJECT_ID})"
  info "  Templates: Deploy All, Validate All, per-service deploy/update/provision"
}

main "$@"
