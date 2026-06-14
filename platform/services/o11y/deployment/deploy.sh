#!/usr/bin/env bash
# o11y — container lifecycle only.
#
# Ansible's deploy-o11y.yml templates .env (the Grafana admin password from
# OpenBao) and the committed config/ travels with the repo. This script does
# NOT generate secrets — it pulls, starts the stack (prometheus + loki + alloy +
# grafana), and waits for Grafana to report healthy (datasources + dashboards
# are provisioned on boot).
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
  [ -f "${SCRIPT_DIR}/.env" ] || error "${SCRIPT_DIR}/.env not found. Run Ansible deploy-o11y.yml first."
  info "  .env present."
}

step_pull_image() {
  if [ "$SKIP_PULL" = true ]; then info "Step 2: Skipping image pull (--no-pull)."; return 0; fi
  info "Step 2: Pulling images..."
  compose pull
}

step_start() {
  info "Step 3: Starting o11y (prometheus + loki + alloy + grafana)..."
  # --force-recreate: Grafana reads runtime config (admin pw, OIDC client
  # settings) from `env_file: .env`. An env_file content change is NOT a
  # compose-spec change, so plain `up -d` keeps the stale env — force-recreate
  # so re-rendered .env always applies. (Config files are bind-mounted ro and
  # also picked up on recreate.)
  compose up -d --force-recreate
}

step_wait_healthy() {
  info "Step 4: Waiting for Grafana to become healthy (datasources/dashboards provision on boot)..."
  wait_for_healthy o11y-grafana 180
}

main() {
  info "=== o11y deployment (container lifecycle) ==="
  detect_runtime
  info "Container engine: ${CONTAINER_ENGINE}"
  step_verify_env
  step_pull_image
  step_start
  step_wait_healthy
  info "=== o11y container lifecycle complete ==="
}

main "$@"
