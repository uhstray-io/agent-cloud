#!/bin/bash
# Secret Generator — generates random secrets and writes them to all env files.
#
# Populates:
#   - env/netbox.env       (DB_PASSWORD, REDIS_PASSWORD, REDIS_CACHE_PASSWORD, SECRET_KEY, API_TOKEN_PEPPERS)
#   - env/postgres.env     (POSTGRES_PASSWORD, DIODE_POSTGRES_PASSWORD, HYDRA_POSTGRES_PASSWORD)
#   - env/discovery.env    (REDIS_PASSWORD, DIODE_POSTGRES_PASSWORD, HYDRA_POSTGRES_PASSWORD, client secrets)
#   - .env                 (compose variable substitution: Hydra/Diode DB creds, Redis passwords, SUPERUSER_PASSWORD)
#   - discovery/hydra.yaml (Hydra system secret)
#
# Idempotent: existing non-empty values are preserved. Empty values are generated.
#
# Usage:
#   ./lib/generate-secrets.sh [NETBOX_URL]

set -euo pipefail

NETBOX_URL="${1:-http://localhost:8000}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "${SCRIPT_DIR}")"

# Source shared library (uses ROOT_DIR for directory layout)
source "${ROOT_DIR}/lib/common.sh"

# Read the hydra system secret from hydra.yaml (Hydra-YAML-specific, not reused elsewhere)
get_hydra_secret() {
  grep -m1 '^ *- ' "${ROOT_DIR}/discovery/hydra.yaml" 2>/dev/null | sed 's/^ *- //' || true
}

echo "==> Secret Generator"
echo "    NetBox URL: ${NETBOX_URL}"
echo "    Env dir:    ${ENV_DIR}"
echo ""

# ─── Verify required files exist ────────────────────────────────────
for f in netbox.env postgres.env discovery.env; do
  [ -f "${ENV_DIR}/${f}" ] || { echo "ERROR: ${ENV_DIR}/${f} not found."; exit 1; }
done

# ═══════════════════════════════════════════════════════════════════
# Read existing values — secrets/ files take priority, then env files
# ═══════════════════════════════════════════════════════════════════

# NetBox core
existing_pg_pass="$(read_existing postgres_password "${ENV_DIR}/postgres.env" POSTGRES_PASSWORD)"
existing_redis_pass="$(read_existing redis_password "${ENV_DIR}/netbox.env" REDIS_PASSWORD)"
existing_redis_cache_pass="$(read_existing redis_cache_password "${ENV_DIR}/netbox.env" REDIS_CACHE_PASSWORD)"
existing_secret_key="$(read_existing secret_key "${ENV_DIR}/netbox.env" SECRET_KEY)"
existing_api_peppers="$(read_existing api_token_peppers "${ENV_DIR}/netbox.env" API_TOKEN_PEPPER_1)"
existing_superuser_pass="$(read_existing superuser_password "${DOT_ENV}" SUPERUSER_PASSWORD)"

# Discovery / Diode
existing_diode_redis="$(read_existing diode_redis_password "${ENV_DIR}/discovery.env" REDIS_PASSWORD)"
existing_diode_pg="$(read_existing diode_postgres_password "${ENV_DIR}/discovery.env" DIODE_POSTGRES_PASSWORD)"
existing_hydra_pg="$(read_existing hydra_postgres_password "${ENV_DIR}/discovery.env" HYDRA_POSTGRES_PASSWORD)"
existing_hydra_secret="$(read_existing hydra_system_secret /dev/null "")"
[ -z "$existing_hydra_secret" ] && existing_hydra_secret="$(get_hydra_secret)"
existing_d2n_secret="$(read_existing diode_to_netbox_client_secret "${ENV_DIR}/discovery.env" DIODE_TO_NETBOX_CLIENT_SECRET)"
existing_n2d_secret="$(read_existing netbox_to_diode_client_secret "${ENV_DIR}/discovery.env" NETBOX_TO_DIODE_CLIENT_SECRET)"
existing_ingest_secret="$(read_existing diode_ingest_client_secret "${ENV_DIR}/discovery.env" DIODE_INGEST_CLIENT_SECRET)"

# Orb Agent plugin credential (created by deploy.sh step 14, may not exist yet)
existing_orb_client_id="$(get_secret orb_agent_client_id 2>/dev/null || echo "")"
existing_orb_client_secret="$(get_secret orb_agent_client_secret 2>/dev/null || echo "")"

