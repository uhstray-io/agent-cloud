#!/usr/bin/env bash
# UhhCraft — post-deploy bootstrap.
#
# Runs migrations and verifies the app is fully serving. Idempotent.
# Invoked by deploy-uhhcraft.yml after deploy.sh completes.
#
# Expects:
#   .env present (DATABASE_URL exported when sourced)
#   The app container is up (deploy.sh has run)
#   The host has the goose CLI installed (or we exec it inside the app container)
#
# Order matters: goose first (creates schema), River second (creates its own tables).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$(dirname "$(dirname "$(dirname "$SCRIPT_DIR")")")/lib"
cd "${SCRIPT_DIR}"

# shellcheck source=/dev/null
source "${LIB_DIR}/common.sh"

UHHCRAFT_URL="${UHHCRAFT_URL:-http://localhost:3000}"

step_load_env() {
  info "Step 1: Loading .env..."
  if [ ! -f "${SCRIPT_DIR}/.env" ]; then
    error ".env not found. deploy.sh and Ansible templating must run first."
  fi
  # shellcheck source=/dev/null
  set -o allexport
  source "${SCRIPT_DIR}/.env"
  set +o allexport
  : "${DATABASE_URL:?DATABASE_URL must be set in .env}"
}

step_goose_migrate() {
  info "Step 2: Running goose migrations..."
  # Always run goose INSIDE the app container (the image bundles /app/goose and
  # /app/db/migrations). DATABASE_URL points at the compose-internal host
  # `postgres:5432`, which only resolves on the compose network — a host-side
  # goose would fail to connect. This also keeps the migrator version identical
  # to what shipped in the image.
  detect_runtime
  compose exec -T app /app/goose -dir /app/db/migrations postgres "${DATABASE_URL}" up \
    || error "goose migrate failed. Ensure the app container is up and DATABASE_URL is reachable from it. Deploy aborted."
}

step_river_migrate() {
  info "Step 3: Running River migrations..."
  detect_runtime
  # River ships its own migration CLI compiled into the app binary
  # (`uhhcraft river migrate-up`). River does NOT create its tables lazily on
  # first enqueue — if this step fails, job processing is broken, so fail the
  # deploy here rather than letting it surface as runtime errors later.
  compose exec -T app /app/uhhcraft river migrate-up \
    || error "River migrate-up failed. The app binary must expose 'river migrate-up' and DATABASE_URL must be reachable. Deploy aborted."
}

step_healthcheck() {
  info "Step 4: Verifying UhhCraft responds..."
  check_http "${UHHCRAFT_URL}/healthz" "UhhCraft healthz"
}

main() {
  info "=== UhhCraft post-deploy bootstrap ==="
  step_load_env
  step_goose_migrate
  step_river_migrate
  step_healthcheck
  info "=== UhhCraft post-deploy complete ==="
}

main "$@"
