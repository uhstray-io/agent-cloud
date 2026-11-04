"""migrate-vm.yml moves ONE declared VM offline to its declared node, and refuses before any write.

The decision tests run the playbook's OWN tasks, found by name, through ansible-playbook on localhost
against synthetic cluster, precondition and recovery data. So a refusal, a no-write path or a rescue
decision is exercised, not only read. The structural pins read the playbook's own order: every write
after a fresh guard, the dry run and the destination checks before the first write, the offline body,
and no forced stop. The run tests need ansible-playbook and skip without it.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import forgeries
import playbook_yaml
import pytest
import yaml
from ansible_python import needs_ansible

ROOT = Path(__file__).resolve().parents[2]
PLAYBOOK = ROOT / "platform/playbooks/migrate-vm.yml"
TEMPLATES = ROOT / "platform/semaphore/templates.yml"
FILTER_DIR = ROOT / "platform/playbooks/filter_plugins"
GUARD_INCLUDE = "tasks/guard-o11y-restore-target.yml"
NODE_INCLUDE = "tasks/assert-node-cpu-model.yml"

# The writes, in file order: shut down, migrate, start again, and the rescue's start.
WRITE_NAMES = (
    "Shut the guest down cleanly (a move takes a stopped guest only)",
    "Migrate the VM offline to the declared node (target and targetstorage only)",
    "Start the guest again on its declared node (it was running before the move)",
    "Recovery: start the guest again on its original node",
)
DENIED_HEADER = "PVEAPIToken=fixture-id=fixture-secret-value"
PRE_DENIED = {
    "json": {"data": {"allowed_nodes": ["ariel"],
                      "not_allowed_nodes": {"apollo": {"unavailable_storages": ["local-lvm"]}}}},
    "invocation": {"module_args": {"headers": {"Authorization": DENIED_HEADER}}},
}
PRE_ALLOWED = {"json": {"data": {"allowed_nodes": ["apollo", "ariel"]}},
               "invocation": {"module_args": {"headers": {"Authorization": DENIED_HEADER}}}}


def _flat():
    """Every task of the playbook's own plays in file order, block and rescue children included."""
    return [t for play in playbook_yaml.plays(PLAYBOOK) for t in playbook_yaml.tasks(play.get("tasks", []))]


def find_task(name):
    for task in _flat():
        if task.get("name") == name:
            return task
    raise AssertionError(f"task {name!r} not found in {PLAYBOOK.name}")


def _is_guard(task):
    return task.get("ansible.builtin.include_tasks") == GUARD_INCLUDE


def _is_write(task):
    uri = task.get("ansible.builtin.uri", {})
    return (uri.get("method") in {"POST", "PUT", "DELETE"}
            and "/api2/json/" in str(uri.get("url", "")))


# The names every refusal message renders. A case overrides what it tests; the rest are the
# declaration the playbook would have computed, so a message cannot fail on an undefined name.
DEFAULTS = {"target_service": "plane", "target_host": "", "_decl_host": "plane-1", "_group_hosts": ["plane-1"],
            "_name": "plane", "_vmid": "225", "_dest_node": "apollo", "_from_node": "ariel",
            "_storage": "local-lvm", "_cpu": "", "_was_running": True}


def run_play(tmp_path, play_vars, tasks, *, extra=(), filter_path=False):
    """Run tasks as one localhost play. Returns (returncode, stdout+stderr)."""
    play_file = tmp_path / "play.yml"
    play_file.write_text(yaml.safe_dump([{"hosts": "localhost", "connection": "local",
                                          "gather_facts": False, "vars": {**DEFAULTS, **play_vars},
                                          "tasks": tasks}],
                                        sort_keys=False))
    env = {"ANSIBLE_NOCOLOR": "1", "PATH": os.environ["PATH"], "ANSIBLE_LOCAL_TEMP": str(tmp_path),
           "ANSIBLE_REMOTE_TEMP": str(tmp_path)}
    if filter_path:
        env["ANSIBLE_FILTER_PLUGINS"] = str(FILTER_DIR)
    proc = subprocess.run(["ansible-playbook", "-i", "localhost,", *extra, str(play_file)],
                          capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL, timeout=180,
                          check=False)
    return proc.returncode, proc.stdout + proc.stderr


