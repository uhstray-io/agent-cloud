#!/usr/bin/env bash
# weft workspace — container lifecycle only, for one company's workspace on its own VM.
#
# Ansible's weft-workspace.yml renders .env (the release's image digests and the paths, no secret),
# weft.toml and seal.key BEFORE this runs, and decides the order of the steps below. This script
# never reads OpenBao, never generates a secret, and never chooses a version: the image it runs is
# the digest .env names, so going back to an earlier release is a re-rendered .env and the same
# commands.
#
# Usage: ./deploy.sh <command> [args]
#
#   pull                       pull the image .env names
#   up                         start: the board first (opening the catalog migrates it), then the
#                              orchestrator, then capture
#   stop                       stop: the orchestrator first, then capture, then the board. With no
#                              .env nothing was ever started here, so there is nothing to stop
#   images                     one line per container: <image> <running>, or <name> missing
#   backup --from V --to V     the binary's own `backup --before-deploy`; prints the set's path
#   restore --from SET [--label WORD]
#                              the binary's own `restore`, which refuses while the catalog is open
#                              and sets the current file aside rather than deleting it
#   health [args]              the binary's own `health`, JSON on stdout, non-zero when it failed
#   restarts                   one line per container: <name> <restart count>
#
# backup, restore and health run the binary in a one-off container of the image .env names,
# with the workspace directory mounted at its own path. They are the release's own tools for its
# own data (docs/cicd.md §6), not credential handling, so they belong here.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$(dirname "$(dirname "$(dirname "$SCRIPT_DIR")")")/lib"
cd "${SCRIPT_DIR}"

# shellcheck source=/dev/null
source "${LIB_DIR}/common.sh"

# Start order; stop is the reverse. The board opens the catalog first because opening it migrates
# it, and nothing else may race that.
SERVICES=(board orchestrator capture)
CONTAINERS=(weft-board weft-orchestrator weft-capture)
REQUIRED=(WEFT_WORKSPACE_IMAGE WEFT_WORKSPACE_DIR WEFT_WORKSPACE_CONFIG WEFT_BOARD_BIND WEFT_BOARD_PORT WEFT_VOICE_UDP WEFT_PODMAN_SOCKET)

usage() {
  echo "Usage: ./deploy.sh <pull|up|stop|images|backup|restore|health|restarts> [args]"
  exit 1
}

[ $# -ge 1 ] || usage
COMMAND="$1"
shift

# Every value compose substitutes must be present and non-empty. An unset variable in a compose
# file becomes an empty string, and an empty image or mount source is a container that starts
# from the wrong place rather than one that refuses to start.
read_env() {
  [ -f .env ] || error "${SCRIPT_DIR}/.env not found. Run the weft workspace deploy first."
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

# One-off run of the workspace binary against its directory. Same user mapping as the services, so
# files it writes belong to the deploy user on the VM, as the running containers' files do.
# weft.toml is outside the workspace directory and read-only.
oneoff() {
  local dir="$WEFT_WORKSPACE_DIR"
  "$CONTAINER_ENGINE" run --rm --userns "keep-id:uid=10001,gid=10001" \
    -v "${dir}:${dir}" -v "${WEFT_WORKSPACE_CONFIG}:${WEFT_WORKSPACE_CONFIG}:ro" \
    -e "WEFT_DB=${dir}/weft.db" -e "WEFT_CONFIG=${WEFT_WORKSPACE_CONFIG}" \
    "$WEFT_WORKSPACE_IMAGE" orchestrator "$@"
}

cmd_pull() {
  require_digest "$WEFT_WORKSPACE_IMAGE"
  info "Pulling ${WEFT_WORKSPACE_IMAGE}..."
  "$CONTAINER_ENGINE" pull "$WEFT_WORKSPACE_IMAGE"
}

cmd_up() {
  require_digest "$WEFT_WORKSPACE_IMAGE"
  # --force-recreate: a new digest in .env is a new container, and a rollback must replace the
  # failed one rather than leave it running beside the restored catalog.
  info "Starting the board (opening the catalog migrates it)..."
  compose up -d --no-deps --force-recreate board
  wait_for_healthy weft-board 300
  info "Starting the orchestrator, then capture..."
  compose up -d --no-deps --force-recreate orchestrator
  compose up -d --no-deps --force-recreate capture
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

# Read only, and needs no .env: the deploy asks before it renders one.
cmd_images() {
  local name
  for name in "${CONTAINERS[@]}"; do
    "$CONTAINER_ENGINE" inspect --format '{{.ImageName}} {{.State.Running}}' "$name" 2>/dev/null \
      || printf '%s missing\n' "$name"
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
  case "$COMMAND" in
    images) cmd_images; return ;;
    # Every weft container is started from a rendered .env, so a host without one runs none of
    # them. A deploy stops before it renders .env, and on a fresh host that stop used to fail here
    # and abort the first deploy (agent-cloud#890 B1).
    stop)
      if [ ! -f .env ]; then
        info "No .env here: no workspace container was started from this directory, so there is nothing to stop."
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
