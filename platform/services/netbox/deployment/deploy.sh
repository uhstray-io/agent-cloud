#!/bin/bash
# NetBox — Infrastructure deployment (container operations only)
#
# Usage:
#   ./deploy.sh [--no-pull] [NETBOX_URL]
#
# Secrets and env files are managed by Ansible (deploy-netbox.yml).
# This script only handles container lifecycle.
#
# Steps (all idempotent):
#   1. Clone/update netbox-docker upstream repository
#   2. Copy .example templates (non-secret config only)
#   3. Verify env files present (fail if Ansible didn't run)
#   4. Pull latest images (unless --no-pull)
#   5. Build custom NetBox image with plugins
#   6. Stop the stack gracefully
#   7. Sync DB passwords to existing Postgres volume
#   8. Start services (staged: backing → Hydra → application)
#   9. Wait for NetBox to become healthy

set -euo pipefail

SKIP_PULL=false
NETBOX_URL=""
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

for arg in "$@"; do
  case "$arg" in
    --no-pull) SKIP_PULL=true ;;
    http://*|https://*) NETBOX_URL="$arg" ;;
    *)
      echo "Unknown option: $arg"
      echo "Usage: ./deploy.sh [--no-pull] [NETBOX_URL]"
      exit 1
      ;;
  esac
done

NETBOX_URL="${NETBOX_URL:-http://localhost:8000}"
DEFAULT_TIMEOUT=300  # 5 minutes

# Source shared library
source "${SCRIPT_DIR}/lib/common.sh"

# ─── Preflight checks ──────────────────────────────────────────────
# Container runtime check is handled by lib/common.sh (CONTAINER_ENGINE)
command -v openssl >/dev/null 2>&1 || error "openssl is not installed (needed for password generation)"
command -v git >/dev/null 2>&1 || error "git is not installed"
[ -f "docker-compose.yml" ] || error "docker-compose.yml not found. Run this script from the netbox/ directory."

# ─── Step 1: Update/clone netbox-docker upstream repo ──────────────
info "Step 1/9: Updating netbox-docker upstream repository..."
NETBOX_DOCKER_DIR="${SCRIPT_DIR}/netbox-docker"
NETBOX_DOCKER_REPO="https://github.com/netbox-community/netbox-docker.git"
NETBOX_DOCKER_BRANCH="release"

if [ -d "${NETBOX_DOCKER_DIR}/.git" ]; then
  git -C "${NETBOX_DOCKER_DIR}" fetch origin "${NETBOX_DOCKER_BRANCH}" --quiet
  LOCAL=$(git -C "${NETBOX_DOCKER_DIR}" rev-parse HEAD)
  REMOTE=$(git -C "${NETBOX_DOCKER_DIR}" rev-parse "origin/${NETBOX_DOCKER_BRANCH}")
  if [ "$LOCAL" != "$REMOTE" ]; then
    git -C "${NETBOX_DOCKER_DIR}" pull --ff-only origin "${NETBOX_DOCKER_BRANCH}" --quiet
    info "  Updated to $(git -C "${NETBOX_DOCKER_DIR}" log --oneline -1)"
  else
    info "  Already up to date."
  fi
else
  info "Cloning netbox-docker upstream repository..."
  git clone --branch "${NETBOX_DOCKER_BRANCH}" "${NETBOX_DOCKER_REPO}" "${NETBOX_DOCKER_DIR}"
  info "  Cloned branch ${NETBOX_DOCKER_BRANCH}."
fi

# ─── Step 2: Copy .example templates ────────────────────────────────
info "Step 2/9: Ensuring templates..."
copy_example_templates

