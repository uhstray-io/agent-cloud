#!/usr/bin/env python3
"""Plan fail-closed transitions for the fixed o11y restore coordination record.

This module validates inputs and produces OpenBao KV-v2 CAS payloads. It does
not authenticate Proxmox or Semaphore evidence; a future caller must obtain
that evidence from the live systems and guard every include input.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sys
from typing import Any

SCHEMA_VERSION = 1
RECORD_KEYS = {
    "schema_version",
    "declaration_fingerprint",
    "target_fingerprint",
    "artifact_fingerprint",
    "public_sha",
    "private_inventory_sha",
    "operation_id",
    "generation",
    "phase",
    "restore_nonce",
    "active_nonce",
    "action",
    "upid",
    "receipt_fingerprint",
    "prior_authenticated_task_id",
}
PHASES = {"reserved", "restore_active", "awaiting_receipt", "cleanup_active", "cleaned"}
FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
REVISION = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
NONCE = re.compile(r"[0-9a-f]{64}\Z")
UPID = re.compile(
    r"UPID:[A-Za-z0-9_.-]+:[0-9A-Fa-f]+:[0-9A-Fa-f]+:[0-9A-Fa-f]+:[A-Za-z0-9_-]+:[A-Za-z0-9_.-]*:[A-Za-z0-9_.@-]*:?\Z"
)
NODE = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")
SAFE_REASONS = {
    "invalid_input",
    "metadata_missing",
    "metadata_tombstone",
    "data_missing",
    "record_shape_invalid",
    "record_binding_mismatch",
    "generation_mismatch",
    "phase_transition_invalid",
    "nonce_invalid",
    "evidence_invalid",
    "cas_version_invalid",
    "readback_mismatch",
    "expected_snapshot_mismatch",
    "expected_version_mismatch",
}


class Refusal(ValueError):
    """A fixed, non-sensitive refusal category."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason if reason in SAFE_REASONS else "invalid_input")
        self.reason = reason if reason in SAFE_REASONS else "invalid_input"


