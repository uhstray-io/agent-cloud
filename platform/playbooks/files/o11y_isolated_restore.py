#!/usr/bin/env python3
"""Pure validators for exact PBS artifacts and isolated restore receipts."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from o11y_pbs_receipts import (
    CONTRACTS,
    RESTORE_RECEIPT_KEYS,
    Refusal as PBSRefusal,
    validate_completed_output,
    validate_completed_task,
    validate_receipt,
)
import o11y_restore_coordination as restore_coordination

VMID = re.compile(r"(?:0|[1-9][0-9]{0,8})\Z")
SAFE_ID = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")
MAX_INPUT_BYTES = 4 * 1024 * 1024
ALLOWED_CONFIG_KEYS = {
    "acpi",
    "agent",
    "balloon",
    "bios",
    "boot",
    "cores",
    "cpu",
    "description",
    "freeze",
    "hotplug",
    "keyboard",
    "kvm",
    "machine",
    "memory",
    "name",
    "numa",
    "onboot",
    "ostype",
    "scsihw",
    "sockets",
    "startup",
    "tablet",
    "tags",
    "vga",
    "vmid",
    "vmgenid",
    "watchdog",
}
SIZE = re.compile(r"(?:^|,)size=([0-9]+(?:\.[0-9]+)?)([KMGT])(?:$|,)", re.I)
MULTIPLIERS = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
DISK = re.compile(r"(?:ide|sata|scsi|virtio|efidisk|tpmstate)[0-9]+\Z")
FORBIDDEN_KEYS = {
    "args",
    "hookscript",
    "hostpci0",
    "hostpci1",
    "hostpci2",
    "hostpci3",
    "hostpci4",
    "hostpci5",
    "hostpci6",
    "hostpci7",
    "hostpci8",
    "hostpci9",
    "hostpci10",
    "hostpci11",
    "hostpci12",
    "hostpci13",
    "hostpci14",
    "hostpci15",
    "hostpci16",
    "hostpci17",
    "hostpci18",
    "hostpci19",
    "hostpci20",
    "hostpci21",
    "hostpci22",
    "hostpci23",
    "hostpci24",
    "hostpci25",
    "hostpci26",
    "hostpci27",
    "hostpci28",
    "hostpci29",
    "hostpci30",
    "hostpci31",
    "usb0",
    "usb1",
    "usb2",
    "usb3",
    "usb4",
    "usb5",
    "usb6",
    "usb7",
    "usb8",
    "usb9",
    "usb10",
    "usb11",
    "usb12",
    "usb13",
    "usb14",
    "usb15",
    "virtiofs0",
    "virtiofs1",
    "virtiofs2",
    "virtiofs3",
    "shared",
    "vmstatestorage",
    "ivshmem",
    "smbios1",
    "tpmstate0",
}
SAFE_REASONS = {
    "invalid_input",
    "artifact_config_invalid",
    "artifact_unsupported",
    "artifact_disk_incomplete",
    "task_binding_unknown",
    "task_binding_mismatch",
    "task_output_invalid",
    "task_receipt_invalid",
    "task_receipt_ambiguous",
    "receipt_binding_mismatch",
    "target_config_invalid",
    "task_chain_invalid",
    "artifact_stale",
    "restore_receipt_invalid",
}
PREREQUISITE_CONTRACTS = (
    "o11y-pbs-backup/v1",
    "o11y-pbs-verify/v1",
    "o11y-pbs-copy-sync/v1",
    "o11y-pbs-copy-ready/v1",
)
RESTORE_LAUNCH_KEYS = {
    "expected_repository_sha",
    "backup_task_id",
    "pbs_verify_task_id",
    "copy_sync_task_id",
    "copy_ready_task_id",
}
RESTORE_TASK_LAUNCH_KEYS = {
    "expected_repository_sha", "site_config_base_sha", "backup_task_id", "pbs_verify_task_id",
    "copy_sync_task_id", "copy_ready_task_id",
}


class Refusal(ValueError):
    """Consumer-owned fixed refusal vocabulary, including restore-specific gates."""

    def __init__(self, reason: str) -> None:
        self.reason = reason if reason in SAFE_REASONS else "invalid_input"
        super().__init__(self.reason)


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise Refusal(reason)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _strict_int(value: object, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def exact_artifact_config(value: object, *, source_vmid: int) -> dict:
    """Reject unsafe restore directives before the target reservation or write."""
    require(
        isinstance(value, dict)
        and set(value) == {"status", "data"}
        and value.get("status") == 200
        and isinstance(value.get("data"), str),
        "artifact_config_invalid",
    )
    require(_strict_int(source_vmid, 100) and source_vmid <= 999_999_999, "artifact_config_invalid")
    raw = value["data"]
    require(len(raw.encode("utf-8")) <= 256 * 1024 and len(raw.splitlines()) <= 4096, "artifact_config_invalid")
    parsed: dict[str, str] = {}
    nics = []
    disks = []
    try:
        for line in raw.splitlines():
            if not line or line.lstrip().startswith("#"):
                continue
            require(":" in line, "artifact_config_invalid")
            key, content = line.split(":", 1)
            key, content = key.strip(), content.strip()
            require(SAFE_ID.fullmatch(key) is not None and key not in parsed, "artifact_config_invalid")
            parsed[key] = content
            require(
                key in ALLOWED_CONFIG_KEYS or DISK.fullmatch(key) or re.fullmatch(r"net[0-9]+", key),
                "artifact_unsupported",
            )
            require(
                key not in FORBIDDEN_KEYS and not key.startswith(("hostpci", "usb", "virtiofs", "mp")),
                "artifact_unsupported",
            )
            require(not key.startswith("unused"), "artifact_disk_incomplete")
            if key == "vmid":
                require(VMID.fullmatch(content) is not None and int(content) == source_vmid, "artifact_config_invalid")
            if key.startswith("net") and key[3:].isdigit():
                nics.append(key)
                require("=" in content, "artifact_config_invalid")
            if DISK.fullmatch(key):
                require(re.search(r"(?:^|,)media=cdrom(?:$|,)", content) is None, "artifact_unsupported")
                options = content.split(":", 1)[-1]
                match = SIZE.findall(options)
                require(len(match) == 1, "artifact_disk_incomplete")
                amount = Decimal(match[0][0]) * MULTIPLIERS[match[0][1].upper()]
                require(amount > 0 and amount == amount.to_integral_value(), "artifact_disk_incomplete")
                backup = re.findall(r"(?:^|,)backup=([01])(?:$|,)", options)
                require(len(backup) <= 1 and (not backup or backup[0] == "1"), "artifact_disk_incomplete")
                disks.append({"device": key, "size_bytes": int(amount)})
    except (UnicodeEncodeError, InvalidOperation, ArithmeticError):
        raise Refusal("artifact_config_invalid") from None
    require(parsed.get("vmid") is not None and disks and len(disks) <= 128, "artifact_disk_incomplete")
    agent = parsed.get("agent", "")
    require(re.search(r"(?:^|,)enabled=1(?:,|$)", agent) is not None, "artifact_unsupported")
    require(all(name in parsed for name in nics), "artifact_config_invalid")
    return {
        "config_sha256": sha256(raw.encode("utf-8")),
        "disk_count": len(disks),
        "disks": sorted(disks, key=lambda item: item["device"]),
        "nic_count": len(nics),
        "nic_keys": sorted(nics),
        "config_keys_sha256": sha256("\n".join(sorted(parsed)).encode()),
    }


def validate_target_config(
    value: object,
    *,
    target_vmid: int,
    marker: str | None,
    isolated: bool,
    expected_disk_keys: list[str],
    storage_id: str,
) -> dict:
    """Validate read-back configuration and absence of host/network escape paths."""
    require(
        isinstance(value, dict) and value.get("status") == 200 and isinstance(value.get("data"), dict),
        "target_config_invalid",
    )
    require(type(isolated) is bool, "target_config_invalid")
    data = value["data"]
    if isolated:
        require(isinstance(marker, str) and 1 <= len(marker.encode("utf-8")) <= 128, "target_config_invalid")
        require(data.get("onboot") in (0, "0", False, None), "target_config_invalid")
        require(data.get("startup") in (None, ""), "target_config_invalid")
        require(data.get("description") == marker, "target_config_invalid")
    else:
        require(marker is None, "target_config_invalid")
    require(type(data.get("vmid")) is int and data["vmid"] == target_vmid, "target_config_invalid")
    require(
        isinstance(expected_disk_keys, list)
        and expected_disk_keys
        and all(isinstance(key, str) and DISK.fullmatch(key) for key in expected_disk_keys)
        and len(expected_disk_keys) == len(set(expected_disk_keys)),
        "target_config_invalid",
    )
    require(isinstance(storage_id, str) and SAFE_ID.fullmatch(storage_id) is not None, "target_config_invalid")
    disk_keys = sorted(key for key in data if DISK.fullmatch(key))
    require(disk_keys == sorted(expected_disk_keys), "target_config_invalid")
    for key in disk_keys:
        require(isinstance(data[key], str) and data[key].startswith(storage_id + ":"), "target_config_invalid")
    for key in data:
        require(
            key in ALLOWED_CONFIG_KEYS or DISK.fullmatch(key) or re.fullmatch(r"net[0-9]+", key) is not None,
            "target_config_invalid",
        )
        require(
            key not in FORBIDDEN_KEYS and not key.startswith(("hostpci", "usb", "virtiofs", "mp")),
            "target_config_invalid",
        )
        require(not key.startswith("unused"), "target_config_invalid")
        if isolated and key.startswith("net") and key[3:].isdigit():
            raise Refusal("target_config_invalid")
    return {
        "config_sha256": sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()),
        "disk_count": sum(bool(DISK.fullmatch(key)) for key in data),
        "disk_keys_sha256": sha256("\n".join(disk_keys).encode()),
        "nic_count": sum(key.startswith("net") and key[3:].isdigit() for key in data),
    }


def verify_task_proof(payload: object) -> dict:
    """Delegate task and receipt authentication to the shared closed contract."""
    keys = {
        "task",
        "output_rows",
        "contract",
        "public_sha",
        "private_sha",
        "playbook",
        "expected_project_id",
        "expected_task_id",
        "expected_launch",
        "expected_artifact_fingerprint",
    }
    require(isinstance(payload, dict) and set(payload) == keys, "invalid_input")
    try:
        result = validate_completed_task(
            payload["task"],
            payload["output_rows"],
            contract=payload["contract"],
            public_sha=payload["public_sha"],
            private_sha=payload["private_sha"],
            playbook=payload["playbook"],
            expected_project_id=payload["expected_project_id"],
            expected_task_id=payload["expected_task_id"],
            expected_launch=payload["expected_launch"],
            expected_artifact_fingerprint=payload["expected_artifact_fingerprint"],
        )
    except PBSRefusal as exc:
        raise Refusal(exc.reason) from None
    require(isinstance(result, dict) and set(result) == {"receipt", "task_metadata"}, "task_proof_invalid")
    return result


def _verify_prerequisite_chain(payload: object) -> dict:
    """Authenticate the exact ordered backup, verify, copy and readback task chain."""
    keys = {
        "public_sha",
        "private_sha",
        "expected_project_id",
        "expected_task_ids",
        "expected_launches",
        "task_proofs",
        "artifact_fingerprint",
        "private_copy_selector",
        "expected_copy_storage_id",
    }
    require(isinstance(payload, dict) and set(payload) == keys, "invalid_input")
    expected_contracts = set(PREREQUISITE_CONTRACTS)
    require(
        isinstance(payload["expected_task_ids"], dict) and set(payload["expected_task_ids"]) == expected_contracts,
        "task_chain_invalid",
    )
    require(
        isinstance(payload["expected_launches"], dict) and set(payload["expected_launches"]) == expected_contracts,
        "task_chain_invalid",
    )
    require(
        isinstance(payload["task_proofs"], dict) and set(payload["task_proofs"]) == expected_contracts,
        "task_chain_invalid",
    )
    selector = payload["private_copy_selector"]
    selector_keys = {"volid", "backend", "format", "creation_time_epoch", "size_bytes"}
    require(
        isinstance(selector, dict)
        and set(selector) == selector_keys
        and isinstance(selector.get("volid"), str)
        and isinstance(selector.get("backend"), str)
        and isinstance(selector.get("format"), str)
        and type(selector.get("creation_time_epoch")) is int
        and type(selector.get("size_bytes")) is int
        and isinstance(payload["expected_copy_storage_id"], str)
        and SAFE_ID.fullmatch(payload["expected_copy_storage_id"]) is not None
        and selector["volid"].split(":", 1)[0] == payload["expected_copy_storage_id"],
        "task_chain_invalid",
    )
    fingerprint = payload["artifact_fingerprint"]
    require(isinstance(fingerprint, str) and re.fullmatch(r"[0-9a-f]{64}", fingerprint), "task_chain_invalid")
    observed_fingerprint = None
    validated = {}
    ids = []
    receipts = {}
    task_id_chain = [payload["expected_task_ids"][contract] for contract in PREREQUISITE_CONTRACTS]
    for index, contract in enumerate(PREREQUISITE_CONTRACTS):
        task_id = payload["expected_task_ids"][contract]
        proof = payload["task_proofs"][contract]
        require(type(task_id) is int and task_id > 0, "task_chain_invalid")
        require(isinstance(proof, dict) and set(proof) == {"task", "receipt"}, "task_chain_invalid")
        expected_launch = {
            "expected_repository_sha": payload["public_sha"],
            "site_config_base_sha": payload["private_sha"],
        }
        if index >= 1:
            expected_launch["backup_task_id"] = task_id_chain[0]
        if index >= 2:
            expected_launch["verify_task_id"] = task_id_chain[1]
        if index >= 3:
            expected_launch["sync_task_id"] = task_id_chain[2]
        require(payload["expected_launches"][contract] == expected_launch, "task_chain_invalid")
        metadata = proof["task"]
        require(
            isinstance(metadata, dict)
            and set(metadata) == {
                "id", "project_id", "template_id", "commit_hash", "status", "created", "start", "end", "launch"
            }
            and metadata["id"] == task_id
            and metadata["project_id"] == payload["expected_project_id"]
            and metadata["commit_hash"] == payload["public_sha"]
            and metadata["status"] == "success"
            and metadata["launch"] == expected_launch,
            "task_chain_invalid",
        )
        try:
            receipt = validate_receipt(
                proof["receipt"],
                contract=contract,
                public_sha=payload["public_sha"],
                private_sha=payload["private_sha"],
                expected_artifact_fingerprint=fingerprint,
            )
        except PBSRefusal as exc:
            raise Refusal(exc.reason) from None
        evidence = receipt["evidence"]
        require(evidence.get("artifact_fingerprint") == fingerprint, "task_chain_invalid")
        ids.append(metadata["id"])
        receipts[contract] = receipt
        validated[contract] = metadata
        observed_fingerprint = observed_fingerprint or fingerprint

    require(ids == sorted(ids) and len(ids) == len(set(ids)), "task_chain_invalid")
    backup, verify, sync, ready = (receipts[item]["evidence"] for item in PREREQUISITE_CONTRACTS)
    locator = ready.get("normalized_restore_locator")
    require(
        isinstance(locator, dict)
        and set(locator) == selector_keys | {"storage_id"}
        and {key: locator[key] for key in selector_keys} == selector
        and locator["storage_id"] == payload["expected_copy_storage_id"],
        "task_chain_invalid",
    )
    require(
        verify["backup_task_id"] == ids[0]
        and verify["backup_upid"] == backup["backup_upid"]
        and verify["snapshot"] == backup["snapshot"],
        "task_chain_invalid",
    )
    require(
        sync["backup_task_id"] == ids[0]
        and sync["verify_task_id"] == ids[1]
        and sync["backup_upid"] == backup["backup_upid"]
        and sync["verification_upid"] == verify["verification_upid"]
        and sync["snapshot"] == backup["snapshot"],
        "task_chain_invalid",
    )
    require(ready["sync_task_id"] == ids[2] and ready["snapshot"] == backup["snapshot"], "task_chain_invalid")
    require(
        ready["snapshot"]["backup_time"] <= int(datetime.now(UTC).timestamp())
        and int(datetime.now(UTC).timestamp()) - ready["snapshot"]["backup_time"] <= 24 * 60 * 60,
        "artifact_stale",
    )
    return {
        "status": "verified",
        "artifact_fingerprint": observed_fingerprint,
        "snapshot": {key: backup["snapshot"][key] for key in ("backup_type", "backup_id", "backup_time")},
        "task_ids": ids,
        "task_metadata": validated,
        "readiness_evidence": ready,
    }


def verify_prerequisite_chain(payload: object) -> dict:
    try:
        return _verify_prerequisite_chain(payload)
    except PBSRefusal as exc:
        raise Refusal(exc.reason) from None


def verify_restore_task_proof(payload: object) -> dict:
    """Authenticate a prior restore task, exact launch and one closed v3 receipt."""
    keys = {
        "task",
        "output_rows",
        "public_sha",
        "expected_project_id",
        "expected_task_id",
        "expected_template_id",
        "expected_launch",
        "selector",
        "expected_selector",
        "expected_receipt_fingerprint",
    }
    require(isinstance(payload, dict) and set(payload) == keys, "invalid_input")
    require(
        payload["selector"] == "type" and payload["expected_selector"] == "restore_receipt/v3",
        "restore_receipt_invalid",
    )
    launch = payload["expected_launch"]
    require(
        isinstance(launch, dict)
        and set(launch) == RESTORE_TASK_LAUNCH_KEYS
        and launch["expected_repository_sha"] == payload["public_sha"]
        and isinstance(launch["site_config_base_sha"], str)
        and re.fullmatch(r"[0-9a-f]{40}", launch["site_config_base_sha"])
        and all(type(launch[key]) is int and launch[key] > 0 for key in RESTORE_TASK_LAUNCH_KEYS - {"expected_repository_sha"}),
        "restore_receipt_invalid",
    )
    try:
        result = validate_completed_output(
            payload["task"],
            payload["output_rows"],
            public_sha=payload["public_sha"],
            expected_project_id=payload["expected_project_id"],
            expected_playbook="platform/playbooks/restore-o11y-isolated.yml",
            expected_launch=payload["expected_launch"],
            selector="type",
            expected_selector="restore_receipt/v3",
            expected_receipt_keys=RESTORE_RECEIPT_KEYS,
            allowed_launch_keys=RESTORE_TASK_LAUNCH_KEYS,
            expected_task_id=payload["expected_task_id"],
            expected_template_id=payload["expected_template_id"],
        )
    except PBSRefusal as exc:
        raise Refusal(exc.reason) from None
    receipt = result["receipt"]
    require(receipt["receipt_fingerprint"] == payload["expected_receipt_fingerprint"], "restore_receipt_invalid")
    require(
        receipt["bindings"]["public_sha"] == payload["public_sha"]
        and receipt["bindings"]["private_inventory_sha"] == launch["site_config_base_sha"],
        "restore_receipt_invalid",
    )
    nested = receipt["coordination_record"]
    require(
        isinstance(nested, dict)
        and nested.get("phase") == "awaiting_receipt"
        and nested.get("active_nonce") is None
        and nested.get("pending") is None
        and type(receipt["coordination_version"]) is int
        and receipt["coordination_version"] > 1,
        "restore_receipt_invalid",
    )
    base_record = dict(nested)
    base_record.update(phase="restore_active", active_nonce=nested["restore_nonce"], receipt_fingerprint=None)
    try:
        restore_coordination._restore_receipt(receipt, base_record, receipt["coordination_version"])
    except restore_coordination.Refusal:
        raise Refusal("restore_receipt_invalid") from None
    result["output_fingerprint"] = hashlib.sha256(
        json.dumps(payload["output_rows"], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    return result


def verify_cleanup_inventory_pins(payload: object) -> dict:
    """Validate historical restore pin A and independently current cleanup pin B."""
    keys = {
        "restore_launch_private_sha",
        "restore_receipt_private_sha",
        "current_private_sha",
        "current_provenance_private_sha",
        "current_inventory_provenance",
        "current_inventory_fingerprint",
    }
    require(isinstance(payload, dict) and set(payload) == keys, "restore_receipt_invalid")
    historical = payload["restore_launch_private_sha"]
    historical_receipt = payload["restore_receipt_private_sha"]
    current = payload["current_private_sha"]
    require(
        isinstance(historical, str)
        and re.fullmatch(r"[0-9a-f]{40}", historical)
        and historical_receipt == historical
        and isinstance(current, str)
        and re.fullmatch(r"[0-9a-f]{40}", current)
        and payload["current_provenance_private_sha"] == current
        and payload["current_inventory_provenance"] == "exact_bytes_match"
        and isinstance(payload["current_inventory_fingerprint"], str)
        and re.fullmatch(r"[0-9a-f]{64}", payload["current_inventory_fingerprint"]),
        "restore_receipt_invalid",
    )
    return {
        "status": "verified",
        "historical_private_sha": historical,
        "current_private_sha": current,
        "pins_are_independent": True,
    }


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _main() -> int:
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        require(len(raw) <= MAX_INPUT_BYTES, "invalid_input")
        payload = json.loads(raw, object_pairs_hook=_unique_pairs)
        mode = payload.get("mode") if isinstance(payload, dict) else None
        if mode == "artifact_config":
            result = exact_artifact_config(payload.get("config"), source_vmid=payload.get("source_vmid"))
        elif mode == "target_config":
            result = validate_target_config(
                payload.get("config"),
                target_vmid=payload.get("target_vmid"),
                marker=payload.get("marker"),
                isolated=payload.get("isolated"),
                expected_disk_keys=payload.get("expected_disk_keys"),
                storage_id=payload.get("storage_id"),
            )
        elif mode == "task_proof":
            result = verify_task_proof(payload.get("payload"))
        elif mode == "prerequisite_chain":
            result = verify_prerequisite_chain(payload.get("payload"))
        elif mode == "restore_task_proof":
            result = verify_restore_task_proof(payload.get("payload"))
        elif mode == "cleanup_inventory_pins":
            result = verify_cleanup_inventory_pins(payload.get("payload"))
        else:
            raise Refusal("invalid_input")
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except Refusal as exc:
        print(json.dumps({"status": "refused", "reason": exc.reason}, sort_keys=True, separators=(",", ":")))
        return 2
    except (AttributeError, KeyError, TypeError, UnicodeError, ValueError):
        print('{"reason":"invalid_input","status":"refused"}')
        return 2


if __name__ == "__main__":
    raise SystemExit(_main())
