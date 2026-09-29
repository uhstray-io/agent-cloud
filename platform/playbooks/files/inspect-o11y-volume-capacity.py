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


def allocated_bytes(engine: str, path: str) -> int | None:
    """Read bytes from rootless Podman storage inside its user namespace."""
    proc = run(engine, "unshare", "du", "-s", "-B1", "--", path)
    if proc is None:
        return None
    rows = proc.stdout.splitlines()
    if len(rows) != 1:
        return None
    try:
        value = int(rows[0].split(maxsplit=1)[0])
    except (ValueError, IndexError):
        return None
    return value if value >= 0 else None


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
    if engine != "podman":
        return result("unsupported_container_engine")
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
            declared_names = {f"o11y_{logical_name}", f"o11y-{logical_name}"}
            if any(
                isinstance(volume, dict)
                and (
                    (
                        isinstance(volume.get("Labels"), dict)
                        and volume["Labels"].get("com.docker.compose.project") == "o11y"
                        and volume["Labels"].get("com.docker.compose.volume") == logical_name
                    )
                    or volume.get("Name") in declared_names
                )
                for volume in volumes
            ):
                return result("orphaned_o11y_volume", volume=logical_name)
        podman_info = run(engine, "info", "--format", "json")
        if podman_info is None:
            return result("podman_info_unavailable")
        try:
            info = json.loads(podman_info.stdout)
        except json.JSONDecodeError:
            return result("podman_info_invalid")
        store = info.get("store") if isinstance(info, dict) else None
        volume_path = store.get("volumePath") if isinstance(store, dict) else None
        if not isinstance(volume_path, str) or not os.path.isabs(volume_path) or not os.path.isdir(volume_path):
            return result("podman_volume_path_unresolved")
        volume_fs = fs_capacity(volume_path)
        if volume_fs is None:
            return result("podman_volume_filesystem_unavailable")
        volume_total, volume_available, _volume_fsid = volume_fs
        volume_free_percent = round(100 * volume_available / volume_total, 2) if volume_total else 0
        if volume_free_percent < minimum_free_percent:
            return result(
                "first_deploy_volume_filesystem_free_below_threshold",
                guest_root_filesystem_total_bytes=root_total,
                guest_root_filesystem_available_bytes=root_available,
                volume_store_filesystem_total_bytes=volume_total,
                volume_store_filesystem_available_bytes=volume_available,
                volume_store_filesystem_free_percent=volume_free_percent,
                required_free_percent=minimum_free_percent,
            )
        print(
            json.dumps(
                {
                    "status": "first_deploy",
                    "guest_root_filesystem_total_bytes": root_total,
                    "guest_root_filesystem_available_bytes": root_available,
                    "volume_store_filesystem_total_bytes": volume_total,
                    "volume_store_filesystem_available_bytes": volume_available,
                    "volume_store_filesystem_free_percent": volume_free_percent,
                    "required_free_percent": minimum_free_percent,
                    "volumes": [],
                },
                sort_keys=True,
            )
        )
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
        if capacity is None:
            return result("volume_filesystem_unresolved", volume=logical_name)
        total, available, fsid = capacity
        report: dict[str, int | str | bool] = {
            "volume": logical_name,
            "mountpoint_verified": True,
            "shares_guest_root_filesystem": fsid == root_fsid,
            "backing_filesystem_total_bytes": total,
            "backing_filesystem_available_bytes": available,
            "backing_filesystem_free_percent": round(100 * available / total, 2) if total else 0,
        }
        if minimum_free_percent is None:
            size = allocated_bytes(engine, mountpoint)
            if size is None:
                return result("volume_size_unreadable", volume=logical_name)
            report["stored_bytes"] = size
        reports.append(report)

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

    print(
        json.dumps(
            {
                "status": "observed",
                "guest_root_filesystem_total_bytes": root_total,
                "guest_root_filesystem_available_bytes": root_available,
                "volumes": reports,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
