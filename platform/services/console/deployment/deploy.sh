#!/usr/bin/env bash
# console — the Dispatch Console: image build on this host, then container lifecycle only.
#
# Ansible's deploy-console.yml renders .env (config; the OIDC client secret is shared-read from
# authentik; the Semaphore API token is read from OpenBao), config/scope.json and config/workflow.json
# BEFORE this runs. This script does NOT
# generate or read secrets. It builds the image from the committed sources when those changed
# (there is no registry), starts the single container, and waits until its exec readiness probe passes.
#
# Usage: ./deploy.sh
# Steps (idempotent): verify .env and scope present, build (skipped when the image on this host
# already matches its inputs), up if changed, wait for the exec probe to pass.
#
# Environment (set by deploy-console.yml, never by hand):
#   CONSOLE_FORCE_REBUILD=true   rebuild even when the image already matches its inputs
#   CONSOLE_BUILD_SECCOMP=<path> seccomp profile for the build, a path ON THE ENGINE (local podman)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$(dirname "$(dirname "$(dirname "$SCRIPT_DIR")")")/lib"
cd "${SCRIPT_DIR}"

# shellcheck source=/dev/null
source "${LIB_DIR}/common.sh"

# Label the built image carries: the digest of everything the build reads. It is how the next
# deploy knows whether the image on this host was built from the inputs it holds now.
CONSOLE_BUILD_LABEL="io.agent-cloud.console.build-inputs-sha256"
CONSOLE_IMAGE_DEFAULT="localhost/agent-cloud-console:local"

# console_env_value KEY: the value of KEY in .env, read as text. The file is never sourced, so
# no other value in it (the OIDC client secret) lands in this shell's environment.
console_env_value() {
  local line
  line=$(grep -E "^${1}=" .env | tail -n 1) || line=""
  printf '%s' "${line#*=}"
}

# console_build_inputs_digest: one sha256 over every file the image is built from, by path: the
# Containerfile and every file under the app crate except its target/ directory (Cargo build
# output, which the build regenerates). Only digests are combined; no content is printed. A file
# that cannot be read is an error, never a skip: a digest over whichever files were readable is a
# guess, not a comparison.
console_build_inputs_digest() {
  local f d listing acc=""
  listing=$({ find ../app \( -path ../app/target -prune \) -o -type f -print \
                && echo Containerfile; } | LC_ALL=C sort) \
    || error "Cannot list the console build inputs; refusing to guess whether the image is current."
  while IFS= read -r f; do
    d=$(_compose_sha256 < "$f") && [ -n "$d" ] \
      || error "Cannot read ${f} to hash the build inputs; refusing to guess whether the image is current."
    acc+="${d}  ${f}"$'\n'
  done <<< "$listing"
  printf '%s' "$acc" | _compose_sha256
}

# build_console_image
# Builds CONSOLE_IMAGE from the committed sources, unless the image already on this host was built
# from the same inputs (console_build_inputs_digest) and CONSOLE_FORCE_REBUILD is not the literal
# `true`. The build keeps the layer cache: every input is in the digest, so a cached layer is only
# reused for identical inputs, and a Rust build from scratch takes minutes.
build_console_image() {
  local want have sec=()
  want=$(console_build_inputs_digest)
  have=$($CONTAINER_ENGINE image inspect --format "{{ index .Config.Labels \"${CONSOLE_BUILD_LABEL}\" }}" \
    "$CONSOLE_IMAGE" 2>/dev/null) || have=""
  if [ "${CONSOLE_FORCE_REBUILD:-}" != "true" ] && [ -n "$have" ] && [ "$have" = "$want" ]; then
    info "Console image ${CONSOLE_IMAGE} already matches its build inputs; skipping the build (CONSOLE_FORCE_REBUILD=true rebuilds)."
    return 0
  fi
  # CONSOLE_BUILD_SECCOMP: the local Semaphore's podman client sends its own default seccomp path
  # to the podman-machine engine, which has the profile only at /usr/share/containers/seccomp.json.
  # Without it the build's RUN steps fail "opening seccomp profile failed" (the NetBox build hit
  # this 2026-09-22; platform/services/netbox/deployment/lib/common.sh). Production leaves it unset.
  if [ -n "${CONSOLE_BUILD_SECCOMP:-}" ]; then
    sec=(--security-opt "seccomp=${CONSOLE_BUILD_SECCOMP}")
  fi
  info "Building console image ${CONSOLE_IMAGE} from platform/services/console..."
  # Context is the console service directory (the parent of deployment/), so the Containerfile
  # can reach app/. The Containerfile names what it copies; nothing else in the context is baked in.
  $CONTAINER_ENGINE build ${sec[@]+"${sec[@]}"} \
    --label "${CONSOLE_BUILD_LABEL}=${want}" \
    -t "$CONSOLE_IMAGE" \
    -f Containerfile \
    ..
}

