#!/usr/bin/env bash
# console — the Dispatch Console: image build on this host, then container lifecycle only.
#
# Ansible's deploy-console.yml renders .env (non-secret config; the OIDC client secret is
# shared-read from authentik) and config/scope.json BEFORE this runs. This script does NOT
# generate or read secrets. It builds the image from the committed sources when those changed
# (there is no registry), starts the single container, and waits for it to report healthy.
#
# Usage: ./deploy.sh
# Steps (idempotent): verify .env and scope present, build (skipped when the image on this host
# already matches its inputs), up if changed, wait healthy.
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
  info "Step 1: Verifying the templated .env and scope file are present..."
  [ -f "${SCRIPT_DIR}/.env" ] || error "${SCRIPT_DIR}/.env not found. Run Ansible deploy-console.yml first."
  [ -f "${SCRIPT_DIR}/config/scope.json" ] || error "${SCRIPT_DIR}/config/scope.json not found. Run Ansible deploy-console.yml first."
  info "  .env and config/scope.json present."
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
  if [ "${LOCAL_MODE:-}" = "true" ] && [ -f compose.local.yml ]; then
    compose_up_if_changed config/scope.json certs/step-ca-bundle.crt
  else
    compose_up_if_changed config/scope.json
  fi
}

step_wait_healthy() {
  info "Step 4: Waiting for console to become healthy..."
  wait_for_healthy console 300
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
