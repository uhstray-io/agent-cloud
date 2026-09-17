#!/usr/bin/env bash
# agentgateway — container lifecycle only.
#
# Ansible's deploy-agentgateway.yml renders .env (upstream key from OpenBao) and
# config.yaml (identities as sha256 hashes, upstream, limits) BEFORE this runs.
# This script does NOT generate secrets — it pulls the image, starts the single
# container, and waits for the readiness listener to answer from the host.
#
# Why no `wait_for_healthy` on the GATEWAY: the image is a Chainguard glibc-dynamic
# base with no shell or curl, so a compose healthcheck cannot run inside it.
# Readiness is probed from the sibling db container over the compose network.
#
# Usage: ./deploy.sh [--no-pull]
# Steps (idempotent): verify rendered files, pull, up, wait db healthy, wait ready.

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

step_verify_rendered() {
  info "Step 1: Verifying rendered .env + config.yaml are present..."
  [ -f "${SCRIPT_DIR}/.env" ] || error "${SCRIPT_DIR}/.env not found. Run Ansible deploy-agentgateway.yml first."
  [ -f "${SCRIPT_DIR}/config.yaml" ] || error "${SCRIPT_DIR}/config.yaml not found. Run Ansible deploy-agentgateway.yml first."
  info "  both present."
}

step_pull_image() {
  if [ "$SKIP_PULL" = true ]; then info "Step 2: Skipping image pull (--no-pull)."; return 0; fi
  info "Step 2: Pulling image..."
  compose pull
}

step_start() {
  info "Step 3: Starting agentgateway..."
  # --force-recreate: the gateway reads config.yaml (a bind mount whose CONTENT
  # changes are not a compose-spec change) and VLLM_API_KEY from env_file. Plain
  # `up -d` would leave the old process running on the old identities/key.
  compose up -d --force-recreate
}

step_wait_db() {
  # The gateway dials Postgres at start (per-key budgets). depends_on gates the
  # start order; this makes the wait visible and bounded in the log.
  info "Step 4: Waiting for agentgateway-db to be healthy..."
  wait_for_healthy agentgateway-db 90
}

step_wait_ready() {
  # Probe from the sibling db container over the compose network: the gateway
  # image has no shell, and the host loopback is the WRONG vantage when this
  # script runs inside the local control plane (Semaphore container) rather than
  # on the host publishing the port. Works identically on the prod VM.
  info "Step 5: Waiting for the readiness listener (via agentgateway-db on the compose network)..."
  local elapsed=0
  while [ "$elapsed" -lt 90 ]; do
    if $CONTAINER_ENGINE exec agentgateway-db wget -q -O /dev/null -T 3 http://agentgateway:19001/healthz/ready 2>/dev/null; then
      info "agentgateway readiness is responding."
      return 0
    fi
    sleep 3; elapsed=$((elapsed + 3))
  done
  error "agentgateway readiness did not respond within 90s (${CONTAINER_ENGINE} logs agentgateway)"
}

main() {
  info "=== agentgateway deployment (container lifecycle) ==="
  detect_runtime
  info "Container engine: ${CONTAINER_ENGINE}"
  step_verify_rendered
  step_pull_image
  step_start
  step_wait_db
  step_wait_ready
  info "=== agentgateway container lifecycle complete ==="
}

main "$@"
