#!/usr/bin/env bash
# step-ca — container lifecycle only.
#
# Ansible's deploy-step-ca.yml templates .env (the init/key password from
# OpenBao) before this runs. This script does NOT generate secrets or keys —
# step-ca auto-initializes its root/intermediate into the persistent volume on
# first boot and reuses them thereafter.
#
# Usage: ./deploy.sh [--no-pull]
# Steps (idempotent): verify .env present, pull, up, wait healthy.

set -euo pipefail

SKIP_PULL=false
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$(dirname "$(dirname "$(dirname "$SCRIPT_DIR")")")/lib"
cd "${SCRIPT_DIR}"

# shellcheck source=/dev/null
source "${LIB_DIR}/common.sh"

for arg in "$@"; do
  case "$arg" in
    --no-pull) SKIP_PULL=true ;;
    *) echo "Unknown option: $arg"; echo "Usage: ./deploy.sh [--no-pull]"; exit 1 ;;
  esac
done

step_verify_env() {
  info "Step 1: Verifying templated .env is present..."
  [ -f "${SCRIPT_DIR}/.env" ] || error "${SCRIPT_DIR}/.env not found. Run Ansible deploy-step-ca.yml first."
  info "  .env present."
}

step_pull_image() {
  if [ "$SKIP_PULL" = true ]; then info "Step 2: Skipping image pull (--no-pull)."; return 0; fi
  info "Step 2: Pulling image..."
  compose pull
}

step_start() {
  info "Step 3: Starting step-ca..."
  compose up -d
}

step_wait_healthy() {
  info "Step 4: Waiting for step-ca to become healthy (first boot initializes the CA)..."
  wait_for_healthy step-ca 120
}

main() {
  info "=== step-ca deployment (container lifecycle) ==="
  detect_runtime
  info "Container engine: ${CONTAINER_ENGINE}"
  step_verify_env
  step_pull_image
  step_start
  step_wait_healthy
  info "=== step-ca container lifecycle complete ==="
}

main "$@"
