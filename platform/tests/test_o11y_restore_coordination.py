"""Regression coverage for the versioned isolated-restore action journal."""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parents[1] / "playbooks" / "files"))
sys.path.insert(0, str(Path(__file__).parent))
import harness_sandbox  # noqa: E402
import o11y_pbs_receipts as pbs_receipts  # noqa: E402
import o11y_restore_coordination as coordination  # noqa: E402
import playbook_yaml  # noqa: E402

TARGET = {"node": "restore-node", "vmid": 910001}
DECLARATION = {
    "declaration_fingerprint": "1" * 64,
    "target_fingerprint": coordination.target_identity_fingerprint(TARGET),
    "artifact_fingerprint": "3" * 64,
    "public_sha": "a" * 40,
    "private_inventory_sha": "b" * 64,
}
RESTORE_UPID = "UPID:restore-node:00000001:00000002:00000003:qmrestore:910001:root@pam:"
START_UPID = "UPID:restore-node:00000004:00000005:00000006:qmstart:910001:root@pam:"
STOP_UPID = "UPID:restore-node:00000007:00000008:00000009:qmstop:910001:root@pam:"
DELETE_UPID = "UPID:restore-node:0000000a:0000000b:0000000c:qmdestroy:910001:root@pam:"


def request(transition, snapshot=None, *, version=0, **changes):
    values = {
        "transition": transition,
        "declaration": DECLARATION,
        "target_ref": TARGET,
        "expected_record": copy.deepcopy(snapshot),
        "expected_version": version,
        "nonce": None if snapshot is None else snapshot["active_nonce"] or snapshot["restore_nonce"],
        "evidence": None,
        "action": None,
        "request_fingerprint": None,
        "handle_type": None,
        "handle": None,
        "effect_fingerprint": None,
    }
    values.update(changes)
    return values


def apply(snapshot, version, transition, **changes):
    planned = coordination.plan_transition(snapshot, version, request(transition, snapshot, version=version, **changes))
    return planned["record"], planned["expected_version"]


def reserve():
    planned = coordination.plan_transition(None, 0, request("reserve"))
    return planned["record"], 1


def action(snapshot, version, name, handle_type, handle, index):
    req_hash = str(index) * 64
    snapshot, version = apply(
        snapshot,
        version,
        "intent_restore" if name == "restore" else "intent_action",
        action=name,
        request_fingerprint=req_hash,
    )
    snapshot, version = apply(
        snapshot,
        version,
        "record_handle",
        action=name,
        request_fingerprint=req_hash,
        handle_type=handle_type,
        handle=handle,
    )
    isolated_config = "f" * 64
    if name == "restore":
        evidence = {
            "type": "restore_effect/v2",
            "action": name,
            "node": TARGET["node"],
            "vmid": TARGET["vmid"],
            "task_state": "stopped",
            "exit_status": "OK",
            "vm_state": "stopped",
            "config_sha256": "1" * 64,
        }
        effect_hash = evidence["config_sha256"]
    elif name == "isolate":
        evidence = {
            "type": "isolation_effect/v2",
            "action": name,
            "node": TARGET["node"],
            "vmid": TARGET["vmid"],
            "status": 200,
            "isolated": True,
            "nic_count": 0,
            "onboot": 0,
            "config_sha256": isolated_config,
        }
        effect_hash = isolated_config
    elif name == "start":
        evidence = {
            "type": "start_effect/v2",
            "action": name,
            "node": TARGET["node"],
            "vmid": TARGET["vmid"],
            "task_state": "stopped",
            "exit_status": "OK",
            "vm_state": "running",
            "isolated": True,
            "config_sha256": isolated_config,
        }
        effect_hash = isolated_config
    elif name == "guest_exec":
        evidence = {
            "type": "guest_effect/v2",
            "action": name,
            "node": TARGET["node"],
            "vmid": TARGET["vmid"],
            "pid": handle,
            "exit_code": 0,
            "checker_status": "verified",
            "checker_receipt_fingerprint": "5" * 64,
            "output_sha256": "6" * 64,
        }
        effect_hash = evidence["checker_receipt_fingerprint"]
    elif name == "stop":
        evidence = {
            "type": "stop_effect/v2",
            "action": name,
            "node": TARGET["node"],
            "vmid": TARGET["vmid"],
            "task_state": "stopped",
            "exit_status": "OK",
            "vm_state": "stopped",
            "isolated": True,
            "config_sha256": isolated_config,
        }
        effect_hash = isolated_config
    else:
        evidence = {
            "type": "delete_effect/v2",
            "action": name,
            "node": TARGET["node"],
            "vmid": TARGET["vmid"],
            "task_state": "stopped",
            "exit_status": "OK",
            "task_output_sha256": "8" * 64,
        }
        effect_hash = evidence["task_output_sha256"]
    snapshot, version = apply(
        snapshot,
        version,
        "verify_effect",
        action=name,
        request_fingerprint=req_hash,
        handle_type=handle_type,
        handle=handle,
        effect_fingerprint=effect_hash,
        evidence=evidence,
    )
    return snapshot, version


