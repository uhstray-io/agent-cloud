#!/usr/bin/env bash
# agentgateway — container lifecycle only.
#
# Ansible's deploy-agentgateway.yml renders .env (upstream key from OpenBao) and
# config.yaml (identities as sha256 hashes, upstream, limits) BEFORE this runs.
# This script does NOT generate secrets — it pulls the images, starts the gateway
# and its own Postgres, waits for the db to be healthy, then for the gateway's
# readiness listener (probed from the db container, see below).
#
# Why no `wait_for_healthy` on the GATEWAY: the image is a Chainguard glibc-dynamic
# base with no shell or curl, so a compose healthcheck cannot run inside it.
# Readiness is probed from the sibling db container over the compose network.
#
# Usage: ./deploy.sh [--no-pull|--pull-only]
# Steps (idempotent): verify rendered files, pull (unless --no-pull), decide, up
# (only when needed), wait db healthy, wait ready. --pull-only prepares images and exits.
#
# Change-aware (gateway task 1.12, design decision 11): a recreate drops every in-flight
# stream, and scheduled or imported runs call this deploy, so the gateway is recreated only
# when what it runs on differs from what it started with. The running container carries a
# label with the sha256 of its inputs (config.yaml, .env, the compose files in use, ./certs);
# a run whose inputs hash to the same value, with every project container on the image its tag
# names now, with readiness answering, leaves both containers alone. The label, not this run's render, is
# the record: a run that fails after rendering leaves the old label, so the next run still
# sees the difference and recreates.
#
# The last line of a run is `deploy-result: recreated (<reason>)` or `deploy-result: unchanged`;
# deploy-agentgateway.yml reads it for the task's changed status.

set -euo pipefail

SKIP_PULL=false
PULL_ONLY=false
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$(dirname "$(dirname "$(dirname "$SCRIPT_DIR")")")/lib"
cd "${SCRIPT_DIR}"

# shellcheck source=/dev/null
source "${LIB_DIR}/common.sh"

for arg in "$@"; do
  case "$arg" in
    --no-pull) SKIP_PULL=true ;;
    --pull-only) PULL_ONLY=true ;;
    *) echo "Unknown option: $arg"; echo "Usage: ./deploy.sh [--no-pull|--pull-only]"; exit 1 ;;
  esac
done
[ "$SKIP_PULL" = false ] || [ "$PULL_ONLY" = false ] || {
  echo "--no-pull and --pull-only cannot be combined." >&2
  exit 1
}

step_verify_rendered() {
  info "Step 1: Verifying rendered .env + config.yaml are present..."
  [ -f "${SCRIPT_DIR}/.env" ] || error "${SCRIPT_DIR}/.env not found. Run Ansible deploy-agentgateway.yml first."
  [ -f "${SCRIPT_DIR}/config.yaml" ] || error "${SCRIPT_DIR}/config.yaml not found. Run Ansible deploy-agentgateway.yml first."
  info "  both present."
}

step_pull_image() {
  if [ "$SKIP_PULL" = true ]; then info "Step 2: Skipping image pull (--no-pull)."; return 0; fi
  info "Step 2: Pulling image..."
  compose pull
}

# The label the gateway container is started with. A container created before this
# change carries none, so its first change-aware run recreates once.
INPUTS_LABEL="io.agent-cloud.inputs-sha256"
DEPLOY_RESULT=""

_sha256() {  # stdin -> hex digest; sha256sum on Linux, shasum where only it exists
  if command -v sha256sum >/dev/null 2>&1; then sha256sum | cut -d' ' -f1
  else shasum -a 256 | cut -d' ' -f1; fi
}

# The compose files compose() (platform/lib/common.sh) passes, in its order: the base, the
# local overlay when LOCAL_MODE=true and it exists, then COMPOSE_OVERLAYS. Hashing them
# means a toggled overlay (listener TLS) or an edited compose file recreates.
_compose_files() {
  echo compose.yml
  if [ "${LOCAL_MODE:-}" = "true" ] && [ -f compose.local.yml ]; then echo compose.local.yml; fi
  local ov
  for ov in ${COMPOSE_OVERLAYS:-}; do echo "$ov"; done
}

