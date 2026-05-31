#!/usr/bin/env bash
# inference-comfyui — container lifecycle only.
#
# Ansible (deploy-inference-comfyui.yml, Phase 4 of the integration plan):
#   1. Fetches secrets from OpenBao via tasks/manage-secrets.yml
#   2. Templates .env from templates/env.j2
#   3. Verifies the host has the NVIDIA Container Toolkit (tasks/install-nvidia-toolkit.yml)
#   4. Verifies ComfyUI is reachable at COMFYUI_URL (separately deployed on the host)
#   5. Runs this script
#   6. Runs post-deploy.sh (model weight checks, smoke generation)
#
# Required on disk before this script runs:
#   .env             — templated by Ansible from OpenBao
set -euo pipefail

SKIP_PULL=false
SERVICE_URL=""
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$(dirname "$(dirname "$(dirname "$SCRIPT_DIR")")")/lib"
cd "${SCRIPT_DIR}"

# shellcheck source=/dev/null
source "${LIB_DIR}/common.sh"

for arg in "$@"; do
  case "$arg" in
    --no-pull) SKIP_PULL=true ;;
    http://*|https://*) SERVICE_URL="$arg" ;;
    *) echo "Unknown option: $arg"; exit 1 ;;
  esac
done

SERVICE_URL="${SERVICE_URL:-http://localhost:8189}"

step_verify_env() {
  info "Step 1: Verifying templated .env is present..."
  [ -f "${SCRIPT_DIR}/.env" ] || error ".env missing. Run deploy-inference-comfyui.yml first."
}

step_pull() {
  if [ "$SKIP_PULL" = true ]; then
    info "Step 2: Skipping image pull (--no-pull)."
    return 0
  fi
  info "Step 2: Pulling images..."
  compose pull
}

step_start() {
  info "Step 3: Starting inference-comfyui stack..."
  compose up -d
}

step_wait_healthy() {
  info "Step 4: Waiting for FastAPI wrapper to become healthy..."
  wait_for_http "${SERVICE_URL}/health" "comfyui-api" 180
}

main() {
  info "=== inference-comfyui deployment ==="
  detect_runtime
  info "Container engine: ${CONTAINER_ENGINE}"
  step_verify_env
  step_pull
  step_start
  step_wait_healthy
  info "=== inference-comfyui lifecycle complete ==="
}

main "$@"
