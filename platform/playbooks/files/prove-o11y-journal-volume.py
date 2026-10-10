#!/usr/bin/env python3
"""Exercise Podman Compose copy/no-copy with disposable volumes and pinned Alloy."""

from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from pathlib import Path, PurePosixPath

IMAGE = "docker.io/grafana/alloy:v1.9.2"
STATE = "/alloy-state"
POSITIVE_PATHS = ("/etc/ssl/certs", "/usr/share/zoneinfo")
MAX_ENTRIES = 20_000
MAX_BYTES = 512 * 1024 * 1024
FIRST_MOUNT_FLOOR = 16 * 1024 * 1024
MAX_FILE = 1024 * 1024
VERSION = re.compile(r"^[A-Za-z0-9_.+-]{1,80}$")
OWNED_CONTAINERS: dict[str, dict] = {}
OWNED_VOLUMES: dict[str, dict] = {}


class Refused(Exception):
    def __init__(self, reason: str):
        self.reason = reason


def run(argv: list[str], timeout: int = 120, check: bool = True) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            argv, capture_output=True, timeout=timeout, check=False, env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Refused("provider_unavailable") from exc
    if check and result.returncode:
        raise Refused("provider_operation_failed")
    return result


def data(argv: list[str]):
    try:
        return json.loads(run(argv).stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Refused("provider_response_invalid") from exc


def one(value, reason: str) -> dict:
    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict):
        return value[0]
    if isinstance(value, dict):
        return value
    raise Refused(reason)


def version(argv: list[str]) -> str:
    try:
        value = run(argv, 20).stdout.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise Refused("provider_version_unverified") from exc
    match = re.search(r"(?:version\s+)?([0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9_.-]+)?)", value, re.I)
    if not match or not VERSION.fullmatch(match.group(1)):
        raise Refused("provider_version_unverified")
    return match.group(1)


def image_files(archive_path: Path) -> tuple[dict[str, bytes], str | None, dict[str, bytes]]:
    if archive_path.stat().st_size > MAX_BYTES:
        raise Refused("image_layout_unbounded")
    state: dict[str, bytes] = {}
    candidates: dict[str, dict[str, bytes]] = {path: {} for path in POSITIVE_PATHS}
    try:
        with tarfile.open(archive_path, mode="r|") as archive:
            for index, member in enumerate(archive):
                if index >= MAX_ENTRIES:
                    raise Refused("image_layout_unbounded")
                full = str(PurePosixPath("/") / member.name.lstrip("/"))
                if ".." in PurePosixPath(full).parts or not member.isfile() or member.size > MAX_FILE:
                    continue
                bucket = None
                relative = ""
                if full.startswith(STATE + "/"):
                    bucket, relative = state, full[len(STATE) + 1:]
                else:
                    for path in POSITIVE_PATHS:
                        if full.startswith(path.rstrip("/") + "/"):
                            bucket, relative = candidates[path], full[len(path.rstrip("/")) + 1:]
                            break
                if bucket is None:
                    continue
                if len(bucket) >= 128:
                    raise Refused("image_layout_unbounded")
                stream = archive.extractfile(member)
                contents = stream.read(MAX_FILE + 1) if stream else b""
                if len(contents) != member.size:
                    raise Refused("image_layout_unavailable")
                bucket[relative] = contents
    except (tarfile.TarError, OSError) as exc:
        raise Refused("image_layout_unavailable") from exc
    positive = next((path for path in POSITIVE_PATHS if candidates[path]), None)
    return state, positive, candidates[positive] if positive else {}


