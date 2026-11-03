#!/usr/bin/env python3
"""Prove the PBS data disk is the serial-pinned ext4 UUID mount.

This is intentionally read-only. PBS configuration invokes it before package
installation and immediately before datastore changes so a stale Ansible fact
cannot authorize a write to a different filesystem.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable

MOUNTPOINT = "/srv/pbs-data"


def run_json(runner: Callable[..., subprocess.CompletedProcess], argv: list[str]) -> dict:
    result = runner(argv, check=True, capture_output=True, text=True)
    parsed = json.loads(result.stdout)
    if not isinstance(parsed, dict):
        raise ValueError("expected JSON object")
    return parsed


def walk(nodes: list[dict]):
    for node in nodes:
        yield node
        yield from walk(node.get("children") or [])


def inspect_mount(vmid: str, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> dict:
    if not re.fullmatch(r"[1-9][0-9]{2,5}", str(vmid)):
        raise ValueError("declared VMID is invalid")

    serial = f"ac-pbs-data-{vmid}"
    block = run_json(runner, [
        "lsblk", "--json", "--output", "PATH,TYPE,SERIAL,UUID,FSTYPE,MOUNTPOINTS",
    ])
    devices = [node for node in walk(block.get("blockdevices", [])) if node.get("serial") == serial]
    if len(devices) != 1:
        raise ValueError("the declared PBS data disk serial must identify exactly one device")

    device = devices[0]
    path = device.get("path")
    uuid = device.get("uuid")
    if (device.get("type") != "disk" or not isinstance(path, str) or not path.startswith("/dev/")
            or not isinstance(uuid, str) or not uuid or device.get("fstype") != "ext4"):
        raise ValueError("the serial-pinned whole disk must carry an ext4 filesystem UUID")

    mounted = run_json(runner, [
        "findmnt", "--json", "--mountpoint", MOUNTPOINT,
        "--output", "SOURCE,TARGET,FSTYPE,UUID,OPTIONS",
    ])
    filesystems = mounted.get("filesystems", [])
    if len(filesystems) != 1:
        raise ValueError("the declared PBS data mountpoint must resolve to exactly one filesystem")
    mount = filesystems[0]
    source = mount.get("source", "")
    if (mount.get("target") != MOUNTPOINT or mount.get("fstype") != "ext4"
            or mount.get("uuid") != uuid or os.path.realpath(source) != os.path.realpath(path)):
        raise ValueError("the mounted PBS data filesystem does not match the serial-pinned disk UUID")
    if "noatime" not in (mount.get("options") or "").split(","):
        raise ValueError("the PBS data mount must retain the declared noatime option")

    fstab = run_json(runner, [
        "findmnt", "--fstab", "--json", "--mountpoint", MOUNTPOINT,
        "--output", "SOURCE,TARGET,FSTYPE,OPTIONS",
    ])
    entries = fstab.get("filesystems", [])
    if len(entries) != 1 or entries[0].get("target") != MOUNTPOINT:
        raise ValueError("the PBS data mountpoint must have exactly one fstab entry")
    entry = entries[0]
    if entry.get("source") != f"UUID={uuid}" or entry.get("fstype") != "ext4":
        raise ValueError("the PBS data fstab entry must mount the proven UUID as ext4")
    if "noatime" not in (entry.get("options") or "").split(","):
        raise ValueError("the PBS data fstab entry must retain the declared noatime option")

    return {"device": path, "uuid": uuid, "target": MOUNTPOINT, "fstype": "ext4"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vmid", required=True)
    args = parser.parse_args()
    try:
        result = inspect_mount(args.vmid)
    except (OSError, subprocess.CalledProcessError, ValueError, json.JSONDecodeError) as exc:
        print(f"PBS data mount identity check failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
