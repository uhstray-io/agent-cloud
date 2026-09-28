#!/usr/bin/env python3
"""Read-only, sanitized inventory of named o11y volume backing filesystems."""

import json
import os
import subprocess
import sys

VOLUMES = {
    "prometheus-data": ("o11y-prometheus", "/prometheus"),
    "loki-data": ("o11y-loki", "/loki"),
    "tempo-data": ("o11y-tempo", "/var/tempo"),
    "grafana-data": ("o11y-grafana", "/var/lib/grafana"),
    "pyroscope-data": ("o11y-pyroscope", "/data"),
}


def result(reason: str, **details: object) -> int:
    print(json.dumps({"status": "unavailable", "reason": reason, **details}, sort_keys=True))
    return 1


def run(engine: str, *args: str) -> subprocess.CompletedProcess[str] | None:
    try:
        proc = subprocess.run([engine, *args], check=False, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc if proc.returncode == 0 else None


def allocated_bytes(path: str) -> int | None:
    total = 0
    seen: set[tuple[int, int]] = set()
    errors: list[OSError] = []

    def walk_error(error: OSError) -> None:
        errors.append(error)

    try:
        for root, dirs, files in os.walk(path, topdown=True, followlinks=False, onerror=walk_error):
            for item in dirs + files:
                full = os.path.join(root, item)
                try:
                    info = os.lstat(full)
                except OSError as error:
                    errors.append(error)
                    continue
                key = (info.st_dev, info.st_ino)
                if key not in seen:
                    total += getattr(info, "st_blocks", 0) * 512
                    seen.add(key)
        if errors:
            return None
    except OSError:
        return None
    return total


def fs_capacity(path: str) -> tuple[int, int, int] | None:
    try:
        stats = os.statvfs(path)
    except OSError:
        return None
    unit = stats.f_frsize or stats.f_bsize
    return stats.f_blocks * unit, stats.f_bavail * unit, stats.f_fsid


def main() -> int:
    if len(sys.argv) not in (2, 3) or not sys.argv[1]:
        return result("container_engine_missing")
    engine = sys.argv[1]
    try:
        minimum_free_percent = float(sys.argv[2]) if len(sys.argv) == 3 else None
    except ValueError:
        return result("minimum_free_percent_invalid")
    if minimum_free_percent is not None and not 0 <= minimum_free_percent <= 100:
        return result("minimum_free_percent_invalid")

    containers = run(engine, "ps", "-a", "--format", "json")
    if containers is None:
        return result("container_list_unavailable")
    try:
        container_rows = json.loads(containers.stdout)
    except json.JSONDecodeError:
        return result("container_list_invalid")
    if not isinstance(container_rows, list):
        return result("container_list_invalid")
    container_names: set[str] = set()
    for row in container_rows:
        if not isinstance(row, dict):
            return result("container_list_invalid")
        names = row.get("Names", row.get("Name", []))
        if isinstance(names, str):
            names = [names]
        if isinstance(names, list):
            container_names.update(name.lstrip("/") for name in names if isinstance(name, str))
    expected_containers = {container for container, _ in VOLUMES.values()}
    present = expected_containers & container_names
    missing = expected_containers - container_names
    root = fs_capacity("/")
    if root is None:
        return result("guest_root_capacity_unavailable")
    root_total, root_available, root_fsid = root
    if not present:
        if minimum_free_percent is None:
            return result("all_backend_containers_missing")
        listed = run(engine, "volume", "ls", "--format", "json")
        if listed is None:
            return result("volume_list_unavailable")
        try:
            volumes = json.loads(listed.stdout)
        except json.JSONDecodeError:
            return result("volume_list_invalid")
        if not isinstance(volumes, list):
            return result("volume_list_invalid")
        for logical_name in VOLUMES:
            candidates = [
                volume for volume in volumes
                if isinstance(volume, dict)
                and (
                    (isinstance(volume.get("Labels"), dict)
                     and volume["Labels"].get("com.docker.compose.volume") == logical_name)
                    or (isinstance(volume.get("Name"), str)
                        and (volume["Name"] == logical_name
                             or volume["Name"].endswith("_" + logical_name)
                             or volume["Name"].endswith("-" + logical_name)))
                )
            ]
            if candidates:
                return result("orphaned_o11y_volume", volume=logical_name)
        root_free_percent = round(100 * root_available / root_total, 2) if root_total else 0
        if root_free_percent < minimum_free_percent:
            return result(
                "first_deploy_guest_root_free_below_threshold",
                guest_root_filesystem_total_bytes=root_total,
                guest_root_filesystem_available_bytes=root_available,
                guest_root_filesystem_free_percent=root_free_percent,
                required_free_percent=minimum_free_percent,
            )
        print(json.dumps({
            "status": "first_deploy",
            "guest_root_filesystem_total_bytes": root_total,
            "guest_root_filesystem_available_bytes": root_available,
            "guest_root_filesystem_free_percent": root_free_percent,
            "required_free_percent": minimum_free_percent,
            "volumes": [],
        }, sort_keys=True))
        return 0
    if missing:
        return result("backend_container_set_partial", missing_container=sorted(missing)[0])

    resolved: dict[str, dict[str, str]] = {}
    volume_names: set[str] = set()
    for logical_name, (container_name, destination) in VOLUMES.items():
        mount_readback = run(engine, "inspect", "--format", "{{json .Mounts}}", container_name)
        if mount_readback is None:
            return result("container_mount_inspect_unavailable", volume=logical_name)
        try:
            mounts = json.loads(mount_readback.stdout)
        except json.JSONDecodeError:
            return result("container_mount_inspect_invalid", volume=logical_name)
        if not isinstance(mounts, list):
            return result("container_mount_inspect_invalid", volume=logical_name)
        matches = [mount for mount in mounts if isinstance(mount, dict) and mount.get("Destination") == destination]
        if len(matches) != 1:
            reason = "container_volume_mount_missing" if not matches else "container_volume_mount_ambiguous"
            return result(reason, volume=logical_name)
        mount = matches[0]
        volume_name = mount.get("Name")
        source = mount.get("Source")
        if mount.get("Type") != "volume" or not isinstance(volume_name, str) or not isinstance(source, str):
            return result("container_mount_not_named_volume", volume=logical_name)
        if volume_name in volume_names:
            return result("volume_name_reused", volume=logical_name)
        volume_names.add(volume_name)
        inspected = run(engine, "volume", "inspect", volume_name)
        if inspected is None:
            return result("volume_inspect_unavailable", volume=logical_name)
        try:
            objects = json.loads(inspected.stdout)
            item = objects[0]
            mountpoint = item.get("Mountpoint")
        except (json.JSONDecodeError, TypeError, IndexError, KeyError):
            return result("volume_inspect_invalid", volume=logical_name)
        if not isinstance(mountpoint, str) or not os.path.isabs(mountpoint) or not os.path.isdir(mountpoint):
            return result("volume_mountpoint_unresolved", volume=logical_name)
        if os.path.realpath(source) != os.path.realpath(mountpoint):
            return result("container_and_volume_mountpoint_mismatch", volume=logical_name)
        resolved[logical_name] = {"mountpoint": mountpoint}

    reports: list[dict[str, int | str | bool]] = []
    for logical_name, details in resolved.items():
        mountpoint = details["mountpoint"]
        capacity = fs_capacity(mountpoint)
        size = allocated_bytes(mountpoint)
        if capacity is None:
            return result("volume_filesystem_unresolved", volume=logical_name)
        if size is None:
            return result("volume_size_unreadable", volume=logical_name)
        total, available, fsid = capacity
        reports.append({
            "volume": logical_name,
            "mountpoint_verified": True,
            "shares_guest_root_filesystem": fsid == root_fsid,
            "backing_filesystem_total_bytes": total,
            "backing_filesystem_available_bytes": available,
            "backing_filesystem_free_percent": round(100 * available / total, 2) if total else 0,
            "stored_bytes": size,
        })

    if minimum_free_percent is not None:
        blocked = next(
            (volume for volume in reports if volume["backing_filesystem_free_percent"] < minimum_free_percent),
            None,
        )
        if blocked is not None:
            return result(
                "backing_filesystem_free_below_threshold",
                volume=blocked["volume"],
                backing_filesystem_free_percent=blocked["backing_filesystem_free_percent"],
                required_free_percent=minimum_free_percent,
                guest_root_filesystem_total_bytes=root_total,
                guest_root_filesystem_available_bytes=root_available,
                volumes=reports,
            )

    print(json.dumps({
        "status": "observed",
        "guest_root_filesystem_total_bytes": root_total,
        "guest_root_filesystem_available_bytes": root_available,
        "volumes": reports,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
