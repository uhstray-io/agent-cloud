#!/bin/bash
# common.sh — Shared library for all NetBox deployment scripts.
#
# Provides: logging, cross-platform sed, compose wrapper, health/state waiters,
# service log verification, OAuth2 client registration, secret generation/persistence,
# env-file value reader, Postgres password sync, NetBox image builder, and
# deployment helper functions (template copy, OAuth2/agent credential management,
# discovery service restart, service verification).
#
# Usage:
#   SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
#   source "${SCRIPT_DIR}/lib/common.sh"    # from root scripts
#   source "${ROOT_DIR}/lib/common.sh"      # from lib/ scripts

# Source guard — prevent double-loading
[ -n "${_COMMON_SH_LOADED:-}" ] && return 0
_COMMON_SH_LOADED=1

# ─── Directory layout ─────────────────────────────────────────────
# Callers must set SCRIPT_DIR (or ROOT_DIR for discovery scripts) before sourcing.
# We derive everything else from that.
LIB_DIR="${SCRIPT_DIR}/lib"
ENV_DIR="${SCRIPT_DIR}/env"
SECRETS_DIR="${SCRIPT_DIR}/secrets"
DOT_ENV="${SCRIPT_DIR}/.env"

# Allow discovery scripts to override via ROOT_DIR
if [ -n "${ROOT_DIR:-}" ]; then
  LIB_DIR="${ROOT_DIR}/lib"
  ENV_DIR="${ROOT_DIR}/env"
  SECRETS_DIR="${ROOT_DIR}/secrets"
  DOT_ENV="${ROOT_DIR}/.env"
fi

DEFAULT_TIMEOUT="${DEFAULT_TIMEOUT:-300}"

# ─── Container runtime ───────────────────────────────────────────
# NetBox requires Docker (privileged orb-agent, bind-mount secrets,
# compose health check dependencies). Podman is not supported.
# CONTAINER_ENGINE can be overridden by environment variable (set by Ansible).
if [ -z "${CONTAINER_ENGINE:-}" ]; then
  if command -v docker >/dev/null 2>&1; then
    CONTAINER_ENGINE="docker"
  else
    echo "ERROR: Docker is not installed. NetBox requires Docker (not Podman)." >&2
    exit 1
  fi
fi
CONTAINER_SEP="-"

# ─── Logging ──────────────────────────────────────────────────────
info()  { echo "==> $*"; }
warn()  { echo "WARNING: $*" >&2; }
error() { echo "ERROR: $*" >&2; exit 1; }

# ─── Cross-platform in-place sed ─────────────────────────────────
# macOS BSD sed requires an empty backup extension; GNU sed does not.
sedi() {
  if sed --version >/dev/null 2>&1; then
    sed -i "$@"
  else
    sed -i '' "$@"
  fi
}

# ─── Compose wrapper ─────────────────────────────────────────────
# Wraps the detected container engine's compose with explicit project name and
# compose file to avoid auto-discovery of override files and keep names stable.
compose() {
  local compose_dir="${ROOT_DIR:-${SCRIPT_DIR}}"
  $CONTAINER_ENGINE compose --project-name "netbox" -f "${compose_dir}/docker-compose.yml" "$@"
}

# ─── Health / state waiters ───────────────────────────────────────

