#!/bin/bash
# NetBox + Discovery — Unified deployment script
#
# Usage:
#   ./deploy.sh [--no-pull] [NETBOX_URL]
#
# Options:
#   --no-pull   Skip pulling latest images (only rebuild the custom image)
#
# Arguments:
#   NETBOX_URL  Base URL for NetBox (default: http://localhost:8000)
#              Passed to generate-secrets.sh for DIODE_TARGET_OVERRIDE and related config.
#
# This script handles both first-time deployment and updates (all steps are idempotent):
#   1.  Updates/clones the netbox-docker upstream repository
#   2.  Copies .example templates to live files (if missing)
#   3.  Verifies secrets exist (Ansible manages generation + OpenBao sync)
#   4.  Pulls latest upstream images (unless --no-pull)
#   5.  Builds the custom NetBox image with plugins
#   6.  Stops the stack gracefully
#   7.  Syncs DB passwords to Postgres (if volumes already exist)
#   8.  Starts all services (with batch-start retry)
#   9.  Waits for hydra-migrate to complete
#  10.  Waits for NetBox to become healthy
#  11.  Runs database migrations
#  12.  Creates the admin superuser (idempotent — skips if it already exists)
#  13.  Waits for diode-auth + registers OAuth2 clients
#  14.  Creates orb-agent credential via Diode plugin API (or reuses existing)
#  15.  Restarts discovery services with registered credentials
#  16.  Starts the Orb Agent (if configured) + verifies all services

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
info "Step 1/16: Updating netbox-docker upstream repository..."
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
info "Step 2/16: Ensuring templates..."
copy_example_templates

# ─── Step 3: Verify env files ───────────────────────────────────────
# Secrets and env files are managed by Ansible (deploy-netbox.yml):
#   Ansible fetches/generates secrets via OpenBao → templates env files directly.
#   No secrets/ directory. No generate-secrets.sh.
#
# deploy.sh verifies env files exist but does NOT generate or manage them.
info "Step 3: Verifying env files..."
chmod +x discovery/init-db.sh
REQUIRED_FILES=".env env/netbox.env env/postgres.env env/discovery.env discovery/hydra.yaml"
for f in $REQUIRED_FILES; do
  [ -f "$f" ] && [ -s "$f" ] || \
    error "${f} missing or empty. Deploy via Semaphore (Ansible templates env files from OpenBao)."
done
info "  All required env files present."

# ─── Step 4: Pull latest images ────────────────────────────────────
if [ "$SKIP_PULL" = false ]; then
  info "Step 4/17: Pulling latest upstream images..."
  # --ignore-buildable is supported by docker compose but not podman-compose.
  # Fall back to pulling explicit services (excluding locally-built netbox/netbox-worker).
  compose pull --ignore-buildable 2>/dev/null || \
    compose pull postgres redis redis-cache ingress-nginx \
      diode-ingester diode-reconciler diode-auth hydra diode-redis
else
  info "Step 4/17: Skipping image pull (--no-pull)"
fi

# ─── Step 5: Build custom NetBox image ──────────────────────────────
info "Step 5/17: Building custom NetBox image with plugins..."
build_netbox_image

# ─── Step 6: Stop the stack ─────────────────────────────────────────
# podman-compose down may silently leave containers behind when pod dependency
# chains block removal (e.g., after a volume-mount path change). If any project
# containers remain after compose down, force-remove them so step 8 creates
# fresh containers from the current compose file.
info "Step 6/17: Stopping services..."
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
info "Step 7/17: Checking for existing Postgres volume..."
sync_postgres_passwords

# ─── Step 8: Start services (staged to avoid DNS race conditions) ──
# Start backing services first, wait for health, then start application services.
# Docker Compose starts everything in parallel — if NetBox starts before Redis
# containers are resolvable via Docker DNS, it fails with name resolution errors.
info "Step 8/17: Starting backing services..."
compose up -d postgres redis redis-cache diode-redis
info "  Waiting for backing services to be ready..."
sleep 15

info "Step 8/17: Starting Hydra + migrations..."
compose up -d hydra hydra-migrate
wait_for_completed "hydra-migrate" 300

info "Step 8/17: Starting application services..."
# compose up -d may report failure due to health check dependencies timing out
# on first boot (Django migrations take several minutes). We start services,
# ignore the dependency failure, then wait for health separately.
compose up -d 2>&1 || true
sleep 5
# Ensure netbox container is at least running (even if unhealthy)
compose up -d --no-deps netbox 2>&1 || true
compose up -d --no-deps netbox-worker 2>&1 || true

# ─── Step 10: Wait for NetBox to become healthy ─────────────────────
info "Step 10/17: Waiting for NetBox to become healthy (up to 10 min for first boot migrations)..."
wait_for_healthy "netbox" 600

# ─── Step 11: Run database migrations ──────────────────────────────
info "Step 11/17: Running database migrations..."
compose exec netbox /opt/netbox/netbox/manage.py migrate --no-input

