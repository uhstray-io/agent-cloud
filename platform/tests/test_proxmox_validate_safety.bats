#!/usr/bin/env bats

@test "Proxmox validation hides token-bearing requests and keeps its summary visible" {
  python3 - "$BATS_TEST_DIRNAME/../playbooks/proxmox-validate.yml" <<'PY'
import sys
import yaml

play, = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
tasks = play["tasks"]
requests = [task for task in tasks if "ansible.builtin.uri" in task]
assert len(requests) == 9
assert all(task.get("no_log") is True for task in requests)
summary, = (task for task in tasks if task["name"] == "Validation Summary")
assert summary.get("no_log") is not True
failure, = (task for task in tasks if task["name"] == "Abort if API unreachable")
assert "_pve_host" in failure["ansible.builtin.fail"]["msg"]
PY
}

@test "Proxmox validation reports node capacity from the cluster API, most free memory first" {
  command -v ansible-playbook >/dev/null || skip "ansible-playbook not installed"
  local play="$BATS_TEST_TMPDIR/cap.yml"
  python3 - "$BATS_TEST_DIRNAME/../playbooks/proxmox-validate.yml" "$play" <<'PY'
import sys
import yaml

src, = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
wanted = ("Derive node capacity", "Report node capacity (most free memory first)")
tasks = [t for t in src["tasks"] if t["name"] in wanted]
assert len(tasks) == 2
gib = 1073741824
nodes = {"json": {"data": [
    {"node": "small", "status": "online", "cpu": 0.5, "maxcpu": 8, "mem": 12 * gib, "maxmem": 16 * gib},
    {"node": "big", "status": "online", "cpu": 0.1, "maxcpu": 32, "mem": 20 * gib, "maxmem": 128 * gib},
    {"node": "down", "status": "offline"},
]}}
res = {"json": {"data": [
    {"type": "qemu", "node": "big", "maxcpu": 4, "maxmem": 8 * gib, "status": "running"},
    {"type": "qemu", "node": "big", "maxcpu": 2, "maxmem": 2 * gib, "status": "stopped"},
    {"type": "qemu", "node": "big", "maxcpu": 2, "maxmem": 2 * gib, "status": "stopped", "template": 1},
    {"type": "storage", "node": "big", "storage": "vm-lvms", "disk": 100 * gib, "maxdisk": 400 * gib},
    {"type": "qemu", "node": "small", "maxcpu": 2, "maxmem": 4 * gib, "status": "running"},
]}}
yaml.safe_dump([{"hosts": "localhost", "gather_facts": False,
                 "vars": {"pve_nodes": nodes, "pve_resources": res, "_vm_storage": "vm-lvms"},
                 "tasks": tasks + [{"name": "dump", "ansible.builtin.copy": {
                     "content": "{{ node_capacity | to_json }}", "dest": sys.argv[2] + ".json", "mode": "0600"}}]}],
               open(sys.argv[2], "w"))
PY
  ansible-playbook -i localhost, -c local "$play" >/dev/null
  python3 - "$play.json" <<'PY'
import json
import sys

cap = json.load(open(sys.argv[1]))
by = {c["node"]: c for c in cap}
assert set(by) == {"big", "small"}, by
big = by["big"]
assert big["vms"] == 2 and big["vms_running"] == 1, big
assert big["vcpus_configured"] == 6 and big["mem_configured_gb"] == 10.0, big
assert big["mem_free_gb"] == 108.0 and big["vm_storage_free_gb"] == 300, big
assert by["small"]["vm_storage_free_gb"] == "n/a", by["small"]
PY
}
