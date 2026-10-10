#!/usr/bin/env python3
"""Prove direct rootless Podman no-copy and Alloy journal cursor persistence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

IMAGE = "docker.io/grafana/alloy:v1.9.2"
STATE_PATH = "/alloy-state"
JOURNAL_PATH = "/synthetic-journal"
CONFIG_PATH = "/etc/alloy/proof.alloy"
MAX_INVENTORY = 4096
MAX_OUTPUT = 2 * 1024 * 1024
MAX_TOTAL_OUTPUT = 64 * 1024 * 1024
MAX_SCAN_ENTRIES = 256
MAX_SCAN_BYTES = 16 * 1024 * 1024
MAX_FILE_BYTES = 1024 * 1024
MAX_CURSOR_BYTES = 65536
MAX_LOG_OUTPUT = 256 * 1024
LOG_TAIL_LINES = 200
CURSOR_WAIT_SECONDS = 20
CURSOR_POLL_SECONDS = 0.5
NAMESPACE_SCAN_REFUSALS = {
    "copy_control_unverified", "layout_changed", "layout_unavailable", "layout_unbounded",
    "namespace_scan_unverified", "state_path_unsafe",
}
PROOF_OWNER = "agent-cloud.proof-owner"
PROOF_ROLE = "agent-cloud.proof-role"
PROJECT_LABELS = ("com.docker.compose.project", "io.podman.compose.project")
RECEIVERS = (
    "o11y-prometheus", "o11y-loki", "o11y-alloy", "o11y-tempo",
    "o11y-pyroscope", "o11y-grafana", "o11y-node-exporter",
)
CURSOR_RE = re.compile(r"s=[0-9a-f]+;i=[0-9a-f]+;b=[0-9a-f]+;m=[0-9a-f]+;t=[0-9a-f]+;x=[0-9a-f]+\Z")
OWNED_CONTAINERS: dict[str, dict] = {}
OWNED_VOLUMES: dict[str, dict] = {}
TOTAL_OUTPUT_BYTES = 0


class Refused(Exception):
    def __init__(self, category: str):
        self.category = category


def digest(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def run(argv: list[str], timeout: int = 60, *, input_data: bytes | None = None,
        output_limit: int = MAX_OUTPUT) -> bytes:
    """Run one command with bounded captured output and no diagnostic forwarding."""
    if input_data is not None:
        if len(input_data) > MAX_SCAN_BYTES:
            raise Refused("fixture_unbounded")
        try:
            result = subprocess.run(
                argv, input=input_data, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=timeout, check=False, env={**os.environ, "LC_ALL": "C"},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise Refused("provider_operation_failed") from exc
        if result.returncode:
            raise Refused("provider_operation_failed")
        return b""
    try:
        process = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env={**os.environ, "LC_ALL": "C"},
        )
    except OSError as exc:
        raise Refused("provider_unavailable") from exc
    assert process.stdout is not None
    output = bytearray()
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise Refused("provider_timeout")
                for key, _ in selector.select(min(1.0, remaining)):
                    chunk = os.read(key.fd, min(65536, output_limit + 1 - len(output)))
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    output.extend(chunk)
                    if len(output) > output_limit:
                        raise Refused("provider_output_unbounded")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Refused("provider_timeout")
        if process.wait(timeout=remaining) != 0:
            raise Refused("provider_operation_failed")
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Refused("provider_operation_failed") from exc
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    global TOTAL_OUTPUT_BYTES
    TOTAL_OUTPUT_BYTES += len(output)
    if TOTAL_OUTPUT_BYTES > MAX_TOTAL_OUTPUT:
        raise Refused("proof_output_unbounded")
    return bytes(output)


def run_streams(argv: list[str], timeout: int, output_limit: int) -> bytes:
    """Capture both protected provider streams under one byte and time limit."""
    try:
        process = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={**os.environ, "LC_ALL": "C"},
        )
    except OSError as exc:
        raise Refused("provider_unavailable") from exc
    assert process.stdout is not None and process.stderr is not None
    output = bytearray()
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            selector.register(process.stderr, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise Refused("provider_timeout")
                for key, _ in selector.select(min(1.0, remaining)):
                    chunk = os.read(key.fd, min(65536, output_limit + 1 - len(output)))
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    output.extend(chunk)
                    if len(output) > output_limit:
                        raise Refused("provider_output_unbounded")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Refused("provider_timeout")
        if process.wait(timeout=remaining) != 0:
            raise Refused("provider_operation_failed")
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Refused("provider_operation_failed") from exc
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()
        process.stderr.close()
    global TOTAL_OUTPUT_BYTES
    TOTAL_OUTPUT_BYTES += len(output)
    if TOTAL_OUTPUT_BYTES > MAX_TOTAL_OUTPUT:
        raise Refused("proof_output_unbounded")
    return bytes(output)


def data(argv: list[str], category: str = "provider_response_invalid"):
    try:
        return json.loads(run(argv).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Refused(category) from exc


def one(value, category: str) -> dict:
    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict):
        return value[0]
    if isinstance(value, dict):
        return value
    raise Refused(category)


def rows(value, category: str, maximum: int = MAX_INVENTORY) -> list[dict]:
    if (not isinstance(value, list) or len(value) > maximum
            or any(not isinstance(row, dict) for row in value)):
        raise Refused(category)
    return value


def labels(row: dict) -> dict[str, str]:
    config = row.get("Config") if isinstance(row.get("Config"), dict) else {}
    value = row.get("Labels", config.get("Labels"))
    if value is None:
        return {}
    if isinstance(value, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        return value
    raise Refused("provider_response_invalid")


def row_name(row: dict):
    value = row.get("Name", row.get("Names"))
    if isinstance(value, list) and len(value) == 1:
        value = value[0]
    return value if isinstance(value, str) and value else None


def podman_info() -> tuple[str, str, dict]:
    binary = shutil.which("podman")
    if not binary or not os.path.isfile(binary) or not os.access(binary, os.X_OK):
        raise Refused("provider_unavailable")
    info = data([binary, "info", "--format", "json"])
    host = info.get("host") or info.get("Host") or {}
    store = info.get("store") or info.get("Store") or {}
    security = host.get("security") or host.get("Security") or {}
    rootless = security.get("rootless")
    if rootless is None:
        rootless = security.get("Rootless")
    if os.geteuid() == 0 or rootless is not True:
        raise Refused("rootless_context_required")
    context = {
        "rootless": rootless,
        "graph_root": store.get("graphRoot", store.get("GraphRoot")),
        "run_root": store.get("runRoot", store.get("RunRoot")),
        "driver": store.get("graphDriverName", store.get("GraphDriverName")),
        "uid_map": host.get("idMappings", host.get("IDMappings", {})),
    }
    if not all(isinstance(context[key], (str, dict)) and context[key] for key in ("graph_root", "run_root", "driver")):
        raise Refused("storage_context_unverified")
    binary_digest = hash_file(Path(binary), 256 * 1024 * 1024)
    client = version([binary, "version", "--format", "{{.Client.Version}}"])
    server = version([binary, "version", "--format", "{{.Server.Version}}"])
    return binary, binary_digest, {"context": context, "client": client, "server": server}


def version(argv: list[str]) -> str:
    try:
        value = run(argv, timeout=20, output_limit=4096).decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise Refused("provider_version_unverified") from exc
    match = re.fullmatch(r"(?:podman version )?([0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9_.-]+)?)", value, re.I)
    if not match or len(value) > 80:
        raise Refused("provider_version_unverified")
    return match.group(1)


def hash_file(path: Path, maximum: int) -> str:
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
            raise Refused("identity_unverified")
        value = hashlib.sha256()
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino, opened.st_size) != (before.st_dev, before.st_ino, before.st_size):
                raise Refused("identity_changed")
            total = 0
            with os.fdopen(fd, "rb", closefd=False) as stream:
                while chunk := stream.read(65536):
                    total += len(chunk)
                    if total > maximum:
                        raise Refused("identity_unverified")
                    value.update(chunk)
            after = os.fstat(fd)
            current = path.lstat()
            if ((after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                    != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
                    or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)):
                raise Refused("identity_changed")
        finally:
            os.close(fd)
        return value.hexdigest()
    except OSError as exc:
        raise Refused("identity_unverified") from exc


def inventories(binary: str) -> tuple[list[dict], list[dict]]:
    containers = rows(data([binary, "ps", "-a", "--format", "json"]), "container_inventory_unverified")
    volumes = rows(data([binary, "volume", "ls", "--format", "json"]), "volume_inventory_unverified")
    for row in containers:
        if not isinstance(row.get("Id", row.get("ID")), str):
            raise Refused("container_inventory_unverified")
    for row in volumes:
        if not row_name(row):
            raise Refused("volume_inventory_unverified")
    return containers, volumes


def inspect_container(binary: str, container_id: str) -> dict:
    value = data([binary, "inspect", container_id], "container_identity_unverified")
    return one(value, "container_identity_unverified")


def inspect_volume(binary: str, name: str, category: str = "volume_identity_unverified") -> dict:
    return one(data([binary, "volume", "inspect", name], category), category)


def volume_identity(row: dict) -> dict:
    fields = ("Name", "CreatedAt", "Driver", "Scope", "Options", "Mountpoint", "Labels")
    identity = {field: row.get(field) for field in fields}
    if (not isinstance(identity["Name"], str) or not identity["Name"]
            or not isinstance(identity["CreatedAt"], str) or not identity["CreatedAt"]
            or not isinstance(identity["Driver"], str) or not identity["Driver"]
            or not isinstance(identity["Mountpoint"], str) or not Path(identity["Mountpoint"]).is_absolute()):
        raise Refused("volume_identity_unverified")
    return identity


def receiver_context(binary: str) -> tuple[dict, str]:
    inspections = []
    project_values = set()
    common_networks: set[tuple[str, str]] | None = None
    for name in RECEIVERS:
        inspected = data([binary, "inspect", name], "receiver_baseline_unverified")
        rows_value = rows(inspected, "receiver_baseline_unverified", 1)
        container = one(rows_value, "receiver_baseline_unverified")
        state = container.get("State") or {}
        if state.get("Running") is not True or not isinstance(container.get("Id"), str):
            raise Refused("receiver_baseline_unverified")
        found_labels = labels(container)
        current_projects = {found_labels[key] for key in PROJECT_LABELS if found_labels.get(key)}
        if len(current_projects) != 1:
            raise Refused("project_identity_unverified")
        project_values.update(current_projects)
        networks = container.get("NetworkSettings", {}).get("Networks", {})
        if not isinstance(networks, dict) or not networks:
            raise Refused("network_identity_unverified")
        identities = set()
        for net_name, net in networks.items():
            if isinstance(net, dict) and isinstance(net.get("NetworkID"), str) and net.get("NetworkID"):
                identities.add((net_name, net["NetworkID"]))
        if not identities:
            raise Refused("network_identity_unverified")
        common_networks = identities if common_networks is None else common_networks & identities
        inspections.append({"id": container["Id"], "state": state.get("Status"),
                            "health": (state.get("Health") or {}).get("Status"), "labels": found_labels})
    if len(project_values) != 1 or common_networks is None or len(common_networks) != 1:
        raise Refused("network_identity_unverified")
    network_name, network_id = next(iter(common_networks))
    inspected_network = data([binary, "network", "inspect", network_name], "network_identity_unverified")
    network = one(inspected_network, "network_identity_unverified")
    if network.get("Id", network.get("ID")) != network_id:
        raise Refused("network_identity_unverified")
    network_labels = labels(network)
    network_projects = {network_labels[key] for key in PROJECT_LABELS if network_labels.get(key)}
    if network_projects != project_values:
        raise Refused("project_identity_unverified")
    identity = {"project": next(iter(project_values)), "network_name": network_name,
                "network_id": network_id, "receivers": inspections, "network_labels": network_labels}
    return identity, digest(identity)


def baseline(binary: str, owner: str) -> str:
    containers, volumes = inventories(binary)
    receiver, _ = receiver_context(binary)
    container_rows = []
    for row in containers:
        name = row_name(row)
        cid = row.get("Id", row.get("ID"))
        inspect = inspect_container(binary, cid)
        state = inspect.get("State") or {}
        found_labels = labels(inspect)
        if found_labels.get(PROOF_OWNER) == owner:
            continue
        container_rows.append({"name": name, "id": cid, "state": state.get("Status"),
                               "started": state.get("StartedAt"), "image": inspect.get("Image"),
                               "labels": found_labels})
    volume_rows = []
    for row in volumes:
        name = row_name(row)
        inspected = inspect_volume(binary, name)
        if labels(inspected).get(PROOF_OWNER) == owner:
            continue
        volume_rows.append({key: inspected.get(key) for key in
                            ("Name", "CreatedAt", "Driver", "Scope", "Options", "MountCount", "Labels")})
    return digest({"receivers": receiver, "containers": container_rows, "volumes": volume_rows})


def scandir_manifest(root: Path, *, target_state: bool = False) -> dict[str, tuple]:
    """Scan one tree without following links or sorting; enforce count and byte budgets."""
    root_fd = None
    pending: list[tuple[int, str, tuple[int, int]]] = []
    try:
        root_stat = root.lstat()
        if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
            raise Refused("state_path_unsafe" if target_state else "copy_control_unverified")
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        root_opened = os.fstat(root_fd)
        if (root_opened.st_dev, root_opened.st_ino) != (root_stat.st_dev, root_stat.st_ino):
            raise Refused("layout_changed")
        if target_state and root_stat.st_mode & 0o7000:
            raise Refused("state_path_unsafe")
        pending.append((root_fd, "", (root_stat.st_dev, root_stat.st_ino)))
        root_fd = None
        manifest: dict[str, tuple] = {}
        total_bytes = 0
        while pending:
            directory_fd, prefix, directory_identity = pending.pop()
            before_dir = os.fstat(directory_fd)
            if (before_dir.st_dev, before_dir.st_ino) != directory_identity:
                os.close(directory_fd)
                raise Refused("layout_changed")
            child_fds = []
            try:
                with os.scandir(directory_fd) as entries:
                    for entry in entries:
                        relative = f"{prefix}/{entry.name}" if prefix else entry.name
                        if len(manifest) >= MAX_SCAN_ENTRIES or len(relative.encode()) > 4096:
                            raise Refused("layout_unbounded")
                        try:
                            info = os.stat(entry.name, dir_fd=directory_fd, follow_symlinks=False)
                        except OSError as exc:
                            raise Refused("layout_unavailable") from exc
                        if target_state and info.st_mode & 0o7000:
                            raise Refused("state_path_unsafe")
                        if stat.S_ISDIR(info.st_mode):
                            child_fd = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                               dir_fd=directory_fd)
                            child_info = os.fstat(child_fd)
                            if (child_info.st_dev, child_info.st_ino) != (info.st_dev, info.st_ino):
                                os.close(child_fd)
                                raise Refused("layout_changed")
                            manifest[relative] = ("dir", stat.S_IMODE(info.st_mode), info.st_uid,
                                                  info.st_gid, info.st_nlink)
                            child_fds.append((child_fd, relative, (info.st_dev, info.st_ino)))
                        elif stat.S_ISLNK(info.st_mode):
                            if target_state:
                                raise Refused("state_path_unsafe")
                            link = os.readlink(entry.name, dir_fd=directory_fd)
                            if len(os.fsencode(link)) > 4096:
                                raise Refused("layout_unbounded")
                            manifest[relative] = ("symlink", link, info.st_uid, info.st_gid,
                                                  stat.S_IMODE(info.st_mode))
                        elif stat.S_ISREG(info.st_mode):
                            if info.st_size > MAX_FILE_BYTES or total_bytes + info.st_size > MAX_SCAN_BYTES:
                                raise Refused("layout_unbounded")
                            total_bytes += info.st_size
                            fd = os.open(entry.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory_fd)
                            try:
                                before = os.fstat(fd)
                                if ((before.st_dev, before.st_ino, before.st_size)
                                        != (info.st_dev, info.st_ino, info.st_size)):
                                    raise Refused("layout_changed")
                                value = hashlib.sha256()
                                size = 0
                                while chunk := os.read(fd, 65536):
                                    size += len(chunk)
                                    if size > MAX_FILE_BYTES:
                                        raise Refused("layout_unbounded")
                                    value.update(chunk)
                                after = os.fstat(fd)
                                current = os.stat(entry.name, dir_fd=directory_fd, follow_symlinks=False)
                                if ((after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                                        != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                                        or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)):
                                    raise Refused("layout_changed")
                            finally:
                                os.close(fd)
                            manifest[relative] = ("file", info.st_size, value.hexdigest(), info.st_uid,
                                                  info.st_gid, stat.S_IMODE(info.st_mode))
                        else:
                            raise Refused("state_path_unsafe" if target_state else "copy_control_unverified")
                after_dir = os.fstat(directory_fd)
                if (after_dir.st_dev, after_dir.st_ino, after_dir.st_mtime_ns) != (
                        before_dir.st_dev, before_dir.st_ino, before_dir.st_mtime_ns):
                    raise Refused("layout_changed")
                pending.extend(child_fds)
                child_fds = []
            finally:
                for child_fd, _, _ in child_fds:
                    os.close(child_fd)
                os.close(directory_fd)
        return manifest
    except Refused:
        raise
    except OSError as exc:
        raise Refused("layout_unavailable") from exc
    finally:
        if root_fd is not None:
            os.close(root_fd)
        for descriptor, _, _ in pending:
            try:
                os.close(descriptor)
            except OSError:
                pass


def image_manifest(binary: str, root: Path) -> tuple[dict, str, dict]:
    state_path = root / "alloy-state"
    try:
        state = namespace_scan(binary, state_path, target_state=True)
    except Refused as exc:
        if exc.category != "layout_absent":
            raise
        state = {}
    for candidate in ("usr/share/zoneinfo", "etc/ssl/certs", "etc"):
        path = root / candidate
        try:
            manifest = namespace_scan(binary, path)
        except Refused as exc:
            if exc.category in ("layout_absent", "layout_unbounded", "copy_control_unverified"):
                continue
            raise
        if any(item[0] == "file" for item in manifest.values()):
            return state, candidate, manifest
    raise Refused("copy_control_unverified")


def copied_manifest(expected: dict[str, tuple], actual: dict[str, tuple]) -> bool:
    """Require every regular file; directory and symlink metadata is not copy evidence."""
    files = {name: item for name, item in expected.items() if item[0] == "file"}
    if not files or any(actual.get(name, ())[:3] != item[:3] for name, item in files.items()):
        return False
    expected_names = set(expected)
    return all(item[0] != "file" or name in expected_names for name, item in actual.items())


def container_identity_matches(inspect: dict, expected: dict, owner: str) -> bool:
    found = labels(inspect)
    return (inspect.get("Id") == expected.get("Id") and inspect.get("Image") == expected.get("Image")
            and inspect.get("Created") == expected.get("Created")
            and found.get(PROOF_OWNER) == owner and found.get(PROOF_ROLE) == expected.get("role"))


def volume_identity_matches(inspect: dict, expected: dict, owner: str, role: str) -> bool:
    try:
        identity = volume_identity(inspect)
    except Refused:
        return False
    found = labels(inspect)
    return (identity == expected.get("identity") and found.get(PROOF_OWNER) == owner
            and found.get(PROOF_ROLE) == role and inspect.get("MountCount", 0) in (0, None))


def volume_mount_arg(name: str, destination: str, *, no_copy: bool) -> str:
    if not re.fullmatch(r"jproof-[0-9a-f]{32}", name) or not destination.startswith("/"):
        raise Refused("volume_identity_unverified")
    return f"{name}:{destination}:rw" + (",nocopy" if no_copy else "")


def create_container(binary: str, name: str, owner: str, role: str, image_id: str,
                     mounts: list[str], command: list[str], *, entrypoint: str = "/bin/alloy") -> dict:
    argv = [binary, "create", "--pull=never", "--name", name, "--network", "none", "--read-only",
            "--user", "0:0", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
            "--restart", "no", "--label", f"{PROOF_OWNER}={owner}", "--label", f"{PROOF_ROLE}={role}"]
    for mount in mounts:
        argv.extend(["--volume", mount])
    argv.extend(["--entrypoint", entrypoint, image_id, *command])
    result = run(argv).decode("ascii", errors="strict").strip()
    if not re.fullmatch(r"[0-9a-f]{12,64}", result):
        raise Refused("container_identity_unverified")
    inspect = inspect_container(binary, result)
    config = inspect.get("Config") or {}
    host = inspect.get("HostConfig") or {}
    actual_labels = labels(inspect)
    if (inspect.get("Id") != result or inspect.get("Image") != image_id
            or actual_labels.get(PROOF_OWNER) != owner or actual_labels.get(PROOF_ROLE) != role
            or host.get("NetworkMode") != "none" or host.get("ReadonlyRootfs") is not True
            or "ALL" not in host.get("CapDrop", []) or config.get("User") != "0:0"
            or host.get("PortBindings") or host.get("RestartPolicy", {}).get("Name") != "no"
            or not any("no-new-privileges" in str(value) for value in host.get("SecurityOpt", []))):
        raise Refused("container_isolation_unverified")
    created = inspect.get("Created")
    if not isinstance(created, str) or not created:
        raise Refused("container_identity_unverified")
    OWNED_CONTAINERS[result] = {"Id": result, "Image": image_id, "Created": created,
                                "owner": owner, "role": role}
    return inspect


def remove_owned_container(binary: str, container_id: str, owner: str) -> None:
    expected = OWNED_CONTAINERS.get(container_id)
    inspect = inspect_container(binary, container_id)
    if expected is None or not container_identity_matches(inspect, expected, owner):
        raise Refused("cleanup_identity_uncertain")
    if (inspect.get("State") or {}).get("Running") is True:
        run([binary, "stop", "--time", "20", container_id], timeout=30)
    for mount in inspect.get("Mounts") or []:
        if mount.get("Type") == "volume" and mount.get("Name") in OWNED_VOLUMES:
            verify_no_foreign_volume_users(binary, mount["Name"], container_id)
    run([binary, "rm", container_id])
    OWNED_CONTAINERS.pop(container_id)


def mount_check(inspect: dict, expected: list[tuple[str, str, bool]]) -> None:
    mounts = inspect.get("Mounts")
    if not isinstance(mounts, list) or len(mounts) != len(expected):
        raise Refused("effective_mount_unverified")
    for destination, kind, writable, name in expected:
        matches = [item for item in mounts if item.get("Destination") == destination]
        if len(matches) != 1:
            raise Refused("effective_mount_unverified")
        item = matches[0]
        if item.get("Type") != kind or item.get("RW") is not writable or (name and item.get("Name") != name):
            raise Refused("effective_mount_unverified")
        if kind == "bind" and (item.get("Source") or "").startswith(("/var/run", "/run/podman", "/var/log/journal")):
            raise Refused("container_isolation_unverified")


def volume_create(binary: str, owner: str, role: str) -> tuple[str, dict]:
    name = f"jproof-{uuid.uuid4().hex}"
    run([binary, "volume", "create", "--label", f"{PROOF_OWNER}={owner}", "--label", f"{PROOF_ROLE}={role}", name])
    inspect = inspect_volume(binary, name)
    identity = volume_identity(inspect)
    found_labels = labels(inspect)
    if (identity["Name"] != name or found_labels.get(PROOF_OWNER) != owner
            or found_labels.get(PROOF_ROLE) != role or inspect.get("MountCount", 0) not in (0, None)):
        raise Refused("volume_identity_changed")
    OWNED_VOLUMES[name] = {"identity": identity, "owner": owner, "role": role, "root": None}
    return name, inspect


def volume_root(binary: str, name: str) -> Path:
    inspect = inspect_volume(binary, name)
    expected = volume_identity(inspect)["Mountpoint"]
    result = run([binary, "volume", "mount", name], timeout=30, output_limit=8192).decode("utf-8").strip()
    path = Path(result)
    try:
        info = path.lstat()
        expected_info = Path(expected).lstat()
    except OSError as exc:
        raise Refused("volume_mount_unverified") from exc
    if (not path.is_absolute() or stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
            or str(path) != expected or (info.st_dev, info.st_ino) != (expected_info.st_dev, expected_info.st_ino)):
        raise Refused("volume_mount_unverified")
    receipt = OWNED_VOLUMES.get(name)
    if receipt is None:
        raise Refused("volume_identity_unverified")
    receipt["root"] = (info.st_dev, info.st_ino)
    return path


def volume_unmount(binary: str, name: str) -> None:
    run([binary, "volume", "unmount", name], timeout=30)
    inspect = inspect_volume(binary, name)
    if inspect.get("MountCount", 0) not in (0, None):
        raise Refused("volume_mount_uncertain")


def volume_scan(binary: str, name: str, *, target_state: bool = False) -> dict:
    root = volume_root(binary, name)
    try:
        return namespace_scan(binary, root, target_state=target_state)
    finally:
        volume_unmount(binary, name)


def namespace_scan(binary: str, root: Path, *, target_state: bool = False) -> dict:
    output = run([
        binary, "unshare", sys.executable, str(Path(__file__).resolve()),
        "--scan-volume", str(root), "state" if target_state else "general",
    ], timeout=30, output_limit=MAX_OUTPUT)
    try:
        response = json.loads(output)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Refused("layout_unverified") from exc
    if not isinstance(response, dict):
        raise Refused("layout_unverified")
    if response.get("status") == "refused":
        reason = response.get("reason")
        if not isinstance(reason, str) or reason not in NAMESPACE_SCAN_REFUSALS:
            raise Refused("layout_unverified")
        raise Refused(reason)
    if response.get("status") == "absent":
        raise Refused("layout_absent")
    manifest = response.get("manifest")
    if (response.get("status") != "scanned" or not isinstance(manifest, dict)
            or any(not isinstance(key, str) or not isinstance(value, list)
                   or any(type(item) not in (str, int) for item in value)
                   for key, value in manifest.items())):
        raise Refused("layout_unverified")
    return {key: tuple(value) for key, value in manifest.items()}


def prove_mount_behavior(binary: str, owner: str, image_id: str, path: str, expected: dict,
                         *, no_copy: bool) -> str:
    role = "nocopy" if no_copy else "copy"
    name, _ = volume_create(binary, owner, role)
    before = volume_scan(binary, name, target_state=path == STATE_PATH)
    if before:
        raise Refused("volume_not_fresh")
    mount_arg = volume_mount_arg(name, path, no_copy=no_copy)
    container_name = f"{owner}-{role}-{uuid.uuid4().hex[:8]}"
    inspect = create_container(binary, container_name, owner, role, image_id, [mount_arg], ["--version"])
    mount_check(inspect, [(path, "volume", True, name)])
    run([binary, "start", inspect["Id"]])
    code = run([binary, "wait", inspect["Id"]], timeout=40, output_limit=64).decode().strip()
    if code != "0":
        raise Refused("image_command_unavailable")
    remove_owned_container(binary, inspect["Id"], owner)
    after = volume_scan(binary, name, target_state=path == STATE_PATH)
    if no_copy:
        if after:
            raise Refused("no_copy_behavior_failed")
        return "no_copy"
    if not copied_manifest(expected, after):
        raise Refused("copy_behavior_unverified")
    return "copied"


def fixture_entry(marker: str, monotonic_us: int) -> bytes:
    boot = "11111111111111111111111111111111"
    machine = "22222222222222222222222222222222"
    fields = (
        f"__REALTIME_TIMESTAMP={int(time.time() * 1_000_000)}",
        f"__MONOTONIC_TIMESTAMP={monotonic_us}",
        f"_BOOT_ID={boot}", f"_MACHINE_ID={machine}", "_HOSTNAME=journal-proof",
        "_SYSTEMD_UNIT=journal-proof.service", "PRIORITY=6", f"MESSAGE={marker}",
    )
    return ("\n".join(fields) + "\n\n").encode("ascii")


def append_fixture(scratch: Path, index: int) -> tuple[Path, str]:
    remote = shutil.which("systemd-journal-remote")
    if not remote or not os.path.isfile(remote) or not os.access(remote, os.X_OK):
        raise Refused("synthetic_journal_unavailable")
    journal_dir = scratch / "journal"
    journal_dir.mkdir(mode=0o700, exist_ok=True)
    output = journal_dir / "synthetic.journal"
    marker = f"journal-proof-{uuid.uuid4().hex}"
    data = fixture_entry(marker, index)
    try:
        result = subprocess.run(
            [remote, f"--output={output}"], input=data, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=20, check=False,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Refused("synthetic_journal_unavailable") from exc
    try:
        info = output.lstat()
    except OSError as exc:
        raise Refused("synthetic_journal_unavailable") from exc
    if result.returncode or not stat.S_ISREG(info.st_mode) or info.st_size <= 0 or info.st_size > MAX_FILE_BYTES:
        raise Refused("synthetic_journal_unavailable")
    return journal_dir, marker


def alloy_config() -> bytes:
    return (
        'loki.echo "synthetic" {}\n'
        'loki.source.journal "o11y_alloy" {\n'
        '  path = "/synthetic-journal"\n'
        '  matches = "_HOSTNAME=journal-proof"\n'
        '  forward_to = [loki.echo.synthetic.receiver]\n'
        '}\n'
    ).encode("ascii")


def cursor_from_manifest(root: Path) -> str:
    layout = scandir_manifest(root, target_state=True)
    component_name = "loki.source.journal.o11y_alloy"
    component = root / component_name
    if layout.keys() - {"alloy_seed.json", component_name, f"{component_name}/positions.yml"}:
        raise Refused("state_layout_unknown")
    if "alloy_seed.json" in layout and layout["alloy_seed.json"][0] != "file":
        raise Refused("state_layout_unknown")
    component_entry = layout.get(component_name)
    if not component_entry or component_entry[0] != "dir":
        raise Refused("cursor_not_persisted")
    positions = component / "positions.yml"
    try:
        before = positions.lstat()
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_size <= 0 or before.st_size > MAX_CURSOR_BYTES
            or before.st_mode & 0o022 or before.st_mode & 0o7000):
            raise Refused("cursor_unavailable")
        fd = os.open(positions, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino, opened.st_size) != (before.st_dev, before.st_ino, before.st_size):
                raise Refused("cursor_changed")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                content = stream.read(MAX_CURSOR_BYTES + 1)
            after = os.fstat(fd)
            path_after = positions.lstat()
            if ((after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                    != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
                    or (path_after.st_dev, path_after.st_ino) != (before.st_dev, before.st_ino)):
                raise Refused("cursor_changed")
        finally:
            os.close(fd)
    except FileNotFoundError as exc:
        raise Refused("cursor_not_persisted") from exc
    except OSError as exc:
        raise Refused("cursor_unavailable") from exc
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Refused("cursor_unavailable") from exc
    match = re.fullmatch(r"positions:\n  \? path: ([^\n]+)\n    labels: ([^\n]+)\n  : ([^\n]+)\n?", text)
    if not match:
        raise Refused("cursor_unavailable")

    def scalar(value: str) -> str:
        if len(value) >= 2 and value[0] == value[-1] == "'":
            return value[1:-1].replace("''", "'")
        if len(value) >= 2 and value[0] == value[-1] == '"':
            return json.loads(value)
        return value

    path, labels_value, cursor = map(scalar, match.groups())
    if path != "cursor-loki.source.journal.o11y_alloy" or labels_value or not CURSOR_RE.fullmatch(cursor):
        raise Refused("cursor_unavailable")
    return cursor


def read_cursor(binary: str, name: str) -> str:
    root = volume_root(binary, name)
    try:
        output = run([
            binary, "unshare", sys.executable, str(Path(__file__).resolve()),
            "--scan-volume", str(root), "cursor",
        ], timeout=30, output_limit=4096)
        try:
            response = json.loads(output)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise Refused("cursor_unavailable") from exc
        if not isinstance(response, dict):
            raise Refused("cursor_unavailable")
        if response.get("status") == "refused":
            reason = response.get("reason")
            if not isinstance(reason, str) or (reason not in NAMESPACE_SCAN_REFUSALS and reason not in (
                    "cursor_not_persisted", "cursor_unavailable", "cursor_changed", "state_layout_unknown")):
                raise Refused("cursor_unavailable")
            raise Refused(reason)
        cursor = response.get("cursor")
        if response.get("status") != "cursor" or not isinstance(cursor, str) or not CURSOR_RE.fullmatch(cursor):
            raise Refused("cursor_unavailable")
        return cursor
    finally:
        volume_unmount(binary, name)


def read_cursor_until_persisted(binary: str, name: str) -> str:
    deadline = time.monotonic() + CURSOR_WAIT_SECONDS
    while True:
        try:
            return read_cursor(binary, name)
        except Refused as exc:
            if exc.category != "cursor_not_persisted":
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Refused("cursor_persistence_timeout") from exc
            time.sleep(min(CURSOR_POLL_SECONDS, remaining))


def namespace_mutation_probe(binary: str, container_id: str) -> None:
    # The pinned image must provide the shell required to prove namespace operations in-place.
    command = (
        "set -eu; test \"$(id -u):$(id -g)\" = 0:0; "
        "stat -c '%F:%u:%g:%a:%h' /alloy-state; "
        "stat -c '%F:%u:%g:%a:%h' /alloy-state/loki.source.journal.o11y_alloy; "
        "stat -c '%F:%u:%g:%a:%h' /alloy-state/loki.source.journal.o11y_alloy/positions.yml; "
        "p=/alloy-state/.journal-proof-probe; q=/alloy-state/.journal-proof-renamed; "
        ": > \"$p\"; mv \"$p\" \"$q\"; rm \"$q\""
    )
    try:
        result = run([binary, "exec", "--user", "0:0", container_id, "/bin/sh", "-ec", command], timeout=20)
    except Refused as exc:
        if exc.category in ("provider_operation_failed", "provider_unavailable"):
            raise Refused("namespace_write_unverified") from exc
        raise
    try:
        records = result.decode("ascii").splitlines()
        if len(records) != 3:
            raise ValueError
        root_type, root_uid, root_gid, root_mode, root_links = records[0].split(":")
        component_type, component_uid, component_gid, component_mode, component_links = records[1].split(":")
        file_type, file_uid, file_gid, file_mode, file_links = records[2].split(":")
        root_bits, component_bits, file_bits = (int(value, 8) for value in (root_mode, component_mode, file_mode))
        if (root_type != "directory" or root_uid != "0" or root_gid != "0" or root_links != "3"
                or root_bits & 0o700 != 0o700 or root_bits & (0o022 | 0o7000)
                or component_type != "directory" or component_uid != "0" or component_gid != "0"
                or component_links != "2" or component_bits & 0o700 != 0o700
                or component_bits & (0o022 | 0o7000)
                or file_type != "regular file" or file_uid != "0" or file_gid != "0" or file_links != "1"
                or file_bits & 0o600 != 0o600 or file_bits & (0o022 | 0o7000)):
            raise ValueError
    except (UnicodeDecodeError, ValueError) as exc:
        raise Refused("namespace_owner_unverified") from exc


def run_alloy_phase(binary: str, owner: str, image_id: str, volume: str, scratch: Path,
                    journal_dir: Path, marker: str, phase: str,
                    forbidden_marker: str | None = None) -> tuple[str, dict]:
    config_path = scratch / f"{phase}.alloy"
    config_path.write_bytes(alloy_config())
    config_path.chmod(0o600)
    mounts = [f"{volume}:{STATE_PATH}:rw,nocopy", f"{config_path}:{CONFIG_PATH}:ro",
              f"{journal_dir}:{JOURNAL_PATH}:ro"]
    name = f"{owner}-alloy-{phase}-{uuid.uuid4().hex[:8]}"
    command = ["run", "--storage.path=/alloy-state",
               "--server.http.listen-addr=127.0.0.1:12345", CONFIG_PATH]
    inspect = create_container(binary, name, owner, f"alloy-{phase}", image_id, mounts, command)
    mount_check(inspect, [(STATE_PATH, "volume", True, volume),
                          (CONFIG_PATH, "bind", False, None), (JOURNAL_PATH, "bind", False, None)])
    observed_mounts = {item.get("Destination"): item for item in inspect["Mounts"]}
    if (Path(observed_mounts[CONFIG_PATH].get("Source", "")).resolve() != config_path.resolve()
            or Path(observed_mounts[JOURNAL_PATH].get("Source", "")).resolve() != journal_dir.resolve()):
        raise Refused("effective_mount_unverified")
    run([binary, "start", inspect["Id"]])
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        output = run_streams(
            [binary, "logs", "--tail", str(LOG_TAIL_LINES), inspect["Id"]],
            timeout=15, output_limit=MAX_LOG_OUTPUT,
        )
        if forbidden_marker and forbidden_marker.encode("ascii") in output:
            raise Refused("cursor_resume_failed")
        if marker.encode("ascii") in output:
            break
        time.sleep(0.5)
    else:
        raise Refused("journal_ingestion_unverified")
    namespace_mutation_probe(binary, inspect["Id"])
    run([binary, "stop", "--time", "20", inspect["Id"]], timeout=30)
    stopped = inspect_container(binary, inspect["Id"])
    if ((stopped.get("State") or {}).get("Running") is not False
            or (stopped.get("State") or {}).get("ExitCode") != 0):
        raise Refused("graceful_stop_unverified")
    cursor = read_cursor_until_persisted(binary, volume)
    remove_owned_container(binary, inspect["Id"], owner)
    return cursor, stopped


def verify_no_foreign_volume_users(binary: str, volume_name: str, owned_id: str) -> None:
    containers, _ = inventories(binary)
    for row in containers:
        cid = row.get("Id", row.get("ID"))
        if cid == owned_id:
            continue
        inspect = inspect_container(binary, cid)
        for mount in inspect.get("Mounts") or []:
            if mount.get("Type") == "volume" and mount.get("Name") == volume_name:
                raise Refused("cleanup_volume_in_use")


def cleanup(binary: str, owner: str) -> None:
    for container_id, expected in list(OWNED_CONTAINERS.items()):
        inspect = inspect_container(binary, container_id)
        if not container_identity_matches(inspect, expected, owner):
            raise Refused("cleanup_identity_uncertain")
        for mount in inspect.get("Mounts") or []:
            if mount.get("Type") == "volume" and mount.get("Name") in OWNED_VOLUMES:
                verify_no_foreign_volume_users(binary, mount["Name"], container_id)
        if (inspect.get("State") or {}).get("Running") is True:
            run([binary, "stop", "--time", "20", container_id], timeout=30)
        run([binary, "rm", container_id])
        OWNED_CONTAINERS.pop(container_id)
    if OWNED_CONTAINERS:
        raise Refused("cleanup_identity_uncertain")
    for name, record in list(OWNED_VOLUMES.items()):
        volume = inspect_volume(binary, name, "cleanup_identity_uncertain")
        if not volume_identity_matches(volume, record, owner, record["role"]):
            raise Refused("cleanup_identity_uncertain")
        verify_no_foreign_volume_users(binary, name, "")
        if record["root"] is not None:
            path = Path(record["identity"]["Mountpoint"])
            try:
                current = path.lstat()
            except OSError as exc:
                raise Refused("cleanup_identity_uncertain") from exc
            if (current.st_dev, current.st_ino) != record["root"]:
                raise Refused("cleanup_identity_uncertain")
        run([binary, "volume", "rm", name])
        OWNED_VOLUMES.pop(name)
    containers, volumes = inventories(binary)
    owned_container_remains = any(
        labels(inspect_container(binary, row.get("Id", row.get("ID")))).get(PROOF_OWNER) == owner
        for row in containers
    )
    if owned_container_remains:
        raise Refused("cleanup_identity_uncertain")
    if any(labels(inspect_volume(binary, row_name(row))).get(PROOF_OWNER) == owner for row in volumes):
        raise Refused("cleanup_identity_uncertain")


def image_identity(binary: str) -> tuple[str, Path]:
    image = one(data([binary, "image", "inspect", IMAGE], "pinned_image_unavailable"), "pinned_image_unavailable")
    image_id = image.get("Id")
    if not isinstance(image_id, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise Refused("pinned_image_identity_unverified")
    config = image.get("Config") or {}
    entrypoint = config.get("Entrypoint")
    if entrypoint != ["/bin/alloy"]:
        raise Refused("pinned_image_capability_unavailable")
    return image_id, Path("/")


def validate_config(binary: str, owner: str, image_id: str, scratch: Path) -> None:
    config = scratch / "validation.alloy"
    config.write_bytes(alloy_config())
    config.chmod(0o600)
    name = f"{owner}-validate-{uuid.uuid4().hex[:8]}"
    inspect = create_container(binary, name, owner, "validate", image_id,
                               [f"{config}:{CONFIG_PATH}:ro"], ["validate", CONFIG_PATH])
    mount_check(inspect, [(CONFIG_PATH, "bind", False, None)])
    run([binary, "start", inspect["Id"]])
    status = run([binary, "wait", inspect["Id"]], timeout=40, output_limit=64).decode().strip()
    if status != "0":
        raise Refused("alloy_config_invalid")
    remove_owned_container(binary, inspect["Id"], owner)


def cursor_proof(binary: str, owner: str, image_id: str, scratch: Path) -> tuple[str, str]:
    name, _ = volume_create(binary, owner, "cursor")
    if volume_scan(binary, name, target_state=True):
        raise Refused("volume_not_fresh")
    journal_dir, first_marker = append_fixture(scratch, 1)
    first_cursor, _ = run_alloy_phase(binary, owner, image_id, name, scratch, journal_dir, first_marker, "first")
    # The same journal file receives a second independently generated record after graceful stop.
    _, second_marker = append_fixture(scratch, 2)
    second_cursor, _ = run_alloy_phase(binary, owner, image_id, name, scratch, journal_dir,
                                       second_marker, "resume", forbidden_marker=first_marker)
    if first_cursor == second_cursor:
        raise Refused("cursor_did_not_advance")
    return "created_resumed_advanced", name


def prove() -> dict:
    config_path = os.environ.get("O11Y_PROOF_CONFIG_PATH", "")
    required = {
        "reviewed_sha": (os.environ.get("O11Y_PROOF_REVIEWED_SHA", ""), r"[0-9a-f]{40}"),
        "inventory_revision": (os.environ.get("O11Y_PROOF_INVENTORY_REVISION", ""), r"[0-9a-f]{64}"),
    }
    if (any(not re.fullmatch(pattern, value) for value, pattern in required.values())
            or not config_path or not Path(config_path).is_absolute()):
        raise Refused("proof_binding_unverified")
    config_digest = hash_file(Path(config_path), MAX_FILE_BYTES)
    binary, binary_sha, runtime = podman_info()
    owner = f"jproof-{uuid.uuid4().hex}"
    before = baseline(binary, owner)
    _, project_network_sha = receiver_context(binary)
    image_id, _ = image_identity(binary)
    scratch_path = Path(tempfile.mkdtemp(prefix="journal-proof-"))
    scratch_identity = scratch_path.lstat()
    outcome: Exception | None = None
    copy_control = positive_no_copy = state_no_copy = repeat_state_no_copy = "unverified"
    cursor_status = "unverified"
    state_layout_digest = positive_layout_digest = "unverified"
    try:
        image_container = create_container(binary, f"{owner}-image", owner, "image", image_id, [], ["--version"])
        image_inspect_id = image_container["Id"]
        inspect = inspect_container(binary, image_inspect_id)
        if (inspect.get("HostConfig", {}).get("NetworkMode") != "none"
                or inspect.get("HostConfig", {}).get("ReadonlyRootfs") is not True):
            raise Refused("container_isolation_unverified")
        mount_path = run([binary, "mount", image_inspect_id], timeout=30, output_limit=8192).decode().strip()
        image_root = Path(mount_path)
        if not image_root.is_absolute():
            raise Refused("image_layout_unavailable")
        try:
            image_state, positive_path, positive = image_manifest(binary, image_root)
            state_layout_digest = digest(image_state)
            positive_layout_digest = digest(positive)
        finally:
            run([binary, "unmount", image_inspect_id], timeout=30)
        if not positive_path or not positive:
            raise Refused("copy_control_unverified")
        copy_control = prove_mount_behavior(binary, owner, image_id, f"/{positive_path}", positive, no_copy=False)
        positive_no_copy = prove_mount_behavior(binary, owner, image_id, f"/{positive_path}", positive, no_copy=True)
        if copy_control != "copied" or positive_no_copy != "no_copy":
            raise Refused("copy_behavior_unverified")
        state_no_copy = prove_mount_behavior(binary, owner, image_id, STATE_PATH, image_state, no_copy=True)
        if state_no_copy != "no_copy":
            raise Refused("no_copy_behavior_failed")
        validate_config(binary, owner, image_id, scratch_path)
        cursor_status, first_volume = cursor_proof(binary, owner, image_id, scratch_path)
        repeat_status, repeat_volume = cursor_proof(binary, owner, image_id, scratch_path)
        if repeat_status != cursor_status or repeat_volume == first_volume:
            raise Refused("repeatability_unverified")
        repeat_state_no_copy = "no_copy"
    except Exception as exc:
        outcome = exc
    cleanup_failed = False
    try:
        cleanup(binary, owner)
    except Exception:
        cleanup_failed = True
    if cleanup_failed:
        raise Refused("cleanup_uncertain") from outcome
    try:
        after = baseline(binary, owner)
        if after != before:
            raise Refused("receiver_baseline_changed")
        current = scratch_path.lstat()
        if (current.st_dev, current.st_ino) != (scratch_identity.st_dev, scratch_identity.st_ino):
            raise Refused("scratch_cleanup_uncertain")
        shutil.rmtree(scratch_path)
    except Exception:
        raise Refused("baseline_or_scratch_uncertain") from outcome
    if outcome:
        if isinstance(outcome, Refused):
            raise outcome
        raise Refused("proof_unavailable") from outcome
    return {
        "status": "partial",
        "reason": "operational_acceptance_gates_incomplete",
        "proof_scope": "disposable_rootless_podman_and_cursor_source_only",
        "copy_control": copy_control,
        "positive_no_copy": positive_no_copy,
        "state_no_copy": state_no_copy,
        "repeat_state_no_copy": repeat_state_no_copy,
        "state_layout": "explicit_component_only",
        "cursor": cursor_status,
        "image_id": image_id,
        "state_layout_sha256": state_layout_digest,
        "positive_layout_sha256": positive_layout_digest,
        "podman_client": runtime["client"],
        "podman_server": runtime["server"],
        "podman_executable_sha256": binary_sha,
        "runtime_context_sha256": digest(runtime["context"]),
        "project_network_sha256": project_network_sha,
        "reviewed_sha": required["reviewed_sha"][0],
        "inventory_revision": required["inventory_revision"][0],
        "config_sha256": config_digest,
    }


def main() -> int:
    if sys.argv[1:2] == ["--scan-volume"]:
        try:
            if (len(sys.argv) != 4 or os.geteuid() != 0
                    or sys.argv[3] not in ("state", "general", "cursor")
                    or not Path(sys.argv[2]).is_absolute()):
                raise Refused("namespace_scan_unverified")
            root = Path(sys.argv[2])
            try:
                root_info = root.lstat()
            except FileNotFoundError:
                response = {"status": "absent"}
            else:
                if not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode):
                    raise Refused("state_path_unsafe" if sys.argv[3] == "state" else "copy_control_unverified")
                if sys.argv[3] == "cursor":
                    response = {"status": "cursor", "cursor": cursor_from_manifest(root)}
                else:
                    manifest = scandir_manifest(root, target_state=sys.argv[3] == "state")
                    response = {"status": "scanned", "manifest": manifest}
        except Refused as exc:
            response = {"status": "refused", "reason": exc.category}
        except Exception:
            response = {"status": "refused", "reason": "layout_unavailable"}
        sys.stdout.write(json.dumps(response, sort_keys=True, separators=(",", ":")) + "\n")
        return 0
    try:
        report = prove()
    except Refused as exc:
        report = {"status": "unverified", "reason": exc.category}
    except Exception:
        report = {"status": "unverified", "reason": "proof_unavailable"}
    sys.stdout.write(json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n")
    return 0 if report.get("status") == "partial" else 1


if __name__ == "__main__":
    raise SystemExit(main())
