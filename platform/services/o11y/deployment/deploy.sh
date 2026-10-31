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
  local availability_helper="${SCRIPT_DIR}/../../../playbooks/files/summarize-o11y-scrape-baseline.py"
  # Grafana reads runtime config (admin pw, OIDC client settings) from `env_file: .env`, and
  # every service reads its config from ./config bind mounts at start. Neither is a
  # compose-spec change, so compose_up_if_changed (common.sh) hashes them with the compose
  # files in effect and recreates only when that digest or an image differs from what the
  # running containers started with. A re-run with identical inputs touches nothing.
  if [ "${O11Y_AVAILABILITY_ENABLED:-false}" = "true" ]; then
    [ -f "$availability_helper" ] || error "Availability runtime verifier is missing."
    export COMPOSE_CMD
    if ! O11Y_DEPLOYMENT_DIR="$SCRIPT_DIR" python3 "$availability_helper" --verify-availability-namespace --candidate >/dev/null; then
      error "Availability refused because the effective provider, candidate network configuration, or Linux namespace proof is unverified."
    fi
    info "Availability provider and current Linux namespace proof passed immediately before lifecycle apply."
  fi
  if [ "${O11Y_AVAILABILITY_ENABLED:-false}" = "true" ] && [ -z "${O11Y_AVAILABILITY_SOURCE_FILE:-}" ]; then
    error "Availability refused because the reviewed current-source declaration is missing."
  fi
  if [ "${O11Y_AVAILABILITY_ENABLED:-false}" = "true" ]; then
    export TMPDIR=/tmp
  fi
  if [ -n "${O11Y_AVAILABILITY_SOURCE_FILE:-}" ]; then
    [[ -f "$availability_helper" && -f "$O11Y_AVAILABILITY_SOURCE_FILE" ]] \
      || error "The reviewed current-source input is unavailable."
    local availability_preapply_source_receipt
    if ! availability_preapply_source_receipt=$(O11Y_DEPLOYMENT_DIR="$SCRIPT_DIR" \
      python3 "$availability_helper" --verify-availability-source \
      < "$O11Y_AVAILABILITY_SOURCE_FILE"); then
      error "The current Alloy self-metrics producer is not uniquely verified."
    fi
    local availability_preapply_binding
    availability_preapply_binding=$(printf '%s' "$availability_preapply_source_receipt" | python3 -c '
import json, re, sys
try:
    value = json.load(sys.stdin)
except Exception:
    raise SystemExit(1)
if not isinstance(value, dict):
    raise SystemExit(1)
source = value.get("source")
container_sha256 = value.get("container_identity_sha256")
if (value.get("status") != "observed" or source not in {"prometheus", "alloy"}
        or not isinstance(container_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", container_sha256) is None):
    raise SystemExit(1)
print(source, container_sha256)
') || error "The current Alloy self-metrics source is unverified."
    local availability_preapply_source availability_preapply_container_sha256
    read -r availability_preapply_source availability_preapply_container_sha256 <<< "$availability_preapply_binding"
    printf 'O11Y_AVAILABILITY_PREAPPLY_SOURCE=%s\n' "$availability_preapply_source"
    printf 'O11Y_AVAILABILITY_PREAPPLY_CONTAINER_SHA256=%s\n' "$availability_preapply_container_sha256"
    local availability_preapply_epoch
    availability_preapply_epoch=$(date -u +%s) \
      || error "Cannot record the source-transition apply boundary."
    [[ "$availability_preapply_epoch" =~ ^[1-9][0-9]{8,}$ ]] \
      || error "Cannot record the source-transition apply boundary."
    printf 'O11Y_AVAILABILITY_PREAPPLY_EPOCH=%s\n' "$availability_preapply_epoch"
    compose_staged_up() {
      if [ "${O11Y_AVAILABILITY_ENABLED:-false}" = "true" ]; then
        if ! O11Y_DEPLOYMENT_DIR="$SCRIPT_DIR" O11Y_AVAILABILITY_FINAL_START=true \
          python3 "$availability_helper" --verify-availability-namespace --candidate >/dev/null; then
          error "Availability final pre-start namespace and effective-compose proof is unverified."
        fi
      fi
      compose up -d --force-recreate --remove-orphans || return 1
    }
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
