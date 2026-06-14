#!/usr/bin/env bash
# authentik — container lifecycle only.
#
# Ansible's deploy-authentik.yml templates .env (secret key, bootstrap admin,
# DB/Redis creds from OpenBao) and places the committed blueprints/ before this
# runs. This script does NOT generate secrets — it pulls, starts the stack
# (postgres + redis + server + worker), and waits for the server to report
# healthy (first boot runs DB migrations + applies blueprints, so allow time).
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
  [ -f "${SCRIPT_DIR}/.env" ] || error "${SCRIPT_DIR}/.env not found. Run Ansible deploy-authentik.yml first."
  info "  .env present."
}

step_pull_image() {
  if [ "$SKIP_PULL" = true ]; then info "Step 2: Skipping image pull (--no-pull)."; return 0; fi
  info "Step 2: Pulling images..."
  compose pull
}

step_start() {
  info "Step 3: Starting authentik (postgres + redis + server + worker)..."
  compose up -d
}

step_wait_healthy() {
  # First boot migrates the DB and applies blueprints — allow generous time.
  info "Step 4: Waiting for authentik-server to become healthy (first boot migrates)..."
  wait_for_healthy authentik-server 300
}

main() {
  info "=== authentik deployment (container lifecycle) ==="
  detect_runtime
  info "Container engine: ${CONTAINER_ENGINE}"
  step_verify_env
  step_pull_image
  step_start
  step_wait_healthy
  info "=== authentik container lifecycle complete ==="
}

main "$@"
