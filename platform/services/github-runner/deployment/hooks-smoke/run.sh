#!/usr/bin/env bash
# run.sh: local smoke run of the GitHub runner's container hooks against rootless podman,
# on a workstation's podman. Touches no runner host, no registry credential and no
# secret. Pulls the hooks release from GitHub and two public images.
#
# It answers the design's runtime question (openspec add-github-actions-runners, D4 and
# tasks 1.1 to 1.2) with the real hooks: whether a `container:` job starts when the hooks'
# `docker` is Docker's CLI client against the runner account's rootless podman API
# socket, and that it does not start through a podman-docker style shim. What it does not
# prove is the live host: its user manager, firewall and unit. That is the deploy's own
# probe and the runbook's job (service CLAUDE.md, "Container jobs under rootless podman").
#
# Usage:
#   platform/services/github-runner/deployment/hooks-smoke/run.sh
# Optional environment:
#   HOOKS_VERSION        runner-container-hooks release (default 0.8.1, the inventory pin)
#   DOCKER_CLI_VERSION   docker-ce-cli version (default 29.9.1, install-podman.yml's pin)
#   HOOKS_SMOKE_IMAGE    the job's `container:` image (default docker.io/library/ubuntu:22.04)
#
# Needs podman able to run a --privileged container (podman inside it creates user
# namespaces), python3, and curl. Exit status is the harness verdict (hooks-cycle.sh).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOKS_VERSION="${HOOKS_VERSION:-0.8.1}"
DOCKER_CLI_VERSION="${DOCKER_CLI_VERSION:-29.9.1}"
TAG="localhost/github-runner-hooks-smoke:${HOOKS_VERSION}-${DOCKER_CLI_VERSION}"

ctx=$(mktemp -d)
trap 'rm -rf "$ctx"' EXIT
cp "$HERE/Containerfile" "$HERE/hooks-cycle.sh" "$ctx/"

# The archive and its digest, both from the release, as deploy-github-runner.yml does: a
# digest written into this repository would go stale the first time the pin moves.
asset="actions-runner-hooks-docker-${HOOKS_VERSION}.zip"
curl -fsSL -H 'Accept: application/vnd.github+json' \
  "https://api.github.com/repos/actions/runner-container-hooks/releases/tags/v${HOOKS_VERSION}" \
  -o "$ctx/release.json"
want=$(python3 - "$ctx/release.json" "$asset" <<'PY'
import json, sys
rel = json.load(open(sys.argv[1]))
for a in rel.get("assets", []):
    if a.get("name") == sys.argv[2]:
        print((a.get("digest") or "").removeprefix("sha256:"))
        break
PY
)
if [ -z "$want" ]; then
  echo "No published digest for $asset; refusing to run unverified hooks." >&2
  exit 1
fi
curl -fsSL "https://github.com/actions/runner-container-hooks/releases/download/v${HOOKS_VERSION}/${asset}" \
  -o "$ctx/actions-runner-hooks-docker.zip"
got=$(python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' \
  "$ctx/actions-runner-hooks-docker.zip")
if [ "$got" != "$want" ]; then
  echo "Digest mismatch for $asset: got $got, release says $want." >&2
  exit 1
fi

podman build --quiet --build-arg "DOCKER_CLI_VERSION=${DOCKER_CLI_VERSION}" -t "$TAG" "$ctx" >/dev/null
podman run --rm --privileged \
  -e "HOOKS_SMOKE_IMAGE=${HOOKS_SMOKE_IMAGE:-docker.io/library/ubuntu:22.04}" \
  "$TAG"
