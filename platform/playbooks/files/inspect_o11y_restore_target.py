#!/usr/bin/env python3
"""Validate an exact, disconnected o11y restore target from protected read facts."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation

from o11y_restore_cloud_init import source_auxiliary, valid_creation_meta, valid_provisioner_serial

VMID = re.compile(r"[1-9][0-9]{0,8}")
NODE_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
STORAGE_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
DISK_DEVICE = re.compile(r"(?:ide|sata|scsi|virtio|efidisk|tpmstate)[0-9]+")
MEDIA_OPTION = re.compile(r"(?:^|,)media=([^,]+)(?:$|,)")
SIZE = re.compile(r"(?:^|,)size=([0-9]+(?:\.[0-9]+)?)([KMGT])(?:$|,)", re.I)
MULTIPLIERS = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
SAFE_REFUSALS = {
    "The restore target declaration is missing or malformed.",
    "The source and restore target declarations conflict.",
    "The restore target must declare disconnected networking.",
    "Proxmox returned malformed or conflicting VM census evidence.",
    "The exact restore target is occupied or conflicts with the cluster census.",
    "Proxmox returned malformed node evidence.",
    "The restore target node is unavailable or shares the source node.",
    "Proxmox returned malformed or conflicting storage evidence.",
    "The declared restore storage is unavailable or does not support VM images.",
    "The declared restore storage does not support the reviewed cloud-init auxiliary shape.",
    "The selected artifact is not bound to the verified artifact census.",
    "Proxmox returned malformed artifact configuration.",
    "The artifact restore layout is incomplete, excluded, or ambiguous.",
    "Proxmox-reported capacity is insufficient for the complete restore layout and declared headroom.",
}
FALLBACK_REFUSAL = "Restore-target preflight refused because protected source data was invalid."


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _safe_refusal(exc: BaseException) -> str:
    message = str(exc)
    return message if message in SAFE_REFUSALS else FALLBACK_REFUSAL


def _positive_vmid(value: object) -> int:
    _require(
        (isinstance(value, str) and VMID.fullmatch(value) is not None)
        or (type(value) is int and 1 <= value <= 999_999_999),
        "The restore target declaration is missing or malformed.",
    )
    return int(value)


def _declaration(payload: Mapping[str, object]) -> tuple[int, str, int, str, str, int]:
    source = payload.get("source")
    target = payload.get("target")
    _require(
        isinstance(source, Mapping) and isinstance(target, Mapping),
        "The restore target declaration is missing or malformed.",
    )
    source_vmid = _positive_vmid(source.get("vmid"))
    target_vmid = _positive_vmid(target.get("vmid"))
    source_node, target_node = source.get("node"), target.get("node")
    storage = target.get("storage_id")
    headroom = target.get("headroom_percent")
    _require(
        isinstance(source_node, str)
        and NODE_ID.fullmatch(source_node) is not None
        and source_node not in {".", ".."}
        and isinstance(target_node, str)
        and NODE_ID.fullmatch(target_node) is not None
        and target_node not in {".", ".."}
        and isinstance(storage, str)
        and STORAGE_ID.fullmatch(storage) is not None
        and storage not in {".", ".."}
        and type(headroom) is int
        and 30 <= headroom <= 90,
        "The restore target declaration is missing or malformed.",
    )
    _require(
        target_vmid != source_vmid and target_node != source_node,
        "The source and restore target declarations conflict.",
    )
    _require(
        target.get("network_mode") == "disconnected",
        "The restore target must declare disconnected networking.",
    )
    return source_vmid, source_node, target_vmid, target_node, storage, headroom


def _artifact_layout(payload: Mapping[str, object]) -> tuple[list[tuple[str, int]], dict[str, object], bool]:
    selector = payload.get("artifact_selector")
    receipt = payload.get("artifact_receipt")
    config = payload.get("artifact_config")
    _require(
        isinstance(selector, Mapping)
        and set(selector) == {"volid", "backend", "format", "creation_time_epoch", "size_bytes"}
        and isinstance(selector.get("volid"), str)
        and isinstance(selector.get("backend"), str)
        and selector.get("backend") in {"pbs", "non-pbs"}
        and isinstance(selector.get("format"), str)
        and type(selector.get("creation_time_epoch")) is int
        and selector["creation_time_epoch"] > 0
        and type(selector.get("size_bytes")) is int
        and selector["size_bytes"] > 0
        and isinstance(receipt, Mapping)
        and receipt.get("target_vm_verified") is True,
        "The selected artifact is not bound to the verified artifact census.",
    )
    candidates = receipt.get("candidates")
    selected_index = receipt.get("selected_candidate_index")
    selected = receipt.get("selected_candidate")
    _require(
        isinstance(candidates, list)
        and len(candidates) <= 10_000
        and type(receipt.get("candidate_artifact_count")) is int
        and receipt["candidate_artifact_count"] == len(candidates)
        and type(selected_index) is int
        and 0 <= selected_index < len(candidates)
        and isinstance(selected, Mapping)
        and isinstance(candidates[selected_index], Mapping)
        and dict(selected) == dict(candidates[selected_index])
        and selected.get("backend_class") == selector.get("backend")
        and selected.get("format") == selector.get("format")
        and selected.get("creation_time_epoch") == selector.get("creation_time_epoch")
        and selected.get("size_bytes") == selector.get("size_bytes"),
        "The selected artifact is not bound to the verified artifact census.",
    )
    _require(
        isinstance(config, Mapping)
        and config.get("status") == 200
        and isinstance(config.get("data"), str),
        "Proxmox returned malformed artifact configuration.",
    )
    source = payload.get("source")
    _require(isinstance(source, Mapping), "Proxmox returned malformed artifact configuration.")
    source_vmid = _positive_vmid(source.get("vmid"))
    try:
        config_bytes = config["data"].encode("utf-8")
        config_lines = config["data"].splitlines()
        _require(
            len(config_bytes) <= 256 * 1024
            and len(config_lines) <= 4096
            and all(len(line.encode("utf-8")) <= 4096 for line in config_lines),
            "Proxmox returned malformed artifact configuration.",
        )
    except UnicodeEncodeError:
        raise ValueError("Proxmox returned malformed artifact configuration.") from None
    devices: dict[str, str] = {}
    source_cloud_init = False
    serial_seen = False
    meta_seen = False
    try:
        for line in config_lines:
            if not line or line.lstrip().startswith("#") or ":" not in line:
                continue
            device, raw = line.split(":", 1)
            device, value = device.strip(), raw.strip()
            if device.startswith("serial"):
                _require(
                    device == "serial0" and not serial_seen and valid_provisioner_serial(value),
                    "The artifact restore layout is incomplete, excluded, or ambiguous.",
                )
                serial_seen = True
                continue
            if device.startswith("meta"):
                _require(
                    device == "meta" and not meta_seen and valid_creation_meta(value),
                    "The artifact restore layout is incomplete, excluded, or ambiguous.",
                )
                meta_seen = True
                continue
            if device == "ide2":
                _require(
                    device not in devices and source_auxiliary(value, source_vmid),
                    "The artifact restore layout is incomplete, excluded, or ambiguous.",
                )
                devices[device] = value
                source_cloud_init = value != "none,media=cdrom"
                continue
            if not DISK_DEVICE.fullmatch(device):
                continue
            _require(device not in devices, "The artifact restore layout is incomplete, excluded, or ambiguous.")
            devices[device] = value
            media_option = MEDIA_OPTION.search(value)
            _require(
                media_option is None,
                "The artifact restore layout is incomplete, excluded, or ambiguous.",
            )
        disks = []
        for device, value in sorted(devices.items()):
            if device == "ide2":
                continue
            size_matches = SIZE.findall(value)
            _require(
                len(size_matches) == 1,
                "The artifact restore layout is incomplete, excluded, or ambiguous.",
            )
            size_match = size_matches[0]
            size = Decimal(size_match[0]) * MULTIPLIERS[size_match[1].upper()]
            _require(
                size > 0 and size == size.to_integral_value(),
                "The artifact restore layout is incomplete, excluded, or ambiguous.",
            )
            backup = re.findall(r"(?:^|,)backup=([01])(?:$|,)", value)
            _require(
                len(backup) <= 1 and (not backup or backup[0] == "1"),
                "The artifact restore layout is incomplete, excluded, or ambiguous.",
            )
            disks.append((device, int(size)))
    except (InvalidOperation, ValueError):
        raise ValueError("The artifact restore layout is incomplete, excluded, or ambiguous.") from None
    _require(bool(disks) and len(disks) <= 128, "The artifact restore layout is incomplete, excluded, or ambiguous.")
    return disks, dict(selected), source_cloud_init


def _vm_census(
    raw: object, source_vmid: int, source_node: str, target_vmid: int
) -> tuple[bool, bool]:
    _require(
        isinstance(raw, list) and all(isinstance(vm, Mapping) for vm in raw),
        "Proxmox returned malformed or conflicting VM census evidence.",
    )
    seen: set[int] = set()
    source_seen = False
    target_seen = False
    for vm in raw:
        vmid_raw = vm.get("vmid")
        if type(vmid_raw) is int and vmid_raw > 0:
            vmid = vmid_raw
        elif isinstance(vmid_raw, str) and VMID.fullmatch(vmid_raw):
            vmid = int(vmid_raw)
        else:
            raise ValueError("Proxmox returned malformed or conflicting VM census evidence.")
        node = vm.get("node")
        vm_type = vm.get("type")
        _require(
            isinstance(node, str)
            and NODE_ID.fullmatch(node) is not None
            and node not in {".", ".."}
            and vm_type in {"qemu", "lxc"}
            and vmid not in seen,
            "Proxmox returned malformed or conflicting VM census evidence.",
        )
        seen.add(vmid)
        if vmid == source_vmid:
            _require(
                vm_type == "qemu" and node == source_node and not source_seen,
                "Proxmox returned malformed or conflicting VM census evidence.",
            )
            source_seen = True
        target_seen = target_seen or vmid == target_vmid
    _require(source_seen, "Proxmox returned malformed or conflicting VM census evidence.")
    return source_seen, target_seen


def _node_is_online(raw: object, node: str) -> bool:
    _require(
        isinstance(raw, list) and all(isinstance(item, Mapping) for item in raw),
        "Proxmox returned malformed node evidence.",
    )
    nodes: dict[str, str] = {}
    for item in raw:
        identity, status = item.get("node"), item.get("status")
        _require(
            isinstance(identity, str)
            and NODE_ID.fullmatch(identity) is not None
            and identity not in {".", ".."}
            and status in {"online", "offline"}
            and identity not in nodes,
            "Proxmox returned malformed node evidence.",
        )
        nodes[identity] = status
    return nodes.get(node) == "online"


def _target_storage(
    raw_reads: object, node: str, storage_id: str
) -> tuple[int, int, str]:
    _require(
        isinstance(raw_reads, list) and all(isinstance(read, Mapping) for read in raw_reads),
        "Proxmox returned malformed or conflicting storage evidence.",
    )
    matches = []
    seen_by_node: dict[str, set[str]] = {}
    matching_node_reads = 0
    for read in raw_reads:
        read_node, status, rows = read.get("item"), read.get("status"), read.get("data")
        _require(
            isinstance(read_node, str)
            and NODE_ID.fullmatch(read_node) is not None
            and status == 200
            and isinstance(rows, list)
            and all(isinstance(row, Mapping) for row in rows),
            "Proxmox returned malformed or conflicting storage evidence.",
        )
        _require(read_node not in seen_by_node, "Proxmox returned malformed or conflicting storage evidence.")
        seen = seen_by_node.setdefault(read_node, set())
        if read_node == node:
            matching_node_reads += 1
        for row in rows:
            identity = row.get("storage")
            backend, active, content, shared = row.get("type"), row.get("active"), row.get("content"), row.get("shared")
            total, available = row.get("total"), row.get("avail")
            _require(
                isinstance(identity, str)
                and STORAGE_ID.fullmatch(identity) is not None
                and identity not in {".", ".."}
                and isinstance(backend, str)
                and backend.strip()
                and (active is True or active is False or (type(active) is int and active in {0, 1}))
                and isinstance(content, str)
                and (shared is True or shared is False or (type(shared) is int and shared in {0, 1})),
                "Proxmox returned malformed or conflicting storage evidence.",
            )
            _require(identity not in seen, "Proxmox returned malformed or conflicting storage evidence.")
            seen.add(identity)
            if read_node == node and identity == storage_id:
                matches.append(row)
    _require(
        matching_node_reads == 1 and len(matches) == 1,
        "The declared restore storage is unavailable or does not support VM images.",
    )
    row = matches[0]
    types = {value.strip() for value in row["content"].split(",") if value.strip()}
    total, available = row.get("total"), row.get("avail")
    _require(
        row["active"] == 1
        and row["shared"] == 0
        and "images" in types
        and type(total) is int
        and total > 0
        and type(available) is int
        and 0 <= available <= total,
        "The declared restore storage is unavailable or does not support VM images.",
    )
    return total, available, row["type"]


def inspect(payload: Mapping[str, object]) -> dict[str, object]:
    source_vmid, source_node, target_vmid, target_node, storage_id, headroom = _declaration(payload)
    disks, selected, has_cloud_init = _artifact_layout(payload)
    _, target_seen = _vm_census(payload.get("vms"), source_vmid, source_node, target_vmid)
    target_config = payload.get("target_config")
    target_nextid = payload.get("target_nextid")
    nextid_value = target_nextid.get("data") if isinstance(target_nextid, Mapping) else None
    nextid_matches_target = (
        type(nextid_value) is int and nextid_value == target_vmid
    ) or (isinstance(nextid_value, str) and VMID.fullmatch(nextid_value) and int(nextid_value) == target_vmid)
    _require(
        isinstance(target_config, Mapping)
        and target_config.get("status") in {200, 404, 500}
        and isinstance(target_nextid, Mapping)
        and target_nextid.get("status") in {200, 400},
        "The exact restore target is occupied or conflicts with the cluster census.",
    )
    _require(
        target_config["status"] in {404, 500}
        and target_nextid["status"] == 200
        and nextid_matches_target
        and not target_seen,
        "The exact restore target is occupied or conflicts with the cluster census.",
    )
    _require(
        _node_is_online(payload.get("nodes"), target_node),
        "The restore target node is unavailable or shares the source node.",
    )
    total, available, storage_type = _target_storage(payload.get("storage_reads"), target_node, storage_id)
    if has_cloud_init:
        _require(
            storage_type == "lvmthin",
            "The declared restore storage does not support the reviewed cloud-init auxiliary shape.",
        )
    required = sum(size for _, size in disks)
    remaining = available - required
    _require(
        remaining >= 0 and remaining * 100 >= total * headroom,
        "Proxmox-reported capacity is insufficient for the complete restore layout and declared headroom.",
    )
    return {
        "survey": "read-only-o11y-restore-target-preflight",
        "source_target_identity_distinct": True,
        "target_network_declared_disconnected": True,
        "artifact_selection_verified": True,
        "artifact_restore_layout_complete": True,
        "restore_disk_count": len(disks),
        "target_vmid_available_now": True,
        "cluster_census_visibility": "unverified",
        "target_vmid_exact_cluster_nextid_match": True,
        "target_vmid_reserved": False,
        "target_node_online_and_off_source": True,
        "target_storage_active_nonshared_image_store": True,
        "proxmox_reported_capacity_includes_declared_headroom": True,
        "capacity_basis": "proxmox-reported",
        "allocation_authorized": False,
        "restore_performed": False,
        "restore_verified": False,
    }


def main() -> int:
    try:
        operation = sys.argv[1]
        payload = json.load(sys.stdin)
        _require(isinstance(payload, Mapping), "Proxmox returned invalid inspection data.")
        _require(operation == "inspect", "Unsupported restore-target inspection operation.")
        result = inspect(payload)
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"refusal": _safe_refusal(exc)}, separators=(",", ":")))
        return 2
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