def _sentinel():
    return {"ansible.builtin.debug": {"msg": "WRITE REACHED"}}


def _result(out, key="RESULT="):
    line = next(ln for ln in out.splitlines() if key in ln)
    return line.split(key, 1)[1].rstrip('"')


# ── structure: the playbook's own order ──────────────────────────────────────────────────────────


def test_the_playbook_imports_the_internal_name_guard_then_the_preflight_first():
    plays = yaml.safe_load(PLAYBOOK.read_text())
    assert plays[0]["ansible.builtin.import_playbook"] == "refuse-internal-extra-vars.yml"
    assert plays[1]["ansible.builtin.import_playbook"] == "preflight-o11y-restore-target-inputs.yml"
    assert plays[2]["ansible.builtin.import_playbook"] == "preflight-target-group.yml"


def test_the_template_is_a_dev_variant_with_its_own_environment_and_no_default_confirm():
    templates = yaml.safe_load(TEMPLATES.read_text())["templates"]
    assert sum(t["name"] == "Migrate VM" for t in templates) == 1
    tpl, = (t for t in templates if t["name"] == "Migrate VM")
    assert tpl["playbook"] == "platform/playbooks/migrate-vm.yml"
    assert tpl["dev_variant"] is True
    assert tpl["isolated_environment"] == "Migrate VM inputs"
    survey = {v["name"]: v for v in tpl["survey_vars"]}
    assert set(survey) == {"target_service", "target_host", "confirm_migrate"}
    assert survey["target_service"]["required"] is True
    assert survey["target_host"]["required"] is False
    assert survey["confirm_migrate"]["required"] is True
    assert "default_value" not in survey["confirm_migrate"]


def _touches_proxmox(task):
    return "ansible.builtin.uri" in task or task.get("ansible.builtin.include_tasks") == NODE_INCLUDE


def test_every_write_has_a_fresh_guard_after_the_last_proxmox_touch_before_it():
    # The guard reads the reservation, which can change during a poll, so the guard must sit after
    # the last Proxmox read or wait that precedes each write, not merely somewhere above it.
    tasks = _flat()
    writes = [i for i, t in enumerate(tasks) if _is_write(t)]
    assert [tasks[i]["name"] for i in writes] == list(WRITE_NAMES)
    guards = [i for i, t in enumerate(tasks) if _is_guard(t)]
    for index in writes:
        touches = [i for i in range(index) if _touches_proxmox(tasks[i])]
        last_touch = max(touches)
        assert any(last_touch < g < index for g in guards), f"no fresh guard before {tasks[index]['name']!r}"


def test_the_dry_run_stops_the_host_before_the_first_write():
    tasks = _flat()
    stop = next(i for i, t in enumerate(tasks) if t.get("name") == "Dry run: stop this host")
    assert tasks[stop]["ansible.builtin.meta"] == "end_host"
    assert tasks[stop]["when"] == "ansible_check_mode"
    assert stop < min(i for i, t in enumerate(tasks) if _is_write(t))


def test_identity_and_idempotence_are_checked_before_the_destination_and_the_destination_before_any_shutdown():
    names = [t.get("name") for t in _flat()]
    identity = names.index("Refuse to migrate a VM that is not the declared guest")
    already = names.index("End play when the VM is already on the declared node")
    precondition = names.index("Read Proxmox's migration precondition for the declared destination")
    refuse = names.index("Refuse a destination Proxmox does not allow")
    cpu = names.index("Refuse a named CPU model the destination node cannot provide")
    dry = names.index("Dry run: report the planned move")
    shutdown = names.index(WRITE_NAMES[0])
    assert identity < already < precondition < refuse < cpu < dry < shutdown


def test_a_named_cpu_model_is_checked_on_the_destination_node_when_declared():
    include = find_task("Refuse a named CPU model the destination node cannot provide")
    assert include["ansible.builtin.include_tasks"] == NODE_INCLUDE
    assert include["vars"] == {"_ncm_node": "{{ _dest_node }}", "_ncm_want_cpu": "{{ _cpu }}"}
    assert include["when"] == "_cpu | length > 0"


