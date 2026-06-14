#!/usr/bin/env bash
# ERPNext — application bootstrap (site creation). Idempotent, check-before-create.
#
# Reads .env ONLY (the admin/db passwords already templated in by Ansible) — it
# never calls OpenBao. NOT run by deploy.sh: deploy.sh handles the container
# lifecycle and leaves the site unborn; this script creates the site so the
# frontend's /api/method/ping starts answering 200. Run it after deploy.sh (the
# deploy playbook invokes it as its own phase, like the prod plan's §7.7).
#
# Usage: ./post-deploy.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$(dirname "$(dirname "$(dirname "$SCRIPT_DIR")")")/lib"
cd "${SCRIPT_DIR}"

# shellcheck source=/dev/null
source "${LIB_DIR}/common.sh"

set -a
# shellcheck source=/dev/null
source "${SCRIPT_DIR}/.env"
set +a

: "${SITE_NAME:?SITE_NAME missing from .env}"
: "${DB_PASSWORD:?DB_PASSWORD missing from .env}"
: "${ADMIN_PASSWORD:?ADMIN_PASSWORD missing from .env}"
PUBLIC_URL="${PUBLIC_URL:-https://${SITE_NAME}}"

bench_exec() {
  compose exec -T backend "$@"
}

step_create_site() {
  info "Step 1: Ensuring site ${SITE_NAME} exists..."
  if bench_exec bash -lc "test -d sites/${SITE_NAME}"; then
    info "  Site exists — skipping new-site."
  else
    info "  Creating site (this takes a few minutes)..."
    bench_exec bench new-site "${SITE_NAME}" \
      --mariadb-user-host-login-scope='%' \
      --db-root-password "${DB_PASSWORD}" \
      --admin-password "${ADMIN_PASSWORD}" \
      --install-app erpnext \
      --set-default
  fi
}

step_ensure_app() {
  info "Step 2: Ensuring erpnext app installed on site..."
  if bench_exec bench --site "${SITE_NAME}" list-apps | grep -q '^erpnext'; then
    info "  erpnext already installed."
  else
    bench_exec bench --site "${SITE_NAME}" install-app erpnext
  fi
}

step_site_config() {
  info "Step 3: Setting host_name + enabling scheduler..."
  bench_exec bench --site "${SITE_NAME}" set-config host_name "${PUBLIC_URL}"
  bench_exec bench --site "${SITE_NAME}" enable-scheduler
}

step_verify() {
  info "Step 4: Verifying app answers..."
  wait_for_http "http://localhost:8080/api/method/ping" "ERPNext" 180
  bench_exec bench version
}

main() {
  info "=== ERPNext post-deploy bootstrap ==="
  detect_runtime
  step_create_site
  step_ensure_app
  step_site_config
  step_verify
  info "=== Bootstrap complete: ${PUBLIC_URL} ==="
}

main "$@"
