#!/usr/bin/env python3
"""Validate protected Proxmox reads and render a private PBS candidate receipt."""

from __future__ import annotations

import json
import re
import sys
from typing import Any

FILE_STORAGE_TYPES = {"dir", "nfs", "cifs", "cephfs", "glusterfs"}
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class Refusal(Exception):
    """A fixed, non-sensitive input refusal."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise Refusal(message)


def parse_id(value: Any, label: str) -> int:
    require(type(value) in (int, str), f"Malformed Proxmox {label} response.")
    rendered = str(value)
    require(rendered.isdigit(), f"Malformed Proxmox {label} response.")
    number = int(rendered)
    require(100 <= number <= 999_999, f"Malformed Proxmox {label} response.")
    return number


def rows_by_storage(value: Any, section: str) -> dict[str, dict[str, Any]]:
    require(isinstance(value, list), f"Proxmox returned an incomplete {section} response.")
    result: dict[str, dict[str, Any]] = {}
    for row in value:
        require(isinstance(row, dict), f"Proxmox returned a malformed {section} response.")
        storage_id = row.get("storage")
        require(
            isinstance(storage_id, str) and ID_PATTERN.fullmatch(storage_id) is not None,
            f"Proxmox returned a malformed {section} response.",
        )
        require(storage_id not in result, f"Proxmox returned duplicate {section} rows.")
        require(isinstance(row.get("type"), str) and row["type"], f"Proxmox returned a malformed {section} response.")
        result[storage_id] = row
    return result


def content_set(value: Any, section: str) -> set[str]:
    require(isinstance(value, str) and value, f"Proxmox returned incomplete {section} capabilities.")
    items = value.split(",")
    require(
        all(item and item.strip() == item for item in items) and len(set(items)) == len(items),
        f"Proxmox returned malformed {section} capabilities.",
    )
    return set(items)


def enabled(config: dict[str, Any]) -> bool:
    value = config.get("disable", 0)
    require(
        type(value) in (bool, int) and value in (False, True, 0, 1),
        "Proxmox returned malformed storage configuration.",
    )
    return value in (False, 0)


def active(status: dict[str, Any]) -> bool:
    value = status.get("active")
    require(
        type(value) in (bool, int) and value in (False, True, 0, 1),
        "Proxmox returned malformed storage activity status.",
    )
    return value is True or (type(value) is int and value == 1)


def in_scope(config: dict[str, Any], target_node: str) -> bool:
    nodes = config.get("nodes")
    if nodes in (None, ""):
        return True
    require(isinstance(nodes, str), "Proxmox returned malformed storage node scope.")
    entries = nodes.split(",")
    require(
        all(node and node.strip() == node for node in entries)
        and len(set(entries)) == len(entries),
        "Proxmox returned malformed storage node scope.",
    )
    return target_node in entries


def inspect(payload: dict[str, Any]) -> dict[str, Any]:
    target_node = payload.get("target_node")
    declared_storage = payload.get("declared_vm_image_storage")
    require(
        isinstance(target_node, str) and ID_PATTERN.fullmatch(target_node) is not None,
        "Private PBS node declaration is malformed.",
    )
    require(
        isinstance(declared_storage, str)
        and ID_PATTERN.fullmatch(declared_storage) is not None,
        "Private PBS image-storage declaration is malformed.",
    )

    seed_status = payload.get("seed_status")
    require(seed_status == 200, "Proxmox next-ID seed request failed.")
    seed = parse_id(payload.get("seed_data"), "next-ID seed")

    expected = list(range(seed, min(seed + 64, 1_000_000)))
    checks = payload.get("nextid_checks")
    require(
        isinstance(checks, list) and len(checks) == len(expected),
        "Proxmox exact next-ID responses are incomplete.",
    )
    free_ids: list[int] = []
    for candidate, response in zip(expected, checks, strict=True):
        require(isinstance(response, dict), "Proxmox exact next-ID response is malformed.")
        require(
            parse_id(response.get("candidate"), "exact next-ID") == candidate,
            "Proxmox exact next-ID request order is invalid.",
        )
        status = response.get("status")
        require(status in (200, 400), "Proxmox exact next-ID response was not authoritative.")
        if status == 200:
            require(
                parse_id(response.get("data"), "exact next-ID") == candidate,
                "Proxmox did not echo the requested free VMID.",
            )
            free_ids.append(candidate)
    require(len(free_ids) >= 2, "Fewer than two exact free Proxmox VMIDs were proven.")

    require(payload.get("node_storage_status") == 200, "Proxmox node-storage response failed.")
    require(payload.get("cluster_storage_status") == 200, "Proxmox cluster-storage response failed.")
    statuses = rows_by_storage(payload.get("node_storage"), "node-storage")
    configs = rows_by_storage(payload.get("cluster_storage"), "cluster-storage")

    for storage_id, config in configs.items():
        if enabled(config) and in_scope(config, target_node):
            require(
                storage_id in statuses,
                "Proxmox storage configuration and node status are incomplete.",
            )
    for storage_id in statuses:
        require(
            storage_id in configs,
            "Proxmox storage status has no matching cluster configuration.",
        )

    import_stores: list[dict[str, Any]] = []
    image_stores: list[dict[str, Any]] = []
    for storage_id in sorted(statuses.keys() & configs.keys()):
        status = statuses[storage_id]
        config = configs[storage_id]
        require(status["type"] == config["type"], "Proxmox storage status and configuration disagree.")
        require("content" in status and "content" in config, "Proxmox returned incomplete storage capabilities.")
        status_content = content_set(status["content"], "node-storage")
        config_content = content_set(config["content"], "cluster-storage")
        shared_content = status_content & config_content
        eligible = active(status) and enabled(config) and in_scope(config, target_node)
        if not eligible:
            continue
        entry = {"storage": storage_id, "type": config["type"], "content": sorted(shared_content)}
        if config["type"] in FILE_STORAGE_TYPES and "import" in shared_content:
            import_stores.append(entry)
        if "images" in shared_content:
            image_stores.append(entry)

    return {
        "target_node": target_node,
        "template_vmid_candidate": free_ids[0],
        "pbs_vmid_candidate": free_ids[1],
        "import_store_status": "candidates_present" if import_stores else "none",
        "active_file_import_stores": import_stores,
        "active_image_stores": image_stores,
        "declared_vm_image_storage_available": any(item["storage"] == declared_storage for item in image_stores),
    }


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        require(isinstance(payload, dict), "Proxmox candidate survey input is malformed.")
        result = inspect(payload)
    except (json.JSONDecodeError, Refusal, TypeError, ValueError):
        print(json.dumps({"refusal": "Proxmox candidate data failed strict validation."}, separators=(",", ":")))
        return 2
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