# wait_for_healthy <service> [timeout]
# Polls `compose ps --format json` until the service reports "(healthy)".
wait_for_healthy() {
  local service="$1"
  local timeout="${2:-$DEFAULT_TIMEOUT}"
  local elapsed=0

  info "Waiting for ${service} to become healthy (timeout: ${timeout}s)..."
  while [ $elapsed -lt $timeout ]; do
    status=$(compose ps --format json 2>/dev/null \
      | SVC_NAME="${service}" python3 -c "
import sys, json, os
svc = os.environ['SVC_NAME']
raw = sys.stdin.read().strip()
try:
    data = json.loads(raw)
    if not isinstance(data, list): data = [data]
except Exception:
    data = [json.loads(l) for l in raw.splitlines() if l.strip()]
for c in data:
    # podman: service in Labels, container name in Names (array)
    # docker: service in Service, name in Name (string)
    c_svc = c.get('Service', '') or c.get('Labels', {}).get('io.podman.compose.service', '')
    c_names = c.get('Names', [c.get('Name', '')])
    if not isinstance(c_names, list): c_names = [c_names]
    if c_svc == svc or any(svc in n for n in c_names):
        st = c.get('Status', '')
        if '(healthy)' in st:
            print('healthy')
        elif '(unhealthy)' in st:
            print('unhealthy')
        else:
            print('starting')
        break
" 2>/dev/null || echo "unknown")

    if [ "$status" = "healthy" ]; then
      info "${service} is healthy."
      return 0
    fi
    sleep 5
    elapsed=$((elapsed + 5))
  done
  error "${service} did not become healthy within ${timeout}s. Check: $CONTAINER_ENGINE compose logs ${service}"
}

# wait_for_running <service> [timeout]
# Polls `compose ps --format json` until the service State is "running".
wait_for_running() {
  local service="$1"
  local timeout="${2:-$DEFAULT_TIMEOUT}"
  local elapsed=0

  info "Waiting for ${service} to start (timeout: ${timeout}s)..."
  while [ $elapsed -lt $timeout ]; do
    state=$(compose ps --format json 2>/dev/null \
      | SVC_NAME="${service}" python3 -c "
import sys, json, os
svc = os.environ['SVC_NAME']
raw = sys.stdin.read().strip()
try:
    data = json.loads(raw)
    if not isinstance(data, list): data = [data]
except Exception:
    data = [json.loads(l) for l in raw.splitlines() if l.strip()]
for c in data:
    c_svc = c.get('Service', '') or c.get('Labels', {}).get('io.podman.compose.service', '')
    c_names = c.get('Names', [c.get('Name', '')])
    if not isinstance(c_names, list): c_names = [c_names]
    if c_svc == svc or any(svc in n for n in c_names):
        print(c.get('State', 'unknown'))
        break
else:
    print('not_found')
" 2>/dev/null || echo "unknown")

    if [ "$state" = "running" ]; then
      info "${service} is running."
      return 0
    fi
    sleep 5
    elapsed=$((elapsed + 5))
  done
  error "${service} did not start within ${timeout}s. Check: $CONTAINER_ENGINE compose logs ${service}"
}

# wait_for_completed <service> [timeout]
# Polls container state until a one-shot container exits with code 0.
# Uses container inspect directly (compose ps may omit exited containers).
wait_for_completed() {
  local service="$1"
  local timeout="${2:-$DEFAULT_TIMEOUT}"
  local elapsed=0
  local container="netbox${CONTAINER_SEP}${service}${CONTAINER_SEP}1"

  info "Waiting for ${service} to complete (timeout: ${timeout}s)..."
  while [ "$elapsed" -lt "$timeout" ]; do
    local state
    state=$($CONTAINER_ENGINE inspect --format '{{.State.Status}}' "${container}" 2>/dev/null || echo "unknown")
    if [ "$state" = "exited" ]; then
      local exit_code
      exit_code=$($CONTAINER_ENGINE inspect --format '{{.State.ExitCode}}' "${container}" 2>/dev/null || echo "1")
      if [ "$exit_code" = "0" ]; then
        info "${service} completed successfully."
        return 0
      else
        error "${service} exited with code ${exit_code}. Check: $CONTAINER_ENGINE logs ${container}"
      fi
    fi
    sleep 3
    elapsed=$((elapsed + 3))
  done
  error "${service} did not complete within ${timeout}s. Check: $CONTAINER_ENGINE logs ${container}"
}

# ─── Service log verification ────────────────────────────────────

# verify_service_logs <service>
# Prints last 10 log lines and checks for fatal errors. Returns 1 if errors detected.
verify_service_logs() {
  local service="$1"
  local logs
  logs=$(compose logs --tail=10 "${service}" 2>&1 || true)

  if echo "${logs}" | grep -qiE '(fatal|panic|segfault|killed|oom)'; then
    warn "${service}: errors detected in logs"
    echo "${logs}" | tail -5
    return 1
  fi
  return 0
}

# ─── OAuth2 client registration ──────────────────────────────────

# register_oauth2_client <client-id> <client-secret> [extra authmanager flags...]
# Idempotent: skips creation if the client already exists.
register_oauth2_client() {
  local client_id="$1"
  local client_secret="$2"
  shift 2

  info "  Registering OAuth2 client: ${client_id}"
  if compose exec diode-auth /usr/local/bin/authmanager get-client \
      -client-id "${client_id}" >/dev/null 2>&1; then
    info "  ${client_id} already registered, skipping."
    return 0
  fi

  local output
  if output=$(compose exec diode-auth /usr/local/bin/authmanager create-client \
      -client-id "${client_id}" \
      -client-secret "${client_secret}" \
      "$@" 2>&1); then
    info "  ${client_id} registered successfully."
  else
    echo "${output}" >&2
    error "Failed to register OAuth2 client: ${client_id}"
  fi
}

# ─── Secret generation & persistence ─────────────────────────────

# gen_secret [raw_bytes] [output_chars]
# Generate a random alphanumeric secret (default 32 chars).
gen_secret() { openssl rand -base64 "${1:-24}" | tr -d '/+=' | head -c "${2:-32}"; }

# gen_django_key
# Generate a Django-style secret key (64 chars with special characters).
gen_django_key() {
  python3 -c "
import secrets, string
chars = string.ascii_letters + string.digits + '!@#\$%^&*(-_=+)'
print(''.join(secrets.choice(chars) for _ in range(64)))
"
}

# get_secret <name>
# Read a persisted secret from secrets/<name>.txt; returns empty string if missing.
get_secret() { cat "${SECRETS_DIR}/${1}.txt" 2>/dev/null || true; }

# put_secret <name> <value>
# Write a secret to secrets/<name>.txt with restricted permissions.
# Uses a subshell with umask 077 to prevent TOCTOU race on directory/file creation.
put_secret() {
  local name="$1" value="$2"
  (umask 077; mkdir -p "${SECRETS_DIR}"; printf '%s' "${value}" > "${SECRETS_DIR}/${name}.txt")
}

# get_val <file> <key>
# Read a value from an env file; returns empty string if not found or empty.
get_val() {
  local file="$1" key="$2"
  grep -m1 "^${key}=" "${file}" 2>/dev/null | cut -d= -f2- | sed "s/^['\"]//;s/['\"]$//" || true
}

# needs_gen <value>
# Returns 0 (true) if the value needs generation (empty or a placeholder).
needs_gen() {
  case "$1" in ""|CHANGE_ME*|placeholder*|HYDRA_SYSTEM_SECRET_PLACEHOLDER) return 0;; *) return 1;; esac
}