def test_the_move_is_offline_and_its_body_names_only_the_destination_and_its_storage():
    task = find_task("Migrate the VM offline to the declared node (target and targetstorage only)")
    uri = task["ansible.builtin.uri"]
    assert uri["method"] == "POST"
    assert uri["url"] == "{{ _pve_host }}/api2/json/nodes/{{ _from_node }}/qemu/{{ _vmid }}/migrate"
    assert uri["body"] == {"target": "{{ _dest_node }}", "targetstorage": "{{ _storage }}"}
    assert "online" not in uri["body"]
    assert "with-local-disks" not in json.dumps(task)


def test_no_forced_stop_is_ever_sent():
    text = json.dumps(_flat())
    assert "status/stop" not in text
    assert "forceStop" not in text


def test_the_write_sequence_is_a_block_whose_rescue_recovers_before_it_fails():
    block, = (t for t in _flat() if t.get("name") == "Move the VM: shut down if running, migrate offline, "
                                                      "start again only if it was running")
    child_names = [c.get("name") for c in block["block"]]
    assert child_names.index(WRITE_NAMES[1]) < child_names.index("Re-check the VM's identity after the move")
    rescue_names = [c.get("name") for c in block["rescue"]]
    assert rescue_names.index("Recovery: decide whether a start is safe") < rescue_names.index(
        "Fail with the reason, after recovering")


def test_the_rescue_start_is_gated_on_the_safe_decision_and_uses_the_original_node():
    start = find_task(WRITE_NAMES[3])
    assert "_rescue_start_ok | bool" in start["when"]
    assert start["ansible.builtin.uri"]["url"] == (
        "{{ _pve_host }}/api2/json/nodes/{{ _from_node }}/qemu/{{ _vmid }}/status/start")


def test_the_post_move_start_uses_only_the_verified_declared_node():
    start = find_task(WRITE_NAMES[2])
    assert start["ansible.builtin.uri"]["url"] == "{{ _pve_host }}/api2/json/nodes/{{ _dest_node }}/qemu/{{ _vmid }}/status/start"


def test_the_header_documents_the_usage_and_that_the_cpu_model_is_checked_not_written():
    head = "\n".join(PLAYBOOK.read_text().splitlines()[:80])
    assert "-e target_service=plane -e confirm_migrate=225" in head
    assert "only CHECKED here, never written" in head


# ── refusals, run through the playbook's own tasks ───────────────────────────────────────────────


@needs_ansible
@pytest.mark.parametrize("confirm, ok", [("225", True), (225, True), ("999", False), ("", False)])
def test_the_run_must_name_the_declared_vmid(tmp_path, confirm, ok):
    rc, out = run_play(tmp_path, {"confirm_migrate": confirm, "_vmid": "225"},
                       [find_task("Require the run to NAME the vmid it moves")])
    assert (rc == 0) is ok, out
    if not ok:
        assert "Refusing: pass -e confirm_migrate=225" in out


@needs_ansible
@pytest.mark.parametrize("live, ok", [
    ({"name": "plane", "type": "qemu", "node": "ariel"}, True),
    ({"name": "other", "type": "qemu", "node": "ariel"}, False),
    ({"name": "plane", "type": "lxc", "node": "ariel"}, False),
    ({"name": "plane", "node": "ariel"}, False),
])
def test_a_live_vm_that_is_not_the_declared_qemu_guest_is_refused(tmp_path, live, ok):
    rc, out = run_play(tmp_path, {"_vmid": "225", "_name": "plane", "_live": live},
                       [find_task("Refuse to migrate a VM that is not the declared guest")])
    assert (rc == 0) is ok, out
    if not ok:
        assert "somebody else" in out


@needs_ansible
def test_an_absent_vmid_is_refused_and_is_not_read_as_a_done_move(tmp_path):
    rc, out = run_play(tmp_path, {"_vmid": "225", "_dest_node": "apollo", "_live": {}},
                       [find_task("Refuse when the vmid is not in the cluster (nothing to migrate)")])
    assert rc != 0 and "is not in the cluster" in out, out


