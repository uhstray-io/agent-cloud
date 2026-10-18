"""Validate Proxmox census responses and emit only fixed, aggregate facts."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Mapping

NODE_NAME = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
STORAGE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
VMID = re.compile(r"[1-9][0-9]{0,8}\Z")
BACKENDS = {"lvm", "lvmthin", "dir", "nfs", "cifs", "cephfs", "rbd", "pbs", "zfspool"}


def _fail(reason: str) -> None:
    raise ValueError(reason)


def _api_rows(value: object, label: str) -> list[object]:
    if not isinstance(value, Mapping) or type(value.get("status")) is not int or value["status"] != 200:
        _fail(f"{label} read was incomplete")
    body = value.get("json")
    if not isinstance(body, Mapping) or not isinstance(body.get("data"), list):
        _fail(f"{label} response was malformed")
    return body["data"]


def _identity_rows(nodes: object, vms: object) -> tuple[dict[str, str], list[dict[str, object]]]:
    node_rows = _api_rows(nodes, "node")
    vm_rows = _api_rows(vms, "VM")
    node_status: dict[str, str] = {}
    for row in node_rows:
        if not isinstance(row, Mapping):
            _fail("node response was malformed")
        name, status = row.get("node"), row.get("status")
        if (not isinstance(name, str) or not NODE_NAME.fullmatch(name)
                or not isinstance(status, str) or status not in {"online", "offline"}):
            _fail("node response was malformed")
        if name in node_status:
            _fail("node response was ambiguous")
        node_status[name] = status

    seen_vmids: set[str] = set()
    records: list[dict[str, object]] = []
    for row in vm_rows:
        if not isinstance(row, Mapping):
            _fail("VM response was malformed")
        kind, node, raw_vmid = row.get("type"), row.get("node"), row.get("vmid")
        vmid = str(raw_vmid) if type(raw_vmid) is int else raw_vmid
        if (not isinstance(kind, str) or kind not in {"qemu", "lxc"}
                or not isinstance(node, str) or node not in node_status):
            _fail("VM response was malformed")
        if not isinstance(vmid, str) or not VMID.fullmatch(vmid):
            _fail("VM response was malformed")
        if vmid in seen_vmids:
            _fail("VM response was ambiguous")
        seen_vmids.add(vmid)
        template = row.get("template", 0)
        if type(template) is not int or template not in {0, 1} or (kind == "lxc" and template == 1):
            _fail("VM response was malformed")
        records.append({"type": kind, "node": node, "vmid": vmid, "template": template})
    return node_status, records


def template_requests(payload: object) -> list[dict[str, object]]:
    if not isinstance(payload, Mapping):
        _fail("census input was malformed")
    statuses, records = _identity_rows(payload.get("nodes"), payload.get("vms"))
    requests = [
        {"node": row["node"], "vmid": row["vmid"]}
        for row in records
        if row["type"] == "qemu" and row["template"] == 1
    ]
    if any(statuses[request["node"]] != "online" for request in requests):
        _fail("template configuration read is unavailable")
    return requests


def inspect(payload: object) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        _fail("census input was malformed")
    nodes = payload.get("nodes")
    vms = payload.get("vms")
    storages = payload.get("storages")
    nextid = payload.get("nextid")
    configs = payload.get("template_configs")
    node_status, vm_records = _identity_rows(nodes, vms)
    storage_rows = _api_rows(storages, "storage")

    if type(nextid) is int:
        next_vmid = str(nextid)
    elif isinstance(nextid, str):
        next_vmid = nextid
    else:
        _fail("next VM identity response was malformed")
    if not VMID.fullmatch(next_vmid):
        _fail("next VM identity response was malformed")
    next_vm_identity_available = next_vmid not in {str(row["vmid"]) for row in vm_records}

    storage_ids: set[str] = set()
    backend_counts = dict.fromkeys(sorted(BACKENDS), 0)
    backend_counts["other"] = 0
    image_capable = 0
    backup_capable = 0
    for row in storage_rows:
        if not isinstance(row, Mapping):
            _fail("storage response was malformed")
        storage_id, kind, content = row.get("storage"), row.get("type"), row.get("content")
        if (not isinstance(storage_id, str) or not STORAGE_ID.fullmatch(storage_id)
                or not isinstance(kind, str) or not kind.strip()
                or not isinstance(content, str)):
            _fail("storage response was malformed")
        if storage_id in storage_ids:
            _fail("storage response was ambiguous")
        storage_ids.add(storage_id)
        kind = kind.strip().lower()
        backend_counts[kind if kind in BACKENDS else "other"] += 1
        content_types = {value.strip().lower() for value in content.split(",") if value.strip()}
        image_capable += "images" in content_types
        backup_capable += "backup" in content_types

    template_rows = [row for row in vm_records if row["type"] == "qemu" and row["template"] == 1]
    config_rows = _api_rows(configs, "template configuration")
    if len(config_rows) != len(template_rows):
        _fail("template configuration census was incomplete")
    configs_by_identity: dict[tuple[str, str], Mapping[str, object]] = {}
    for row in config_rows:
        if not isinstance(row, Mapping):
            _fail("template configuration response was malformed")
        item, status, body = row.get("item"), row.get("status"), row.get("json")
        if not isinstance(item, Mapping) or type(status) is not int or status != 200:
            _fail("template configuration read was incomplete")
        node, vmid = item.get("node"), item.get("vmid")
        if not isinstance(node, str) or not isinstance(vmid, str) or not isinstance(body, Mapping):
            _fail("template configuration response was malformed")
        data = body.get("data")
        if not isinstance(data, Mapping):
            _fail("template configuration response was malformed")
        identity = (node, vmid)
        if identity in configs_by_identity:
            _fail("template configuration census was ambiguous")
        configs_by_identity[identity] = data

    expected_identities = {(str(row["node"]), str(row["vmid"])) for row in template_rows}
    if set(configs_by_identity) != expected_identities:
        _fail("template configuration census was ambiguous")

    cloud_init_templates = 0
    debian_name_hints = 0
    linux_guest_type_hints = 0
    for config in configs_by_identity.values():
        name = config.get("name")
        ostype = config.get("ostype")
        if (name is not None and not isinstance(name, str)) or (ostype is not None and not isinstance(ostype, str)):
            _fail("template configuration response was malformed")
        if isinstance(name, str) and "debian" in name.casefold():
            debian_name_hints += 1
        if isinstance(ostype, str) and ostype.casefold().startswith("l"):
            linux_guest_type_hints += 1
        if any(isinstance(value, str) and "cloudinit" in value.casefold()
               for key, value in config.items() if re.fullmatch(r"(?:ide|sata|scsi|virtio)[0-9]+", str(key))):
            cloud_init_templates += 1

    # Proxmox config names and guest OS type are not image provenance. The census has
    # no trusted manifest or digest attestation from which Debian provenance can be proven.
    return {
        "survey": "pbs-vm-census",
        "read_only": True,
        "inventory_complete": True,
        "node_count": len(node_status),
        "online_node_count": sum(status == "online" for status in node_status.values()),
        "offline_node_count": sum(status == "offline" for status in node_status.values()),
        "vm_count": len(vm_records),
        "qemu_vm_count": sum(row["type"] == "qemu" for row in vm_records),
        "lxc_container_count": sum(row["type"] == "lxc" for row in vm_records),
        "unused_vm_identity_available": next_vm_identity_available,
        "template_count": len(template_rows),
        "cloud_init_template_count": cloud_init_templates,
        "debian_name_hint_count": debian_name_hints,
        "linux_guest_type_hint_count": linux_guest_type_hints,
        "proven_debian_image_provenance_count": 0,
        "storage_count": len(storage_ids),
        "storage_backend_counts": backend_counts,
        "image_capable_storage_count": image_capable,
        "backup_capable_storage_count": backup_capable,
    }


def main() -> int:
    try:
        operation = sys.argv[1] if len(sys.argv) == 2 else ""
        payload = json.load(sys.stdin)
        if operation == "template-requests":
            result = template_requests(payload)
        elif operation == "inspect":
            result = inspect(payload)
        else:
            result = None
        if result is None:
            _fail("unsupported census operation")
    except (ValueError, TypeError, json.JSONDecodeError):
        print("Proxmox census refused: input was malformed, incomplete, or ambiguous.", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
