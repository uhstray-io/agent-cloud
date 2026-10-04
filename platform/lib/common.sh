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
  # Engine may be preset by the deploy playbooks (environment: CONTAINER_ENGINE).
  # Still derive COMPOSE_CMD when unset — early-returning without it left
  # compose() running an empty command (latent bug found by local-dev).
  if [ -n "${CONTAINER_ENGINE:-}" ]; then
    if [ -z "${COMPOSE_CMD:-}" ]; then
      case "$CONTAINER_ENGINE" in
        podman)
          COMPOSE_CMD="podman-compose"
          command -v podman-compose &>/dev/null || COMPOSE_CMD="podman compose"
          ;;
        docker)
          COMPOSE_CMD="docker compose"
          ;;
        *)
          error "Unknown CONTAINER_ENGINE: ${CONTAINER_ENGINE}"
          ;;
      esac
    fi
    return 0
  fi
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
#
# COMPOSE_OVERLAYS — optional, space-separated extra overlay files, appended
# LAST (after the local overlay) so a declared overlay always wins. The deploy
# playbook sets it from inventory (e.g. postiz's search-node gate); each named
# file must exist, because a missing -f is how a gated topology silently
# degrades to the base one while the run reports success.
compose() {
  detect_runtime
  local files=(-f compose.yml)
  if [ "${LOCAL_MODE:-}" = "true" ] && [ -f compose.local.yml ]; then
    files+=(-f compose.local.yml)
  fi
  local ov
  for ov in ${COMPOSE_OVERLAYS:-}; do
    [ -f "$ov" ] || error "COMPOSE_OVERLAYS names '$ov', which does not exist here ($(pwd))"
    files+=(-f "$ov")
  done
  $COMPOSE_CMD "${files[@]}" "$@"
}

# ── Change-aware start ────────────────────────────────────────────────────────
#
# compose_up_if_changed [input-path ...]
# Start a compose project, recreating its containers ONLY when what they run on changed.
# A re-run with identical inputs is a true no-op (operator decision 2026-10-04). Plain
# `up -d` is not enough on its own: env_file content and bind-mounted config are not part of
# the compose spec, so a changed .env would leave the old process on stale values. So every
# input that must trigger a recreate is hashed — the compose files in effect, .env,
# env/*.env, plus each path the caller names (a file, or a directory hashed recursively,
# following symlinks) — and the digest rides a label on every service, through a generated
# overlay. The project's running containers are compared against it: a missing, stopped,
# orphaned (service no longer declared) or unlabelled container, a different digest, or a container whose image ID is not the one its
# tag names now (a re-pulled tag) recreates the whole project with --force-recreate. The
# label, not this run's render, is the record: a run that fails after rendering keeps the old
# label, so the next run still sees the difference.
# Prior art: agentgateway's deploy.sh (gateway task 1.12), which keeps its own copy.
#
# Prints `DEPLOY_CHANGED=true` or `DEPLOY_CHANGED=false` on its own line; playbooks read it
# for the task's changed status. Run from the deploy directory, like compose().
COMPOSE_INPUTS_LABEL="io.agent-cloud.inputs-sha256"

_compose_sha256() {  # stdin -> hex digest; sha256sum on Linux, shasum where only it exists
  if command -v sha256sum >/dev/null 2>&1; then sha256sum | cut -d' ' -f1
  else shasum -a 256 | cut -d' ' -f1; fi
}

# compose_files: the compose files compose() passes, in its order.
compose_files() {
  echo compose.yml
  if [ "${LOCAL_MODE:-}" = "true" ] && [ -f compose.local.yml ]; then echo compose.local.yml; fi
  local ov
  for ov in ${COMPOSE_OVERLAYS:-}; do echo "$ov"; done
}

# compose_services: every service in the EFFECTIVE config — the same file list compose() uses,
# so a service only an overlay declares (postiz's compose.search.yml) is labelled too. Both
# providers print one name per line for `config --services` (docker compose v2;
# podman-compose 1.6.0). If the provider cannot, every file compose_files names is parsed for
# its top-level service keys instead.
compose_services() {
  local out
  if out=$(compose config --services 2>/dev/null) && [ -n "$out" ]; then
    printf '%s\n' "$out"; return 0
  fi
  local f
  while IFS= read -r f; do
    awk '/^services:[[:space:]]*$/ {s=1; next}
         s && /^[^[:space:]#]/ {s=0}
         s && /^  [A-Za-z0-9._-]+:[[:space:]]*$/ {sub(/^  /, ""); sub(/:.*/, ""); print}' "$f"
  done < <(compose_files) | awk '!seen[$0]++'
}

# _compose_refuse_symlink_loop <dir>: refuse a directory symlink that points at itself or an
# ancestor. GNU find -L reports such a loop and fails (refused below); BSD find skips it
# silently, so the inputs would hash one way on Linux and another on a Mac. Checked here so
# both refuse. A loop through two links, neither pointing at its own ancestor, is left to
# find: GNU fails on it, BSD skips it — neither hangs.
_compose_refuse_symlink_loop() {
  local l tgt dir links
  links=$(find "$1" -type l) || error "Cannot list ${1}; refusing to guess whether the service's inputs changed."
  while IFS= read -r l; do
    [ -n "$l" ] && [ -d "$l" ] || continue
    tgt=$(cd -P "$l" && pwd) || error "Cannot resolve ${l}; refusing to guess."
    dir=$(cd -P "$(dirname "$l")" && pwd) || error "Cannot resolve ${l}; refusing to guess."
    case "${dir}/" in
      "${tgt}/"*) error "Symlink loop at ${l}; refusing to guess whether the service's inputs changed." ;;
    esac
  done <<< "$links"
}

