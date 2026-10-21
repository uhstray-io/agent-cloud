"""Regression tests for the source-only OpenBao restore coordination primitive."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from jinja2 import Environment

sys.path.insert(0, str(Path(__file__).parents[1] / "playbooks" / "files"))

import o11y_restore_coordination as coordination  # noqa: E402

DECLARATION = {
    "declaration_fingerprint": "1" * 64,
    "target_fingerprint": coordination.target_identity_fingerprint({"node": "pve", "vmid": 102}),
    "artifact_fingerprint": "3" * 64,
    "public_sha": "a" * 40,
    "private_inventory_sha": "b" * 64,
}
RESTORE_UPID = "UPID:pve:00000001:00000002:00000003:qmrestore:102:root@pam:"
DELETE_UPID = "UPID:pve:00000004:00000005:00000006:qmdelete:102:root@pam:"
_AUTO = object()


def record_version(snapshot):
    if snapshot["phase"] == "reserved":
        return 1
    if snapshot["phase"] == "restore_active":
        return 2 if snapshot["upid"] is None else 3
    if snapshot["phase"] == "awaiting_receipt":
        return 4
    if snapshot["phase"] == "cleanup_active":
        return 5 if snapshot["upid"] is None else 6
    if snapshot["phase"] == "cleaned":
        return 7
    raise AssertionError(f"unexpected test phase: {snapshot['phase']}")


def owner_nonce(snapshot):
    return snapshot["active_nonce"] if snapshot["phase"] in {"cleanup_active", "cleaned"} else snapshot["restore_nonce"]


def request(
    transition: str,
    *,
    snapshot=None,
    nonce=_AUTO,
    evidence=None,
    upid=None,
    expected_record=_AUTO,
    expected_version=_AUTO,
):
    if expected_record is _AUTO:
        expected_record = snapshot
    if expected_version is _AUTO:
        expected_version = record_version(snapshot) if snapshot is not None else 0
    if nonce is _AUTO:
        nonce = owner_nonce(snapshot) if snapshot is not None else None
    return {
        "transition": transition,
        "declaration": DECLARATION,
        "target_ref": {"node": "pve", "vmid": 102},
        "expected_record": expected_record,
        "expected_version": expected_version,
        "nonce": nonce,
        "evidence": evidence,
        "upid": upid,
    }


def planned_reservation():
    plan = coordination.plan_transition(
        None,
        0,
        request("reserve"),
    )
    return plan["record"]


def start_restore(snapshot=None):
    current = snapshot or planned_reservation()
    return coordination.plan_transition(
        current,
        1,
        request("start_restore", snapshot=current),
    )["record"]


def record_restore_upid(snapshot=None):
    current = snapshot or start_restore()
    return coordination.plan_transition(
        current,
        2,
        request("record_restore_upid", snapshot=current, upid=RESTORE_UPID),
    )["record"]


def restore_evidence(snapshot=None):
    current = snapshot or record_restore_upid()
    return {
        "type": "restore_receipt/v1",
        "receipt_fingerprint": "5" * 64,
        "bindings": {
            "operation_id": current["operation_id"],
            "target_fingerprint": current["target_fingerprint"],
            "artifact_fingerprint": current["artifact_fingerprint"],
            "restore_nonce": current["restore_nonce"],
            "public_sha": current["public_sha"],
            "private_inventory_sha": current["private_inventory_sha"],
        },
        "restore_upid": current["upid"],
        "target_state": "stopped",
        "pending_action": None,
    }


def authenticated_restore_task(snapshot=None):
    current = snapshot or awaiting_receipt()
    return {
        "type": "prior_restore_task_proof/v1",
        "task_id": 4102,
        "task_status": "success",
        "output_fingerprint": "4" * 64,
        "receipt_fingerprint": current["receipt_fingerprint"],
        "bindings": {
            "operation_id": current["operation_id"],
            "target_fingerprint": current["target_fingerprint"],
            "artifact_fingerprint": current["artifact_fingerprint"],
            "restore_nonce": current["restore_nonce"],
            "public_sha": current["public_sha"],
            "private_inventory_sha": current["private_inventory_sha"],
            "restore_upid": current["upid"],
        },
        "coordination_record": current,
        "coordination_version": record_version(current),
    }


def awaiting_receipt(snapshot=None):
    current = snapshot or record_restore_upid()
    return coordination.plan_transition(
        current,
        3,
        request("awaiting_receipt", snapshot=current, evidence=restore_evidence(current)),
    )["record"]


def start_cleanup(snapshot=None):
    current = snapshot or awaiting_receipt()
    return coordination.plan_transition(
        current,
        4,
        request("start_cleanup", snapshot=current, evidence=authenticated_restore_task(current)),
    )["record"]


def record_cleanup_upid(snapshot=None):
    current = snapshot or start_cleanup()
    return coordination.plan_transition(
        current,
        5,
        request("record_cleanup_upid", snapshot=current, upid=DELETE_UPID),
    )["record"]


def cleanup_evidence(snapshot=None):
    current = snapshot or record_cleanup_upid()
    return {
        "type": "pve_delete_verification/v1",
        "bindings": {
            "operation_id": current["operation_id"],
            "target_fingerprint": current["target_fingerprint"],
            "artifact_fingerprint": current["artifact_fingerprint"],
            "restore_nonce": current["restore_nonce"],
            "active_nonce": current["active_nonce"],
            "prior_authenticated_task_id": current["prior_authenticated_task_id"],
            "public_sha": current["public_sha"],
            "private_inventory_sha": current["private_inventory_sha"],
        },
        "target_absence": {
            "type": "pve_exact_vmid_absence/v1",
            "node": "pve",
            "vmid": 102,
            "nextid_status": 200,
            "nextid_echo": 102,
            "node_config_status": 404,
            "target_fingerprint": current["target_fingerprint"],
        },
        "delete_upid": current["upid"],
        "task_state": "stopped",
        "exit_status": "OK",
    }


def cleaned(snapshot=None):
    current = snapshot or record_cleanup_upid()
    return coordination.plan_transition(
        current,
        6,
        request("cleaned", snapshot=current, evidence=cleanup_evidence(current)),
    )["record"]


def kv_reads(record, version=1):
    metadata = {
        "current_version": version,
        "versions": {str(version): {"created_time": "2026-10-21T12:00:00Z", "deletion_time": "", "destroyed": False}},
    }
    secret = {"data": {"data": record, "metadata": {"version": version}}}
    return metadata, secret


def invoke_rendered_plan_command(request_value, *, metadata_status=404, metadata=None, data_status=404, secret=None):
    """Render the include's actual stdin expression and invoke its helper argv."""
    task_path = Path(__file__).parents[1] / "playbooks" / "tasks" / "o11y-restore-coordination.yml"
    tasks = yaml.safe_load(task_path.read_text())
    plan_task = next(task for task in tasks if task["name"] == "Restore coordination: plan the strict transition")
    command = plan_task["ansible.builtin.command"]
    assert command["argv"] == ["python3", "{{ playbook_dir }}/files/o11y_restore_coordination.py"]
    environment = Environment()
    environment.filters["to_json"] = lambda value: json.dumps(value, separators=(",", ":"))
    rendered_stdin = environment.from_string(command["stdin"]).render(
        _o11y_rc_metadata={"status": metadata_status, "json": {"data": metadata}},
        _o11y_rc_secret={"status": data_status, "json": secret},
        o11y_restore_coordination_request=request_value,
    )
    return subprocess.run(
        [sys.executable, str(Path(coordination.__file__))],
        input=rendered_stdin,
        text=True,
        capture_output=True,
        check=False,
    )


