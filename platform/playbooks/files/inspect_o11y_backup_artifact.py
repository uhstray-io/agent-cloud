#!/usr/bin/env python3
"""Produce a sanitized, read-only o11y backup artifact inspection receipt."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections.abc import Mapping

VMID = re.compile(r"[1-9][0-9]{0,8}")
STORAGE_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
VOLID = re.compile(
    r"(?P<storage>[A-Za-z0-9._-]{1,64}):backup/vzdump-qemu-(?P<vmid>[1-9][0-9]{0,8})-"
    r"[A-Za-z0-9_.-]+\.(?P<format>vma(?:\.zst|\.gz|\.lzo)?|tar)"
)
DISK_DEVICE = re.compile(r"(?:ide|sata|scsi|virtio)[0-9]+")
SIZE = re.compile(r"(?:^|,)size=(?P<value>[0-9]+(?:\.[0-9]+)?)(?P<unit>[KMGT])(?:$|,)", re.I)
SIZE_MULTIPLIERS = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
SAFE_REFUSALS = {
    "The declared o11y VM identity is missing or malformed.",
    "Proxmox returned a malformed VM listing.",
    "The declared o11y VM is missing or ambiguous.",
    "Proxmox returned a malformed storage listing.",
    "Proxmox returned a storage with a malformed identity or backend type.",
    "Proxmox returned duplicate storage identities.",
    "Proxmox returned an incomplete backup-content listing.",
    "Proxmox returned a malformed backup-content listing.",
    "A listed backup artifact has a malformed identity or metadata.",
    "A listed backup artifact does not match the declared o11y VM.",
    "Proxmox returned a malformed VM disk configuration.",
    "Proxmox returned a malformed VM disk size.",
    "Unsupported artifact inspection operation.",
}
FALLBACK_REFUSAL = "Backup artifact inspection refused because Proxmox returned invalid data."


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _safe_refusal(exc: BaseException) -> str:
    message = str(exc)
    return message if message in SAFE_REFUSALS else FALLBACK_REFUSAL


def _positive_int(value: object) -> int:
    _require(type(value) is int and value > 0, "A listed backup artifact has a malformed identity or metadata.")
    return value


def _storage_classes(storages: object) -> dict[str, str]:
    _require(isinstance(storages, list), "Proxmox returned a malformed storage listing.")
    result: dict[str, str] = {}
    for storage in storages:
        _require(isinstance(storage, Mapping), "Proxmox returned a malformed storage listing.")
        identity, backend = storage.get("storage"), storage.get("type")
        _require(
            isinstance(identity, str)
            and STORAGE_ID.fullmatch(identity) is not None
            and isinstance(backend, str)
            and backend.strip(),
            "Proxmox returned a storage with a malformed identity or backend type.",
        )
        _require(identity not in result, "Proxmox returned duplicate storage identities.")
        result[identity] = "pbs" if backend.strip().lower() == "pbs" else "non-pbs"
    return result


def _inspect_candidate(candidate: object, vmid: int, storage_classes: Mapping[str, str]) -> dict[str, object]:
    _require(isinstance(candidate, Mapping), "A listed backup artifact has a malformed identity or metadata.")
    volid = candidate.get("volid")
    match = VOLID.fullmatch(volid) if isinstance(volid, str) else None
    _require(match is not None, "A listed backup artifact has a malformed identity or metadata.")
    _require(int(match.group("vmid")) == vmid, "A listed backup artifact does not match the declared o11y VM.")
    storage = match.group("storage")
    _require(storage in storage_classes, "A listed backup artifact has a malformed identity or metadata.")
    fmt = candidate.get("format", match.group("format"))
    _require(isinstance(fmt, str) and fmt == match.group("format"),
             "A listed backup artifact has a malformed identity or metadata.")
    size = _positive_int(candidate.get("size"))
    ctime = _positive_int(candidate.get("ctime"))
    protected = candidate.get("protected")
    if protected is True or (type(protected) is int and protected == 1):
        protected_status = "protected"
    elif protected is False or (type(protected) is int and protected == 0):
        protected_status = "not-protected"
    elif protected is None:
        protected_status = "unknown"
    else:
        _require(False, "A listed backup artifact has a malformed identity or metadata.")
        protected_status = "unknown"
    return {
        # This fingerprint identifies the record without disclosing its storage/path.
        "artifact_identity_sha256": hashlib.sha256(volid.encode("utf-8")).hexdigest(),
        "backend_class": storage_classes[storage],
        "format": fmt,
        "size_bytes": size,
        "creation_time_epoch": ctime,
        "proxmox_protected_flag": protected_status,
        # PVE's per-record protected flag is mutable metadata, not immutable storage.
        "immutability_verified": False,
    }


def _disk_layout(config: object) -> list[dict[str, object]]:
    _require(isinstance(config, Mapping), "Proxmox returned a malformed VM disk configuration.")
    disks = []
    for device, raw in config.items():
        if not isinstance(device, str) or DISK_DEVICE.fullmatch(device) is None:
            continue
        _require(isinstance(raw, str), "Proxmox returned a malformed VM disk configuration.")
        options = raw.split(":", 1)[-1]
        if re.search(r"(?:^|,)media=cdrom(?:$|,)", options):
            continue
        size_match = SIZE.search(options)
        _require(size_match is not None or "size=" not in options,
                 "Proxmox returned a malformed VM disk size.")
        size_bytes = None
        if size_match is not None:
            try:
                size_bytes = int(float(size_match.group("value")) * SIZE_MULTIPLIERS[size_match.group("unit").upper()])
            except (KeyError, ValueError, OverflowError):
                raise ValueError("Proxmox returned a malformed VM disk size.") from None
            _require(size_bytes > 0, "Proxmox returned a malformed VM disk size.")
        backup_match = re.search(r"(?:^|,)backup=(?P<value>[01])(?:$|,)", options)
        disks.append({
            "device": device,
            "size_bytes": size_bytes,
            "included_in_backup": None if backup_match is None else backup_match.group("value") == "1",
        })
    return sorted(disks, key=lambda item: str(item["device"]))


def inspect(payload: Mapping[str, object]) -> dict[str, object]:
    vmid_value = payload.get("vmid")
    node = payload.get("node")
    _require(isinstance(vmid_value, str) and VMID.fullmatch(vmid_value) is not None
             and isinstance(node, str) and node,
             "The declared o11y VM identity is missing or malformed.")
    vmid = int(vmid_value)

    vms = payload.get("vms")
    _require(isinstance(vms, list) and all(isinstance(vm, Mapping) for vm in vms),
             "Proxmox returned a malformed VM listing.")
    matches = [vm for vm in vms if vm.get("type") == "qemu" and vm.get("vmid") == vmid and vm.get("node") == node]
    _require(len(matches) == 1, "The declared o11y VM is missing or ambiguous.")

    storage_classes = _storage_classes(payload.get("storages"))
    reads = payload.get("content_reads")
    _require(isinstance(reads, list), "Proxmox returned an incomplete backup-content listing.")
    candidates = []
    seen_storages = set()
    for read in reads:
        _require(isinstance(read, Mapping), "Proxmox returned an incomplete backup-content listing.")
        item = read.get("item")
        storage = read.get("storage")
        if storage is None and isinstance(item, Mapping):
            storage = item.get("storage")
        status = read.get("status")
        response = read.get("json")
        data = read.get("data")
        if data is None and isinstance(response, Mapping):
            data = response.get("data")
        _require(storage in storage_classes and storage not in seen_storages and status == 200,
                 "Proxmox returned an incomplete backup-content listing.")
        seen_storages.add(storage)
        _require(isinstance(data, list), "Proxmox returned a malformed backup-content listing.")
        for candidate in data:
            _require(isinstance(candidate, Mapping) and candidate.get("vmid") == vmid,
                     "A listed backup artifact does not match the declared o11y VM.")
            candidates.append(_inspect_candidate(candidate, vmid, storage_classes))
    _require(seen_storages == set(storage_classes),
             "Proxmox returned an incomplete backup-content listing.")

    config = payload.get("vm_config")
    _require(isinstance(config, Mapping) and isinstance(config.get("status"), int)
             and config["status"] == 200 and isinstance(config.get("data"), Mapping),
             "Proxmox returned a malformed VM disk configuration.")
    disk_layout = _disk_layout(config["data"])
    _require(bool(disk_layout), "Proxmox returned a malformed VM disk configuration.")
    return {
        "survey": "read-only-artifact-detail",
        "target_vm_verified": True,
        "candidate_artifact_count": len(candidates),
        "candidates": candidates,
        "source_disk_count": len(disk_layout),
        "source_disk_layout": disk_layout,
        "source_disk_layout_complete": all(disk["size_bytes"] is not None for disk in disk_layout),
        "artifact_immutability_verified": False,
        "isolated_restore_target_verified": False,
        "restore_test_verified": False,
    }


def main() -> int:
    try:
        operation = sys.argv[1]
        payload = json.load(sys.stdin)
        _require(isinstance(payload, Mapping), "Proxmox returned invalid inspection data.")
        if operation != "inspect":
            raise ValueError("Unsupported artifact inspection operation.")
        result = inspect(payload)
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"refusal": _safe_refusal(exc)}, separators=(",", ":")))
        return 2
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