# read_existing <secret_name> <env_file> <env_key>
# Read from secrets/ first, fall back to env file value.
read_existing() {
  local secret_name="$1" env_file="$2" env_key="$3"
  local val
  val="$(get_secret "${secret_name}")"
  if [ -z "$val" ]; then
    val="$(get_val "${env_file}" "${env_key}")"
  fi
  printf '%s' "$val"
}

# write_env_val <file> <key> <value>
# Safely writes KEY=VALUE to an env file, replacing any existing KEY= line.
# Uses Python to avoid sed metacharacter issues with special chars in values.
write_env_val() {
  local file="$1" key="$2" value="$3"
  python3 -c "
import sys
path, key, val = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path) as f: lines = f.readlines()
found = False
with open(path, 'w') as f:
    for line in lines:
        if line.startswith(key + '='):
            f.write(key + '=' + val + '\n')
            found = True
        else:
            f.write(line)
    if not found:
        f.write(key + '=' + val + '\n')
" "$file" "$key" "$value"
}

# ─── Diode plugin credential management ─────────────────────

# create_agent_credential [name]
# Creates an orb-agent credential via the Diode plugin's create_client API.
# Uses the Django management shell to call the plugin, which authenticates to
# diode-auth with netbox-to-diode credentials and calls POST /clients.
# Returns JSON with client_id and client_secret.
# Note: Django shell prints config-loading messages to stdout; grep filters to JSON only.
create_agent_credential() {
  local name="${1:-orb-agent}"
  compose exec -T netbox /opt/netbox/netbox/manage.py shell -c "
import json
from netbox_diode_plugin.client import create_client
result = create_client(None, '${name}', 'diode:ingest')
print(json.dumps(result))
" 2>/dev/null | grep '^{'
}

