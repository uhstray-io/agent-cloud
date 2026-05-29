#!/usr/bin/env bash
# UhhCraft — container lifecycle only.
#
# This script does NOT generate or fetch secrets. Ansible's deploy-uhhcraft.yml
# (Phase 4 of the WebSmith integration plan) is responsible for:
#   1. Fetching secrets from OpenBao via tasks/manage-secrets.yml
#   2. Templating .env from templates/env.j2
#   3. Running this script
#   4. Running post-deploy.sh (migrations + healthcheck)
#   5. Rendering templates/caddy-site.j2 into the central Caddy
#
# Usage:
#   ./deploy.sh [--no-pull] [UHHCRAFT_URL]
#
# Required on disk before this script runs:
#   .env             — templated by Ansible from OpenBao
#
# Steps (all idempotent):
#   1. Verify .env present (fail fast if Ansible didn't run)
#   2. Pull latest images (unless --no-pull)
#   3. Start the stack (Postgres → Redis → MinIO → app)
#   4. Wait for the app to become healthy

set -euo pipefail

SKIP_PULL=false
UHHCRAFT_URL=""
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$(dirname "$(dirname "$(dirname "$SCRIPT_DIR")")")/lib"
cd "${SCRIPT_DIR}"

# shellcheck source=/dev/null
source "${LIB_DIR}/common.sh"

for arg in "$@"; do
  case "$arg" in
    --no-pull) SKIP_PULL=true ;;
    http://*|https://*) UHHCRAFT_URL="$arg" ;;
    *)
      echo "Unknown option: $arg"
      echo "Usage: ./deploy.sh [--no-pull] [UHHCRAFT_URL]"
      exit 1
      ;;
  esac
done

UHHCRAFT_URL="${UHHCRAFT_URL:-http://localhost:3000}"

step_verify_env() {
  info "Step 1: Verifying templated .env is present..."
  if [ ! -f "${SCRIPT_DIR}/.env" ]; then
    error "${SCRIPT_DIR}/.env not found. Run Ansible deploy-uhhcraft.yml first; it templates this file from OpenBao."
  fi
  info "  .env present."
}

step_pull_images() {
  if [ "$SKIP_PULL" = true ]; then
    info "Step 2: Skipping image pull (--no-pull)."
    return 0
  fi
  info "Step 2: Pulling images..."
  compose pull
}

step_start_services() {
  info "Step 3: Starting UhhCraft stack..."
  compose up -d
}

step_wait_healthy() {
  info "Step 4: Waiting for UhhCraft to become healthy..."
  wait_for_http "${UHHCRAFT_URL}/healthz" "UhhCraft" 180
}

main() {
  info "=== UhhCraft deployment (container lifecycle) ==="
  detect_runtime
  info "Container engine: ${CONTAINER_ENGINE}"

  step_verify_env
  step_pull_images
  step_start_services
  step_wait_healthy

  info "=== UhhCraft container lifecycle complete ==="
  info "Next: run post-deploy.sh for migrations and bootstrap."
}

main "$@"
