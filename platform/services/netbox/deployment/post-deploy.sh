#!/bin/bash
# NetBox Post-Deploy — Application configuration after containers are healthy
#
# Prerequisites: deploy.sh completed successfully (NetBox container healthy)
#
# Steps (all idempotent):
#  11.  Runs database migrations
#  12.  Creates the admin superuser (skips if exists)
#  13.  Waits for diode-auth + registers OAuth2 clients
#  14.  Creates orb-agent credential via Diode plugin API (or reuses existing)
#  15.  Restarts discovery services with registered credentials
#  16.  Starts the Orb Agent (if configured) + verifies all services
#
# Usage:
#   ./post-deploy.sh [NETBOX_URL]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

NETBOX_URL="${1:-http://localhost:8000}"

# Source shared library
source "${SCRIPT_DIR}/lib/common.sh"

# ─── Preflight: verify NetBox is running ──────────────────────────
info "=== NetBox Post-Deploy ==="
info "  URL: ${NETBOX_URL}"

compose ps --format json 2>/dev/null | head -1 >/dev/null || error "No compose services running. Run deploy.sh first."

# No secrets/ directory — all credentials managed by Ansible/OpenBao.
# Functions read from .env (templated by Ansible from OpenBao).

# ─── Step 11: Run database migrations ──────────────────────────────
info "Step 11: Running database migrations..."
compose exec netbox /opt/netbox/netbox/manage.py migrate --no-input

# ─── Step 12: Create admin superuser ───────────────────────────────
info "Step 12: Creating admin superuser..."

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
} || {
  if echo "${su_output}" | grep -qi "already taken\|already exists\|duplicate"; then
    info "Superuser already exists, skipping."
  else
    echo "${su_output}" >&2
    error "Failed to create superuser"
  fi
}
echo "    Username: ${SU_NAME}"
echo "    Password: ${SU_PASS:0:4}....  (from .env SUPERUSER_PASSWORD)"

# ─── Step 13: Wait for diode-auth + register OAuth2 clients ────────
info "Step 13: Registering OAuth2 clients..."
wait_for_running "diode-auth" 120
register_oauth2_clients

# ─── Step 14: Ensure orb-agent credentials ─────────────────────────
info "Step 14: Ensuring orb-agent credentials..."
ensure_agent_credentials

# ─── Step 15: Restart discovery services ───────────────────────────
info "Step 15: Restarting discovery services..."
restart_discovery_services

# ─── Step 16: Start Orb Agent + verify all services ────────────────
if [ -f "discovery/agent.yaml" ]; then
  info "Step 16: Starting Orb Agent..."
  start_orb_agent
  wait_for_agent_running 60
else
  info "Step 16: Skipping Orb Agent (discovery/agent.yaml not found)"
fi

# ─── Optional: pfSense REST API sync ──────────────────────────────
PFSENSE_KEY="$(get_val "${DOT_ENV}" PFSENSE_API_KEY 2>/dev/null || echo "")"
if [ -f "lib/pfsense-sync.py" ] && [ -n "$PFSENSE_KEY" ]; then
  if command -v uv >/dev/null 2>&1; then
    info "Running pfSense REST API sync..."
    uv run --project "${SCRIPT_DIR}" lib/pfsense-sync.py 2>&1 || warn "pfSense sync failed (non-fatal)."
  else
    warn "Skipping pfSense sync (uv not found)"
  fi
fi

# ─── Post-deployment verification ──────────────────────────────────
info "Verifying service health..."
verify_services "${NETBOX_URL}"

info ""
info "=== Post-deploy complete ==="
echo "  Admin login:"
echo "    Username: ${SU_NAME}"
echo "    Email:    ${SU_EMAIL}"
echo "    Password: ${SU_PASS:0:4}....  (from .env)"
echo ""
echo "  NetBox: ${NETBOX_URL}/"
echo ""
