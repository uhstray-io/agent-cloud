#!/usr/bin/env bash
# ERPNext — container lifecycle only (composable pattern).
#
# deploy-erpnext.yml templates .env (DB root/admin passwords from OpenBao via
# manage-secrets) BEFORE this runs. This script does NOT generate or read
# secrets from OpenBao — it pulls images and stages container startup, then
# leaves the site unborn. App bootstrap (site creation) is post-deploy.sh, NOT
# here (and NOT run by this script).
#
# Staged startup REPLACES `depends_on:` conditions: podman-compose 1.0.6 ignores
# them, so we bring up the backing tier, wait for it healthy, run the one-shot
# configurator, then start the app tier.
#
# Usage: ./deploy.sh [--no-pull]
#
# Required on disk before this script runs:
#   .env  — templated by Ansible (deploy-erpnext.yml) from OpenBao

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
  [ -f "${SCRIPT_DIR}/.env" ] || error ".env not found — run deploy-erpnext.yml (manage-secrets) first."
  info "  .env present."
}

step_pull_images() {
  if [ "$SKIP_PULL" = true ]; then
    info "Step 2: Skipping image pull (--no-pull)."
    return 0
  fi
  info "Step 2: Pulling images..."
  # Pull-tolerant: a registry rate-limit / transient offline must not block a
  # deploy when the images are already cached. `compose up` still fails clearly
  # if an image is genuinely missing.
  compose pull || info "WARN: image pull failed (registry rate-limit/offline) — continuing with cached images."
}

step_start_backing() {
  info "Step 3: Starting backing services (db, redis-cache, redis-queue)..."
  compose up -d db redis-cache redis-queue
  # podman-compose 1.0.6 ignores depends_on conditions — wait explicitly.
  wait_for_healthy "erpnext-db" 120
  wait_for_healthy "erpnext-redis-cache" 60
  wait_for_healthy "erpnext-redis-queue" 60
}

step_configure() {
  info "Step 4: Running one-shot configurator (writes common_site_config.json)..."
  compose run --rm configurator
}

step_start_app() {
  info "Step 5: Starting app tier (backend, websocket, queue, scheduler, frontend)..."
  compose up -d backend websocket queue scheduler frontend
  wait_for_healthy "erpnext-backend" 120
}

main() {
  info "=== ERPNext deployment (container lifecycle) ==="
  detect_runtime
  info "Container engine: ${CONTAINER_ENGINE}"

  step_verify_env
  step_pull_images
  step_start_backing
  step_configure
  step_start_app

  info "=== ERPNext container lifecycle complete ==="
  info "Next: run post-deploy.sh for site bootstrap (frontend reports healthy after it)."
}

main "$@"
