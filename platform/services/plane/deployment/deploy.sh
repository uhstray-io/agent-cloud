#!/usr/bin/env bash
# plane — container lifecycle only.
#
# deploy-plane.yml runs first. It fetches Plane's Commercial docker-compose.yml for the pinned
# release, verifies its sha256 and places it here as compose.yml, and renders .env from
# OpenBao. This script does NOT generate or read secrets: it checks those inputs, pulls,
# starts the stack with agent-cloud's overlay (compose.agentcloud.yml) always applied, and
# waits for the API to report healthy. The first boot runs the migrator, so allow time.
#
# Usage: ./deploy.sh [--no-pull]

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

# Plane's file is the base; agent-cloud's overlay always applies on top of it, before any
# overlay the playbook names.
export COMPOSE_OVERLAYS="compose.agentcloud.yml ${COMPOSE_OVERLAYS:-}"

# Top-level service names in a compose file (two-space keys under `services:`).
service_names() {
  awk '/^services:[[:space:]]*$/ {s=1; next}
       s && /^[^[:space:]#]/ {s=0}
       s && /^  [A-Za-z0-9._-]+:/ {sub(/^  /, ""); sub(/:.*/, ""); print}' "$1" | sort -u
}

step_verify_inputs() {
  info "Step 1: Verifying the fetched Compose file and the rendered .env are present..."
  [ -f .env ] || error "${SCRIPT_DIR}/.env not found. Run Ansible deploy-plane.yml first."
  [ -f compose.yml ] || error "${SCRIPT_DIR}/compose.yml not found. deploy-plane.yml fetches it for the pinned release."
  # A release that adds a service must not run on Plane's own restart policy: every base
  # service has to be named in the overlay first.
  local missing
  missing=$(comm -23 <(service_names compose.yml) <(service_names compose.agentcloud.yml))
  [ -z "$missing" ] || error "compose.agentcloud.yml does not name these services from Plane's file: $(echo "$missing" | tr '\n' ' ')"
  info "  inputs present; every Plane service is covered by the overlay."
}

step_pull_images() {
  if [ "$SKIP_PULL" = true ]; then info "Step 2: Skipping image pull (--no-pull)."; return 0; fi
  info "Step 2: Pulling images..."
  compose pull
}

step_start() {
  info "Step 3: Starting Plane..."
  # Recreates only when an input or image changed and prints DEPLOY_CHANGED for the playbook.
  # The migrator is a labelled one-shot: once it has exited 0 it is finished, not a change.
  compose_up_if_changed
}

step_wait_healthy() {
  info "Step 4: Waiting for the Plane API to become healthy (first boot runs migrations)..."
  wait_for_healthy plane-api 600
}

main() {
  info "=== plane deployment (container lifecycle) ==="
  detect_runtime
  info "Container engine: ${CONTAINER_ENGINE}"
  step_verify_inputs
  step_pull_images
  step_start
  step_wait_healthy
  info "=== plane container lifecycle complete ==="
}

main "$@"
