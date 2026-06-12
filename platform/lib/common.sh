#!/usr/bin/env bash
# common.sh — Shared library for agent-cloud deploy scripts
# Source guard: safe to source multiple times
[ -n "${_WA_COMMON_SH_LOADED:-}" ] && return 0
_WA_COMMON_SH_LOADED=1

# ── Logging ───────────────────────────────────────────────────────────────────

info() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
warn() { printf '[%s] WARN: %s\n' "$(date +%H:%M:%S)" "$*" >&2; }
error() { printf '[%s] ERROR: %s\n' "$(date +%H:%M:%S)" "$*" >&2; exit 1; }

# ── Secret Generation ─────────────────────────────────────────────────────────

# gen_secret [raw_bytes] [output_chars]
# Generate a random alphanumeric secret (default 32 chars).
gen_secret() {
  openssl rand -base64 "${1:-24}" | tr -d '/+=' | head -c "${2:-32}"
}

# ── Secret Persistence ────────────────────────────────────────────────────────

# get_secret <dir> <name> — read from <dir>/<name>.txt; empty string if missing
get_secret() {
  cat "${1}/${2}.txt" 2>/dev/null || true
}

# put_secret <dir> <name> <value> — write with restricted permissions (TOCTOU-safe)
put_secret() {
  local dir="$1" name="$2" value="$3"
  (umask 077; mkdir -p "$dir"; printf '%s' "$value" > "${dir}/${name}.txt")
}

# needs_gen <value> — returns 0 (true) if value needs generation
needs_gen() {
  case "$1" in ""|REPLACE_*|changeme*|placeholder*) return 0;; *) return 1;; esac
}

# ── Container Runtime Detection ───────────────────────────────────────────────

detect_runtime() {
  [ -n "${CONTAINER_ENGINE:-}" ] && return 0
  if command -v podman &>/dev/null; then
    CONTAINER_ENGINE=podman
    COMPOSE_CMD="podman-compose"
    # Verify podman-compose is available, fall back to podman compose
    if ! command -v podman-compose &>/dev/null; then
      COMPOSE_CMD="podman compose"
    fi
  elif command -v docker &>/dev/null; then
    CONTAINER_ENGINE=docker
    COMPOSE_CMD="docker compose"
  else
    error "Neither podman nor docker found. Install one to continue."
  fi
}

# ── Compose Wrapper ───────────────────────────────────────────────────────────

# compose [args...] — wraps compose with explicit -f to prevent override auto-discovery
# Local-dev overlay (plan/development/LOCAL-DEV-DEPLOYMENT.md): compose.local.yml
# is appended only when LOCAL_MODE=true AND the overlay exists on disk.
# LOCAL_MODE unset or file absent => byte-identical prod behavior.
compose() {
  detect_runtime
  if [ "${LOCAL_MODE:-}" = "true" ] && [ -f compose.local.yml ]; then
    $COMPOSE_CMD -f compose.yml -f compose.local.yml "$@"
  else
    $COMPOSE_CMD -f compose.yml "$@"
  fi
}

# ── Health Waiters ────────────────────────────────────────────────────────────

# wait_for_healthy <container_name> <timeout_seconds>
# Polls container health status until healthy or timeout
wait_for_healthy() {
  local name="$1" timeout="${2:-120}" elapsed=0
  detect_runtime
  info "Waiting for ${name} to be healthy (timeout ${timeout}s)..."
  while [ "$elapsed" -lt "$timeout" ]; do
    local status
    status=$($CONTAINER_ENGINE inspect --format='{{.State.Health.Status}}' "$name" 2>/dev/null) || status=""
    if [ "$status" = "healthy" ]; then
      info "${name} is healthy."
      return 0
    fi
    sleep 2
    elapsed=$((elapsed + 2))
  done
  error "${name} did not become healthy within ${timeout}s"
}

# wait_for_http <url> <label> <timeout_seconds>
# Polls an HTTP endpoint until it returns 200 or timeout
wait_for_http() {
  local url="$1" label="$2" timeout="${3:-120}" elapsed=0
  info "Waiting for ${label} at ${url} (timeout ${timeout}s)..."
  while [ "$elapsed" -lt "$timeout" ]; do
    local code
    code=$(curl -s -o /dev/null -w "%{http_code}" "$url" 2>/dev/null) || code="000"
    if [ "$code" = "200" ]; then
      info "${label} is responding."
      return 0
    fi
    sleep 2
    elapsed=$((elapsed + 2))
  done
  error "${label} did not respond at ${url} within ${timeout}s"
}

# ── HTTP Check ────────────────────────────────────────────────────────────────

# check_http <url> <label> [header_name] [header_value]
# Checks if URL returns HTTP 200. Optional auth header.
check_http() {
  local url="$1" label="$2" header_name="${3:-}" header_value="${4:-}"
  local code args=(-s -o /dev/null -w "%{http_code}")
  [ -n "$header_name" ] && args+=(-H "${header_name}: ${header_value}")
  code=$(curl "${args[@]}" "$url" 2>/dev/null) || code="000"
  if [ "$code" = "200" ]; then
    info "  ${label}: OK"
    return 0
  else
    warn "  ${label}: HTTP ${code}"
    return 1
  fi
}

