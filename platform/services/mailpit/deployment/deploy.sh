#!/usr/bin/env bash
# Mailpit — local-dev mail catcher, container lifecycle only.
#
# LOCAL-DEV ONLY. Mailpit accepts every message and every AUTH credential, so nothing in production
# may point at it. There is no production inventory entry, Semaphore template or (Dev) variant for
# it, and the deploy playbook (platform/playbooks/deploy-mailpit.yml) refuses any host that is not a
# local-dev host before it changes anything.
#
# Mailpit has no secret: it holds no credential, and the mail it keeps is not persisted (no volume).
# So the only file this script writes is .env, from its environment (write_env): the compose
# substitutions, nothing secret. The script does NOT generate secrets; it pulls the image, starts
# the container and confirms it is running. The deploy playbook verifies the HTTP API and the SMTP
# port.
#
# Usage: ./deploy.sh [--no-pull]

set -euo pipefail

SKIP_PULL=false
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$(dirname "$(dirname "$(dirname "$SCRIPT_DIR")")")/lib"
cd "${SCRIPT_DIR}"

# shellcheck source=/dev/null
source "${LIB_DIR}/common.sh"

for arg in "$@"; do
  case "$arg" in
    --no-pull) SKIP_PULL=true ;;
    *) echo "Unknown option: $arg"; echo "Usage: ./deploy.sh [--no-pull]"; exit 1 ;;
  esac
done

# write_env: .env holds the values the compose files substitute, so compose_up_if_changed (which
# hashes .env) sees a changed image, bind or port. Only values the caller set are written; an unset
# one keeps its compose default. None is a secret: Mailpit has none.
write_env() {
  local v
  : > .env
  for v in MAILPIT_IMAGE MAILPIT_BIND MAILPIT_SMTP_PORT MAILPIT_HTTP_PORT; do
    if [ -n "${!v:-}" ]; then printf '%s=%s\n' "$v" "${!v}" >> .env; fi
  done
}

main() {
  local running
  info "=== Mailpit deployment (local-dev mail catcher, container lifecycle) ==="
  detect_runtime
  info "Container engine: ${CONTAINER_ENGINE}"
  # Before the pull: compose.yml takes the image from MAILPIT_IMAGE, which .env also carries.
  write_env
  if [ "$SKIP_PULL" = true ]; then info "Skipping image pull (--no-pull)."; else compose pull; fi
  # No extra input paths: the compose files and .env are the whole configuration. A change to either
  # recreates the container; an unchanged project is left alone (common.sh compose_up_if_changed).
  compose_up_if_changed
  # Poll the container is running. The playbook's verify play probes the API and the SMTP port.
  info "Waiting for the mailpit container to be running..."
  running=false
  for _ in $(seq 1 20); do
    if [ "$(${CONTAINER_ENGINE} inspect --format '{{.State.Status}}' mailpit 2>/dev/null)" = running ]; then
      info "mailpit running."
      running=true
      break
    fi
    sleep 2
  done
  if [ "$running" != true ]; then
    error "Mailpit container did not reach running state within timeout."
  fi
  info "=== Mailpit container lifecycle complete ==="
}

main "$@"
