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

step_oidc() {
  # Idempotent: create/update the Authentik "Social Login Key" so users can log
  # in via OIDC. Skips cleanly when no client secret is present (e.g. a deploy
  # without Authentik), so the base flow never depends on the IdP. The provider
  # name "authentik" must match the callback in erpnext-oidc.yaml:
  #   /api/method/frappe.integrations.oauth2_logins.custom/authentik
  if [ -z "${ERPNEXT_OIDC_CLIENT_SECRET:-}" ]; then
    info "Step 4: OIDC client secret absent — skipping Social Login Key."
    return 0
  fi
  # Secret is set, so OIDC is intended — the base URL is required (referenced
  # unguarded below). Fail loudly here instead of hard-erroring under `set -u`.
  : "${ERPNEXT_OIDC_BASE_URL:?ERPNEXT_OIDC_BASE_URL missing when ERPNEXT_OIDC_CLIENT_SECRET is set}"
  info "Step 4: Configuring Authentik Social Login Key (idempotent)..."
  # Write the upsert script into the backend, then run it through bench console
  # (reads the values from env so the secret isn't baked into the script body).
  bench_exec bash -lc 'cat > /tmp/oidc_setup.py' <<'PYEOF'
import os, frappe
name = os.environ["OIDC_PROVIDER"]
exists = frappe.db.exists("Social Login Key", name)
doc = frappe.get_doc("Social Login Key", name) if exists else frappe.new_doc("Social Login Key")
if not exists:
    doc.social_login_provider = "Custom"
    doc.provider_name = name
doc.client_id = os.environ["OIDC_CID"]
doc.client_secret = os.environ["OIDC_SECRET"]
doc.base_url = os.environ["OIDC_BASE"]
# REQUIRED: without custom_base_url, Frappe (apps/frappe/.../utils/oauth.py)
# ignores base_url and resolves the relative authorize/token paths against the
# SITE url (erp.<zone>) -> the IdP authorize redirect points at ERPNext itself
# and 404s. Setting it makes Frappe build <base_url><authorize_url> = the IdP.
doc.custom_base_url = 1
doc.authorize_url = "/application/o/authorize/"
doc.access_token_url = "/application/o/token/"
doc.api_endpoint = "/application/o/userinfo/"
# Required by Frappe; MUST match the redirect_uri in erpnext-oidc.yaml:
#   <public_url>/api/method/frappe.integrations.oauth2_logins.custom/<provider>
doc.redirect_url = os.environ["OIDC_REDIRECT"]
doc.auth_url_data = '{"response_type": "code", "scope": "openid email profile"}'
doc.enable_social_login = 1
doc.save(ignore_permissions=True)
frappe.db.commit()
assert frappe.db.exists("Social Login Key", name), "Social Login Key not persisted"
print("OK: Social Login Key '%s' %s" % (name, "updated" if exists else "created"))
PYEOF
  local _provider="${ERPNEXT_OIDC_PROVIDER_NAME:-authentik}"
  compose exec -T \
    -e OIDC_PROVIDER="${_provider}" \
    -e OIDC_CID="${ERPNEXT_OIDC_CLIENT_ID:-erpnext}" \
    -e OIDC_SECRET="${ERPNEXT_OIDC_CLIENT_SECRET}" \
    -e OIDC_BASE="${ERPNEXT_OIDC_BASE_URL}" \
    -e OIDC_REDIRECT="${PUBLIC_URL}/api/method/frappe.integrations.oauth2_logins.custom/${_provider}" \
    backend bash -lc "bench --site ${SITE_NAME} console < /tmp/oidc_setup.py"
  rm_oidc_tmp
  step_oidc_admin
}

rm_oidc_tmp() { bench_exec rm -f /tmp/oidc_setup.py /tmp/oidc_admin.py 2>/dev/null || true; }