# ─── Step 12: Create admin superuser ───────────────────────────────
# Uses SUPERUSER_NAME, SUPERUSER_EMAIL, SUPERUSER_PASSWORD env vars already set
# on the netbox container (see docker-compose.yml environment section).
# Django's createsuperuser --noinput reads the password from DJANGO_SUPERUSER_PASSWORD.
# Idempotent: skips if the username already exists.
info "Step 12/17: Creating admin superuser..."

# Read superuser credentials from the container environment
SU_NAME=$(compose exec netbox bash -c 'echo "$SUPERUSER_NAME"' 2>/dev/null | tr -d '\r')
SU_EMAIL=$(compose exec netbox bash -c 'echo "$SUPERUSER_EMAIL"' 2>/dev/null | tr -d '\r')
SU_PASS=$(compose exec netbox bash -c 'echo "$SUPERUSER_PASSWORD"' 2>/dev/null | tr -d '\r')

su_output=$(compose exec netbox bash -c '
  export DJANGO_SUPERUSER_PASSWORD="$SUPERUSER_PASSWORD"
  /opt/netbox/netbox/manage.py createsuperuser --noinput \
    --username "$SUPERUSER_NAME" \
    --email "$SUPERUSER_EMAIL" 2>&1
' 2>&1) && {
  info "Superuser created successfully."
  echo "    Username: ${SU_NAME}"
  echo "    Email:    ${SU_EMAIL}"
  echo "    Password: ${SU_PASS:0:4}....  (secrets/superuser_password.txt)"
} || {
  if echo "${su_output}" | grep -qi "already taken\|already exists\|duplicate"; then
    info "Superuser already exists, skipping."
    echo "    Username: ${SU_NAME}"
    echo "    Email:    ${SU_EMAIL}"
    echo "    Password: ${SU_PASS:0:4}....  (secrets/superuser_password.txt)"
  else
    echo "${su_output}" >&2
    error "Failed to create superuser"
  fi
}

# ─── Step 13: Wait for diode-auth + register OAuth2 clients ────────
# Secrets were pre-generated by generate-secrets.sh (step 3) and stored in discovery.env.
# authmanager runs inside the diode-auth container and uses its OAUTH2_ADMIN_SERVER_URL
# env var (set to http://hydra:4445) to reach the Hydra admin API.
info "Step 13/17: Waiting for diode-auth and registering OAuth2 clients..."
wait_for_running "diode-auth" 120
register_oauth2_clients

# ─── Step 14: Ensure orb-agent credentials ─────────────────────────
# The Diode plugin manages its own "Client Credentials" (visible in the NetBox
# UI under Diode > Client Credentials) by calling POST /clients on diode-auth.
# These are distinct from the infrastructure OAuth2 clients registered in step 13.
# If secrets/orb_agent_client_id.txt already exists (e.g., user-created via UI),
# those values are used instead.
info "Step 14/17: Ensuring orb-agent credentials..."
ensure_agent_credentials

# ─── Step 15: Restart discovery services ───────────────────────────
# ingester and reconciler may have started before clients were registered; restart them.
# hydra-migrate dependency was moved from compose to this script (step 9), so podman restart
# no longer fails on the transitive dependency chain to the exited one-shot container.
info "Step 15/17: Restarting discovery services with registered credentials..."
restart_discovery_services

# ─── Step 16: Start Orb Agent + verify all services ────────────────
# The agent runs as a standalone privileged container via sudo (not compose-managed).
# This gives it CAP_NET_RAW for SYN scans and ICMP host discovery.
if [ -f "discovery/agent.yaml" ]; then
  info "Step 16/17: Starting Orb Agent (privileged, via sudo)..."
  start_orb_agent
  wait_for_agent_running 60
else
  info "Step 16/17: Skipping Orb Agent (discovery/agent.yaml not found)"
fi

# ─── Optional: pfSense REST API sync ──────────────────────────────
# Runs the pfSense sync script if it exists and the pfSense API key is configured.
# This supplements SNMP discovery with richer data (serial, platform version,
# interface descriptions, ARP table, gateways) from the pfSense REST API.
# Uses uv to manage the virtual environment and dependencies automatically.
if [ -f "lib/pfsense-sync.py" ] && [ -f "secrets/pfsense_api_key.txt" ]; then
  if command -v uv >/dev/null 2>&1; then
    info "Running pfSense REST API sync..."
    uv run --project "${SCRIPT_DIR}" lib/pfsense-sync.py 2>&1 || warn "pfSense sync failed (non-fatal). Check lib/pfsense-sync.py output."
  else
    warn "Skipping pfSense sync (uv not found — install: curl -LsSf https://astral.sh/uv/install.sh | sh)"
  fi
else
  info "Skipping pfSense sync (lib/pfsense-sync.py or secrets/pfsense_api_key.txt not found)"
fi

# ─── Post-deployment verification ──────────────────────────────────
info "Verifying service health..."
verify_services "${NETBOX_URL}"

echo "  Admin login:"
echo "    Username: ${SU_NAME}"
echo "    Email:    ${SU_EMAIL}"
echo "    Password: ${SU_PASS:0:4}....  (secrets/superuser_password.txt)"
echo ""
echo "  Next steps:"
echo "    1. Log into NetBox at ${NETBOX_URL}/"
echo "    2. Customize subnet targets in discovery/agent.yaml"
echo ""
