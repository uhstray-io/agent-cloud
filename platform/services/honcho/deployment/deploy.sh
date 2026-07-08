#!/usr/bin/env bash
# honcho — container lifecycle only.
#
# Ansible's deploy-honcho.yml templates .env (JWT secret + DB password from
# OpenBao, Gemini key shared-read from nemoclaw) BEFORE this runs. This script
# does NOT generate secrets — it pulls the GHCR-built image, starts the four
# rootless containers (api, deriver, db, redis), and waits for the api to
# report healthy (first boot runs the alembic migrations, incl. the pgvector
# extension, so allow some time).
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
  [ -f "${SCRIPT_DIR}/.env" ] || error "${SCRIPT_DIR}/.env not found. Run Ansible deploy-honcho.yml first."
  info "  .env present."
}

step_pull_images() {
  if [ "$SKIP_PULL" = true ]; then info "Step 2: Skipping image pull (--no-pull)."; return 0; fi
  info "Step 2: Pulling images..."
  if ! compose pull; then
    # The api/deriver image is CI-published to GHCR. Before the first publish
    # (local-dev bring-up on a locally-built image) — or during a registry
    # outage — the pull fails even though a usable image is already in the
    # local store. Only that case is tolerated; a pull failure with NO local
    # image is still fatal.
    local img
    img=$(grep -E '^HONCHO_IMAGE=' .env | cut -d= -f2-)
    img=${img:-ghcr.io/uhstray-io/honcho:v3.0.11}
    if "${CONTAINER_ENGINE}" image exists "$img"; then
      info "  pull failed but ${img} is in the local store — continuing."
    else
      error "compose pull failed and ${img} is not present locally."
    fi
  fi
}

step_start() {
  info "Step 3: Starting honcho (api + deriver + db + redis)..."
  # --force-recreate: honcho reads runtime config (JWT secret, DB URI, LLM
  # keys) from `env_file: .env`. An env_file content change is NOT a
  # compose-spec change, so plain `up -d` leaves the old containers running
  # with stale env. Force-recreate so a re-rendered .env always applies.
  compose up -d --force-recreate
}

step_wait_healthy() {
  # First boot runs the alembic migrations (schema + pgvector extension)
  # through the api entrypoint before fastapi answers — allow extra time.
  info "Step 4: Waiting for honcho-api to become healthy (first boot runs migrations)..."
  wait_for_healthy honcho-api 180
}

main() {
  info "=== honcho deployment (container lifecycle) ==="
  detect_runtime
  info "Container engine: ${CONTAINER_ENGINE}"
  step_verify_env
  step_pull_images
  step_start
  step_wait_healthy
  info "=== honcho container lifecycle complete ==="
}

main "$@"
