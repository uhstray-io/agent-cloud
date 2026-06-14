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
  compose up -d
  # postgres + redis carry healthchecks; gate on postgres (n8n waits on it via
  # depends_on). The deploy playbook verifies n8n's /healthz endpoint.
  wait_for_healthy workflow-n8n-postgres 120
  info "=== n8n container lifecycle complete ==="
}

main "$@"
