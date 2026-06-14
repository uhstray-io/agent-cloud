#!/usr/bin/env bash
# ok()/no() always return 0, so the `cond && ok || no` idiom never mis-fires.
# shellcheck disable=SC2015
# local-smoke.sh — repeatable smoke test for the local-dev deployment.
#
# Validates the live local stack end-to-end from the Mac: control plane, DNS
# (authoritative + forward), and Caddy (TLS reverse-proxy). Read-only — runs
# every check, tallies, and exits non-zero if any hard check failed. Deployed-
# but-absent services are SKIPped, not failed, so this is safe to run at any
# stage. `--full` also runs the static suite (lint + BATS).
#
# Usage: scripts/local-smoke.sh [--full]    (or: make local-smoke)

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE="${HOME}/.agent-cloud-local/credentials.env"
INV="${REPO_ROOT}/platform/inventory/local-dev.yml"
FULL=false
[ "${1:-}" = "--full" ] && FULL=true

# Read the dev zone (dns_zone) from the working inventory so these checks track
# the configured zone instead of hardcoding it. Falls back to the committed
# default if the working inventory or ansible-inventory is absent.
ZONE="$(ansible-inventory -i "$INV" --host dns-local 2>/dev/null \
        | python3 -c 'import json,sys; print(json.load(sys.stdin).get("dns_zone",""))' 2>/dev/null)"
[ -n "$ZONE" ] || ZONE="agent-cloud.test"

pass=0 fail=0 skip=0
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$1"; pass=$((pass+1)); }
no()   { printf '  \033[31mFAIL\033[0m %s\n' "$1"; fail=$((fail+1)); }
sk()   { printf '  \033[33mSKIP\033[0m %s\n' "$1"; skip=$((skip+1)); }
hdr()  { printf '\n\033[1m%s\033[0m\n' "$1"; }

running() { podman container exists "$1" 2>/dev/null && \
            [ "$(podman inspect -f '{{.State.Status}}' "$1" 2>/dev/null)" = running ]; }

http_is() { # url expected_code label
  local code; code=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 5 "$1" 2>/dev/null)
  [ "$code" = "$2" ] && ok "$3 ($code)" || no "$3 (got ${code:-none}, want $2)"
}

hdr "1. Engine + state"
if podman machine inspect --format '{{.State}}' 2>/dev/null | grep -q running; then
  ok "podman machine running"; else no "podman machine running"; fi
[ -f "$STATE" ] && ok "state file present ($STATE)" || no "state file present"

hdr "2. Control plane"
for c in local-openbao local-semaphore; do
  running "$c" && ok "container $c running" || no "container $c running"
done
http_is "http://127.0.0.1:8200/v1/sys/health" "200" "OpenBao health (127.0.0.1:8200)"
http_is "http://127.0.0.1:3000/api/ping"      "200" "Semaphore ping (127.0.0.1:3000)"

hdr "3. DNS (hickory)"
if running dns; then
  ok "container dns running"
  wild=$(dig +short +time=2 +tries=1 -p 5300 @127.0.0.1 "probe.${ZONE}" A 2>/dev/null)
  [ "$wild" = "127.0.0.1" ] && ok "wildcard *.${ZONE} -> 127.0.0.1" || no "wildcard (got '${wild:-none}')"
  fwd=$(dig +short +time=3 +tries=2 -p 5300 @127.0.0.1 one.one.one.one A 2>/dev/null)
  [ -n "$fwd" ] && ok "forward one.one.one.one -> $fwd" || no "forward upstream (no answer)"
else sk "dns not deployed (make local-deploy-dns)"; fi

hdr "4. Caddy (TLS reverse-proxy)"
if running caddy; then
  ok "container caddy running"
  # Use --resolve to simulate /etc/resolver (proves the name -> Caddy TLS -> app chain).
  for pair in "semaphore.${ZONE}:/api/ping" "openbao.${ZONE}:/v1/sys/health"; do
    host="${pair%%:*}"; path="${pair#*:}"
    code=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 5 \
           --resolve "${host}:8443:127.0.0.1" "https://${host}:8443${path}" 2>/dev/null)
    [ "$code" = "200" ] && ok "https://${host}:8443 -> 200" || no "https://${host}:8443 (got ${code:-none})"
  done
else sk "caddy not deployed (make local-deploy-caddy)"; fi

hdr "5. NetBox (app tier under podman)"
if running netbox-netbox-1; then
  ok "container netbox-netbox-1 running"
  http_is "http://127.0.0.1:8000/login/" "200" "NetBox UI (127.0.0.1:8000)"
  vms=$(podman exec netbox-netbox-1 /opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py shell -c \
        "from virtualization.models import VirtualMachine; print(VirtualMachine.objects.filter(cluster__name='agent-cloud-local').count())" 2>/dev/null \
        | grep -oE '^[0-9]+$' | tail -1)
  [ -n "${vms:-}" ] && [ "$vms" -gt 0 ] 2>/dev/null \
    && ok "container discovery: ${vms} VM(s) in agent-cloud-local cluster" \
    || no "container discovery (no VMs — run make local-netbox-discover)"
else sk "netbox not deployed (make local-netbox)"; fi

hdr "6. /etc/resolver (native macOS resolution)"
if [ -f "/etc/resolver/${ZONE}" ]; then
  ok "/etc/resolver/${ZONE} present"
else sk "/etc/resolver/${ZONE} not set (make local-dns-resolver) — native name resolution off"; fi

if [ "$FULL" = true ]; then
  hdr "7. Static suite (--full)"
  # -S warning matches the repo's CI severity (info-level notes don't gate).
  ( cd "$REPO_ROOT" && shellcheck -S warning scripts/*.sh platform/lib/*.sh platform/services/*/deployment/deploy.sh >/dev/null 2>&1 ) \
    && ok "shellcheck (-S warning)" || no "shellcheck"
  ( cd "$REPO_ROOT" && bats platform/tests/test_common.bats platform/tests/test_service_dns.bats platform/tests/test_service_caddy.bats >/dev/null 2>&1 ) \
    && ok "BATS (common/dns/caddy)" || no "BATS"
fi

hdr "Result: ${pass} passed, ${fail} failed, ${skip} skipped"
[ "$fail" -eq 0 ]
