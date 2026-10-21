#!/usr/bin/env python3
"""Pure validators for exact PBS artifacts and isolated restore receipts."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from decimal import Decimal, InvalidOperation

from o11y_pbs_receipts import Refusal, validate_completed_task

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
}


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
    marker: str,
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
    require(isinstance(marker, str) and 1 <= len(marker.encode("utf-8")) <= 128, "target_config_invalid")
    data = value["data"]
    require(type(data.get("vmid")) is int and data["vmid"] == target_vmid, "target_config_invalid")
    require(data.get("onboot") in (0, "0", False, None), "target_config_invalid")
    require(data.get("startup") in (None, ""), "target_config_invalid")
    require(data.get("description") == marker, "target_config_invalid")
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
    require(isinstance(result, dict) and set(result) == {"receipt", "task_metadata"}, "task_proof_invalid")
    return result


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