# get_agent_credentials
# Lists Diode plugin credentials and returns the first matching 'orb-agent' entry.
# Returns JSON object if found, empty JSON object {} if not.
get_agent_credentials() {
  compose exec -T netbox /opt/netbox/netbox/manage.py shell -c "
import json
from netbox_diode_plugin.client import list_clients
clients = list_clients(None)
matching = [c for c in clients if c.get('client_name') == 'orb-agent']
print(json.dumps(matching[0] if matching else {}))
" 2>/dev/null | grep '^{'
}

# ─── Postgres password sync ──────────────────────────────────────

# Check if the netbox postgres volume already exists (i.e. data was persisted).
postgres_volume_exists() {
  $CONTAINER_ENGINE volume inspect "netbox${CONTAINER_SEP}netbox-postgres" >/dev/null 2>&1
}

# sync_postgres_passwords — If the postgres volume exists, start only postgres,
# wait for it to accept connections, then ALTER USER for each DB user to match
# the passwords in the env files. No-op if the volume doesn't exist.
sync_postgres_passwords() {
  if ! postgres_volume_exists; then
    info "Postgres volume not found — skipping password sync (first deploy)."
    return 0
  fi

  info "Postgres volume exists — syncing DB passwords to match env files..."

  # Read current passwords from env files
  local pg_pass diode_pg_pass hydra_pg_pass
  pg_pass="$(get_val "${ENV_DIR}/postgres.env" POSTGRES_PASSWORD)"
  diode_pg_pass="$(get_val "${ENV_DIR}/postgres.env" DIODE_POSTGRES_PASSWORD)"
  hydra_pg_pass="$(get_val "${ENV_DIR}/postgres.env" HYDRA_POSTGRES_PASSWORD)"

  if [ -z "$pg_pass" ]; then
    warn "POSTGRES_PASSWORD is empty in env/postgres.env — skipping sync."
    return 0
  fi

  # Start only postgres (detached) and wait for it to accept connections
  compose up -d postgres
  local elapsed=0 timeout=60
  while [ "$elapsed" -lt "$timeout" ]; do
    if compose exec postgres pg_isready -U netbox >/dev/null 2>&1; then
      break
    fi
    sleep 2
    elapsed=$((elapsed + 2))
  done

  if [ "$elapsed" -ge "$timeout" ]; then
    warn "Postgres did not become ready within ${timeout}s — skipping password sync."
    return 0
  fi

  # ALTER USER for each role using format(%L) to safely quote passwords.
  # SQL is piped via stdin to avoid exposing passwords in the process list.
  local sql=""
  sql+="DO \$\$ BEGIN EXECUTE format('ALTER USER netbox WITH PASSWORD %L', '${pg_pass//\'/\'\'}'); END \$\$; "
  [ -n "$diode_pg_pass" ] && sql+="DO \$\$ BEGIN IF EXISTS (SELECT FROM pg_roles WHERE rolname='diode') THEN EXECUTE format('ALTER USER diode WITH PASSWORD %L', '${diode_pg_pass//\'/\'\'}'); END IF; END \$\$; "
  [ -n "$hydra_pg_pass" ] && sql+="DO \$\$ BEGIN IF EXISTS (SELECT FROM pg_roles WHERE rolname='hydra') THEN EXECUTE format('ALTER USER hydra WITH PASSWORD %L', '${hydra_pg_pass//\'/\'\'}'); END IF; END \$\$; "

  if echo "${sql}" | compose exec -T postgres psql -U netbox -d netbox >/dev/null 2>&1; then
    info "Postgres passwords synced successfully."
  else
    warn "Postgres password sync had errors (non-fatal). Users may not exist yet."
  fi
}

# ─── NetBox image builder ────────────────────────────────────────

