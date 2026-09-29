#!/usr/bin/env python3
"""Emit allow-listed, read-only Proxmox physical-storage survey facts."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Mapping

NODE_NAME = re.compile(r"[A-Za-z0-9._-]{1,64}")
PATH = re.compile(r"/[A-Za-z0-9._/+:-]{1,255}")
SIZE_BANDS = ((100 * 1024**3, "under-100-GiB"), (1024**4, "100-GiB-to-under-1-TiB"))
SAFE_REFUSALS = {
    "The private storage node declaration is missing or malformed.",
    "The declared storage node is not uniquely online.",
    "Proxmox returned an incomplete disk inventory.",
    "Proxmox returned an incomplete LVM inventory.",
    "Proxmox returned an incomplete thin-pool inventory.",
    "Proxmox returned an incomplete directory inventory.",
    "Proxmox returned an incomplete storage status inventory.",
    "Proxmox returned a malformed disk inventory.",
    "Proxmox returned a malformed LVM inventory.",
    "Proxmox returned a malformed thin-pool inventory.",
    "Proxmox returned a malformed directory inventory.",
    "Proxmox returned a malformed storage status inventory.",
}
FALLBACK_REFUSAL = "Physical-storage survey refused because Proxmox returned invalid data."


def _require(ok: bool, message: str) -> None:
    if not ok:
        raise ValueError(message)


def _safe_refusal(exc: BaseException) -> str:
    message = str(exc)
    return message if message in SAFE_REFUSALS else FALLBACK_REFUSAL


def _integer(value: object, *, positive: bool = False) -> bool:
    return type(value) is int and (value > 0 if positive else value >= 0)


def _size_band(value: int) -> str:
    if value < SIZE_BANDS[0][0]:
        return SIZE_BANDS[0][1]
    if value < SIZE_BANDS[1][0]:
        return SIZE_BANDS[1][1]
    return "at-least-1-TiB"


def _size_band_counts() -> dict[str, int]:
    return {"under-100-GiB": 0, "100-GiB-to-under-1-TiB": 0, "at-least-1-TiB": 0}


def _headroom_band_counts() -> dict[str, int]:
    return {"under-30-percent": 0, "30-to-under-70-percent": 0, "at-least-70-percent": 0, "unknown": 0}


def _api_data(value: object, *, section: str, expected: type = list) -> object:
    refusal = f"Proxmox returned an incomplete {section} inventory."
    _require(
        isinstance(value, Mapping)
        and value.get("status") == 200
        and isinstance(value.get("json"), Mapping)
        and isinstance(value["json"].get("data"), expected),
        refusal,
    )
    return value["json"]["data"]


def validate_node(payload: object) -> dict[str, object]:
    _require(isinstance(payload, Mapping), "The private storage node declaration is missing or malformed.")
    target, nodes = payload.get("target_node"), payload.get("nodes")
    _require(
        isinstance(target, str)
        and NODE_NAME.fullmatch(target) is not None
        and target not in {".", ".."}
        and isinstance(nodes, list)
        and all(isinstance(node, Mapping) for node in nodes),
        "The private storage node declaration is missing or malformed.",
    )
    seen: set[str] = set()
    matches = 0
    for node in nodes:
        name, status = node.get("node"), node.get("status")
        _require(
            isinstance(name, str)
            and NODE_NAME.fullmatch(name) is not None
            and name not in {".", ".."}
            and status in {"online", "offline"}
            and name not in seen,
            "The declared storage node is not uniquely online.",
        )
        seen.add(name)
        if name == target and status == "online":
            matches += 1
    _require(matches == 1, "The declared storage node is not uniquely online.")
    return {"node": target}


def _disk_facts(rows: list[object]) -> dict[str, object]:
    _require(all(isinstance(row, Mapping) for row in rows), "Proxmox returned a malformed disk inventory.")
    paths: set[str] = set()
    disk_count = partition_count = unknown_used = 0
    used_counts = {
        "lvm": 0, "zfs": 0, "filesystem": 0, "mounted": 0,
        "partition": 0, "other_reported": 0,
    }
    raw_bytes = 0
    for row in rows:
        path, parent, size, used = row.get("devpath"), row.get("parent"), row.get("size"), row.get("used")
        parent_valid = "parent" not in row or (isinstance(parent, str) and PATH.fullmatch(parent) is not None)
        used_valid = "used" not in row or (isinstance(used, str) and bool(used.strip()))
        _require(
            isinstance(path, str)
            and PATH.fullmatch(path) is not None
            and path not in paths
            and parent_valid
            and _integer(size, positive=True)
            and used_valid,
            "Proxmox returned a malformed disk inventory.",
        )
        paths.add(path)
        if "parent" not in row:
            disk_count += 1
            raw_bytes += size
        else:
            partition_count += 1
        if "used" not in row:
            unknown_used += 1
        else:
            normalized = used.strip().lower()
            category = (
                "lvm" if "lvm" in normalized else "zfs" if "zfs" in normalized
                else "filesystem" if (
                    re.match(r"^ext[0-9]+(?:$|[^a-z0-9])", normalized) is not None
                    or any(fs in normalized for fs in ("xfs", "btrfs", "filesystem"))
                )
                else "mounted" if "mount" in normalized
                else "partition" if "partition" in normalized else "other_reported"
            )
            used_counts[category] += 1
    return {
        "physical_device_count": disk_count,
        "partition_count": partition_count,
        "device_usage_class_counts": used_counts,
        "device_usage_unknown_count": unknown_used,
        "reported_raw_device_capacity_band": _size_band(raw_bytes) if disk_count else "unknown",
    }


def _lvm_facts(data: object) -> dict[str, object]:
    groups = data.get("children") if isinstance(data, Mapping) else None
    _require(isinstance(groups, list) and all(isinstance(group, Mapping) for group in groups),
             "Proxmox returned a malformed LVM inventory.")
    total_free = 0
    pv_count = 0
    group_names: set[str] = set()
    pv_names: set[str] = set()
    for group in groups:
        name, size, free = group.get("name"), group.get("size"), group.get("free")
        children = group.get("children", [])
        _require(
            isinstance(name, str) and name and name not in group_names
            and _integer(size) and _integer(free) and free <= size
            and isinstance(children, list) and all(isinstance(child, Mapping) for child in children),
            "Proxmox returned a malformed LVM inventory.",
        )
        for child in children:
            pv_size, pv_free = child.get("size"), child.get("free")
            _require(
                isinstance(child.get("name"), str) and bool(child["name"])
                and child["name"] not in pv_names
                and _integer(pv_size) and _integer(pv_free) and pv_free <= pv_size,
                "Proxmox returned a malformed LVM inventory.",
            )
            pv_names.add(child["name"])
        group_names.add(name)
        total_free += free
        pv_count += len(children)
    return {
        "lvm_volume_group_count": len(groups),
        "lvm_physical_volume_count": pv_count,
        "lvm_reported_free_capacity_band": _size_band(total_free) if groups else "unknown",
    }


def _thin_facts(rows: list[object]) -> dict[str, object]:
    _require(all(isinstance(row, Mapping) for row in rows), "Proxmox returned a malformed thin-pool inventory.")
    total, used = 0, 0
    pool_names: set[tuple[str, str]] = set()
    for pool in rows:
        identity = (pool.get("vg"), pool.get("lv"))
        size, consumed = pool.get("lv_size"), pool.get("used")
        _require(
            all(isinstance(part, str) and part for part in identity)
            and identity not in pool_names
            and _integer(size, positive=True) and _integer(consumed) and consumed <= size,
            "Proxmox returned a malformed thin-pool inventory.",
        )
        pool_names.add(identity)
        metadata_size, metadata_used = pool.get("metadata_size"), pool.get("metadata_used")
        _require(
            _integer(metadata_size, positive=True)
            and _integer(metadata_used)
            and metadata_used <= metadata_size,
            "Proxmox returned a malformed thin-pool inventory.",
        )
        total += size
        used += consumed
    free = total - used
    return {
        "lvm_thin_pool_count": len(rows),
        "lvm_thin_reported_logical_free_capacity_band": _size_band(free) if rows else "unknown",
        "lvm_thin_reported_logical_headroom_band": _headroom_band(total, free) if rows else "unknown",
    }


def _headroom_band(total: int, available: int) -> str:
    if total <= 0:
        return "unknown"
    if available * 10 < total * 3:
        return "under-30-percent"
    if available * 10 < total * 7:
        return "30-to-under-70-percent"
    return "at-least-70-percent"


def _directory_facts(rows: list[object]) -> dict[str, object]:
    _require(all(isinstance(row, Mapping) for row in rows), "Proxmox returned a malformed directory inventory.")
    types = {"ext": 0, "xfs": 0, "zfs": 0, "network_fs": 0, "other_reported": 0}
    paths: set[str] = set()
    for row in rows:
        path, device, fstype, options = row.get("path"), row.get("device"), row.get("type"), row.get("options")
        _require(
            isinstance(path, str) and PATH.fullmatch(path) is not None
            and path not in paths
            and isinstance(device, str) and bool(device)
            and isinstance(fstype, str) and bool(fstype)
            and isinstance(options, str),
            "Proxmox returned a malformed directory inventory.",
        )
        paths.add(path)
        lowered = fstype.lower()
        category = (
            "ext" if lowered.startswith("ext")
            else "xfs" if lowered == "xfs"
            else "zfs" if lowered == "zfs"
            else "network_fs" if lowered in {"nfs", "nfs4", "cifs", "smbfs"}
            else "other_reported"
        )
        types[category] += 1
    return {"managed_directory_count": len(rows), "managed_directory_type_counts": types,
            "directory_locality_verified": False}


def _storage_facts(rows: list[object]) -> dict[str, object]:
    _require(all(isinstance(row, Mapping) for row in rows), "Proxmox returned a malformed storage status inventory.")
    seen: set[str] = set()
    types: dict[str, int] = {"lvm": 0, "lvmthin": 0, "directory": 0, "other": 0}
    capacity_known = 0
    shared_true = shared_false = shared_unknown = 0
    active_count = active_unknown = 0
    total_bands, available_bands = _size_band_counts(), _size_band_counts()
    headroom_bands = _headroom_band_counts()
    for row in rows:
        name, kind, content = row.get("storage"), row.get("type"), row.get("content")
        active, shared = row.get("active"), row.get("shared")
        _require(
            isinstance(name, str) and NODE_NAME.fullmatch(name) is not None and name not in seen
            and isinstance(kind, str) and kind and isinstance(content, str)
            and ("active" not in row or (type(active) in {bool, int} and active in (0, 1)))
            and ("shared" not in row or (type(shared) in {bool, int} and shared in (0, 1))),
            "Proxmox returned a malformed storage status inventory.",
        )
        seen.add(name)
        normalized_type = kind.lower()
        category = normalized_type if normalized_type in {"lvm", "lvmthin", "dir"} else "other"
        types["directory" if category == "dir" else category] += 1
        if active is True or active == 1:
            active_count += 1
        elif "active" not in row:
            active_unknown += 1
        if shared is True or shared == 1:
            shared_true += 1
        elif shared is False or shared == 0:
            shared_false += 1
        elif "shared" not in row:
            shared_unknown += 1
        all_capacity = all(key in row for key in ("total", "used", "avail"))
        for key in ("total", "used", "avail"):
            if key in row:
                _require(
                    _integer(row[key]),
                    "Proxmox returned a malformed storage status inventory.",
                )
        if all_capacity:
            t, u, a = row["total"], row["used"], row["avail"]
            _require(
                _integer(t) and _integer(u) and _integer(a) and u <= t and a <= t,
                "Proxmox returned a malformed storage status inventory.",
            )
            if t > 0:
                total_bands[_size_band(t)] += 1
                available_bands[_size_band(a)] += 1
                headroom_bands[_headroom_band(t, a)] += 1
                capacity_known += 1
    return {
        "visible_storage_count": len(rows),
        "active_storage_count": active_count,
        "storage_active_unknown_count": active_unknown,
        "storage_backend_counts": types,
        "storage_shared_true_count": shared_true,
        "storage_shared_false_count": shared_false,
        "storage_shared_unknown_count": shared_unknown,
        "storage_capacity_known_count": capacity_known,
        "storage_capacity_unreported_count": len(rows) - capacity_known,
        "storage_reported_total_capacity_band_counts": total_bands,
        "storage_reported_available_capacity_band_counts": available_bands,
        "storage_reported_headroom_band_counts": headroom_bands,
    }


def inspect(payload: object) -> dict[str, object]:
    _require(isinstance(payload, Mapping), "Proxmox returned an incomplete disk inventory.")
    disk_rows = _api_data(payload.get("disks"), section="disk")
    lvm_data = _api_data(payload.get("lvm"), section="LVM", expected=Mapping)
    thin_rows = _api_data(payload.get("thinpool"), section="thin-pool")
    directory_rows = _api_data(payload.get("directories"), section="directory")
    storage_rows = _api_data(payload.get("storage"), section="storage status")
    report: dict[str, object] = {
        "survey": "read-only-physical-storage-inventory",
        "capacity_basis": "reported_capacity_only",
        **_disk_facts(disk_rows),
        **_lvm_facts(lvm_data),
        **_thin_facts(thin_rows),
        **_directory_facts(directory_rows),
        **_storage_facts(storage_rows),
        "device_selected": False,
        "device_safety_verified": False,
        "filesystem_readiness_verified": False,
        "pbs_suitability_verified": False,
        "pbs_readiness_verified": False,
        "write_authorized": False,
    }
    return report


def main() -> int:
    try:
        operation = sys.argv[1]
        payload = json.load(sys.stdin)
        if operation == "validate-node":
            result = validate_node(payload)
        elif operation == "inspect":
            result = inspect(payload)
        else:
            raise ValueError("Unsupported physical-storage survey operation.")
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"refusal": _safe_refusal(exc)}, separators=(",", ":")))
        return 2
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
