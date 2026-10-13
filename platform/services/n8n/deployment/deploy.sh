#!/usr/bin/env bash
# n8n — container lifecycle only (composable pattern).
#
# deploy-n8n.yml templates .env (DB creds + N8N_ENCRYPTION_KEY from OpenBao via
# manage-secrets) BEFORE this runs. This script does NOT generate secrets — it
# pulls + starts the 4-container stack (postgres, redis, n8n, worker) and waits
# for postgres healthy. App bootstrap (owner / API key) is NOT here: the
# existing instance already has it in the DB (and the encryption key is
# preserved across the migration); greenfield owner-setup is a later step.
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

# compose_staged_up — the ordered start compose_up_if_changed calls when the project must be
# recreated (it has already put the digest label overlay in COMPOSE_OVERLAYS, so every `up`
# here labels what it creates).
# The worker must NOT boot while the app is still running its DB migrations:
# two n8n processes racing one migration chain half-apply it (seen live in
# the prod cutover — a crash mid-chain left a column added but unrecorded,
# and every later boot failed on "already exists"). compose.yml declares the
# ordering (worker depends_on n8n: service_healthy), but depends_on
# conditions cannot be relied on across engines (prod podman-compose 1.0.6
# ignores them), so the serialization is ALSO enforced here: infrastructure +
# app first, gate on the app's readiness healthcheck (/healthz/readiness —
# migrations done), then the worker. 300s covers a long cross-version
# migration chain.
# Removing the old containers first is the recreate: podman-compose 1.0.6 cannot replace a
# container that has dependents in place, and it makes `up` start every stage fresh. The
# named volumes (database, redis, n8n_data) are not touched — no -v.
# No --remove-orphans on `down`: the compose project name is this directory's basename
# (`deployment`, there is no `name:` and adding one would rename the volumes), which other
# services' compose files share, and podman-compose removes every container carrying the
# project label — another service's included. The four containers this stack owns are removed
# by name below instead. n8n always starts through this hook, so common.sh's one-shot
# `up --remove-orphans` (the same project-wide scope) never runs for it.
compose_staged_up() {
  compose down || info "WARN: compose down failed; removing the containers by name."
  local c
  for c in workflow-n8n-worker workflow-n8n workflow-n8n-redis workflow-n8n-postgres; do
    $CONTAINER_ENGINE rm -f "$c" >/dev/null 2>&1 || true
  done
  compose up -d n8n-postgres n8n-redis n8n || return 1
  wait_for_healthy workflow-n8n-postgres 120
  wait_for_healthy workflow-n8n 300
  compose up -d n8n-worker || return 1
}

main() {
  info "=== n8n deployment (container lifecycle) ==="
  detect_runtime
  info "Container engine: ${CONTAINER_ENGINE}"
  [ -f .env ] || error ".env not found — run deploy-n8n.yml (manage-secrets) first."
  # Pull-tolerant: a registry rate-limit / transient offline must not block a
  # deploy when the images are already cached (notably the prod cutover, where
  # n8n's image is already present). `compose up` still fails clearly if an image
  # is genuinely missing.
  if [ "$SKIP_PULL" = true ]; then
    info "Skipping image pull (--no-pull)."
  else
    compose pull || info "WARN: image pull failed (registry rate-limit/offline) — continuing with cached images."
  fi
  # Recreate only when an input or image changed (common.sh compose_up_if_changed, operator
  # decision 2026-10-04: an unchanged re-run is a true no-op). It hashes the compose files,
  # .env and the postgres init script the compose file bind-mounts (config/n8n-init-data.sh;
  # NOT config/n8n.env, the legacy live env the cutover guard reads), so a re-rendered .env
  # reaches the running processes — plain `up -d` left them on stale env — and prints
  # DEPLOY_CHANGED=true|false for deploy-n8n.yml. The ordered start below is the helper's
  # compose_staged_up hook, so it runs only when something changed.
  compose_up_if_changed config/n8n-init-data.sh
  # Readiness gate on every run, changed or not: an unchanged stack that is running but not
  # ready must still fail the deploy here rather than later in the verify phase.
  wait_for_healthy workflow-n8n 300
  info "=== n8n container lifecycle complete ==="
}

main "$@"