# build_netbox_image
# Extracts VERSION from docker-compose.yml and builds the custom image.
build_netbox_image() {
  local compose_dir="${ROOT_DIR:-${SCRIPT_DIR}}"
  local version
  version=$(grep -m1 'VERSION' "${compose_dir}/docker-compose.yml" \
    | grep -o 'v[0-9][^}]*' | head -1 || true)
  version="${version:-v4.5-4.0.0}"

  info "Building NetBox image (VERSION=${version})..."
  $CONTAINER_ENGINE build --no-cache \
    -t netbox:latest-plugins \
    -f "${compose_dir}/Dockerfile-Plugins" \
    --build-arg "VERSION=${version}" \
    "${compose_dir}"
}

# ─── Deployment helper functions ─────────────────────────────────
# Used by deploy.sh to avoid code duplication.

# copy_example_templates
# Copies .example files to live files if they don't exist yet.
copy_example_templates() {
  for tmpl in env/netbox.env env/postgres.env env/discovery.env discovery/hydra.yaml discovery/agent.yaml; do
    if [ ! -f "$tmpl" ] && [ -f "${tmpl}.example" ]; then
      cp "${tmpl}.example" "$tmpl"
      info "Created ${tmpl} from template"
    fi
  done

  [ -f "env/discovery.env" ] || error "env/discovery.env not found. Is the env/ directory set up?"
  [ -f "env/postgres.env" ] || error "env/postgres.env not found."
}

# register_oauth2_clients
# Reads secrets from discovery.env and registers the 3 infrastructure OAuth2 clients.
register_oauth2_clients() {
  local d2n_secret n2d_secret ingest_secret
  d2n_secret=$(grep -m1 '^DIODE_TO_NETBOX_CLIENT_SECRET=' env/discovery.env | cut -d= -f2-)
  n2d_secret=$(grep -m1 '^NETBOX_TO_DIODE_CLIENT_SECRET=' env/discovery.env | cut -d= -f2-)
  ingest_secret=$(grep -m1 '^DIODE_INGEST_CLIENT_SECRET=' env/discovery.env | cut -d= -f2-)

  register_oauth2_client "diode-to-netbox" "${d2n_secret}" -scope "netbox:read netbox:write"
  register_oauth2_client "netbox-to-diode" "${n2d_secret}" -scope "diode:read diode:write"
  register_oauth2_client "diode-ingest" "${ingest_secret}" -allow-ingest
}

# ensure_agent_credentials
# Creates or reuses orb-agent credentials via the Diode plugin API.
# Updates .env with the credentials for compose substitution.
ensure_agent_credentials() {
  local orb_client_id orb_client_secret

  # Read from .env (templated by Ansible from OpenBao)
  orb_client_id="$(get_val "${DOT_ENV}" ORB_AGENT_CLIENT_ID 2>/dev/null || echo "")"
  orb_client_secret="$(get_val "${DOT_ENV}" ORB_AGENT_CLIENT_SECRET 2>/dev/null || echo "")"

  if [ -n "$orb_client_id" ] && [ -n "$orb_client_secret" ]; then
    info "  Using existing agent credential: ${orb_client_id}"
  else
    local cred_json
    cred_json=$(create_agent_credential "orb-agent")
    orb_client_id=$(echo "$cred_json" | python3 -c "import sys,json; print(json.load(sys.stdin)['client_id'])")
    orb_client_secret=$(echo "$cred_json" | python3 -c "import sys,json; print(json.load(sys.stdin)['client_secret'])")
    info "  Created agent credential: ${orb_client_id}"
  fi

  # Update .env with credentials (Ansible Phase 4 syncs back to OpenBao)
  grep -q '^ORB_AGENT_CLIENT_ID=' "${DOT_ENV}" && \
    sedi "s|^ORB_AGENT_CLIENT_ID=.*|ORB_AGENT_CLIENT_ID=${orb_client_id}|" "${DOT_ENV}" || \
    echo "ORB_AGENT_CLIENT_ID=${orb_client_id}" >> "${DOT_ENV}"
  grep -q '^ORB_AGENT_CLIENT_SECRET=' "${DOT_ENV}" && \
    sedi "s|^ORB_AGENT_CLIENT_SECRET=.*|ORB_AGENT_CLIENT_SECRET=${orb_client_secret}|" "${DOT_ENV}" || \
    echo "ORB_AGENT_CLIENT_SECRET=${orb_client_secret}" >> "${DOT_ENV}"
}

