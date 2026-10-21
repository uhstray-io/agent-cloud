#!/usr/bin/env python3
"""Select the one declared PBS virtual disk from lsblk JSON, refusing ambiguity."""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any


class Refusal(ValueError):
    pass


def _walk(devices: list[dict[str, Any]]):
    for device in devices:
        yield device
        yield from _walk(device.get("children") or [])


def select_disk(document: dict[str, Any], expected_serial: str, expected_size: int) -> dict[str, Any]:
    devices = document.get("blockdevices")
    if not isinstance(devices, list):
        raise Refusal("lsblk did not return a block device list")
    matches = [d for d in devices if d.get("type") == "disk" and d.get("serial") == expected_serial]
    if len(matches) != 1:
        raise Refusal("declared virtual disk serial is missing or ambiguous")
    disk = matches[0]
    filesystem_uuid = str(disk.get("uuid") or "")
    if filesystem_uuid:
        uuid_matches = [d for d in _walk(devices) if d.get("uuid") == filesystem_uuid]
        if len(uuid_matches) != 1 or uuid_matches[0] is not disk:
            raise Refusal("declared filesystem UUID is not unique across guest block devices")
    try:
        size = int(disk["size"])
    except (KeyError, TypeError, ValueError) as exc:
        raise Refusal("declared virtual disk size is unavailable") from exc
    if size != expected_size:
        raise Refusal("declared virtual disk size does not match inventory")
    if disk.get("type") != "disk" or disk.get("pkname"):
        raise Refusal("declared identity resolved to a partition or dependent device")
    return disk


def disk_state(disk: dict[str, Any], mountpoint: str) -> str:
    children = disk.get("children") or []
    fstype = disk.get("fstype") or ""
    label = disk.get("label") or ""
    mounted = disk.get("mountpoints") or []
    mounted = [path for path in mounted if path]
    if children:
        raise Refusal("declared virtual disk already has partitions or child devices")
    if fstype:
        if fstype != "ext4" or label != "pbs-data":
            raise Refusal("existing filesystem is not the PBS data filesystem created by this workflow")
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", str(disk.get("uuid") or "")):
            raise Refusal("existing PBS data filesystem has no valid UUID")
        if not mounted:
            return "existing_unmounted"
        if mounted == [mountpoint]:
            return "existing"
        raise Refusal("existing PBS data filesystem is mounted at an unexpected path")
    if mounted or disk.get("partuuid"):
        raise Refusal("blank-disk identity conflicts with existing mount or partition metadata")
    return "blank"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-serial", required=True)
    parser.add_argument("--expected-size", required=True, type=int)
    parser.add_argument("--mountpoint", required=True)
    args = parser.parse_args()
    try:
        document = json.load(sys.stdin)
        disk = select_disk(document, args.expected_serial, args.expected_size)
        state = disk_state(disk, args.mountpoint)
    except (json.JSONDecodeError, Refusal) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    name = disk.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9._+-]+", name):
        print("refused: unsafe device name", file=sys.stderr)
        return 2
    print(json.dumps({"device": f"/dev/{name}", "state": state}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
