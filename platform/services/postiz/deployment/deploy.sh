#!/usr/bin/env bash
# postiz — container lifecycle only.
#
# Ansible's deploy-postiz.yml renders BOTH env files from OpenBao BEFORE this
# runs:
#   .env               compose-substitution values (image pins, bind/port, the
#                      two Postgres passwords)
#   config/postiz.env  the app's own config, bind-mounted read-only into the
#                      container at /config/postiz.env (upstream "Option B")
#
# This script does NOT generate or read secrets — it pulls images, starts the
# five rootless containers (app, its Postgres + Redis, the workflow engine and
# the engine's Postgres), and waits for the app to report healthy. First boot
# runs both the app's Prisma migrations and the workflow engine's schema setup,
# so allow generous time.
#
# Usage: ./deploy.sh [--no-pull]
# Steps (idempotent): verify both env files, pull, up, wait healthy.

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
  info "Step 1: Verifying templated env files are present..."
  [ -f "${SCRIPT_DIR}/.env" ] || error "${SCRIPT_DIR}/.env not found. Run Ansible deploy-postiz.yml first."
  # The app config is a BIND MOUNT. If it is missing, the container runtime
  # would create a DIRECTORY at that path rather than failing, and postiz would
  # start with no configuration at all — a confusing downstream failure. Fail
  # here instead, where the cause is obvious.
  [ -f "${SCRIPT_DIR}/config/postiz.env" ] || \
    error "${SCRIPT_DIR}/config/postiz.env not found. Run Ansible deploy-postiz.yml first (it renders this file; a missing bind source would silently become a directory)."
  info "  .env and config/postiz.env present."
}

step_pull_images() {
  if [ "$SKIP_PULL" = true ]; then info "Step 2: Skipping image pull (--no-pull)."; return 0; fi
  info "Step 2: Pulling images..."
  # All five images are public upstream images — no registry login needed, and
  # a pull failure here is a genuine problem rather than the private-GHCR case
  # some other services tolerate.
  compose pull
}

step_start() {
  info "Step 3: Starting postiz (app + postgres + redis + temporal + temporal-postgresql)..."
  # --force-recreate: the app reads its runtime config from a bind-mounted file.
  # A change to that file's CONTENT is not a compose-spec change, so a plain
  # `up -d` would leave the running container with stale configuration loaded.
  # Force-recreate so a re-rendered config always takes effect.
  compose up -d --force-recreate
}

step_wait_healthy() {
  # First boot runs the app's Prisma migrations AND the workflow engine's schema
  # provisioning before the app answers — allow well beyond a warm restart.
  info "Step 4: Waiting for postiz to become healthy (first boot runs migrations + workflow schema setup)..."
  wait_for_healthy postiz 300
}

main() {
  info "=== postiz deployment (container lifecycle) ==="
  detect_runtime
  info "Container engine: ${CONTAINER_ENGINE}"
  step_verify_env
  step_pull_images
  step_start
  step_wait_healthy
  info "=== postiz container lifecycle complete ==="
}

main "$@"
