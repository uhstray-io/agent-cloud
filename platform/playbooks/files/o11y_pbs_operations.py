#!/usr/bin/env python3
"""Strict, content-free checks for exact PBS backup operation bindings."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from o11y_pbs_receipts import Refusal, _require

VMID = re.compile(r"[1-9][0-9]{0,8}\Z")
STORAGE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
PBS_VOLID = re.compile(
    r"(?P<storage>[A-Za-z0-9][A-Za-z0-9._-]{0,63}):backup/vm/"
    r"(?P<vmid>[1-9][0-9]{0,8})/(?P<time>\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)\Z"
)
DISK_DEVICE = re.compile(r"(?:ide|sata|scsi|virtio|efidisk|tpmstate)[0-9]+\Z")
DISK_FILE = re.compile(r"drive-(?P<device>(?:ide|sata|scsi|virtio|efidisk|tpmstate)[0-9]+)\.img\.(?:fidx|didx)\Z")
UPID = re.compile(
    r"UPID:[A-Za-z0-9_.-]+:[0-9A-Fa-f]+:[0-9A-Fa-f]+:[0-9A-Fa-f]+:"
    r"[A-Za-z0-9_-]+:[A-Za-z0-9_.-]*:[A-Za-z0-9_.@-]*:?\Z"
)


def _positive(value: Any) -> bool:
    return type(value) is int and value > 0


def snapshot_from_operation(
    rows: object,
    *,
    vmid: object,
    storage_id: object,
    started_epoch: object,
    ended_epoch: object,
    baseline_volids: object,
) -> dict[str, object]:
    """Select one new PBS snapshot created inside the exact PVE UPID window."""
    _require(isinstance(vmid, str) and VMID.fullmatch(vmid) is not None, "artifact_invalid")
    _require(isinstance(storage_id, str) and STORAGE_ID.fullmatch(storage_id) is not None, "artifact_invalid")
    _require(_positive(started_epoch) and _positive(ended_epoch) and ended_epoch >= started_epoch, "artifact_invalid")
    _require(
        isinstance(baseline_volids, list) and all(isinstance(value, str) for value in baseline_volids),
        "artifact_invalid",
    )
    _require(isinstance(rows, list), "artifact_invalid")
    seen: set[str] = set()
    candidates: list[dict[str, object]] = []
    for row in rows:
        _require(isinstance(row, Mapping), "artifact_invalid")
        volid, row_vmid = row.get("volid"), row.get("vmid")
        size, ctime, fmt = row.get("size"), row.get("ctime"), row.get("format")
        _require(isinstance(volid, str) and _positive(size) and _positive(ctime), "artifact_invalid")
        _require(type(row_vmid) is int and row_vmid == int(vmid), "artifact_invalid")
        match = PBS_VOLID.fullmatch(volid)
        _require(
            match is not None
            and match.group("storage") == storage_id
            and match.group("vmid") == vmid
            and fmt == "pbs-vm",
            "artifact_invalid",
        )
        _require(volid not in seen, "artifact_invalid")
        seen.add(volid)
        stamp = datetime.strptime(match.group("time"), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        snapshot_epoch = int(stamp.timestamp())
        _require(ctime == snapshot_epoch, "artifact_invalid")
        if volid not in baseline_volids and started_epoch <= ctime <= ended_epoch:
            candidates.append(
                {
                    "backup_type": "vm",
                    "backup_id": vmid,
                    "backup_time": ctime,
                    "volid": volid,
                    "size_bytes": size,
                }
            )
    _require(len(candidates) == 1, "artifact_mismatch")
    return candidates[0]


def validate_operation_upid(value: object) -> dict[str, object]:
    """Accept only an actual PVE/PBS UPID-shaped operation selector."""
    _require(isinstance(value, str) and UPID.fullmatch(value) is not None, "verification_incomplete")
    return {"operation_bound": True}


def select_copy_locator(
    rows: object, *, vmid: object, storage_id: object, snapshot_time: object, selector: object
) -> dict[str, object]:
    """Bind the private PVE restore selector to one exact destination snapshot."""
    _require(isinstance(vmid, str) and VMID.fullmatch(vmid) is not None, "readback_incomplete")
    _require(isinstance(storage_id, str) and STORAGE_ID.fullmatch(storage_id) is not None, "readback_incomplete")
    _require(_positive(snapshot_time), "readback_incomplete")
    keys = {"volid", "backend", "format", "creation_time_epoch", "size_bytes"}
    _require(isinstance(selector, Mapping) and set(selector) == keys, "readback_incomplete")
    _require(isinstance(rows, list), "readback_incomplete")
    matches: list[dict[str, object]] = []
    seen: set[str] = set()
    for row in rows:
        _require(isinstance(row, Mapping), "readback_incomplete")
        volid, row_vmid = row.get("volid"), row.get("vmid")
        size, ctime = row.get("size"), row.get("ctime")
        _require(
            isinstance(volid, str)
            and type(row_vmid) is int
            and row_vmid == int(vmid)
            and _positive(size)
            and _positive(ctime),
            "readback_incomplete",
        )
        match = PBS_VOLID.fullmatch(volid)
        _require(
            match is not None
            and match.group("storage") == storage_id
            and match.group("vmid") == vmid
            and row.get("format") == "pbs-vm",
            "readback_incomplete",
        )
        _require(volid not in seen, "readback_incomplete")
        seen.add(volid)
        timestamp = datetime.strptime(match.group("time"), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        epoch = int(timestamp.timestamp())
        _require(ctime == epoch, "readback_incomplete")
        normalized = {
            "volid": volid,
            "backend": "pbs",
            "format": "pbs-vm",
            "creation_time_epoch": ctime,
            "size_bytes": size,
        }
        if epoch == snapshot_time and normalized == dict(selector):
            matches.append(normalized)
    _require(len(matches) == 1, "readback_incomplete")
    return {**matches[0], "storage_id": storage_id}


def validate_manifest_disk_layout(manifest_filenames: object, vm_config: object) -> dict[str, int]:
    """Require the exact PBS manifest to contain the declared VM config and disks."""
    _require(
        isinstance(manifest_filenames, list) and all(isinstance(name, str) for name in manifest_filenames),
        "artifact_invalid",
    )
    _require(len(manifest_filenames) == len(set(manifest_filenames)), "artifact_invalid")
    _require("qemu-server.conf.blob" in manifest_filenames, "artifact_invalid")
    _require(isinstance(vm_config, Mapping) and bool(vm_config), "artifact_invalid")
    expected: set[str] = set()
    devices: set[str] = set()
    for device, raw in vm_config.items():
        if not isinstance(device, str) or DISK_DEVICE.fullmatch(device) is None:
            continue
        _require(isinstance(raw, str), "artifact_invalid")
        options = raw.split(":", 1)[-1]
        if re.search(r"(?:^|,)media=cdrom(?:$|,)", options):
            continue
        backup = re.search(r"(?:^|,)backup=(?P<value>[01])(?:$|,)", options)
        if backup is not None and backup.group("value") == "0":
            continue
        devices.add(device)
        candidates = [
            name
            for name in manifest_filenames
            if DISK_FILE.fullmatch(name) and DISK_FILE.fullmatch(name).group("device") == device
        ]
        _require(len(candidates) == 1, "artifact_mismatch")
        expected.add(candidates[0])
    _require(bool(devices), "artifact_invalid")
    manifest_disks = [name for name in manifest_filenames if name.startswith("drive-")]
    _require(set(manifest_disks) == expected, "artifact_mismatch")
    return {"source_disk_count": len(devices), "manifest_disk_count": len(manifest_disks)}


def main() -> int:
    try:
        request = json.load(sys.stdin)
        _require(isinstance(request, Mapping), "invalid_input")
        operation = request.get("operation")
        if operation == "snapshot_from_operation":
            result: object = snapshot_from_operation(
                request.get("rows"),
                vmid=request.get("vmid"),
                storage_id=request.get("storage_id"),
                started_epoch=request.get("started_epoch"),
                ended_epoch=request.get("ended_epoch"),
                baseline_volids=request.get("baseline_volids"),
            )
        elif operation == "validate_upid":
            result = validate_operation_upid(request.get("upid"))
        elif operation == "select_copy_locator":
            result = select_copy_locator(
                request.get("rows"),
                vmid=request.get("vmid"),
                storage_id=request.get("storage_id"),
                snapshot_time=request.get("snapshot_time"),
                selector=request.get("selector"),
            )
        elif operation == "validate_manifest_disk_layout":
            result = validate_manifest_disk_layout(request.get("manifest_filenames"), request.get("vm_config"))
        else:
            raise Refusal("invalid_input")
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except Refusal as exc:
        print(json.dumps({"status": "refused", "reason": exc.reason}, sort_keys=True))
        return 2
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        print(json.dumps({"status": "refused", "reason": "invalid_input"}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