def compose_yaml(project: str, container: str, target: str, nocopy: bool, config: Path) -> str:
    q = json.dumps
    return "\n".join((
        "services:", "  proof:", f"    image: {q(IMAGE)}", f"    container_name: {q(container)}",
        '    pull_policy: "never"',
        '    command: ["validate", "--stability.level=generally-available", "/etc/alloy/proof.alloy"]',
        '    user: "0:0"', "    read_only: true", '    network_mode: "none"', '    cap_drop: ["ALL"]',
        '    security_opt: ["no-new-privileges:true"]', '    restart: "no"',
        "    labels:", '      "agent-cloud.proof-owner": ' + q(project), "    volumes:",
        "      - type: volume", "        source: state", f"        target: {q(target)}",
        f"        volume: {{nocopy: {'true' if nocopy else 'false'}}}",
        "      - type: bind", f"        source: {q(str(config))}", '        target: "/etc/alloy/proof.alloy"',
        "        read_only: true", "        bind: {create_host_path: false}",
        "volumes:", "  state:", "    labels:", '      "agent-cloud.proof-owner": ' + q(project), "",
    ))


def list_volumes() -> list[dict]:
    rows = data(["podman", "volume", "ls", "--format", "json"])
    if not isinstance(rows, list) or len(rows) > 4096 or any(not isinstance(row, dict) for row in rows):
        raise Refused("volume_inventory_unverified")
    return rows


def resource_name(row: dict):
    name = row.get("Name") or row.get("Names")
    if isinstance(name, list) and len(name) == 1:
        return name[0]
    return name if isinstance(name, str) else None