step_verify_inputs() {
  info "Step 1: Verifying the templated .env, scope and workflow files are present..."
  [ -f "${SCRIPT_DIR}/.env" ] || error "${SCRIPT_DIR}/.env not found. Run Ansible deploy-console.yml first."
  [ -f "${SCRIPT_DIR}/config/scope.json" ] || error "${SCRIPT_DIR}/config/scope.json not found. Run Ansible deploy-console.yml first."
  [ -f "${SCRIPT_DIR}/config/workflow.json" ] || error "${SCRIPT_DIR}/config/workflow.json not found. Run Ansible deploy-console.yml first."
  [ -f "${SCRIPT_DIR}/config/flows.json" ] || error "${SCRIPT_DIR}/config/flows.json not found. Run Ansible deploy-console.yml first."
  [ -f "${SCRIPT_DIR}/config/proxmox-ca.pem" ] || error "${SCRIPT_DIR}/config/proxmox-ca.pem not found. Run Ansible deploy-console.yml first."
  info "  .env, config/scope.json, config/workflow.json, config/flows.json and config/proxmox-ca.pem present."
}

step_build() {
  info "Step 2: Building the image on this host (skipped when it already matches its inputs)..."
  build_console_image
}

step_start() {
  info "Step 3: Starting console..."
  # console reads its runtime config from `env_file: .env`, and scope.json is a bind mount. Neither
  # is a compose-spec change, so plain `up -d` leaves a running container with stale input.
  # compose_up_if_changed hashes .env, the compose files and the inputs named here, and recreates
  # only when that digest or the image ID changed. A rebuilt image has a new ID, so it is picked
  # up; an unchanged re-run recreates nothing and prints DEPLOY_CHANGED=true|false.
  # config/flows.json and config/proxmox-ca.pem are bind-mounted too, so a change to either recreates.
  if [ "${LOCAL_MODE:-}" = "true" ] && [ -f compose.local.yml ]; then
    compose_up_if_changed config/scope.json config/workflow.json config/flows.json config/proxmox-ca.pem certs/step-ca-bundle.crt
  else
    compose_up_if_changed config/scope.json config/workflow.json config/flows.json config/proxmox-ca.pem
  fi
}

# _console_probe_once: one readiness probe, run inside the container with `podman exec`. The probe
# is the binary's own subcommand, the same one deploy-console.yml Phase 3 runs.
#
# Each probe is bounded on the host. A `podman exec` can stall while the container is up, and an
# unbounded call would then hold step_wait_healthy past its deadline (CodeRabbit on PR #763). The
# bound is coreutils `timeout`, 15 s. It is absent on some hosts (macOS without coreutils), so
# there the exec runs unbounded (step_wait_healthy warns once). The targets are Linux, where
# `timeout` is normally present. `-k 5` sends KILL 5 s after TERM, in case the exec ignores TERM.
_console_probe_once() {
  if command -v timeout >/dev/null 2>&1; then
    timeout -k 5 15 "$CONTAINER_ENGINE" exec console /usr/local/app/server probe /healthz 200 >/dev/null 2>&1
  else
    "$CONTAINER_ENGINE" exec console /usr/local/app/server probe /healthz 200 >/dev/null 2>&1
  fi
}

# step_wait_healthy: readiness by exec probe, not by the engine's health status. The compose file
# declares no healthcheck (see the comment there: podman-compose 1.0.6 runs it through /bin/sh, and
# the image has none), so there is no status to poll. Bounded by a wall-clock deadline on bash's
# SECONDS, not by a sum of sleeps: a stalled probe spends real time, and that time counts toward
# the deadline. Sleep 5 s between attempts. On timeout: the container's diagnostics, then a failure.
step_wait_healthy() {
  info "Step 4: Waiting for console to become healthy..."
  command -v timeout >/dev/null 2>&1 || warn "coreutils timeout not found: each readiness probe runs unbounded"
  local timeout=300 deadline
  deadline=$((SECONDS + timeout))
  while [ "$SECONDS" -lt "$deadline" ]; do
    if _console_probe_once; then
      info "console is ready."
      return 0
    fi
    sleep 5
  done
  dump_container_diagnostics console
  error "console did not become ready within ${timeout}s"
}

main() {
  info "=== console deployment (host image build + container lifecycle) ==="
  detect_runtime
  info "Container engine: ${CONTAINER_ENGINE}"
  step_verify_inputs
  # Shell environment wins over .env, as compose's own interpolation does.
  CONSOLE_IMAGE="${CONSOLE_IMAGE:-$(console_env_value CONSOLE_IMAGE)}"
  CONSOLE_IMAGE="${CONSOLE_IMAGE:-${CONSOLE_IMAGE_DEFAULT}}"
  step_build
  step_start
  step_wait_healthy
  info "=== console container lifecycle complete ==="
}

main "$@"