# inputs_digest: one sha256 over every file the gateway's container is built from or reads.
# ./certs is mounted as a directory and a renewal swaps `current/` (a symlink), so every
# file reachable under it is hashed by path. Only digests are combined; no content is printed.
# Every listing and every per-file digest is checked: a failure inside a process substitution
# or a command substitution passed as an argument is invisible to `set -e`/pipefail, and a
# digest over the files that happened to be readable is a guess, not a comparison.
inputs_digest() {
  local f listing digest combined="" files=()
  files+=(.env config.yaml)
  while IFS= read -r f; do files+=("$f"); done < <(_compose_files)
  if [ -d certs ]; then
    listing=$(find -L certs -type f) \
      || error "Cannot read certs: listing its files failed; refusing to guess whether the gateway's inputs changed."
    listing=$(printf '%s\n' "$listing" | LC_ALL=C sort)
    while IFS= read -r f; do if [ -n "$f" ]; then files+=("$f"); fi; done <<< "$listing"
  fi
  for f in "${files[@]}"; do
    if ! digest=$(_sha256 < "$f") || [ -z "$digest" ]; then
      error "Cannot read ${f} to hash the gateway's inputs; refusing to guess whether it changed."
    fi
    combined+="${digest}  ${f}"$'\n'
  done
  printf '%s' "$combined" | _sha256
}

_inspect() { $CONTAINER_ENGINE inspect --format "$1" agentgateway 2>/dev/null; }

# Every container this compose project created (stopped ones too), by the compose working-
# directory label: the same selector tasks/list-service-containers.yml uses, so the list is
# derived from the project rather than written down here.
_project_containers() {
  $CONTAINER_ENGINE ps -a --filter "label=com.docker.compose.project.working_dir=${SCRIPT_DIR}" \
    --format '{{.Names}}' 2>/dev/null
}

# One readiness probe, the same one step_wait_ready repeats.
_ready_once() {
  local addr
  addr=$(CONTAINER_ENGINE="$CONTAINER_ENGINE" "${SCRIPT_DIR}/gateway-addr.sh" 2>/dev/null) || return 1
  [ -n "$addr" ] || return 1
  $CONTAINER_ENGINE exec agentgateway-db wget -q -O /dev/null -T 3 "http://${addr}:19001/healthz/ready" 2>/dev/null
}

# step_decide: sets RECREATE_REASON, empty when the running gateway already matches.
step_decide() {
  info "Step 3: Comparing the rendered inputs with the running gateway..."
  WANT_DIGEST=$(inputs_digest)
  RECREATE_REASON=""
  local running have names name ref image_now image_run
  running=$(_inspect '{{.State.Running}}') || running=""
  if [ -z "$running" ]; then RECREATE_REASON="no gateway container"; return 0; fi
  if [ "$running" != "true" ]; then RECREATE_REASON="gateway container not running"; return 0; fi
  have=$(_inspect "{{ index .Config.Labels \"${INPUTS_LABEL}\" }}") || have=""
  if [ "$have" != "$WANT_DIGEST" ]; then RECREATE_REASON="inputs changed"; return 0; fi
  # A re-pulled tag moves to a new image without changing any input file, for EVERY service:
  # agentgateway-db runs a mutable tag too, so comparing only the gateway would leave the db on
  # the old image and report unchanged. `up -d --force-recreate` recreates the whole project.
  names=$(_project_containers) || names=""
  # The gateway is known to exist and run by now; a listing without it means the selector
  # did not match, and an empty comparison is not a match.
  case $'\n'"${names}"$'\n' in
    *$'\n'agentgateway$'\n'*) ;;
    *) RECREATE_REASON="project containers not listed"; return 0 ;;
  esac
  while IFS= read -r name; do
    [ -n "$name" ] || continue
    ref=$($CONTAINER_ENGINE inspect --format '{{.Config.Image}}' "$name" 2>/dev/null) || ref=""
    image_run=$($CONTAINER_ENGINE inspect --format '{{.Image}}' "$name" 2>/dev/null) || image_run=""
    image_now=$($CONTAINER_ENGINE image inspect --format '{{.Id}}' "$ref" 2>/dev/null) || image_now=""
    if [ -z "$image_now" ] || [ "$image_now" != "$image_run" ]; then
      RECREATE_REASON="image changed: ${name}"; return 0
    fi
  done <<< "$names"
  # Matching inputs on a gateway that does not answer readiness is not converged: a run whose
  # recreate never became ready left the new label behind.
  if ! _ready_once; then RECREATE_REASON="readiness not answering"; return 0; fi
  info "  running gateway matches the rendered inputs and image."
}

