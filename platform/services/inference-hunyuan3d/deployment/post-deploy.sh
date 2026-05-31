#!/usr/bin/env bash
# inference-hunyuan3d — post-deploy verification.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$(dirname "$(dirname "$(dirname "$SCRIPT_DIR")")")/lib"
cd "${SCRIPT_DIR}"

# shellcheck source=/dev/null
source "${LIB_DIR}/common.sh"

SERVICE_URL="${SERVICE_URL:-http://localhost:8001}"
SMOKE="${SMOKE:-0}"

step_check_api() {
  info "Step 1: Wrapper /health..."
  check_http "${SERVICE_URL}/health" "hunyuan3d-api health"
}

step_check_weights_present() {
  info "Step 2: Verifying model weights are present on disk..."
  local resp
  resp=$(curl -sf "${SERVICE_URL}/health/weights" 2>/dev/null) || {
    warn "  Weights directory missing. Run tasks/ensure-weights.yml to download them to HUNYUAN3D_WEIGHTS_DIR."
    return 1
  }
  info "  Weights: ${resp}"
}

step_check_gpu() {
  info "Step 3: Verifying GPU visibility..."
  local resp
  resp=$(curl -sf "${SERVICE_URL}/health/gpu" 2>/dev/null) || {
    warn "  Cannot fetch GPU status. Verify PCIe passthrough and NVIDIA toolkit on host."
    return 1
  }
  info "  GPU: ${resp}"
}

step_smoke() {
  if [ "$SMOKE" != "1" ]; then
    info "Step 4: Skipping smoke generation (set SMOKE=1 to run; ~30s on RTX 5070)."
    return 0
  fi
  info "Step 4: Running smoke 3D generation..."
  local resp
  resp=$(curl -sf -X POST "${SERVICE_URL}/generate" \
            -H 'Content-Type: application/json' \
            -d '{"generation_id":"00000000-0000-0000-0000-000000000000","prompt":"a small smooth pebble"}' \
            --max-time 180 2>/dev/null) || {
    warn "  Smoke generation failed."
    return 1
  }
  info "  Smoke OK: ${resp}"
}

main() {
  info "=== inference-hunyuan3d post-deploy ==="
  step_check_api
  step_check_weights_present || warn "(continuing despite weights warning)"
  step_check_gpu || warn "(continuing despite GPU warning)"
  step_smoke
  info "=== inference-hunyuan3d post-deploy complete ==="
}

main "$@"