def test_absent_path_reserves_once_with_cas_zero_and_exact_record_shape():
    plan = coordination.plan_transition(None, 0, request("reserve"))

    assert plan["result"] == "write"
    assert plan["payload"] == {"data": plan["record"], "options": {"cas": 0}}
    assert plan["record"]["phase"] == "reserved"
    assert plan["record"]["generation"] == 1
    assert coordination._nonce(plan["record"]["restore_nonce"])
    assert set(plan["record"]) == coordination.RECORD_KEYS


def test_restore_nonce_comes_from_one_token_hex_32_call_per_new_phase(monkeypatch):
    calls = []
    tokens = iter(["c" * 64, "d" * 64])

    def fixed_token_hex(size):
        calls.append(size)
        return next(tokens)

    monkeypatch.setattr(coordination.secrets, "token_hex", fixed_token_hex)
    reserved = coordination.plan_transition(None, 0, request("reserve"))["record"]
    assert calls == [32]

    receipt = awaiting_receipt(record_restore_upid(start_restore(reserved)))
    cleanup = coordination.plan_transition(
        receipt,
        4,
        request("start_cleanup", snapshot=receipt, evidence=authenticated_restore_task(receipt)),
    )["record"]
    assert calls == [32, 32]
    assert cleanup["active_nonce"] == "d" * 64


