#!/usr/bin/env bash
# skynet — container lifecycle only.
#
# The skynet deploy playbook renders .env (skynet's own API bearer and database password from
# OpenBao, the Semaphore token and gateway client keys as shared reads, plus inventory values)
# BEFORE this runs. This script does NOT generate secrets — it pulls the image, starts skynet
# and its own Postgres, and waits for skynet to report healthy.
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
  [ -f "${SCRIPT_DIR}/.env" ] || error "${SCRIPT_DIR}/.env not found. Render it with the skynet deploy playbook first."
  info "  .env present."
}

step_pull_images() {
  if [ "$SKIP_PULL" = true ]; then info "Step 2: Skipping image pull (--no-pull)."; return 0; fi
  info "Step 2: Pulling images..."
  if ! compose pull; then
    # The skynet image is pinned by digest and may not be in the registry yet (local-dev
    # bring-up on a locally built image). Only that case is tolerated; a pull failure with NO
    # local image is still fatal.
    local img
    img=$(grep -E '^SKYNET_IMAGE=' .env | cut -d= -f2-)
    # env.j2 always renders SKYNET_IMAGE; an empty value is a templating bug, not a
    # pull-tolerance case — fail loud rather than probe a bogus ref.
    [ -n "$img" ] || error "compose pull failed and SKYNET_IMAGE is missing from .env."
    if "${CONTAINER_ENGINE}" image exists "$img"; then
      info "  pull failed but ${img} is in the local store — continuing."
    else
      error "compose pull failed and ${img} is not present locally."
    fi
  fi
}

step_start() {
  info "Step 3: Starting skynet (orchestration API + checkpoint database)..."
  # skynet reads its runtime config from `env_file: .env`. An env_file content change is NOT a
  # compose-spec change, so plain `up -d` would leave the old containers running with stale env.
  # compose_up_if_changed hashes .env with the compose files and recreates only when that digest
  # or an image changed, and prints DEPLOY_CHANGED=true|false for the playbook.
  # certs/ is hashed only where it exists: the local overlay mounts it, production does not, and
  # a missing input is an error to the helper, not an empty input.
  if [ -d certs ]; then
    compose_up_if_changed certs
  else
    compose_up_if_changed
  fi
}

step_wait_healthy() {
  info "Step 4: Waiting for skynet to become healthy..."
  wait_for_healthy skynet 120
}

# Staged start (Rule 4, plan/architecture/05-platform-infra.md): the checkpoint database first,
# gated on its own healthcheck, then the API. skynet's depends_on: service_healthy would also order
# them, but the rule is explicit staging, not reliance on a compose condition. --no-deps keeps the
# second up from touching skynet-db again. common.sh calls this in place of the one-shot up when the
# inputs changed; the hook owns recreating, ordering and orphan removal, and each command chains
# `|| return 1` because the helper runs it with errexit suspended.
compose_staged_up() {
  compose up -d --force-recreate --remove-orphans skynet-db || return 1
  wait_for_healthy skynet-db 120
  compose up -d --force-recreate --no-deps skynet || return 1
}

main() {
  info "=== skynet deployment (container lifecycle) ==="
  detect_runtime
  info "Container engine: ${CONTAINER_ENGINE}"
  step_verify_env
  step_pull_images
  step_start
  step_wait_healthy
  info "=== skynet container lifecycle complete ==="
}

main "$@"