def labels(row: dict) -> dict[str, str]:
    value = row.get("Labels")
    if value is None:
        value = (row.get("Config") or {}).get("Labels")
    if value is None:
        return {}
    if isinstance(value, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        return value
    if isinstance(value, str):
        if not value:
            return {}
        result = {}
        for item in value.split(","):
            key, separator, content = item.partition("=")
            if not separator or not key or key in result:
                raise Refused("provider_response_invalid")
            result[key] = content
        return result
    raise Refused("provider_response_invalid")


def volume_identity(row: dict) -> dict:
    fields = ("Name", "CreatedAt", "Driver", "Scope", "Options", "Mountpoint", "Labels")
    identity = {field: row.get(field) for field in fields}
    if (not isinstance(identity["Name"], str) or not identity["Name"]
            or not isinstance(identity["CreatedAt"], str) or not identity["CreatedAt"]
            or not isinstance(identity["Driver"], str) or not isinstance(identity["Mountpoint"], str)):
        raise Refused("volume_identity_unverified")
    return identity


def resource_snapshot() -> str:
    rows = []
    for volume in list_volumes():
        volume_labels = labels(volume)
        if volume_labels.get("agent-cloud.proof-owner"):
            continue
        name = resource_name(volume)
        if not name:
            raise Refused("volume_inventory_unverified")
        rows.append([name, volume.get("Driver"), volume.get("CreatedAt"), volume.get("MountCount"), volume_labels])
    receiver_names = (
        "o11y-prometheus", "o11y-loki", "o11y-alloy", "o11y-tempo",
        "o11y-pyroscope", "o11y-grafana", "o11y-node-exporter",
    )
    for name in receiver_names:
        row = one(data(["podman", "inspect", name]), "receiver_inventory_unverified")
        rows.append([name, row.get("Id"), row.get("State", {}).get("Status"), row.get("State", {}).get("StartedAt")])
    rows.sort(key=lambda row: row[0])
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def owned_rows(project: str, kind: str) -> list[dict]:
    rows = list_volumes() if kind == "volume" else data(["podman", "ps", "-a", "--format", "json"])
    if not isinstance(rows, list) or len(rows) > 4096:
        raise Refused("cleanup_inventory_unverified")
    return [row for row in rows if labels(row).get("agent-cloud.proof-owner") == project]


def test_case(
    project: str, target: str, nocopy: bool, expected: dict[str, bytes], scratch: Path, image_id: str,
) -> str:
    if owned_rows(project, "volume") or owned_rows(project, "container"):
        raise Refused("proof_identity_collision")
    container = f"{project}-c"
    config = scratch / f"{project}.alloy"
    compose = scratch / f"{project}.yml"
    config.write_text('loki.echo "proof" {}\n', encoding="utf-8")
    config.chmod(0o600)
    compose.write_text(compose_yaml(project, container, target, nocopy, config), encoding="utf-8")
    compose.chmod(0o600)
    rendered = run(["podman-compose", "-p", project, "-f", str(compose), "config"]).stdout.decode("utf-8")
    policy = f"nocopy: {'true' if nocopy else 'false'}"
    no_pull = re.search(r"(?m)^\s*pull_policy:\s*[\"']?never[\"']?\s*$", rendered)
    if IMAGE not in rendered or policy not in rendered or not no_pull:
        raise Refused("compose_policy_unverified")
    run(["podman-compose", "-p", project, "-f", str(compose), "up", "-d"], 180)
    container_data = one(data(["podman", "inspect", container]), "container_identity_unverified")
    if (labels(container_data).get("agent-cloud.proof-owner") != project
            or container_data.get("Config", {}).get("Image") != IMAGE):
        raise Refused("container_identity_changed")
    if container_data.get("Image") != image_id:
        raise Refused("pinned_image_identity_changed")
    if not isinstance(container_data.get("Created"), str) or not container_data["Created"]:
        raise Refused("container_identity_unverified")
    OWNED_CONTAINERS[project] = {
        "Id": container_data.get("Id"), "Image": container_data.get("Image"),
        "Created": container_data["Created"], "owner": project,
    }
    if container_data.get("State", {}).get("ExitCode") != 0:
        raise Refused("image_validation_failed")
    host_config = container_data.get("HostConfig") or {}
    config_data = container_data.get("Config") or {}
    if (host_config.get("NetworkMode") != "none" or host_config.get("ReadonlyRootfs") is not True
            or "ALL" not in host_config.get("CapDrop", [])
            or "no-new-privileges:true" not in host_config.get("SecurityOpt", [])
            or host_config.get("PortBindings") or config_data.get("User") != "0:0"):
        raise Refused("container_isolation_unverified")
    mounts = container_data.get("Mounts") or []
    state_mounts = [mount for mount in mounts if mount.get("Destination") == target]
    config_mounts = [mount for mount in mounts if mount.get("Destination") == "/etc/alloy/proof.alloy"]
    if (len(mounts) != 2 or len(state_mounts) != 1 or state_mounts[0].get("Type") != "volume"
            or not state_mounts[0].get("RW") or len(config_mounts) != 1
            or config_mounts[0].get("Type") != "bind" or config_mounts[0].get("RW")
            or Path(config_mounts[0].get("Source", "")).resolve() != config.resolve()):
        raise Refused("effective_mount_unverified")
    volume_rows = owned_rows(project, "volume")
    if len(volume_rows) != 1:
        raise Refused("volume_identity_ambiguous")
    name = resource_name(volume_rows[0])
    volume = one(data(["podman", "volume", "inspect", name]), "volume_identity_unverified")
    identity = volume_identity(volume)
    if (identity["Name"] != name or labels(volume).get("agent-cloud.proof-owner") != project
            or state_mounts[0].get("Name") != name or volume.get("MountCount", 0) not in (0, None)):
        raise Refused("volume_identity_changed")
    OWNED_VOLUMES[project] = identity
    mounted = run(["podman", "volume", "mount", name], 30).stdout.decode("utf-8").strip()
    try:
        root = Path(mounted)
        expected_root = volume.get("Mountpoint")
        if (not root.is_absolute() or not isinstance(expected_root, str) or not Path(expected_root).is_absolute()
                or not root.is_dir() or root.is_symlink() or root.resolve() != Path(expected_root).resolve()):
            raise Refused("volume_mount_unverified")
        found: dict[str, bytes] = {}
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root).as_posix()
            if len(found) >= 128 or path.is_symlink() or ".." in PurePosixPath(relative).parts:
                raise Refused("volume_layout_unsafe")
            if not path.is_file() or path.stat().st_size > MAX_FILE:
                raise Refused("volume_layout_unsafe")
            found[relative] = path.read_bytes()
    finally:
        run(["podman", "volume", "unmount", name], 30)
    if nocopy:
        if found:
            raise Refused("no_copy_behavior_failed")
        return "empty"
    if not any(file in found and hashlib.sha256(found[file]).digest() == hashlib.sha256(contents).digest()
               for file, contents in expected.items()):
        raise Refused("copy_behavior_unverified")
    return "copied"


