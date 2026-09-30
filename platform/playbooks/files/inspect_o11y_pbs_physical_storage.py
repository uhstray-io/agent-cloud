#!/usr/bin/env python3
"""Emit allow-listed, read-only Proxmox physical-storage survey facts."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Mapping

NODE_NAME = re.compile(r"[A-Za-z0-9._-]{1,64}")
STORAGE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
VG_NAME = re.compile(r"[A-Za-z0-9+_.-]{1,127}")
PATH = re.compile(r"/[A-Za-z0-9._/+:-]{1,255}")
SIZE_BANDS = ((100 * 1024**3, "under-100-GiB"), (1024**4, "100-GiB-to-under-1-TiB"))
PROPOSED_DISK_SIZES = (256, 512, 1024)
SAFE_REFUSALS = {
    "The private storage node declaration is missing or malformed.",
    "The private VM image-storage declaration is missing or malformed.",
    "The declared storage node is not uniquely online.",
    "Proxmox returned an incomplete disk inventory.",
    "Proxmox returned an incomplete LVM inventory.",
    "Proxmox returned an incomplete thin-pool inventory.",
    "Proxmox returned an incomplete directory inventory.",
    "Proxmox returned an incomplete storage status inventory.",
    "Proxmox returned an incomplete cluster storage config inventory.",
    "Proxmox returned an incomplete declared storage config inventory.",
    "Proxmox returned an incomplete visible volume inventory.",
    "Proxmox returned an incomplete declared storage permissions inventory.",
    "Proxmox returned incomplete declared storage linkage.",
    "Proxmox returned inconsistent declared storage capacity.",
    "Proxmox returned a malformed visible volume inventory.",
    "Proxmox returned a malformed disk inventory.",
    "Proxmox returned a malformed LVM inventory.",
    "Proxmox returned a malformed thin-pool inventory.",
    "Proxmox returned a malformed directory inventory.",
    "Proxmox returned a malformed storage status inventory.",
    "Proxmox returned a malformed cluster storage config inventory.",
    "Proxmox returned a malformed cluster storage node scope.",
    "Proxmox returned an incomplete thick-LVM storage linkage.",
    "Proxmox returned inconsistent thick-LVM capacity.",
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


def _storage_applies_to_node(config: Mapping, target_node: str) -> bool:
    """Decode Proxmox's optional comma-separated storage node restriction."""
    if "nodes" not in config:
        return True
    encoded_nodes = config["nodes"]
    _require(
        isinstance(encoded_nodes, str) and bool(encoded_nodes),
        "Proxmox returned a malformed cluster storage node scope.",
    )
    node_names = encoded_nodes.split(",")
    _require(
        all(
            name == name.strip()
            and NODE_NAME.fullmatch(name) is not None
            and name not in {".", ".."}
            for name in node_names
        )
        and len(set(node_names)) == len(node_names),
        "Proxmox returned a malformed cluster storage node scope.",
    )
    return target_node in node_names


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


def _storage_facts(rows: list[object], declared_storage_id: str) -> tuple[dict[str, object], Mapping | None]:
    _require(all(isinstance(row, Mapping) for row in rows), "Proxmox returned a malformed storage status inventory.")
    seen: set[str] = set()
    types: dict[str, int] = {"lvm": 0, "lvmthin": 0, "directory": 0, "other": 0}
    capacity_known = 0
    shared_true = shared_false = shared_unknown = 0
    active_count = active_unknown = 0
    total_bands, available_bands = _size_band_counts(), _size_band_counts()
    headroom_bands = _headroom_band_counts()
    declared_rows: list[Mapping] = []
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
        if name == declared_storage_id:
            declared_rows.append(row)
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
    candidate = declared_rows[0] if len(declared_rows) == 1 else None
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
    }, candidate


