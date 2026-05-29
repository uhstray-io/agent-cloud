#!/usr/bin/env bash
# inference-comfyui — post-deploy verification.
#
# Confirms:
#   1. FastAPI wrapper is up.
#   2. The wrapper can reach ComfyUI (separately deployed on the host).
#   3. Required Flux.1 model weights are present.
#   4. A no-op smoke prompt round-trips (optional, gated by SMOKE=1).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$(dirname "$(dirname "$(dirname "$SCRIPT_DIR")")")/lib"
cd "${SCRIPT_DIR}"

# shellcheck source=/dev/null
source "${LIB_DIR}/common.sh"

SERVICE_URL="${SERVICE_URL:-http://localhost:8189}"
SMOKE="${SMOKE:-0}"

step_check_api() {
  info "Step 1: Wrapper /health..."
  check_http "${SERVICE_URL}/health" "comfyui-api health"
}

step_check_comfyui_reachable() {
  info "Step 2: Verifying wrapper -> ComfyUI reachability..."
  local resp
  resp=$(curl -sf "${SERVICE_URL}/health/comfyui" 2>/dev/null) || {
    warn "  Wrapper cannot reach ComfyUI. Check COMFYUI_URL in .env and that ComfyUI is running on the host."
    return 1
  }
  info "  ComfyUI reachable: ${resp}"
}

step_check_model_weights() {
  info "Step 3: Verifying Flux.1 Schnell weights..."
  local resp
  resp=$(curl -sf "${SERVICE_URL}/health/models" 2>/dev/null) || {
    warn "  Wrapper cannot enumerate models. Verify ComfyUI/models/unet/ contains flux1-schnell-fp8.safetensors."
    return 1
  }
  info "  Models: ${resp}"
}

step_smoke() {
  if [ "$SMOKE" != "1" ]; then
    info "Step 4: Skipping smoke generation (set SMOKE=1 to run)."
    return 0
  fi
  info "Step 4: Running smoke generation..."
  local resp
  resp=$(curl -sf -X POST "${SERVICE_URL}/generate" \
            -H 'Content-Type: application/json' \
            -d '{"generation_id":"00000000-0000-0000-0000-000000000000","prompt":"a smoke-test sticker of a small grey cat, simple flat illustration"}' \
            --max-time 120 2>/dev/null) || {
    warn "  Smoke generation failed."
    return 1
  }
  info "  Smoke OK: ${resp}"
}

main() {
  info "=== inference-comfyui post-deploy ==="
  step_check_api
  step_check_comfyui_reachable || warn "(continuing despite ComfyUI reachability warning)"
  step_check_model_weights || warn "(continuing despite model check warning)"
  step_smoke
  info "=== inference-comfyui post-deploy complete ==="
}

main "$@"