@needs_ansible
@pytest.mark.parametrize("node, sentinel_expected", [("apollo", False), ("ariel", True)])
def test_a_vm_already_on_the_declared_node_passes_without_a_write(tmp_path, node, sentinel_expected):
    tasks = [find_task("Already on the declared node: nothing to move"),
             find_task("End play when the VM is already on the declared node"),
             _sentinel()]
    rc, out = run_play(tmp_path, {"_vmid": "225", "_name": "plane", "_dest_node": "apollo",
                                  "_live": {"node": node, "name": "plane", "type": "qemu"}}, tasks)
    assert rc == 0, out
    assert ("WRITE REACHED" in out) is sentinel_expected, out


@needs_ansible
def test_a_destination_proxmox_does_not_allow_is_refused_with_its_reason_and_no_header(tmp_path):
    tasks = [find_task("Work out whether the destination is allowed, and the reason when it is not"),
             find_task("Refuse a destination Proxmox does not allow"), _sentinel()]
    rc, out = run_play(tmp_path, {"_dest_node": "apollo", "_from_node": "ariel", "_name": "plane",
                                  "_vmid": "225", "_pre": PRE_DENIED}, tasks)
    assert rc != 0 and "unavailable_storages" in out and "Nothing was shut down" in out, out
    assert "WRITE REACHED" not in out
    assert "fixture-secret-value" not in out and "PVEAPIToken" not in out


@needs_ansible
def test_a_destination_listed_as_allowed_passes(tmp_path):
    tasks = [find_task("Work out whether the destination is allowed, and the reason when it is not"),
             find_task("Refuse a destination Proxmox does not allow"), _sentinel()]
    rc, out = run_play(tmp_path, {"_dest_node": "apollo", "_from_node": "ariel", "_name": "plane",
                                  "_vmid": "225", "_pre": PRE_ALLOWED}, tasks)
    assert rc == 0 and "WRITE REACHED" in out, out


@needs_ansible
def test_a_destination_missing_from_both_lists_is_refused_as_not_allowed(tmp_path):
    pre = {"json": {"data": {"allowed_nodes": ["ariel"], "not_allowed_nodes": {}}}}
    tasks = [find_task("Work out whether the destination is allowed, and the reason when it is not"),
             find_task("Refuse a destination Proxmox does not allow")]
    rc, out = run_play(tmp_path, {"_dest_node": "apolo", "_from_node": "ariel", "_name": "plane",
                                  "_vmid": "225", "_pre": pre}, tasks)
    assert rc != 0 and "not listed as allowed" in out, out


@needs_ansible
@pytest.mark.parametrize("vmid, name, node, storage, ok", [
    ("225", "plane", "apollo", "local-lvm", True),
    ("", "plane", "apollo", "local-lvm", False),
    ("225", "", "apollo", "local-lvm", False),
    ("225", "plane", "", "local-lvm", False),
    ("225", "plane", "apollo", "", False),
])
def test_a_declaration_missing_any_identity_or_destination_field_is_refused(tmp_path, vmid, name, node, storage, ok):
    rc, out = run_play(tmp_path, {"_vmid": vmid, "_name": name, "_dest_node": node, "_storage": storage},
                       [find_task("Require a complete declaration (vm_vmid, vm_name, vm_node, vm_disk_storage)")])
    assert (rc == 0) is ok, out
    if not ok:
        assert "lacks vm_vmid, vm_name, vm_node or vm_disk_storage" in out, out


@needs_ansible
@pytest.mark.parametrize("group, target_host, decl_host, ok", [
    (["plane-1"], "", "plane-1", True),
    (["plane-1", "plane-2"], "", "plane-1", False),
    (["plane-1", "plane-2"], "plane-2", "plane-2", True),
    (["plane-1", "plane-2"], "other-svc", "other-svc", False),
])
def test_target_host_must_name_one_host_of_the_service_when_it_has_several(tmp_path, group, target_host,
                                                                           decl_host, ok):
    rc, out = run_play(tmp_path, {"target_service": "plane", "_group_hosts": group, "target_host": target_host,
                                  "_decl_host": decl_host},
                       [find_task("Require an explicit target_host when the service declares more than one")])
    assert (rc == 0) is ok, out
    if not ok:
        assert "pass -e target_host=<one of those names>" in out, out