def test_a_lost_reservation_response_cannot_turn_a_rerun_into_takeover():
    # Model a successful CAS whose response was lost: the next invocation sees
    # the stored reservation, and its attempted second reservation must stop.
    snapshot = planned_reservation()

    with pytest.raises(coordination.Refusal, match="expected_snapshot_mismatch"):
        coordination.plan_transition(
            snapshot,
            1,
            request("reserve", snapshot=snapshot, expected_record=None, expected_version=0, nonce=None),
        )


def test_stale_expected_version_cannot_be_used_for_a_competing_transition():
    snapshot = planned_reservation()
    stale = request("start_restore", snapshot=snapshot, expected_version=0)

    with pytest.raises(coordination.Refusal, match="expected_version_mismatch"):
        coordination.plan_transition(snapshot, 1, stale)


def test_expected_snapshot_and_kv_version_require_strict_integer_types():
    snapshot = planned_reservation()
    malformed_record = {**snapshot, "generation": True}
    malformed_snapshot_request = request("start_restore", snapshot=snapshot, expected_record=malformed_record)
    with pytest.raises(coordination.Refusal, match="record_shape_invalid"):
        coordination.plan_transition(snapshot, 1, malformed_snapshot_request)

    malformed_version_request = request("start_restore", snapshot=snapshot, expected_version=True)
    with pytest.raises(coordination.Refusal, match="expected_version_mismatch"):
        coordination.plan_transition(snapshot, 1, malformed_version_request)


def test_transition_refuses_a_newer_observed_version_instead_of_adopting_it():
    snapshot = planned_reservation()

    with pytest.raises(coordination.Refusal, match="expected_version_mismatch"):
        coordination.plan_transition(snapshot, 8, request("start_restore", snapshot=snapshot))


def test_fresh_get_cannot_be_adopted_as_expected_state_by_a_new_run():
    stored = planned_reservation()
    metadata, secret = kv_reads(stored)
    fresh_record, fresh_version = coordination.read_snapshot(200, metadata, 200, secret)

    attempt = request(
        "start_restore",
        snapshot=fresh_record,
        expected_record=None,
        expected_version=0,
        nonce=None,
    )
    with pytest.raises(coordination.Refusal, match="expected_snapshot_mismatch"):
        coordination.plan_transition(fresh_record, fresh_version, attempt)


def test_reading_the_latest_record_does_not_supply_the_held_owner_nonce():
    snapshot = awaiting_receipt()
    metadata, secret = kv_reads(snapshot, version=record_version(snapshot))
    fresh_record, fresh_version = coordination.read_snapshot(200, metadata, 200, secret)

    with pytest.raises(coordination.Refusal, match="nonce_invalid"):
        coordination.plan_transition(
            fresh_record,
            fresh_version,
            request("start_cleanup", snapshot=fresh_record, nonce=None, evidence=authenticated_restore_task(snapshot)),
        )


