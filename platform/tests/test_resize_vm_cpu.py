"""resize-vm.yml and provision-vm.yml converge the Proxmox CPU model (vm_cpu), restart-aware.

The decision tests run the playbook's own resolve / restart-decision / config-change tasks against
synthetic Proxmox API responses (the same harness as test_resize_vm_agent.py), so the logic is
exercised, not just its text. The format tests run the playbooks' own bare-name assert, and the
wiring tests pin where the shared node-support task sits in each playbook. Needs ansible-playbook;
the run tests skip without it.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import playbook_yaml
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
RESIZE = ROOT / "platform/playbooks/resize-vm.yml"
PROVISION = ROOT / "platform/playbooks/provision-vm.yml"
NODE_INCLUDE = "tasks/assert-node-cpu-model.yml"
NODE_CHECK = "Refuse a named CPU model the node cannot provide"
FORMAT_CHECK = "Validate the declared CPU model"
DECISION_TASKS = ("Resolve current values + the disk device to grow",
                  "Decide whether the guest needs a restart to pick up its config",
                  "Compute the config changes actually needed")

pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")


def run_play(tmp_path, play_vars, tasks):
    """Run tasks as one localhost play. Returns (returncode, stdout+stderr)."""
    play_file = tmp_path / "play.yml"
    play_file.write_text(yaml.safe_dump([{"hosts": "localhost", "connection": "local",
                                          "gather_facts": False, "vars": play_vars, "tasks": tasks}],
                                        sort_keys=False))
    proc = subprocess.run(["ansible-playbook", "-i", "localhost,", str(play_file)], capture_output=True,
                          text=True, env={"ANSIBLE_NOCOLOR": "1", "PATH": os.environ["PATH"],
                                          "ANSIBLE_LOCAL_TEMP": str(tmp_path)},
                          stdin=subprocess.DEVNULL, timeout=120)
    return proc.returncode, proc.stdout + proc.stderr


def find_task(path, name):
    for play in playbook_yaml.plays(path):
        for task in playbook_yaml.tasks(play.get("tasks", [])):
            if task.get("name") == name:
                return task
    raise AssertionError(f"task {name!r} not found in {path.name}")


def task_names(path):
    return [task.get("name") for play in playbook_yaml.plays(path)
            for task in playbook_yaml.tasks(play.get("tasks", []))]


def decide_cpu(tmp_path, *, want="x86-64-v3", cfg_cpu=None, pending=(), running=True):
    play = next(candidate for candidate in playbook_yaml.plays(RESIZE)
                if set(DECISION_TASKS) <= {task.get("name") for task in
                                           playbook_yaml.tasks(candidate.get("tasks", []))})
    tasks = {t["name"]: t for t in playbook_yaml.tasks(play["tasks"]) if t.get("name") in DECISION_TASKS}
    assert set(tasks) == set(DECISION_TASKS)
    cfg = {"cores": 2, "memory": 4096, "sockets": 1, **({"cpu": cfg_cpu} if cfg_cpu is not None else {})}
    play_vars = {
        "_want_cores": "", "_want_memory": "", "_want_disk_gb": "", "_want_agent": False,
        "_want_cpu": want,
        "_cfg": {"json": {"data": cfg}},
        "_state": {"json": {"data": {"status": "running" if running else "stopped",
                                     "maxmem": 4096 * 1048576, "cpus": 2}}},
        "_pending": {"json": {"data": [dict(p) for p in pending]}},
        # The playbook's own lazy definitions, not copies.
        **{k: play["vars"][k] for k in ("_agent_items", "_agent_awaits_restart",
                                        "_cpu_items", "_cpu_awaits_restart")},
    }
    tasks_run = [tasks[n] for n in DECISION_TASKS] + [{"ansible.builtin.debug": {
        "msg": "RESULT={{ {'changes': _cfg_changes, 'restart': _needs_restart | bool} | to_json }}"}}]
    rc, out = run_play(tmp_path, play_vars, tasks_run)
    assert rc == 0, out
    line = next(ln for ln in out.splitlines() if "RESULT=" in ln)
    return json.loads(line.split("RESULT=", 1)[1].rstrip('"').replace('\\"', '"'))


def test_an_unmanaged_cpu_is_left_alone(tmp_path):
    assert decide_cpu(tmp_path, want="") == {"changes": {}, "restart": False}


def test_a_running_vm_on_the_default_cpu_gets_the_model_and_a_restart(tmp_path):
    assert decide_cpu(tmp_path) == {"changes": {"cpu": "x86-64-v3"}, "restart": True}


def test_a_model_already_applied_is_left_alone(tmp_path):
    assert decide_cpu(tmp_path, cfg_cpu="x86-64-v3") == {"changes": {}, "restart": False}


def test_a_model_written_by_an_earlier_run_and_still_pending_needs_the_restart(tmp_path):
    # GET /config reports the pending value by default, so the config already names the model;
    # only /pending shows that it has not reached the running guest.
    pending = [{"key": "cpu", "value": "qemu64", "pending": "x86-64-v3"}]
    assert decide_cpu(tmp_path, cfg_cpu="x86-64-v3", pending=pending) == {"changes": {}, "restart": True}


def test_a_pending_change_to_another_key_is_not_a_cpu_change(tmp_path):
    pending = [{"key": "agent", "value": "0", "pending": "1"}]
    assert decide_cpu(tmp_path, cfg_cpu="x86-64-v3", pending=pending) == {"changes": {}, "restart": False}


def test_key_value_items_survive_a_model_change(tmp_path):
    result = decide_cpu(tmp_path, cfg_cpu="x86-64-v2-AES,flags=+aes;-md-clear")
    assert result == {"changes": {"cpu": "x86-64-v3,flags=+aes;-md-clear"}, "restart": True}


def test_a_cputype_key_is_replaced_by_the_bare_model(tmp_path):
    result = decide_cpu(tmp_path, cfg_cpu="cputype=host,flags=+aes")
    assert result == {"changes": {"cpu": "x86-64-v3,flags=+aes"}, "restart": True}


def test_a_cputype_key_already_at_the_model_is_left_alone(tmp_path):
    assert decide_cpu(tmp_path, cfg_cpu="cputype=x86-64-v3,flags=+aes") == {"changes": {}, "restart": False}


def test_a_stopped_vm_gets_the_model_without_a_restart(tmp_path):
    assert decide_cpu(tmp_path, running=False) == {"changes": {"cpu": "x86-64-v3"}, "restart": False}


@pytest.mark.parametrize("path, var", [(RESIZE, "_want_cpu"), (PROVISION, "_cpu")])
@pytest.mark.parametrize("value", ["x86-64-v3", "x86-64-v2-AES", "host", "custom-lab_1.0", ""])
def test_a_bare_model_name_passes_the_format_check(tmp_path, path, var, value):
    rc, out = run_play(tmp_path, {var: value}, [find_task(path, FORMAT_CHECK)])
    assert rc == 0, out


@pytest.mark.parametrize("path, var", [(RESIZE, "_want_cpu"), (PROVISION, "_cpu")])
@pytest.mark.parametrize("value", ["x86-64-v3,flags=+aes", "host,hidden=1", "x86=3", "x86 64",
                                   "-x86", "x86-64-v3;x"])
def test_a_value_that_could_carry_extra_property_keys_is_refused_before_any_write(tmp_path, path, var, value):
    rc, out = run_play(tmp_path, {var: value}, [find_task(path, FORMAT_CHECK)])
    assert rc != 0 and "Nothing was written" in out, out


def test_resize_checks_the_node_before_the_config_write_only_when_the_model_is_written_or_pending():
    include = find_task(RESIZE, NODE_CHECK)
    assert include["ansible.builtin.include_tasks"] == NODE_INCLUDE
    assert include["vars"] == {"_ncm_node": "{{ _node }}", "_ncm_want_cpu": "{{ _want_cpu }}"}
    assert include["when"] == "_cpu_awaits_restart | bool"
    names = task_names(RESIZE)
    assert names.index(NODE_CHECK) < names.index("Apply cores/memory/agent to the VM config")


def test_resize_reads_the_hoisted_inventory_model_and_the_vm_specs_fallback():
    text = RESIZE.read_text()
    assert '_hv_vm_cpu: "{{ _hv.vm_cpu | default(omit) }}"' in text
    assert "_want_cpu: \"{{ (_hv_vm_cpu | default(_svc.cpu | default(''))) | string | trim }}\"" in text


def test_provision_checks_the_declared_node_before_the_configure_put():
    include = find_task(PROVISION, NODE_CHECK)
    assert include["ansible.builtin.include_tasks"] == NODE_INCLUDE
    assert include["vars"] == {"_ncm_node": "{{ _node }}", "_ncm_want_cpu": "{{ _cpu }}"}
    assert include["when"] == "_cpu | length > 0"
    names = task_names(PROVISION)
    assert names.index(NODE_CHECK) < names.index("Configure VM resources and cloud-init")


def test_provision_puts_the_model_in_the_configure_body_and_omits_it_when_unset():
    # Scoped to the one task: the bats slice of the same block stops at its first status_code.
    body = find_task(PROVISION, "Configure VM resources and cloud-init")["ansible.builtin.uri"]["body"]
    assert body["cpu"] == "{{ _cpu if (_cpu | length > 0) else omit }}"
    assert body["agent"] == "{{ _agent }}"


def test_provision_refuses_vm_cpu_as_a_launch_override_in_pbs_mode():
    setter = next(task for play in playbook_yaml.plays(PROVISION)
                  for task in playbook_yaml.tasks(play.get("tasks", []))
                  if "_pbs_forbidden_extra_vars" in (task.get("vars") or {}))
    assert "vm_cpu" in setter["vars"]["_pbs_forbidden_extra_vars"]
    assert "vm_agent" in setter["vars"]["_pbs_forbidden_extra_vars"]


def test_the_current_vs_desired_report_shows_the_cpu_change_and_its_pending_restart(tmp_path):
    # The report is rendered from the playbook's own task, so a Jinja error in it fails here, not on a run.
    rc, out = run_play(tmp_path, {
        "_vmid": "1", "_node": "n", "_is_running": True,
        "_have_cores": 2, "_want_cores": "", "_have_memory": 4096, "_want_memory": "",
        "_have_agent": True, "_want_agent": True, "_agent_pending": False,
        "_have_disk_gb": 8, "_want_disk_gb": "", "_disk_device": "scsi0",
        "_have_cpu": "", "_want_cpu": "x86-64-v3", "_cpu_pending": True,
    }, [find_task(RESIZE, "Current vs desired")])
    assert rc == 0, out
    assert "cpu:    default -> x86-64-v3 (pending restart)" in out


def test_the_restart_note_names_a_waiting_cpu_model(tmp_path):
    rc, out = run_play(tmp_path, {
        "_want_cores": "", "_have_cores": 2, "_want_memory": "", "_have_memory": 4096,
        "_running_cores": 2, "_running_memory": 4096, "_sockets": 1,
        "_agent_awaits_restart": False, "_cpu_awaits_restart": True,
        "_needs_restart": True, "_allow_reboot": False,
    }, [find_task(RESIZE, "Report a pending change that needs a reboot")])
    assert rc == 0, out
    assert "The CPU model is also waiting for a restart." in out