def _allocation_facts(
    candidate: Mapping | None,
    declared_storage_id: str,
    config_response: object,
    permissions_response: object,
    volume_response: object,
    thinpool_rows: list[object],
) -> dict[str, object]:
    result = {
        "declared_storage_row_found": candidate is not None,
        "declared_storage_row_eligible": False,
        "declared_thinpool_linked": False,
        "visible_volume_permissions_verified": False,
        "visible_volume_inventory_well_formed": False,
        "snapshot_inventory_complete_verified": False,
        "declared_metadata_headroom_30_percent": False,
        "snapshot_unverified_visible_volume_preflight_passes_256_gib_disk": False,
        "snapshot_unverified_visible_volume_preflight_passes_512_gib_disk": False,
        "snapshot_unverified_visible_volume_preflight_passes_1024_gib_disk": False,
        "storage_allocation_authorized": False,
    }
    if candidate is None:
        return result

    config = _api_data(config_response, section="declared storage config", expected=Mapping)
    permissions = _api_data(
        permissions_response, section="declared storage permissions", expected=Mapping
    )
    permission_path = f"/storage/{declared_storage_id}"
    effective_privileges = permissions.get(permission_path)
    _require(
        effective_privileges is None or isinstance(effective_privileges, Mapping),
        "Proxmox returned an incomplete declared storage permissions inventory.",
    )
    # Values are propagation flags; a present privilege applies at this exact path
    # even when its flag is false.
    has_complete_visibility = isinstance(effective_privileges, Mapping) and all(
        privilege in effective_privileges
        and type(effective_privileges[privilege]) in {bool, int}
        and effective_privileges[privilege] in (False, True, 0, 1)
        for privilege in ("Datastore.Allocate", "Datastore.Audit")
    )
    result["visible_volume_permissions_verified"] = has_complete_visibility
    vg, pool_name = config.get("vgname"), config.get("thinpool")
    _require(
        config.get("type") == "lvmthin"
        and isinstance(vg, str) and bool(vg)
        and isinstance(pool_name, str) and bool(pool_name),
        "Proxmox returned incomplete declared storage linkage.",
    )
    configured_content = {entry.strip() for entry in candidate.get("content", "").split(",")}
    eligible = (
        candidate.get("type") == "lvmthin"
        and (candidate.get("active") is True or type(candidate.get("active")) is int and candidate.get("active") == 1)
        and (candidate.get("shared") is False or type(candidate.get("shared")) is int and candidate.get("shared") == 0)
        and configured_content == {"images", "rootdir"}
    )
    result["declared_storage_row_eligible"] = eligible
    if not eligible or not has_complete_visibility:
        return result

    matching_pools = [
        row for row in thinpool_rows
        if isinstance(row, Mapping) and row.get("vg") == vg and row.get("lv") == pool_name
    ]
    _require(
        len(matching_pools) == 1,
        "Proxmox returned incomplete declared storage linkage.",
    )
    pool = matching_pools[0]
    pool_size, pool_used = pool.get("lv_size"), pool.get("used")
    metadata_size, metadata_used = pool.get("metadata_size"), pool.get("metadata_used")
    status_size, status_used, status_avail = (
        candidate.get(key) for key in ("total", "used", "avail")
    )
    _require(
        _integer(pool_size, positive=True)
        and _integer(pool_used)
        and _integer(metadata_size, positive=True)
        and _integer(metadata_used)
        and metadata_used <= metadata_size
        and _integer(status_size, positive=True)
        and _integer(status_used)
        and _integer(status_avail)
        and status_size == pool_size
        # These separate GETs have no common snapshot; concurrent writes can
        # make the values differ. Refuse and require a fresh survey, no tolerance.
        and status_used == pool_used
        and status_used + status_avail == status_size,
        "Proxmox returned inconsistent declared storage capacity.",
    )
    result["declared_thinpool_linked"] = True
    result["declared_metadata_headroom_30_percent"] = (
        (metadata_size - metadata_used) * 10 >= metadata_size * 3
    )

    # The content API returns image/rootdir volumes but the upstream LVM-thin
    # list_images method omits snap_* LVs, so this inventory is never snapshot-complete.
    volume_rows = _api_data(volume_response, section="visible volume", expected=list)
    volume_ids: set[str] = set()
    provisioned_virtual_bytes = 0
    for volume in volume_rows:
        _require(isinstance(volume, Mapping), "Proxmox returned a malformed visible volume inventory.")
        volume_id, size = volume.get("volid"), volume.get("size")
        prefix = f"{declared_storage_id}:"
        volume_name = volume_id[len(prefix):] if isinstance(volume_id, str) and volume_id.startswith(prefix) else ""
        name_match = re.fullmatch(r"(?:vm|base)-([0-9]{1,9})-([A-Za-z0-9][A-Za-z0-9_.+-]*)", volume_name)
        valid_volume = (
            isinstance(volume_id, str)
            and volume_id.startswith(prefix)
            and name_match is not None
            and int(name_match.group(1)) > 0
        )
        _require(
            valid_volume
            and volume_id not in volume_ids
            and volume.get("content") in {"images", "rootdir"}
            and volume.get("format") == "raw"
            and _integer(size, positive=True),
            "Proxmox returned a malformed visible volume inventory.",
        )
        volume_ids.add(volume_id)
        provisioned_virtual_bytes += size
    result["visible_volume_inventory_well_formed"] = True

    metadata_ok = result["declared_metadata_headroom_30_percent"]
    for size_gib in PROPOSED_DISK_SIZES:
        proposed_bytes = size_gib * 1024**3
        logical_remaining = pool_size - provisioned_virtual_bytes - proposed_bytes
        written_remaining = status_avail - proposed_bytes
        passes = (
            logical_remaining >= 0
            and logical_remaining * 10 >= pool_size * 3
            and written_remaining >= 0
            and written_remaining * 10 >= pool_size * 3
            and metadata_ok
        )
        result[f"snapshot_unverified_visible_volume_preflight_passes_{size_gib}_gib_disk"] = passes
    return result


