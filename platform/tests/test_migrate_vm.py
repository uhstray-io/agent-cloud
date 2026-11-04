"""migrate-vm.yml moves ONE declared VM offline to its declared node, and refuses before any write.

The decision tests run the playbook's OWN tasks, found by name, through ansible-playbook on localhost
against synthetic cluster, precondition, configuration and recovery data, so a refusal, a no-write
path or a rescue decision is exercised, not only read. The structural pins read the playbook's own
order: every write after a fresh guard and after a dry-run stop, the checks before the first write,
the offline body, the bounded waits, and no forced stop. The run tests need ansible-playbook.
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

# The writes, in file order: the already-there start, shut down, migrate, the start after the move,
# and the rescue's start.
WRITE_NAMES = (
    "Start the guest on its declared node (already there, stopped, start_after true)",
    "Shut the guest down cleanly (a move takes a stopped guest only)",
    "Migrate the VM offline to the declared node (target and targetstorage only)",
    "Start the guest on its declared node after the move",
    "Recovery: start the guest again where it is",
)
DENIED_HEADER = "PVEAPIToken=fixture-id=fixture-secret-value"
PRE_DENIED = {
    "json": {
        "data": {"allowed_nodes": ["ariel"], "not_allowed_nodes": {"apollo": {"unavailable_storages": ["local-lvm"]}}}
    },
    "invocation": {"module_args": {"headers": {"Authorization": DENIED_HEADER}}},
}
PRE_ALLOWED = {
    "json": {"data": {"allowed_nodes": ["apollo", "ariel"]}},
    "invocation": {"module_args": {"headers": {"Authorization": DENIED_HEADER}}},
}
PRE_CLEAN = {"json": {"data": {"allowed_nodes": ["apollo", "ariel"], "local_resources": [], "local_disks": []}}}
PLANE = {"vmid": 225, "name": "plane"}


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
    return uri.get("method") in {"POST", "PUT", "DELETE"} and "/api2/json/" in str(uri.get("url", ""))


def _touches_proxmox(task):
    return "ansible.builtin.uri" in task or task.get("ansible.builtin.include_tasks") == NODE_INCLUDE


def _is_dry_run_stop(task):
    return task.get("ansible.builtin.meta") == "end_host" and "ansible_check_mode" in str(task.get("when"))


# The names every refusal message renders. A case overrides what it tests; the rest are the
# declaration the playbook would have computed, so a message cannot fail on an undefined name.
DEFAULTS = {
    "target_service": "plane",
    "target_host": "",
    "_decl_host": "plane-1",
    "_group_hosts": ["plane-1"],
    "_name": "plane",
    "_vmid": "225",
    "_dest_node": "apollo",
    "_from_node": "ariel",
    "_storage": "local-lvm",
    "_cpu": "",
    "_was_running": True,
    "_start_after": True,
    "_start_now": True,
    "_live": {"node": "ariel", "name": "plane", "type": "qemu", "status": "running"},
    "_pre": PRE_CLEAN,
    "_at_dest": {"json": {"data": {"status": "running", "lock": ""}}},
    "_cfg": {"json": {"data": {"cpu": ""}}},
}


def run_play(tmp_path, play_vars, tasks, *, extra=(), filter_path=False):
    """Run tasks as one localhost play. Returns (returncode, stdout+stderr)."""
    play_file = tmp_path / "play.yml"
    play_file.write_text(
        yaml.safe_dump(
            [
                {
                    "hosts": "localhost",
                    "connection": "local",
                    "gather_facts": False,
                    "vars": {**DEFAULTS, **play_vars},
                    "tasks": tasks,
                }
            ],
            sort_keys=False,
        )
    )
    env = {
        "ANSIBLE_NOCOLOR": "1",
        "PATH": os.environ["PATH"],
        "ANSIBLE_LOCAL_TEMP": str(tmp_path),
        "ANSIBLE_REMOTE_TEMP": str(tmp_path),
    }
    if filter_path:
        env["ANSIBLE_FILTER_PLUGINS"] = str(FILTER_DIR)
    proc = subprocess.run(
        ["ansible-playbook", "-i", "localhost,", *extra, str(play_file)],
        capture_output=True,
        text=True,
        env=env,
        stdin=subprocess.DEVNULL,
        timeout=180,
        check=False,
    )
    return proc.returncode, proc.stdout + proc.stderr


def _sentinel():
    return {"ansible.builtin.debug": {"msg": "WRITE REACHED"}}


def _echo(expr, key="RESULT="):
    return {"ansible.builtin.debug": {"msg": key + "{{ " + expr + " }}"}}


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
    (tpl,) = (t for t in templates if t["name"] == "Migrate VM")
    assert tpl["playbook"] == "platform/playbooks/migrate-vm.yml"
    assert tpl["dev_variant"] is True
    assert tpl["isolated_environment"] == "Migrate VM inputs"
    survey = {v["name"]: v for v in tpl["survey_vars"]}
    assert set(survey) == {"target_service", "target_host", "confirm_migrate", "start_after"}
    assert survey["target_service"]["required"] is True
    assert survey["target_host"]["required"] is False
    assert survey["confirm_migrate"]["required"] is True
    assert "default_value" not in survey["confirm_migrate"]
    assert survey["start_after"]["required"] is False
    assert survey["start_after"]["default_value"] == "true"


def test_the_play_defines_the_start_after_and_the_bounded_waits():
    play_vars = yaml.safe_load(PLAYBOOK.read_text())[3]["vars"]
    assert play_vars["_start_after"] == "{{ (start_after | default('true')) | bool }}"
    assert play_vars["_migrate_wait_retries"] == "{{ (migrate_wait_minutes | default(60) | int) * 6 }}"
    assert play_vars["_start_wait_retries"] == "{{ start_wait_retries | default(24) | int }}"


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


def test_every_write_follows_a_dry_run_stop_in_the_file():
    tasks = _flat()
    stops = [i for i, t in enumerate(tasks) if _is_dry_run_stop(t)]
    for index in (i for i, t in enumerate(tasks) if _is_write(t)):
        assert any(s < index for s in stops), f"{tasks[index]['name']!r} has no dry-run stop above it"


def test_the_checks_all_run_before_the_first_write_in_this_order():
    names = [t.get("name") for t in _flat()]
    order = [
        "Refuse to migrate a VM that is not the declared guest",
        "Record the source node, whether the guest is running, and whether it is already on the declared node",
        "Work out whether a start is due on the declared node, and whether one is due after the move",
        "End play when the VM is already on the declared node",
        "Read Proxmox's migration precondition for the declared destination",
        "Refuse when the VM is HA-managed (its migrate is an HA migrate that ignores targetstorage)",
        "Refuse a destination Proxmox does not allow",
        "Refuse a move that carries a local device or a CD drive other than cloud-init",
        "Read the guest's configuration on its source node (its CPU model)",
        "Refuse a live CPU model the destination node cannot provide",
        "Refuse a named CPU model the destination node cannot provide",
        "Dry run: report the planned move",
        WRITE_NAMES[1],
    ]
    positions = [names.index(n) for n in order]
    assert positions == sorted(positions), list(zip(order, positions, strict=True))


def test_the_already_there_start_sits_between_its_own_dry_run_and_the_end_play():
    names = [t.get("name") for t in _flat()]
    stop = names.index("Dry run (already there): stop this host")
    start = names.index(WRITE_NAMES[0])
    end = names.index("End play when the VM is already on the declared node")
    assert stop < start < end


def test_a_named_cpu_model_is_checked_on_the_destination_node_when_declared():
    include = find_task("Refuse a named CPU model the destination node cannot provide")
    assert include["ansible.builtin.include_tasks"] == NODE_INCLUDE
    assert include["vars"] == {"_ncm_node": "{{ _dest_node }}", "_ncm_want_cpu": "{{ _cpu }}"}
    assert include["when"] == "_cpu | length > 0"


def test_the_configured_cpu_model_is_checked_on_the_destination_when_it_differs_from_the_declared_one():
    include = find_task("Refuse a live CPU model the destination node cannot provide")
    assert include["ansible.builtin.include_tasks"] == NODE_INCLUDE
    assert include["vars"] == {"_ncm_node": "{{ _dest_node }}", "_ncm_want_cpu": "{{ _live_cpu }}"}
    assert include["when"] == "(_live_cpu | length > 0) and (_live_cpu != _cpu)"


def test_the_source_configuration_is_read_without_check_mode():
    read = find_task("Read the guest's configuration on its source node (its CPU model)")
    assert read["ansible.builtin.uri"]["url"] == (
        "{{ _pve_host }}/api2/json/nodes/{{ _from_node }}/qemu/{{ _vmid }}/config"
    )
    assert read["check_mode"] is False


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


def test_the_shutdown_only_happens_for_a_guest_that_was_running():
    assert "_was_running | bool" in find_task(WRITE_NAMES[1])["when"]


def test_the_start_after_the_move_is_due_when_it_was_running_or_start_after_is_set():
    start = find_task(WRITE_NAMES[3])
    assert "_start_now | bool" in start["when"]
    assert "_was_running | bool" not in start["when"]


def test_the_migrate_wait_is_bounded_and_retries_a_failed_poll():
    wait = find_task("Wait for the migrate task")
    assert wait["retries"] == "{{ _migrate_wait_retries }}"
    assert "is succeeded" in wait["until"]


def test_the_start_waits_are_bounded_by_start_wait_retries():
    assert find_task("Wait for the guest to be running on the declared node")["retries"] == "{{ _start_wait_retries }}"
    assert find_task("Wait for the guest to be running on its declared node after the move")["retries"] == (
        "{{ _start_wait_retries }}"
    )


def test_the_write_sequence_is_a_block_whose_rescue_recovers_before_it_fails():
    (block,) = (
        t
        for t in _flat()
        if t.get("name") == "Move the VM: shut down if running, migrate offline, start after the move when due"
    )
    child_names = [c.get("name") for c in block["block"]]
    assert child_names.index(WRITE_NAMES[2]) < child_names.index("Re-check the VM's identity after the move")
    rescue_names = [c.get("name") for c in block["rescue"]]
    assert rescue_names.index("Recovery: decide whether a start is safe") < rescue_names.index(
        "Fail with the reason, after recovering"
    )


def test_the_rescue_start_is_gated_on_the_safe_decision_and_uses_the_node_it_is_on():
    start = find_task(WRITE_NAMES[4])
    assert "_rescue_start_ok | bool" in start["when"]
    assert start["ansible.builtin.uri"]["url"] == (
        "{{ _pve_host }}/api2/json/nodes/{{ _rescue_node }}/qemu/{{ _vmid }}/status/start"
    )
    assert start["ignore_errors"] is True


def test_the_rescue_reads_the_guests_own_state_on_a_node_this_run_owns():
    read = find_task("Recovery: read the guest's own state on the node it is on")
    assert (
        read["ansible.builtin.uri"]["url"]
        == "{{ _pve_host }}/api2/json/nodes/{{ _rescue_node }}/qemu/{{ _vmid }}/status/current"
    )
    assert read["when"] == "_rescue_node in [_from_node, _dest_node]"
    status = find_task("Recovery: take the guest's state from that read")
    assert "_rescue_cur" in status["ansible.builtin.set_fact"]["_rescue_status"]


def test_the_rescue_waits_boundedly_for_a_started_guest_and_never_fails_on_that_wait_alone():
    wait = find_task("Recovery: wait, bounded, for the started guest to run")
    assert wait["failed_when"] is False
    assert wait["retries"] == "{{ _start_wait_retries }}"


def test_the_playbook_is_documented_with_its_options_and_the_cpu_is_checked_not_written():
    head = "\n".join(PLAYBOOK.read_text().splitlines()[:90])
    assert "-e target_service=plane -e confirm_migrate=225" in head
    assert "only CHECKED here, never written" in head
    for option in ("start_after", "migrate_wait_minutes", "start_wait_retries", "migrate_shutdown_timeout"):
        assert option in head, option
    assert "one of the two nodes this run owns" in head


# ── refusals and decisions, run through the playbook's own tasks ─────────────────────────────────


@needs_ansible
@pytest.mark.parametrize("confirm, ok", [("225", True), (225, True), ("999", False), ("", False)])
def test_the_run_must_name_the_declared_vmid(tmp_path, confirm, ok):
    rc, out = run_play(tmp_path, {"confirm_migrate": confirm}, [find_task("Require the run to NAME the vmid it moves")])
    assert (rc == 0) is ok, out
    if not ok:
        assert "Refusing: pass -e confirm_migrate=225" in out


@needs_ansible
@pytest.mark.parametrize(
    "live, ok",
    [
        ({"name": "plane", "type": "qemu", "node": "ariel"}, True),
        ({"name": "other", "type": "qemu", "node": "ariel"}, False),
        ({"name": "plane", "type": "lxc", "node": "ariel"}, False),
        ({"name": "plane", "node": "ariel"}, False),
    ],
)
def test_a_live_vm_that_is_not_the_declared_qemu_guest_is_refused(tmp_path, live, ok):
    rc, out = run_play(tmp_path, {"_live": live}, [find_task("Refuse to migrate a VM that is not the declared guest")])
    assert (rc == 0) is ok, out
    if not ok:
        assert "somebody else" in out


@needs_ansible
def test_an_absent_vmid_is_refused_and_is_not_read_as_a_done_move(tmp_path):
    rc, out = run_play(
        tmp_path, {"_live": {}}, [find_task("Refuse when the vmid is not in the cluster (nothing to migrate)")]
    )
    assert rc != 0 and "is not in the cluster" in out, out


@needs_ansible
@pytest.mark.parametrize(
    "node, status, start_after, converge, text",
    [
        ("apollo", "running", True, False, "Nothing to move (idempotent no-op)."),
        ("apollo", "stopped", True, True, "It is started, so it ends running there (start_after is true)."),
        (
            "apollo",
            "stopped",
            False,
            False,
            "Nothing to move; start_after is false, so the stopped guest stays stopped.",
        ),
        ("apollo", "running", False, False, "Nothing to move (idempotent no-op)."),
    ],
    ids=["running-start-after", "stopped-start-after", "stopped-no-start-after", "running-no-start-after"],
)
def test_a_vm_already_on_the_declared_node_is_started_only_when_stopped_and_start_after(
    tmp_path, node, status, start_after, converge, text
):
    tasks = [
        find_task(
            "Record the source node, whether the guest is running, and whether it is already on the declared node"
        ),
        find_task("Record the state the guest has on the declared node"),
        find_task("Work out whether a start is due on the declared node, and whether one is due after the move"),
        find_task("Already on the declared node: report what is left to do"),
        _echo("_converge | bool"),
        find_task("End play when the VM is already on the declared node"),
        _sentinel(),
    ]
    rc, out = run_play(
        tmp_path,
        {
            "_live": {"node": node, "name": "plane", "type": "qemu", "status": status},
            "_at_dest": {"json": {"data": {"status": status, "lock": ""}}},
            "_start_after": start_after,
        },
        tasks,
    )
    assert rc == 0, out
    assert _result(out) == str(converge), out
    assert text in out, out
    assert "WRITE REACHED" not in out, "the play must end on the declared node, not reach a move"


@needs_ansible
def test_a_check_run_on_a_stopped_guest_already_there_reports_the_start_and_writes_nothing(tmp_path):
    tasks = [
        find_task(
            "Record the source node, whether the guest is running, and whether it is already on the declared node"
        ),
        find_task("Record the state the guest has on the declared node"),
        find_task("Work out whether a start is due on the declared node, and whether one is due after the move"),
        find_task("Already on the declared node: report what is left to do"),
        find_task("Dry run (already there): stop this host"),
        _sentinel(),
    ]
    rc, out = run_play(
        tmp_path,
        {
            "_live": {"node": "apollo", "name": "plane", "type": "qemu", "status": "stopped"},
            "_at_dest": {"json": {"data": {"status": "stopped", "lock": ""}}},
        },
        tasks,
        extra=["--check"],
    )
    assert rc == 0, out
    assert "Check mode: it would be started" in out, out
    assert "WRITE REACHED" not in out


@needs_ansible
def test_a_locked_guest_on_the_declared_node_is_not_started(tmp_path):
    tasks = [
        find_task(
            "Record the source node, whether the guest is running, and whether it is already on the declared node"
        ),
        find_task("Record the state the guest has on the declared node"),
        find_task("Work out whether a start is due on the declared node, and whether one is due after the move"),
        find_task("Refuse to start a locked guest on the declared node"),
    ]
    rc, out = run_play(
        tmp_path,
        {
            "_live": {"node": "apollo", "name": "plane", "type": "qemu", "status": "stopped"},
            "_at_dest": {"json": {"data": {"status": "stopped", "lock": "migrate"}}},
        },
        tasks,
    )
    assert rc != 0 and "is locked on apollo (migrate), so it was not started" in out, out


@needs_ansible
def test_a_destination_proxmox_does_not_allow_is_refused_with_its_storage_and_no_header(tmp_path):
    tasks = [
        find_task("Work out whether the destination is allowed, and the reason when it is not"),
        find_task("Refuse a destination Proxmox does not allow"),
        _sentinel(),
    ]
    rc, out = run_play(tmp_path, {"_from_node": "ariel", "_pre": PRE_DENIED}, tasks)
    assert rc != 0, out
    assert "lacks the source storage id(s) local-lvm" in out, out
    assert "vm_disk_storage (local-lvm)" in out, out
    assert "Nothing was shut down" in out and "WRITE REACHED" not in out
    assert "fixture-secret-value" not in out and "PVEAPIToken" not in out


@needs_ansible
def test_a_destination_listed_as_allowed_passes(tmp_path):
    tasks = [
        find_task("Work out whether the destination is allowed, and the reason when it is not"),
        find_task("Refuse a destination Proxmox does not allow"),
        _sentinel(),
    ]
    rc, out = run_play(tmp_path, {"_pre": PRE_ALLOWED}, tasks)
    assert rc == 0 and "WRITE REACHED" in out, out


@needs_ansible
def test_a_destination_missing_from_both_lists_is_refused_as_not_allowed(tmp_path):
    pre = {"json": {"data": {"allowed_nodes": ["ariel"], "not_allowed_nodes": {}}}}
    tasks = [
        find_task("Work out whether the destination is allowed, and the reason when it is not"),
        find_task("Refuse a destination Proxmox does not allow"),
    ]
    rc, out = run_play(tmp_path, {"_dest_node": "apolo", "_pre": pre}, tasks)
    assert rc != 0 and "not listed as allowed" in out, out


@needs_ansible
@pytest.mark.parametrize(
    "live, pre, ok",
    [
        ({"name": "plane", "hastate": "started"}, PRE_CLEAN, False),
        (
            {"name": "plane", "hastate": ""},
            {"json": {"data": {"allowed_nodes": ["apollo"], "dependent-ha-resources": []}}},
            False,
        ),
        ({"name": "plane", "hastate": ""}, PRE_CLEAN, True),
    ],
    ids=["hastate-set", "precondition-reports-ha", "not-ha"],
)
def test_an_ha_managed_vm_is_refused_before_any_shutdown(tmp_path, live, pre, ok):
    rc, out = run_play(
        tmp_path,
        {"_live": live, "_pre": pre},
        [find_task("Refuse when the VM is HA-managed (its migrate is an HA migrate that ignores targetstorage)")],
    )
    assert (rc == 0) is ok, out
    if not ok:
        assert "Remove it from HA first" in out, out


@needs_ansible
@pytest.mark.parametrize(
    "pre_data, ok, named",
    [
        ({"local_resources": ["usb0"], "local_disks": []}, False, "usb0"),
        (
            {"local_resources": [], "local_disks": [{"volid": "local:iso/tools.iso", "cdrom": True, "size": 1}]},
            False,
            "local:iso/tools.iso",
        ),
        ({"local_resources": [], "local_disks": [{"volid": "local-lvm:vm-225-cloudinit", "cdrom": True}]}, True, None),
        (
            {"local_resources": [], "local_disks": [{"volid": "local-lvm:vm-225-disk-0", "cdrom": False, "size": 1}]},
            True,
            None,
        ),
        ({}, True, None),
    ],
    ids=["local-device", "non-cloudinit-cdrom", "cloudinit-drive-is-carried-by-the-move", "data-disk", "nothing"],
)
def test_a_local_device_or_a_non_cloud_init_cd_drive_is_refused_before_shutdown(tmp_path, pre_data, ok, named):
    tasks = [
        find_task("Work out what on the source blocks an offline move"),
        find_task("Refuse a move that carries a local device or a CD drive other than cloud-init"),
        _sentinel(),
    ]
    rc, out = run_play(tmp_path, {"_pre": {"json": {"data": {"allowed_nodes": ["apollo"], **pre_data}}}}, tasks)
    assert (rc == 0) is ok, out
    if ok:
        assert "WRITE REACHED" in out, out
    else:
        assert named in out and "Nothing was shut down or moved" in out, out


@needs_ansible
@pytest.mark.parametrize(
    "cpu, live",
    [
        ("", ""),
        ("x86-64-v3", "x86-64-v3"),
        ("cputype=host,flags=+aes", "host"),
        ("x86-64-v2-AES,flags=+aes;-md-clear", "x86-64-v2-AES"),
        ("flags=+aes,cputype=x86-64-v3", "x86-64-v3"),
    ],
)
def test_the_configured_cpu_model_is_parsed_the_way_resize_parses_it(tmp_path, cpu, live):
    tasks = [
        find_task("Split the configured CPU property string"),
        find_task("Work out the CPU model the guest is configured with"),
        _echo("_live_cpu"),
    ]
    rc, out = run_play(tmp_path, {"_cfg": {"json": {"data": {"cpu": cpu}}}}, tasks)
    assert rc == 0, out
    assert _result(out) == live, out


@needs_ansible
@pytest.mark.parametrize(
    "live_cpu, declared, include",
    [
        ("x86-64-v3", "", True),
        ("host", "", True),
        ("x86-64-v3", "x86-64-v3", False),
        ("", "", False),
    ],
)
def test_the_live_model_is_checked_only_when_it_is_set_and_differs_from_the_declared_one(
    tmp_path, live_cpu, declared, include
):
    # The playbook's own gating expression, run as written. The node check itself (a status read on
    # the destination) is assert-node-cpu-model.yml's and needs a live node, so it is pinned by the
    # structural test above rather than run here.
    when = find_task("Refuse a live CPU model the destination node cannot provide")["when"]
    rc, out = run_play(
        tmp_path,
        {"_live_cpu": live_cpu, "_cpu": declared},
        [{"ansible.builtin.debug": {"msg": "INCLUDE"}, "when": when}],
    )
    assert rc == 0, out
    assert ("INCLUDE" in out) is include, out


@needs_ansible
@pytest.mark.parametrize("value", ["", "x86-64-v3", "x86-64-v2-AES", "host", "custom-lab_1.0"])
def test_a_bare_cpu_model_name_passes_the_format_check(tmp_path, value):
    rc, out = run_play(tmp_path, {"_cpu": value}, [find_task("Refuse a vm_cpu that is not a bare model name")])
    assert rc == 0, out


@needs_ansible
@pytest.mark.parametrize("value", ["x86-64-v3,flags=+aes", "host,hidden=1", "x86=3", "x86 64", "-x86", "x86-64-v3;x"])
def test_a_cpu_value_that_could_carry_extra_keys_is_refused(tmp_path, value):
    rc, out = run_play(tmp_path, {"_cpu": value}, [find_task("Refuse a vm_cpu that is not a bare model name")])
    assert rc != 0 and "Nothing was moved" in out, out


@needs_ansible
@pytest.mark.parametrize(
    "vmid, name, node, storage, ok",
    [
        ("225", "plane", "apollo", "local-lvm", True),
        ("", "plane", "apollo", "local-lvm", False),
        ("225", "", "apollo", "local-lvm", False),
        ("225", "plane", "", "local-lvm", False),
        ("225", "plane", "apollo", "", False),
    ],
)
def test_a_declaration_missing_any_identity_or_destination_field_is_refused(tmp_path, vmid, name, node, storage, ok):
    rc, out = run_play(
        tmp_path,
        {"_vmid": vmid, "_name": name, "_dest_node": node, "_storage": storage},
        [find_task("Require a complete declaration (vm_vmid, vm_name, vm_node, vm_disk_storage)")],
    )
    assert (rc == 0) is ok, out
    if not ok:
        assert "lacks vm_vmid, vm_name, vm_node or vm_disk_storage" in out, out


@needs_ansible
@pytest.mark.parametrize(
    "group, target_host, decl_host, ok",
    [
        (["plane-1"], "", "plane-1", True),
        (["plane-1", "plane-2"], "", "plane-1", False),
        (["plane-1", "plane-2"], "plane-2", "plane-2", True),
        (["plane-1", "plane-2"], "other-svc", "other-svc", False),
    ],
)
def test_target_host_must_name_one_host_of_the_service_when_it_has_several(tmp_path, group, target_host, decl_host, ok):
    rc, out = run_play(
        tmp_path,
        {"_group_hosts": group, "target_host": target_host, "_decl_host": decl_host},
        [find_task("Require an explicit target_host when the service declares more than one")],
    )
    assert (rc == 0) is ok, out
    if not ok:
        assert "pass -e target_host=<one of those names>" in out, out


@needs_ansible
@pytest.mark.parametrize("exitstatus, ok", [("OK", True), ("migration aborted", False)])
def test_the_move_fails_unless_the_migrate_task_reports_ok(tmp_path, exitstatus, ok):
    task = {"json": {"data": {"status": "stopped", "exitstatus": exitstatus}}}
    rc, out = run_play(tmp_path, {"_migrate_task": task}, [find_task("Verify the migrate task succeeded")])
    assert (rc == 0) is ok, out
    if not ok:
        assert "Migrate to apollo failed: task exit status migration aborted" in out, out


@needs_ansible
@pytest.mark.parametrize(
    "name, node, ok",
    [
        ("plane", "apollo", True),
        ("other", "apollo", False),
        ("plane", "ariel", False),
    ],
)
def test_the_identity_is_rechecked_on_the_declared_node_after_the_move(tmp_path, name, node, ok):
    placed = {"json": {"data": [{"vmid": 225, "name": name, "node": node}]}}
    rc, out = run_play(tmp_path, {"_placed": placed}, [find_task("Re-check the VM's identity after the move")])
    assert (rc == 0) is ok, out
    if not ok:
        assert "after the move" in out, out


# The rescue's decision, as the playbook's own tasks.
RESCUE_DECIDE = (
    "Recovery: work out where the VM is",
    "Recovery: read its node and lock",
    "Recovery: take the guest's state from that read",
    "Recovery: record whether this run delivered a shutdown request",
    "Recovery: decide whether a start is safe",
)


def _rec(node="ariel", **fields):
    return {"json": {"data": [{**PLANE, "node": node, **fields}]}}


@needs_ansible
@pytest.mark.parametrize(
    "live, cur, was_running, expect",
    [
        (_rec("ariel", status="stopped"), {"json": {"data": {"status": "stopped"}}}, True, True),
        (_rec("apollo", status="stopped"), {"json": {"data": {"status": "stopped"}}}, True, True),
        (_rec("ariel", status="running"), {"json": {"data": {"status": "running"}}}, True, False),
        (_rec("ariel", status="stopped"), {"json": {"data": {"status": "running"}}}, True, False),
        (_rec("ariel", status="stopped"), {}, True, False),
        (_rec("ariel", status="stopped"), {"json": {"data": {"status": "stopped"}}}, False, False),
        (_rec("pluto", status="stopped"), {"json": {"data": {"status": "stopped"}}}, True, False),
        (_rec("ariel", status="stopped", lock="migrate"), {"json": {"data": {"status": "stopped"}}}, True, False),
        (_rec("ariel", status="stopped", name="other"), {"json": {"data": {"status": "stopped"}}}, True, False),
        ({}, {"json": {"data": {"status": "stopped"}}}, True, False),
    ],
    ids=[
        "started-back-on-source",
        "started-back-on-destination",
        "still-running",
        "status-read-says-running",
        "status-unreadable",
        "not-running-before",
        "other-node",
        "locked",
        "name-changed",
        "record-unreadable",
    ],
)
def test_the_rescue_starts_a_guest_only_when_it_was_running_and_is_stopped_on_a_node_this_run_owns(
    tmp_path, live, cur, was_running, expect
):
    tasks = [find_task(n) for n in RESCUE_DECIDE] + [_echo("_rescue_start_ok | bool")]
    rc, out = run_play(tmp_path, {"_rescue_live": live, "_rescue_cur": cur, "_was_running": was_running}, tasks)
    assert rc == 0, out
    assert _result(out) == str(expect), out


@needs_ansible
@pytest.mark.parametrize(
    "start_ok, start, wait_state, was_running, node, status, locked, shutdown, text",
    [
        (
            True,
            {},
            "running",
            True,
            "ariel",
            "stopped",
            False,
            False,
            "this run started it there again and it is running. The move did not happen.",
        ),
        (
            True,
            {},
            "running",
            True,
            "apollo",
            "stopped",
            False,
            False,
            "The move itself completed; only the start after it had failed.",
        ),
        (
            True,
            {"failed": True, "msg": "boom"},
            "stopped",
            True,
            "ariel",
            "stopped",
            False,
            False,
            "Starting it on ariel FAILED (boom): start it by hand.",
        ),
        (
            True,
            {},
            "stopped",
            True,
            "ariel",
            "stopped",
            False,
            False,
            "reports 'stopped' and is not running yet: check it by hand.",
        ),
        (
            False,
            {"skipped": True},
            "not read",
            False,
            "ariel",
            "stopped",
            False,
            False,
            "It was not running before this run, so nothing was started.",
        ),
        (
            False,
            {"skipped": True},
            "not read",
            True,
            "pluto",
            "running",
            False,
            False,
            "It is on pluto, neither ariel nor apollo: nothing was started.",
        ),
        (
            False,
            {"skipped": True},
            "not read",
            True,
            "ariel",
            "running",
            False,
            True,
            "A shutdown request was delivered and may still be acted on by the guest; check its state on ariel.",
        ),
        (
            False,
            {"skipped": True},
            "not read",
            True,
            "ariel",
            "running",
            False,
            False,
            "It is running, not stopped, so no start was issued.",
        ),
        (
            False,
            {"skipped": True},
            "not read",
            True,
            "ariel",
            "stopped",
            True,
            False,
            "It is still locked, so no start was issued.",
        ),
    ],
    ids=[
        "started-running-source",
        "started-running-destination",
        "start-failed",
        "start-not-running",
        "not-running-before",
        "other-node",
        "shutdown-may-still-act",
        "running-no-shutdown",
        "locked",
    ],
)
def test_the_failure_note_says_where_the_guest_is_and_what_was_done(
    tmp_path, start_ok, start, wait_state, was_running, node, status, locked, shutdown, text
):
    tasks = [find_task("Recovery: say what was done and what is left to check"), _echo("_start_note")]
    rc, out = run_play(
        tmp_path,
        {
            "_rescue_start_ok": start_ok,
            "_rescue_start": start,
            "_rescue_wait_state": wait_state,
            "_was_running": was_running,
            "_rescue_node": node,
            "_rescue_status": status,
            "_rescue_locked": locked,
            "_shutdown_sent": shutdown,
            "_from_node": "ariel",
            "_dest_node": "apollo",
        },
        tasks,
    )
    assert rc == 0, out
    assert text in out, out


@needs_ansible
def test_the_dry_run_reports_the_plan_and_changes_nothing(tmp_path):
    tasks = [find_task("Dry run: report the planned move"), find_task("Dry run: stop this host"), _sentinel()]
    rc, out = run_play(
        tmp_path,
        {
            "_name": "plane",
            "_vmid": "225",
            "_from_node": "ariel",
            "_dest_node": "apollo",
            "_storage": "local-lvm",
            "_was_running": True,
            "_start_now": True,
        },
        tasks,
        extra=["--check"],
    )
    assert rc == 0, out
    assert "would be shut down, moved offline to apollo (disk storage local-lvm), then started on apollo" in out
    assert "Nothing was changed." in out
    assert "WRITE REACHED" not in out


@needs_ansible
@pytest.mark.parametrize(
    "was_running, start_after, start_now",
    [
        (True, True, True),
        (False, True, True),
        (True, False, True),
        (False, False, False),
    ],
)
def test_a_start_after_the_move_is_due_when_it_was_running_or_start_after_is_set(
    tmp_path, was_running, start_after, start_now
):
    tasks = [
        find_task("Work out whether a start is due on the declared node, and whether one is due after the move"),
        _echo("_start_now | bool"),
    ]
    rc, out = run_play(
        tmp_path,
        {"_was_running": was_running, "_start_after": start_after, "_already_there": False, "_dest_state": "unknown"},
        tasks,
    )
    assert rc == 0, out
    assert _result(out) == str(start_now), out


@needs_ansible
@pytest.mark.parametrize(
    "shutdown, expect",
    [
        ({"json": {"data": "UPID:ariel:0001:shutdown"}}, True),
        ({"skipped": True}, False),
        ({"failed": True, "msg": "refused"}, False),
        (None, False),
    ],
)
def test_a_shutdown_counts_as_sent_only_when_the_request_was_accepted(tmp_path, shutdown, expect):
    tasks = [
        find_task("Recovery: record whether this run delivered a shutdown request"),
        _echo("_shutdown_sent | bool"),
    ]
    play_vars = {} if shutdown is None else {"_shutdown": shutdown}
    rc, out = run_play(tmp_path, play_vars, tasks)
    assert rc == 0, out
    assert _result(out) == str(expect), out


@needs_ansible
@pytest.mark.parametrize(
    "was_running, start_now, text",
    [
        (True, True, "It was running, so it was shut down for the move and started again on apollo."),
        (False, True, "It was stopped and was started on apollo (start_after is true)."),
        (False, False, "It was stopped before the move and stays stopped (start_after is false)."),
    ],
)
def test_the_report_says_what_became_of_the_guest(tmp_path, was_running, start_now, text):
    rc, out = run_play(
        tmp_path, {"_was_running": was_running, "_start_now": start_now, "_cpu": "x86-64-v3"}, [find_task("Report")]
    )
    assert rc == 0, out
    assert text in out, out
    assert "Run Resize VM to converge vm_cpu (declared: x86-64-v3)" in out


@needs_ansible
def test_the_playbook_passes_ansible_syntax_check():
    proc = subprocess.run(
        ["ansible-playbook", "-i", "localhost,", "--syntax-check", str(PLAYBOOK)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={"ANSIBLE_NOCOLOR": "1", "PATH": os.environ["PATH"]},
        stdin=subprocess.DEVNULL,
        timeout=180,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


# The identity guard: refused when a forged value reaches it, passed when nothing is forged.
FORGED_IDENTITY = [
    pytest.param(f.values[0], id=f"{name}-{f.id}")
    for name, honest, forged in (
        ("vm_vmid", "225", "999"),
        ("vm_name", "plane", "other"),
        ("vm_node", "apollo", "ariel"),
        ("vm_disk_storage", "local-lvm", "local"),
        ("vm_cpu", "x86-64-v3", "host"),
    )
    for f in forgeries.templated_forgeries(name, honest, forged)
]


@needs_ansible
def test_the_identity_guard_passes_a_run_that_declares_its_own_identity(tmp_path):
    rc, out = run_play(
        tmp_path, {}, [find_task("Refuse a launch that overrides the declared identity")], filter_path=True
    )
    assert rc == 0, out


@needs_ansible
@pytest.mark.parametrize("forge", FORGED_IDENTITY)
def test_a_forged_declared_identity_value_is_refused(tmp_path, forge):
    rc, out = run_play(
        tmp_path,
        {},
        [find_task("Refuse a launch that overrides the declared identity")],
        extra=["-e", forge(tmp_path)],
        filter_path=True,
    )
    assert rc != 0 and "set as an extra var" in out, out


# ── the offline move's shape, pinned in the playbook's own text ──────────────────────────────────


def test_the_already_there_guard_and_start_are_structurally_gated_on_converge():
    guard = find_task("Refresh reserved-target guard before starting the guest on the declared node")
    start = find_task(WRITE_NAMES[0])
    assert "_converge | bool" in guard["when"]
    assert "_converge | bool" in start["when"]
    assert (
        start["ansible.builtin.uri"]["url"]
        == "{{ _pve_host }}/api2/json/nodes/{{ _dest_node }}/qemu/{{ _vmid }}/status/start"
    )
    wait = find_task("Wait for the guest to be running on the declared node")
    assert wait["retries"] == "{{ _start_wait_retries }}"