def test_two_cleanup_claimants_cannot_take_over_after_the_cas_loser(monkeypatch):
    awaiting = awaiting_receipt()
    evidence = authenticated_restore_task(awaiting)
    nonces = iter(["d" * 64, "e" * 64])
    monkeypatch.setattr(coordination.secrets, "token_hex", lambda size: next(nonces))
    owner_request = request("start_cleanup", snapshot=awaiting, evidence=evidence)

    winner = coordination.plan_transition(awaiting, 4, owner_request)
    loser = coordination.plan_transition(awaiting, 4, owner_request)
    assert winner["payload"]["options"] == loser["payload"]["options"] == {"cas": 4}
    assert winner["record"]["active_nonce"] != loser["record"]["active_nonce"]

    metadata, secret = kv_reads(winner["record"], version=5)
    with pytest.raises(coordination.Refusal, match="readback_mismatch"):
        coordination.verify_readback(loser, 200, metadata, 200, secret)

    with pytest.raises(coordination.Refusal, match="nonce_invalid"):
        coordination.plan_transition(
            winner["record"],
            5,
            request(
                "record_cleanup_upid",
                snapshot=winner["record"],
                nonce=loser["record"]["active_nonce"],
                upid=DELETE_UPID,
            ),
        )


def test_each_later_transition_requires_the_held_nonce_after_a_fresh_read():
    restore_active = start_restore()
    restore_upid_record = record_restore_upid()
    awaiting = awaiting_receipt()
    cleanup_active = start_cleanup(awaiting)
    cleanup_upid_record = record_cleanup_upid(cleanup_active)
    attempts = [
        (restore_active, "record_restore_upid", {"upid": RESTORE_UPID}),
        (restore_upid_record, "awaiting_receipt", {"evidence": restore_evidence(restore_upid_record)}),
        (awaiting, "start_cleanup", {"evidence": authenticated_restore_task(awaiting)}),
        (cleanup_upid_record, "cleaned", {"evidence": cleanup_evidence(cleanup_upid_record)}),
    ]

    for stored, transition, extra in attempts:
        metadata, secret = kv_reads(stored, version=record_version(stored))
        fresh_record, fresh_version = coordination.read_snapshot(200, metadata, 200, secret)
        attempt = request(transition, snapshot=fresh_record, nonce=None, **extra)
        with pytest.raises(coordination.Refusal, match="nonce_invalid"):
            coordination.plan_transition(fresh_record, fresh_version, attempt)


def test_metadata_and_kv_version_drift_refuse_before_planning_a_write():
    snapshot = planned_reservation()
    metadata, secret = kv_reads(snapshot, version=2)
    secret["data"]["metadata"]["version"] = 1

    with pytest.raises(coordination.Refusal, match="cas_version_invalid"):
        coordination.read_snapshot(200, metadata, 200, secret)


def test_deleted_metadata_and_metadata_only_records_are_never_absence():
    tombstone = {"current_version": 1, "versions": {"1": {"deletion_time": "2026-10-21T12:00:00Z", "destroyed": False}}}
    with pytest.raises(coordination.Refusal, match="metadata_tombstone"):
        coordination.read_snapshot(200, tombstone, 404, None)

    metadata, _secret = kv_reads(planned_reservation())
    with pytest.raises(coordination.Refusal, match="data_missing"):
        coordination.read_snapshot(200, metadata, 404, None)


def test_wrong_target_or_inventory_owner_fingerprint_refuses():
    snapshot = planned_reservation()
    wrong_owner = {**DECLARATION, "private_inventory_sha": "f" * 64}

    with pytest.raises(coordination.Refusal, match="record_binding_mismatch"):
        coordination.plan_transition(
            snapshot,
            1,
            {**request("start_restore", snapshot=snapshot), "declaration": wrong_owner},
        )


def test_record_generation_is_fixed_and_cannot_be_reinterpreted():
    snapshot = planned_reservation()
    changed_generation = {**snapshot, "generation": 2}

    with pytest.raises(coordination.Refusal, match="record_shape_invalid"):
        coordination.validate_record(changed_generation)


def test_unsupported_lifecycle_action_is_refused():
    with pytest.raises(coordination.Refusal, match="invalid_input"):
        coordination.plan_transition(None, 0, request("isolate_nic"))

    record = planned_reservation()
    with pytest.raises(coordination.Refusal, match="record_shape_invalid"):
        coordination.validate_record({**record, "action": "start"})