def _is_int(value: object, *, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise Refusal(reason)


def _sha(value: object) -> bool:
    return isinstance(value, str) and FINGERPRINT.fullmatch(value) is not None


def _revision(value: object) -> bool:
    return isinstance(value, str) and REVISION.fullmatch(value) is not None


def _nonce(value: object) -> bool:
    return isinstance(value, str) and NONCE.fullmatch(value) is not None


def fresh_nonce() -> str:
    """Create the one 256-bit correlation nonce for a new operation phase."""
    return secrets.token_hex(32)


def operation_identity(declaration: dict[str, Any]) -> str:
    """Derive a stable opaque identity from the reviewed immutable bindings."""
    fields = (
        "declaration_fingerprint",
        "target_fingerprint",
        "artifact_fingerprint",
        "public_sha",
        "private_inventory_sha",
    )
    canonical = {name: declaration[name] for name in fields}
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def validate_declaration(declaration: object) -> dict[str, Any]:
    """Validate exact declaration fields; values contain fingerprints only."""
    required = {
        "declaration_fingerprint",
        "target_fingerprint",
        "artifact_fingerprint",
        "public_sha",
        "private_inventory_sha",
    }
    _require(isinstance(declaration, dict) and set(declaration) == required, "invalid_input")
    _require(_sha(declaration["declaration_fingerprint"]), "invalid_input")
    _require(_sha(declaration["target_fingerprint"]), "invalid_input")
    _require(_sha(declaration["artifact_fingerprint"]), "invalid_input")
    _require(_revision(declaration["public_sha"]), "invalid_input")
    _require(_revision(declaration["private_inventory_sha"]), "invalid_input")
    return declaration


def validate_record(record: object) -> dict[str, Any]:
    """Require the exact versioned schema, types, and phase-specific invariants."""
    _require(isinstance(record, dict) and set(record) == RECORD_KEYS, "record_shape_invalid")
    _require(
        record["schema_version"] == SCHEMA_VERSION and type(record["schema_version"]) is int, "record_shape_invalid"
    )
    _require(_sha(record["declaration_fingerprint"]), "record_shape_invalid")
    _require(_sha(record["target_fingerprint"]), "record_shape_invalid")
    _require(_sha(record["artifact_fingerprint"]), "record_shape_invalid")
    _require(_revision(record["public_sha"]), "record_shape_invalid")
    _require(_revision(record["private_inventory_sha"]), "record_shape_invalid")
    _require(_sha(record["operation_id"]), "record_shape_invalid")
    _require(record["generation"] == 1 and type(record["generation"]) is int, "record_shape_invalid")
    _require(isinstance(record["phase"], str) and record["phase"] in PHASES, "record_shape_invalid")
    _require(_nonce(record["restore_nonce"]), "record_shape_invalid")
    _require(record["active_nonce"] is None or _nonce(record["active_nonce"]), "record_shape_invalid")
    _require(
        record["action"] is None or (isinstance(record["action"], str) and record["action"] in {"restore", "delete"}),
        "record_shape_invalid",
    )
    _require(
        record["upid"] is None or (isinstance(record["upid"], str) and UPID.fullmatch(record["upid"]) is not None),
        "record_shape_invalid",
    )
    _require(record["receipt_fingerprint"] is None or _sha(record["receipt_fingerprint"]), "record_shape_invalid")
    _require(
        record["prior_authenticated_task_id"] is None or _is_int(record["prior_authenticated_task_id"], minimum=1),
        "record_shape_invalid",
    )
    if record["phase"] == "reserved":
        _require(
            record["active_nonce"] is None and record["action"] is None and record["upid"] is None,
            "record_shape_invalid",
        )
        _require(
            record["receipt_fingerprint"] is None and record["prior_authenticated_task_id"] is None,
            "record_shape_invalid",
        )
    elif record["phase"] == "restore_active":
        _require(record["active_nonce"] == record["restore_nonce"], "record_shape_invalid")
        _require(record["action"] == "restore" and record["receipt_fingerprint"] is None, "record_shape_invalid")
    elif record["phase"] == "awaiting_receipt":
        _require(record["active_nonce"] is None and record["action"] is None, "record_shape_invalid")
        _require(record["upid"] is not None and _sha(record["receipt_fingerprint"]), "record_shape_invalid")
        _require(record["prior_authenticated_task_id"] is None, "record_shape_invalid")
    elif record["phase"] == "cleanup_active":
        _require(
            record["active_nonce"] != record["restore_nonce"] and _nonce(record["active_nonce"]), "record_shape_invalid"
        )
        _require(record["action"] == "delete" and _sha(record["receipt_fingerprint"]), "record_shape_invalid")
        _require(_is_int(record["prior_authenticated_task_id"], minimum=1), "record_shape_invalid")
    elif record["phase"] == "cleaned":
        _require(_nonce(record["active_nonce"]), "record_shape_invalid")
        _require(record["action"] is None and record["upid"] is not None, "record_shape_invalid")
        _require(_sha(record["receipt_fingerprint"]), "record_shape_invalid")
        _require(_is_int(record["prior_authenticated_task_id"], minimum=1), "record_shape_invalid")
    _require(record["operation_id"] == operation_identity(record), "record_binding_mismatch")
    return record


def read_snapshot(
    metadata_status: object, metadata_payload: object, data_status: object, data_payload: object
) -> tuple[dict[str, Any] | None, int]:
    """Decode a KV-v2 read pair; only a true never-created path is absent."""
    _require(type(metadata_status) is int and metadata_status in {200, 404}, "invalid_input")
    _require(type(data_status) is int and data_status in {200, 404}, "invalid_input")
    if metadata_status == 404 and data_status == 404:
        return None, 0
    _require(metadata_status == 200, "metadata_missing")
    _require(isinstance(metadata_payload, dict), "record_shape_invalid")
    current_version = metadata_payload.get("current_version")
    _require(_is_int(current_version, minimum=1), "metadata_tombstone")
    versions = metadata_payload.get("versions")
    _require(isinstance(versions, dict), "record_shape_invalid")
    current_metadata = versions.get(str(current_version))
    _require(isinstance(current_metadata, dict), "metadata_tombstone")
    _require(current_metadata.get("deletion_time") in (None, ""), "metadata_tombstone")
    _require(
        type(current_metadata.get("destroyed")) is bool and not current_metadata["destroyed"], "metadata_tombstone"
    )
    _require(data_status == 200 and isinstance(data_payload, dict), "data_missing")
    data = data_payload.get("data")
    _require(isinstance(data, dict) and set(data) == {"data", "metadata"}, "record_shape_invalid")
    record = data.get("data")
    kv_metadata = data.get("metadata")
    _require(
        isinstance(kv_metadata, dict)
        and type(kv_metadata.get("version")) is int
        and kv_metadata["version"] == current_version,
        "cas_version_invalid",
    )
    return validate_record(record), current_version


def _bindings_match(record: dict[str, Any], declaration: dict[str, Any]) -> None:
    for key, value in declaration.items():
        _require(record[key] == value, "record_binding_mismatch")
    _require(record["operation_id"] == operation_identity(declaration), "record_binding_mismatch")


def _task_id(value: object) -> bool:
    return _is_int(value, minimum=1)


def _validate_target_ref(target_ref: object) -> dict[str, Any]:
    _require(isinstance(target_ref, dict) and set(target_ref) == {"node", "vmid"}, "invalid_input")
    _require(isinstance(target_ref["node"], str) and NODE.fullmatch(target_ref["node"]) is not None, "invalid_input")
    _require(_is_int(target_ref["vmid"], minimum=100), "invalid_input")
    return target_ref


def target_identity_fingerprint(target_ref: dict[str, Any]) -> str:
    """Fingerprint the canonical Proxmox node and VMID identity only."""
    encoded = json.dumps(
        {"node": target_ref["node"], "vmid": target_ref["vmid"]}, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _upid_matches(upid: object, target_ref: dict[str, Any], operation: str) -> bool:
    if not isinstance(upid, str) or UPID.fullmatch(upid) is None:
        return False
    parts = upid.split(":")
    return parts[1] == target_ref["node"] and parts[5] == operation and parts[6] == str(target_ref["vmid"])


def _owner_nonce(record: dict[str, Any]) -> str:
    return record["active_nonce"] if record["phase"] in {"cleanup_active", "cleaned"} else record["restore_nonce"]


def _validate_saved_upid(record: dict[str, Any], target_ref: dict[str, Any]) -> None:
    if record["upid"] is None:
        return
    operation = "qmdelete" if record["phase"] in {"cleanup_active", "cleaned"} else "qmrestore"
    _require(_upid_matches(record["upid"], target_ref, operation), "evidence_invalid")


def _restore_receipt(evidence: object, record: dict[str, Any]) -> str:
    keys = {
        "type",
        "receipt_fingerprint",
        "bindings",
        "restore_upid",
        "target_state",
        "pending_action",
    }
    _require(isinstance(evidence, dict) and set(evidence) == keys, "evidence_invalid")
    _require(evidence["type"] == "restore_receipt/v1", "evidence_invalid")
    _require(_sha(evidence["receipt_fingerprint"]), "evidence_invalid")
    expected = {
        "operation_id": record["operation_id"],
        "target_fingerprint": record["target_fingerprint"],
        "artifact_fingerprint": record["artifact_fingerprint"],
        "restore_nonce": record["restore_nonce"],
        "public_sha": record["public_sha"],
        "private_inventory_sha": record["private_inventory_sha"],
    }
    _require(evidence["bindings"] == expected, "evidence_invalid")
    _require(evidence["restore_upid"] == record["upid"], "evidence_invalid")
    _require(evidence["target_state"] == "stopped" and evidence["pending_action"] is None, "evidence_invalid")
    return evidence["receipt_fingerprint"]


def _validate_restore_task_proof_shape(evidence: object, record: dict[str, Any], version: int) -> int:
    keys = {
        "type",
        "task_id",
        "task_status",
        "output_fingerprint",
        "receipt_fingerprint",
        "bindings",
        "coordination_record",
        "coordination_version",
    }
    _require(isinstance(evidence, dict) and set(evidence) == keys, "evidence_invalid")
    _require(evidence["type"] == "prior_restore_task_proof/v1", "evidence_invalid")
    _require(_task_id(evidence["task_id"]) and evidence["task_status"] == "success", "evidence_invalid")
    _require(_sha(evidence["output_fingerprint"]) and _sha(evidence["receipt_fingerprint"]), "evidence_invalid")
    expected = {
        "operation_id": record["operation_id"],
        "target_fingerprint": record["target_fingerprint"],
        "artifact_fingerprint": record["artifact_fingerprint"],
        "restore_nonce": record["restore_nonce"],
        "public_sha": record["public_sha"],
        "private_inventory_sha": record["private_inventory_sha"],
        "restore_upid": record["upid"],
    }
    _require(evidence["bindings"] == expected, "evidence_invalid")
    _require(evidence["receipt_fingerprint"] == record["receipt_fingerprint"], "evidence_invalid")
    _require(evidence["coordination_record"] == record, "evidence_invalid")
    _require(
        type(evidence["coordination_version"]) is int and evidence["coordination_version"] == version,
        "evidence_invalid",
    )
    return evidence["task_id"]


def _delete_verification(evidence: object, record: dict[str, Any], target_ref: dict[str, Any]) -> None:
    keys = {"type", "bindings", "delete_upid", "task_state", "exit_status", "target_absence"}
    _require(isinstance(evidence, dict) and set(evidence) == keys, "evidence_invalid")
    _require(evidence["type"] == "pve_delete_verification/v1", "evidence_invalid")
    expected_bindings = {
        "operation_id": record["operation_id"],
        "target_fingerprint": record["target_fingerprint"],
        "artifact_fingerprint": record["artifact_fingerprint"],
        "restore_nonce": record["restore_nonce"],
        "active_nonce": record["active_nonce"],
        "prior_authenticated_task_id": record["prior_authenticated_task_id"],
        "public_sha": record["public_sha"],
        "private_inventory_sha": record["private_inventory_sha"],
    }
    _require(evidence["bindings"] == expected_bindings, "evidence_invalid")
    _require(evidence["delete_upid"] == record["upid"], "evidence_invalid")
    _require(evidence["task_state"] == "stopped" and evidence["exit_status"] == "OK", "evidence_invalid")
    absence = evidence["target_absence"]
    absence_keys = {"type", "node", "vmid", "nextid_status", "nextid_echo", "node_config_status", "target_fingerprint"}
    _require(isinstance(absence, dict) and set(absence) == absence_keys, "evidence_invalid")
    _require(absence["type"] == "pve_exact_vmid_absence/v1", "evidence_invalid")
    _require(absence["node"] == target_ref["node"], "evidence_invalid")
    _require(type(absence["nextid_status"]) is int and absence["nextid_status"] == 200, "evidence_invalid")
    _require(
        _is_int(absence["vmid"], minimum=100)
        and absence["vmid"] == target_ref["vmid"]
        and _is_int(absence["nextid_echo"], minimum=1)
        and absence["nextid_echo"] == absence["vmid"],
        "evidence_invalid",
    )
    _require(type(absence["node_config_status"]) is int and absence["node_config_status"] == 404, "evidence_invalid")
    _require(absence["target_fingerprint"] == record["target_fingerprint"], "evidence_invalid")


def plan_transition(snapshot: dict[str, Any] | None, version: int, request: object) -> dict[str, Any]:
    """Validate a single requested transition and return an exact CAS plan."""
    expected_request_keys = {
        "transition",
        "declaration",
        "target_ref",
        "expected_record",
        "expected_version",
        "nonce",
        "evidence",
        "upid",
    }
    _require(isinstance(request, dict) and set(request) == expected_request_keys, "invalid_input")
    declaration = validate_declaration(request["declaration"])
    target_ref = _validate_target_ref(request["target_ref"])
    _require(target_identity_fingerprint(target_ref) == declaration["target_fingerprint"], "record_binding_mismatch")
    transition = request["transition"]
    _require(
        isinstance(transition, str)
        and transition
        in {
            "reserve",
            "start_restore",
            "record_restore_upid",
            "awaiting_receipt",
            "start_cleanup",
            "record_cleanup_upid",
            "cleaned",
        },
        "invalid_input",
    )
    _require(_is_int(version), "cas_version_invalid")
    _require(_is_int(request["expected_version"]), "expected_version_mismatch")

    if snapshot is None:
        _require(transition == "reserve" and version == 0, "phase_transition_invalid")
        _require(request["expected_record"] is None and request["expected_version"] == 0, "expected_snapshot_mismatch")
        _require(request["evidence"] is None and request["upid"] is None, "invalid_input")
        _require(request["nonce"] is None, "nonce_invalid")
        generated_nonce = fresh_nonce()
        _require(_nonce(generated_nonce), "nonce_invalid")
        record = {
            **declaration,
            "schema_version": SCHEMA_VERSION,
            "operation_id": operation_identity(declaration),
            "generation": 1,
            "phase": "reserved",
            "restore_nonce": generated_nonce,
            "active_nonce": None,
            "action": None,
            "upid": None,
            "receipt_fingerprint": None,
            "prior_authenticated_task_id": None,
        }
        cas = 0
    else:
        expected_record = request["expected_record"]
        _require(isinstance(expected_record, dict), "expected_snapshot_mismatch")
        validate_record(expected_record)
        _require(expected_record == snapshot, "expected_snapshot_mismatch")
        _require(request["expected_version"] == version, "expected_version_mismatch")
        _bindings_match(snapshot, declaration)
        _validate_saved_upid(snapshot, target_ref)
        _require(isinstance(request["nonce"], str) and request["nonce"] == _owner_nonce(snapshot), "nonce_invalid")
        record = dict(snapshot)
        cas = version
        if snapshot["phase"] == "cleaned" and transition == "reserve":
            return {"result": "unchanged", "record": snapshot, "version": version}
        if transition == "start_restore":
            _require(snapshot["phase"] == "reserved", "phase_transition_invalid")
            _require(request["evidence"] is None and request["upid"] is None, "invalid_input")
            record.update(phase="restore_active", active_nonce=snapshot["restore_nonce"], action="restore")
        elif transition == "record_restore_upid":
            _require(
                snapshot["phase"] == "restore_active" and snapshot["action"] == "restore" and snapshot["upid"] is None,
                "phase_transition_invalid",
            )
            _require(request["evidence"] is None, "invalid_input")
            _require(_upid_matches(request["upid"], target_ref, "qmrestore"), "evidence_invalid")
            record["upid"] = request["upid"]
        elif transition == "awaiting_receipt":
            _require(
                snapshot["phase"] == "restore_active"
                and snapshot["action"] == "restore"
                and snapshot["upid"] is not None,
                "phase_transition_invalid",
            )
            _require(request["upid"] is None, "invalid_input")
            receipt = _restore_receipt(request["evidence"], snapshot)
            record.update(
                phase="awaiting_receipt",
                active_nonce=None,
                action=None,
                receipt_fingerprint=receipt,
                prior_authenticated_task_id=None,
            )
        elif transition == "start_cleanup":
            _require(snapshot["phase"] == "awaiting_receipt", "phase_transition_invalid")
            cleanup_nonce = fresh_nonce()
            _require(_nonce(cleanup_nonce) and cleanup_nonce != snapshot["restore_nonce"], "nonce_invalid")
            _require(request["upid"] is None, "invalid_input")
            task_id = _validate_restore_task_proof_shape(request["evidence"], snapshot, version)
            record.update(
                phase="cleanup_active",
                active_nonce=cleanup_nonce,
                action="delete",
                upid=None,
                prior_authenticated_task_id=task_id,
            )
        elif transition == "record_cleanup_upid":
            _require(
                snapshot["phase"] == "cleanup_active" and snapshot["action"] == "delete" and snapshot["upid"] is None,
                "phase_transition_invalid",
            )
            _require(request["evidence"] is None, "invalid_input")
            _require(_upid_matches(request["upid"], target_ref, "qmdelete"), "evidence_invalid")
            record["upid"] = request["upid"]
        elif transition == "cleaned":
            _require(
                snapshot["phase"] == "cleanup_active"
                and snapshot["action"] == "delete"
                and snapshot["upid"] is not None,
                "phase_transition_invalid",
            )
            _require(request["upid"] is None, "invalid_input")
            _delete_verification(request["evidence"], snapshot, target_ref)
            record.update(phase="cleaned", action=None)
        else:
            raise Refusal("phase_transition_invalid")

    validate_record(record)
    return {
        "result": "write",
        "expected_version": version + 1,
        "payload": {"data": record, "options": {"cas": cas}},
        "record": record,
    }


def verify_readback(
    plan: object, metadata_status: object, metadata_payload: object, data_status: object, data_payload: object
) -> dict[str, Any]:
    """Require the CAS write to be visible exactly at the expected KV version."""
    _require(isinstance(plan, dict) and plan.get("result") == "write", "invalid_input")
    _require(_is_int(plan.get("expected_version"), minimum=1), "invalid_input")
    actual, version = read_snapshot(metadata_status, metadata_payload, data_status, data_payload)
    expected = validate_record(plan.get("record"))
    _require(version == plan.get("expected_version") and actual == expected, "readback_mismatch")
    return {"result": "verified", "phase": expected["phase"], "version": version}


def _main() -> int:
    try:
        payload = json.load(sys.stdin)
        mode = payload.get("mode") if isinstance(payload, dict) else None
        if mode == "plan":
            result = plan_transition(
                *read_snapshot(
                    payload.get("metadata_status"),
                    payload.get("metadata"),
                    payload.get("data_status"),
                    payload.get("secret"),
                ),
                payload.get("request"),
            )
        elif mode == "readback":
            result = verify_readback(
                payload.get("plan"),
                payload.get("metadata_status"),
                payload.get("metadata"),
                payload.get("data_status"),
                payload.get("secret"),
            )
        else:
            raise Refusal("invalid_input")
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except Refusal as exc:
        print(json.dumps({"result": "refused", "reason": exc.reason}, sort_keys=True, separators=(",", ":")))
        return 2
    except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        print(json.dumps({"result": "refused", "reason": "invalid_input"}, sort_keys=True, separators=(",", ":")))
        return 2


if __name__ == "__main__":
    raise SystemExit(_main())