# restart_discovery_services
# Restarts ingester, reconciler, and nginx containers after credential registration.
restart_discovery_services() {
  $CONTAINER_ENGINE restart \
    "netbox${CONTAINER_SEP}diode-ingester${CONTAINER_SEP}1" \
    "netbox${CONTAINER_SEP}diode-reconciler${CONTAINER_SEP}1" \
    "netbox${CONTAINER_SEP}ingress-nginx${CONTAINER_SEP}1" 2>/dev/null || true
}

# ─── Orb Agent lifecycle (standalone, privileged) ─────────────────

ORB_AGENT_CONTAINER="netbox-orb-agent"

# start_orb_agent
# Starts the orb-agent as a standalone privileged container via sudo.
# Reads credentials from .env (managed by Ansible/OpenBao), mounts agent.yaml, uses host networking.
# Idempotent: removes any existing container first.
start_orb_agent() {
  local compose_dir="${SCRIPT_DIR}"

  # Read credentials from .env (templated by Ansible from OpenBao)
  local client_id client_secret snmp_community
  client_id="$(get_val "${DOT_ENV}" ORB_AGENT_CLIENT_ID 2>/dev/null || echo "")"
  client_secret="$(get_val "${DOT_ENV}" ORB_AGENT_CLIENT_SECRET 2>/dev/null || echo "")"
  snmp_community="$(get_val "${DOT_ENV}" SNMP_COMMUNITY 2>/dev/null || echo "public")"

  if [ -z "$client_id" ] || [ -z "$client_secret" ]; then
    warn "Agent credentials not found in .env — skipping orb-agent start."
    warn "Ensure Ansible has stored orb_agent_client_id/secret in OpenBao."
    return 1
  fi

  # Resolve env var placeholders in agent.yaml — the orb-agent binary does not
  # reliably perform env var substitution on its config file. Write the resolved
  # copy to discovery/ (gitignored, chmod 600) so credentials never leak.
  local resolved_config="${compose_dir}/discovery/agent-resolved.yaml"
  # Docker may create this as a directory if the bind-mount source was missing
  [ -d "$resolved_config" ] && rm -rf "$resolved_config"
  sed \
    -e "s|\${DIODE_CLIENT_ID}|${client_id}|g" \
    -e "s|\${DIODE_CLIENT_SECRET}|${client_secret}|g" \
    -e "s|\${SNMP_COMMUNITY}|${snmp_community}|g" \
    "${compose_dir}/discovery/agent.yaml" > "${resolved_config}"

  # OS-aware network scan mode:
  # - macOS: Podman runs in a VM (applehv/qemu). Raw sockets (SYN/ICMP) cannot
  #   traverse the VM's NAT, so nmap -sS fails immediately. Force TCP connect.
  # - Linux: --privileged --net=host gives real CAP_NET_RAW. Use default SYN scan.
  if [ "$(uname -s)" = "Darwin" ]; then
    # Ensure scan_types and skip_host are present in network_discovery scope.
    # Use awk (not sed insert) because BSD sed's i\ can't capture indentation.
    if ! grep -q 'scan_types:' "${resolved_config}"; then
      awk '/^[[:space:]]*ports:/ && injected==0 {
        match($0, /^[[:space:]]*/); indent = substr($0, RSTART, RLENGTH)
        print indent "scan_types: [connect]"
        print indent "skip_host: true"
        injected = 1
      } {print}' "${resolved_config}" > "${resolved_config}.tmp" \
        && mv "${resolved_config}.tmp" "${resolved_config}"
      info "macOS detected — forcing TCP connect scan (VM NAT blocks raw sockets)"
    fi
  else
    # On Linux, remove connect scan overrides if present (use default SYN)
    if grep -q 'scan_types: \[connect\]' "${resolved_config}"; then
      sed -i.bak \
        -e '/scan_types: \[connect\]/d' \
        -e '/skip_host: true/d' \
        "${resolved_config}" && rm -f "${resolved_config}.bak"
      info "Linux detected — using default SYN scan (privileged mode)"
    fi
  fi

  chmod 600 "${resolved_config}"

  # Remove any existing container (idempotent)
  sudo $CONTAINER_ENGINE rm -f "${ORB_AGENT_CONTAINER}" 2>/dev/null || true

  info "Starting orb-agent with sudo (privileged, host networking)..."
  sudo $CONTAINER_ENGINE run -d \
    --name "${ORB_AGENT_CONTAINER}" \
    --privileged \
    --net=host \
    --restart unless-stopped \
    -v "${resolved_config}:/opt/orb/agent.yaml:ro,z" \
    -v "${compose_dir}/discovery/snmp-extensions:/opt/orb/snmp-extensions:ro,z" \
    docker.io/netboxlabs/orb-agent:latest run -c /opt/orb/agent.yaml
}