def test_restore_nonce_must_bind_receipt_and_cleanup_uses_a_fresh_nonce():
    snapshot = record_restore_upid()
    evidence = restore_evidence(snapshot)
    evidence["bindings"]["restore_nonce"] = "e" * 64

    with pytest.raises(coordination.Refusal, match="evidence_invalid"):
        coordination.plan_transition(snapshot, 3, request("awaiting_receipt", snapshot=snapshot, evidence=evidence))

    receipt = awaiting_receipt()
    cleanup = start_cleanup(receipt)
    assert cleanup["active_nonce"] != receipt["restore_nonce"]
    assert coordination._nonce(cleanup["active_nonce"])


def test_pending_upid_or_missing_restore_action_refuses_receipt_transition():
    snapshot = start_restore()

    with pytest.raises(coordination.Refusal, match="phase_transition_invalid"):
        coordination.plan_transition(
            snapshot, 2, request("awaiting_receipt", snapshot=snapshot, evidence=restore_evidence(snapshot))
        )


@pytest.mark.parametrize(
    "upid",
    [
        DELETE_UPID,
        "UPID:other-node:00000001:00000002:00000003:qmrestore:102:root@pam:",
        "UPID:pve:00000001:00000002:00000003:qmrestore:103:root@pam:",
    ],
)
def test_restore_upid_must_match_exact_target_node_vmid_and_operation(upid):
    snapshot = start_restore()
    with pytest.raises(coordination.Refusal, match="evidence_invalid"):
        coordination.plan_transition(
            snapshot,
            2,
            request("record_restore_upid", snapshot=snapshot, upid=upid),
        )


def test_cleanup_upid_must_be_the_exact_delete_operation():
    snapshot = start_cleanup()
    with pytest.raises(coordination.Refusal, match="evidence_invalid"):
        coordination.plan_transition(
            snapshot,
            5,
            request("record_cleanup_upid", snapshot=snapshot, upid=RESTORE_UPID),
        )


def test_swapped_transient_target_ref_cannot_reuse_expected_record_and_nonce():
    snapshot = start_restore()
    req = request("record_restore_upid", snapshot=snapshot, upid=RESTORE_UPID)
    req["target_ref"] = {"node": "other-node", "vmid": 102}

    with pytest.raises(coordination.Refusal, match="record_binding_mismatch"):
        coordination.plan_transition(snapshot, 2, req)


def test_lost_restore_response_keeps_the_target_reserved_for_manual_reconciliation():
    snapshot = start_restore()

    with pytest.raises(coordination.Refusal, match="phase_transition_invalid"):
        coordination.plan_transition(snapshot, 2, request("start_restore", snapshot=snapshot))
    with pytest.raises(coordination.Refusal, match="phase_transition_invalid"):
        coordination.plan_transition(
            snapshot, 2, request("awaiting_receipt", snapshot=snapshot, evidence=restore_evidence(snapshot))
        )


def test_boolean_and_unbound_receipt_proofs_are_rejected():
    snapshot = record_restore_upid()

    with pytest.raises(coordination.Refusal, match="evidence_invalid"):
        coordination.plan_transition(snapshot, 3, request("awaiting_receipt", snapshot=snapshot, evidence=True))

    evidence = restore_evidence(snapshot)
    evidence["bindings"]["target_fingerprint"] = "0" * 64
    with pytest.raises(coordination.Refusal, match="evidence_invalid"):
        coordination.plan_transition(snapshot, 3, request("awaiting_receipt", snapshot=snapshot, evidence=evidence))


def test_generated_receipt_does_not_authorize_cleanup_without_prior_task_proof():
    snapshot = awaiting_receipt()
    assert snapshot["prior_authenticated_task_id"] is None
    with pytest.raises(coordination.Refusal, match="evidence_invalid"):
        coordination.plan_transition(
            snapshot,
            4,
            request("start_cleanup", snapshot=snapshot, evidence=restore_evidence(snapshot)),
        )

    proof = authenticated_restore_task(snapshot)
    proof["task_status"] = "running"
    with pytest.raises(coordination.Refusal, match="evidence_invalid"):
        coordination.plan_transition(snapshot, 4, request("start_cleanup", snapshot=snapshot, evidence=proof))

    proof = authenticated_restore_task(snapshot)
    proof["coordination_version"] -= 1
    with pytest.raises(coordination.Refusal, match="evidence_invalid"):
        coordination.plan_transition(snapshot, 4, request("start_cleanup", snapshot=snapshot, evidence=proof))


