#!/usr/bin/env bash
# validate.sh — post-deploy health check for WisBot.
# Confirms the container is up and the /health endpoint reports ready (200).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLONE_DIR="${CLONE_DIR:-$(dirname "$(dirname "$(dirname "$SCRIPT_DIR")")")}"
LIB_DIR="${CLONE_DIR}/platform/lib"
# shellcheck source=/dev/null
source "${LIB_DIR}/common.sh"

WISBOT_URL="${WISBOT_URL:-http://localhost:8080}"

main() {
  info "=== WisBot Validation ==="
  if ! check_http "${WISBOT_URL}/health" "WisBot /health"; then
    error "WisBot health check failed at ${WISBOT_URL}/health"
  fi
  info "=== WisBot validation passed ==="
}

main "$@"