# ─── Step 3/9: Verify env files ───────────────────────────────────────
# Secrets and env files are managed by Ansible (deploy-netbox.yml):
#   Ansible fetches/generates secrets via OpenBao → templates env files directly.
#   No secrets/ directory. No generate-secrets.sh.
#
# deploy.sh verifies env files exist but does NOT generate or manage them.
info "Step 3/9: Verifying env files..."
chmod +x discovery/init-db.sh
REQUIRED_FILES=".env env/netbox.env env/postgres.env env/discovery.env discovery/hydra.yaml"
for f in $REQUIRED_FILES; do
  [ -f "$f" ] && [ -s "$f" ] || \
    error "${f} missing or empty. Deploy via Semaphore (Ansible templates env files from OpenBao)."
done
info "  All required env files present."

# ─── Step 4: Pull latest images ────────────────────────────────────
if [ "$SKIP_PULL" = false ]; then
  info "Step 4/9: Pulling latest upstream images..."
  # --ignore-buildable is supported by docker compose but not podman-compose.
  # Fall back to pulling explicit services (excluding locally-built netbox/netbox-worker).
  compose pull --ignore-buildable 2>/dev/null || \
    compose pull postgres redis redis-cache ingress-nginx \
      diode-ingester diode-reconciler diode-auth hydra diode-redis
else
  info "Step 4/9: Skipping image pull (--no-pull)"
fi

# ─── Step 5: Build custom NetBox image ──────────────────────────────
info "Step 5/9: Building custom NetBox image with plugins..."
build_netbox_image

# ─── Step 6: Stop the stack ─────────────────────────────────────────
# podman-compose down may silently leave containers behind when pod dependency
# chains block removal (e.g., after a volume-mount path change). If any project
# containers remain after compose down, force-remove them so step 8 creates
# fresh containers from the current compose file.
info "Step 6/9: Stopping services..."
stop_orb_agent 2>/dev/null || true
compose down 2>&1 || true
leftover=$($CONTAINER_ENGINE ps -a --format '{{.Names}}' 2>/dev/null | grep "^netbox${CONTAINER_SEP}" || true)
if [ -n "$leftover" ]; then
  warn "Stale containers remain after compose down — force-removing..."
  echo "$leftover" | xargs $CONTAINER_ENGINE rm -f 2>/dev/null || true
  # Remove orphaned pod/network if present
  $CONTAINER_ENGINE pod rm -f "pod_netbox" 2>/dev/null || true
  $CONTAINER_ENGINE network rm "netbox_default" 2>/dev/null || true
fi

# ─── Step 7: Sync DB passwords to existing Postgres volume ─────────
info "Step 7/9: Checking for existing Postgres volume..."
sync_postgres_passwords

# ─── Step 8: Start services (staged to avoid DNS race conditions) ──
# Start backing services first, wait for health, then start application services.
# Docker Compose starts everything in parallel — if NetBox starts before Redis
# containers are resolvable via Docker DNS, it fails with name resolution errors.
info "Step 8/9: Starting backing services..."
compose up -d postgres redis redis-cache diode-redis
info "  Waiting for backing services to be ready..."
sleep 15

info "Step 8/9: Starting Hydra + migrations..."
compose up -d hydra hydra-migrate
wait_for_completed "hydra-migrate" 300

info "Step 8/9: Starting application services..."
# compose up -d may report failure due to health check dependencies timing out
# on first boot (Django migrations take several minutes). We start services,
# ignore the dependency failure, then wait for health separately.
compose up -d 2>&1 || true
sleep 5
# Ensure netbox container is at least running (even if unhealthy)
compose up -d --no-deps netbox 2>&1 || true
compose up -d --no-deps netbox-worker 2>&1 || true

# ─── Step 9: Wait for NetBox to become healthy ──────────────────────
info "Step 9/9: Waiting for NetBox to become healthy (up to 10 min for first boot migrations)..."
wait_for_healthy "netbox" 600

info ""
info "=== deploy.sh complete (infrastructure ready) ==="
info "  NetBox is healthy at ${NETBOX_URL}"
info "  Next: post-deploy.sh (migrations, superuser, OAuth2)"
info "  Then: deploy-orb-agent.yml (Diode credentials + Orb Agent)"
info ""