# ── OpenBao Token Storage ────────────────────────────────────────────────────

# store_token_in_openbao <secrets_dir> <secret_name> <bao_path> <field_name>
# Reads a secret from local file, stores it in OpenBao via AppRole auth.
store_token_in_openbao() {
  local secrets_dir="$1" secret_name="$2" bao_path="$3" field_name="$4"
  local value
  value=$(get_secret "$secrets_dir" "$secret_name")
  if needs_gen "$value"; then
    info "No ${field_name} to store — skipping OpenBao update."
    return 0
  fi
  info "Storing ${field_name} in OpenBao at secret/${bao_path}..."
  if bao_wait_ready 10; then
    bao_authenticate "$secrets_dir"
    bao_kv_patch "$bao_path" "${field_name}=${value}"
    info "  Stored at secret/${bao_path}"
  else
    warn "  OpenBao not reachable — saved locally in secrets/"
  fi
}

# ── NocoDB Env Generation ─────────────────────────────────────────────────────

generate_nocodb_env() {
  local env_file="$1" secrets_dir="$2"
  if [ -f "$env_file" ]; then
    info "$(basename "$env_file") already exists — skipping."
    return 0
  fi
  info "Generating $(basename "$env_file")..."
  local pg_pass jwt_secret
  pg_pass=$(get_secret "$secrets_dir" nocodb_pg_password)
  needs_gen "$pg_pass" && pg_pass=$(gen_secret)
  jwt_secret=$(get_secret "$secrets_dir" nocodb_jwt_secret)
  needs_gen "$jwt_secret" && jwt_secret=$(gen_secret)
  put_secret "$secrets_dir" nocodb_pg_password "$pg_pass"
  put_secret "$secrets_dir" nocodb_jwt_secret "$jwt_secret"
  cat > "$env_file" << EOF
POSTGRES_USER=nocodb
POSTGRES_PASSWORD=${pg_pass}
POSTGRES_DB=nocodb
NC_DB=pg://workflow-nocodb-postgres:5432?u=nocodb&p=${pg_pass}&d=nocodb
NC_AUTH_JWT_SECRET=${jwt_secret}
EOF
  chmod 600 "$env_file"
}

# ── n8n Env Generation ────────────────────────────────────────────────────────

generate_n8n_env() {
  local env_file="$1" secrets_dir="$2"
  if [ -f "$env_file" ]; then
    info "$(basename "$env_file") already exists — skipping."
    return 0
  fi
  info "Generating $(basename "$env_file")..."
  local admin_pass user_pass enc_key
  admin_pass=$(get_secret "$secrets_dir" n8n_admin_password)
  needs_gen "$admin_pass" && admin_pass=$(gen_secret 24 48)
  user_pass=$(get_secret "$secrets_dir" n8n_user_password)
  needs_gen "$user_pass" && user_pass=$(gen_secret 24 48)
  enc_key=$(get_secret "$secrets_dir" n8n_encryption_key)
  needs_gen "$enc_key" && enc_key=$(gen_secret 32 64)
  put_secret "$secrets_dir" n8n_admin_password "$admin_pass"
  put_secret "$secrets_dir" n8n_user_password "$user_pass"
  put_secret "$secrets_dir" n8n_encryption_key "$enc_key"
  cat > "$env_file" << EOF
POSTGRES_USER=n8n_admin
POSTGRES_PASSWORD=${admin_pass}
POSTGRES_DB=n8n
POSTGRES_NON_ROOT_USER=n8n_user
POSTGRES_NON_ROOT_PASSWORD=${user_pass}
DB_POSTGRESDB_PASSWORD=${user_pass}
N8N_ENCRYPTION_KEY=${enc_key}
EOF
  chmod 600 "$env_file"
}

# ── Semaphore Env Generation ──────────────────────────────────────────────────

generate_semaphore_env() {
  local env_file="$1" secrets_dir="$2"
  if [ -f "$env_file" ]; then
    info "$(basename "$env_file") already exists — skipping."
    return 0
  fi
  info "Generating $(basename "$env_file")..."
  local db_pass admin_pass runner_token
  db_pass=$(get_secret "$secrets_dir" semaphore_db_password)
  needs_gen "$db_pass" && db_pass=$(gen_secret 24 48)
  admin_pass=$(get_secret "$secrets_dir" semaphore_admin_password)
  needs_gen "$admin_pass" && admin_pass=$(gen_secret 16 32)
  runner_token=$(get_secret "$secrets_dir" semaphore_runner_token)
  needs_gen "$runner_token" && runner_token=$(gen_secret 24 48)
  put_secret "$secrets_dir" semaphore_db_password "$db_pass"
  put_secret "$secrets_dir" semaphore_admin_password "$admin_pass"
  put_secret "$secrets_dir" semaphore_runner_token "$runner_token"
  cat > "$env_file" << EOF
POSTGRES_USER=semaphore
POSTGRES_PASSWORD=${db_pass}
POSTGRES_DB=semaphore
SEMAPHORE_DB_PASS=${db_pass}
SEMAPHORE_ADMIN_PASSWORD=${admin_pass}
SEMAPHORE_RUNNER_REGISTRATION_TOKEN=${runner_token}
EOF
  chmod 600 "$env_file"
}
