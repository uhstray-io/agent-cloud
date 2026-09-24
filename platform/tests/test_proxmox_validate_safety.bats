#!/usr/bin/env bats

@test "Proxmox validation hides token-bearing requests and keeps its summary visible" {
  python3 - "$BATS_TEST_DIRNAME/../playbooks/proxmox-validate.yml" <<'PY'
import sys
import yaml

play, = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
tasks = play["tasks"]
requests = [task for task in tasks if "ansible.builtin.uri" in task]
assert len(requests) == 8
assert all(task.get("no_log") is True for task in requests)
summary, = (task for task in tasks if task["name"] == "Validation Summary")
assert summary.get("no_log") is not True
failure, = (task for task in tasks if task["name"] == "Abort if API unreachable")
assert "_pve_host" in failure["ansible.builtin.fail"]["msg"]
PY
}
