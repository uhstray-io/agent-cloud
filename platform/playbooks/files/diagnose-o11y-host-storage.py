#!/usr/bin/env python3
"""Collect sanitized read-only receiver-host and Podman storage diagnostics."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Any

INSPECT_FORMAT = (
    '{"pid_mode":{{json .HostConfig.PidMode}},"state":{{json .State.Status}},'
    '"mounts":{{json .Mounts}},"ports":{{json .HostConfig.PortBindings}},'
    '"networks":{{json .NetworkSettings.Networks}}}'
)


class Unavailable(Exception):
    """A fixed, safe-to-display failure reason."""


def _json_object(value: Any, reason: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        raise Unavailable(reason) from None
    if not isinstance(parsed, dict):
        raise Unavailable(reason)
    return parsed


def filesystem_capacity(path: str) -> dict[str, int] | None:
    try:
        stat = os.statvfs(path)
        device = os.stat(path).st_dev
    except OSError:
        return None
    unit = stat.f_frsize or stat.f_bsize
    total, available = stat.f_blocks * unit, stat.f_bavail * unit
    if total <= 0 or available < 0 or available > total:
        return None
    return {"total_bytes": total, "available_bytes": available, "device_id": device}


def block_device_number(path: str) -> str | None:
    """Resolve an LVM device path to its kernel device number without reading data."""
    try:
        metadata = os.stat(path)
    except OSError:
        return None
    if not stat.S_ISBLK(metadata.st_mode):
        return None
    return f"{os.major(metadata.st_rdev)}:{os.minor(metadata.st_rdev)}"


def _reported_bytes(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise Unavailable("lvm_capacity_invalid")
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise Unavailable("lvm_capacity_invalid") from None
    if not amount.is_finite() or amount < 0 or amount != amount.to_integral_value():
        raise Unavailable("lvm_capacity_invalid")
    return int(amount)


def _lvm_rows(raw: Any, report_name: str) -> list[dict[str, Any]]:
    report = _json_object(raw, "lvm_report_invalid").get("report")
    if not isinstance(report, list) or len(report) != 1 or not isinstance(report[0], dict):
        raise Unavailable("lvm_report_invalid")
    rows = report[0].get(report_name)
    if not isinstance(rows, list):
        raise Unavailable("lvm_report_invalid")
    if any(not isinstance(row, dict) for row in rows):
        raise Unavailable("lvm_report_invalid")
    return rows


def root_lvm_capacity(
    lvs_raw: Any,
    vgs_raw: Any,
    root_major_minor: str,
    root_lv_size_bytes: int,
    device_number_reader: Callable[[str], str | None] = block_device_number,
) -> dict[str, Any]:
    """Join the mounted root LV to read-only LVM reports, returning values only."""
    try:
        if lvs_raw in (None, "") or vgs_raw in (None, ""):
            raise Unavailable("lvm_unavailable")
        lvs = _lvm_rows(lvs_raw, "lv")
        vgs = _lvm_rows(vgs_raw, "vg")
        matches: list[dict[str, Any]] = []
        for row in lvs:
            path, vg_name = row.get("lv_path"), row.get("vg_name")
            if not isinstance(path, str) or not path.startswith("/"):
                continue
            if not isinstance(vg_name, str) or not vg_name:
                raise Unavailable("lvm_report_invalid")
            size = _reported_bytes(row.get("lv_size"))
            if device_number_reader(path) == root_major_minor:
                matches.append({"vg_name": vg_name, "size_bytes": size})
        if len(matches) != 1 or matches[0]["size_bytes"] != root_lv_size_bytes:
            raise Unavailable("root_logical_volume_unresolved")

        root_vg_name = matches[0]["vg_name"]
        vg_matches: list[dict[str, Any]] = []
        for row in vgs:
            name = row.get("vg_name")
            if not isinstance(name, str) or not name:
                raise Unavailable("lvm_report_invalid")
            free_bytes = _reported_bytes(row.get("vg_free"))
            if name == root_vg_name:
                vg_matches.append({"free_bytes": free_bytes})
        if len(vg_matches) != 1:
            raise Unavailable("root_volume_group_unresolved")
        return {
            "status": "observed",
            "logical_volume_size_bytes": matches[0]["size_bytes"],
            "volume_group_free_bytes": vg_matches[0]["free_bytes"],
        }
    except Unavailable as exc:
        return {"status": "unavailable", "reason": str(exc)}


def _root_block_chain(raw: Any, root_major_minor: str) -> list[dict[str, Any]]:
    block = _json_object(raw, "block_topology_invalid").get("blockdevices")
    if not isinstance(block, list) or not block:
        raise Unavailable("block_topology_invalid")
    devices: dict[str, dict[str, Any]] = {}
    parents: dict[str, str] = {}
    major_minors: dict[str, list[str]] = {}

    def visit(node: Any, parent: str = "") -> None:
        if not isinstance(node, dict):
            raise Unavailable("block_topology_invalid")
        path, kind, size = node.get("name"), node.get("type"), node.get("size")
        if (not isinstance(path, str) or not path.startswith("/") or not isinstance(kind, str)
                or not kind or isinstance(size, bool) or not isinstance(size, int) or size < 0
                or path in devices):
            raise Unavailable("block_topology_invalid")
        devices[path] = {"path": path, "type": kind, "size_bytes": size}
        major_minor = node.get("maj:min")
        if isinstance(major_minor, str) and major_minor:
            major_minors.setdefault(major_minor, []).append(path)
        if parent:
            parents[path] = parent
        children = node.get("children") or []
        if not isinstance(children, list):
            raise Unavailable("block_topology_invalid")
        for child in children:
            visit(child, path)

    for item in block:
        visit(item)

    # findmnt and lsblk both expose the kernel device number. Use it to join
    # /dev/mapper aliases to /dev/dm-N without inferring from path spelling.
    candidates = major_minors.get(root_major_minor, [])
    if len(candidates) != 1:
        raise Unavailable("root_block_device_unresolved")

    chain: list[dict[str, Any]] = []
    seen: set[str] = set()
    current = candidates[0]
    while current:
        if current in seen:
            raise Unavailable("block_topology_cycle")
        seen.add(current)
        chain.append(devices[current])
        node_stack = list(block)
        node: dict[str, Any] | None = None
        while node_stack:
            candidate = node_stack.pop()
            if candidate["name"] == current:
                node = candidate
                break
            node_stack.extend(candidate.get("children") or [])
        if node is None:
            raise Unavailable("block_topology_invalid")
        parent_name = node.get("pkname")
        if isinstance(parent_name, str) and parent_name:
            matches = [parent_name] if parent_name in devices else [
                path for path in devices if os.path.basename(path) == os.path.basename(parent_name)
            ]
            if len(matches) != 1:
                raise Unavailable("block_parent_unresolved")
            current = matches[0]
        else:
            current = parents.get(current, "")
    return chain


def diagnose(
    payload: dict[str, Any],
    capacity_reader: Callable[[str], dict[str, int] | None] = filesystem_capacity,
    device_number_reader: Callable[[str], str | None] = block_device_number,
) -> dict[str, Any]:
    try:
        exporter = _json_object(payload.get("exporter_inspect"), "exporter_inspect_invalid")
        pid = exporter.get("pid_mode")
        if not isinstance(pid, (str, type(None))):
            raise Unavailable("exporter_pid_mode_invalid")
        state = exporter.get("state")
        if not isinstance(state, str):
            raise Unavailable("exporter_state_invalid")
        state = state.lower()
        state = state if state in {"running", "created", "paused", "exited", "stopped"} else "other"

        mounts = exporter.get("mounts")
        if not isinstance(mounts, list) or any(not isinstance(item, dict) for item in mounts):
            raise Unavailable("exporter_mounts_invalid")
        root_mounts = [item for item in mounts if item.get("Source") == "/" or item.get("Destination") == "/host"]
        if any(not isinstance(item.get("RW"), bool) for item in root_mounts):
            raise Unavailable("exporter_mounts_invalid")
        root_read_only = (
            len(root_mounts) == 1 and root_mounts[0].get("Source") == "/"
            and root_mounts[0].get("Destination") == "/host" and root_mounts[0]["RW"] is False
        )

        networks = exporter.get("networks")
        if not isinstance(networks, dict) or any(not isinstance(key, str) for key in networks):
            raise Unavailable("exporter_networks_invalid")
        ports = exporter.get("ports")
        if ports is None:
            published_ports = 0
        elif isinstance(ports, dict):
            published_ports = 0
            for bindings in ports.values():
                if bindings is None:
                    continue
                if not isinstance(bindings, list) or any(not isinstance(row, dict) for row in bindings):
                    raise Unavailable("exporter_ports_invalid")
                published_ports += len(bindings)
        else:
            raise Unavailable("exporter_ports_invalid")

        podman = _json_object(payload.get("podman_info"), "podman_info_invalid")
        store = podman.get("store")
        if not isinstance(store, dict):
            raise Unavailable("podman_store_invalid")
        paths = [store.get("volumePath"), store.get("graphRoot")]
        if any(not isinstance(path, str) or not os.path.isabs(path) or not os.path.isdir(path) for path in paths):
            raise Unavailable("podman_storage_path_unresolved")
        capacities = [capacity_reader(path) for path in paths]
        if any(not isinstance(item, dict) for item in capacities):
            raise Unavailable("podman_storage_capacity_unavailable")
        for capacity in capacities:
            assert isinstance(capacity, dict)
            values = [capacity.get(key) for key in ("total_bytes", "available_bytes", "device_id")]
            if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
                raise Unavailable("podman_storage_capacity_invalid")
            total, available, _device = values
            if total <= 0 or available < 0 or available > total:
                raise Unavailable("podman_storage_capacity_invalid")

        root = _json_object(payload.get("root_mount"), "root_mount_invalid").get("filesystems")
        if not isinstance(root, list) or len(root) != 1 or not isinstance(root[0], dict):
            raise Unavailable("root_mount_invalid")
        source, fstype, major_minor = (
            root[0].get("source"), root[0].get("fstype"), root[0].get("maj:min")
        )
        if (not isinstance(source, str) or not source or not isinstance(fstype, str) or not fstype
                or not isinstance(major_minor, str) or not major_minor):
            raise Unavailable("root_mount_invalid")
        chain = _root_block_chain(payload.get("block_topology"), major_minor)
        root_capacity = capacity_reader("/")
        if not isinstance(root_capacity, dict):
            raise Unavailable("root_filesystem_capacity_unavailable")
        root_total, root_available = root_capacity.get("total_bytes"), root_capacity.get("available_bytes")
        if (isinstance(root_total, bool) or not isinstance(root_total, int) or root_total <= 0
                or isinstance(root_available, bool) or not isinstance(root_available, int)
                or root_available < 0 or root_available > root_total):
            raise Unavailable("root_filesystem_capacity_invalid")
        root_lv = chain[0] if chain[0]["type"] == "lvm" else None
        if root_lv is None:
            lvm_report = {"status": "not_lvm"}
        else:
            lvm_report = root_lvm_capacity(
                payload.get("lvs"), payload.get("vgs"), major_minor,
                root_lv["size_bytes"], device_number_reader,
            )

        def capacity_report(item: dict[str, int]) -> dict[str, int | float]:
            total, available = item["total_bytes"], item["available_bytes"]
            return {"total_bytes": total, "available_bytes": available,
                    "free_percent": round(100 * available / total, 2)}

        volume_capacity, graph_capacity = capacities
        assert isinstance(volume_capacity, dict) and isinstance(graph_capacity, dict)
        return {
            "status": "observed",
            "node_exporter": {
                "pid_mode": "host" if pid == "host" else "private" if pid in (None, "", "private") else "other",
                "state": state,
                "running": state == "running",
                "host_root_mount_read_only": root_read_only,
                "network_count": len(networks),
                "published_port_count": published_ports,
            },
            "guest_root": {
                "filesystem_type": fstype,
                "filesystem_total_bytes": root_total,
                "filesystem_available_bytes": root_available,
                "block_chain": [{"type": item["type"], "size_bytes": item["size_bytes"]} for item in chain],
                "lvm": lvm_report,
            },
            "podman_storage": {
                "volume_path_filesystem": capacity_report(volume_capacity),
                "graph_root_filesystem": capacity_report(graph_capacity),
                "volume_path_and_graph_root_share_filesystem": (
                    volume_capacity["device_id"] == graph_capacity["device_id"]
                ),
            },
        }
    except Unavailable as exc:
        return {"status": "unavailable", "reason": str(exc)}


def _read(argv: list[str]) -> str | None:
    try:
        result = subprocess.run(argv, check=False, capture_output=True, text=True, timeout=30,
                                env={**os.environ, "LC_ALL": "C"})
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def collect() -> dict[str, Any]:
    commands = {
        "exporter_inspect": ["podman", "inspect", "--format", INSPECT_FORMAT, "o11y-node-exporter"],
        "podman_info": ["podman", "info", "--format", "json"],
        "root_mount": ["findmnt", "--json", "--output", "SOURCE,FSTYPE,MAJ:MIN", "--target", "/"],
        "block_topology": ["lsblk", "--json", "--bytes", "--paths", "--output",
                           "NAME,TYPE,SIZE,PKNAME,MAJ:MIN"],
        "lvs": ["lvs", "--readonly", "--reportformat", "json", "--units", "b", "--nosuffix",
                "--options", "lv_path,vg_name,lv_size"],
        "vgs": ["vgs", "--readonly", "--reportformat", "json", "--units", "b", "--nosuffix",
                "--options", "vg_name,vg_free"],
    }
    payload: dict[str, Any] = {}
    for key, command in commands.items():
        value = _read(command)
        if value is None:
            if key in {"lvs", "vgs"}:
                payload[key] = None
                continue
            return {"status": "unavailable", "reason": f"{key}_unavailable"}
        payload[key] = value
    return diagnose(payload)


def main() -> int:
    try:
        report = collect()
    except Exception:
        report = {"status": "unavailable", "reason": "diagnostic_internal_error"}
    print(json.dumps(report, sort_keys=True))
    return 0 if report.get("status") == "observed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