# stop_orb_agent
# Stops and removes the standalone orb-agent container and resolved config.
stop_orb_agent() {
  sudo $CONTAINER_ENGINE stop "${ORB_AGENT_CONTAINER}" 2>/dev/null || true
  sudo $CONTAINER_ENGINE rm -f "${ORB_AGENT_CONTAINER}" 2>/dev/null || true
  local compose_dir="${SECRETS_DIR%/secrets}"
  rm -f "${compose_dir}/discovery/agent-resolved.yaml" 2>/dev/null || true
}

# wait_for_agent_running [timeout]
# Polls container state until the standalone orb-agent is running.
wait_for_agent_running() {
  local timeout="${1:-60}"
  local elapsed=0

  info "Waiting for orb-agent to start (timeout: ${timeout}s)..."
  while [ "$elapsed" -lt "$timeout" ]; do
    local state
    state=$(sudo $CONTAINER_ENGINE inspect --format '{{.State.Status}}' "${ORB_AGENT_CONTAINER}" 2>/dev/null || echo "not_found")
    if [ "$state" = "running" ]; then
      info "orb-agent is running."
      return 0
    fi
    sleep 3
    elapsed=$((elapsed + 3))
  done
  error "orb-agent did not start within ${timeout}s. Check: sudo $CONTAINER_ENGINE logs ${ORB_AGENT_CONTAINER}"
}

# verify_services
# Checks logs for all services and prints a summary banner.
# Arguments: NETBOX_URL
verify_services() {
  local netbox_url="${1:-http://localhost:8000}"
  local services=(netbox netbox-worker postgres redis redis-cache ingress-nginx diode-ingester diode-reconciler diode-auth hydra diode-redis)
  local failed_services=()

  for svc in "${services[@]}"; do
    if ! verify_service_logs "${svc}"; then
      failed_services+=("${svc}")
    fi
  done

  # Check standalone orb-agent (not compose-managed)
  if sudo $CONTAINER_ENGINE inspect "${ORB_AGENT_CONTAINER}" >/dev/null 2>&1; then
    local agent_logs
    agent_logs=$(sudo $CONTAINER_ENGINE logs --tail=10 "${ORB_AGENT_CONTAINER}" 2>&1 || true)
    if echo "${agent_logs}" | grep -qiE '(fatal|panic|segfault|killed|oom)'; then
      warn "orb-agent: errors detected in logs"
      echo "${agent_logs}" | tail -5
      failed_services+=("orb-agent")
    fi
  fi

  echo ""
  echo "════════════════════════════════════════════════════════════════"

  if [ ${#failed_services[@]} -gt 0 ]; then
    echo ""
    echo "  DEPLOYMENT COMPLETED WITH WARNINGS"
    echo ""
    echo "  The following services have errors in their logs:"
    for svc in "${failed_services[@]}"; do
      echo "    - ${svc}"
    done
    echo ""
    echo "  Review logs with:  $CONTAINER_ENGINE compose logs <service>"
    echo ""
  else
    echo ""
    echo "  DEPLOYMENT SUCCESSFUL"
    echo ""
  fi

  echo "  NetBox UI:   ${netbox_url}/"
  echo "  Diode gRPC:  localhost:8081"
  echo ""
  echo "  Verify:      curl -sf ${netbox_url}/login/"
  echo "  View logs:   $CONTAINER_ENGINE compose logs -f netbox"
  echo ""
  echo "════════════════════════════════════════════════════════════════"
  echo ""
}
