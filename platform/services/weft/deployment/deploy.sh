#!/usr/bin/env bash
# weft — container lifecycle only, for one workspace or one front door.
#
# Ansible's deploy-weft-workspace.yml / deploy-weft-door.yml render .env (the release's image
# digests and the paths, no secret), secrets.env (workspace) or door.key (front door) BEFORE this
# runs, and they decide the order of the steps below. This script never reads OpenBao, never
# generates a secret, and never chooses a version: the image it runs is the digest .env names,
# so a rollback is a re-rendered .env and the same commands.
#
# Usage: ./deploy.sh <workspace|door> <command> [args]
#
#   pull                       pull every image .env names
#   up                         start: the board first (opening the catalog migrates it), then the
#                              orchestrator, then capture. A front door is one container.
#   stop                       stop: the orchestrator first, then capture, then the board
#   backup --from V --to V [--release FILE]
#                              the binary's own `backup --before-deploy`; prints the set's path
#   restore --from SET [--label WORD]
#                              the binary's own `restore`, which refuses while the catalog is open
#                              and sets the current file aside rather than deleting it
#   health [args]              the binary's own `health`, JSON on stdout, non-zero when it failed
#   restarts                   one line per container: <name> <restart count>
#
# backup, restore and health run the binary in a one-off container of the image .env names,
# with the role's directory mounted at its own path. They are the release's own tools for its
# own data (docs/cicd.md §6), not credential handling, so they belong here.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$(dirname "$(dirname "$(dirname "$SCRIPT_DIR")")")/lib"
cd "${SCRIPT_DIR}"

# shellcheck source=/dev/null
source "${LIB_DIR}/common.sh"

usage() {
  echo "Usage: ./deploy.sh <workspace|door> <pull|up|stop|backup|restore|health|restarts> [args]"
  exit 1
}

[ $# -ge 2 ] || usage
ROLE="$1"
COMMAND="$2"
shift 2

case "$ROLE" in
  workspace)
    export COMPOSE_BASE=compose.workspace.yml
    # Start order; stop is the reverse. The board opens the catalog first because opening it
    # migrates it, and nothing else may race that.
    SERVICES=(board orchestrator capture)
    CONTAINERS=(weft-board weft-orchestrator weft-capture)
    REQUIRED=(WEFT_WORKSPACE_IMAGE WEFT_WORKSPACE_DIR WEFT_BOARD_BIND WEFT_BOARD_PORT WEFT_VOICE_UDP WEFT_PODMAN_SOCKET)
    ;;
  door)
    export COMPOSE_BASE=compose.door.yml
    SERVICES=(door)
    CONTAINERS=(weft-door)
    REQUIRED=(WEFT_DOOR_IMAGE WEFT_DOOR_DIR WEFT_DOOR_BIND WEFT_DOOR_PORT WEFT_DOOR_WORKSPACE_URL)
    ;;
  *) usage ;;
esac

# Every value compose substitutes must be present and non-empty. An unset variable in a compose
# file becomes an empty string, and an empty image or mount source is a container that starts
# from the wrong place rather than one that refuses to start.
read_env() {
  [ -f .env ] || error "${SCRIPT_DIR}/.env not found. Run the deploy-weft-${ROLE} playbook first."
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

image() {
  if [ "$ROLE" = workspace ]; then echo "$WEFT_WORKSPACE_IMAGE"; else echo "$WEFT_DOOR_IMAGE"; fi
}

# One-off run of the role's binary against its directory. Same user mapping as the services, so
# files it writes belong to the deploy user on the VM, as the running containers' files do.
oneoff() {
  local dir args=(--rm --userns "keep-id:uid=10001,gid=10001")
  if [ "$ROLE" = workspace ]; then
    dir="$WEFT_WORKSPACE_DIR"
    args+=(-v "${dir}:${dir}" -e "WEFT_DB=${dir}/weft.db" -e "WEFT_CONFIG=${dir}/weft.toml" "$(image)" orchestrator)
  else
    dir="$WEFT_DOOR_DIR"
    # The door image's entrypoint is front-door. No key is mounted: none of its deploy commands
    # signs anything.
    args+=(-v "${dir}:${dir}" -e "WEFT_FRONT_DOOR_RECORDS=${dir}/front-door.db" "$(image)")
  fi
  "$CONTAINER_ENGINE" run "${args[@]}" "$@"
}

cmd_pull() {
  info "Pulling $(image)..."
  require_digest "$(image)"
  "$CONTAINER_ENGINE" pull "$(image)"
}

cmd_up() {
  require_digest "$(image)"
  # --force-recreate: a new digest in .env is a new container, and a rollback must replace the
  # failed one rather than leave it running beside the restored catalog.
  if [ "$ROLE" = workspace ]; then
    info "Starting the board (opening the catalog migrates it)..."
    compose up -d --no-deps --force-recreate board
    wait_for_healthy weft-board 300
    info "Starting the orchestrator, then capture..."
    compose up -d --no-deps --force-recreate orchestrator
    compose up -d --no-deps --force-recreate capture
  else
    info "Starting the front door..."
    compose up -d --no-deps --force-recreate door
    wait_for_http "http://${WEFT_DOOR_BIND}:${WEFT_DOOR_PORT}/" "front door" 120
  fi
}

cmd_stop() {
  local i
  # Reverse of the start order: the orchestrator ends its agent sessions before the board that
  # people are reading goes away.
  for ((i = ${#SERVICES[@]} - 1; i >= 0; i--)); do
    info "Stopping ${SERVICES[$i]}..."
    compose stop "${SERVICES[$i]}" || warn "${SERVICES[$i]} was not running"
  done
}

cmd_restarts() {
  local name
  for name in "${CONTAINERS[@]}"; do
    printf '%s %s\n' "$name" \
      "$("$CONTAINER_ENGINE" inspect --format '{{.RestartCount}}' "$name" 2>/dev/null || echo missing)"
  done
}

main() {
  detect_runtime
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