def _thick_lvm_facts(
    status_rows: list[object], config_response: object, lvm_data: Mapping, target_node: str,
) -> dict[str, object]:
    """Report visible non-shared thick-LVM image-store headroom only."""
    config_rows = _api_data(config_response, section="cluster storage config")
    _require(
        all(isinstance(row, Mapping) for row in config_rows),
        "Proxmox returned a malformed cluster storage config inventory.",
    )
    configs: dict[str, Mapping] = {}
    local_configs: dict[str, Mapping] = {}
    visible_lvm_configs: dict[str, Mapping] = {}
    foreign_lvm_config_ids: set[str] = set()
    foreign_lvm_config_row_count = 0
    for row in config_rows:
        storage_id, kind = row.get("storage"), row.get("type")
        _require(
            isinstance(storage_id, str) and STORAGE_ID.fullmatch(storage_id) is not None
            and storage_id not in configs and isinstance(kind, str) and bool(kind),
            "Proxmox returned a malformed cluster storage config inventory.",
        )
        configs[storage_id] = row
        if kind in {"lvm", "lvmthin"}:
            visible_lvm_configs[storage_id] = row
            applies_locally = _storage_applies_to_node(row, target_node)
            if applies_locally:
                local_configs[storage_id] = row
            else:
                foreign_lvm_config_row_count += 1
                foreign_lvm_config_ids.add(storage_id)
        if kind == "lvm" and "content" in row:
            _require(isinstance(row["content"], str),
                    "Proxmox returned a malformed cluster storage config inventory.")

    # _storage_facts already validates every visible status ID and rejects duplicates.
    status_by_id = {row["storage"]: row for row in status_rows}

    group_rows = lvm_data.get("children")
    _require(
        isinstance(group_rows, list) and all(isinstance(group, Mapping) for group in group_rows),
        "Proxmox returned a malformed LVM inventory.",
    )
    # _lvm_facts already validates VG rows and rejects duplicate names. Keep this
    # join from tightening acceptance of unrelated LVM inventory rows.
    groups = {group["name"]: group for group in group_rows}
    local_lvm_config_rows = list(local_configs.items())
    local_config_vg_join_incomplete_count = sum(
        not (
            isinstance(config.get("vgname"), str)
            and VG_NAME.fullmatch(config["vgname"]) is not None
            and config["vgname"] in groups
        )
        for _, config in local_lvm_config_rows
    )
    unmatched_local_status_row_count = 0
    for row in status_rows:
        if row.get("type") not in {"lvm", "lvmthin"}:
            continue
        storage_id = row["storage"]
        visible_config = visible_lvm_configs.get(storage_id)
        enabled = row.get("enabled")
        foreign_disabled_pair = (
            storage_id in foreign_lvm_config_ids
            and visible_config is not None
            and visible_config.get("type") == row.get("type")
            and (enabled is False or type(enabled) is int and enabled == 0)
        )
        if foreign_disabled_pair:
            continue
        if (
            storage_id not in local_configs
            or local_configs[storage_id].get("type") != row.get("type")
        ):
            unmatched_local_status_row_count += 1
    unmatched_local_config_row_count = sum(
        status_by_id.get(storage_id) is None
        or status_by_id[storage_id].get("type") != config.get("type")
        for storage_id, config in local_lvm_config_rows
    )
    config_vg_mappings_complete = (
        local_config_vg_join_incomplete_count == 0
        and unmatched_local_status_row_count == 0
        and unmatched_local_config_row_count == 0
    )

    candidates: list[tuple[Mapping, Mapping]] = []
    visible_alias_suppressed_candidate_count = 0
    for storage_id, config in local_configs.items():
        if config.get("type") != "lvm":
            continue
        status = status_by_id.get(storage_id)
        if status is None or status.get("type") != "lvm":
            continue
        status_content = {
            part.strip() for part in status.get("content", "").split(",") if part.strip()
        }
        active = status.get("active") is True or (
            type(status.get("active")) is int and status.get("active") == 1
        )
        unshared = status.get("shared") is False or (
            type(status.get("shared")) is int and status.get("shared") == 0
        )
        if not active or not unshared or "images" not in status_content:
            continue
        if "disable" in config:
            _require(
                type(config["disable"]) in {bool, int} and config["disable"] in (0, 1),
                "Proxmox returned a malformed cluster storage config inventory.",
            )
        disabled = config.get("disable") is True or (
            type(config.get("disable")) is int and config.get("disable") == 1
        )
        if disabled:
            continue
        status_parts = status.get("content", "").split(",")
        _require(
            all(part and part.strip() == part for part in status_parts)
            and len(set(status_parts)) == len(status_parts),
            "Proxmox returned an incomplete thick-LVM storage linkage.",
        )
        configured_content = config.get("content")
        _require(
            isinstance(configured_content, str),
            "Proxmox returned an incomplete thick-LVM storage linkage.",
        )
        config_parts = configured_content.split(",")
        _require(
            all(part and part.strip() == part for part in config_parts)
            and len(set(config_parts)) == len(config_parts),
            "Proxmox returned a malformed cluster storage config inventory.",
        )
        config_content = set(config_parts)
        _require(
            "images" in config_content,
            "Proxmox returned an incomplete thick-LVM storage linkage.",
        )
        vg_name = config.get("vgname")
        if not (
            isinstance(vg_name, str)
            and VG_NAME.fullmatch(vg_name) is not None
            and vg_name in groups
        ):
            continue
        if any(
            other_id != storage_id
            and other_config.get("vgname") == vg_name
            for other_id, other_config in visible_lvm_configs.items()
        ):
            visible_alias_suppressed_candidate_count += 1
            continue
        # Any incomplete local mapping suppresses all candidate totals, even if
        # this particular config/status pair is individually well formed.
        if not config_vg_mappings_complete:
            continue
        group = groups[vg_name]
        total, used, available = (status.get(key) for key in ("total", "used", "avail"))
        group_total, group_free = group["size"], group["free"]
        _require(
            _integer(total, positive=True) and _integer(used) and _integer(available)
            and total == group_total and available == group_free
            and used == group_total - group_free,
            "Proxmox returned inconsistent thick-LVM capacity.",
        )
        candidates.append((status, group))

    result: dict[str, object] = {
        "visible_thick_lvm_image_store_count": len(candidates),
        "thick_lvm_config_vg_mappings_complete": config_vg_mappings_complete,
        "thick_lvm_foreign_lvm_config_row_count": foreign_lvm_config_row_count,
        "thick_lvm_local_config_vg_join_incomplete_count": local_config_vg_join_incomplete_count,
        "thick_lvm_unmatched_local_status_row_count": unmatched_local_status_row_count,
        "thick_lvm_unmatched_local_config_row_count": unmatched_local_config_row_count,
        "thick_lvm_visible_alias_suppressed_candidate_count": visible_alias_suppressed_candidate_count,
        "thick_lvm_reported_vg_headroom_candidate_count_256_gib": 0,
        "thick_lvm_reported_vg_headroom_candidate_count_512_gib": 0,
        "thick_lvm_reported_vg_headroom_candidate_count_1024_gib": 0,
        "thick_lvm_backing_media_verified": False,
        "thick_lvm_allocation_authorized": False,
    }
    for _, group in candidates:
        total, free = group["size"], group["free"]
        for size_gib in PROPOSED_DISK_SIZES:
            remaining = free - size_gib * 1024**3
            if remaining >= 0 and remaining * 10 >= total * 3:
                result[f"thick_lvm_reported_vg_headroom_candidate_count_{size_gib}_gib"] += 1
    return result


