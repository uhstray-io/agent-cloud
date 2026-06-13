#!/usr/bin/env bash
# hickory-dns — container lifecycle only.
#
# This script does NOT render config or generate secrets. Ansible's
# deploy-dns.yml is responsible for:
#   1. Rendering config/named.toml + config/zones/<zone>.zone + .env from
#      inventory vars (NO OpenBao in Phase 1 — DNS has no runtime credentials)
#   2. Running this script
#   3. Verifying resolution with dig
#
# Usage:
#   ./deploy.sh [--no-pull]
#
# Required on disk before this script runs:
#   config/named.toml        — rendered by Ansible
#   config/zones/*.zone      — rendered by Ansible
#   .env                     — compose interpolation vars (non-secret)
#
# Steps (all idempotent):
#   1. Verify rendered config present (fail fast if Ansible didn't run)
#   2. Pull image (unless --no-pull)
#   3. Start the container
#   4. Wait for the container to become healthy

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
    *)
      echo "Unknown option: $arg"
      echo "Usage: ./deploy.sh [--no-pull]"
      exit 1
      ;;
  esac
done

step_verify_config() {
  info "Step 1: Verifying rendered config is present..."
  if [ ! -f "${SCRIPT_DIR}/config/named.toml" ]; then
    error "${SCRIPT_DIR}/config/named.toml not found. Run Ansible deploy-dns.yml first; it renders config from inventory vars."
  fi
  info "  config/named.toml present."
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
  info "Step 3: Starting hickory-dns..."
  compose up -d
}

step_wait_healthy() {
  info "Step 4: Waiting for hickory-dns to become healthy..."
  wait_for_healthy dns 60
}

main() {
  info "=== hickory-dns deployment (container lifecycle) ==="
  detect_runtime
  info "Container engine: ${CONTAINER_ENGINE}"

  step_verify_config
  step_pull_image
  step_start
  step_wait_healthy

  info "=== hickory-dns container lifecycle complete ==="
}

main "$@"