def cleanup(project: str) -> None:
    containers = owned_rows(project, "container")
    if len(containers) > 1:
        raise Refused("cleanup_container_ambiguous")
    for row in containers:
        cid = row.get("Id") or row.get("ID")
        row_labels = labels(row)
        if not isinstance(cid, str) or not re.fullmatch(r"[0-9a-f]{12,64}", cid):
            raise Refused("cleanup_container_unverified")
        if row_labels.get("agent-cloud.proof-owner") != project:
            raise Refused("cleanup_container_unverified")
        inspect = one(data(["podman", "inspect", cid]), "cleanup_container_unverified")
        expected = OWNED_CONTAINERS.get(project)
        if (expected is None or inspect.get("Id") != cid or inspect.get("Config", {}).get("Image") != IMAGE
                or inspect.get("Id") != expected["Id"] or inspect.get("Image") != expected["Image"]
                or inspect.get("Created") != expected["Created"]):
            raise Refused("cleanup_container_unverified")
        if inspect.get("State", {}).get("Running"):
            run(["podman", "stop", "--time", "10", cid], 30)
        run(["podman", "rm", cid], 30)
        OWNED_CONTAINERS.pop(project, None)
    if owned_rows(project, "container"):
        raise Refused("cleanup_container_unverified")
    volumes = owned_rows(project, "volume")
    if len(volumes) > 1:
        raise Refused("cleanup_volume_ambiguous")
    for row in volumes:
        name = resource_name(row)
        if not name:
            raise Refused("cleanup_volume_unverified")
        inspect = one(data(["podman", "volume", "inspect", name]), "cleanup_volume_unverified")
        expected = OWNED_VOLUMES.get(project)
        if (expected is None or volume_identity(inspect) != expected
                or labels(inspect).get("agent-cloud.proof-owner") != project
                or inspect.get("MountCount", 0) not in (0, None)):
            raise Refused("cleanup_volume_unverified")
        run(["podman", "volume", "rm", name], 30)
        OWNED_VOLUMES.pop(project, None)
    if owned_rows(project, "volume"):
        raise Refused("cleanup_volume_unverified")


def export_image(container: str, destination: Path) -> None:
    process = None
    try:
        with destination.open("wb") as output:
            process = subprocess.Popen(
                ["podman", "export", container], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env={**os.environ, "LC_ALL": "C"},
            )
            deadline = time.monotonic() + 180
            total = 0
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise Refused("image_export_unavailable")
                    for key, _ in selector.select(min(1, remaining)):
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        total += len(chunk)
                        if total > MAX_BYTES:
                            raise Refused("image_export_unavailable")
                        output.write(chunk)
            if process.wait(timeout=5) != 0:
                raise Refused("image_export_unavailable")
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Refused("image_export_unavailable") from exc
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()


