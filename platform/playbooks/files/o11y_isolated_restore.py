#!/usr/bin/env python3
"""Pure validators for exact PBS artifacts and isolated restore receipts."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

VMID = re.compile(r"(?:0|[1-9][0-9]{0,8})\Z")
SAFE_ID = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")
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


class Refusal(ValueError):
    def __init__(self, reason: str):
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
        isinstance(value, dict) and value.get("status") == 200 and isinstance(value.get("data"), str),
        "artifact_config_invalid",
    )
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
    data = value["data"]
    require(type(data.get("vmid")) is int and data["vmid"] == target_vmid, "target_config_invalid")
    require(data.get("onboot") in (0, "0", False, None), "target_config_invalid")
    require(data.get("startup") in (None, ""), "target_config_invalid")
    require(data.get("description") == marker, "target_config_invalid")
    require(isinstance(expected_disk_keys, list) and expected_disk_keys, "target_config_invalid")
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


def receipt_fingerprint(receipt: Mapping[str, object]) -> str:
    body = {key: value for key, value in receipt.items() if key != "receipt_fingerprint"}
    return sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())


def _objects(text: str):
    """Yield complete JSON objects embedded in bounded Semaphore output."""
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
                    value = json.loads(text[start : index + 1], object_pairs_hook=_unique_pairs)
                except (json.JSONDecodeError, UnicodeError, ValueError):
                    value = None
                if isinstance(value, dict):
                    yield value
                    for nested in value.values():
                        if isinstance(nested, str):
                            yield from _objects(nested)
                start = None


def _utc(value: object) -> datetime:
    require(isinstance(value, str), "task_output_invalid")
    match = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?(Z|\+00:00)", value)
    require(match is not None, "task_output_invalid")
    stamp = match.group(1) + ("." + match.group(2)[:6] if match.group(2) else "") + "+00:00"
    try:
        return datetime.fromisoformat(stamp).astimezone(UTC)
    except ValueError:
        raise Refusal("task_output_invalid") from None


def verify_task_proof(payload: object) -> dict:
    """Validate a task already fetched over the protected Semaphore API path.

    The caller supplies historical task bindings separately from current
    template metadata. Missing task-level environment, inventory, project, or
    repository binding is unknown and fails closed.
    """
    keys = {
        "task_id",
        "project_id",
        "environment_id",
        "inventory_id",
        "template_id",
        "repository_id",
        "expected_commit",
        "expected_template_name",
        "expected_playbook",
        "expected_repository_url",
        "expected_receipt_type",
        "expected_receipt_bindings",
        "task",
        "output",
        "template",
        "repository",
    }
    require(isinstance(payload, dict) and set(payload) == keys, "invalid_input")
    for key in ("task_id", "project_id", "environment_id", "inventory_id", "template_id", "repository_id"):
        require(_strict_int(payload[key], 1), "invalid_input")
    task, template, repository = payload["task"], payload["template"], payload["repository"]
    require(
        isinstance(task, dict) and isinstance(template, dict) and isinstance(repository, dict), "task_binding_unknown"
    )
    required_task = {
        "id": payload["task_id"],
        "project_id": payload["project_id"],
        "environment_id": payload["environment_id"],
        "inventory_id": payload["inventory_id"],
        "template_id": payload["template_id"],
        "repository_id": payload["repository_id"],
        "status": "success",
        "commit_hash": payload["expected_commit"],
        "playbook": payload["expected_playbook"],
    }
    for name, expected in required_task.items():
        require(name in task, "task_binding_unknown")
        require(task[name] == expected, "task_binding_mismatch")
    require(
        task.get("arguments") in (None, "")
        and task.get("git_branch") in (None, "")
        and task.get("limit", "") == ""
        and task.get("secret", "") in (None, "")
        and task.get("schedule_id") is None
        and task.get("workflow_run_id") is None
        and task.get("params") in (None, {"dry_run": False, "diff": False}),
        "task_binding_mismatch",
    )
    require(
        template.get("id") == payload["template_id"]
        and template.get("project_id") == payload["project_id"]
        and template.get("name") == payload["expected_template_name"]
        and template.get("playbook") == payload["expected_playbook"]
        and template.get("environment_id") == payload["environment_id"]
        and template.get("inventory_id") == payload["inventory_id"]
        and template.get("repository_id") == payload["repository_id"],
        "task_binding_mismatch",
    )
    require(
        repository.get("id") == payload["repository_id"]
        and repository.get("project_id") == payload["project_id"]
        and repository.get("name") == "agent-cloud dev"
        and repository.get("git_url") == payload["expected_repository_url"],
        "task_binding_mismatch",
    )
    require(
        isinstance(payload["expected_commit"], str) and re.fullmatch(r"[0-9a-f]{40}", payload["expected_commit"]),
        "invalid_input",
    )
    rows = payload["output"]
    require(isinstance(rows, list) and 1 <= len(rows) <= 5000, "task_output_invalid")
    consumed = 0
    fragments = []
    last_id = -1
    zero_id_mode = None
    prior_time = None
    created = _utc(task.get("created"))
    started = _utc(task.get("start"))
    ended = _utc(task.get("end"))
    now = datetime.now(UTC)
    require(created <= started <= ended <= now, "task_binding_mismatch")
    for row in rows:
        require(
            isinstance(row, dict)
            and row.get("task_id") == payload["task_id"]
            and type(row.get("id")) is int
            and row["id"] >= 0
            and isinstance(row.get("output"), str),
            "task_output_invalid",
        )
        is_zero = row["id"] == 0
        require(
            (zero_id_mode is None or zero_id_mode == is_zero) and (is_zero or row["id"] > last_id),
            "task_output_invalid",
        )
        zero_id_mode = is_zero
        if not is_zero:
            last_id = row["id"]
        stamp = _utc(row.get("time"))
        require(created <= stamp <= ended <= now and (prior_time is None or prior_time <= stamp), "task_output_invalid")
        prior_time = stamp
        raw = row["output"]
        consumed += len(raw.encode("utf-8"))
        require(consumed <= 1024 * 1024, "task_output_invalid")
        fragments.append(raw)
    combined = re.sub(r"\x1b\[[0-9;]*m", "", "\n".join(fragments))
    candidates = [item for item in _objects(combined) if item.get("type") == payload["expected_receipt_type"]]
    require(len(candidates) == 1, "task_receipt_ambiguous" if candidates else "task_output_invalid")
    receipt = candidates[0]
    require(
        isinstance(receipt, dict)
        and receipt.get("type") == payload["expected_receipt_type"]
        and receipt.get("status") == "verified",
        "task_receipt_invalid",
    )
    require(receipt.get("bindings") == payload["expected_receipt_bindings"], "receipt_binding_mismatch")
    fp = receipt.get("receipt_fingerprint")
    require(
        isinstance(fp, str) and re.fullmatch(r"[0-9a-f]{64}", fp) and receipt_fingerprint(receipt) == fp,
        "task_receipt_invalid",
    )
    return {
        "schema_version": 1,
        "status": "verified",
        "task_id": payload["task_id"],
        "project_id": payload["project_id"],
        "template_id": payload["template_id"],
        "repository_id": payload["repository_id"],
        "commit": payload["expected_commit"],
        "receipt_type": payload["expected_receipt_type"],
        "receipt_fingerprint": fp,
        "output_fingerprint": sha256(combined.encode()),
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
        payload = json.load(sys.stdin)
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
    except (AttributeError, KeyError, TypeError, ValueError):
        print('{"reason":"invalid_input","status":"refused"}')
        return 2


if __name__ == "__main__":
    raise SystemExit(_main())