# SNMP community string — read from secrets/ first (static, user-managed),
# fall back to .env, default to "public" if neither exists.
existing_snmp_community="$(get_secret snmp_community 2>/dev/null || echo "")"
[ -z "$existing_snmp_community" ] && existing_snmp_community="$(get_val "${DOT_ENV}" SNMP_COMMUNITY 2>/dev/null || echo "public")"
[ -z "$existing_snmp_community" ] && existing_snmp_community="public"

# ═══════════════════════════════════════════════════════════════════
# Generate any missing secrets
# ═══════════════════════════════════════════════════════════════════

needs_gen "$existing_pg_pass"         && PG_PASS="$(gen_secret)"              || PG_PASS="$existing_pg_pass"
needs_gen "$existing_redis_pass"      && REDIS_PASS="$(gen_secret)"           || REDIS_PASS="$existing_redis_pass"
needs_gen "$existing_redis_cache_pass" && REDIS_CACHE_PASS="$(gen_secret)"    || REDIS_CACHE_PASS="$existing_redis_cache_pass"
needs_gen "$existing_secret_key"      && SECRET_KEY="$(gen_django_key)"       || SECRET_KEY="$existing_secret_key"
needs_gen "$existing_api_peppers"     && API_PEPPERS="$(gen_django_key)"      || API_PEPPERS="$existing_api_peppers"
needs_gen "$existing_superuser_pass"  && SUPERUSER_PASS="$(gen_secret 18 24)" || SUPERUSER_PASS="$existing_superuser_pass"

needs_gen "$existing_diode_redis"     && DIODE_REDIS_PASS="$(gen_secret)"          || DIODE_REDIS_PASS="$existing_diode_redis"
needs_gen "$existing_diode_pg"        && DIODE_PG_PASS="$(gen_secret)"             || DIODE_PG_PASS="$existing_diode_pg"
needs_gen "$existing_hydra_pg"        && HYDRA_PG_PASS="$(gen_secret)"             || HYDRA_PG_PASS="$existing_hydra_pg"
needs_gen "$existing_hydra_secret"    && HYDRA_SECRET="$(gen_secret)"              || HYDRA_SECRET="$existing_hydra_secret"
needs_gen "$existing_d2n_secret"      && D2N_SECRET="$(gen_secret)"                || D2N_SECRET="$existing_d2n_secret"
needs_gen "$existing_n2d_secret"      && N2D_SECRET="$(gen_secret)"                || N2D_SECRET="$existing_n2d_secret"
needs_gen "$existing_ingest_secret"   && INGEST_SECRET="$(gen_secret)"             || INGEST_SECRET="$existing_ingest_secret"

# ═══════════════════════════════════════════════════════════════════
# Persist all secrets to secrets/ (source of truth for future runs)
# ═══════════════════════════════════════════════════════════════════

put_secret postgres_password          "$PG_PASS"
put_secret redis_password             "$REDIS_PASS"
put_secret redis_cache_password       "$REDIS_CACHE_PASS"
put_secret secret_key                 "$SECRET_KEY"
put_secret api_token_peppers          "$API_PEPPERS"
put_secret superuser_password         "$SUPERUSER_PASS"
put_secret diode_redis_password       "$DIODE_REDIS_PASS"
put_secret diode_postgres_password    "$DIODE_PG_PASS"
put_secret hydra_postgres_password    "$HYDRA_PG_PASS"
put_secret hydra_system_secret        "$HYDRA_SECRET"
put_secret diode_to_netbox_client_secret "$D2N_SECRET"
put_secret netbox_to_diode_client_secret "$N2D_SECRET"
put_secret diode_ingest_client_secret "$INGEST_SECRET"
echo "==> Persisted 13 secrets to secrets/"

# ═══════════════════════════════════════════════════════════════════
# Write secrets to env files
# ═══════════════════════════════════════════════════════════════════

# --- env/postgres.env ---
write_env_val "${ENV_DIR}/postgres.env" POSTGRES_PASSWORD "$PG_PASS"
write_env_val "${ENV_DIR}/postgres.env" DIODE_POSTGRES_PASSWORD "$DIODE_PG_PASS"
write_env_val "${ENV_DIR}/postgres.env" HYDRA_POSTGRES_PASSWORD "$HYDRA_PG_PASS"
echo "==> Updated env/postgres.env"

