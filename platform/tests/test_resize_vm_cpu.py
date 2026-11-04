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


def test_provision_checks_the_declared_node_before_the_clone_and_so_before_any_write():
    include = find_task(PROVISION, NODE_CHECK)
    assert include["ansible.builtin.include_tasks"] == NODE_INCLUDE
    assert include["vars"] == {"_ncm_node": "{{ _node }}", "_ncm_want_cpu": "{{ _cpu }}"}
    assert include["when"] == "_cpu | length > 0"
    names = task_names(PROVISION)
    # Before the clone guard, so a refused model never reaches the clone, the migration or the write.
    assert names.index(NODE_CHECK) < names.index("Guard target and clone-source VMIDs before cloning")
    assert names.index(NODE_CHECK) < names.index("Clone template to new VM (on the template's node)")
    assert names.index(NODE_CHECK) < names.index("Configure VM resources and cloud-init")


def test_provision_puts_the_model_in_the_configure_body_and_omits_it_when_unset():
    # Scoped to the one task: the bats slice of the same block stops at its first status_code.
    body = find_task(PROVISION, "Configure VM resources and cloud-init")["ansible.builtin.uri"]["body"]
    assert body["cpu"] == "{{ _cpu_put if (_cpu | length > 0) else omit }}"
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


def result_line(out):
    line = next(ln for ln in out.splitlines() if "RESULT=" in ln)
    return json.loads(line.split("RESULT=", 1)[1].rstrip('"').replace('\\"', '"'))


@pytest.mark.parametrize("cur, put", [
    (None, "x86-64-v3"),
    ("x86-64-v2-AES,flags=+aes;-md-clear", "x86-64-v3,flags=+aes;-md-clear"),
    ("cputype=host,hidden=1", "x86-64-v3,hidden=1"),
])
def test_provision_keeps_the_flags_an_adopted_vm_already_carries(tmp_path, cur, put):
    # Runs the playbook's own two tasks against the VM's current config, so the rebuild is what runs.
    tasks = [find_task(PROVISION, "List the VM's CPU items as they are now"),
             find_task(PROVISION, "Work out the CPU model to write"),
             {"ansible.builtin.debug": {"msg": "RESULT={{ {'value': _cpu_put} | to_json }}"}}]
    data = {} if cur is None else {"cpu": cur}
    rc, out = run_play(tmp_path, {"_cpu": "x86-64-v3", "_cpu_cur": {"json": {"data": data}}}, tasks)
    assert rc == 0, out
    assert result_line(out) == {"value": put}


RESTORE_TASK = "Recovery: work out whether the CPU model must be restored before a start"


@pytest.mark.parametrize("cfg_cpu, changes, status, needed, body", [
    ("x86-64-v2-AES,flags=+aes", {"cpu": "x86-64-v3,flags=+aes"}, "stopped", True,
     {"cpu": "x86-64-v2-AES,flags=+aes"}),
    (None, {"cpu": "x86-64-v3"}, "stopped", True, {"delete": "cpu"}),
    ("x86-64-v2-AES", {"cpu": "x86-64-v3"}, "running", False, {"cpu": "x86-64-v2-AES"}),
    ("x86-64-v2-AES", {}, "stopped", False, {"cpu": "x86-64-v2-AES"}),
])
def test_a_stopped_guest_gets_the_cpu_model_it_had_before_the_run(tmp_path, cfg_cpu, changes, status,
                                                                 needed, body):
    cfg = {} if cfg_cpu is None else {"cpu": cfg_cpu}
    tasks = [find_task(RESIZE, RESTORE_TASK),
             {"ansible.builtin.debug": {"msg": "RESULT={{ {'needed': _cpu_restore_needed | bool, "
                                               "'body': _cpu_restore_body} | to_json }}"}}]
    rc, out = run_play(tmp_path, {"_cfg": {"json": {"data": cfg}}, "_cfg_changes": changes,
                                  "_rescue_state": {"json": {"data": {"status": status}}}}, tasks)
    assert rc == 0, out
    assert result_line(out) == {"needed": needed, "body": body}


def test_resize_restores_the_cpu_model_after_a_failed_restart_and_before_any_start():
    names = task_names(RESIZE)
    guard = names.index("Recovery: guard target VMID before the CPU model restore")
    restore = names.index("Recovery: restore the previous CPU model before any start")
    start_guard = names.index("Recovery: guard target VMID before start")
    start = names.index("Recovery: bring a stopped guest back up")
    assert guard < restore < start_guard < start
    restore_task = find_task(RESIZE, "Recovery: restore the previous CPU model before any start")
    assert restore_task["register"] == "_cpu_restore" and restore_task["ignore_errors"] is True
    assert restore_task["ansible.builtin.uri"]["body"] == "{{ _cpu_restore_body }}"
    # A failed restore withholds the start: starting would boot the new model.
    assert "not (_cpu_restore is failed)" in find_task(RESIZE, "Recovery: bring a stopped guest back up")["when"]