step_start() {
  if [ -z "$RECREATE_REASON" ]; then
    info "Step 4: Leaving the running gateway alone (no input or image change)."
    DEPLOY_RESULT="unchanged"
    return 0
  fi
  info "Step 4: Recreating agentgateway (${RECREATE_REASON})..."
  # --force-recreate: config.yaml and ./certs are bind mounts and .env is an env_file, and a
  # change to their CONTENT is not a compose-spec change, so plain `up -d` would leave the old
  # process on the old identities and key. The label rides a generated overlay, appended
  # after the declared ones, so the new container records the inputs it starts with.
  local label_dir
  label_dir=$(mktemp -d)
  # shellcheck disable=SC2064  # expand now: the trap must remove THIS directory
  trap "rm -rf '${label_dir}'" EXIT
  printf 'services:\n  agentgateway:\n    labels:\n      %s: "%s"\n' "$INPUTS_LABEL" "$WANT_DIGEST" \
    > "${label_dir}/inputs-label.yml"
  COMPOSE_OVERLAYS="${COMPOSE_OVERLAYS:-} ${label_dir}/inputs-label.yml" compose up -d --force-recreate
  DEPLOY_RESULT="recreated (${RECREATE_REASON})"
}

step_wait_db() {
  # The gateway dials Postgres at start (per-key budgets). depends_on gates the
  # start order; this makes the wait visible and bounded in the log.
  info "Step 5: Waiting for agentgateway-db to be healthy..."
  wait_for_healthy agentgateway-db 90
}

step_wait_ready() {
  # Probe from the sibling db container over the compose network: the gateway
  # image has no shell, and the host loopback is the WRONG vantage when this
  # script runs inside the local control plane (Semaphore container) rather than
  # on the host publishing the port. Works identically on the prod VM.
  info "Step 6: Waiting for the readiness listener (via agentgateway-db on the compose network)..."
  # By ADDRESS on the shared network, never by name: see gateway-addr.sh (task 1215, a VM
  # named `agentgateway` made the name resolve to the db container itself).
  local elapsed=0 addr=""
  while [ "$elapsed" -lt 90 ]; do
    addr=$(CONTAINER_ENGINE="$CONTAINER_ENGINE" "${SCRIPT_DIR}/gateway-addr.sh" 2>/dev/null) || addr=""
    if [ -n "$addr" ] && $CONTAINER_ENGINE exec agentgateway-db wget -q -O /dev/null -T 3 "http://${addr}:19001/healthz/ready" 2>/dev/null; then
      info "agentgateway readiness is responding."
      return 0
    fi
    sleep 3; elapsed=$((elapsed + 3))
  done
  # The loop's probe is quiet; say why it fails. Task 1213: the gateway logged itself ready on
  # 0.0.0.0:19001 and the probe still never answered, so name resolution and the probe's own
  # error are what the next run must show.
  warn "Readiness probe, verbose, from agentgateway-db (gateway address: ${addr:-none found}):"
  CONTAINER_ENGINE="$CONTAINER_ENGINE" "${SCRIPT_DIR}/gateway-addr.sh" 2>&1 | redact_secrets >&2 || true
  $CONTAINER_ENGINE exec agentgateway-db sh -c \
    "getent hosts agentgateway || echo 'agentgateway does not resolve here'; cat /etc/resolv.conf; [ -n '${addr}' ] && wget -S -O /dev/null -T 5 http://${addr}:19001/healthz/ready" \
    2>&1 | redact_secrets >&2 || true
  dump_container_diagnostics agentgateway
  dump_container_diagnostics agentgateway-db 20
  error "agentgateway readiness did not respond within 90s (diagnostics above)"
}

main() {
  info "=== agentgateway deployment (container lifecycle) ==="
  detect_runtime
  info "Container engine: ${CONTAINER_ENGINE}"
  step_verify_rendered
  step_pull_image
  if [ "$PULL_ONLY" = true ]; then
    info "=== agentgateway image pull complete; no containers were changed ==="
    echo "image-pull-result: complete"
    return 0
  fi
  step_decide
  step_start
  step_wait_db
  step_wait_ready
  info "=== agentgateway container lifecycle complete ==="
  echo "deploy-result: ${DEPLOY_RESULT}"
}

main "$@"