def prove() -> dict:
    if os.geteuid() == 0:
        raise Refused("rootless_user_required")
    compose_binary = shutil.which("podman-compose")
    if not compose_binary or not os.path.isfile(compose_binary) or not os.access(compose_binary, os.X_OK):
        raise Refused("compose_provider_unavailable")
    client = version(["podman", "version", "--format", "{{.Client.Version}}"])
    server = version(["podman", "version", "--format", "{{.Server.Version}}"])
    compose_version = version(["podman-compose", "--version"])
    if run(["podman", "info", "--format", "{{.Host.Security.Rootless}}"]).stdout.decode().strip() != "true":
        raise Refused("rootless_runtime_required")
    image = one(data(["podman", "image", "inspect", IMAGE]), "pinned_image_unavailable")
    image_id = image.get("Id")
    if not isinstance(image_id, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise Refused("pinned_image_identity_unverified")
    image_size = image.get("Size")
    if not isinstance(image_size, int) or image_size <= 0 or image_size > MAX_BYTES:
        raise Refused("image_layout_unbounded")
    binary_sha = hashlib.sha256(Path(compose_binary).read_bytes()).hexdigest()
    baseline = resource_snapshot()
    token = uuid.uuid4().hex[:12]
    scratch = Path(tempfile.mkdtemp(prefix="o11y-journal-proof-"))
    if shutil.disk_usage(scratch).free < MAX_BYTES + FIRST_MOUNT_FLOOR:
        shutil.rmtree(scratch)
        raise Refused("proof_scratch_capacity_low")
    image_container = f"jpi{token}"
    run(["podman", "create", "--pull=never", "--network", "none", "--read-only",
         "--label", f"agent-cloud.proof-owner=image-{token}", "--name", image_container, IMAGE], 60)
    image_row = one(data(["podman", "inspect", image_container]), "image_container_identity_unverified")
    image_container_id = image_row.get("Id")
    image_container_created = image_row.get("Created")
    if (not isinstance(image_container_id, str) or not re.fullmatch(r"[0-9a-f]{12,64}", image_container_id)
            or not isinstance(image_container_created, str) or not image_container_created
            or labels(image_row).get("agent-cloud.proof-owner") != f"image-{token}"
            or image_row.get("Image") != image_id or image_row.get("State", {}).get("Running")
            or image_row.get("Mounts")):
        raise Refused("image_container_identity_unverified")
    rootfs_path = scratch / "image-export.tar"
    try:
        export_image(image_container_id, rootfs_path)
    finally:
        row = one(data(["podman", "inspect", image_container_id]), "image_container_identity_unverified")
        if (row.get("Id") != image_container_id or row.get("Created") != image_container_created
                or labels(row).get("agent-cloud.proof-owner") != f"image-{token}"
                or row.get("Image") != image_id or row.get("State", {}).get("Running") or row.get("Mounts")):
            raise Refused("image_container_identity_unverified")
        run(["podman", "rm", image_container_id], 30)
        if run(["podman", "container", "exists", image_container_id], 20, check=False).returncode == 0:
            raise Refused("image_container_cleanup_unverified")
    state_files, positive_path, positive_files = image_files(rootfs_path)
    if not positive_path or not positive_files:
        shutil.rmtree(scratch)
        raise Refused("copy_behavior_unverified")
    projects = [f"jpv{token}a", f"jpv{token}b", f"jpv{token}c"]
    outcome = None
    try:
        ordinary = test_case(projects[0], positive_path, False, positive_files, scratch, image_id)
        positive_no_copy = test_case(projects[1], positive_path, True, positive_files, scratch, image_id)
        state_no_copy = test_case(projects[2], STATE, True, state_files, scratch, image_id)
        if (ordinary, positive_no_copy, state_no_copy) != ("copied", "empty", "empty"):
            raise Refused("copy_behavior_unverified")
    except Exception as exc:
        outcome = exc
    cleanup_failed = False
    for project in projects:
        try:
            cleanup(project)
        except Exception:
            cleanup_failed = True
    if cleanup_failed:
        raise Refused("cleanup_uncertain") from outcome
    try:
        if resource_snapshot() != baseline:
            raise Refused("receiver_or_volume_baseline_changed")
    finally:
        shutil.rmtree(scratch)
    if outcome:
        raise outcome
    return {
        "status": "provider_copy_behavior_proven",
        "copy_control": "copied",
        "positive_control_no_copy": "empty",
        "state_path_no_copy": "empty",
        "effective_mount": "rw",
        "image": IMAGE,
        "image_id": image_id,
        "compose_version": compose_version,
        "compose_binary_sha256": binary_sha,
        "podman_client": client,
        "podman_server": server,
        "state_destination": "alloy_state",
        "cursor_resume": "unverified",
    }


def main() -> int:
    try:
        report = prove()
    except Refused as exc:
        report = {"status": "unverified", "reason": exc.reason}
    except Exception:
        report = {"status": "unverified", "reason": "proof_unavailable"}
    sys.stdout.write(json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n")
    return 0 if report.get("status") == "provider_copy_behavior_proven" else 1


if __name__ == "__main__":
    raise SystemExit(main())
