#!/usr/bin/env bash
# local-netbox-discover.sh — discover the running agent-cloud podman containers
# into the local NetBox as VirtualMachines under a "agent-cloud-local" cluster.
#
# Local-dev container discovery: the prod Diode/orb-agent pipeline does network
# scanning (privileged, excluded locally), so this feeds the live container
# inventory straight into NetBox via the Django ORM inside the netbox container
# — no API token, no Diode pipeline. Idempotent (update_or_create by name).
#
# Usage: scripts/local-netbox-discover.sh    (or: make local-netbox-discover)

set -euo pipefail

NETBOX_CTR="${NETBOX_CONTAINER:-netbox-netbox-1}"
MANAGE="/opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py"

podman container exists "$NETBOX_CTR" \
  || { echo "[discover] NetBox container '$NETBOX_CTR' not running — deploy NetBox first" >&2; exit 1; }

# Normalize `podman ps` into [{name,image,ports,status}] (the agent-cloud fleet).
data=$(podman ps --format json | python3 -c '
import json, sys
out = []
for c in json.load(sys.stdin):
    names = c.get("Names") or c.get("Name") or []
    name = names[0] if isinstance(names, list) and names else (names or "unknown")
    out.append({
        "name": name,
        "image": c.get("Image", ""),
        "ports": str(c.get("Ports") or ""),
        "status": c.get("State") or c.get("Status", ""),
    })
print(json.dumps(out))
')

count=$(printf '%s' "$data" | python3 -c 'import json,sys;print(len(json.load(sys.stdin)))')
echo "[discover] feeding ${count} running container(s) into NetBox..."

# Pipe the inventory to an ORM script inside the container (stdin = the JSON).
printf '%s' "$data" | podman exec -i "$NETBOX_CTR" sh -c "$MANAGE shell -c '
import json, sys
from virtualization.models import Cluster, ClusterType, VirtualMachine
data = json.load(sys.stdin)
ctype, _ = ClusterType.objects.get_or_create(name=\"Podman\", slug=\"podman\")
cluster, _ = Cluster.objects.get_or_create(name=\"agent-cloud-local\", defaults={\"type\": ctype})
created = updated = 0
for c in data:
    vm, was_created = VirtualMachine.objects.update_or_create(
        name=c[\"name\"], cluster=cluster,
        defaults={\"status\": \"active\", \"comments\": \"image: %s\\nports: %s\\nstatus: %s\" % (c[\"image\"], c[\"ports\"], c[\"status\"])},
    )
    created += was_created
    updated += (not was_created)
    print((\"+\" if was_created else \"=\"), vm.name)
print(\"RESULT created=%d updated=%d total=%d cluster=%s\" % (created, updated, len(data), cluster.name))
'"