def inspect(payload: object) -> dict[str, object]:
    _require(isinstance(payload, Mapping), "Proxmox returned an incomplete disk inventory.")
    declared_storage_id = payload.get("declared_storage_id")
    _require(
        isinstance(declared_storage_id, str)
        and STORAGE_ID.fullmatch(declared_storage_id) is not None,
        "The private VM image-storage declaration is missing or malformed.",
    )
    target_node = payload.get("target_node")
    _require(
        isinstance(target_node, str)
        and NODE_NAME.fullmatch(target_node) is not None
        and target_node not in {".", ".."},
        "The private storage node declaration is missing or malformed.",
    )
    disk_rows = _api_data(payload.get("disks"), section="disk")
    lvm_data = _api_data(payload.get("lvm"), section="LVM", expected=Mapping)
    thin_rows = _api_data(payload.get("thinpool"), section="thin-pool")
    directory_rows = _api_data(payload.get("directories"), section="directory")
    storage_rows = _api_data(payload.get("storage"), section="storage status")
    storage_config_rows = payload.get("storage_config_rows")
    storage_facts, declared_storage = _storage_facts(storage_rows, declared_storage_id)
    report: dict[str, object] = {
        "survey": "read-only-physical-storage-inventory",
        "capacity_basis": "reported_capacity_only",
        **_disk_facts(disk_rows),
        **_lvm_facts(lvm_data),
        **_thin_facts(thin_rows),
        **_directory_facts(directory_rows),
        **storage_facts,
        **_allocation_facts(
            declared_storage,
            declared_storage_id,
            payload.get("storage_config"),
            payload.get("storage_permissions"),
            payload.get("visible_volumes"),
            thin_rows,
        ),
        **_thick_lvm_facts(storage_rows, storage_config_rows, lvm_data, target_node),
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
