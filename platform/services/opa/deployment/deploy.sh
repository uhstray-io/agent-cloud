#!/usr/bin/env bash
# OPA — container lifecycle only.
#
# Phase 1 has no secrets (OPA returns decisions, not credentials) and runs
# unauthenticated on the internal network, so there is no .env to template — the
# Rego policies under ./policies are the config, mounted read-only. This script
# does NOT generate secrets; it pulls, starts the server, and confirms the
# container is running. The deploy playbook verifies the HTTP API + runs the
# Rego unit tests. Plan: plan/development/OPA-INTEGRATION-PLAN.md.
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

main() {
  info "=== OPA deployment (container lifecycle) ==="
  detect_runtime
  info "Container engine: ${CONTAINER_ENGINE}"
  if [ "$SKIP_PULL" = true ]; then info "Skipping image pull (--no-pull)."; else compose pull; fi
  compose up -d
  # The OPA image is distroless (no shell/wget for a compose healthcheck), so
  # poll the container is running; the playbook gates on GET /health.
  info "Waiting for the opa container to be running..."
  running=false
  for _ in $(seq 1 20); do
    if [ "$(${CONTAINER_ENGINE} inspect -f '{{.State.Status}}' opa 2>/dev/null)" = running ]; then
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
