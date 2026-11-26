#!/usr/bin/env bash
# plane-mcp — container lifecycle only.
#
# Plane's MCP server (makeplane/plane-mcp-server) runs as its own compose project, beside Plane. The
# server refuses to start without the OAuth client id and secret Plane issued to the registered app,
# so deploy-plane-mcp.yml must have rendered .env (from OpenBao) first, and Plane must be up.
#
# This script does NOT read or generate secrets. It checks the inputs, pulls the image, starts the
# container, and waits until the container is running and stays up.
#
# Usage: ./deploy.sh [--no-pull]
# Steps (idempotent): verify .env and Plane's network, pull, start, wait running.

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

# Publish overlay: production only. Local mode reaches the container by name over the local-dev network
# (compose.local.yml), so nothing is published to the host there. The test is the one compose() in
# common.sh uses for compose.local.yml, negated.
if [ "${LOCAL_MODE:-}" != "true" ] || [ ! -f compose.local.yml ]; then
  export COMPOSE_OVERLAYS="compose.publish.yml ${COMPOSE_OVERLAYS:-}"
fi

# container_field <name> <go-template>: one engine inspect value. Empty when the container is absent.
container_field() {
  $CONTAINER_ENGINE inspect --format "$2" "$1" 2>/dev/null || true
}

step_verify_inputs() {
  info "Step 1: Verifying the rendered .env and Plane's network are present..."
  [ -f "${SCRIPT_DIR}/.env" ] || error "${SCRIPT_DIR}/.env not found. Run Ansible deploy-plane-mcp.yml first."
  # plane_default is Plane's stack network (Plane is compose project `plane`). Without it there is no
  # api or plane-redis to reach, so refuse here with the reason instead of letting compose fail.
  $CONTAINER_ENGINE network inspect plane_default >/dev/null 2>&1 \
    || error "network plane_default not found. Deploy Plane first (deploy-plane.yml)."
  if [ "${LOCAL_MODE:-}" = "true" ] && [ -f compose.local.yml ]; then
    $CONTAINER_ENGINE network inspect local-dev >/dev/null 2>&1 \
      || error "network local-dev not found (compose.local.yml joins it in local mode)."
  fi
  info "  .env present; plane_default present."
}

step_pull_image() {
  if [ "$SKIP_PULL" = true ]; then info "Step 2: Skipping image pull (--no-pull)."; return 0; fi
  info "Step 2: Pulling image..."
  compose pull
}

step_start() {
  info "Step 3: Starting plane-mcp..."
  # env_file content is not a compose-spec change, so a plain `up -d` would leave the old process on
  # stale env. compose_up_if_changed hashes .env with the compose files and the overlays applied above,
  # and recreates only when that digest or the image changed. It prints DEPLOY_CHANGED=true|false.
  compose_up_if_changed
}

step_wait_running() {
  local name=plane-mcp timeout=120 elapsed=0 running restarts after
  info "Step 4: Waiting for ${name} to be running (timeout ${timeout}s)..."
  running=""
  while [ "$elapsed" -lt "$timeout" ]; do
    running=$(container_field "$name" '{{.State.Running}}')
    if [ "$running" = "true" ]; then break; fi
    sleep 2
    elapsed=$((elapsed + 2))
  done
  if [ "$running" != "true" ]; then
    dump_container_diagnostics "$name"
    error "${name} did not reach running within ${timeout}s"
  fi
  # A missing OAuth client id makes the server exit at start-up, and restart: always brings it back.
  # One running sample therefore proves little. It must stay up, with no restart, through a settle window.
  restarts=$(container_field "$name" '{{.RestartCount}}')
  sleep 15
  running=$(container_field "$name" '{{.State.Running}}')
  after=$(container_field "$name" '{{.RestartCount}}')
  if [ "$running" != "true" ] || [ "$restarts" != "$after" ]; then
    dump_container_diagnostics "$name"
    error "${name} did not stay running (restarts ${restarts} -> ${after}, running=${running})"
  fi
  info "  ${name} is running and stable (restarts: ${after})."
}

main() {
  info "=== plane-mcp deployment (container lifecycle) ==="
  detect_runtime
  info "Container engine: ${CONTAINER_ENGINE}"
  step_verify_inputs
  step_pull_image
  step_start
  step_wait_running
  info "=== plane-mcp container lifecycle complete ==="
}

main "$@"
