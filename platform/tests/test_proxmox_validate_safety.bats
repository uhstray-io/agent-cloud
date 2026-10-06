#!/usr/bin/env bats

@test "Proxmox validation hides token-bearing requests and keeps its summary visible" {
  python3 - "$BATS_TEST_DIRNAME/../playbooks/proxmox-validate.yml" <<'PY'
import sys
import yaml

play, = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))[1:]  # [0] imports the extra-var guard
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

src, = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))[1:]  # [0] imports the extra-var guard
tasks = [t for t in src["tasks"] if t["name"] == "Derive node capacity"]
assert len(tasks) == 1
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
    {"type": "lxc", "node": "big", "maxcpu": 1, "maxmem": 1 * gib, "status": "running"},
    {"type": "qemu", "node": "big", "status": "unknown"},
    {"type": "storage", "node": "big", "storage": "vm-lvms", "disk": 100 * gib, "maxdisk": 400 * gib},
    {"type": "qemu", "node": "small", "maxcpu": 2, "maxmem": 4 * gib, "status": "running"},
    {"type": "storage", "node": "small", "storage": "vm-lvms", "status": "unknown"},
    {"type": "pool", "pool": "p"},
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
assert [c["node"] for c in cap] == ["big", "small"], cap  # sorted by free memory
big, small = cap
assert big == {"node": "big", "cpu_used_pct": 10.0, "cores": 32, "vcpus_configured": 7,
               "mem_used_gb": 20.0, "mem_total_gb": 128.0, "mem_free_gb": 108.0,
               "mem_configured_gb": 11.0, "guests": 4, "guests_running": 2,
               "vm_storage_free_gb": 300}, big
assert small["cpu_used_pct"] == 50.0 and small["mem_free_gb"] == 4.0, small
assert small["vm_storage_free_gb"] == "n/a", small
PY
}
