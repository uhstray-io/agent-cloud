#!/usr/bin/env bash
# deploy.sh — WisBot container lifecycle only.
#
# Secrets and the env file are managed by Ansible (manage-secrets renders
# templates/wisbot.env.j2 -> config/wisbot.env). This script never generates
# secrets and never talks to OpenBao. Idempotent: safe to re-run.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# CLONE_DIR is set by Ansible (run-deploy); fall back to the repo root from here.
CLONE_DIR="${CLONE_DIR:-$(dirname "$(dirname "$(dirname "$SCRIPT_DIR")")")}"
LIB_DIR="${CLONE_DIR}/platform/lib"
# shellcheck source=/dev/null
source "${LIB_DIR}/common.sh"

CONFIG_DIR="${SCRIPT_DIR}/config"
WISBOT_URL="${WISBOT_URL:-http://localhost:8080}"

main() {
  info "=== WisBot Deployment ==="
  detect_runtime

  # Ansible must have templated the env file first.
  if [ ! -f "${CONFIG_DIR}/wisbot.env" ]; then
    error "config/wisbot.env missing — deploy via Semaphore (Ansible templates it from OpenBao)."
  fi

  cd "$SCRIPT_DIR"
  info "Pulling image..."
  compose pull
  info "Starting WisBot..."
  compose up -d

  wait_for_http "${WISBOT_URL}/health" "WisBot" 120
  info "=== WisBot deployment complete ==="
}

main "$@"
