#!/usr/bin/env bash
# o11y-render-proof.sh — prove Grafana renders every dashboard and alert rule from the
# committed provisioning alone, on a FRESH, EMPTY data volume.
#
# Author: Joseph A. Wisneski IV <stray@uhstray.io>
#
# Change inference-telemetry-production, task 3.5, scenario "Dashboards render from
# provisioning alone". The operator decision (2026-10-04) is to prove this on throwaway
# state, never by wiping the production receiver. This script:
#
#   1. renders templates/alerts.yml.j2 with the DGX and probe rule groups switched on
#      (paused: no contact point, so nothing can notify), through Ansible's own template
#      engine — the same renderer deploy-o11y.yml uses;
#   2. starts ONE throwaway Grafana container (unique name, anonymous volume, no
#      published port, no shared network) with the committed datasources, dashboard
#      provider and dashboard JSON, plus the rendered alert file;
#   3. reads back over Grafana's API that every committed dashboard uid is present and
#      every rendered alert-rule uid is provisioned;
#   4. removes the container and its anonymous volume on every exit path.
#
# Nothing here touches the running o11y stack: container and volume names are unique
# per run, and no port or network is shared. The admin password is random per run and
# lives only in this process's environment and the throwaway container.
#
# Usage: scripts/o11y-render-proof.sh        (or: make local-o11y-render-proof)
# Env:   CONTAINER_ENGINE (default podman), O11Y_GRAFANA_IMAGE (default: compose.yml's pin)

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY="$ROOT/platform/services/o11y/deployment"
ENGINE="${CONTAINER_ENGINE:-podman}"
# The pin compose.yml declares, read from the file so the two cannot drift.
DEFAULT_IMAGE="$(sed -nE 's/^ *image: \$\{O11Y_GRAFANA_IMAGE:-([^}]+)\}$/\1/p' "$DEPLOY/compose.yml")"
IMAGE="${O11Y_GRAFANA_IMAGE:-$DEFAULT_IMAGE}"
[[ -n "$IMAGE" ]] || { echo "FAIL: could not read the Grafana image pin from compose.yml" >&2; exit 1; }

RUN_ID="$(od -An -N6 -tx1 /dev/urandom | tr -d ' \n')"
NAME="o11y-render-proof-$RUN_ID"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/o11y-render-proof.XXXXXX")"
ADMIN_PW="$(od -An -N24 -tx1 /dev/urandom | tr -d ' \n')"

cleanup() {
  # -v removes the anonymous data volume with the container: nothing persists.
  "$ENGINE" rm -f -v "$NAME" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

# 1. Provisioning tree: committed datasources + dashboard provider, rendered alerts.
cp -R "$DEPLOY/config/grafana/provisioning" "$WORK/provisioning"
mkdir -p "$WORK/provisioning/alerting"
ANSIBLE_DEPRECATION_WARNINGS=False ANSIBLE_LOCALHOST_WARNING=False \
  ansible localhost -c local \
  -m ansible.builtin.template \
  -a "src=$DEPLOY/templates/alerts.yml.j2 dest=$WORK/provisioning/alerting/observability.yml" \
  -e '{"dgx_spark_scrape_enabled": true, "o11y_inference_probe_enabled": true, "o11y_alerts_enabled": false}' \
  </dev/null >"$WORK/render.log" 2>&1 || { cat "$WORK/render.log" >&2; exit 1; }

EXPECTED_DASH="$(python3 -c '
import glob, json, sys
for p in sorted(glob.glob(sys.argv[1] + "/*.json")):
    print(json.load(open(p))["uid"])
' "$DEPLOY/config/grafana/dashboards")"
EXPECTED_RULES="$(python3 -c '
import sys, yaml
doc = yaml.safe_load(open(sys.argv[1]))
for g in doc.get("groups", []):
    for r in g.get("rules", []):
        print(r["uid"])
' "$WORK/provisioning/alerting/observability.yml" | sort)"

# 2. Throwaway Grafana on an empty anonymous volume.
"$ENGINE" run -d --name "$NAME" \
  -e "GF_SECURITY_ADMIN_PASSWORD=$ADMIN_PW" \
  -e GF_ANALYTICS_REPORTING_ENABLED=false \
  -e GF_ANALYTICS_CHECK_FOR_UPDATES=false \
  -v "$WORK/provisioning:/etc/grafana/provisioning:ro" \
  -v "$DEPLOY/config/grafana/dashboards:/var/lib/grafana/dashboards:ro" \
  -v /var/lib/grafana \
  "$IMAGE" >/dev/null

api() {
  "$ENGINE" exec "$NAME" sh -c \
    'printf "user = \"admin:%s\"\n" "$GF_SECURITY_ADMIN_PASSWORD" | curl -fsS --config - "http://127.0.0.1:3000$1"' \
    _ "$1"
}

for _ in $(seq 1 60); do
  api /api/health >/dev/null 2>&1 && break
  sleep 2
done
api /api/health >/dev/null || { echo "FAIL: throwaway Grafana never became healthy" >&2; exit 1; }

# 3. Readback. Dashboard and rule provisioning finish shortly after /api/health.
got_dash="" got_rules=""
for _ in $(seq 1 30); do
  got_dash="$(api '/api/search?type=dash-db&limit=5000' | python3 -c 'import json,sys; print("\n".join(sorted(d["uid"] for d in json.load(sys.stdin))))')"
  got_rules="$(api /api/v1/provisioning/alert-rules | python3 -c 'import json,sys; print("\n".join(sorted(r["uid"] for r in json.load(sys.stdin))))')"
  [[ "$got_dash" == "$(sort <<<"$EXPECTED_DASH")" && "$got_rules" == "$EXPECTED_RULES" ]] && break
  sleep 2
done

status=0
if [[ "$got_dash" != "$(sort <<<"$EXPECTED_DASH")" ]]; then
  echo "FAIL: dashboards differ from config/grafana/dashboards" >&2
  diff <(sort <<<"$EXPECTED_DASH") <(printf '%s\n' "$got_dash") >&2 || true
  status=1
fi
if [[ "$got_rules" != "$EXPECTED_RULES" ]]; then
  echo "FAIL: alert rules differ from the rendered alerts.yml.j2" >&2
  diff <(printf '%s\n' "$EXPECTED_RULES") <(printf '%s\n' "$got_rules") >&2 || true
  status=1
fi
[[ $status -eq 0 ]] || exit 1

echo "PASS: fresh volume, $(wc -l <<<"$EXPECTED_DASH" | tr -d ' ') dashboards and $(wc -l <<<"$EXPECTED_RULES" | tr -d ' ') alert rules rendered from provisioning alone ($IMAGE)"
