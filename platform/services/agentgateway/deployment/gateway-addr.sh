#!/usr/bin/env bash
# gateway-addr.sh — print agentgateway's address on the compose network it shares
# with agentgateway-db, for probes that run inside the db container.
#
# Why an address, not the name: podman 4.x seeds a container's /etc/hosts from the
# host's, and Ubuntu maps the host's own name to 127.0.1.1. On the production VM,
# whose hostname IS `agentgateway`, the db container resolved `agentgateway` to
# itself and every readiness probe was refused (Semaphore task 1215), while the
# gateway was healthy. /etc/hosts wins over the network's DNS, so no name is safe
# against a VM named after its service.
#
# Usage: CONTAINER_ENGINE=podman ./gateway-addr.sh   (prints one address, e.g. 192.0.2.7)
set -euo pipefail
engine="${CONTAINER_ENGINE:-podman}"
net=$($engine inspect agentgateway-db --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' | awk '{print $1}')
[ -n "$net" ] || { echo "gateway-addr: agentgateway-db is on no network" >&2; exit 1; }
addr=$($engine inspect agentgateway --format "{{(index .NetworkSettings.Networks \"${net}\").IPAddress}}")
[ -n "$addr" ] || { echo "gateway-addr: agentgateway has no address on ${net}" >&2; exit 1; }
printf '%s\n' "$addr"