@needs_ansible
@pytest.mark.parametrize("value", ["", "x86-64-v3", "x86-64-v2-AES", "host", "custom-lab_1.0"])
def test_a_bare_cpu_model_name_passes_the_format_check(tmp_path, value):
    rc, out = run_play(tmp_path, {"_cpu": value}, [find_task("Refuse a vm_cpu that is not a bare model name")])
    assert rc == 0, out


@needs_ansible
@pytest.mark.parametrize("value", ["x86-64-v3,flags=+aes", "host,hidden=1", "x86=3", "x86 64", "-x86",
                                   "x86-64-v3;x"])
def test_a_cpu_value_that_could_carry_extra_keys_is_refused(tmp_path, value):
    rc, out = run_play(tmp_path, {"_cpu": value}, [find_task("Refuse a vm_cpu that is not a bare model name")])
    assert rc != 0 and "Nothing was moved" in out, out


# Each declared identity name, forged as a plain, context-keyed and stateful template (forgeries.py).
FORGED_IDENTITY = [
    pytest.param(f.values[0], id=f"{name}-{f.id}")
    for name, honest, forged in (("vm_vmid", "225", "999"), ("vm_name", "plane", "other"),
                                 ("vm_node", "apollo", "ariel"), ("vm_disk_storage", "local-lvm", "local"),
                                 ("vm_cpu", "x86-64-v3", "host"))
    for f in forgeries.templated_forgeries(name, honest, forged)
]


@needs_ansible
def test_the_identity_guard_passes_a_run_that_declares_its_own_identity(tmp_path):
    rc, out = run_play(tmp_path, {}, [find_task("Refuse a launch that overrides the declared identity")],
                       filter_path=True)
    assert rc == 0, out


@needs_ansible
@pytest.mark.parametrize("forge", FORGED_IDENTITY)
def test_a_forged_declared_identity_value_is_refused(tmp_path, forge):
    rc, out = run_play(tmp_path, {}, [find_task("Refuse a launch that overrides the declared identity")],
                       extra=["-e", forge(tmp_path)], filter_path=True)
    assert rc != 0 and "set as an extra var" in out, out


@needs_ansible
@pytest.mark.parametrize("exitstatus, ok", [("OK", True), ("migration aborted", False)])
def test_the_move_fails_unless_the_migrate_task_reports_ok(tmp_path, exitstatus, ok):
    task = {"json": {"data": {"status": "stopped", "exitstatus": exitstatus}}}
    rc, out = run_play(tmp_path, {"_migrate_task": task}, [find_task("Verify the migrate task succeeded")])
    assert (rc == 0) is ok, out
    if not ok:
        assert "Migrate to apollo failed: task exit status migration aborted" in out, out


@needs_ansible
@pytest.mark.parametrize("name, node, ok", [
    ("plane", "apollo", True),
    ("other", "apollo", False),
    ("plane", "ariel", False),
])
def test_the_identity_is_rechecked_on_the_declared_node_after_the_move(tmp_path, name, node, ok):
    placed = {"json": {"data": [{"vmid": 225, "name": name, "node": node}]}}
    rc, out = run_play(tmp_path, {"_placed": placed}, [find_task("Re-check the VM's identity after the move")])
    assert (rc == 0) is ok, out
    if not ok:
        assert "after the move" in out, out


def test_the_shutdown_and_the_start_happen_only_when_the_guest_was_running():
    for name in (WRITE_NAMES[0], WRITE_NAMES[2]):
        assert "_was_running | bool" in find_task(name)["when"], name


@needs_ansible
def test_the_dry_run_reports_the_plan_and_changes_nothing(tmp_path):
    tasks = [find_task("Dry run: report the planned move"), find_task("Dry run: stop this host"), _sentinel()]
    rc, out = run_play(tmp_path, {"_name": "plane", "_vmid": "225", "_from_node": "ariel", "_dest_node": "apollo",
                                  "_storage": "local-lvm", "_was_running": True}, tasks, extra=["--check"])
    assert rc == 0, out
    assert "would be shut down, moved offline to apollo" in out
    assert "Nothing was changed." in out
    assert "WRITE REACHED" not in out


# ── the rescue: starts only what this run found running, stopped on its original node ───────────

