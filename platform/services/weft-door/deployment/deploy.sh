#!/usr/bin/env bash
# weft front door — container lifecycle only, for the one front door of an environment.
#
# Ansible's weft-door.yml renders .env (the release's image digest and the paths, no secret) and
# door.key BEFORE this runs, and decides the order of the steps below. This script never reads
# OpenBao, never generates a secret, and never chooses a version: the image it runs is the digest
# .env names.
#
# Usage: ./deploy.sh <command> [args]
#
#   pull                       pull the image .env names
#   up                         start the front door and wait for its page
#   stop                       stop it. With no .env nothing was ever started here
#   images                     <image> <running>, or weft-door missing
#   backup --from V --to V     the binary's own `backup --before-deploy`; prints the set's path
#   restore --from SET [--label WORD]
#                              the binary's own `restore`, which sets the current file aside
#   health                     the binary's own `health`: every workspace on the records agrees
#                              about its id; non-zero when one names another
#   restarts                   weft-door <restart count>
#
# backup, restore and health run the binary in a one-off container of the image .env names, with
# the records' directory mounted at its own path. No key is mounted: none of them signs anything.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$(dirname "$(dirname "$(dirname "$SCRIPT_DIR")")")/lib"
cd "${SCRIPT_DIR}"

# shellcheck source=/dev/null
source "${LIB_DIR}/common.sh"

CONTAINER=weft-door
REQUIRED=(WEFT_DOOR_IMAGE WEFT_DOOR_DIR WEFT_DOOR_BIND WEFT_DOOR_PORT WEFT_DOOR_WORKSPACE_URL)

usage() {
  echo "Usage: ./deploy.sh <pull|up|stop|images|backup|restore|health|restarts> [args]"
  exit 1
}

[ $# -ge 1 ] || usage
COMMAND="$1"
shift

# Every value compose substitutes must be present and non-empty: an empty image or mount source is
# a container that starts from the wrong place rather than one that refuses to start.
read_env() {
  [ -f .env ] || error "${SCRIPT_DIR}/.env not found. Run the weft front door deploy first."
  local key value
  for key in "${REQUIRED[@]}"; do
    value=$(grep -E "^${key}=" .env | tail -1 | cut -d= -f2-)
    [ -n "$value" ] || error ".env has no value for ${key}"
    printf -v "$key" '%s' "$value"
  done
}

# A digest, never a tag: the signature was checked on the digest, and a tag can move after.
require_digest() {
  local image="$1"
  [[ "$image" =~ @sha256:[0-9a-f]{64}$ ]] || error "${image} is not pinned by digest"
}

# The door image's entrypoint is front-door.
oneoff() {
  local dir="$WEFT_DOOR_DIR"
  "$CONTAINER_ENGINE" run --rm --userns "keep-id:uid=10001,gid=10001" \
    -v "${dir}:${dir}" -e "WEFT_FRONT_DOOR_RECORDS=${dir}/front-door.db" \
    "$WEFT_DOOR_IMAGE" "$@"
}

cmd_pull() {
  require_digest "$WEFT_DOOR_IMAGE"
  info "Pulling ${WEFT_DOOR_IMAGE}..."
  "$CONTAINER_ENGINE" pull "$WEFT_DOOR_IMAGE"
}

cmd_up() {
  require_digest "$WEFT_DOOR_IMAGE"
  info "Starting the front door..."
  compose up -d --no-deps --force-recreate door
  wait_for_http "http://${WEFT_DOOR_BIND}:${WEFT_DOOR_PORT}/" "front door" 120
}

cmd_stop() {
  info "Stopping the front door..."
  compose stop door || warn "the front door was not running"
}

# Read only, and needs no .env: the deploy asks before it renders one.
cmd_images() {
  "$CONTAINER_ENGINE" inspect --format '{{.ImageName}} {{.State.Running}}' "$CONTAINER" 2>/dev/null \
    || printf '%s missing\n' "$CONTAINER"
}

cmd_restarts() {
  printf '%s %s\n' "$CONTAINER" \
    "$("$CONTAINER_ENGINE" inspect --format '{{.RestartCount}}' "$CONTAINER" 2>/dev/null || echo missing)"
}

main() {
  detect_runtime
  case "$COMMAND" in
    images) cmd_images; return ;;
    # The front door is started from a rendered .env, so a host without one runs nothing, and the
    # stop a deploy runs before it renders .env must find nothing to do (agent-cloud#890 B1).
    stop)
      if [ ! -f .env ]; then
        info "No .env here: the front door was never started from this directory, so there is nothing to stop."
        return
      fi ;;
  esac
  read_env
  case "$COMMAND" in
    pull) cmd_pull ;;
    up) cmd_up ;;
    stop) cmd_stop ;;
    backup) oneoff backup --before-deploy "$@" ;;
    restore) oneoff restore "$@" ;;
    health) oneoff health "$@" ;;
    restarts) cmd_restarts ;;
    *) usage ;;
  esac
}

main "$@"
