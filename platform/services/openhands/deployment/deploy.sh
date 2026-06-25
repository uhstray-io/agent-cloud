#!/usr/bin/env bash
# openhands — container lifecycle only (Docker).
#
# Ansible's deploy-openhands.yml renders .env (non-secret config from inventory)
# and places the repo before this runs. This script does NOT generate secrets —
# it pulls, starts the container, and waits for it to report healthy.
#
# Docker (not podman): OpenHands launches per-session runtime containers via the
# host Docker socket, so a real Docker daemon is required. CONTAINER_ENGINE is
# forced to docker by the playbook; detect_runtime honors it.
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
  [ -f "${SCRIPT_DIR}/.env" ] || error "${SCRIPT_DIR}/.env not found. Run Ansible deploy-openhands.yml first."
  info "  .env present."
}

step_pull_image() {
  if [ "$SKIP_PULL" = true ]; then info "Step 2: Skipping image pull (--no-pull)."; return 0; fi
  info "Step 2: Pulling images..."
  compose pull
}

step_start() {
  info "Step 3: Starting openhands..."
  # --force-recreate: the container reads runtime config from env_file: .env; an
  # env_file change is not a compose-spec change, so plain `up -d` would keep the
  # old container with stale env. Force-recreate so a re-rendered .env applies.
  compose up -d --force-recreate
}

step_wait_healthy() {
  info "Step 4: Waiting for openhands to become healthy..."
  wait_for_healthy openhands 180
}

main() {
  info "=== openhands deployment (container lifecycle) ==="
  detect_runtime
  info "Container engine: ${CONTAINER_ENGINE}"
  step_verify_env
  step_pull_image
  step_start
  step_wait_healthy
  info "=== openhands container lifecycle complete ==="
}

main "$@"
