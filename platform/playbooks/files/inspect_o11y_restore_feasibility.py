#!/usr/bin/env python3
"""Emit sanitized, read-only feasibility facts for an isolated o11y restore."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Mapping

NODE_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
STORAGE_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
SUPPORTED_FORMATS = {"vma", "vma.zst", "vma.gz", "vma.lzo", "pbs-vm"}
SAFE_REFUSALS = {
    "Proxmox returned a malformed node listing.",
    "Proxmox returned duplicate node identities.",
    "Proxmox returned no online nodes.",
    "The source node is missing from the online cluster listing.",
    "Proxmox returned an incomplete storage-capacity listing.",
    "Proxmox returned a malformed storage-capacity listing.",
    "Proxmox returned duplicate storage identities on one node.",
    "Proxmox returned a malformed image-storage capacity.",
    "Proxmox returned a malformed next-VMID response.",
    "Proxmox did not return a usable next VMID.",
    "The artifact receipt is incomplete or malformed.",
}
FALLBACK_REFUSAL = "Restore feasibility inspection refused because Proxmox returned invalid data."
NEXTID_VALUE = re.compile(r"[1-9][0-9]{0,8}")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _safe_refusal(exc: BaseException) -> str:
    message = str(exc)
    return message if message in SAFE_REFUSALS else FALLBACK_REFUSAL


def _valid_nonnegative_integer(value: object) -> bool:
    return type(value) is int and value >= 0


def _valid_nextid(value: object) -> bool:
    if type(value) is int:
        return 1 <= value <= 999_999_999
    return isinstance(value, str) and NEXTID_VALUE.fullmatch(value) is not None


def _source_facts(receipt: object) -> tuple[bool, bool, bool, int | None]:
    _require(isinstance(receipt, Mapping), "The artifact receipt is incomplete or malformed.")
    candidates = receipt.get("candidates")
    disks = receipt.get("source_disk_layout")
    count = receipt.get("source_disk_count")
    _require(
        isinstance(candidates, list)
        and isinstance(disks, list)
        and type(count) is int
        and count == len(disks)
        and bool(disks)
        and all(isinstance(candidate, Mapping) for candidate in candidates)
        and all(isinstance(disk, Mapping) for disk in disks),
        "The artifact receipt is incomplete or malformed.",
    )
    candidate = candidates[0] if len(candidates) == 1 else {}
    format_supported = isinstance(candidate, Mapping) and candidate.get("format") in SUPPORTED_FORMATS
    layout_parsed = format_supported and receipt.get("target_vm_verified") is True
    sizes_complete = bool(disks) and all(
        type(disk.get("size_bytes")) is int and disk["size_bytes"] > 0 for disk in disks
    )
    restore_bytes = (
        sum(disk["size_bytes"] for disk in disks) if sizes_complete and layout_parsed else None
    )
    inclusion_verified = bool(disks) and all(disk.get("included_in_backup") is True for disk in disks)
    return layout_parsed, sizes_complete, inclusion_verified, restore_bytes


def _online_nodes(raw_nodes: object) -> list[str]:
    _require(isinstance(raw_nodes, list) and all(isinstance(node, Mapping) for node in raw_nodes),
             "Proxmox returned a malformed node listing.")
    seen: set[str] = set()
    online = []
    for node in raw_nodes:
        identity, status = node.get("node"), node.get("status")
        _require(
            isinstance(identity, str)
            and NODE_ID.fullmatch(identity) is not None
            and status in {"online", "offline"},
            "Proxmox returned a malformed node listing.",
        )
        _require(identity not in seen, "Proxmox returned duplicate node identities.")
        seen.add(identity)
        if status == "online":
            online.append(identity)
    _require(bool(online), "Proxmox returned no online nodes.")
    return online


def _capacity_storage_count(
    raw_nodes: object, reads: object, source_node: object, restore_bytes: int | None
) -> int:
    online = _online_nodes(raw_nodes)
    _require(
        isinstance(source_node, str)
        and NODE_ID.fullmatch(source_node) is not None
        and source_node in online,
        "The source node is missing from the online cluster listing.",
    )
    _require(
        isinstance(reads, list) and all(isinstance(read, Mapping) for read in reads),
        "Proxmox returned an incomplete storage-capacity listing.",
    )
    reads_by_node: dict[str, Mapping[str, object]] = {}
    for read in reads:
        node, status = read.get("item"), read.get("status")
        response = read.get("json")
        data = response.get("data") if isinstance(response, Mapping) else read.get("data")
        _require(
            isinstance(node, str)
            and node in online
            and node not in reads_by_node
            and status == 200
            and isinstance(data, list)
            and all(isinstance(item, Mapping) for item in data),
            "Proxmox returned a malformed storage-capacity listing.",
        )
        reads_by_node[node] = {"data": data}
    _require(
        set(reads_by_node) == set(online),
        "Proxmox returned an incomplete storage-capacity listing.",
    )

    eligible_storage_ids: set[str] = set()
    for node in online:
        seen_on_node: set[str] = set()
        for storage in reads_by_node[node]["data"]:
            identity = storage.get("storage")
            backend = storage.get("type")
            active = storage.get("active")
            content = storage.get("content")
            _require(
                isinstance(identity, str)
                and STORAGE_ID.fullmatch(identity) is not None
                and isinstance(backend, str)
                and bool(backend.strip())
                and (active is True or active is False or (type(active) is int and active in {0, 1}))
                and isinstance(content, str),
                "Proxmox returned a malformed storage-capacity listing.",
            )
            _require(
                identity not in seen_on_node,
                "Proxmox returned duplicate storage identities on one node.",
            )
            seen_on_node.add(identity)
            content_types = {entry.strip() for entry in content.split(",") if entry.strip()}
            if active != 1 or "images" not in content_types:
                continue
            total, available = storage.get("total"), storage.get("avail")
            _require(
                _valid_nonnegative_integer(total)
                and total > 0
                and _valid_nonnegative_integer(available)
                and available <= total,
                "Proxmox returned a malformed image-storage capacity.",
            )
            remaining_after_restore = available - restore_bytes if restore_bytes is not None else -1
            # Integer arithmetic preserves the inclusive 30% threshold exactly.
            if (
                restore_bytes is not None
                and node != source_node
                and remaining_after_restore >= 0
                and remaining_after_restore * 10 >= total * 3
            ):
                eligible_storage_ids.add(identity)
    return len(eligible_storage_ids)


def inspect(payload: Mapping[str, object]) -> dict[str, object]:
    layout_parsed, sizes_complete, inclusion_verified, restore_bytes = _source_facts(
        payload.get("artifact_receipt")
    )
    storage_count = _capacity_storage_count(
        payload.get("nodes"), payload.get("storage_reads"), payload.get("source_node"), restore_bytes
    )
    nextid = payload.get("nextid")
    _require(
        isinstance(nextid, Mapping)
        and nextid.get("status") == 200
        and isinstance(nextid.get("json"), Mapping)
        and _valid_nextid(nextid["json"].get("data")),
        "Proxmox returned a malformed next-VMID response.",
    )
    return {
        "survey": "read-only-restore-target-feasibility",
        "source_layout_parsed": layout_parsed,
        "source_disk_sizes_complete": sizes_complete,
        "backup_inclusion_verified": inclusion_verified,
        "storage_capacity_basis": "proxmox-reported",
        "storage_count_scope": "distinct-image-storage-ids-off-source-online-nodes",
        "image_storage_ids_off_source_meeting_reported_capacity_count": storage_count,
        "other_node_reported_capacity_sufficient": storage_count > 0,
        "unused_cluster_vmid_available_now": True,
        "vmid_reserved": False,
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
            raise ValueError("Unsupported restore feasibility inspection operation.")
        result = inspect(payload)
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"refusal": _safe_refusal(exc)}, separators=(",", ":")))
        return 2
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
