#!/usr/bin/env bash
# deploy.sh — install, configure and run one self-hosted GitHub Actions runner.
#
# Lifecycle only. It installs the runner, registers it, and manages its service. It
# does NOT generate, fetch or store secrets: the registration token is minted by
# Ansible from the App credential in OpenBao and handed to this script on STDIN, the
# same division the platform's other services keep (AGENTS.md, Critical Deployment
# Rules 2 and 4).
#
# Why this is a host service and not a container, when the platform's norm is rootless
# podman: the runner's whole job is to CREATE containers for each job it accepts. Running
# it inside one buys nothing and costs the nested-container problem. Isolation is instead
# enforced per job (see the env template) and by the unprivileged account it runs as.
#
# Reads its configuration from an env file rendered by Ansible (config/runner.env).
# Everything it needs is declared there; nothing is discovered at run time.
#
# Usage:
#   printf '%s' "$REG_TOKEN" | ./deploy.sh configure
#   ./deploy.sh start | stop | status | remove   (remove reads a REMOVE token on stdin)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
. "${SCRIPT_DIR}/../../../lib/common.sh"

ENV_FILE="${RUNNER_ENV_FILE:-${SCRIPT_DIR}/config/runner.env}"
[ -r "$ENV_FILE" ] || error "missing $ENV_FILE — Ansible renders it before this runs"
set -a
# shellcheck source=/dev/null
. "$ENV_FILE"
set +a

: "${RUNNER_HOME:?RUNNER_HOME must be set in runner.env}"
: "${RUNNER_VERSION:?RUNNER_VERSION must be set in runner.env}"
: "${RUNNER_NAME:?RUNNER_NAME must be set in runner.env}"
: "${RUNNER_LABELS:?RUNNER_LABELS must be set in runner.env}"
: "${RUNNER_GROUP:?RUNNER_GROUP must be set in runner.env}"
: "${RUNNER_ORG_URL:?RUNNER_ORG_URL must be set in runner.env}"
: "${HOOKS_VERSION:?HOOKS_VERSION must be set in runner.env}"

RUNNER_TGZ="actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz"
RUNNER_URL="https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/${RUNNER_TGZ}"
HOOKS_ZIP="actions-runner-hooks-docker-${HOOKS_VERSION}.zip"
HOOKS_URL="https://github.com/actions/runner-container-hooks/releases/download/v${HOOKS_VERSION}/${HOOKS_ZIP}"

# ── integrity ─────────────────────────────────────────────────────────────────
# The expected digest is not hardcoded here. It is published in the release metadata
# for the pinned version, so it is fetched for THAT version and compared — a constant
# copied into this file would silently go stale the first time the pin moves, and a
# stale-but-present digest reads as verified.
verify_digest() {
  local file="$1" expected="$2" actual
  [ -n "$expected" ] || error "no published digest for $(basename "$file") — refusing to run an unverified artefact"
  actual=$(sha256sum "$file" | cut -d' ' -f1)
  if [ "$actual" != "$expected" ]; then
    rm -f "$file"
    error "digest mismatch for $(basename "$file"): expected $expected, got $actual"
  fi
  info "digest verified: $(basename "$file")"
}

fetch() {
  local url="$1" dest="$2"
  [ -f "$dest" ] && { info "already present: $(basename "$dest")"; return 0; }
  info "downloading $(basename "$dest")"
  curl -fsSL --retry 3 --retry-delay 2 -o "$dest" "$url" \
    || error "download failed: $url"
}

cmd_install() {
  mkdir -p "$RUNNER_HOME" "${RUNNER_HOME}/_hooks"
  cd "$RUNNER_HOME"

  fetch "$RUNNER_URL" "${RUNNER_HOME}/${RUNNER_TGZ}"
  verify_digest "${RUNNER_HOME}/${RUNNER_TGZ}" "${RUNNER_SHA256:-}"
  # Extract only when the binary is absent, so a re-run is a no-op rather than
  # clobbering a configured runner's .runner / .credentials files.
  [ -x "${RUNNER_HOME}/run.sh" ] || tar xzf "${RUNNER_HOME}/${RUNNER_TGZ}" -C "$RUNNER_HOME"

  fetch "$HOOKS_URL" "${RUNNER_HOME}/${HOOKS_ZIP}"
  verify_digest "${RUNNER_HOME}/${HOOKS_ZIP}" "${HOOKS_SHA256:-}"
  [ -f "${RUNNER_HOME}/_hooks/index.js" ] || unzip -oq "${RUNNER_HOME}/${HOOKS_ZIP}" -d "${RUNNER_HOME}/_hooks"

  info "installed runner ${RUNNER_VERSION} + container hooks ${HOOKS_VERSION}"
}

cmd_configure() {
  cmd_install
  cd "$RUNNER_HOME"

  local token
  # Read from stdin, never a flag. argv is world-readable via /proc, so a registration
  # token passed as --token leaks to every local user for the life of the process.
  token=$(cat)
  [ -n "$token" ] || error "no registration token on stdin"

  if [ -f "${RUNNER_HOME}/.runner" ]; then
    info "already configured — leaving registration intact (re-run is a no-op)"
    return 0
  fi

  # The token goes in the ENVIRONMENT, not argv. The runner reads any
  # ACTIONS_RUNNER_INPUT_<ARG> variable, masks the ones it knows are secret, and then
  # deletes the variable from its own environment block
  # (Runner.Listener/CommandSettings.cs). argv, by contrast, is world-readable through
  # /proc for the life of the process, so `--token "$token"` would expose a live
  # registration credential to every local user. There is no --token-file option; this
  # env path is the argv-free route.
  #
  # --replace so a rebuilt host reclaims its own name instead of accumulating a second
  # offline entry. --disableupdate so the version stays the DECLARED one: a self-update
  # would silently undo the pin and make two hosts differ.
  ACTIONS_RUNNER_INPUT_TOKEN="$token" ./config.sh \
    --unattended \
    --url "$RUNNER_ORG_URL" \
    --name "$RUNNER_NAME" \
    --labels "$RUNNER_LABELS" \
    --runnergroup "$RUNNER_GROUP" \
    --work "_work" \
    --replace \
    --disableupdate \
    || error "runner configuration failed"

  info "registered $RUNNER_NAME (labels: $RUNNER_LABELS, group: $RUNNER_GROUP)"
}

cmd_remove() {
  cd "$RUNNER_HOME"
  [ -f "${RUNNER_HOME}/.runner" ] || { info "not configured — nothing to remove"; return 0; }
  local token
  token=$(cat)
  [ -n "$token" ] || error "no remove token on stdin"
  ACTIONS_RUNNER_INPUT_TOKEN="$token" ./config.sh remove || error "de-registration failed"
  info "de-registered $RUNNER_NAME (host left intact)"
}

cmd_status() {
  systemctl --user is-active "actions-runner.service" 2>/dev/null || true
  if [ -f "${RUNNER_HOME}/.runner" ]; then
    info "configured"
  else
    warn "NOT configured"
  fi
}

case "${1:-}" in
  install)   cmd_install ;;
  configure) cmd_configure ;;
  remove)    cmd_remove ;;
  start)     systemctl --user enable --now actions-runner.service; info "started" ;;
  stop)      systemctl --user stop actions-runner.service; info "stopped" ;;
  status)    cmd_status ;;
  *)         error "usage: $0 {install|configure|remove|start|stop|status}" ;;
esac
