#!/usr/bin/env python3
"""Plan strict versioned CAS transitions for one isolated o11y restore.

This helper validates journal structure. It does not authenticate Proxmox,
Semaphore, or QGA evidence; callers must obtain every fact through protected,
exact-bound APIs. Schema 1 records are intentionally not adopted or migrated.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sys
from typing import Any

from o11y_pbs_receipts import restore_receipt_fingerprint, seal_restore_receipt

SCHEMA_VERSION = 2
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
    "pending",
    "journal",
    "restore_upid",
    "receipt_fingerprint",
    "prior_authenticated_task_id",
}
PHASES = {"reserved", "restore_active", "awaiting_receipt", "cleanup_active", "cleaned"}
FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
REVISION = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
NONCE = re.compile(r"[0-9a-f]{64}\Z")
NODE = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")
UPID = re.compile(
    r"UPID:[A-Za-z0-9_.-]+:[0-9A-Fa-f]+:[0-9A-Fa-f]+:[0-9A-Fa-f]+:[A-Za-z0-9_-]+:[A-Za-z0-9_.-]*:[A-Za-z0-9_.@-]*:?\Z"
)
SAFE_REASONS = {
    "invalid_input",
    "metadata_missing",
    "metadata_tombstone",
    "data_missing",
    "record_shape_invalid",
    "record_binding_mismatch",
    "phase_transition_invalid",
    "nonce_invalid",
    "evidence_invalid",
    "cas_version_invalid",
    "readback_mismatch",
    "expected_snapshot_mismatch",
    "expected_version_mismatch",
}
JOURNAL_ACTIONS = {"restore", "isolate", "start", "guest_exec", "stop", "delete"}
HANDLE_TYPES = {"upid", "qga_pid", "response_sha256"}


class Refusal(ValueError):
    """A fixed, non-sensitive refusal category."""

    def __init__(self, reason: str) -> None:
        self.reason = reason if reason in SAFE_REASONS else "invalid_input"
        super().__init__(self.reason)


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise Refusal(reason)


def _is_int(value: object, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def _sha(value: object) -> bool:
    return isinstance(value, str) and FINGERPRINT.fullmatch(value) is not None


def _revision(value: object) -> bool:
    return isinstance(value, str) and REVISION.fullmatch(value) is not None


def _nonce(value: object) -> bool:
    return isinstance(value, str) and NONCE.fullmatch(value) is not None


def fresh_nonce() -> str:
    return secrets.token_hex(32)


def operation_identity(declaration: dict[str, Any]) -> str:
    fields = (
        "declaration_fingerprint",
        "target_fingerprint",
        "artifact_fingerprint",
        "public_sha",
        "private_inventory_sha",
    )
    encoded = json.dumps({key: declaration[key] for key in fields}, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def validate_declaration(declaration: object) -> dict[str, Any]:
    keys = {
        "declaration_fingerprint",
        "target_fingerprint",
        "artifact_fingerprint",
        "public_sha",
        "private_inventory_sha",
    }
    _require(isinstance(declaration, dict) and set(declaration) == keys, "invalid_input")
    _require(
        all(
            _sha(declaration[key]) for key in ("declaration_fingerprint", "target_fingerprint", "artifact_fingerprint")
        ),
        "invalid_input",
    )
    _require(_revision(declaration["public_sha"]) and _revision(declaration["private_inventory_sha"]), "invalid_input")
    return declaration


def _target_ref(value: object) -> dict[str, Any]:
    _require(isinstance(value, dict) and set(value) == {"node", "vmid"}, "invalid_input")
    _require(
        isinstance(value["node"], str) and NODE.fullmatch(value["node"]) and value["node"] not in {".", ".."},
        "invalid_input",
    )
    _require(_is_int(value["vmid"], 100) and value["vmid"] <= 999_999_999, "invalid_input")
    return value


def target_identity_fingerprint(target_ref: dict[str, Any]) -> str:
    body = json.dumps(
        {"node": target_ref["node"], "vmid": target_ref["vmid"]}, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(body).hexdigest()


def declaration_fingerprint(target: object) -> str:
    keys = {"vmid", "node", "storage_id", "network_mode", "headroom_percent"}
    _require(isinstance(target, dict) and set(target) == keys, "invalid_input")
    vmid = target["vmid"]
    if isinstance(vmid, str):
        _require(re.fullmatch(r"(?:0|[1-9][0-9]{0,8})", vmid) is not None, "invalid_input")
        vmid = int(vmid)
    _require(_is_int(vmid, 100) and vmid <= 999_999_999, "invalid_input")
    node, storage, network, headroom = (
        target["node"],
        target["storage_id"],
        target["network_mode"],
        target["headroom_percent"],
    )
    _require(isinstance(node, str) and NODE.fullmatch(node) and node not in {".", ".."}, "invalid_input")
    _require(
        isinstance(storage, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,64}", storage) and storage not in {".", ".."},
        "invalid_input",
    )
    _require(network == "disconnected" and _is_int(headroom, 30) and headroom <= 90, "invalid_input")
    canonical = {
        "vmid": vmid,
        "node": node,
        "storage_id": storage,
        "network_mode": network,
        "headroom_percent": headroom,
    }
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def _upid(value: object, target: dict[str, Any], operation: str) -> bool:
    if not isinstance(value, str) or UPID.fullmatch(value) is None:
        return False
    fields = value.split(":")
    return fields[1] == target["node"] and fields[5] == operation and fields[6] == str(target["vmid"])


def _expected_handle(action: str, handle_type: object, handle: object, target: dict[str, Any]) -> bool:
    expected = {"restore": "qmrestore", "start": "qmstart", "stop": "qmstop", "delete": "qmdestroy"}
    if action in expected:
        return handle_type == "upid" and _upid(handle, target, expected[action])
    if action == "guest_exec":
        return handle_type == "qga_pid" and _is_int(handle, 0)
    return action == "isolate" and handle_type == "response_sha256" and _sha(handle)


def _handle_shape(action: str, handle_type: object, handle: object) -> bool:
    if action in {"restore", "start", "stop", "delete"}:
        return handle_type == "upid" and isinstance(handle, str) and UPID.fullmatch(handle) is not None
    if action == "guest_exec":
        return handle_type == "qga_pid" and _is_int(handle, 0)
    return action == "isolate" and handle_type == "response_sha256" and _sha(handle)


def _action_effect_fingerprint(action: str, evidence: object, target: dict[str, Any], record: dict[str, Any]) -> str:
    fields = {
        "restore": {"type", "action", "node", "vmid", "task_state", "exit_status", "vm_state", "config_sha256"},
        "isolate": {"type", "action", "node", "vmid", "status", "isolated", "nic_count", "onboot", "config_sha256"},
        "start": {
            "type",
            "action",
            "node",
            "vmid",
            "task_state",
            "exit_status",
            "vm_state",
            "isolated",
            "config_sha256",
        },
        "guest_exec": {
            "type",
            "action",
            "node",
            "vmid",
            "pid",
            "exit_code",
            "checker_status",
            "checker_receipt_fingerprint",
            "output_sha256",
        },
        "stop": {
            "type",
            "action",
            "node",
            "vmid",
            "task_state",
            "exit_status",
            "vm_state",
            "isolated",
            "config_sha256",
        },
        "delete": {"type", "action", "node", "vmid", "task_state", "exit_status", "task_output_sha256"},
    }
    _require(isinstance(evidence, dict) and set(evidence) == fields[action], "evidence_invalid")
    _require(
        evidence["action"] == action and evidence["node"] == target["node"] and evidence["vmid"] == target["vmid"],
        "evidence_invalid",
    )
    if action == "restore":
        _require(
            evidence["type"] == "restore_effect/v2"
            and evidence["task_state"] == "stopped"
            and evidence["exit_status"] == "OK"
            and evidence["vm_state"] == "stopped",
            "evidence_invalid",
        )
        fingerprint = evidence["config_sha256"]
    elif action == "isolate":
        _require(
            evidence["type"] == "isolation_effect/v2"
            and evidence["status"] == 200
            and evidence["isolated"] is True
            and evidence["nic_count"] == 0
            and evidence["onboot"] == 0,
            "evidence_invalid",
        )
        fingerprint = evidence["config_sha256"]
    elif action == "start":
        _require(
            evidence["type"] == "start_effect/v2"
            and evidence["task_state"] == "stopped"
            and evidence["exit_status"] == "OK"
            and evidence["vm_state"] == "running"
            and evidence["isolated"] is True,
            "evidence_invalid",
        )
        fingerprint = evidence["config_sha256"]
        _require(
            len(record["journal"]) >= 2
            and record["journal"][1]["action"] == "isolate"
            and record["journal"][1]["effect_fingerprint"] == fingerprint,
            "evidence_invalid",
        )
    elif action == "guest_exec":
        _require(
            evidence["type"] == "guest_effect/v2"
            and evidence["pid"] == record["pending"]["handle"]
            and _is_int(evidence["exit_code"])
            and evidence["exit_code"] == 0
            and evidence["checker_status"] == "verified",
            "evidence_invalid",
        )
        fingerprint = evidence["checker_receipt_fingerprint"]
        _require(_sha(evidence["output_sha256"]), "evidence_invalid")
    elif action == "stop":
        _require(
            evidence["type"] == "stop_effect/v2"
            and evidence["task_state"] == "stopped"
            and evidence["exit_status"] == "OK"
            and evidence["vm_state"] == "stopped"
            and evidence["isolated"] is True,
            "evidence_invalid",
        )
        fingerprint = evidence["config_sha256"]
        _require(
            len(record["journal"]) >= 3
            and record["journal"][1]["action"] == "isolate"
            and record["journal"][1]["effect_fingerprint"] == fingerprint,
            "evidence_invalid",
        )
    else:
        _require(
            evidence["type"] == "delete_effect/v2"
            and evidence["task_state"] == "stopped"
            and evidence["exit_status"] == "OK",
            "evidence_invalid",
        )
        fingerprint = evidence["task_output_sha256"]
    _require(_sha(fingerprint), "evidence_invalid")
    return fingerprint


def _validate_event(event: object, target: dict[str, Any], *, completed: bool) -> None:
    base = {"action", "request_fingerprint", "handle_type", "handle", "effect_fingerprint"}
    _require(isinstance(event, dict) and set(event) == base, "record_shape_invalid")
    action = event["action"]
    _require(action in JOURNAL_ACTIONS and _sha(event["request_fingerprint"]), "record_shape_invalid")
    if completed:
        _require(_expected_handle(action, event["handle_type"], event["handle"], target), "record_shape_invalid")
        _require(_sha(event["effect_fingerprint"]), "record_shape_invalid")
    else:
        _require(
            event["handle_type"] is None and event["handle"] is None and event["effect_fingerprint"] is None,
            "record_shape_invalid",
        )


def validate_record(record: object) -> dict[str, Any]:
    _require(isinstance(record, dict) and set(record) == RECORD_KEYS, "record_shape_invalid")
    _require(
        type(record["schema_version"]) is int and record["schema_version"] == SCHEMA_VERSION, "record_shape_invalid"
    )
    for name in ("declaration_fingerprint", "target_fingerprint", "artifact_fingerprint", "operation_id"):
        _require(_sha(record[name]), "record_shape_invalid")
    for name in ("public_sha", "private_inventory_sha"):
        _require(_revision(record[name]), "record_shape_invalid")
    _require(record["generation"] == 1 and type(record["generation"]) is int, "record_shape_invalid")
    _require(record["phase"] in PHASES and isinstance(record["phase"], str), "record_shape_invalid")
    _require(_nonce(record["restore_nonce"]), "record_shape_invalid")
    _require(record["active_nonce"] is None or _nonce(record["active_nonce"]), "record_shape_invalid")
    _require(record["pending"] is None or isinstance(record["pending"], dict), "record_shape_invalid")
    _require(isinstance(record["journal"], list) and len(record["journal"]) <= 6, "record_shape_invalid")
    target = record.get("_target_ref")
    # The target reference is deliberately transient and not part of the stored schema.
    _require(target is None, "record_shape_invalid")
    for event in record["journal"]:
        _require(
            isinstance(event, dict)
            and set(event) == {"action", "request_fingerprint", "handle_type", "handle", "effect_fingerprint"},
            "record_shape_invalid",
        )
        _require(event["action"] in JOURNAL_ACTIONS and _sha(event["request_fingerprint"]), "record_shape_invalid")
        _require(_handle_shape(event["action"], event["handle_type"], event["handle"]), "record_shape_invalid")
        _require(_sha(event["effect_fingerprint"]), "record_shape_invalid")
    if record["pending"] is not None:
        pending = record["pending"]
        _require(
            isinstance(pending, dict)
            and set(pending) == {"action", "request_fingerprint", "handle_type", "handle", "effect_fingerprint"},
            "record_shape_invalid",
        )
        _require(pending["action"] in JOURNAL_ACTIONS and _sha(pending["request_fingerprint"]), "record_shape_invalid")
        if pending["handle_type"] is None:
            _require(pending["handle"] is None and pending["effect_fingerprint"] is None, "record_shape_invalid")
        else:
            _require(
                _handle_shape(pending["action"], pending["handle_type"], pending["handle"]), "record_shape_invalid"
            )
            _require(pending["effect_fingerprint"] is None, "record_shape_invalid")
    _require(
        record["restore_upid"] is None
        or (isinstance(record["restore_upid"], str) and UPID.fullmatch(record["restore_upid"])),
        "record_shape_invalid",
    )
    _require(record["receipt_fingerprint"] is None or _sha(record["receipt_fingerprint"]), "record_shape_invalid")
    _require(
        record["prior_authenticated_task_id"] is None or _is_int(record["prior_authenticated_task_id"], 1),
        "record_shape_invalid",
    )
    actions = [event["action"] for event in record["journal"]]
    if record["restore_upid"] is None:
        _require(not actions or actions[0] != "restore", "record_shape_invalid")
    else:
        _require(
            bool(record["journal"])
            and record["journal"][0]["action"] == "restore"
            and record["journal"][0]["handle"] == record["restore_upid"],
            "record_shape_invalid",
        )
    if record["phase"] == "reserved":
        _require(
            not actions
            and record["pending"] is None
            and record["active_nonce"] is None
            and record["restore_upid"] is None,
            "record_shape_invalid",
        )
    elif record["phase"] == "restore_active":
        _require(
            record["active_nonce"] == record["restore_nonce"] and record["receipt_fingerprint"] is None,
            "record_shape_invalid",
        )
        _require(
            actions == ["restore", "isolate", "start", "guest_exec", "stop"][: len(actions)], "record_shape_invalid"
        )
        expected_pending = (
            ["restore", "isolate", "start", "guest_exec", "stop"][len(actions)] if len(actions) < 5 else None
        )
        _require(
            record["pending"] is None
            if expected_pending is None
            else (record["pending"] is None or record["pending"]["action"] == expected_pending),
            "record_shape_invalid",
        )
    elif record["phase"] == "awaiting_receipt":
        _require(
            record["active_nonce"] is None
            and record["pending"] is None
            and actions == ["restore", "isolate", "start", "guest_exec", "stop"],
            "record_shape_invalid",
        )
        _require(
            isinstance(record["restore_upid"], str)
            and UPID.fullmatch(record["restore_upid"]) is not None
            and _sha(record["receipt_fingerprint"]),
            "record_shape_invalid",
        )
        _require(record["prior_authenticated_task_id"] is None, "record_shape_invalid")
    elif record["phase"] == "cleanup_active":
        _require(
            record["active_nonce"] != record["restore_nonce"] and _nonce(record["active_nonce"]), "record_shape_invalid"
        )
        _require(
            actions
            in (
                ["restore", "isolate", "start", "guest_exec", "stop"],
                ["restore", "isolate", "start", "guest_exec", "stop", "delete"],
            )
            and record["receipt_fingerprint"]
            and _is_int(record["prior_authenticated_task_id"], 1),
            "record_shape_invalid",
        )
        _require(
            record["pending"] is None or (record["pending"]["action"] == "delete" and len(actions) == 5),
            "record_shape_invalid",
        )
    else:
        _require(record["active_nonce"] is not None and record["pending"] is None, "record_shape_invalid")
        _require(actions == ["restore", "isolate", "start", "guest_exec", "stop", "delete"], "record_shape_invalid")
        _require(
            _sha(record["receipt_fingerprint"]) and _is_int(record["prior_authenticated_task_id"], 1),
            "record_shape_invalid",
        )
    if record["pending"] is not None:
        _require(
            record["pending"]["action"] == ("delete" if record["phase"] == "cleanup_active" else expected_pending),
            "record_shape_invalid",
        )
    _require(record["operation_id"] == operation_identity(record), "record_binding_mismatch")
    return record


def read_snapshot(
    metadata_status: object, metadata_payload: object, data_status: object, data_payload: object
) -> tuple[dict[str, Any] | None, int]:
    _require(type(metadata_status) is int and metadata_status in {200, 404}, "invalid_input")
    _require(type(data_status) is int and data_status in {200, 404}, "invalid_input")
    if metadata_status == data_status == 404:
        return None, 0
    _require(metadata_status == 200 and isinstance(metadata_payload, dict), "metadata_missing")
    version = metadata_payload.get("current_version")
    _require(_is_int(version, 1), "metadata_tombstone")
    current = metadata_payload.get("versions", {}).get(str(version))
    _require(
        isinstance(current, dict) and current.get("deletion_time") in (None, "") and current.get("destroyed") is False,
        "metadata_tombstone",
    )
    _require(data_status == 200 and isinstance(data_payload, dict), "data_missing")
    data = data_payload.get("data")
    _require(isinstance(data, dict) and set(data) == {"data", "metadata"}, "record_shape_invalid")
    _require(
        isinstance(data["metadata"], dict)
        and type(data["metadata"].get("version")) is int
        and data["metadata"]["version"] == version,
        "cas_version_invalid",
    )
    return validate_record(data["data"]), version


def _owner_nonce(record: dict[str, Any]) -> str:
    return record["active_nonce"] if record["phase"] in {"cleanup_active", "cleaned"} else record["restore_nonce"]


def _bindings_match(record: dict[str, Any], declaration: dict[str, Any]) -> None:
    _require(all(record[key] == value for key, value in declaration.items()), "record_binding_mismatch")
    _require(record["operation_id"] == operation_identity(declaration), "record_binding_mismatch")


def _restore_receipt(evidence: object, record: dict[str, Any], expected_version: int) -> str:
    keys = {
        "type",
        "receipt_fingerprint",
        "bindings",
        "restore_upid",
        "target_state",
        "pending_action",
        "completed_actions",
        "coordination_record",
        "coordination_version",
        "artifact_config",
        "checker_sha256",
        "checker_receipt_fingerprint",
        "guest_execution_pid",
        "data_checks",
        "isolation_config_sha256",
    }
    _require(
        isinstance(evidence, dict) and set(evidence) == keys and evidence["type"] == "restore_receipt/v3",
        "evidence_invalid",
    )
    expected = {
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
    _require(
        evidence["bindings"] == expected and evidence["restore_upid"] == record["restore_upid"], "evidence_invalid"
    )
    _require(
        evidence["target_state"] == "stopped"
        and evidence["pending_action"] is None
        and evidence["completed_actions"] == ["restore", "isolate", "start", "guest_exec", "stop"],
        "evidence_invalid",
    )
    _require(
        type(evidence["coordination_version"]) is int and evidence["coordination_version"] == expected_version,
        "evidence_invalid",
    )
    _require(
        _sha(evidence["checker_sha256"])
        and _sha(evidence["checker_receipt_fingerprint"])
        and _is_int(evidence["guest_execution_pid"], 1)
        and _sha(evidence["isolation_config_sha256"]),
        "evidence_invalid",
    )
    config = evidence["artifact_config"]
    _require(
        isinstance(config, dict)
        and set(config) == {"config_sha256", "disk_count", "disks", "nic_count", "nic_keys", "config_keys_sha256"}
        and _sha(config["config_sha256"])
        and _is_int(config["disk_count"], 1)
        and _is_int(config["nic_count"])
        and isinstance(config["disks"], list)
        and len(config["disks"]) == config["disk_count"]
        and isinstance(config["nic_keys"], list)
        and len(config["nic_keys"]) == config["nic_count"]
        and _sha(config["config_keys_sha256"]),
        "evidence_invalid",
    )
    data = evidence["data_checks"]
    data_keys = {
        "schema_version", "status", "reason", "volume_count", "volume_bindings_sha256",
        "container_bindings_sha256", "backend_count", "grafana_integrity", "grafana_migrations",
        "grafana_database_sha256", "backends", "receipt_fingerprint",
    }
    _require(
        isinstance(data, dict)
        and set(data) == data_keys
        and data["schema_version"] == 1
        and type(data["schema_version"]) is int
        and data["status"] == "verified"
        and data["reason"] == "restored_historical_data_verified"
        and data["volume_count"] == 5
        and data["backend_count"] == 4
        and data["grafana_integrity"] == "ok"
        and _is_int(data["grafana_migrations"])
        and all(_sha(data[key]) for key in (
            "volume_bindings_sha256", "container_bindings_sha256", "grafana_database_sha256", "receipt_fingerprint"
        ))
        and evidence["checker_receipt_fingerprint"] == data["receipt_fingerprint"],
        "evidence_invalid",
    )
    backends = data["backends"]
    _require(
        isinstance(backends, dict) and set(backends) == {"prometheus", "loki", "tempo", "pyroscope"},
        "evidence_invalid",
    )
    for item in backends.values():
        _require(
            isinstance(item, dict)
            and set(item) == {"expectation", "historical_records", "evidence_sha256", "readback_sha256"}
            and item["expectation"] in {"present", "quiet"}
            and _is_int(item["historical_records"])
            and (
                item["historical_records"] > 0
                if item["expectation"] == "present"
                else item["historical_records"] == 0
            )
            and _sha(item["evidence_sha256"])
            and _sha(item["readback_sha256"]),
            "evidence_invalid",
        )
    nested = evidence["coordination_record"]
    _require(isinstance(nested, dict) and set(nested) == RECORD_KEYS, "evidence_invalid")
    final_record = dict(record)
    final_record.update(phase="awaiting_receipt", active_nonce=None)
    final_record["receipt_fingerprint"] = None
    normalized_nested = dict(nested)
    normalized_nested["receipt_fingerprint"] = None
    _require(normalized_nested == final_record, "evidence_invalid")
    candidate = dict(evidence)
    candidate["coordination_record"] = dict(final_record)
    candidate["receipt_fingerprint"] = None
    fingerprint = restore_receipt_fingerprint(candidate)
    final_record["receipt_fingerprint"] = fingerprint
    candidate["coordination_record"] = final_record
    candidate["receipt_fingerprint"] = fingerprint
    _require(
        evidence == candidate
        and evidence["coordination_record"]["receipt_fingerprint"] == fingerprint
        and _sha(fingerprint),
        "evidence_invalid",
    )
    validate_record(final_record)
    return fingerprint


def _restore_task_proof(evidence: object, record: dict[str, Any], version: int) -> int:
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
    _require(
        isinstance(evidence, dict) and set(evidence) == keys and evidence["type"] == "prior_restore_task_proof/v2",
        "evidence_invalid",
    )
    _require(
        _is_int(evidence["task_id"], 1)
        and evidence["task_status"] == "success"
        and _sha(evidence["output_fingerprint"]),
        "evidence_invalid",
    )
    expected = {
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
    expected["restore_upid"] = record["restore_upid"]
    _require(
        evidence["bindings"] == expected and evidence["receipt_fingerprint"] == record["receipt_fingerprint"],
        "evidence_invalid",
    )
    _require(
        evidence["coordination_record"] == record
        and type(evidence["coordination_version"]) is int
        and evidence["coordination_version"] == version,
        "evidence_invalid",
    )
    return evidence["task_id"]


def plan_transition(snapshot: dict[str, Any] | None, version: int, request: object) -> dict[str, Any]:
    keys = {
        "transition",
        "declaration",
        "target_ref",
        "expected_record",
        "expected_version",
        "nonce",
        "evidence",
        "action",
        "request_fingerprint",
        "handle_type",
        "handle",
        "effect_fingerprint",
    }
    _require(isinstance(request, dict) and set(request) == keys, "invalid_input")
    declaration = validate_declaration(request["declaration"])
    target = _target_ref(request["target_ref"])
    _require(target_identity_fingerprint(target) == declaration["target_fingerprint"], "record_binding_mismatch")
    transition = request["transition"]
    allowed = {
        "reserve",
        "intent_restore",
        "intent_action",
        "record_handle",
        "verify_effect",
        "awaiting_receipt",
        "start_cleanup",
        "cleaned",
    }
    _require(transition in allowed and _is_int(version) and _is_int(request["expected_version"]), "invalid_input")
    if snapshot is None:
        _require(
            transition == "reserve"
            and version == request["expected_version"] == 0
            and request["expected_record"] is None
            and request["nonce"] is None,
            "phase_transition_invalid",
        )
        _require(
            all(
                request[key] is None
                for key in ("evidence", "action", "request_fingerprint", "handle_type", "handle", "effect_fingerprint")
            ),
            "invalid_input",
        )
        nonce = fresh_nonce()
        record = {
            **declaration,
            "schema_version": SCHEMA_VERSION,
            "operation_id": operation_identity(declaration),
            "generation": 1,
            "phase": "reserved",
            "restore_nonce": nonce,
            "active_nonce": None,
            "pending": None,
            "journal": [],
            "restore_upid": None,
            "receipt_fingerprint": None,
            "prior_authenticated_task_id": None,
        }
        cas = 0
    else:
        expected = request["expected_record"]
        _require(isinstance(expected, dict) and validate_record(expected) == snapshot, "expected_snapshot_mismatch")
        _require(request["expected_version"] == version, "expected_version_mismatch")
        _bindings_match(snapshot, declaration)
        expected_actions = ["restore", "isolate", "start", "guest_exec", "stop"]
        for index, event in enumerate(snapshot["journal"]):
            action = event["action"]
            _require(
                index < len(expected_actions) + (action == "delete")
                and action
                == (
                    "delete"
                    if snapshot["phase"] in {"cleanup_active", "cleaned"} and index == 5
                    else expected_actions[index]
                ),
                "record_shape_invalid",
            )
            _require(_expected_handle(action, event["handle_type"], event["handle"], target), "evidence_invalid")
        if snapshot["restore_upid"] is not None:
            _require(_upid(snapshot["restore_upid"], target, "qmrestore"), "evidence_invalid")
        if snapshot["pending"] is not None and snapshot["pending"]["handle_type"] is not None:
            _require(
                _expected_handle(
                    snapshot["pending"]["action"],
                    snapshot["pending"]["handle_type"],
                    snapshot["pending"]["handle"],
                    target,
                ),
                "evidence_invalid",
            )
        _require(request["nonce"] == _owner_nonce(snapshot), "nonce_invalid")
        record = dict(snapshot)
        record["journal"] = list(snapshot["journal"])
        cas = version
        if snapshot["phase"] == "cleaned" and transition == "reserve":
            return {"result": "unchanged", "record": snapshot, "version": version}
        if transition == "intent_restore":
            _require(
                snapshot["phase"] == "reserved" and snapshot["pending"] is None and not snapshot["journal"],
                "phase_transition_invalid",
            )
            _require(request["action"] == "restore" and _sha(request["request_fingerprint"]), "evidence_invalid")
            _require(
                all(request[key] is None for key in ("evidence", "handle_type", "handle", "effect_fingerprint")),
                "invalid_input",
            )
            record.update(
                phase="restore_active",
                active_nonce=snapshot["restore_nonce"],
                pending={
                    "action": "restore",
                    "request_fingerprint": request["request_fingerprint"],
                    "handle_type": None,
                    "handle": None,
                    "effect_fingerprint": None,
                },
            )
        elif transition == "intent_action":
            expected_actions = ["restore", "isolate", "start", "guest_exec", "stop"]
            expected_index = len(snapshot["journal"])
            _require(
                snapshot["phase"] == "restore_active" and snapshot["pending"] is None and expected_index in range(1, 5),
                "phase_transition_invalid",
            )
            _require(
                request["action"] == expected_actions[expected_index] and _sha(request["request_fingerprint"]),
                "evidence_invalid",
            )
            _require(
                all(request[key] is None for key in ("evidence", "handle_type", "handle", "effect_fingerprint")),
                "invalid_input",
            )
            record["pending"] = {
                "action": request["action"],
                "request_fingerprint": request["request_fingerprint"],
                "handle_type": None,
                "handle": None,
                "effect_fingerprint": None,
            }
        elif transition == "record_handle":
            pending = snapshot["pending"]
            _require(
                snapshot["phase"] in {"restore_active", "cleanup_active"} and isinstance(pending, dict),
                "phase_transition_invalid",
            )
            _require(
                request["action"] == pending["action"]
                and _expected_handle(pending["action"], request["handle_type"], request["handle"], target),
                "evidence_invalid",
            )
            _require(
                request["request_fingerprint"] == pending["request_fingerprint"]
                and request["evidence"] is None
                and request["effect_fingerprint"] is None,
                "evidence_invalid",
            )
            pending = dict(pending)
            pending.update(handle_type=request["handle_type"], handle=request["handle"])
            record["pending"] = pending
        elif transition == "verify_effect":
            pending = snapshot["pending"]
            _require(
                snapshot["phase"] in {"restore_active", "cleanup_active"}
                and isinstance(pending, dict)
                and pending["handle_type"] is not None,
                "phase_transition_invalid",
            )
            _require(
                request["action"] == pending["action"]
                and request["request_fingerprint"] == pending["request_fingerprint"],
                "evidence_invalid",
            )
            _require(
                request["handle_type"] == pending["handle_type"] and request["handle"] == pending["handle"],
                "evidence_invalid",
            )
            expected_effect = _action_effect_fingerprint(pending["action"], request["evidence"], target, snapshot)
            _require(request["effect_fingerprint"] == expected_effect, "evidence_invalid")
            action = pending["action"]
            _require(
                action
                == (
                    "delete"
                    if snapshot["phase"] == "cleanup_active"
                    else ["restore", "isolate", "start", "guest_exec", "stop"][len(snapshot["journal"])]
                ),
                "phase_transition_invalid",
            )
            event = dict(pending, effect_fingerprint=request["effect_fingerprint"])
            record["journal"].append(event)
            record["pending"] = None
            if action == "restore":
                _require(pending["handle_type"] == "upid", "evidence_invalid")
                record["restore_upid"] = pending["handle"]
        elif transition == "awaiting_receipt":
            _require(
                snapshot["phase"] == "restore_active"
                and snapshot["pending"] is None
                and [item["action"] for item in snapshot["journal"]]
                == ["restore", "isolate", "start", "guest_exec", "stop"],
                "phase_transition_invalid",
            )
            _require(
                all(
                    request[key] is None
                    for key in ("action", "request_fingerprint", "handle_type", "handle", "effect_fingerprint")
                ),
                "invalid_input",
            )
            receipt = _restore_receipt(request["evidence"], snapshot, version + 1)
            record.update(phase="awaiting_receipt", active_nonce=None, receipt_fingerprint=receipt)
        elif transition == "start_cleanup":
            _require(
                snapshot["phase"] == "awaiting_receipt" and snapshot["pending"] is None, "phase_transition_invalid"
            )
            task_id = _restore_task_proof(request["evidence"], snapshot, version)
            nonce = fresh_nonce()
            _require(_nonce(nonce) and nonce != snapshot["restore_nonce"], "nonce_invalid")
            record.update(
                phase="cleanup_active",
                active_nonce=nonce,
                prior_authenticated_task_id=task_id,
                pending={
                    "action": "delete",
                    "request_fingerprint": request["request_fingerprint"],
                    "handle_type": None,
                    "handle": None,
                    "effect_fingerprint": None,
                },
            )
            _require(
                _sha(request["request_fingerprint"])
                and all(request[key] is None for key in ("action", "handle_type", "handle", "effect_fingerprint")),
                "evidence_invalid",
            )
        elif transition == "cleaned":
            _require(
                snapshot["phase"] == "cleanup_active"
                and snapshot["pending"] is None
                and [item["action"] for item in snapshot["journal"]][-1:] == ["delete"],
                "phase_transition_invalid",
            )
            evidence = request["evidence"]
            _require(
                isinstance(evidence, dict)
                and set(evidence)
                == {"type", "task_id", "delete_upid", "task_state", "exit_status", "target_absence", "bindings"},
                "evidence_invalid",
            )
            _require(
                evidence["type"] == "pve_delete_verification/v2"
                and evidence["task_id"] == snapshot["prior_authenticated_task_id"],
                "evidence_invalid",
            )
            _require(
                evidence["delete_upid"] == snapshot["journal"][-1]["handle"]
                and evidence["task_state"] == "stopped"
                and evidence["exit_status"] == "OK",
                "evidence_invalid",
            )
            absence = evidence["target_absence"]
            _require(
                isinstance(absence, dict)
                and set(absence)
                == {"node", "vmid", "nextid_status", "nextid_echo", "node_config_status", "target_fingerprint"},
                "evidence_invalid",
            )
            _require(
                absence
                == {
                    "node": target["node"],
                    "vmid": target["vmid"],
                    "nextid_status": 200,
                    "nextid_echo": target["vmid"],
                    "node_config_status": 404,
                    "target_fingerprint": snapshot["target_fingerprint"],
                },
                "evidence_invalid",
            )
            expected_bindings = {
                key: snapshot[key]
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
            }
            _require(evidence["bindings"] == expected_bindings, "evidence_invalid")
            record.update(phase="cleaned", active_nonce=record["active_nonce"])
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
    _require(
        isinstance(plan, dict) and plan.get("result") == "write" and _is_int(plan.get("expected_version"), 1),
        "invalid_input",
    )
    actual, version = read_snapshot(metadata_status, metadata_payload, data_status, data_payload)
    expected = validate_record(plan.get("record"))
    _require(version == plan["expected_version"] and actual == expected, "readback_mismatch")
    return {"result": "verified", "phase": expected["phase"], "version": version}


def build_restore_receipt(record: object, version: object, fields: object) -> dict[str, Any]:
    """Build the closed private v3 receipt from the verified stopped journal."""
    _require(isinstance(record, dict), "invalid_input")
    record = validate_record(record)
    _require(_is_int(version, 1), "invalid_input")
    _require(record["phase"] == "restore_active" and record["pending"] is None, "phase_transition_invalid")
    _require(
        [event["action"] for event in record["journal"]] == ["restore", "isolate", "start", "guest_exec", "stop"],
        "phase_transition_invalid",
    )
    keys = {
        "artifact_config", "checker_sha256", "checker_receipt_fingerprint", "guest_execution_pid",
        "data_checks", "isolation_config_sha256",
    }
    _require(isinstance(fields, dict) and set(fields) == keys, "evidence_invalid")
    candidate = {
        "type": "restore_receipt/v3",
        "bindings": {
            key: record[key]
            for key in (
                "operation_id", "target_fingerprint", "artifact_fingerprint", "restore_nonce",
                "public_sha", "private_inventory_sha",
            )
        },
        "restore_upid": record["restore_upid"],
        "target_state": "stopped",
        "pending_action": None,
        "completed_actions": ["restore", "isolate", "start", "guest_exec", "stop"],
        "coordination_record": {
            **record,
            "phase": "awaiting_receipt",
            "active_nonce": None,
            "receipt_fingerprint": None,
        },
        "coordination_version": version + 1,
        **fields,
    }
    sealed = seal_restore_receipt(candidate)
    _restore_receipt(sealed, record, version + 1)
    return sealed


def _main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise Refusal("invalid_input")
        if payload.get("mode") == "plan":
            result = plan_transition(
                *read_snapshot(
                    payload.get("metadata_status"),
                    payload.get("metadata"),
                    payload.get("data_status"),
                    payload.get("secret"),
                ),
                payload.get("request"),
            )
        elif payload.get("mode") == "readback":
            result = verify_readback(
                payload.get("plan"),
                payload.get("metadata_status"),
                payload.get("metadata"),
                payload.get("data_status"),
                payload.get("secret"),
            )
        elif payload.get("mode") == "build_restore_receipt":
            _require(set(payload) == {"mode", "record", "version", "fields"}, "invalid_input")
            result = build_restore_receipt(payload.get("record"), payload.get("version"), payload.get("fields"))
        else:
            raise Refusal("invalid_input")
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except Refusal as exc:
        print(json.dumps({"result": "refused", "reason": exc.reason}, sort_keys=True, separators=(",", ":")))
        return 2
    except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        print('{"reason":"invalid_input","result":"refused"}')
        return 2


if __name__ == "__main__":
    raise SystemExit(_main())
