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
# Steps (idempotent): verify .env present, pull, up (recreate only on input/image change),
# wait healthy. Prints DEPLOY_CHANGED=true|false; the playbook reads it for changed status.

set -euo pipefail

SKIP_PULL=false
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$(dirname "$(dirname "$(dirname "$SCRIPT_DIR")")")/lib"
cd "${SCRIPT_DIR}"

# shellcheck source=/dev/null
source "${LIB_DIR}/common.sh"

# Production Grafana must reach Authentik through the declared LAN Caddy host;
# public DNS sends its token exchange through Cloudflare's browser challenge.
if [ "${LOCAL_MODE:-}" != "true" ]; then
  COMPOSE_OVERLAYS="compose.prod.yml ${COMPOSE_OVERLAYS:-}"
fi

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
  info "Step 3: Starting o11y (prometheus + loki + alloy + grafana), recreating only on change..."
  # Grafana reads runtime config (admin pw, OIDC client settings) from `env_file: .env`, and
  # every service reads its config from ./config bind mounts at start. Neither is a
  # compose-spec change, so compose_up_if_changed (common.sh) hashes them with the compose
  # files in effect and recreates only when that digest or an image differs from what the
  # running containers started with. A re-run with identical inputs touches nothing.
  if [ "${O11Y_AVAILABILITY_ENABLED:-false}" = "true" ]; then
    local availability_helper="${SCRIPT_DIR}/../../../playbooks/files/summarize-o11y-scrape-baseline.py"
    [ -f "$availability_helper" ] || error "Availability runtime verifier is missing."
    export COMPOSE_CMD
    if ! O11Y_DEPLOYMENT_DIR="$SCRIPT_DIR" python3 "$availability_helper" --verify-availability-namespace --candidate >/dev/null; then
      error "Availability refused because the effective provider, candidate network configuration, or Linux namespace proof is unverified."
    fi
    info "Availability provider and current Linux namespace proof passed immediately before lifecycle apply."
  fi
  compose_up_if_changed config
  if [ "${O11Y_AVAILABILITY_ENABLED:-false}" = "true" ]; then
    if ! O11Y_DEPLOYMENT_DIR="$SCRIPT_DIR" O11Y_EXPECTED_NAMESPACE_SNAPSHOT_SHA256="" \
      python3 "$availability_helper" --verify-availability-namespace --candidate >/dev/null; then
      error "Availability post-apply Linux namespace proof is unverified; the running stack is preserved for review."
    fi
    info "Availability post-apply Linux namespace proof passed."
  fi
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