def active_restore():
    record, version = reserve()
    # The initial restore intent is the only transition allowed from reserved.
    record, version = action(record, version, "restore", "upid", RESTORE_UPID, 1)
    for name, kind, handle, marker in (
        ("isolate", "response_sha256", "2" * 64, 2),
        ("start", "upid", START_UPID, 3),
        ("guest_exec", "qga_pid", 44, 4),
        ("stop", "upid", STOP_UPID, 5),
    ):
        record, version = action(record, version, name, kind, handle, marker)
    return record, version


def completed_restore():
    record, version = active_restore()
    data_checks = {
        "schema_version": 1,
        "status": "verified",
        "reason": "restored_historical_data_verified",
        "volume_count": 5,
        "volume_bindings_sha256": "4" * 64,
        "container_bindings_sha256": "5" * 64,
        "backend_count": 4,
        "grafana_integrity": "ok",
        "grafana_migrations": 3,
        "grafana_database_sha256": "6" * 64,
        "backends": {
            name: {
                "expectation": "present",
                "historical_records": 1,
                "evidence_sha256": "7" * 64,
                "readback_sha256": "8" * 64,
            }
            for name in ("prometheus", "loki", "tempo", "pyroscope")
        },
        "receipt_fingerprint": "9" * 64,
    }
    nested_record = copy.deepcopy(record)
    nested_record.update(phase="awaiting_receipt", active_nonce=None)
    evidence = {
        "type": "restore_receipt/v3",
        "bindings": {
            key: record[key]
            for key in (
                "operation_id",
                "target_fingerprint",
                "artifact_fingerprint",
                "restore_nonce",
                "public_sha",
                "private_inventory_sha",
            )
        },
        "restore_upid": RESTORE_UPID,
        "target_state": "stopped",
        "pending_action": None,
        "completed_actions": ["restore", "isolate", "start", "guest_exec", "stop"],
        "coordination_record": nested_record,
        "coordination_version": version + 1,
        "artifact_config": {
            "config_sha256": "a" * 64,
            "disk_count": 1,
            "disks": [{"device": "scsi0", "size_bytes": 20 * 1024**3}],
            "nic_count": 1,
            "nic_keys": ["net0"],
            "config_keys_sha256": "b" * 64,
        },
        "checker_sha256": "c" * 64,
        "checker_receipt_fingerprint": data_checks["receipt_fingerprint"],
        "guest_execution_pid": 44,
        "data_checks": data_checks,
        "isolation_config_sha256": "d" * 64,
    }
    evidence = pbs_receipts.seal_restore_receipt(evidence)
    record, version = apply(record, version, "awaiting_receipt", evidence=evidence)
    return record, version


