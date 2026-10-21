#!/usr/bin/env python3
"""Closed PBS artifact and completed-task receipt contracts for o11y recovery."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

SCHEMA_VERSION = 1
ENVELOPE_KEYS = {
    "schema_version",
    "contract",
    "receipt_fingerprint",
    "status",
    "reason",
    "public_sha",
    "private_sha",
    "inventory_provenance",
    "inventory_fingerprint",
    "source_fingerprint",
    "artifact_fingerprint",
    "observed_start",
    "observed_end",
    "role_context",
    "evidence",
}
CONTRACTS = {
    "o11y-pbs-backup/v1": {
        "playbook": "platform/playbooks/backup-o11y-to-pbs.yml",
        "roles": {"primary": "pve_backup", "verification": "primary_pbs_verify", "destination": "not_applicable"},
        "evidence": {
            "backup_task_id", "backup_upid", "snapshot", "source_vmid", "source_node",
            "artifact_fingerprint", "playbook",
        },
    },
    "o11y-pbs-verify/v1": {
        "playbook": "platform/playbooks/verify-o11y-pbs-artifact.yml",
        "roles": {"primary": "pve_backup", "verification": "primary_pbs_verify", "destination": "not_applicable"},
        "evidence": {
            "backup_task_id", "backup_upid", "verification_upid", "verification_status", "snapshot",
            "source_vmid", "artifact_fingerprint", "playbook",
        },
    },
    "o11y-pbs-copy-sync/v1": {
        "playbook": "platform/playbooks/sync-o11y-protected-pbs-copy.yml",
        "roles": {"primary": "pve_backup", "verification": "primary_pbs_verify", "destination": "destination_pbs_pull"},
        "evidence": {
            "backup_task_id", "verify_task_id", "backup_upid", "verification_upid", "sync_upid",
            "verification_status", "snapshot", "artifact_fingerprint", "remove_vanished", "prune_enabled", "playbook",
        },
    },
    "o11y-pbs-copy-ready/v1": {
        "playbook": "platform/playbooks/verify-o11y-protected-pbs-copy.yml",
        "roles": {
            "primary": "pve_backup",
            "verification": "primary_pbs_verify",
            "destination": "destination_pbs_readback",
        },
        "evidence": {
            "sync_task_id", "snapshot", "artifact_fingerprint", "readback_status", "acl_separation",
            "pull_direction", "deletion_isolation", "maintenance_isolation", "playbook",
            "normalized_restore_locator", "destination_identity_fingerprint",
            "destination_storage_config_fingerprint",
        },
    },
}
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
REVISION = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
UPID = re.compile(
    r"UPID:[A-Za-z0-9_.-]+:[0-9A-Fa-f]+:[0-9A-Fa-f]+:[0-9A-Fa-f]+:"
    r"[A-Za-z0-9_-]+:[A-Za-z0-9_.-]*:[A-Za-z0-9_.@-]*:?\Z"
)
SAFE_REASONS = {
    "invalid_input", "inventory_unavailable", "inventory_mismatch", "task_missing", "task_not_successful",
    "task_binding_mismatch", "receipt_missing", "receipt_ambiguous", "receipt_stale", "output_invalid",
    "output_time_invalid", "output_too_large", "artifact_invalid",
    "artifact_mismatch", "verification_incomplete", "copy_incomplete", "readback_incomplete",
}
BACKUP_TYPES = {"ct", "host", "vm"}
CRYPT_MODES = {"none", "encrypt", "sign-only"}
MANIFEST_KEYS = {"backup-type", "backup-id", "backup-time", "files", "unprotected", "signature"}
FILE_KEYS = {"filename", "crypt-mode", "size", "csum"}
FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}\.(?:fidx|didx|blob)\Z")
CHECKSUM = re.compile(r"[0-9a-f]{64}\Z")
MAX_OUTPUT_BYTES = 1024 * 1024
MAX_OUTPUT_ROWS = 5000
RESTORE_RECEIPT_KEYS = {
    "type", "receipt_fingerprint", "bindings", "restore_upid", "target_state", "pending_action",
    "completed_actions", "coordination_record", "coordination_version", "artifact_config",
    "checker_sha256", "checker_receipt_fingerprint", "guest_execution_pid", "data_checks",
    "isolation_config_sha256",
}
CUSTOM_RECEIPTS = {"restore_receipt/v3": RESTORE_RECEIPT_KEYS}
CUSTOM_LAUNCH_KEYS = {
    "restore_receipt/v3": {
        frozenset({
            "expected_repository_sha", "backup_task_id", "pbs_verify_task_id",
            "copy_sync_task_id", "copy_ready_task_id",
        }),
        frozenset({"expected_repository_sha", "restore_task_id", "expected_receipt_sha"}),
    }
}


class Refusal(ValueError):
    """A bounded refusal category that does not include request or response data."""

    def __init__(self, reason: str) -> None:
        self.reason = reason if reason in SAFE_REASONS else "invalid_input"
        super().__init__(self.reason)


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise Refusal(reason)


def _revision(value: object) -> bool:
    return isinstance(value, str) and REVISION.fullmatch(value) is not None


def _sha(value: object) -> bool:
    return isinstance(value, str) and SHA256.fullmatch(value) is not None


def receipt_fingerprint(receipt: Mapping[str, object]) -> str:
    """Hash the complete receipt body without its self-referential fingerprint."""
    body = {key: value for key, value in receipt.items() if key != "receipt_fingerprint"}
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def seal_receipt(receipt: Mapping[str, object]) -> dict[str, object]:
    """Return a copy carrying the canonical fingerprint consumed by validators."""
    _require(isinstance(receipt, Mapping) and "receipt_fingerprint" not in receipt, "invalid_input")
    result = dict(receipt)
    result["receipt_fingerprint"] = receipt_fingerprint(result)
    return result


def restore_receipt_fingerprint(receipt: Mapping[str, object]) -> str:
    """Hash restore receipt body with the coordination pointer normalized to null."""
    body = {key: value for key, value in receipt.items() if key != "receipt_fingerprint"}
    coordination = body.get("coordination_record")
    _require(isinstance(coordination, Mapping), "invalid_input")
    normalized_coordination = dict(coordination)
    _require("receipt_fingerprint" in normalized_coordination, "invalid_input")
    normalized_coordination["receipt_fingerprint"] = None
    body["coordination_record"] = normalized_coordination
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def seal_restore_receipt(receipt: Mapping[str, object]) -> dict[str, object]:
    """Seal both restore receipt pointers over the normalized closed body."""
    _require(isinstance(receipt, Mapping) and "receipt_fingerprint" not in receipt, "invalid_input")
    result = dict(receipt)
    coordination = result.get("coordination_record")
    _require(isinstance(coordination, Mapping), "invalid_input")
    normalized_coordination = dict(coordination)
    normalized_coordination["receipt_fingerprint"] = None
    result["coordination_record"] = normalized_coordination
    digest = restore_receipt_fingerprint(result)
    normalized_coordination["receipt_fingerprint"] = digest
    result["coordination_record"] = normalized_coordination
    result["receipt_fingerprint"] = digest
    return result


def _duplicate_rejecting_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        _require(key not in result, "artifact_invalid")
        result[key] = value
    return result


def parse_manifest(data: str | bytes) -> dict[str, object]:
    """Parse the official PBS manifest JSON shape and reject duplicate object keys."""
    try:
        raw = json.loads(data, object_pairs_hook=_duplicate_rejecting_object)
    except Refusal:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        raise Refusal("artifact_invalid") from None
    _require(isinstance(raw, dict) and set(raw) <= MANIFEST_KEYS, "artifact_invalid")
    _require({"backup-type", "backup-id", "backup-time", "files"} <= set(raw), "artifact_invalid")
    _require(isinstance(raw["backup-type"], str) and raw["backup-type"] in BACKUP_TYPES, "artifact_invalid")
    _require(
        isinstance(raw["backup-id"], str)
        and re.fullmatch(r"[A-Za-z0-9_.@-]{1,128}", raw["backup-id"]),
        "artifact_invalid",
    )
    _require(type(raw["backup-time"]) is int and raw["backup-time"] > 0, "artifact_invalid")
    _require(isinstance(raw["files"], list) and bool(raw["files"]), "artifact_invalid")
    files = []
    seen = set()
    for item in raw["files"]:
        _require(isinstance(item, dict) and set(item) <= FILE_KEYS, "artifact_invalid")
        _require(FILE_KEYS <= set(item), "artifact_invalid")
        filename, crypt_mode, size, csum = item["filename"], item["crypt-mode"], item["size"], item["csum"]
        _require(isinstance(filename, str) and FILE_NAME.fullmatch(filename) is not None, "artifact_invalid")
        _require(filename not in seen, "artifact_invalid")
        seen.add(filename)
        _require(isinstance(crypt_mode, str) and crypt_mode in CRYPT_MODES, "artifact_invalid")
        _require(type(size) is int and size > 0, "artifact_invalid")
        _require(isinstance(csum, str) and CHECKSUM.fullmatch(csum) is not None, "artifact_invalid")
        files.append({"filename": filename, "crypt_mode": crypt_mode, "size": size, "csum": csum})
    return {
        "backup_type": raw["backup-type"],
        "backup_id": raw["backup-id"],
        "backup_time": raw["backup-time"],
        "files": sorted(files, key=lambda item: str(item["filename"])),
    }


def artifact_fingerprint(manifest: Mapping[str, object] | str | bytes) -> str:
    """Hash only stable PBS snapshot identity and verified file metadata."""
    if isinstance(manifest, (str, bytes)):
        normalized = parse_manifest(manifest)
    elif isinstance(manifest, Mapping) and "backup-type" in manifest:
        normalized = parse_manifest(json.dumps(manifest, separators=(",", ":")))
    else:
        normalized = manifest
    _require(isinstance(normalized, Mapping), "artifact_invalid")
    _require(set(normalized) == {"backup_type", "backup_id", "backup_time", "files"}, "artifact_invalid")
    # Reuse the wire validator so Python callers cannot bypass duplicate, type,
    # checksum, filename, or canonical ordering requirements.
    wire = {
        "backup-type": normalized.get("backup_type"),
        "backup-id": normalized.get("backup_id"),
        "backup-time": normalized.get("backup_time"),
        "files": [
            {
                "filename": item.get("filename"),
                "crypt-mode": item.get("crypt_mode"),
                "size": item.get("size"),
                "csum": item.get("csum"),
            }
            for item in normalized.get("files", [])
            if isinstance(item, Mapping)
        ] if isinstance(normalized.get("files"), list) else normalized.get("files"),
    }
    stable = parse_manifest(json.dumps(wire, separators=(",", ":")))
    encoded = json.dumps(
        {"contract": "pbs-artifact/v1", **stable},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def clean_refusal(contract: str, public_sha: str, private_sha: str, reason: str) -> dict[str, object]:
    """Return a closed, content-free refusal envelope for a known workflow."""
    _require(contract in CONTRACTS and _revision(public_sha) and _revision(private_sha), "invalid_input")
    safe_reason = reason if reason in SAFE_REASONS else "invalid_input"
    return seal_receipt({
        "schema_version": SCHEMA_VERSION,
        "contract": contract,
        "status": "refused",
        "reason": safe_reason,
        "public_sha": public_sha,
        "private_sha": private_sha,
        "inventory_provenance": "unverified",
        "inventory_fingerprint": None,
        "source_fingerprint": None,
        "artifact_fingerprint": None,
        "observed_start": None,
        "observed_end": None,
        "role_context": dict(CONTRACTS[contract]["roles"]),
        "evidence": {},
    })


def _parse_time(value: object) -> datetime:
    _require(isinstance(value, str), "invalid_input")
    match = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?(Z|\+00:00)", value)
    _require(match is not None, "invalid_input")
    stamp = match.group(1) + ("." + match.group(2)[:6] if match.group(2) else "") + "+00:00"
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        raise Refusal("invalid_input") from None
    _require(parsed.tzinfo is not None, "invalid_input")
    return parsed.astimezone(UTC)


def _positive_id(value: object) -> bool:
    return type(value) is int and value > 0


def _validate_snapshot(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"backup_type", "backup_id", "backup_time"}
        and value["backup_type"] == "vm"
        and isinstance(value["backup_id"], str)
        and re.fullmatch(r"[1-9][0-9]{0,8}", value["backup_id"]) is not None
        and type(value["backup_time"]) is int
        and value["backup_time"] > 0
    )


def _validate_restore_locator(locator: object, snapshot: object) -> bool:
    selector_keys = {"volid", "backend", "format", "creation_time_epoch", "size_bytes"}
    if not isinstance(locator, dict) or set(locator) != selector_keys | {"storage_id"}:
        return False
    volid = locator.get("volid")
    match = re.fullmatch(
        r"(?P<storage>[A-Za-z0-9._-]{1,64}):backup/vm/(?P<vmid>[1-9][0-9]{0,8})/"
        r"(?P<timestamp>[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z)",
        volid if isinstance(volid, str) else "",
    )
    if match is None or not _validate_snapshot(snapshot):
        return False
    try:
        timestamp = datetime.strptime(match.group("timestamp"), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return False
    return (
        locator.get("storage_id") == match.group("storage")
        and locator.get("backend") == "pbs"
        and locator.get("format") == "pbs-vm"
        and type(locator.get("creation_time_epoch")) is int
        and locator["creation_time_epoch"] == int(timestamp.timestamp()) == snapshot["backup_time"]
        and match.group("vmid") == snapshot["backup_id"]
        and type(locator.get("size_bytes")) is int
        and locator["size_bytes"] > 0
    )


def validate_receipt(
    receipt: object,
    *,
    contract: str,
    public_sha: str,
    private_sha: str,
    now: datetime | None = None,
    expected_artifact_fingerprint: str | None = None,
) -> dict[str, object]:
    """Validate a fresh completed receipt against one exact workflow contract."""
    _require(contract in CONTRACTS and _revision(public_sha) and _revision(private_sha), "invalid_input")
    _require(isinstance(receipt, dict) and set(receipt) == ENVELOPE_KEYS, "invalid_input")
    _require(receipt["schema_version"] == SCHEMA_VERSION and type(receipt["schema_version"]) is int, "invalid_input")
    _require(receipt["contract"] == contract and receipt["status"] == "complete" and receipt["reason"] == "none", "task_binding_mismatch")
    _require(receipt["public_sha"] == public_sha and receipt["private_sha"] == private_sha, "task_binding_mismatch")
    _require(receipt["inventory_provenance"] == "exact_bytes_match", "inventory_unavailable")
    _require(_sha(receipt["inventory_fingerprint"]) and _sha(receipt["source_fingerprint"]), "inventory_unavailable")
    artifact = receipt["artifact_fingerprint"]
    _require(_sha(artifact), "artifact_invalid")
    if expected_artifact_fingerprint is not None:
        _require(_sha(expected_artifact_fingerprint) and artifact == expected_artifact_fingerprint, "artifact_mismatch")
    _require(_sha(receipt["receipt_fingerprint"]), "invalid_input")
    _require(receipt_fingerprint(receipt) == receipt["receipt_fingerprint"], "invalid_input")
    started, ended = _parse_time(receipt["observed_start"]), _parse_time(receipt["observed_end"])
    current = (now or datetime.now(UTC)).astimezone(UTC)
    _require(started <= ended <= current + timedelta(minutes=5), "invalid_input")
    _require(ended >= current - timedelta(hours=24), "receipt_stale")
    contract_schema = CONTRACTS[contract]
    _require(receipt["role_context"] == contract_schema["roles"], "task_binding_mismatch")
    evidence = receipt["evidence"]
    _require(isinstance(evidence, dict) and set(evidence) == contract_schema["evidence"], "invalid_input")
    _require(evidence.get("playbook") == contract_schema["playbook"], "task_binding_mismatch")
    _require(evidence.get("artifact_fingerprint") == artifact, "artifact_mismatch")
    if "snapshot" in evidence:
        _require(_validate_snapshot(evidence["snapshot"]), "artifact_invalid")
    for key in ("backup_task_id", "verify_task_id", "sync_task_id"):
        if key in evidence:
            _require(_positive_id(evidence[key]), "task_binding_mismatch")
    for key in ("backup_upid", "verification_upid", "sync_upid"):
        if key in evidence:
            _require(isinstance(evidence[key], str) and UPID.fullmatch(evidence[key]) is not None, "task_binding_mismatch")
    if contract in {"o11y-pbs-verify/v1", "o11y-pbs-copy-sync/v1"}:
        _require(evidence.get("verification_status") == "OK", "verification_incomplete")
    if contract == "o11y-pbs-copy-sync/v1":
        _require(evidence.get("remove_vanished") is False and evidence.get("prune_enabled") is False, "copy_incomplete")
    if contract == "o11y-pbs-copy-ready/v1":
        expected = {
            "readback_status": "readable", "acl_separation": "verified", "pull_direction": "destination_initiated",
            "deletion_isolation": "verified", "maintenance_isolation": "verified",
        }
        _require(all(evidence.get(key) == value for key, value in expected.items()), "readback_incomplete")
        locator = evidence.get("normalized_restore_locator")
        _require(
            _validate_restore_locator(locator, evidence.get("snapshot")),
            "readback_incomplete",
        )
        _require(
            _sha(evidence.get("destination_identity_fingerprint"))
            and _sha(evidence.get("destination_storage_config_fingerprint")),
            "readback_incomplete",
        )
    return receipt


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        _require(key not in result, "output_invalid")
        result[key] = value
    return result


def _objects(text: str):
    """Yield complete JSON objects embedded in joined Semaphore output."""
    start = None
    depth = 0
    quoted = escaped = False
    for index, char in enumerate(text):
        if start is None:
            if char == "{":
                start, depth, quoted, escaped = index, 1, False, False
            continue
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    value = json.loads(text[start:index + 1], object_pairs_hook=_unique_object)
                    yield value
                    if isinstance(value, dict):
                        for nested in value.values():
                            if isinstance(nested, str):
                                yield from _objects(nested)
                except (json.JSONDecodeError, UnicodeError, Refusal):
                    pass
                start = None


def _extract_receipt(output: str, selector: str, expected: str, expected_keys: set[str]) -> dict[str, object] | None:
    candidates = [
        value for value in _objects(re.sub(r"\x1b\[[0-9;]*m", "", output))
        if isinstance(value, dict) and set(value) == expected_keys and value.get(selector) == expected
    ]
    if len(candidates) > 1:
        raise Refusal("receipt_ambiguous")
    return candidates[0] if candidates else None


def validate_completed_output(
    task: object,
    output_rows: object,
    *,
    public_sha: str,
    expected_project_id: int,
    expected_launch: Mapping[str, object],
    selector: str,
    expected_selector: str,
    expected_receipt_keys: set[str],
    allowed_launch_keys: set[str],
    expected_task_id: int | None = None,
    expected_template_id: int | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Authenticate task metadata and reconstruct one exact receipt from its ordered output rows."""
    _require(isinstance(task, dict) and selector in {"contract", "type"}, "invalid_input")
    _require(expected_receipt_keys in (ENVELOPE_KEYS, RESTORE_RECEIPT_KEYS), "invalid_input")
    _require(expected_selector in CONTRACTS or expected_selector in CUSTOM_RECEIPTS, "invalid_input")
    _require(_revision(public_sha), "invalid_input")
    if selector == "contract":
        _require(allowed_launch_keys == {"expected_repository_sha", "site_config_base_sha"}, "invalid_input")
    else:
        _require(frozenset(allowed_launch_keys) in CUSTOM_LAUNCH_KEYS[expected_selector], "invalid_input")
        _require(expected_receipt_keys == CUSTOM_RECEIPTS[expected_selector], "invalid_input")
    task_id, project_id = task.get("id"), task.get("project_id")
    _require(_positive_id(task_id) and type(project_id) is int and project_id == expected_project_id, "task_binding_mismatch")
    _require(expected_task_id is None or task_id == expected_task_id, "task_binding_mismatch")
    _require(task.get("status") == "success" and task.get("commit_hash") == public_sha, "task_not_successful")
    _require(_revision(task.get("commit_hash")), "task_binding_mismatch")
    created, task_start, task_end = (
        _parse_time(task.get("created")), _parse_time(task.get("start")), _parse_time(task.get("end"))
    )
    current = (now or datetime.now(UTC)).astimezone(UTC)
    _require(created <= task_start < task_end <= current, "task_binding_mismatch")
    _require(type(task.get("template_id")) is int and task["template_id"] > 0, "task_binding_mismatch")
    _require(expected_template_id is None or task["template_id"] == expected_template_id, "task_binding_mismatch")
    launch = task.get("environment")
    if isinstance(launch, str):
        try:
            launch = json.loads(launch, object_pairs_hook=_unique_object)
        except (json.JSONDecodeError, Refusal):
            raise Refusal("task_binding_mismatch") from None
    _require(isinstance(launch, dict), "task_binding_mismatch")
    _require(set(launch) <= allowed_launch_keys, "task_binding_mismatch")
    _require(isinstance(expected_launch, Mapping) and launch == dict(expected_launch), "task_binding_mismatch")
    _require(launch.get("expected_repository_sha") == public_sha, "task_binding_mismatch")
    params = task.get("params")
    if isinstance(params, str):
        try:
            params = json.loads(params, object_pairs_hook=_unique_object)
        except (json.JSONDecodeError, Refusal):
            raise Refusal("task_binding_mismatch") from None
    _require(params in (None, {"diff": False, "dry_run": False}), "task_binding_mismatch")
    _require(task.get("playbook") == "", "task_binding_mismatch")
    _require(isinstance(output_rows, list) and 1 <= len(output_rows) <= MAX_OUTPUT_ROWS, "output_invalid")
    chunks = []
    used = 0
    previous_id = 0
    zero_id_rows = None
    previous_time = None
    row_timestamps = []
    for row in output_rows:
        _require(
            isinstance(row, dict)
            and type(row.get("id")) is int
            and row["id"] >= 0
            and row.get("task_id") == task_id
            and row.get("stage_id") is None
            and isinstance(row.get("output"), str),
            "output_invalid",
        )
        is_zero_id = row["id"] == 0
        _require(
            (zero_id_rows is None or zero_id_rows == is_zero_id)
            and (is_zero_id or row["id"] > previous_id),
            "output_invalid",
        )
        zero_id_rows = is_zero_id
        if not is_zero_id:
            previous_id = row["id"]
        stamp = _parse_time(row.get("time"))
        _require(created <= stamp <= task_end and stamp <= current, "output_time_invalid")
        _require(previous_time is None or stamp >= previous_time, "output_time_invalid")
        previous_time = stamp
        row_timestamps.append(stamp)
        output = row["output"]
        used += len(output.encode("utf-8"))
        _require(used <= MAX_OUTPUT_BYTES, "output_too_large")
        chunks.append(output)
    joined = "\n".join(chunks)
    _require(len(joined.encode("utf-8")) <= MAX_OUTPUT_BYTES, "output_too_large")
    _require(row_timestamps and min(row_timestamps) >= task_start, "output_time_invalid")
    receipt = _extract_receipt(joined, selector, expected_selector, expected_receipt_keys)
    _require(receipt is not None, "receipt_missing")
    _require(_sha(receipt.get("receipt_fingerprint")), "receipt_missing")
    if selector == "contract":
        _require(receipt_fingerprint(receipt) == receipt["receipt_fingerprint"], "receipt_missing")
    else:
        _require(
            restore_receipt_fingerprint(receipt) == receipt["receipt_fingerprint"]
            and receipt["coordination_record"].get("receipt_fingerprint") == receipt["receipt_fingerprint"],
            "receipt_missing",
        )
    if selector == "contract":
        _require(
            _parse_time(receipt["observed_start"]) >= task_start
            and _parse_time(receipt["observed_end"]) <= task_end,
            "task_binding_mismatch",
        )
    else:
        _require(receipt.get("type") == "restore_receipt/v3", "receipt_missing")
    task_metadata = {
        "id": task_id,
        "project_id": project_id,
        "template_id": task["template_id"],
        "commit_hash": task["commit_hash"],
        "status": task["status"],
        "created": task["created"],
        "start": task["start"],
        "end": task["end"],
        "launch": {key: launch[key] for key in sorted(launch)},
    }
    return {"receipt": receipt, "task_metadata": task_metadata}


