#!/usr/bin/env bash
# OPA — container lifecycle only.
#
# OPA has no secrets (it returns decisions, not credentials) and no login: the callers the
# host firewall admits reach it, and the authorization policy (policies/system/authz.rego)
# limits them to decisions and health. So there is no secret .env to template: the Rego policies
# under ./policies are the config, mounted read-only, and the .env this script writes holds only
# the compose substitutions (write_env). This script
# does NOT generate secrets; it pulls, starts the server, and confirms the
# container is running. The deploy playbook verifies the HTTP API + runs the
# Rego unit tests. Plan: plan/development/03-guardrails-governance.md (OPA section).
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

# write_env: .env holds the values the compose files substitute, so compose_up_if_changed (which
# hashes .env) sees a changed image, log level, bind, port or leaf name. Only values the caller
# set are written; an unset one keeps its compose default. None is a secret: OPA has none.
write_env() {
  local v
  : > .env
  for v in OPA_IMAGE OPA_LOG_LEVEL OPA_BIND OPA_PORT OPA_SERVER_LEAF; do
    if [ -n "${!v:-}" ]; then printf '%s=%s\n' "$v" "${!v}" >> .env; fi
  done
}

main() {
  info "=== OPA deployment (container lifecycle) ==="
  detect_runtime
  info "Container engine: ${CONTAINER_ENGINE}"
  # Before the pull: compose.yml takes the image from OPA_IMAGE, which .env also carries.
  write_env
  if [ "$SKIP_PULL" = true ]; then info "Skipping image pull (--no-pull)."; else compose pull; fi
  # OPA reads ./policies once, at startup (--bundle, no --watch). Plain `up -d` leaves a running
  # container alone when only the bind-mounted files changed, so a deploy that placed new policies
  # left the server answering from the old ones: production served its 2026-11-01 policies until
  # 2026-11-12 (docs/MISTAKES.md 3.11). compose_up_if_changed hashes the policy tree, the compose
  # files and .env into the container label and recreates OPA when any of them differs.
  compose_up_if_changed policies
  # The OPA image is distroless (no shell/wget for a compose healthcheck), so
  # poll the container is running; the playbook gates on GET /health.
  info "Waiting for the opa container to be running..."
  running=false
  for _ in $(seq 1 20); do
    if [ "$(${CONTAINER_ENGINE} inspect --format '{{.State.Status}}' opa 2>/dev/null)" = running ]; then
      info "opa running."
      running=true
      break
    fi
    sleep 2
  done
  if [ "$running" != true ]; then
    error "OPA container did not reach running state within timeout."
  fi
  info "=== OPA container lifecycle complete ==="
}

main "$@"
