"""The VM lifecycle executors record their workflow step result (D10 review 2026-10-02).

Create VM Template (vm-template), Provision VM (provision-vm) and Resize VM (vm-rightsize)
each judge their step from a Proxmox read-back and record it through emit-step-result.yml.
These tests run each playbook's REAL decide + record tasks against synthetic read-backs and
read the recorded result from Ansible's custom stats, so the verdict logic is exercised, not
just its text. Needs ansible-playbook.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
PLAYBOOKS = ROOT / "platform/playbooks"
EMIT = str(PLAYBOOKS / "tasks/emit-step-result.yml")
RUN = re.compile(r"^\s*RUN:\s*(\{.*\})\s*$")

pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")


def _plays(name):
    return yaml.safe_load((PLAYBOOKS / name).read_text())


def _tasks(play, names):
    by_name = {t.get("name"): t for t in play["tasks"]}
    missing = [n for n in names if n not in by_name]
    assert not missing, missing
    out = []
    for n in names:
        task = dict(by_name[n])
        if "ansible.builtin.include_tasks" in task:
            task["ansible.builtin.include_tasks"] = EMIT
        out.append(task)
    return out


def _run(tmp_path, harness, check=False):
    play_file = tmp_path / "play.yml"
    play_file.write_text(yaml.safe_dump(harness, sort_keys=False))
    done = subprocess.run(
        ["ansible-playbook", "-i", "localhost,", "-c", "local", str(play_file), *(["--check"] if check else [])],
        capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120,
        env={"ANSIBLE_NOCOLOR": "1", "PATH": os.environ["PATH"], "ANSIBLE_LOCAL_TEMP": str(tmp_path),
             "ANSIBLE_SHOW_CUSTOM_STATS": "1"})
    results = [json.loads(m.group(1))["step_result"] for m in map(RUN.match, done.stdout.splitlines()) if m]
    assert len(results) == 1, done.stdout + done.stderr
    return results[0], done.returncode


# ── vm-template ──────────────────────────────────────────────────────────────
TEMPLATE_TASKS = ("Decide the template step", "Record the step result", "Fail when the template step did not pass")


def _template(tmp_path, data, status=200, present=True, check=False):
    play = next(p for p in _plays("provision-template.yml") if p.get("tasks"))
    harness = [{"hosts": "localhost", "gather_facts": False,
                "vars": {"_vmid": "9000", "_node": "n1", "_tmpl_present": present,
                         "_tmpl_after": {"status": status, "json": {"data": data}}},
                "tasks": _tasks(play, TEMPLATE_TASKS)}]
    return _run(tmp_path, harness, check)


def test_template_with_a_cloud_init_drive_passes(tmp_path):
    result, rc = _template(tmp_path, {"template": 1, "ide2": "vm-lvms:vm-9000-cloudinit,media=cdrom"})
    assert (result["step"], result["status"], rc) == ("vm-template", "pass", 0)
    assert result["evidence"] == {"template_vmid": 9000, "node": "n1"}


def test_template_without_a_cloud_init_drive_fails_and_says_why(tmp_path):
    result, rc = _template(tmp_path, {"template": 1, "ide2": "none,media=cdrom"})
    assert (result["status"], rc != 0) == ("fail", True)
    assert "no cloud-init drive" in result["error"]


def test_a_vm_that_is_not_a_template_fails(tmp_path):
    result, rc = _template(tmp_path, {}, status=500)
    assert (result["status"], rc != 0) == ("fail", True)


def test_a_dry_run_with_no_template_yet_records_a_skip(tmp_path):
    result, rc = _template(tmp_path, {}, status=500, present=False, check=True)
    assert (result["status"], result["check_mode"], rc) == ("skip", True, 0)


# ── vm-rightsize ─────────────────────────────────────────────────────────────
RESIZE_TASKS = ("Decide the rightsize step", "Record the step result",
                "Fail when the VM does not match its declared spec")


def _resize(tmp_path, cfg, needs_restart=False, allow_reboot=False):
    play = _plays("resize-vm.yml")[0]
    harness = [{"hosts": "localhost", "gather_facts": False,
                "vars": {"_want_cores": "4", "_want_memory": "8192", "_want_disk_gb": "32",
                         "_disk_device": "scsi0", "_needs_restart": needs_restart,
                         "_allow_reboot": allow_reboot, "_cfg_after": {"json": {"data": cfg}}},
                "tasks": _tasks(play, RESIZE_TASKS)}]
    return _run(tmp_path, harness)


CONVERGED = {"cores": 4, "memory": 8192, "scsi0": "vm-lvms:vm-215-disk-0,size=32G"}


def test_rightsize_converged_passes_with_the_read_back_values(tmp_path):
    result, rc = _resize(tmp_path, CONVERGED)
    assert (result["step"], result["status"], rc) == ("vm-rightsize", "pass", 0)
    assert result["evidence"] == {"cores": 4, "memory_mb": 8192, "disk_gb": 32}


def test_rightsize_with_a_restart_pending_is_not_live_yet(tmp_path):
    result, rc = _resize(tmp_path, CONVERGED, needs_restart=True)
    assert (result["status"], rc != 0) == ("fail", True)
    assert "restart is pending" in result["error"]


def test_rightsize_drift_fails_per_dimension(tmp_path):
    result, _ = _resize(tmp_path, {"cores": 2, "memory": 8192, "scsi0": "x,size=320G"})
    assert result["status"] == "fail"
    assert "cores 2" in result["error"] and "is not 32G" in result["error"]


# ── provision-vm ─────────────────────────────────────────────────────────────
def _provision(tmp_path, cfg, status=200, vm_exists=True, do_migrate=False, provisioned=False, check=False):
    plays = _plays("provision-vm.yml")
    prov = next(p for p in plays if p.get("name") == "Provision VM from template")
    record = next(p for p in plays if p.get("name") == "Record the provision-vm step result")
    decide = _tasks(prov, ["Decide the provision-vm step"])
    rec = dict(record, tasks=_tasks(record, [t["name"] for t in record["tasks"]]))
    harness = [{"hosts": "localhost", "gather_facts": False,
                "vars": {"target_service": "dns", "_vmid": "220", "_node": "n1", "_cores": 2, "_mem": 4096,
                         "_disk": "32G", "_onboot": "1", "vm_exists": vm_exists, "_do_migrate": do_migrate,
                         "_pv_cfg": {"status": status, "json": {"data": cfg}}},
                "tasks": decide}]
    if provisioned:
        harness[0]["tasks"].append({"ansible.builtin.add_host": {"name": "vm1", "groups": "_provisioned_vm"}})
    return _run(tmp_path, harness + [rec], check)


GOOD_VM = {"cores": 2, "memory": 4096, "onboot": 1, "scsi0": "vm-lvms:vm-220-disk-0,size=32G"}


def test_provision_vm_matching_the_declaration_passes(tmp_path):
    result, rc = _provision(tmp_path, GOOD_VM)
    assert (result["step"], result["status"], rc) == ("provision-vm", "pass", 0)
    assert result["evidence"] == {"vmid": 220, "node": "n1", "onboot": 1}
    assert result["undo"] == "Destroy VM"


def test_provision_vm_without_onboot_fails(tmp_path):
    result, rc = _provision(tmp_path, {**GOOD_VM, "onboot": 0})
    assert (result["status"], rc != 0) == ("fail", True)
    assert "onboot 0" in result["error"]


def test_provision_vm_failure_waits_for_the_verdict_when_post_boot_follows(tmp_path):
    # The record play must not stop the run before the post-boot checks; the verdict fails it.
    result, rc = _provision(tmp_path, {**GOOD_VM, "scsi0": "x,size=20G"}, provisioned=True)
    assert (result["status"], rc) == ("fail", 0)


def test_provision_vm_dry_run_of_an_absent_vm_records_a_skip(tmp_path):
    result, rc = _provision(tmp_path, {}, status=404, vm_exists=False, check=True)
    assert (result["status"], result["check_mode"], rc) == ("skip", True, 0)


def test_provision_vm_verdict_fails_on_a_recorded_mismatch():
    verdict = next(p for p in _plays("provision-vm.yml") if str(p.get("name", "")).startswith("Verdict:"))
    that = verdict["tasks"][0]["ansible.builtin.assert"]["that"]
    assert any("_pv_step.errors" in c for c in that)


@pytest.mark.parametrize("playbook,step", [("provision-template.yml", "vm-template"),
                                           ("resize-vm.yml", "vm-rightsize"),
                                           ("provision-vm.yml", "provision-vm")])
def test_the_result_is_recorded_after_every_change_the_play_makes(playbook, step):
    # "Emitted last": nothing after the record but the failure that reports it.
    for play in _plays(playbook):
        tasks = play.get("tasks") or []
        idx = [i for i, t in enumerate(tasks)
               if str(t.get("ansible.builtin.include_tasks", "")).endswith("emit-step-result.yml")]
        if not idx:
            continue
        assert tasks[idx[0]]["vars"]["step_result_step"] == step
        assert all("ansible.builtin.fail" in t for t in tasks[idx[0] + 1:]), playbook
        return
    pytest.fail(f"{playbook} records no step result")