def validate_completed_task(
    task: object,
    output_rows: object,
    *,
    contract: str,
    public_sha: str,
    private_sha: str,
    playbook: str,
    expected_project_id: int,
    expected_task_id: int | None = None,
    expected_launch: Mapping[str, object] | None = None,
    now: datetime | None = None,
    expected_artifact_fingerprint: str | None = None,
) -> dict[str, object]:
    """Bind one fresh shared-envelope receipt to authenticated Semaphore task metadata."""
    _require(contract in CONTRACTS and playbook == CONTRACTS[contract]["playbook"], "invalid_input")
    result = validate_completed_output(
        task,
        output_rows,
        public_sha=public_sha,
        expected_project_id=expected_project_id,
        expected_launch=expected_launch or {},
        selector="contract",
        expected_selector=contract,
        expected_receipt_keys=ENVELOPE_KEYS,
        allowed_launch_keys={"expected_repository_sha", "site_config_base_sha"},
        expected_task_id=expected_task_id,
        now=now,
    )
    receipt = validate_receipt(
        result["receipt"],
        contract=contract,
        public_sha=public_sha,
        private_sha=private_sha,
        now=now,
        expected_artifact_fingerprint=expected_artifact_fingerprint,
    )
    _require(receipt["evidence"].get("playbook") == playbook, "task_binding_mismatch")
    result["receipt"] = receipt
    return result


def main() -> int:
    """Small stdin/stdout interface used by playbooks; raw failures never escape."""
    import sys

    try:
        request = json.load(sys.stdin)
        _require(isinstance(request, dict), "invalid_input")
        operation = request.get("operation")
        if operation == "fingerprint":
            result: object = {"artifact_fingerprint": artifact_fingerprint(request.get("manifest"))}
        elif operation == "validate_receipt":
            result = validate_receipt(
                request.get("receipt"), contract=request.get("contract"), public_sha=request.get("public_sha"),
                private_sha=request.get("private_sha"), expected_artifact_fingerprint=request.get("expected_artifact_fingerprint"),
            )
        elif operation == "clean_refusal":
            result = clean_refusal(
                request.get("contract"), request.get("public_sha"), request.get("private_sha"), request.get("reason")
            )
        else:
            raise Refusal("invalid_input")
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except Refusal as exc:
        print(json.dumps({"status": "refused", "reason": exc.reason}, sort_keys=True, separators=(",", ":")))
        return 2
    except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        print(json.dumps({"status": "refused", "reason": "invalid_input"}, sort_keys=True, separators=(",", ":")))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
