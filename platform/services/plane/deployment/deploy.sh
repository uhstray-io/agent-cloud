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

# Plane's file is the base; agent-cloud's overlay and its one publish always apply on top of it,
# before any overlay the playbook names.
export COMPOSE_OVERLAYS="compose.agentcloud.yml compose.publish.yml ${COMPOSE_OVERLAYS:-}"

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

# Is the instance set up? Asked of the API directly, inside its own container.
instance_set_up() {
  $CONTAINER_ENGINE exec plane-api python -c \
    "import json, sys, urllib.request; sys.exit(0 if json.load(urllib.request.urlopen('http://localhost:8000/api/instances/', timeout=10))['instance']['is_setup_done'] else 1)"
}

# The staged start compose_up_if_changed calls when the project must be (re)created. The proxy
# is the ONLY way in, and until the first admin signs up anyone who reaches /god-mode/ claims
# the instance. So every other service starts first, and the proxy starts only once the
# instance is set up. Otherwise it stays down (an old one is removed) and PLANE_PROXY_HELD=true
# tells deploy-plane.yml to complete setup against the API directly and run this script again.
compose_staged_up() {
  local others
  others=$(compose_services | grep -vx proxy)
  # shellcheck disable=SC2086  # one service name per word
  compose up -d --force-recreate --remove-orphans $others || return 1
  wait_for_healthy plane-api 600
  if instance_set_up; then
    compose up -d --force-recreate --no-deps proxy || return 1
  else
    info "Instance not set up yet: the proxy stays down until deploy-plane.yml completes setup."
    if $CONTAINER_ENGINE inspect plane-proxy >/dev/null 2>&1; then
      $CONTAINER_ENGINE rm -f plane-proxy >/dev/null || return 1
    fi
    echo "PLANE_PROXY_HELD=true"
  fi
}

step_start() {
  info "Step 3: Starting Plane..."
  # Recreates only when an input or image changed and prints DEPLOY_CHANGED for the playbook.
  # The migrator is a labelled one-shot: once it has exited 0 it is finished, not a change.
  # A held proxy is a missing container, so the run after setup recreates and starts it.
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
