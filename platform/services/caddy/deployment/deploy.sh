#!/usr/bin/env bash
# Caddy reverse proxy — container lifecycle only.
#
# Ansible's deploy-caddy.yml renders the compose .env (and, in local-dev, a
# Caddyfile.local) from inventory vars before this runs. This script does NOT
# render config or manage secrets.
#
# Usage:
#   ./deploy.sh [--no-pull]
#
# Required on disk before this runs:
#   Caddyfile  (prod, committed)  OR  Caddyfile.local  (local-dev, rendered)
#   .env       (optional — compose falls back to prod defaults if absent)
#
# Steps (idempotent): verify a Caddyfile is present, pull, up, wait healthy.

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

step_verify_config() {
  info "Step 1: Verifying a Caddyfile is present..."
  if [ ! -f "${SCRIPT_DIR}/Caddyfile" ] && [ ! -f "${SCRIPT_DIR}/Caddyfile.local" ]; then
    error "No Caddyfile or Caddyfile.local found. Run Ansible deploy-caddy.yml first."
  fi
  info "  Caddyfile present."
}

step_pull_image() {
  if [ "$SKIP_PULL" = true ]; then
    info "Step 2: Skipping image pull (--no-pull)."
    return 0
  fi
  info "Step 2: Pulling image..."
  compose pull
}

step_start() {
  # --force-recreate: the Caddyfile is a SINGLE-FILE bind mount, and Ansible
  # renders it via atomic rename (new inode), so a plain `compose up -d` (a
  # no-op on an already-running container) keeps serving the stale inode and a
  # `caddy reload` re-reads that same stale file. Recreating re-binds the mount
  # to the freshly rendered file. Idempotent; brief restart only.
  info "Step 3: Starting Caddy (force-recreate to pick up Caddyfile changes)..."
  compose up -d --force-recreate
}

step_wait_healthy() {
  info "Step 4: Waiting for Caddy to become healthy..."
  wait_for_healthy caddy 60
}

main() {
  info "=== Caddy deployment (container lifecycle) ==="
  detect_runtime
  info "Container engine: ${CONTAINER_ENGINE}"

  step_verify_config
  step_pull_image
  step_start
  step_wait_healthy

  info "=== Caddy container lifecycle complete ==="
}

main "$@"
