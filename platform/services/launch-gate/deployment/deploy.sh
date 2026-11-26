#!/usr/bin/env bash
# launch-gate — container lifecycle only.
#
# The deploy playbook renders .env (the image and bind, the Semaphore token and the caller tokens) and
# config.json, and places the step-ca trust bundle and the server leaf under certs/, BEFORE this runs.
# That playbook is platform/playbooks/deploy-launch-gate.yml (Deploy launch-gate). This script does NOT
# generate secrets: it pulls the image, starts the gate, and confirms the container is running.
#
# Usage: ./deploy.sh [--no-pull]
# Steps (idempotent): verify inputs, pull, change-aware start, wait for running.

set -euo pipefail

SKIP_PULL=false
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$(dirname "$(dirname "$(dirname "$SCRIPT_DIR")")")/lib"
cd "${SCRIPT_DIR}"

# shellcheck source=/dev/null
source "${LIB_DIR}/common.sh"

# Production (outside LOCAL_MODE): the overlay sets the uid mapping that lets the non-root process read
# the 0600 TLS key. Local-dev loads compose.local.yml instead, which common.sh adds by itself.
if [ "${LOCAL_MODE:-}" != "true" ]; then
  COMPOSE_OVERLAYS="compose.prod.yml ${COMPOSE_OVERLAYS:-}"
fi

for arg in "$@"; do
  case "$arg" in
    --no-pull) SKIP_PULL=true ;;
    *) echo "Unknown option: $arg"; echo "Usage: ./deploy.sh [--no-pull]"; exit 1 ;;
  esac
done

step_verify_inputs() {
  info "Step 1: Verifying the rendered inputs are present..."
  [ -f "${SCRIPT_DIR}/.env" ] || error "${SCRIPT_DIR}/.env not found. Render it with the launch-gate deploy playbook first."
  [ -f "${SCRIPT_DIR}/config.json" ] || error "${SCRIPT_DIR}/config.json not found. Render it with the launch-gate deploy playbook first."
  [ -f "${SCRIPT_DIR}/certs/step-ca-bundle.crt" ] || error "${SCRIPT_DIR}/certs/step-ca-bundle.crt not found. Place the trust bundle first."
  info "  inputs present."
}

step_pull() {
  if [ "$SKIP_PULL" = true ]; then info "Step 2: Skipping image pull (--no-pull)."; return 0; fi
  info "Step 2: Pulling the image..."
  compose pull
}

step_start() {
  info "Step 3: Starting launch-gate..."
  # The gate reads config.json and the bundle once, at start; .env is its environment. Each is hashed
  # with the compose files, so a change recreates the container and an unchanged re-run is a no-op.
  # The app directory is hashed because it is bind-mounted read-only and the process reads it at start.
  # The leaf is NOT hashed: the gate re-reads its certificate on every connection, so a renewal must
  # not recreate the container. Hashing the whole certs/ directory would make every renewal a restart.
  compose_up_if_changed app config.json certs/step-ca-bundle.crt
}

step_wait_running() {
  info "Step 4: Waiting for the launch-gate container to be running..."
  local _ status
  for _ in $(seq 1 20); do
    status=$(${CONTAINER_ENGINE} inspect -f '{{.State.Status}}' launch-gate 2>/dev/null || true)
    if [ "$status" = running ]; then
      info "  launch-gate running."
      return 0
    fi
    sleep 2
  done
  # Running here says only that the process is up. The deploy playbook proves the TLS listener.
  dump_container_diagnostics launch-gate
  error "launch-gate container did not reach running state within timeout."
}

main() {
  info "=== launch-gate deployment (container lifecycle) ==="
  detect_runtime
  info "Container engine: ${CONTAINER_ENGINE}"
  step_verify_inputs
  step_pull
  step_start
  step_wait_running
  info "=== launch-gate container lifecycle complete ==="
}

main "$@"