def authenticated_task(record, version):
    bindings = {
        key: record[key]
        for key in (
            "operation_id",
            "target_fingerprint",
            "artifact_fingerprint",
            "restore_nonce",
            "public_sha",
            "private_inventory_sha",
        )
    }
    bindings["restore_upid"] = RESTORE_UPID
    return {
        "type": "prior_restore_task_proof/v2",
        "task_id": 4182,
        "task_status": "success",
        "output_fingerprint": "7" * 64,
        "receipt_fingerprint": record["receipt_fingerprint"],
        "bindings": bindings,
        "coordination_record": copy.deepcopy(record),
        "coordination_version": version,
    }


def test_every_lifecycle_write_has_intent_real_handle_and_verified_effect():
    record, version = completed_restore()
    assert record["schema_version"] == 2
    assert record["phase"] == "awaiting_receipt"
    assert record["restore_upid"] == RESTORE_UPID
    assert [event["action"] for event in record["journal"]] == ["restore", "isolate", "start", "guest_exec", "stop"]
    assert record["journal"][3]["handle_type"] == "qga_pid"
    assert record["journal"][1]["handle_type"] == "response_sha256"
    assert record["pending"] is None
    assert version == 17


def test_restore_receipt_builder_seals_both_fingerprints_from_stopped_journal():
    record, version = reserve()
    for name, kind, handle, marker in (
        ("restore", "upid", RESTORE_UPID, 1),
        ("isolate", "response_sha256", "2" * 64, 2),
        ("start", "upid", START_UPID, 3),
        ("guest_exec", "qga_pid", 44, 4),
        ("stop", "upid", STOP_UPID, 5),
    ):
        record, version = action(record, version, name, kind, handle, marker)
    _final, _version = completed_restore()
    nested = _final["journal"]
    data_checks = {
        "schema_version": 1,
        "status": "verified",
        "reason": "restored_historical_data_verified",
        "volume_count": 5,
        "volume_bindings_sha256": "4" * 64,
        "container_bindings_sha256": "5" * 64,
        "backend_count": 4,
        "grafana_integrity": "ok",
        "grafana_migrations": 3,
        "grafana_database_sha256": "6" * 64,
        "backends": {
            name: {
                "expectation": "present",
                "historical_records": 1,
                "evidence_sha256": "7" * 64,
                "readback_sha256": "8" * 64,
            }
            for name in ("prometheus", "loki", "tempo", "pyroscope")
        },
        "receipt_fingerprint": "9" * 64,
    }
    receipt = coordination.build_restore_receipt(
        record,
        version,
        {
            "artifact_config": {
                "config_sha256": "a" * 64,
                "disk_count": 1,
                "disks": [{"device": "scsi0", "size_bytes": 20 * 1024**3}],
                "nic_count": 1,
                "nic_keys": ["net0"],
                "config_keys_sha256": "b" * 64,
            },
            "checker_sha256": "c" * 64,
            "checker_receipt_fingerprint": data_checks["receipt_fingerprint"],
            "guest_execution_pid": 44,
            "data_checks": data_checks,
            "isolation_config_sha256": "d" * 64,
        },
    )
    assert receipt["receipt_fingerprint"] == receipt["coordination_record"]["receipt_fingerprint"]
    assert receipt["coordination_record"]["journal"][-1] == nested[-1]
    restored, _ = apply(record, version, "awaiting_receipt", evidence=receipt)
    assert restored["receipt_fingerprint"] == receipt["receipt_fingerprint"]


@pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")
def test_real_restore_receipt_builder_gates_cleanup_on_hash_bound_target_config(tmp_path):
    repo = playbook_yaml.REPO
    playbooks = repo / "platform/playbooks"
    restore_tasks = list(playbook_yaml.tasks(playbook_yaml.load(playbooks / "restore-o11y-isolated.yml")))
    cleanup_tasks = list(playbook_yaml.tasks(playbook_yaml.load(playbooks / "cleanup-o11y-isolated-restore.yml")))
    receipt_task = copy.deepcopy(
        next(
            task
            for task in restore_tasks
            if task.get("name") == "Build the private identity-bearing v3 restore receipt"
        )
    )
    receipt_guard = copy.deepcopy(
        next(
            task
            for task in restore_tasks
            if task.get("name") == "Require the full stopped journal to seal one closed private receipt"
        )
    )
    cleanup_target = copy.deepcopy(
        next(
            task
            for task in cleanup_tasks
            if task.get("name") == "Validate stopped isolated target configuration and exact source disks"
        )
    )
    cleanup_guard = copy.deepcopy(
        next(
            task
            for task in cleanup_tasks
            if task.get("name") == "Require stopped target and unchanged isolation fingerprint before CAS transfer"
        )
    )
    receipt_task["ansible.builtin.command"]["argv"][1] = str(playbooks / "files/o11y_restore_coordination.py")
    cleanup_target["ansible.builtin.command"]["argv"][1] = str(playbooks / "files/o11y_isolated_restore.py")

    artifact_config = {
        "config_sha256": "a" * 64,
        "disk_count": 1,
        "disks": [{"device": "scsi0", "size_bytes": 20 * 1024**3}],
        "nic_count": 1,
        "nic_keys": ["net0"],
        "config_keys_sha256": "b" * 64,
    }
    checker_sha = "c" * 64
    data_checks = {
        "schema_version": 1,
        "status": "verified",
        "reason": "restored_historical_data_verified",
        "volume_count": 5,
        "volume_bindings_sha256": "4" * 64,
        "container_bindings_sha256": "5" * 64,
        "backend_count": 4,
        "grafana_integrity": "ok",
        "grafana_migrations": 3,
        "grafana_database_sha256": "6" * 64,
        "backends": {
            name: {
                "expectation": "present",
                "historical_records": 1,
                "evidence_sha256": "7" * 64,
                "readback_sha256": "8" * 64,
            }
            for name in ("prometheus", "loki", "tempo", "pyroscope")
        },
        "receipt_fingerprint": "9" * 64,
    }

    def config(ide2: str | None = "target-lvm:vm-910001-cloudinit,media=cdrom", **changes):
        data = {
            "vmid": 910001,
            "digest": "a" * 40,
            "onboot": 0,
            "description": f"o11y-restore:{DECLARATION['declaration_fingerprint']}",
            "scsi0": "target-lvm:vm-910001-disk-0,size=20G",
        }
        if ide2 is not None:
            data["ide2"] = ide2
        data.update(changes)
        return {"status": 200, "data": data}

    def fingerprint(candidate):
        return hashlib.sha256(json.dumps(candidate["data"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    base_config = config()
    cases = [
        ("backed auxiliary", base_config, base_config, None, False, False),
        ("explicit empty auxiliary", config("none,media=cdrom"), config("none,media=cdrom"), None, False, False),
        (
            "config changed after receipt",
            base_config,
            config("target-lvm:vm-910002-cloudinit,media=cdrom"),
            None,
            False,
            True,
        ),
        ("wrong receipt hash", base_config, base_config, "f" * 64, False, True),
        (
            "wrong target auxiliary",
            config("target-lvm:vm-910002-cloudinit,media=cdrom"),
            config("target-lvm:vm-910002-cloudinit,media=cdrom"),
            None,
            False,
            True,
        ),
        (
            "wrong included disk store",
            config(scsi0="other-lvm:vm-910001-disk-0,size=20G"),
            config(scsi0="other-lvm:vm-910001-disk-0,size=20G"),
            None,
            False,
            True,
        ),
        (
            "unexpected disk",
            config(scsi1="target-lvm:vm-910001-disk-1,size=20G"),
            config(scsi1="target-lvm:vm-910001-disk-1,size=20G"),
            None,
            False,
            True,
        ),
        (
            "wrong target ownership marker",
            config(description="o11y-restore:wrong"),
            config(description="o11y-restore:wrong"),
            None,
            False,
            True,
        ),
        ("incomplete lifecycle journal", base_config, base_config, None, True, True),
    ]

    for index, (label, sealed_config, current_config, expected_hash, wrong_journal, should_refuse) in enumerate(cases):
        record, version = active_restore()
        if wrong_journal:
            record["journal"] = record["journal"][:-1]
        bound_hash = expected_hash or fingerprint(sealed_config)
        variables = {
            "_o11y_rc_plan": {"record": record, "expected_version": version},
            "_restore_artifact_layout": artifact_config,
            "_restore_checker_sha": checker_sha,
            "_restore_guest_receipt": data_checks,
            "_restore_guest_exec_submit": {"json": {"data": {"pid": 44}}},
            "_restore_isolated_config_sha256": bound_hash,
            "_cleanup_target_read": {
                "results": [
                    {"item": {"name": "config"}, "status": 200, "json": {"data": current_config["data"]}},
                    {"item": {"name": "status"}, "status": 200, "json": {"data": {"status": "stopped"}}},
                ]
            },
            "_cleanup_target_ref": {"vmid": 910001, "node": TARGET["node"]},
            "_cleanup_target_declaration": {"storage_id": "target-lvm"},
        }
        tasks = [
            receipt_task,
            receipt_guard,
            {
                "name": "Pass only the sealed receipt to the actual cleanup task",
                "ansible.builtin.set_fact": {
                    "_cleanup_receipt": "{{ _restore_sealed_receipt_raw.stdout | from_json }}",
                    "_cleanup_record": "{{ (_restore_sealed_receipt_raw.stdout | from_json).coordination_record }}",
                },
            },
            cleanup_target,
            {
                "name": "Report only cleanup validator status",
                "ansible.builtin.debug": {"msg": "cleanup_target_config_rc={{ _cleanup_config_validated.rc }}"},
            },
            cleanup_guard,
            {"name": "Synthetic deletion gate sentinel", "ansible.builtin.debug": {"msg": "DELETE_GATE_REACHED"}},
        ]
        play = [{"hosts": "localhost", "connection": "local", "gather_facts": False, "vars": variables, "tasks": tasks}]
        case_dir = tmp_path / str(index)
        case_dir.mkdir()
        inventory = case_dir / "inventory.ini"
        inventory.write_text("localhost ansible_connection=local\n")
        harness = case_dir / "receipt-to-cleanup.yml"
        harness.write_text(yaml.safe_dump(play))
        result = harness_sandbox.run(
            ["ansible-playbook", "-i", str(inventory), str(harness)],
            case_dir,
            cwd=repo,
            env=harness_sandbox.env_for(case_dir),
        )
        if should_refuse:
            assert result.returncode != 0, label
            assert "DELETE_GATE_REACHED" not in result.stdout, label
        else:
            assert result.returncode == 0, f"{label}: {result.stdout}\n{result.stderr}"
            assert "cleanup_target_config_rc=0" in result.stdout, label
            assert "DELETE_GATE_REACHED" in result.stdout, label

    cleanup_positions = {
        task.get("name"): index
        for index, task in enumerate(cleanup_tasks)
        if task.get("name") in {
            "Require stopped target and unchanged isolation fingerprint before CAS transfer",
            "Transfer ownership and persist delete intent with exact journal CAS",
            "Delete only the authenticated disconnected target",
        }
    }
    assert (
        cleanup_positions["Require stopped target and unchanged isolation fingerprint before CAS transfer"]
        < cleanup_positions["Transfer ownership and persist delete intent with exact journal CAS"]
        < cleanup_positions["Delete only the authenticated disconnected target"]
    )


def test_new_reservation_is_version_cas_zero_and_record_readback_is_exact():
    plan = coordination.plan_transition(None, 0, request("reserve"))
    assert plan["payload"]["options"] == {"cas": 0}
    assert plan["record"]["schema_version"] == 2
    secret = {"data": {"data": plan["record"], "metadata": {"version": 1}}}
    metadata = {"current_version": 1, "versions": {"1": {"deletion_time": "", "destroyed": False}}}
    assert coordination.verify_readback(plan, 200, metadata, 200, secret) == {
        "result": "verified",
        "phase": "reserved",
        "version": 1,
    }


@pytest.mark.parametrize(
    "bad",
    [
        lambda record: record.update(schema_version=1),
        lambda record: record.update(active_nonce="0" * 64),
        lambda record: record.update(pending={"action": "restore"}),
        lambda record: record.update(journal=[{"action": "start"}]),
    ],
)
def test_malformed_or_legacy_records_are_refused_without_implicit_migration(bad):
    record, _ = reserve()
    bad(record)
    with pytest.raises(coordination.Refusal):
        coordination.validate_record(record)


def test_schema_one_snapshot_cannot_be_reserved_or_adopted():
    legacy, version = reserve()
    legacy["schema_version"] = 1
    metadata = {"current_version": version, "versions": {str(version): {"deletion_time": "", "destroyed": False}}}
    secret = {"data": {"data": legacy, "metadata": {"version": version}}}
    with pytest.raises(coordination.Refusal):
        coordination.read_snapshot(200, metadata, 200, secret)


@pytest.mark.parametrize(
    "bad_upid",
    [
        "UPID:other-node:00000001:00000002:00000003:qmrestore:910001:root@pam:",
        "UPID:restore-node:00000001:00000002:00000003:qmrestore:910002:root@pam:",
        "UPID:restore-node:00000001:00000002:00000003:qmdestroy:910001:root@pam:",
        "UPID:restore-node:00000001:00000002:00000003:qmrestore:910001:root@pam:extra",
    ],
)
def test_wrong_target_or_wrong_operation_handle_is_rejected_before_effect(bad_upid):
    record, version = reserve()
    intent = request("intent_restore", record, version=version, action="restore", request_fingerprint="1" * 64)
    record = coordination.plan_transition(record, version, intent)["record"]
    version += 1
    handle_request = request(
        "record_handle",
        record,
        version=version,
        action="restore",
        request_fingerprint="1" * 64,
        handle_type="upid",
        handle=bad_upid,
    )
    with pytest.raises(coordination.Refusal):
        coordination.plan_transition(record, version, handle_request)


def test_stale_snapshot_wrong_nonce_and_cross_target_cannot_advance():
    record, version = reserve()
    stale = copy.deepcopy(record)
    advanced, next_version = apply(record, version, "intent_restore", action="restore", request_fingerprint="1" * 64)
    with pytest.raises(coordination.Refusal):
        coordination.plan_transition(
            advanced,
            next_version,
            request(
                "record_handle",
                stale,
                version=version,
                action="restore",
                request_fingerprint="1" * 64,
                handle_type="upid",
                handle=RESTORE_UPID,
            ),
        )
    bad = request(
        "intent_restore", record, version=version, nonce="f" * 64, action="restore", request_fingerprint="1" * 64
    )
    with pytest.raises(coordination.Refusal):
        coordination.plan_transition(record, version, bad)
    other = {"node": "restore-node", "vmid": 910002}
    cross = request(
        "intent_restore", record, version=version, target_ref=other, action="restore", request_fingerprint="1" * 64
    )
    with pytest.raises(coordination.Refusal):
        coordination.plan_transition(record, version, cross)


def test_unknown_or_unverified_write_remains_pending_and_cannot_retry():
    record, version = reserve()
    record, version = apply(record, version, "intent_restore", action="restore", request_fingerprint="a" * 64)
    assert record["pending"]["action"] == "restore"
    with pytest.raises(coordination.Refusal):
        apply(record, version, "intent_restore", action="restore", request_fingerprint="a" * 64)
    with pytest.raises(coordination.Refusal):
        apply(
            record,
            version,
            "verify_effect",
            action="restore",
            request_fingerprint="a" * 64,
            handle_type="upid",
            handle=RESTORE_UPID,
            effect_fingerprint="b" * 64,
            evidence={
                "action": "restore",
                "handle_type": "upid",
                "handle": RESTORE_UPID,
                "status": "failed",
                "effect_fingerprint": "b" * 64,
            },
        )


def test_transition_order_prevents_start_or_guest_exec_before_isolation():
    record, version = reserve()
    record, version = apply(record, version, "intent_restore", action="restore", request_fingerprint="1" * 64)
    with pytest.raises(coordination.Refusal):
        apply(record, version, "intent_action", action="start", request_fingerprint="2" * 64)


def test_cleanup_requires_exact_authenticated_completed_restore_record_and_version():
    record, version = completed_restore()
    proof = authenticated_task(record, version)
    bad = copy.deepcopy(proof)
    bad["task_status"] = "running"
    with pytest.raises(coordination.Refusal):
        apply(record, version, "start_cleanup", evidence=bad, request_fingerprint="8" * 64)
    bad = copy.deepcopy(proof)
    bad["coordination_version"] += 1
    with pytest.raises(coordination.Refusal):
        apply(record, version, "start_cleanup", evidence=bad, request_fingerprint="8" * 64)
    cleanup, cleanup_version = apply(record, version, "start_cleanup", evidence=proof, request_fingerprint="8" * 64)
    assert cleanup["phase"] == "cleanup_active"
    assert cleanup["prior_authenticated_task_id"] == proof["task_id"]
    assert cleanup["active_nonce"] != cleanup["restore_nonce"]
    assert cleanup_version == version + 1


def test_delete_must_be_owned_qmdestroy_and_exact_absence_before_cleaned():
    record, version = completed_restore()
    record, version = apply(
        record, version, "start_cleanup", evidence=authenticated_task(record, version), request_fingerprint="8" * 64
    )
    record, version = apply(
        record,
        version,
        "record_handle",
        action="delete",
        request_fingerprint="8" * 64,
        handle_type="upid",
        handle=DELETE_UPID,
    )
    effect = "9" * 64
    record, version = apply(
        record,
        version,
        "verify_effect",
        action="delete",
        request_fingerprint="8" * 64,
        handle_type="upid",
        handle=DELETE_UPID,
        effect_fingerprint=effect,
        evidence={
            "type": "delete_effect/v2",
            "action": "delete",
            "node": TARGET["node"],
            "vmid": TARGET["vmid"],
            "task_state": "stopped",
            "exit_status": "OK",
            "task_output_sha256": effect,
        },
    )
    evidence = {
        "type": "pve_delete_verification/v2",
        "task_id": record["prior_authenticated_task_id"],
        "delete_upid": DELETE_UPID,
        "task_state": "stopped",
        "exit_status": "OK",
        "target_absence": {
            "node": TARGET["node"],
            "vmid": TARGET["vmid"],
            "nextid_status": 200,
            "nextid_echo": TARGET["vmid"],
            "node_config_status": 404,
            "target_fingerprint": DECLARATION["target_fingerprint"],
        },
        "bindings": {
            key: record[key]
            for key in (
                "operation_id",
                "target_fingerprint",
                "artifact_fingerprint",
                "restore_nonce",
                "active_nonce",
                "prior_authenticated_task_id",
                "public_sha",
                "private_inventory_sha",
            )
        },
    }
    cleaned, _ = apply(record, version, "cleaned", evidence=evidence)
    assert cleaned["phase"] == "cleaned"
    broken = copy.deepcopy(evidence)
    broken["target_absence"]["node_config_status"] = 500
    with pytest.raises(coordination.Refusal):
        apply(record, version, "cleaned", evidence=broken)