step_oidc_admin() {
  # Pre-provision the SSO admin so OIDC login binds to an existing account.
  #
  # WHY: Frappe matches the OIDC identity BY EMAIL (apps/frappe/.../utils/oauth.py
  # update_oauth_user). If the User is absent it tries to SELF-REGISTER, gated by
  # Website Settings.disable_signup — which is on by default, so the login 403s
  # ("Signup from Website is disabled") instead of creating the account. We keep
  # self-signup DISABLED (no open registration on the ERP) and instead create the
  # one known admin here, mirroring the platform model (akadmin/Administrator =
  # break-glass; agent-cloud-admin = the daily SSO admin with full access). The
  # email MUST equal the Authentik agent-cloud-admin email (agent-cloud-admin.yaml)
  # or the claim won't match this User. We grant EVERY assignable role (not just
  # System Manager): System Manager is an ADMIN role only — the functional module
  # doctypes (Purchase/Stock/Accounts/Projects/...) each gate on their own roles,
  # so "full access" is the union of all roles. Only the special Administrator
  # USER bypasses permission checks; a normal SSO user gets exactly its roles.
  : "${ERPNEXT_OIDC_ADMIN_EMAIL:?ERPNEXT_OIDC_ADMIN_EMAIL missing when ERPNEXT_OIDC_CLIENT_SECRET is set}"
  info "Step 4b: Pre-provisioning OIDC admin ${ERPNEXT_OIDC_ADMIN_EMAIL} (idempotent)..."
  bench_exec bash -lc 'cat > /tmp/oidc_admin.py' <<'PYEOF'
import os, frappe
email = os.environ["ADMIN_EMAIL"]
if frappe.db.exists("User", email):
    user = frappe.get_doc("User", email)
    user.enabled = 1
    user.user_type = "System User"
    user.save(ignore_permissions=True)
    action = "updated"
else:
    user = frappe.get_doc({
        "doctype": "User",
        "email": email,
        "first_name": "agent-cloud",
        "last_name": "Admin",
        "user_type": "System User",
        "enabled": 1,
        "send_welcome_email": 0,
    })
    user.flags.no_welcome_mail = True
    user.insert(ignore_permissions=True)
    action = "created"
# Full access = the union of ALL assignable roles. Administrator/All/Guest are
# special and must not be assigned directly. add_roles() dedups + saves once at
# the end -> idempotent across re-runs.
SKIP = {"Administrator", "All", "Guest"}
roles = [r for r in frappe.get_all("Role", filters={"disabled": 0}, pluck="name") if r not in SKIP]
user.add_roles(*roles)
frappe.db.commit()
assert frappe.db.exists("User", email), "OIDC admin not persisted"
print("OK: OIDC admin '%s' %s with %d roles (full access)" % (email, action, len(roles)))
PYEOF
  compose exec -T \
    -e ADMIN_EMAIL="${ERPNEXT_OIDC_ADMIN_EMAIL}" \
    backend bash -lc "bench --site ${SITE_NAME} console < /tmp/oidc_admin.py"
  rm_oidc_tmp
}

step_reload_proxy() {
  # The backend gets a NEW container IP on every recreate; the frontend (nginx)
  # and websocket proxies cache the old upstream IP and then 502 with "no route
  # to host". Restart the proxy tier so they re-resolve the live backend. Cheap +
  # idempotent; runs after the backend is settled so the re-resolved IP sticks.
  info "Step 5: Reloading frontend/websocket to re-resolve the backend IP..."
  compose restart frontend websocket
}

step_verify() {
  # Smoke only: confirm bench + the site load. The AUTHORITATIVE HTTP health
  # check is the deploy playbook's Phase 3 (http://erpnext-frontend:8080/api/
  # method/ping over the container network). Do NOT curl localhost:8080 here —
  # post-deploy.sh runs in the Semaphore container, whose loopback is NOT the
  # published frontend, so that check is a false negative (it errored every
  # ERPNext deploy while the stack was actually healthy).
  info "Step 6: Smoke-checking bench + site..."
  bench_exec bench version
}

main() {
  info "=== ERPNext post-deploy bootstrap ==="
  detect_runtime
  step_create_site
  step_ensure_app
  step_site_config
  step_oidc
  step_reload_proxy
  step_verify
  info "=== Bootstrap complete: ${PUBLIC_URL} ==="
}

main "$@"