RESCUE_DECIDE = ("Recovery: work out where the VM is", "Recovery: read its node, state and lock",
                 "Recovery: decide whether a start is safe")
PLANE = {"vmid": 225, "name": "plane"}


def _record(**fields):
    return {"json": {"data": [{**PLANE, **fields}]}}


@needs_ansible
@pytest.mark.parametrize("live, was_running, expect_start", [
    (_record(node="ariel", status="stopped"), True, True),
    (_record(node="ariel", status="stopped", lock=""), True, True),
    (_record(node="ariel", status="stopped"), False, False),
    (_record(node="ariel", status="running"), True, False),
    (_record(node="apollo", status="stopped"), True, False),
    (_record(node="ariel", status="stopped", lock="migrate"), True, False),
    (_record(node="ariel", status="stopped", name="other"), True, False),
    (_record(node="ariel"), True, False),
    ({}, True, False),
], ids=["started-back", "lock-empty", "not-running-before", "still-running", "on-destination",
        "locked", "name-changed", "status-unknown", "unreadable"])
def test_the_rescue_starts_a_guest_only_when_this_run_found_it_running_and_it_is_stopped_on_its_source(
        tmp_path, live, was_running, expect_start):
    tasks = [find_task(n) for n in RESCUE_DECIDE]
    tasks.append({"ansible.builtin.debug": {"msg": "RESULT={{ _rescue_start_ok | bool }}"}})
    rc, out = run_play(tmp_path, {"_rescue_live": live, "_vmid": "225", "_name": "plane", "_from_node": "ariel",
                                  "_dest_node": "apollo", "_was_running": was_running}, tasks)
    assert rc == 0, out
    assert _result(out) == str(expect_start), out


@needs_ansible
@pytest.mark.parametrize("start, start_ok, was_running, node, status, locked, expected", [
    ({}, True, True, "ariel", "stopped", False, "this run started it there again"),
    ({"failed": True, "msg": "boom"}, True, True, "ariel", "stopped", False, "Starting it on ariel FAILED (boom)"),
    ({"skipped": True}, False, False, "ariel", "stopped", False, "It was not running before this run"),
    ({"skipped": True}, False, True, "apollo", "stopped", False, "It is on apollo, not its original ariel"),
    ({"skipped": True}, False, True, "ariel", "running", False, "It is running, not stopped, so no start"),
    ({"skipped": True}, False, True, "ariel", "stopped", True, "It is still locked, so no start"),
], ids=["started", "start-failed", "not-running", "on-other-node", "still-running", "locked"])
def test_the_failure_note_says_where_the_guest_is_and_whether_it_was_started(
        tmp_path, start, start_ok, was_running, node, status, locked, expected):
    tasks = [find_task("Recovery: say what was done and what is left to check"),
             {"ansible.builtin.debug": {"msg": "RESULT={{ _start_note }}"}}]
    rc, out = run_play(tmp_path, {"_rescue_start_ok": start_ok, "_rescue_start": start,
                                  "_was_running": was_running, "_rescue_node": node, "_from_node": "ariel",
                                  "_rescue_status": status, "_rescue_locked": locked}, tasks)
    assert rc == 0, out
    assert expected in out, out


@needs_ansible
def test_the_report_renders_for_a_running_guest_and_names_the_cpu_step(tmp_path):
    rc, out = run_play(tmp_path, {"_name": "plane", "_vmid": "225", "_from_node": "ariel", "_dest_node": "apollo",
                                  "_storage": "local-lvm", "_was_running": True, "_cpu": "x86-64-v3"},
                       [find_task("Report")])
    assert rc == 0, out
    assert "started again on apollo" in out
    assert "Run Resize VM to converge vm_cpu (declared: x86-64-v3)" in out


# ── the shared CPU check and the offline body are the playbook's own, not copies ─────────────────


@needs_ansible
def test_the_playbook_passes_ansible_syntax_check():
    proc = subprocess.run(["ansible-playbook", "-i", "localhost,", "--syntax-check", str(PLAYBOOK)],
                          cwd=ROOT, capture_output=True, text=True,
                          env={"ANSIBLE_NOCOLOR": "1", "PATH": os.environ["PATH"]},
                          stdin=subprocess.DEVNULL, timeout=180, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
