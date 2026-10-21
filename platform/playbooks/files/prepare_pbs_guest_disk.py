#!/usr/bin/env python3
"""Select the one declared PBS virtual disk from lsblk JSON, refusing ambiguity."""

from __future__ import annotations

import argparse
import errno
import json
import os
import re
import stat
import subprocess
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


def _mount_targets(tree: dict[str, Any]) -> list[str]:
    targets: list[str] = []
    for filesystem in tree.get("filesystems") or []:
        target = filesystem.get("target")
        if isinstance(target, str):
            targets.append(target)
        targets.extend(_mount_targets({"filesystems": filesystem.get("children") or []}))
    return targets


def validate_datastore_path(mountpoint: str, datastore_path: str, mount_targets: list[str]) -> None:
    """Refuse symlink, cross-filesystem, or nested-mount datastore paths."""
    normalized_mount = os.path.normpath(mountpoint)
    normalized_datastore = os.path.normpath(datastore_path)
    try:
        common = os.path.commonpath((normalized_mount, normalized_datastore))
    except ValueError as exc:
        raise Refusal("datastore path is outside the PBS data filesystem") from exc
    if common != normalized_mount or normalized_datastore == normalized_mount:
        raise Refusal("datastore path is outside the PBS data filesystem")
    if os.path.realpath(mountpoint) != normalized_mount:
        raise Refusal("PBS data mountpoint resolves through a symlink")
    try:
        mount_stat = os.stat(mountpoint, follow_symlinks=False)
    except OSError as exc:
        raise Refusal("PBS data mountpoint is unavailable") from exc
    if not stat.S_ISDIR(mount_stat.st_mode):
        raise Refusal("PBS data mountpoint is not a real directory")

    try:
        datastore_stat = os.lstat(datastore_path)
    except FileNotFoundError:
        datastore_stat = None
    except OSError as exc:
        raise Refusal("PBS datastore path cannot be inspected safely") from exc
    if datastore_stat is not None:
        if stat.S_ISLNK(datastore_stat.st_mode):
            raise Refusal("PBS datastore path is a symlink")
        if not stat.S_ISDIR(datastore_stat.st_mode):
            raise Refusal("PBS datastore path is not a directory")
        if datastore_stat.st_dev != mount_stat.st_dev:
            raise Refusal("PBS datastore path is not on the selected filesystem")
    expected_path = os.path.join(os.path.realpath(mountpoint), os.path.basename(normalized_datastore))
    if os.path.realpath(datastore_path) != expected_path:
        raise Refusal("datastore path resolves outside the PBS data filesystem")

    normalized_targets = {os.path.normpath(target) for target in mount_targets}
    if normalized_mount not in normalized_targets:
        raise Refusal("PBS data filesystem is absent from the mount tree")
    for target in normalized_targets - {normalized_mount}:
        try:
            target_common = os.path.commonpath((normalized_datastore, target))
        except ValueError:
            continue
        if target_common == normalized_datastore:
            raise Refusal("nested mount exists at or below the PBS datastore path")


def ensure_datastore_path(mountpoint: str, datastore_path: str, mount_targets: list[str]) -> bool:
    """Create the direct datastore child without following paths outside the mount."""
    validate_datastore_path(mountpoint, datastore_path, mount_targets)
    child_name = os.path.basename(os.path.normpath(datastore_path))
    if child_name in {"", ".", ".."}:
        raise Refusal("datastore path is not a direct child of the PBS data filesystem")
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    except AttributeError as exc:
        raise Refusal("guest platform lacks no-follow directory support") from exc
    created = False
    try:
        mount_fd = os.open(mountpoint, flags)
    except OSError as exc:
        raise Refusal("PBS data mountpoint cannot be opened without following links") from exc
    try:
        mount_stat = os.fstat(mount_fd)
        if not stat.S_ISDIR(mount_stat.st_mode):
            raise Refusal("PBS data mountpoint is not a real directory")
        try:
            os.mkdir(child_name, mode=0o750, dir_fd=mount_fd)
            created = True
        except FileExistsError:
            pass
        except OSError as exc:
            raise Refusal("PBS datastore directory could not be created safely") from exc
        try:
            datastore_fd = os.open(child_name, flags, dir_fd=mount_fd)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise Refusal("PBS datastore path is not a real directory") from exc
            raise Refusal("PBS datastore path cannot be opened safely") from exc
        try:
            datastore_stat = os.fstat(datastore_fd)
            if not stat.S_ISDIR(datastore_stat.st_mode):
                raise Refusal("PBS datastore path is not a real directory")
            if datastore_stat.st_dev != mount_stat.st_dev:
                raise Refusal("PBS datastore path is not on the selected filesystem")
        finally:
            os.close(datastore_fd)
    finally:
        os.close(mount_fd)
    return created


def _discover_block_devices() -> dict[str, Any]:
    try:
        result = subprocess.run(
            [
                "lsblk", "--json", "--bytes", "--output",
                "NAME,TYPE,SERIAL,SIZE,FSTYPE,LABEL,UUID,MOUNTPOINTS,PARTUUID,PKNAME",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise Refusal("guest block-device inventory is unavailable") from exc
    if result.returncode != 0:
        raise Refusal("guest block-device inventory is unavailable")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise Refusal("guest block-device inventory is malformed") from exc


def _discover_mount_tree(mountpoint: str) -> list[str]:
    try:
        result = subprocess.run(
            ["findmnt", "--json", "--submounts", "--target", mountpoint, "--output", "TARGET"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise Refusal("PBS data mount tree is unavailable") from exc
    if result.returncode != 0:
        raise Refusal("PBS data mount tree is unavailable")
    try:
        return _mount_targets(json.loads(result.stdout))
    except json.JSONDecodeError as exc:
        raise Refusal("PBS data mount tree is malformed") from exc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-serial")
    parser.add_argument("--expected-size", type=int)
    parser.add_argument("--mountpoint", required=True)
    parser.add_argument("--validate-datastore-path", action="store_true")
    parser.add_argument("--ensure-datastore-path", action="store_true")
    parser.add_argument("--datastore-path")
    args = parser.parse_args()
    try:
        if args.validate_datastore_path or args.ensure_datastore_path:
            if not args.datastore_path:
                raise Refusal("PBS datastore path is required")
            mount_targets = _discover_mount_tree(args.mountpoint)
            created = False
            if args.ensure_datastore_path:
                created = ensure_datastore_path(args.mountpoint, args.datastore_path, mount_targets)
            else:
                validate_datastore_path(args.mountpoint, args.datastore_path, mount_targets)
            print(json.dumps({"datastore_path": "validated", "created": created}))
            return 0
        if args.expected_size is None:
            raise Refusal("declared virtual disk size is required")
        if not args.expected_serial:
            raise Refusal("declared virtual disk serial is required")
        input_json = sys.stdin.read()
        document = json.loads(input_json) if input_json.strip() else _discover_block_devices()
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
