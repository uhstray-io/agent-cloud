"""resize-vm.yml converges the Proxmox guest-agent option (agent=1), restart-aware.

Runs the playbook's own resolve / restart-decision / config-change tasks against synthetic
Proxmox API responses, so the decision logic is exercised, not just its text. Needs
ansible-playbook.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
PLAYBOOK = ROOT / "platform/playbooks/resize-vm.yml"
TASKS = ("Resolve current values + the disk device to grow",
         "Decide whether the guest needs a restart to pick up its config",
         "Compute the config changes actually needed")

pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")


def decide(tmp_path, *, agent=None, pending=(), running=True, want_agent=True):
    play = yaml.safe_load(PLAYBOOK.read_text())[0]
    tasks = {t["name"]: t for t in play["tasks"] if t.get("name") in TASKS}
    assert set(tasks) == set(TASKS)
    cfg = {"cores": 2, "memory": 4096, "sockets": 1, **({"agent": agent} if agent is not None else {})}
    harness = [{
        "hosts": "localhost", "connection": "local", "gather_facts": False,
        "vars": {"_want_cores": "", "_want_memory": "", "_want_disk_gb": "", "_want_agent": want_agent,
                 "_cfg": {"json": {"data": cfg}},
                 "_state": {"json": {"data": {"status": "running" if running else "stopped",
                                              "maxmem": 4096 * 1048576, "cpus": 2}}},
                 "_pending": {"json": {"data": [dict(p) for p in pending]}}},
        "tasks": [tasks[n] for n in TASKS] + [{"ansible.builtin.debug": {
            "msg": "RESULT={{ {'changes': _cfg_changes, 'restart': _needs_restart | bool} | to_json }}"}}],
    }]
    play_file = tmp_path / "play.yml"
    play_file.write_text(yaml.safe_dump(harness, sort_keys=False))
    out = subprocess.run(["ansible-playbook", "-i", "localhost,", str(play_file)], capture_output=True, text=True,
                         env={"ANSIBLE_NOCOLOR": "1", "PATH": __import__("os").environ["PATH"],
                              "ANSIBLE_LOCAL_TEMP": str(tmp_path)},
                         stdin=subprocess.DEVNULL, timeout=120).stdout
    line = next(ln for ln in out.splitlines() if "RESULT=" in ln)
    return json.loads(line.split("RESULT=", 1)[1].rstrip('"').replace('\\"', '"'))


def test_a_running_vm_without_the_agent_gets_it_and_needs_a_restart(tmp_path):
    assert decide(tmp_path) == {"changes": {"agent": "1"}, "restart": True}


def test_an_agent_change_still_pending_from_an_earlier_run_still_needs_the_restart(tmp_path):
    # The earlier run wrote agent=1 without a reboot; this run must not report "converged".
    result = decide(tmp_path, agent="1", pending=[{"key": "agent", "value": "0", "pending": "1"}])
    assert result == {"changes": {}, "restart": True}


def test_an_enabled_agent_in_property_form_is_left_alone(tmp_path):
    assert decide(tmp_path, agent="enabled=1,fstrim_cloned_disks=1") == {"changes": {}, "restart": False}


def test_the_per_host_opt_out_writes_nothing(tmp_path):
    assert decide(tmp_path, want_agent=False) == {"changes": {}, "restart": False}


def test_a_stopped_vm_gets_the_option_without_a_restart(tmp_path):
    assert decide(tmp_path, running=False) == {"changes": {"agent": "1"}, "restart": False}