def test_lost_cleanup_upid_response_leaves_deletion_pending_and_uncleanable():
    snapshot = start_cleanup()

    with pytest.raises(coordination.Refusal, match="phase_transition_invalid"):
        coordination.plan_transition(
            snapshot, 5, request("cleaned", snapshot=snapshot, evidence=cleanup_evidence(snapshot))
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("nextid_echo", 103),
        ("nextid_status", 400),
        ("node", "other-node"),
        ("node_config_status", 500),
        ("node_config_status", 403),
        ("target_fingerprint", "0" * 64),
    ],
)
def test_cleaned_requires_authoritative_exact_vmid_absence(field, value):
    snapshot = record_cleanup_upid()
    evidence = cleanup_evidence(snapshot)
    evidence["target_absence"][field] = value
    with pytest.raises(coordination.Refusal, match="evidence_invalid"):
        coordination.plan_transition(snapshot, 6, request("cleaned", snapshot=snapshot, evidence=evidence))


def test_cleaned_does_not_accept_boolean_absence_or_wrong_delete_upid():
    snapshot = record_cleanup_upid()
    proof = cleanup_evidence(snapshot)
    proof["target_absence"] = True
    with pytest.raises(coordination.Refusal, match="evidence_invalid"):
        coordination.plan_transition(snapshot, 6, request("cleaned", snapshot=snapshot, evidence=proof))

    proof = cleanup_evidence(snapshot)
    proof["delete_upid"] = RESTORE_UPID
    with pytest.raises(coordination.Refusal, match="evidence_invalid"):
        coordination.plan_transition(snapshot, 6, request("cleaned", snapshot=snapshot, evidence=proof))

    proof = cleanup_evidence(snapshot)
    proof["target_absence"]["vmid"] = True
    proof["target_absence"]["nextid_echo"] = True
    with pytest.raises(coordination.Refusal, match="evidence_invalid"):
        coordination.plan_transition(snapshot, 6, request("cleaned", snapshot=snapshot, evidence=proof))


def test_cleaned_rerun_is_idempotent_and_never_creates_a_new_generation():
    snapshot = cleaned()
    assert snapshot["prior_authenticated_task_id"] == 4102
    assert snapshot["receipt_fingerprint"] == "5" * 64
    plan = coordination.plan_transition(snapshot, 7, request("reserve", snapshot=snapshot))

    assert plan == {"result": "unchanged", "record": snapshot, "version": 7}


def test_exact_readback_requires_expected_record_and_version():
    reserve = coordination.plan_transition(None, 0, request("reserve"))
    expected = reserve["record"]
    metadata, secret = kv_reads(expected, version=1)

    assert coordination.verify_readback(reserve, 200, metadata, 200, secret) == {
        "result": "verified",
        "phase": "reserved",
        "version": 1,
    }
    transition = coordination.plan_transition(expected, 1, request("start_restore", snapshot=expected))
    drifted_metadata, drifted_secret = kv_reads(expected, version=2)
    with pytest.raises(coordination.Refusal, match="readback_mismatch"):
        coordination.verify_readback(transition, 200, drifted_metadata, 200, drifted_secret)