# --- env/netbox.env ---
write_env_val "${ENV_DIR}/netbox.env" DB_PASSWORD "$PG_PASS"
write_env_val "${ENV_DIR}/netbox.env" REDIS_PASSWORD "$REDIS_PASS"
write_env_val "${ENV_DIR}/netbox.env" REDIS_CACHE_PASSWORD "$REDIS_CACHE_PASS"
# SECRET_KEY and API_TOKEN_PEPPER_1 may contain special chars — write_env_val handles safely
ENV_FILE="${ENV_DIR}/netbox.env" SECRET_KEY="$SECRET_KEY" API_PEPPERS="$API_PEPPERS" python3 -c "
import re, os
path = os.environ['ENV_FILE']
with open(path) as f: content = f.read()
content = re.sub(r'^SECRET_KEY=.*$', 'SECRET_KEY=' + os.environ['SECRET_KEY'], content, flags=re.M)
content = re.sub(r'^API_TOKEN_PEPPER(?:_1|S)=.*$', 'API_TOKEN_PEPPER_1=' + os.environ['API_PEPPERS'], content, flags=re.M)
with open(path, 'w') as f: f.write(content)
"
echo "==> Updated env/netbox.env"

# --- env/discovery.env ---
write_env_val "${ENV_DIR}/discovery.env" REDIS_PASSWORD "$DIODE_REDIS_PASS"
write_env_val "${ENV_DIR}/discovery.env" DIODE_POSTGRES_PASSWORD "$DIODE_PG_PASS"
write_env_val "${ENV_DIR}/discovery.env" HYDRA_POSTGRES_PASSWORD "$HYDRA_PG_PASS"
write_env_val "${ENV_DIR}/discovery.env" DIODE_TO_NETBOX_CLIENT_SECRET "$D2N_SECRET"
write_env_val "${ENV_DIR}/discovery.env" NETBOX_TO_DIODE_CLIENT_SECRET "$N2D_SECRET"
write_env_val "${ENV_DIR}/discovery.env" DIODE_INGEST_CLIENT_SECRET "$INGEST_SECRET"
echo "==> Updated env/discovery.env"

# --- discovery/hydra.yaml ---
sedi "s|^ *- .*|    - ${HYDRA_SECRET}|" "${ROOT_DIR}/discovery/hydra.yaml"
echo "==> Updated discovery/hydra.yaml"

# --- root .env (compose variable substitution) ---
cat > "${DOT_ENV}" <<EOF
# Generated by lib/generate-secrets.sh — do not edit manually.
# Used by podman/docker compose for variable substitution in docker-compose.yml.
HYDRA_POSTGRES_USER=hydra
HYDRA_POSTGRES_PASSWORD=${HYDRA_PG_PASS}
HYDRA_POSTGRES_DB_NAME=hydra
DIODE_POSTGRES_DB_NAME=diode
DIODE_POSTGRES_USER=diode
DIODE_POSTGRES_PASSWORD=${DIODE_PG_PASS}
SUPERUSER_PASSWORD=${SUPERUSER_PASS}
REDIS_PASSWORD=${REDIS_PASS}
REDIS_CACHE_PASSWORD=${REDIS_CACHE_PASS}
DIODE_INGEST_CLIENT_SECRET=${INGEST_SECRET}
DIODE_REDIS_PASSWORD=${DIODE_REDIS_PASS}
ORB_AGENT_CLIENT_ID=${existing_orb_client_id}
ORB_AGENT_CLIENT_SECRET=${existing_orb_client_secret}
SNMP_COMMUNITY=${existing_snmp_community}
EOF
echo "==> Wrote .env"

echo ""
echo "==> All secrets generated."
echo ""
echo "    Admin password:  ${SUPERUSER_PASS:0:4}••••  (secrets/superuser_password.txt)"
echo ""
echo "    Diode ingest credentials (for Orb Agent):"
echo "      Client ID:     diode-ingest"
echo "      Client Secret: ${INGEST_SECRET:0:4}••••  (secrets/diode_ingest_client_secret.txt)"
echo "      Token URL:     http://diode-auth:8080/token"
echo ""
