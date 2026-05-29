#!/usr/bin/env bash
# inference-hunyuan3d — container lifecycle only.
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

SERVICE_URL="${SERVICE_URL:-http://localhost:8001}"

step_verify_env() {
  info "Step 1: Verifying templated .env is present..."
  [ -f "${SCRIPT_DIR}/.env" ] || error ".env missing. Run deploy-inference-hunyuan3d.yml first."
}

step_verify_weights() {
  info "Step 2: Verifying model weights on host..."
  local weights_dir="${HUNYUAN3D_WEIGHTS_DIR:-/srv/hunyuan3d/weights}"
  if [ ! -d "${weights_dir}" ]; then
    warn "  Weights directory ${weights_dir} not found."
    warn "  See ../context/architecture/weights.md for download instructions."
    error "Refusing to start without weights mount."
  fi
  info "  Weights present at ${weights_dir}."
}

step_pull() {
  if [ "$SKIP_PULL" = true ]; then
    info "Step 3: Skipping image pull (--no-pull)."
    return 0
  fi
  info "Step 3: Pulling images..."
  compose pull
}

step_start() {
  info "Step 4: Starting inference-hunyuan3d stack..."
  compose up -d
}

step_wait_healthy() {
  info "Step 5: Waiting for FastAPI wrapper to become healthy..."
  # 240s — model weights take ~60-90s to load on cold start.
  wait_for_http "${SERVICE_URL}/health" "hunyuan3d-api" 240
}

main() {
  info "=== inference-hunyuan3d deployment ==="
  detect_runtime
  info "Container engine: ${CONTAINER_ENGINE}"
  step_verify_env
  step_verify_weights
  step_pull
  step_start
  step_wait_healthy
  info "=== inference-hunyuan3d lifecycle complete ==="
}

main "$@"
