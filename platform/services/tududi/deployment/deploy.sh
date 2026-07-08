#!/usr/bin/env bash
# tududi — container lifecycle only.
#
# Ansible's deploy-tududi.yml templates .env (session secret + break-glass admin
# from OpenBao, OIDC client secret shared-read from authentik) BEFORE this runs.
# This script does NOT generate secrets — it pulls the image, starts the single
# rootless container, and waits for it to report healthy (first boot creates the
# SQLite schema, so allow a little time).
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
  [ -f "${SCRIPT_DIR}/.env" ] || error "${SCRIPT_DIR}/.env not found. Run Ansible deploy-tududi.yml first."
  info "  .env present."
}

step_pull_image() {
  if [ "$SKIP_PULL" = true ]; then info "Step 2: Skipping image pull (--no-pull)."; return 0; fi
  info "Step 2: Pulling image..."
  compose pull
}

step_start() {
  info "Step 3: Starting tududi..."
  # --force-recreate: tududi reads runtime config (session/OIDC secrets, BASE_URL)
  # from `env_file: .env`. An env_file content change is NOT a compose-spec change,
  # so plain `up -d` leaves the old container running with stale env. Force-recreate
  # so a re-rendered .env always applies.
  compose up -d --force-recreate
}

step_wait_healthy() {
  # First boot creates the SQLite schema + break-glass admin — allow some time.
  info "Step 4: Waiting for tududi to become healthy (first boot creates the SQLite schema)..."
  wait_for_healthy tududi 120
}

main() {
  info "=== tududi deployment (container lifecycle) ==="
  detect_runtime
  info "Container engine: ${CONTAINER_ENGINE}"
  step_verify_env
  step_pull_image
  step_start
  step_wait_healthy
  info "=== tududi container lifecycle complete ==="
}

main "$@"