def test_cli_refusals_never_echo_untrusted_payload_values():
    secret_marker = "PRIVATE_TARGET_SENTINEL"
    proc = subprocess.run(
        [sys.executable, str(Path(coordination.__file__))],
        input=json.dumps({"mode": "unknown", "private_value": secret_marker}),
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 2
    assert secret_marker not in proc.stdout
    assert secret_marker not in proc.stderr
    assert json.loads(proc.stdout) == {"result": "refused", "reason": "invalid_input"}


def test_rendered_include_request_executes_real_helper_with_fixed_cas_payload():
    proc = invoke_rendered_plan_command(request("reserve"))

    assert proc.returncode == 0
    result = json.loads(proc.stdout)
    assert result["result"] == "write"
    assert result["payload"]["options"] == {"cas": 0}
    assert result["payload"]["data"]["phase"] == "reserved"
    assert proc.stderr == ""


def test_rendered_include_hands_protected_record_version_and_nonce_to_next_transition():
    reserve_proc = invoke_rendered_plan_command(request("reserve"))
    assert reserve_proc.returncode == 0
    reservation = json.loads(reserve_proc.stdout)
    held_record = reservation["record"]
    held_version = reservation["expected_version"]
    metadata, secret = kv_reads(held_record, version=held_version)

    next_proc = invoke_rendered_plan_command(
        request("start_restore", snapshot=held_record, expected_version=held_version),
        metadata_status=200,
        metadata=metadata,
        data_status=200,
        secret=secret,
    )

    assert next_proc.returncode == 0
    transition = json.loads(next_proc.stdout)
    assert transition["payload"]["options"] == {"cas": held_version}
    assert transition["record"]["phase"] == "restore_active"
    assert transition["record"]["active_nonce"] == held_record["restore_nonce"]


def test_rendered_include_request_rejects_stale_expected_version():
    snapshot = planned_reservation()
    metadata, secret = kv_reads(snapshot)
    stale_request = request("start_restore", snapshot=snapshot, expected_version=0)
    proc = invoke_rendered_plan_command(
        stale_request,
        metadata_status=200,
        metadata=metadata,
        data_status=200,
        secret=secret,
    )

    assert proc.returncode == 2
    assert json.loads(proc.stdout) == {"result": "refused", "reason": "expected_version_mismatch"}


@pytest.mark.parametrize(
    ("input_text", "expected_reason"),
    [
        ('{"mode":"plan","request":{}}', "invalid_input"),
        ("{", "invalid_input"),
    ],
)
def test_helper_command_refuses_missing_status_or_malformed_json(input_text, expected_reason):
    proc = subprocess.run(
        [sys.executable, str(Path(coordination.__file__))],
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 2
    assert json.loads(proc.stdout) == {"result": "refused", "reason": expected_reason}
    assert proc.stderr == ""


def test_ansible_include_enforces_path_argv_and_safe_logging_boundaries():
    task_path = Path(__file__).parents[1] / "playbooks" / "tasks" / "o11y-restore-coordination.yml"
    source = task_path.read_text()
    parsed = yaml.safe_load(source)
    tasks = {task["name"]: task for task in parsed}

    assert isinstance(parsed, list) and parsed
    assert "/v1/secret/data/coordination/o11y-restore-target" in source
    assert "/v1/secret/metadata/coordination/o11y-restore-target" in source
    command_tasks = [task for task in parsed if "ansible.builtin.command" in task]
    assert len(command_tasks) == 2
    for task in command_tasks:
        command = task["ansible.builtin.command"]
        assert command["argv"] == ["python3", "{{ playbook_dir }}/files/o11y_restore_coordination.py"]
        assert "cmd" not in command
        assert task["no_log"] is True
        assert task["failed_when"] is False
    uri_tasks = [task for task in parsed if "ansible.builtin.uri" in task]
    assert len(uri_tasks) == 5
    assert all(task["no_log"] is True for task in uri_tasks)
    assert all(task["ansible.builtin.uri"]["follow_redirects"] == "none" for task in uri_tasks)
    assert tasks["Restore coordination: retain the validated plan in controller memory"]["no_log"] is True

    safe_assertions = (
        "Restore coordination: refuse an invalid or stale transition",
        "Restore coordination: require CAS success",
        "Restore coordination: require verified readback",
    )
    for name in safe_assertions:
        task = tasks[name]
        assert "no_log" not in task
        assert "{{" not in task["ansible.builtin.assert"]["fail_msg"]
        condition_text = " ".join(task["ansible.builtin.assert"]["that"])
        assert "stdout" not in condition_text and ".json" not in condition_text

    plan_assert = tasks[safe_assertions[0]]["ansible.builtin.assert"]["that"]
    assert plan_assert == [
        "_o11y_rc_plan_result is defined",
        "_o11y_rc_plan_result.rc is defined",
        "_o11y_rc_plan_result.rc == 0",
    ]
    assert "status_code: [200, 400, 412]" in source
    assert "retries:" not in source
    assert "until:" not in source
    assert "ttl" not in source.lower()
    assert "cas" in source.lower()