# compose_inputs_digest [input-path ...]: one sha256 over every input file, by path.
# Every listing and per-file digest is checked: a digest over whichever files happened to be
# readable is a guess, not a comparison. Only digests are combined; no content is printed.
compose_inputs_digest() {
  local f p listing digest combined="" files=()
  while IFS= read -r f; do files+=("$f"); done < <(compose_files)
  files+=(.env)
  for f in env/*.env; do [ -f "$f" ] && files+=("$f"); done
  for p in "$@"; do
    if [ -d "$p" ]; then
      _compose_refuse_symlink_loop "$p"
      listing=$(find -L "$p" -type f) \
        || error "Cannot list ${p}; refusing to guess whether the service's inputs changed."
      listing=$(printf '%s\n' "$listing" | LC_ALL=C sort)
      while IFS= read -r f; do if [ -n "$f" ]; then files+=("$f"); fi; done <<< "$listing"
    else
      files+=("$p")
    fi
  done
  for f in "${files[@]}"; do
    if ! digest=$(_compose_sha256 < "$f") || [ -z "$digest" ]; then
      error "Cannot read ${f} to hash the service's inputs; refusing to guess whether it changed."
    fi
    combined+="${digest}  ${f}"$'\n'
  done
  printf '%s' "$combined" | _compose_sha256
}

# _compose_recreate_reason <digest> <services>: empty when every project container matches.
# A container whose service is no longer declared is an orphan: it counts as a change, and
# the recreate's --remove-orphans removes it, so the run after is stable.
_compose_recreate_reason() {
  local want="$1" services="$2" names name svc seen=$'\n' running have ref image_run image_now
  names=$($CONTAINER_ENGINE ps -a --filter "label=com.docker.compose.project.working_dir=$(pwd)" \
    --format '{{.Names}}' 2>/dev/null) || names=""
  while IFS= read -r name; do
    [ -n "$name" ] || continue
    svc=$($CONTAINER_ENGINE inspect --format '{{ index .Config.Labels "com.docker.compose.service" }}' "$name" 2>/dev/null) || svc=""
    case $'\n'"${services}"$'\n' in
      *$'\n'"${svc}"$'\n'*) ;;
      *) echo "orphan container: ${name}"; return 0 ;;
    esac
    seen+="${svc}"$'\n'
    running=$($CONTAINER_ENGINE inspect --format '{{.State.Running}}' "$name" 2>/dev/null) || running=""
    if [ "$running" != "true" ]; then echo "container not running: ${name}"; return 0; fi
    have=$($CONTAINER_ENGINE inspect --format "{{ index .Config.Labels \"${COMPOSE_INPUTS_LABEL}\" }}" "$name" 2>/dev/null) || have=""
    if [ "$have" != "$want" ]; then echo "inputs changed: ${name}"; return 0; fi
    ref=$($CONTAINER_ENGINE inspect --format '{{.Config.Image}}' "$name" 2>/dev/null) || ref=""
    image_run=$($CONTAINER_ENGINE inspect --format '{{.Image}}' "$name" 2>/dev/null) || image_run=""
    image_now=$($CONTAINER_ENGINE image inspect --format '{{.Id}}' "$ref" 2>/dev/null) || image_now=""
    if [ -z "$image_now" ] || [ "$image_now" != "$image_run" ]; then echo "image changed: ${name}"; return 0; fi
  done <<< "$names"
  while IFS= read -r svc; do
    [ -n "$svc" ] || continue
    case "$seen" in
      *$'\n'"${svc}"$'\n'*) ;;
      *) echo "no container for service: ${svc}"; return 0 ;;
    esac
  done <<< "$services"
}

compose_up_if_changed() {
  detect_runtime
  local want services reason label_dir svc
  want=$(compose_inputs_digest "$@")
  services=$(compose_services)
  [ -n "$services" ] || error "No services found in the compose files; refusing to guess."
  reason=$(_compose_recreate_reason "$want" "$services")
  if [ -z "$reason" ]; then
    info "Every container matches its inputs and image; leaving the project alone."
    echo "DEPLOY_CHANGED=false"
    return 0
  fi
  info "Recreating the project (${reason})..."
  label_dir=$(mktemp -d)
  {
    echo "services:"
    while IFS= read -r svc; do
      [ -n "$svc" ] || continue
      printf '  %s:\n    labels:\n      %s: "%s"\n' "$svc" "$COMPOSE_INPUTS_LABEL" "$want"
    done <<< "$services"
  } > "${label_dir}/inputs-label.yml"
  # --remove-orphans: a service dropped from the compose files would otherwise leave its old
  # labelled container behind and every later run would recreate (podman-compose 1.6.0 and
  # docker compose v2 both accept it).
  if ! COMPOSE_OVERLAYS="${COMPOSE_OVERLAYS:-} ${label_dir}/inputs-label.yml" compose up -d --force-recreate --remove-orphans; then
    rm -rf "$label_dir"
    error "compose up --force-recreate failed."
  fi
  rm -rf "$label_dir"
  echo "DEPLOY_CHANGED=true"
}

# ── Health Waiters ────────────────────────────────────────────────────────────

# redact_secrets — filter stdin so a container log can go into a task log (Semaphore stores
# task output). It does not try to find where a value ends, because a value can hold a space, a
# comma or a quote (PR 231 Codex review): after an Authorization header or a password, passwd,
# secret, token, api-key or bare key label (the last is how agentgateway's local-dev config
# carries a client key), the REST OF THE LINE is blanked. Up to three words may sit between the
# label and its `:` or `=`: step-ca 0.30.2 prints "Your CA administrative password is: <the key
# password>" on first boot, which a label-then-colon rule let through. A URL's userinfo is
# blanked up to its last `@`, and a Bearer token wherever it appears. Over-redaction is the accepted cost. It is
# still a best-effort filter, not a guarantee: an unlabelled secret passes through, so logs are
# dumped only on failure.
redact_secrets() {
  sed -E \
    -e 's#(://[^:/@[:space:]]+:)[^[:space:]]*@#\1***@#g' \
    -e 's#([Bb][Ee][Aa][Rr][Ee][Rr][[:space:]]+)[^[:space:]]+#\1***#g' \
    -e 's#([Aa][Uu][Tt][Hh][Oo][Rr][Ii][Zz][Aa][Tt][Ii][Oo][Nn]["'"'"']?[[:space:]]*[:=]).*$#\1 ***#' \
    -e 's#(^|[^[:alnum:]])(([Pp][Aa][Ss][Ss][Ww][Oo][Rr][Dd]|[Pp][Aa][Ss][Ss][Ww][Dd]|[Ss][Ee][Cc][Rr][Ee][Tt]|[Tt][Oo][Kk][Ee][Nn]|[Aa][Pp][Ii]_?[Kk][Ee][Yy]|[Kk][Ee][Yy])["'"'"']?([[:space:]]+[[:alpha:]]+){0,3}[[:space:]]*[:=]).*$#\1\2 ***#'
}

# dump_container_diagnostics <container_name> [lines]
# On a failed wait, print the container's state and the tail of its log
# (redacted), so a failed deploy says WHY in its own output instead of pointing
# at a host nobody may log into. Never fails the caller.
dump_container_diagnostics() {
  local name="$1" lines="${2:-60}"
  warn "Diagnostics for ${name}:"
  $CONTAINER_ENGINE inspect --format \
    'state={{.State.Status}} exit={{.State.ExitCode}} restarts={{.RestartCount}} started={{.State.StartedAt}} error={{.State.Error}}' \
    "$name" 2>&1 | redact_secrets >&2 || true
  warn "Last ${lines} log lines of ${name} (redacted, engine timestamps):"
  # Timestamps place the container's own events against the wait that timed out (task 1213:
  # a healthy gateway, a probe that never saw it). An engine without the flag prints plain lines.
  { $CONTAINER_ENGINE logs --timestamps --tail "$lines" "$name" 2>&1 \
      || $CONTAINER_ENGINE logs --tail "$lines" "$name" 2>&1; } | redact_secrets >&2 || true
}

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
  dump_container_diagnostics "$name"
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

# n8n's env generation used to live here (generate_n8n_env). Removed after the
# composable cutover (2026-09-02): n8n's secrets are OpenBao-sourced via
# manage-secrets + n8n.env.j2, and its stateful values were pre-seeded from the
# live instance (seed-n8n-secrets.yml). generate_nocodb_env above stays only
# until the NocoDB decommission change retires that service (replaced by
# tududi); nothing new may call it.

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