DECIDE = "Decide the rightsize step"
RECORD = "Record the step result"


def rightsize(tmp_path, want, cfg_cpu):
    cfg = {"cores": 4, "memory": 8192, "scsi0": "vm-lvms:vm-215-disk-0,size=32G"}
    if cfg_cpu is not None:
        cfg["cpu"] = cfg_cpu
    # The record task's own evidence expression, evaluated after the decision it reads.
    play_vars = {"_want_cores": "4", "_want_memory": "8192", "_want_disk_gb": "32", "_disk_device": "scsi0",
                 "_needs_restart": False, "_allow_reboot": False, "_cfg_after": {"json": {"data": cfg}},
                 "_want_cpu": want,
                 "_rs_ev_expr": find_task(RESIZE, RECORD)["vars"]["step_result_evidence"]}
    tasks = [find_task(RESIZE, DECIDE),
             {"ansible.builtin.debug": {"msg": "RESULT={{ {'errors': _rs_errors, "
                                               "'evidence': _rs_ev_expr} | to_json }}"}}]
    rc, out = run_play(tmp_path, play_vars, tasks)
    assert rc == 0, out
    return result_line(out)


def test_rightsize_passes_when_the_reread_model_is_the_declared_one(tmp_path):
    result = rightsize(tmp_path, "x86-64-v3", "x86-64-v3")
    assert result == {"errors": [], "evidence": {"cores": 4, "memory_mb": 8192, "disk_gb": 32, "cpu": "x86-64-v3"}}


def test_rightsize_fails_on_a_different_reread_model_and_names_it(tmp_path):
    # The message names the model the config carries, not its flags.
    assert rightsize(tmp_path, "x86-64-v3", "x86-64-v2-AES,flags=+aes")["errors"] == [
        "cpu x86-64-v2-AES, declared x86-64-v3"]


def test_rightsize_reads_an_absent_model_as_the_default(tmp_path):
    assert rightsize(tmp_path, "x86-64-v3", None)["errors"] == ["cpu default, declared x86-64-v3"]


def test_rightsize_records_no_cpu_key_for_an_unmanaged_vm(tmp_path):
    result = rightsize(tmp_path, "", "x86-64-v2-AES")
    assert result == {"errors": [], "evidence": {"cores": 4, "memory_mb": 8192, "disk_gb": 32}}


def provision_decide(tmp_path, cfg_cpu, want):
    cfg = {"cores": 2, "memory": 4096, "onboot": 1, "scsi0": "vm-lvms:vm-215-disk-0,size=32G"}
    if cfg_cpu is not None:
        cfg["cpu"] = cfg_cpu
    play_vars = {"target_service": "svc", "_vmid": "215", "_node": "n", "_cores": "2", "_mem": "4096",
                 "_disk": "32", "_onboot": "1", "_vm_exists": True, "_do_migrate": False, "_cpu": want,
                 "_pv_cfg": {"status": 200, "json": {"data": cfg}}}
    tasks = [find_task(PROVISION, "Decide the provision-vm step"),
             {"ansible.builtin.debug": {"msg": "RESULT={{ {'errors': _pv_step.errors, "
                                               "'cpu_declared': _pv_step.cpu_declared} | to_json }}"}}]
    rc, out = run_play(tmp_path, play_vars, tasks)
    assert rc == 0, out
    return result_line(out)


def test_provision_reads_a_matching_model_back_as_clean(tmp_path):
    assert provision_decide(tmp_path, "x86-64-v3", "x86-64-v3") == {"errors": [], "cpu_declared": True}


def test_provision_reports_a_read_back_model_that_differs(tmp_path):
    assert provision_decide(tmp_path, "x86-64-v2-AES,flags=+aes", "x86-64-v3") == {
        "errors": ["cpu x86-64-v2-AES, declared x86-64-v3"], "cpu_declared": True}


def test_provision_ignores_the_model_when_none_is_declared(tmp_path):
    assert provision_decide(tmp_path, "x86-64-v2-AES", "") == {"errors": [], "cpu_declared": False}


def provision_evidence(tmp_path, pv_s):
    play = next(p for p in playbook_yaml.plays(PROVISION) if "_pv_evidence" in (p.get("vars") or {}))
    rc, out = run_play(tmp_path, {"_pv_s": pv_s, "_pv_evidence": play["vars"]["_pv_evidence"]},
                       [{"ansible.builtin.debug": {"msg": "RESULT={{ {'evidence': _pv_evidence} | to_json }}"}}])
    assert rc == 0, out
    return result_line(out)["evidence"]


def test_provision_evidence_records_the_model_only_when_declared(tmp_path):
    base = {"vmid": 215, "node": "n", "onboot": "1"}
    assert provision_evidence(tmp_path, {**base, "cpu": "x86-64-v3", "cpu_declared": True}) == {**base, "cpu": "x86-64-v3"}
    assert provision_evidence(tmp_path, {**base, "cpu": "x86-64-v2-AES", "cpu_declared": False}) == base
    assert provision_evidence(tmp_path, base) == base
