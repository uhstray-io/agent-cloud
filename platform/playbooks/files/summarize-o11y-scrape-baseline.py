#!/usr/bin/env python3
"""Collect private scrape evidence and emit only a bounded public receipt."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import platform
import re
import selectors
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode, urlsplit

import yaml

ENGINE = "podman"
CONTAINER = "o11y-grafana"
PROMETHEUS = "http://prometheus:9090"
PROMETHEUS_SELF_INSTANCE = "localhost:9090"
PROMETHEUS_SELF_URL = "http://localhost:9090/metrics"
PROMETHEUS_SELF_PROBE_URL = "http://prometheus:9090/metrics"
SAMPLES_QUERY = "max_over_time(scrape_samples_scraped[24h])"
MAX_API_BYTES = 16 * 1024 * 1024
MAX_RESPONSE_BYTES = 160 * 1024 * 1024
MAX_RESPONSE_HEADERS = 64 * 1024
CONNECT_TIMEOUT = 3
TRANSFER_TIMEOUT = 10
CURL_MIN_VERSION = (8, 4, 0)
MAX_ROWS = 10000
MAX_INTEGER_DIGITS = 20
ALERT_INPUTS = (
    ("otelcol_receiver_refused_log_records_total", "alloy"),
    ("otelcol_receiver_refused_spans_total", "alloy"),
    ("otelcol_exporter_enqueue_failed_spans_total", "alloy"),
    ("otelcol_exporter_send_failed_spans_total", "alloy"),
    ("loki_discarded_samples_total", "loki"),
    ("tempo_discarded_spans_total", "tempo"),
)
EXPECTED_IDENTITY_FIELDS = ("job", "instance", "service", "cluster", "environment")
EXPECTED_SOURCE_VALUES = {"prometheus", "alloy"}
EXPECTED_UID = re.compile(r"^o11y_missing_[a-z0-9_]{1,27}\Z")
EXPECTED_GROUP_UID = re.compile(r"^o11y_missing_g[a-f0-9]{26}\Z")
MAX_IDENTITY_VALUE_LENGTH = 128
SAFE_IDENTITY_VALUE = re.compile(
    rf"^[A-Za-z0-9][A-Za-z0-9_.:/@-]{{0,{MAX_IDENTITY_VALUE_LENGTH - 1}}}\Z"
)
MAX_EXPECTED_TARGETS = 100
MAX_EXPECTED_GROUP_EXPRESSION_LENGTH = (
    MAX_EXPECTED_TARGETS * (
        len('absent_over_time(up{job="",instance="",service="",cluster="",environment=""}[5m])')
        + len(EXPECTED_IDENTITY_FIELDS) * MAX_IDENTITY_VALUE_LENGTH
    )
    + (MAX_EXPECTED_TARGETS - 1) * len(" or ")
)
MAX_SOURCE_CONFIG_BYTES = 1024 * 1024
SOURCE_CONFIG_SUFFIX = ("platform", "services", "o11y", "deployment", "config", "scrape.d")
SOURCE_CONFIG_FILES = {
    "agentgateway.yml": {"agentgateway"},
    "dgx-spark.yml": {"dgx-spark-node", "dgx-spark-gpu", "dgx-spark-vllm"},
}
SOURCE_LIMIT_FIELDS = (
    "body_size_limit",
    "sample_limit",
    "label_limit",
    "label_name_length_limit",
    "label_value_length_limit",
)

JOB_ALIASES = {
    "prometheus": "prometheus",
    "receiver-host": "receiver-host",
    "grafana": "grafana",
    "loki": "loki",
    "alloy": "alloy",
    "tempo": "tempo",
    "pyroscope": "pyroscope",
    "agentgateway": "agentgateway",
    "dgx-spark-node": "dgx-spark",
    "dgx-spark-gpu": "dgx-spark",
    "dgx-spark-vllm": "dgx-spark",
}
HOST_JOB = re.compile(r"host-node-[a-z0-9][a-z0-9_-]{0,31}\Z")
VERSION = re.compile(rb"^curl (\d+)\.(\d+)\.(\d+)", re.MULTILINE)
STATUS_LINE = re.compile(rb"^HTTP/\d(?:\.\d)? ([0-9]{3})(?: .*)?\r?$", re.IGNORECASE)
DECIMAL = re.compile(r"^[0-9]{1,20}$")
NUMERIC_HOST = re.compile(r"(?:[0-9]+|0[xX][0-9a-fA-F]+)(?:\.(?:[0-9]+|0[xX][0-9a-fA-F]+))*\Z")


class Refusal(ValueError):
    """A fixed refusal category that contains no target-controlled text."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


MAX_AVAILABILITY_CONTAINERS = 128
MAX_AVAILABILITY_COMMAND_BYTES = 256 * 1024
MAX_AVAILABILITY_INSPECT_SECONDS = 20
MAX_AVAILABILITY_DETAIL_BYTES = 64 * 1024
MAX_AVAILABILITY_CONFIG_BYTES = 1_048_576
MAX_AVAILABILITY_READBACK_BYTES = 16 * 1024 * 1024
MAX_AVAILABILITY_READBACK_SECONDS = 120
MAX_AVAILABILITY_DETAIL_ROWS = 256
AVAILABILITY_COMPONENT_DETAIL_PATH = "/api/v0/web/components/"
AVAILABILITY_COMPONENTS_PATH = "/api/v0/web/components"
AVAILABILITY_READY_PATH = "/-/ready"
ALLOY_ROOT_COMPONENT_NAMES = {
    "discovery.docker", "discovery.relabel", "loki.source.docker", "loki.write",
    "prometheus.scrape", "prometheus.relabel", "prometheus.remote_write",
    "prometheus.exporter.blackbox", "otelcol.receiver.otlp", "otelcol.processor.memory_limiter",
    "otelcol.connector.spanlogs", "otelcol.processor.attributes", "otelcol.exporter.loki",
    "otelcol.processor.batch", "otelcol.exporter.otlp", "pyroscope.write", "pyroscope.scrape",
}
ALLOY_ROOT_COMPONENT_SENTINELS = {
    "loki.write.default", "otelcol.receiver.otlp.traces", "otelcol.receiver.otlp.conformance",
}
ALLOY_DETAIL_FIELDS = {
    "arguments", "debugInfo", "exports", "health", "label", "localID", "moduleID",
    "name", "original", "referencedBy", "referencesTo", "type",
}
ALLOY_HEALTH_METRICS = (
    "up", "probe_success", "probe_duration_seconds", "probe_http_status_code",
    "scrape_samples_scraped", "scrape_body_size_bytes",
)
ALLOY_SELF_METRICS = ("up", "scrape_samples_scraped", "scrape_body_size_bytes")
SHARED_SCRAPE_LIMIT_FIELDS = (
    "body_size_limit_bytes", "sample_limit", "label_limit",
    "label_name_length_limit", "label_value_length_limit",
)
AVAILABILITY_PROCESS_BYTES = 4096
AVAILABILITY_INSPECT_FORMAT = (
    '{{.Id}}\t{{.Image}}\t{{.State.Running}}\t{{.State.Pid}}\t{{.State.StartedAt}}\t'
    '{{.HostConfig.NetworkMode}}\t{{json .HostConfig.PortBindings}}\t'
    '{{index .Config.Labels "com.docker.compose.project"}}\t'
    '{{index .Config.Labels "com.docker.compose.service"}}\t{{.Name}}'
)
AVAILABILITY_PROVIDER_OVERRIDE_NAMES = {
    "COMPOSE_FILE", "COMPOSE_PROJECT_NAME", "CONTAINER_CONNECTION", "CONTAINER_HOST",
    "DOCKER_HOST", "PODMAN_COMPOSE_PROVIDER",
}


def _availability_command(
    argv: list[str], *, timeout: float = 5, cwd: str | None = None
) -> bytes:
    """Run one bounded runtime metadata command without surfacing its output."""
    try:
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=cwd)
    except OSError:
        raise Refusal("namespace_unverified") from None
    assert process.stdout is not None
    output = bytearray()
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                if not selector.select(remaining):
                    raise TimeoutError
                chunk = os.read(process.stdout.fileno(), 64 * 1024)
                if not chunk:
                    break
                if len(output) + len(chunk) > MAX_AVAILABILITY_COMMAND_BYTES:
                    raise ValueError
                output.extend(chunk)
        returncode = process.wait(timeout=max(0, deadline - time.monotonic()))
    except (OSError, TimeoutError, ValueError, subprocess.TimeoutExpired):
        process.kill()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        raise Refusal("namespace_unverified") from None
    if returncode != 0:
        raise Refusal("namespace_unverified")
    return bytes(output)


def _availability_inspect(row: bytes) -> dict[str, Any]:
    try:
        fields = row.decode("utf-8", "strict").rstrip("\n").split("\t")
        if len(fields) != 10 or any("\x00" in field or "\n" in field for field in fields):
            raise ValueError
        container_id, image, running_raw, pid_raw, started_at, network_mode, ports_raw, project, service, name = fields
        if not re.fullmatch(r"[0-9a-f]{12,64}", container_id) or not image:
            raise ValueError
        if running_raw not in {"true", "false"} or not network_mode:
            raise ValueError
        if started_at in {"", "<no value>", "<nil>"}:
            started_at = ""
        if pid_raw in {"", "<no value>", "<nil>"}:
            pid = 0
        elif re.fullmatch(r"[0-9]{1,10}", pid_raw):
            pid = int(pid_raw)
        else:
            raise ValueError
        ports = json.loads(ports_raw) if ports_raw not in {"", "<no value>", "<nil>"} else {}
        if ports is None:
            ports = {}
        if not isinstance(ports, dict):
            raise ValueError
        for bindings in ports.values():
            if bindings is not None and (
                not isinstance(bindings, list)
                or any(
                    not isinstance(binding, dict)
                    or set(binding) - {"HostIp", "HostPort"}
                    or not isinstance(binding.get("HostPort"), str)
                    for binding in bindings
                )
            ):
                raise ValueError
        if project in {"<no value>", "<nil>"}:
            project = ""
        if service in {"<no value>", "<nil>"}:
            service = ""
        if name in {"<no value>", "<nil>"}:
            name = ""
        if any(len(value.encode("utf-8")) > 256 for value in (project, service, name, started_at, network_mode)):
            raise ValueError
        if name.startswith("/"):
            name = name[1:]
        return {
            "id": container_id,
            "image": image,
            "running": running_raw == "true",
            "pid": pid,
            "started_at": started_at,
            "network_mode": network_mode,
            "ports": ports,
            "project": project,
            "service": service,
            "name": name,
        }
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        raise Refusal("namespace_unverified") from None


def _availability_process_identity(pid: int) -> dict[str, Any]:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        raise Refusal("namespace_unverified")
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            stat_bytes = handle.read(8193)
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            command_bytes = handle.read(AVAILABILITY_PROCESS_BYTES + 1)
        namespace = os.stat(f"/proc/{pid}/ns/net", follow_symlinks=True)
        if len(stat_bytes) > 8192 or len(command_bytes) > AVAILABILITY_PROCESS_BYTES:
            raise ValueError
        right_paren = stat_bytes.rfind(b")")
        if right_paren < 0:
            raise ValueError
        stat_fields = stat_bytes[right_paren + 2 :].split()
        # /proc/PID/stat starts at field 3 after the comm field; starttime is field 22.
        if len(stat_fields) <= 19 or not re.fullmatch(rb"[0-9]+", stat_fields[19]):
            raise ValueError
        if not command_bytes or not command_bytes.endswith(b"\x00"):
            raise ValueError
        return {
            "pid": pid,
            "proc_start_ticks": int(stat_fields[19]),
            "cmdline_sha256": hashlib.sha256(command_bytes).hexdigest(),
            "netns_dev": int(namespace.st_dev),
            "netns_ino": int(namespace.st_ino),
        }
    except (OSError, ValueError, OverflowError):
        raise Refusal("namespace_unverified") from None


def _read_bounded_regular_file(path: str, *, proc_root_file: bool = False) -> bytes:
    flags = os.O_RDONLY | (0 if proc_root_file else getattr(os, "O_NOFOLLOW", 0))
    try:
        descriptor = os.open(path, flags)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_AVAILABILITY_CONFIG_BYTES:
                raise ValueError
            chunks = bytearray()
            while True:
                block = os.read(descriptor, 64 * 1024)
                if not block:
                    break
                if len(chunks) + len(block) > MAX_AVAILABILITY_CONFIG_BYTES:
                    raise ValueError
                chunks.extend(block)
            after = os.fstat(descriptor)
            if (
                metadata.st_dev != after.st_dev or metadata.st_ino != after.st_ino
                or metadata.st_size != after.st_size or len(chunks) != metadata.st_size
            ):
                raise ValueError
            return bytes(chunks)
        finally:
            os.close(descriptor)
    except (OSError, ValueError):
        raise Refusal("native_source_unavailable") from None


def _verify_loaded_candidate_config(
    record: dict[str, Any], process: dict[str, Any], candidate_sha256: str,
    *, availability_enabled: bool, lower_bound: int | None = None,
) -> str:
    """Bind actual Alloy argv, read-only config mount and bytes to this candidate."""
    if not re.fullmatch(r"[0-9a-f]{64}", candidate_sha256):
        raise Refusal("native_source_unavailable")
    pid = record.get("pid")
    started_at = _parse_native_time(record.get("started_at"))
    if lower_bound is not None and (started_at <= lower_bound):
        raise Refusal("native_source_unavailable")
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            command_line = handle.read(AVAILABILITY_PROCESS_BYTES + 1)
        if (
            len(command_line) > AVAILABILITY_PROCESS_BYTES or not command_line.endswith(b"\x00")
            or process.get("pid") != pid
            or hashlib.sha256(command_line).hexdigest() != process.get("cmdline_sha256")
        ):
            raise ValueError
        argv = [piece.decode("utf-8", "strict") for piece in command_line[:-1].split(b"\x00")]
    except (OSError, UnicodeError, ValueError):
        raise Refusal("native_source_unavailable") from None
    listener = "127.0.0.1:12345" if availability_enabled else "0.0.0.0:12345"
    if (
        len(argv) != 4 or os.path.basename(argv[0]) != "alloy"
        or argv[1:] != ["run", f"--server.http.listen-addr={listener}", "/etc/alloy/config.alloy"]
    ):
        raise Refusal("native_source_unavailable")

    deployment_dir = os.environ.get("O11Y_DEPLOYMENT_DIR", "")
    if not deployment_dir or not os.path.isabs(deployment_dir) or not os.path.isdir(deployment_dir):
        raise Refusal("native_source_unavailable")
    try:
        (
            compose_cmd, overlays, provider_version, podman_version, stable_overlays, identities,
        ) = _availability_provider_context()
        project = _verify_availability_effective_compose(deployment_dir, compose_cmd, overlays)
        podman_path = identities["podman"]["path"]
    except Exception:
        raise Refusal("native_source_unavailable") from None
    if project != record.get("project"):
        raise Refusal("native_source_unavailable")
    try:
        actual_image_name = _availability_command(
            [podman_path, "inspect", "--format", "{{.ImageName}}", record["id"]], timeout=5,
        ).decode("utf-8", "strict").strip()
    except (Refusal, UnicodeError):
        raise Refusal("native_source_unavailable") from None
    if actual_image_name != "docker.io/grafana/alloy:v1.5.1":
        raise Refusal("native_source_unavailable")
    config_path = os.path.join(deployment_dir, "config", "config.alloy")
    config_bytes = _read_bounded_regular_file(config_path)
    if hashlib.sha256(config_bytes).hexdigest() != candidate_sha256:
        raise Refusal("native_source_unavailable")
    actual_config = _read_bounded_regular_file(
        f"/proc/{pid}/root/etc/alloy/config.alloy", proc_root_file=True,
    )
    if actual_config != config_bytes:
        raise Refusal("native_source_unavailable")

    try:
        config_text = config_bytes.decode("utf-8", "strict")
    except UnicodeError:
        raise Refusal("native_source_unavailable") from None
    component_blocks = re.findall(
        r'(?m)^\s*([A-Za-z][A-Za-z0-9_.]*)\s+"([A-Za-z0-9_.-]{1,64})"\s*\{', config_text,
    )
    if not component_blocks or re.search(r"(?m)^\s*(?:import|remotecfg|module)\b", config_text):
        raise Refusal("native_source_unavailable")
    component_ids: set[str] = set()
    for name, label in component_blocks:
        component_id = f"{name}.{label}"
        if name not in ALLOY_ROOT_COMPONENT_NAMES or component_id in component_ids:
            raise Refusal("native_source_unavailable")
        component_ids.add(component_id)
    if not availability_enabled and any(
        component_id == "prometheus.scrape.alloy_self"
        or component_id.startswith((
            "prometheus.exporter.blackbox.", "prometheus.scrape.health_",
            "prometheus.relabel.availability_health_",
        ))
        for component_id in component_ids
    ):
        raise Refusal("native_source_unavailable")

    mount_bytes = _availability_command(
        [podman_path, "inspect", "--format", "{{json .Mounts}}", record["id"]], timeout=5,
    )
    try:
        mounts = json.loads(mount_bytes)
    except (ValueError, TypeError, json.JSONDecodeError):
        raise Refusal("native_source_unavailable") from None
    if not isinstance(mounts, list) or len(mounts) > 32:
        raise Refusal("native_source_unavailable")
    target_mounts = [
        mount for mount in mounts
        if isinstance(mount, dict) and mount.get("Destination") == "/etc/alloy/config.alloy"
    ]
    if (
        len(target_mounts) != 1
        or target_mounts[0].get("Type") != "bind"
        or target_mounts[0].get("Source") != config_path
        or target_mounts[0].get("RW") is not False
        or any(
            isinstance(mount, dict) and isinstance(mount.get("Destination"), str)
            and mount.get("Destination") != "/etc/alloy/config.alloy"
            and mount.get("Destination").startswith("/etc/alloy/")
            for mount in mounts
        )
    ):
        raise Refusal("native_source_unavailable")
    provider_binding = hashlib.sha256(json.dumps({
        "compose_cmd": compose_cmd,
        "overlays": overlays,
        "stable_overlays": stable_overlays,
        "provider_version": provider_version,
        "podman_version": podman_version,
        "identities": identities,
        "project": project,
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return hashlib.sha256(f"{candidate_sha256}:{provider_binding}".encode("ascii")).hexdigest()


def _safe_network_mode(value: str, project: str) -> bool:
    if value in {"host", "none", "bridge", "slirp4netns", "pasta", "private"}:
        return True
    # A project-prefixed Compose network name is ordinary; unknown names and
    # namespace-joining modes stay unverified.
    return bool(
        project
        and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", value)
        and (value == project or value.startswith((f"{project}_", f"{project}-")))
    )


def validate_availability_namespace_snapshot(
    containers: list[dict[str, Any]],
    process_identities: dict[str, dict[str, Any]],
    host_namespace: tuple[int, int],
) -> dict[str, str]:
    """Validate actual bounded inspect and kernel namespace observations."""
    if not isinstance(containers, list) or not 1 <= len(containers) <= MAX_AVAILABILITY_CONTAINERS:
        raise Refusal("namespace_unverified")
    if (
        not isinstance(host_namespace, tuple)
        or len(host_namespace) != 2
        or any(not isinstance(part, int) or isinstance(part, bool) for part in host_namespace)
        or host_namespace[0] < 0
        or host_namespace[1] <= 0
    ):
        raise Refusal("namespace_unverified")
    ids: set[str] = set()
    for container in containers:
        if (
            not isinstance(container, dict)
            or not isinstance(container.get("id"), str)
            or not re.fullmatch(r"[0-9a-f]{12,64}", container["id"])
            or not isinstance(container.get("image"), str)
            or not container["image"]
            or not isinstance(container.get("running"), bool)
            or not isinstance(container.get("pid"), int)
            or isinstance(container.get("pid"), bool)
            or container["pid"] < 0
            or not isinstance(container.get("started_at"), str)
            or not isinstance(container.get("network_mode"), str)
            or not isinstance(container.get("project"), str)
            or not isinstance(container.get("service"), str)
            or not isinstance(container.get("name"), str)
            or not isinstance(container.get("ports"), dict)
        ):
            raise Refusal("namespace_unverified")
        if container["id"] in ids:
            raise Refusal("namespace_unverified")
        ids.add(container["id"])
    required = _availability_related(containers)
    alloy = required[0]
    project = alloy["project"]
    if (
        alloy.get("running") is not True
        or not isinstance(alloy.get("started_at"), str)
        or not alloy["started_at"]
        or not isinstance(alloy.get("network_mode"), str)
        or not _safe_network_mode(alloy["network_mode"], project)
        or alloy["network_mode"] in {"host", "none"}
        or ":" in alloy["network_mode"]
        or not isinstance(alloy.get("ports"), dict)
    ):
        raise Refusal("namespace_unverified")
    host_port_12345 = any(
        binding.get("HostPort") == "12345"
        for bindings in alloy["ports"].values()
        if isinstance(bindings, list)
        for binding in bindings
        if isinstance(binding, dict)
    )
    if host_port_12345:
        raise Refusal("namespace_unverified")
    identities: dict[str, dict[str, Any]] = {}
    for container in required:
        mode = container.get("network_mode")
        if (
            not isinstance(mode, str)
            or not _safe_network_mode(mode, project)
            or mode in {"host", "none"}
            or mode.startswith(("container:", "service:", "ns:"))
        ):
            raise Refusal("namespace_unverified")
        if container.get("running") is not True:
            # A stopped project sibling can join on the next apply, so inspect its
            # declared mode but do not invent a current kernel namespace for it.
            continue
        if not container.get("started_at"):
            raise Refusal("namespace_unverified")
        process = process_identities.get(container["id"])
        if (
            not isinstance(process, dict)
            or process.get("pid") != container.get("pid")
            or not isinstance(process.get("proc_start_ticks"), int)
            or isinstance(process.get("proc_start_ticks"), bool)
            or process["proc_start_ticks"] <= 0
            or not isinstance(process.get("netns_dev"), int)
            or isinstance(process.get("netns_dev"), bool)
            or process["netns_dev"] < 0
            or not isinstance(process.get("netns_ino"), int)
            or isinstance(process.get("netns_ino"), bool)
            or process["netns_ino"] <= 0
            or not isinstance(process.get("pid"), int)
            or isinstance(process.get("pid"), bool)
            or process["pid"] <= 1
            or not isinstance(process.get("cmdline_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", process["cmdline_sha256"])
        ):
            raise Refusal("namespace_unverified")
        identities[container["id"]] = process
    alloy_identity = identities.get(alloy["id"])
    if alloy_identity is None:
        raise Refusal("namespace_unverified")
    alloy_ns = (alloy_identity["netns_dev"], alloy_identity["netns_ino"])
    if alloy_ns == host_namespace or any(
        (item["netns_dev"], item["netns_ino"]) == alloy_ns
        for container_id, item in identities.items()
        if container_id != alloy["id"]
    ):
        raise Refusal("namespace_unverified")
    return {
        "status": "isolated",
        "reason": "distinct_kernel_namespaces",
        "container_count": str(len(required)),
    }


def _availability_executable_identity(name: str) -> dict[str, Any]:
    path = shutil.which(name)
    if not path:
        raise Refusal("namespace_unverified")
    resolved = os.path.realpath(path)
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(resolved, flags)
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode) or not os.access(resolved, os.X_OK):
                raise Refusal("namespace_unverified")
            if metadata.st_size <= 0 or metadata.st_size > 64 * 1024 * 1024:
                raise Refusal("namespace_unverified")
            digest = hashlib.sha256()
            size = 0
            while True:
                block = os.read(fd, 64 * 1024)
                if not block:
                    break
                size += len(block)
                if size > metadata.st_size or size > 64 * 1024 * 1024:
                    raise Refusal("namespace_unverified")
                digest.update(block)
            if size != metadata.st_size:
                raise Refusal("namespace_unverified")
        finally:
            os.close(fd)
    except OSError:
        raise Refusal("namespace_unverified") from None
    return {
        "path": resolved, "device": metadata.st_dev, "inode": metadata.st_ino,
        "size": metadata.st_size, "sha256": digest.hexdigest(),
    }


def _availability_provider_context() -> tuple[list[str], list[str], str, str, list[str], dict[str, Any]]:
    if platform.system() != "Linux" or os.environ.get("CONTAINER_ENGINE") != "podman":
        raise Refusal("namespace_unverified")
    if any(os.environ.get(name) for name in AVAILABILITY_PROVIDER_OVERRIDE_NAMES):
        raise Refusal("namespace_unverified")
    if any(
        name.startswith("COMPOSE_")
        and name not in {"COMPOSE_OVERLAYS", "COMPOSE_CMD"}
        and value
        for name, value in os.environ.items()
    ):
        raise Refusal("namespace_unverified")
    if any(name.startswith("PODMAN_COMPOSE_") and value for name, value in os.environ.items()):
        raise Refusal("namespace_unverified")
    for name in ("CONTAINERS_CONF", "CONTAINERS_CONF_OVERRIDE", "PODMAN_CONNECTION", "PODMAN_HOST"):
        if os.environ.get(name):
            raise Refusal("namespace_unverified")
    deployment_dir = os.environ.get("O11Y_DEPLOYMENT_DIR", "")
    if not deployment_dir or not os.path.isabs(deployment_dir) or not os.path.isdir(deployment_dir):
        raise Refusal("namespace_unverified")
    overlays = os.environ.get("COMPOSE_OVERLAYS", "").split()
    allowed_overlays = {
        "capacity/compose.textfile.yml", "compose.prod.yml", "compose.otlp-tls.yml",
        "compose.availability.yml", "compose.availability-ca.yml",
    }
    final_start = os.environ.get("O11Y_AVAILABILITY_FINAL_START", "")
    if final_start not in {"", "true"}:
        raise Refusal("namespace_unverified")
    temporary_overlays = []
    for overlay in overlays:
        if overlay in allowed_overlays:
            continue
        if final_start == "true" and _validate_compose_inputs_overlay_path(overlay):
            temporary_overlays.append(overlay)
            continue
        raise Refusal("namespace_unverified")
    if (
        len(overlays) != len(set(overlays))
        or len(temporary_overlays) != (1 if final_start == "true" else 0)
    ):
        raise Refusal("namespace_unverified")
    if os.environ.get("LOCAL_MODE") != "true" and "compose.prod.yml" not in overlays:
        overlays.insert(0, "compose.prod.yml")
    availability_enabled = os.environ.get("O11Y_AVAILABILITY_ENABLED")
    if availability_enabled not in {"true", "false"}:
        raise Refusal("namespace_unverified")
    if availability_enabled == "true" and "compose.availability.yml" not in overlays:
        raise Refusal("namespace_unverified")
    compose_cmd_value = os.environ.get("COMPOSE_CMD", "")
    if compose_cmd_value:
        try:
            compose_cmd = shlex.split(compose_cmd_value)
        except ValueError:
            raise Refusal("namespace_unverified") from None
    else:
        compose_cmd = ["podman-compose"] if shutil.which("podman-compose") else ["podman", "compose"]
    # The wrapper does not expose a stable, local way to prove which external provider
    # it selected. Availability therefore supports only the directly resolved provider.
    provider_identity = _availability_executable_identity("podman-compose")
    podman_identity = _availability_executable_identity("podman")
    resolved_provider = provider_identity["path"]
    if compose_cmd not in (["podman-compose"], [resolved_provider]):
        raise Refusal("namespace_unverified")
    compose_cmd = [resolved_provider]
    version_argv = [resolved_provider, "--version"]
    version_raw = _availability_command(version_argv, timeout=10)
    podman_version_raw = _availability_command([podman_identity["path"], "--version"], timeout=10)
    try:
        version_lines = version_raw.decode("utf-8", "strict").splitlines()
        podman_version = podman_version_raw.decode("ascii", "strict").strip()
    except UnicodeError:
        raise Refusal("namespace_unverified") from None
    if re.fullmatch(r"podman version [0-9]+\.[0-9]+\.[0-9]+", podman_version) is None:
        raise Refusal("namespace_unverified")
    provider_banners = {
        "podman-compose version 1.0.6", "podman-compose version 1.3.0", "podman-compose version 1.6.0",
    }
    if len(version_lines) != 2 or version_lines.count(podman_version) != 1:
        raise Refusal("namespace_unverified")
    provider_banner = next((line for line in version_lines if line in provider_banners), None)
    if provider_banner is None or len(set(version_lines)) != 2:
        raise Refusal("namespace_unverified")
    provider_version = provider_banner.rsplit(" ", 1)[-1]
    effective_overlays = (["compose.local.yml"] if os.environ.get("LOCAL_MODE") == "true" else []) + overlays
    stable_overlays = [overlay for overlay in effective_overlays if overlay not in temporary_overlays]
    return compose_cmd, effective_overlays, provider_version, podman_version, stable_overlays, {
        "compose": provider_identity, "podman": podman_identity,
    }


def _validate_compose_inputs_overlay_path(path: str) -> bool:
    if not isinstance(path, str) or not os.path.isabs(path) or os.path.basename(path) != "inputs-label.yml":
        return False
    temp_root = os.path.realpath(tempfile.gettempdir())
    parent = os.path.dirname(path)
    try:
        if os.path.commonpath((temp_root, parent)) != temp_root or os.path.dirname(parent) != temp_root:
            return False
        parent_stat = os.lstat(parent)
        metadata = os.lstat(path)
        if (
            not stat.S_ISDIR(parent_stat.st_mode) or stat.S_ISLNK(parent_stat.st_mode)
            or parent_stat.st_uid != os.getuid() or stat.S_IMODE(parent_stat.st_mode) & 0o077
            or not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 16_384
        ):
            return False
        with open(path, "rb") as handle:
            raw = handle.read(16_385)
        if len(raw) != metadata.st_size or len(raw) > 16_384:
            return False
        document = yaml.load(raw.decode("utf-8", "strict"), Loader=_SourceUniqueKeyLoader)
        services = document.get("services") if isinstance(document, dict) and set(document) == {"services"} else None
        if not isinstance(services, dict) or not 1 <= len(services) <= 32:
            return False
        return all(
            isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name)
            and isinstance(service, dict) and set(service) == {"labels"}
            and isinstance(service["labels"], dict)
            and set(service["labels"]) == {"io.agent-cloud.inputs-sha256"}
            and isinstance(service["labels"]["io.agent-cloud.inputs-sha256"], str)
            and re.fullmatch(r"[0-9a-f]{64}", service["labels"]["io.agent-cloud.inputs-sha256"])
            for name, service in services.items()
        )
    except (OSError, ValueError, TypeError, UnicodeError, yaml.YAMLError):
        return False


def _availability_inspect_all(deadline: float, podman_path: str) -> list[dict[str, Any]]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise Refusal("namespace_unverified")
    ids_raw = _availability_command(
        [podman_path, "ps", "-a", "--no-trunc", "--format", "{{.ID}}"],
        timeout=min(5, remaining),
    )
    try:
        ids = ids_raw.decode("ascii", "strict").splitlines()
    except UnicodeError:
        raise Refusal("namespace_unverified") from None
    if not 1 <= len(ids) <= MAX_AVAILABILITY_CONTAINERS or any(
        not re.fullmatch(r"[0-9a-f]{12,64}", container_id) for container_id in ids
    ) or len(ids) != len(set(ids)):
        raise Refusal("namespace_unverified")
    records = []
    for container_id in ids:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Refusal("namespace_unverified")
        row = _availability_command(
            [podman_path, "inspect", "--format", AVAILABILITY_INSPECT_FORMAT, container_id],
            timeout=min(5, remaining),
        )
        record = _availability_inspect(row)
        if record["id"] != container_id:
            raise Refusal("namespace_unverified")
        records.append(record)
    return records


def _availability_related(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    alloy = [record for record in records if record.get("service") == "alloy" and record.get("project")]
    if len(alloy) != 1:
        raise Refusal("namespace_unverified")
    target = alloy[0]
    target_id = target["id"]
    project = target["project"]
    related = [target]
    related_ids = {target_id}
    # Include the project and transitive container-namespace joiners. Resolve any
    # nonempty ID prefix against the complete bounded container list; ambiguous or
    # unresolved references are included so validation refuses instead of omitting
    # a possible namespace joiner. All ns: modes are similarly fail-closed.
    changed = True
    while changed:
        changed = False
        for record in records:
            if record in related:
                continue
            mode = record.get("network_mode", "")
            joins_related = record.get("project") == project or mode.startswith("ns:")
            if mode.startswith("container:"):
                reference = mode.partition(":")[2]
                matching_ids = [
                    row["id"] for row in records
                    if isinstance(reference, str) and reference
                    and row.get("id", "").startswith(reference)
                ]
                matching_names = [row for row in records if row.get("name") == reference]
                if len(matching_ids) + len(matching_names) == 1:
                    resolved_id = matching_ids[0] if matching_ids else matching_names[0]["id"]
                    joins_related = joins_related or resolved_id in related_ids
                else:
                    joins_related = True
            elif mode.startswith("service:"):
                reference = mode.partition(":")[2]
                matching_services = [row for row in records if row.get("service") == reference]
                if len(matching_services) == 1:
                    joins_related = joins_related or matching_services[0]["id"] in related_ids
                else:
                    joins_related = True
            if joins_related:
                related.append(record)
                related_ids.add(record["id"])
                changed = True
    if len({record["id"] for record in related}) != len(related):
        raise Refusal("namespace_unverified")
    return related


def _availability_runtime_pass(podman_path: str) -> tuple[
    list[dict[str, Any]], dict[str, dict[str, Any]], tuple[int, int]
]:
    if not isinstance(podman_path, str) or not os.path.isabs(podman_path):
        raise Refusal("namespace_unverified")
    records = _availability_inspect_all(
        time.monotonic() + MAX_AVAILABILITY_INSPECT_SECONDS, podman_path,
    )
    host_ns = os.stat("/proc/1/ns/net", follow_symlinks=True)
    host_namespace = (int(host_ns.st_dev), int(host_ns.st_ino))
    related = _availability_related(records)
    if any(
        binding.get("HostPort") == "12345"
        for record in records
        for bindings in record.get("ports", {}).values()
        if isinstance(bindings, list)
        for binding in bindings
        if isinstance(binding, dict)
    ):
        raise Refusal("namespace_unverified")
    process_identities = {
        record["id"]: _availability_process_identity(record["pid"])
        for record in related if record.get("running") is True
    }
    return related, process_identities, host_namespace


def _availability_namespace_fingerprint(
    records: list[dict[str, Any]],
    processes: dict[str, dict[str, Any]],
    host_namespace: tuple[int, int],
) -> str:
    payload = {
        "containers": [
            {
                key: record[key]
                for key in (
                    "id", "image", "running", "pid", "started_at", "network_mode",
                    "ports", "project", "service", "name",
                )
            }
            for record in sorted(records, key=lambda row: row["id"])
        ],
        "processes": {key: processes[key] for key in sorted(processes)},
        "host_namespace": host_namespace,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _verify_availability_effective_compose(
    deployment_dir: str, compose_cmd: list[str], overlays: list[str],
) -> str:
    compose_files = ["-f", "compose.yml"]
    if os.environ.get("LOCAL_MODE") == "true" and os.path.isfile(os.path.join(deployment_dir, "compose.local.yml")):
        compose_files.extend(["-f", "compose.local.yml"])
    final_label_overlays = []
    for overlay in overlays:
        overlay_path = overlay if os.path.isabs(overlay) else os.path.join(deployment_dir, overlay)
        if not os.path.isfile(overlay_path):
            raise Refusal("namespace_unverified")
        if os.path.isabs(overlay):
            final_label_overlays.append(overlay)
        compose_files.extend(["-f", overlay_path])
    compose_raw = _availability_command(
        [*compose_cmd, *compose_files, "config"], timeout=10, cwd=deployment_dir
    )
    try:
        config = yaml.load(compose_raw.decode("utf-8", "strict"), Loader=_SourceUniqueKeyLoader)
    except (UnicodeError, yaml.YAMLError):
        raise Refusal("namespace_unverified") from None
    services = config.get("services") if isinstance(config, dict) else None
    if not isinstance(services, dict) or not 1 <= len(services) <= 32:
        raise Refusal("namespace_unverified")
    if len(final_label_overlays) > 1:
        raise Refusal("namespace_unverified")
    if final_label_overlays:
        path = final_label_overlays[0]
        if not _validate_compose_inputs_overlay_path(path):
            raise Refusal("namespace_unverified")
        try:
            with open(path, encoding="utf-8") as handle:
                label_overlay = yaml.load(handle, Loader=_SourceUniqueKeyLoader)
        except (OSError, UnicodeError, yaml.YAMLError):
            raise Refusal("namespace_unverified") from None
        label_services = label_overlay.get("services") if isinstance(label_overlay, dict) else None
        if not isinstance(label_services, dict) or set(label_services) != set(services):
            raise Refusal("namespace_unverified")
        for service_name, expected in label_services.items():
            effective_labels = services[service_name].get("labels")
            input_digest = expected["labels"]["io.agent-cloud.inputs-sha256"]
            if (
                not isinstance(effective_labels, dict)
                or effective_labels.get("io.agent-cloud.inputs-sha256") != input_digest
            ):
                raise Refusal("namespace_unverified")

    alloy_service = services.get("alloy")
    if not isinstance(alloy_service, dict):
        raise Refusal("namespace_unverified")
    expected_listener = (
        "127.0.0.1:12345" if os.environ.get("O11Y_AVAILABILITY_ENABLED") == "true" else "0.0.0.0:12345"
    )
    effective_command = alloy_service.get("command")
    expected_mount_source = os.path.realpath(os.path.join(deployment_dir, "config", "config.alloy"))
    volumes = alloy_service.get("volumes")
    if (
        os.environ.get("O11Y_AVAILABILITY_ENABLED") == "true" and config.get("x-podman") != {"in_pod": False}
    ) or (
        alloy_service.get("entrypoint") is not None
        or effective_command != ["run", f"--server.http.listen-addr={expected_listener}", "/etc/alloy/config.alloy"]
        or not isinstance(effective_command, list)
        or "ports" in alloy_service
        or alloy_service.get("network_mode") is not None
        or alloy_service.get("image") != "docker.io/grafana/alloy:v1.5.1"
        or not isinstance(volumes, list)
    ):
        raise Refusal("namespace_unverified")
    config_mounts = [
        mount for mount in volumes
        if isinstance(mount, dict) and mount.get("target") == "/etc/alloy/config.alloy"
    ]
    if (
        len(config_mounts) != 1
        or config_mounts[0].get("type") != "bind"
        or os.path.realpath(config_mounts[0].get("source", "")) != expected_mount_source
        or config_mounts[0].get("read_only") is not True
        or any(
            isinstance(mount, dict) and isinstance(mount.get("target"), str)
            and mount.get("target") != "/etc/alloy/config.alloy"
            and mount.get("target").startswith("/etc/alloy/")
            for mount in volumes
        )
    ):
        raise Refusal("namespace_unverified")
    for service in services.values():
        if not isinstance(service, dict) or service.get("network_mode") is not None:
            raise Refusal("namespace_unverified")
        ports = service.get("ports", [])
        if ports is None:
            ports = []
        if not isinstance(ports, list):
            raise Refusal("namespace_unverified")
        for port in ports:
            if isinstance(port, dict):
                if str(port.get("published")) == "12345":
                    raise Refusal("namespace_unverified")
            elif isinstance(port, (str, int)) and re.search(r"(?:^|:)12345(?::|/|$)", str(port)):
                raise Refusal("namespace_unverified")
            else:
                raise Refusal("namespace_unverified")
    project = config.get("name")
    if not isinstance(project, str) or not project:
        raise Refusal("namespace_unverified")
    return project


def verify_availability_namespace(*, validate_candidate: bool = False) -> dict[str, str]:
    """Prove current Alloy and project siblings use distinct Linux netns."""
    (
        compose_cmd,
        overlays,
        provider_version,
        podman_version,
        stable_overlays,
        executable_identity,
    ) = _availability_provider_context()
    candidate_project = None
    if validate_candidate:
        candidate_project = _verify_availability_effective_compose(
            os.environ["O11Y_DEPLOYMENT_DIR"], compose_cmd, overlays,
        )
    podman_path = executable_identity["podman"]["path"]
    first = _availability_runtime_pass(podman_path)
    second = _availability_runtime_pass(podman_path)
    if (
        _availability_executable_identity("podman") != executable_identity["podman"]
        or _availability_executable_identity("podman-compose") != executable_identity["compose"]
    ):
        raise Refusal("namespace_unverified")
    if first != second:
        raise Refusal("namespace_unverified")
    records, process_identities, host_namespace = second
    result = validate_availability_namespace_snapshot(records, process_identities, host_namespace)
    if validate_candidate:
        running_alloy = [record for record in records if record.get("service") == "alloy"]
        if len(running_alloy) != 1 or candidate_project != running_alloy[0].get("project"):
            raise Refusal("namespace_unverified")
    namespace_fingerprint = _availability_namespace_fingerprint(records, process_identities, host_namespace)
    expected_namespace = os.environ.get("O11Y_EXPECTED_NAMESPACE_SNAPSHOT_SHA256", "")
    if expected_namespace and (
        not re.fullmatch(r"[0-9a-f]{64}", expected_namespace)
        or expected_namespace != namespace_fingerprint
    ):
        raise Refusal("namespace_unverified")
    provider_payload = {
        "engine": "podman",
        "compose_command": compose_cmd,
        "provider_version": provider_version,
        "podman_version": podman_version,
        "executables": executable_identity,
        "local_mode": os.environ.get("LOCAL_MODE") == "true",
        "overlays": stable_overlays,
    }
    provider_fingerprint = hashlib.sha256(
        json.dumps(provider_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    expected_provider = os.environ.get("O11Y_EXPECTED_RUNTIME_CONTEXT_SHA256", "")
    if expected_provider and (
        not re.fullmatch(r"[0-9a-f]{64}", expected_provider)
        or expected_provider != provider_fingerprint
    ):
        raise Refusal("namespace_unverified")
    result["namespace_snapshot_sha256"] = namespace_fingerprint
    result["runtime_context_sha256"] = provider_fingerprint
    if not validate_candidate:
        result["provider"] = "podman-compose"
        result["provider_version"] = provider_version
        return result
    if os.environ.get("O11Y_AVAILABILITY_ENABLED") == "true" and "compose.availability.yml" not in overlays:
        raise Refusal("namespace_unverified")
    result["provider"] = "podman-compose"
    result["provider_version"] = provider_version
    return result


class _SourceUniqueKeyLoader(yaml.SafeLoader):
    """Safe, bounded YAML loader for the two generated scrape fragments."""

    def __init__(self, stream: Any) -> None:
        super().__init__(stream)
        self._collection_depth = 0

    def compose_node(self, parent: Any, index: Any) -> yaml.Node:
        if self.check_event(yaml.AliasEvent):
            raise yaml.YAMLError("aliases are refused")
        if not self.check_event(yaml.MappingStartEvent, yaml.SequenceStartEvent):
            return super().compose_node(parent, index)
        self._collection_depth += 1
        try:
            if self._collection_depth > 64:
                raise yaml.YAMLError("nesting limit exceeded")
            return super().compose_node(parent, index)
        finally:
            self._collection_depth -= 1


def _source_unique_mapping(
    loader: _SourceUniqueKeyLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ValueError("duplicate YAML key")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_SourceUniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _source_unique_mapping)


def _open_source_directory(path: str) -> int:
    """Open the exact expected directory chain without following any symlink."""
    if not isinstance(path, str) or not path.startswith("/") or "\x00" in path:
        raise Refusal("source_path_refused")
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise Refusal("source_path_refused")
    parts = path.split("/")[1:]
    suffix = tuple(parts[-len(SOURCE_CONFIG_SUFFIX):])
    if len(parts) < len(SOURCE_CONFIG_SUFFIX) or suffix != SOURCE_CONFIG_SUFFIX:
        raise Refusal("source_path_refused")
    if any(part in {"", ".", ".."} for part in parts):
        raise Refusal("source_path_refused")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open("/", flags)
    try:
        for part in parts:
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise Refusal("source_path_refused")
        return fd
    except Exception:
        os.close(fd)
        raise


def _read_source_file(directory_fd: int, filename: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(filename, flags, dir_fd=directory_fd)
    except OSError:
        raise Refusal("source_file_unavailable") from None
    try:
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_mode & 0o022
            or not 0 < metadata.st_size <= MAX_SOURCE_CONFIG_BYTES
        ):
            raise Refusal("source_file_refused")
        chunks = bytearray()
        while len(chunks) <= MAX_SOURCE_CONFIG_BYTES:
            chunk = os.read(fd, min(65536, MAX_SOURCE_CONFIG_BYTES + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
        if len(chunks) > MAX_SOURCE_CONFIG_BYTES or len(chunks) != metadata.st_size:
            raise Refusal("source_file_refused")
        after = os.fstat(fd)
        before_signature = (
            metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns
        )
        after_signature = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        if before_signature != after_signature:
            raise Refusal("source_file_refused")
        return bytes(chunks)
    finally:
        os.close(fd)


def _source_scrape_limit_readback(directory: str) -> dict[str, Any]:
    """Read only the two generated fragments and retain only job limits privately."""
    directory_fd = _open_source_directory(directory)
    jobs: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        for filename, allowed_jobs in SOURCE_CONFIG_FILES.items():
            raw = _read_source_file(directory_fd, filename)
            try:
                documents = list(yaml.load_all(raw.decode("utf-8"), Loader=_SourceUniqueKeyLoader))
            except (UnicodeError, yaml.YAMLError, ValueError, TypeError):
                raise Refusal("source_yaml_refused") from None
            if len(documents) != 1 or not isinstance(documents[0], dict) or set(documents[0]) != {"scrape_configs"}:
                raise Refusal("source_yaml_refused")
            configured = documents[0]["scrape_configs"]
            if not isinstance(configured, list) or not configured:
                raise Refusal("source_yaml_refused")
            for item in configured:
                if not isinstance(item, dict):
                    raise Refusal("source_yaml_refused")
                job_name = item.get("job_name")
                if not isinstance(job_name, str) or job_name not in allowed_jobs or job_name in seen:
                    raise Refusal("source_job_refused")
                seen.add(job_name)
                limits = {field: item.get(field) for field in SOURCE_LIMIT_FIELDS}
                if any(type(value) not in (str, int) for value in limits.values()):
                    raise Refusal("source_limit_refused")
                jobs.append({"job_name": job_name, "limits": limits})
    finally:
        os.close(directory_fd)
    if "agentgateway" not in seen or not {"dgx-spark-node", "dgx-spark-vllm"}.issubset(seen):
        raise Refusal("source_job_refused")
    return {"status": "observed", "reason": "source_files_read", "jobs": jobs}


def validate_expected_target_declaration(
    targets: Any,
    retired_uids: Any = None,
    *,
    reviewed: Any = True,
    other_rule_uids: Any = None,
) -> tuple[list[dict[str, str]], list[str]]:
    """Validate the bounded private declaration without exposing its values."""
    retired_uids = [] if retired_uids is None else retired_uids
    other_rule_uids = [] if other_rule_uids is None else other_rule_uids
    if reviewed is not True or not isinstance(targets, list) or not isinstance(retired_uids, list):
        raise Refusal("invalid_declaration")
    if len(targets) > MAX_EXPECTED_TARGETS or len(retired_uids) > MAX_EXPECTED_TARGETS:
        raise Refusal("invalid_declaration")
    if not targets and not retired_uids:
        raise Refusal("invalid_declaration")
    if not isinstance(other_rule_uids, list) or len(other_rule_uids) > MAX_ROWS:
        raise Refusal("invalid_declaration")

    normalized: list[dict[str, str]] = []
    identities: set[tuple[str, ...]] = set()
    target_uids: list[str] = []
    for target in targets:
        base_fields = {"uid", *EXPECTED_IDENTITY_FIELDS}
        if not isinstance(target, dict) or set(target) not in (
            base_fields, base_fields | {"source"}, base_fields | {"source", "target_id"},
        ):
            raise Refusal("invalid_declaration")
        uid = target.get("uid")
        if (
            not isinstance(uid, str) or not EXPECTED_UID.fullmatch(uid)
            or EXPECTED_GROUP_UID.fullmatch(uid)
        ):
            raise Refusal("invalid_declaration")
        values: dict[str, str] = {}
        for field in EXPECTED_IDENTITY_FIELDS:
            value = target.get(field)
            if not isinstance(value, str) or not SAFE_IDENTITY_VALUE.fullmatch(value):
                raise Refusal("invalid_declaration")
            values[field] = value
        source = target.get("source")
        if source is not None:
            if not isinstance(source, str) or source not in EXPECTED_SOURCE_VALUES:
                raise Refusal("invalid_declaration")
            values["source"] = source
        target_id = target.get("target_id")
        if source == "alloy":
            if (
                target_id != "service:o11y"
                or values["job"] != "alloy"
                or values["instance"] != "alloy:12345"
                or values["service"] != "o11y/alloy"
            ):
                raise Refusal("invalid_declaration")
            values["target_id"] = target_id
        elif target_id is not None:
            raise Refusal("invalid_declaration")
        identity = tuple(values[field] for field in EXPECTED_IDENTITY_FIELDS)
        if uid in target_uids or identity in identities:
            raise Refusal("invalid_declaration")
        target_uids.append(uid)
        identities.add(identity)
        normalized.append({"uid": uid, **values})

    if len(set(retired_uids)) != len(retired_uids):
        raise Refusal("invalid_declaration")
    if any(
        not isinstance(uid, str) or not EXPECTED_UID.fullmatch(uid) or EXPECTED_GROUP_UID.fullmatch(uid)
        for uid in retired_uids
    ):
        raise Refusal("invalid_declaration")
    if not isinstance(other_rule_uids, list) or any(
        not isinstance(uid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", uid) for uid in other_rule_uids
    ):
        raise Refusal("invalid_declaration")
    all_target_uids = set(target_uids) | set(retired_uids)
    if len(all_target_uids) != len(target_uids) + len(retired_uids):
        raise Refusal("invalid_declaration")
    if all_target_uids.intersection(other_rule_uids):
        raise Refusal("invalid_declaration")
    group_uids = [group["uid"] for group in expected_target_groups(normalized)]
    if len(set(group_uids)) != len(group_uids) or set(group_uids) & all_target_uids:
        raise Refusal("invalid_declaration")
    if set(group_uids).intersection(other_rule_uids):
        raise Refusal("invalid_declaration")
    return normalized, list(retired_uids)


def expected_target_groups(targets: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Group targets by stable routing identity and assign a deterministic rule UID."""
    grouped: dict[tuple[str, str, str], list[dict[str, str]]] = {}
    for target in targets:
        key = (target["service"], target["environment"], target["cluster"])
        grouped.setdefault(key, []).append(target)
    result = []
    for (service, environment, cluster), members in sorted(grouped.items()):
        canonical_key = json.dumps([service, environment, cluster], ensure_ascii=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical_key.encode("utf-8")).hexdigest()[:26]
        uid = f"o11y_missing_g{digest}"
        result.append({
            "uid": uid,
            "service": service,
            "environment": environment,
            "cluster": cluster,
            "targets": sorted(members, key=lambda target: (target["job"], target["instance"])),
        })
    return result


def expected_target_group_expression(group: dict[str, Any]) -> str:
    """Emit one absent series per exact expected target selector."""
    expressions = []
    for target in group["targets"]:
        matchers = ",".join(
            f"{field}={json.dumps(target[field], ensure_ascii=True)}"
            for field in ("job", "instance", "service", "cluster", "environment")
        )
        expressions.append(f"absent_over_time(up{{{matchers}}}[5m])")
    return " or ".join(expressions)


def _tombstone_title(uid: str, *, retired: bool = False) -> str:
    state = "retired" if retired else "paused"
    return f"Expected target {state} ({uid})"


def _group_tombstone_title(uid: str) -> str:
    return f"Expected target group paused ({uid})"


def _as_tombstone(row: dict[str, Any], *, retired: bool = False) -> dict[str, Any]:
    if row.get("_kind") == "group" or (
        isinstance(row.get("uid"), str) and EXPECTED_GROUP_UID.fullmatch(row["uid"])
    ):
        return {
            "uid": row["uid"], "title": _group_tombstone_title(row["uid"]),
            "summary": "Expected target group rule is paused.",
            "description": "The group UID remains owned and inert for a reviewed rollback or disable transition.",
            "labels": {"severity": "warning", "service": row["service"],
                       "environment": row["environment"], "cluster": row["cluster"]},
        }
    identity = {field: row[field] for field in EXPECTED_IDENTITY_FIELDS if row.get(field)}
    labels = {"severity": "warning", **identity,
              "environment": identity.get("environment", "disabled")}
    return {
        "uid": row["uid"], "title": _tombstone_title(row["uid"], retired=retired),
        "summary": "Expected metrics target rule is disabled.",
        "description": "This owned UID was explicitly retired and remains inert." if retired
        else ("The original target identity remains owned and inert." if identity else
              "No target identity is retained while the reviewed alert gate is disabled."),
        "labels": labels,
    }


def _group_from_rule(rule: dict[str, Any]) -> dict[str, Any]:
    """Validate the generated group identity and recover its exact selector members."""
    uid = rule.get("uid")
    labels = rule.get("labels")
    if not isinstance(uid, str) or not EXPECTED_UID.fullmatch(uid) or not isinstance(labels, dict):
        raise Refusal("invalid_previous_rules")
    service, environment, cluster = (labels.get(key) for key in ("service", "environment", "cluster"))
    if any(not isinstance(value, str) or not SAFE_IDENTITY_VALUE.fullmatch(value)
           for value in (service, environment, cluster)):
        raise Refusal("invalid_previous_rules")
    expected_group = expected_target_groups([{
        "uid": "o11y_missing_internal",
        "job": "internal", "instance": "internal:1", "service": service,
        "environment": environment, "cluster": cluster,
    }])[0]
    if uid != expected_group["uid"]:
        raise Refusal("invalid_previous_rules")
    data = rule.get("data")
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise Refusal("invalid_previous_rules")
    expression = data[0].get("model", {}).get("expr")
    if not isinstance(expression, str) or len(expression) > MAX_EXPECTED_GROUP_EXPRESSION_LENGTH:
        raise Refusal("invalid_previous_rules")
    member_rows = []
    for term in expression.split(" or "):
        match = re.fullmatch(
            r'absent_over_time\(up\{job="([A-Za-z0-9][A-Za-z0-9_.:/@-]{0,127})",'
            r'instance="([A-Za-z0-9][A-Za-z0-9_.:/@-]{0,127})",'
            r'service="([A-Za-z0-9][A-Za-z0-9_.:/@-]{0,127})",'
            r'cluster="([A-Za-z0-9][A-Za-z0-9_.:/@-]{0,127})",'
            r'environment="([A-Za-z0-9][A-Za-z0-9_.:/@-]{0,127})"\}\[5m\]\)',
            term,
        )
        if not match:
            raise Refusal("invalid_previous_rules")
        member = dict(zip(EXPECTED_IDENTITY_FIELDS, (match[1], match[2], match[3], match[4], match[5]), strict=True))
        if (member["service"], member["environment"], member["cluster"]) != (service, environment, cluster):
            raise Refusal("invalid_previous_rules")
        member_rows.append(member)
    member_identities = {tuple(row[field] for field in EXPECTED_IDENTITY_FIELDS) for row in member_rows}
    if not member_rows or len(member_identities) != len(member_rows):
        raise Refusal("invalid_previous_rules")
    if expression != " or ".join(
        f'absent_over_time(up{{job={json.dumps(row["job"])},instance={json.dumps(row["instance"])},'
        f'service={json.dumps(row["service"])},cluster={json.dumps(row["cluster"])},'
        f'environment={json.dumps(row["environment"])}}}[5m])'
        for row in member_rows
    ):
        raise Refusal("invalid_previous_rules")
    return {"uid": uid, "service": service, "environment": environment, "cluster": cluster,
            "targets": member_rows, "_expr": expression}


def _expected_target_queries_match(data: Any, expression: str) -> bool:
    """Validate the complete A -> B rule, allowing only Grafana's API metadata."""
    if not isinstance(data, list) or len(data) != 2:
        return False
    expected_rows = (
        (
            "A", "prometheus", {"from": 300 if expression == "vector(0)" else 600, "to": 0},
            {
                "editorMode": "code", "expr": expression, "instant": True, "range": False,
                "intervalMs": 1000, "maxDataPoints": 43200, "refId": "A",
            },
            "prometheus",
        ),
        (
            "B", "__expr__", {"from": 0, "to": 0},
            {
                "expression": "A", "type": "threshold",
                "conditions": [{
                    "evaluator": {"type": "gt", "params": [0.5]},
                    "operator": {"type": "and"}, "query": {"params": ["B"]},
                    "reducer": {"type": "last", "params": []}, "type": "query",
                }],
                "intervalMs": 1000, "maxDataPoints": 43200, "refId": "B",
            },
            "__expr__",
        ),
    )
    for index, (ref_id, datasource_uid, time_range, expected_model, datasource_type) in enumerate(expected_rows):
        row = data[index]
        if not isinstance(row, dict) or set(row) - {
            "refId", "datasourceUid", "relativeTimeRange", "model", "queryType",
        }:
            return False
        if (
            row.get("refId") != ref_id
            or row.get("datasourceUid") != datasource_uid
            or ("queryType" in row and row["queryType"] != "")
        ):
            return False
        actual_range = row.get("relativeTimeRange", {"from": 0, "to": 0} if ref_id == "B" else None)
        if actual_range != time_range:
            return False
        model = row.get("model")
        if not isinstance(model, dict) or set(model) - (set(expected_model) | {"datasource", "hide"}):
            return False
        if any(model.get(key) != value for key, value in expected_model.items()):
            return False
        if "hide" in model and model["hide"] is not False:
            return False
        if "datasource" in model and model["datasource"] != {
            "type": datasource_type, "uid": datasource_uid,
        }:
            return False
    return True


def _live_rule_matches_owned(rule: Any, target: dict[str, Any], folder_uid: str) -> bool:
    """Compare an API readback with one validated provisioning owner."""
    if not isinstance(rule, dict) or rule.get("folderUID") != folder_uid:
        return False
    if target.get("_kind") == "group":
        if target["_inert"]:
            return (
                rule.get("title") == _group_tombstone_title(target["uid"])
                and rule.get("condition") == "B" and rule.get("isPaused") is True
                and rule.get("labels") == {
                    "severity": "warning", "service": target["service"],
                    "environment": target["environment"], "cluster": target["cluster"],
                    "owner": "platform-operations",
                }
                and _expected_target_queries_match(rule.get("data"), "vector(0)")
                and rule.get("noDataState") == "OK" and rule.get("execErrState") == "OK"
                and rule.get("for") == "0s"
                and rule.get("annotations") == {
                    "summary": "Expected target group rule is paused.",
                    "description": (
                        "The group UID remains owned and inert for a reviewed rollback or disable transition."
                    ),
                }
                and rule.get("notification_settings") in (None, {})
            )
        return (
            rule.get("title") == target["_title"]
            and rule.get("condition") == "B"
            and rule.get("labels") == target["_labels"]
            and _expected_target_queries_match(rule.get("data"), target["_expr"])
            and rule.get("annotations") == target["_annotations"]
            and rule.get("isPaused") is target["_is_paused"]
            and rule.get("notification_settings") == target["_notification_settings"]
            and rule.get("noDataState") == "OK" and rule.get("execErrState") == "Alerting"
            and rule.get("for") == "2m"
        )
    if target["_inert"]:
        retired = target["_retired"]
        return (
            rule.get("title") == (
                target.get("_title") or _tombstone_title(target["uid"], retired=retired)
            )
            and rule.get("condition") == "B"
            and rule.get("isPaused") is True
            and rule.get("labels") == target.get("_labels", {
                "severity": "warning", "owner": "platform-operations", "environment": "disabled",
            })
            and _expected_target_queries_match(rule.get("data"), "vector(0)")
            and rule.get("noDataState") == "OK"
            and rule.get("execErrState") == "OK"
            and rule.get("for") == "0s"
            and rule.get("annotations") == {
                "summary": "Expected metrics target rule is disabled.",
                "description": (
                    "This owned UID was explicitly retired and remains inert."
                    if retired else ("The original target identity remains owned and inert."
                                     if target.get("job") else
                                     "No target identity is retained while the reviewed alert gate is disabled.")
                ),
            }
            and rule.get("notification_settings") in (None, {})
        )
    labels = rule.get("labels")
    return (
        isinstance(labels, dict)
        and rule.get("condition") == "B"
        and all(labels.get(field) == target[field] for field in EXPECTED_IDENTITY_FIELDS)
        and labels == target["_labels"]
        and rule.get("title") == target["_title"]
        and _expected_target_queries_match(rule.get("data"), target["_expr"])
        and rule.get("annotations") == target["_annotations"]
        and rule.get("isPaused") is target["_is_paused"]
        and rule.get("notification_settings") == target["_notification_settings"]
        and rule.get("noDataState") == "OK"
        and rule.get("execErrState") == "Alerting"
        and rule.get("for") == "2m"
    )


def validate_active_expected_target_rules(input_document: Any) -> dict[str, Any]:
    """Prove active targets, gate-off owners, and explicit inert tombstones separately."""
    if not isinstance(input_document, dict):
        raise Refusal("invalid_live_rules")
    if input_document.get("targets") == [] and input_document.get("retired_uids") == []:
        targets, retired = [], []
        if input_document.get("reviewed") is not True:
            raise Refusal("invalid_declaration")
    else:
        targets, retired = validate_expected_target_declaration(
            input_document.get("targets"), input_document.get("retired_uids"),
            reviewed=input_document.get("reviewed"),
        )
    mode = input_document.get("mode", "legacy")
    if mode not in {"grouped", "legacy"}:
        raise Refusal("invalid_declaration")
    groups = expected_target_groups(targets)
    active_target_rows = targets if mode == "legacy" else []
    active_group_rows = groups if mode == "grouped" else []
    paused = input_document.get("paused_uids", [])
    tombstones = input_document.get("tombstones", [])
    active_uids = {row["uid"] for row in [*active_target_rows, *active_group_rows]}
    if (
        not isinstance(paused, list)
        or any(not isinstance(uid, str) or not EXPECTED_UID.fullmatch(uid) for uid in paused)
        or len(set(paused)) != len(paused)
        or set(paused) & (active_uids | set(retired))
    ):
        raise Refusal("invalid_declaration")
    rules = input_document.get("live_rules")
    folders = input_document.get("live_folders")
    if (
        not isinstance(rules, list) or len(rules) > MAX_ROWS
        or not isinstance(folders, list) or len(folders) > MAX_ROWS
    ):
        raise Refusal("invalid_live_rules")
    matching_folders = [
        folder for folder in folders
        if isinstance(folder, dict) and folder.get("title") == "agent-cloud"
    ]
    if len(matching_folders) != 1 or not isinstance(matching_folders[0].get("uid"), str):
        raise Refusal("live_folder_drift")
    folder_uid = matching_folders[0]["uid"]
    by_uid = {rule.get("uid"): rule for rule in rules if isinstance(rule, dict)}
    expected_tombstones = {row.get("uid"): row for row in tombstones if isinstance(row, dict)}
    for target in targets:
        if mode == "grouped":
            expected_tombstones[target["uid"]] = _as_tombstone({"_kind": "target", **target})
    for uid in retired:
        expected_tombstones.setdefault(uid, _as_tombstone({"uid": uid}, retired=True))
    expected_ids = active_uids | set(paused) | set(retired)
    if len(by_uid) != len(rules) or set(by_uid) != expected_ids:
        raise Refusal("live_target_drift")
    active_target_contracts = _expected_target_contracts(active_target_rows)
    active_group_contracts = _expected_group_contracts(active_group_rows)
    for target in active_target_rows:
        rule = by_uid[target["uid"]]
        if not _live_rule_matches_owned(rule, active_target_contracts[target["uid"]], folder_uid):
            raise Refusal("live_target_drift")
        if rule.get("isPaused") is not False or rule.get("notification_settings") != {
            "receiver": "agent-cloud-ops", "group_by": ["service", "environment", "cluster", "alertname"],
            "group_wait": "30s", "group_interval": "5m", "repeat_interval": "4h",
        }:
            raise Refusal("live_target_drift")
    for group in active_group_rows:
        if not _live_rule_matches_owned(by_uid[group["uid"]], active_group_contracts[group["uid"]], folder_uid):
            raise Refusal("live_target_drift")
        if by_uid[group["uid"]].get("isPaused") is not False or by_uid[group["uid"]].get("notification_settings") != {
            "receiver": "agent-cloud-ops", "group_by": ["service", "environment", "cluster", "alertname"],
            "group_wait": "30s", "group_interval": "5m", "repeat_interval": "4h",
        }:
            raise Refusal("live_target_drift")
    for uid in set(paused) | set(retired):
        rule = by_uid[uid]
        tombstone = expected_tombstones.get(uid)
        if tombstone is None:
            raise Refusal("live_target_drift")
        if EXPECTED_GROUP_UID.fullmatch(tombstone["uid"]):
            expected = {
                "uid": uid, "_kind": "group", "_inert": True, "_retired": False,
                "service": tombstone["labels"]["service"],
                "environment": tombstone["labels"]["environment"],
                "cluster": tombstone["labels"]["cluster"],
            }
        else:
            expected = {"uid": uid, "_kind": "target", **{
                field: tombstone["labels"].get(field) for field in EXPECTED_IDENTITY_FIELDS
                if tombstone["labels"].get(field)
            }, "_title": tombstone["title"], "_labels": {
                **tombstone["labels"], "owner": "platform-operations",
            }, "_inert": True, "_retired": uid in retired}
        if not _live_rule_matches_owned(rule, expected, folder_uid):
            raise Refusal("live_target_drift")
    return {"status": "validated", "active_count": len(active_uids), "retired_count": len(retired)}


def _expected_group_contracts(groups: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    contracts = {}
    for group in groups:
        expr = expected_target_group_expression(group)
        contracts[group["uid"]] = {
            **group, "_kind": "group", "_inert": False, "_retired": False,
            "_title": f"Missing telemetry group ({group['uid']})", "_expr": expr,
            "_labels": {
                "severity": "critical", "job": "{{ $labels.job }}", "instance": "{{ $labels.instance }}",
                "service": group["service"], "owner": "platform-operations",
                "environment": group["environment"], "cluster": group["cluster"],
            },
            "_annotations": {
                "summary": "Expected metrics have disappeared for {{ $labels.service }} at {{ $labels.instance }}.",
                "dashboard_url": f"/d/service-overview?var-service={group['service']}",
                "description": "Check the declared metrics target and its service onboarding receipt.",
            },
            "_is_paused": False,
            "_notification_settings": {
                "receiver": "agent-cloud-ops", "group_by": ["service", "environment", "cluster", "alertname"],
                "group_wait": "30s", "group_interval": "5m", "repeat_interval": "4h",
            },
        }
    return contracts


def _expected_target_contracts(targets: list[dict[str, str]]) -> dict[str, dict[str, Any]]:
    """Build the validated alert contract from the reviewed declaration."""
    contracts = {}
    for target in targets:
        expr = (
            "absent_over_time(up{job=" + json.dumps(target["job"]) + ",instance=" + json.dumps(target["instance"])
            + ",service=" + json.dumps(target["service"]) + ",cluster=" + json.dumps(target["cluster"])
            + ",environment=" + json.dumps(target["environment"]) + "}[5m])"
        )
        contracts[target["uid"]] = {
            **target,
            "_inert": False,
            "_retired": False,
            "_title": f"Missing telemetry for {target['service']} at {target['instance']}",
            "_expr": expr,
            "_labels": {
                "severity": "critical", "job": target["job"], "instance": target["instance"],
                "service": target["service"], "owner": "platform-operations",
                "environment": target["environment"], "cluster": target["cluster"],
            },
            "_annotations": {
                "summary": f"Expected metrics have disappeared for {target['service']} at {target['instance']}.",
                "dashboard_url": f"/d/service-overview?var-service={target['service']}",
                "description": "Check the declared metrics target and its service onboarding receipt.",
            },
            "_is_paused": False,
            "_notification_settings": {
                "receiver": "agent-cloud-ops", "group_by": ["service", "environment", "cluster", "alertname"],
                "group_wait": "30s", "group_interval": "5m", "repeat_interval": "4h",
            },
        }
    return contracts


def _owned_expected_targets(document: Any) -> list[dict[str, Any]]:
    """Read only exact generated expected-target rules from our provisioning group."""
    if not isinstance(document, dict) or document.get("apiVersion") != 1:
        raise Refusal("invalid_previous_rules")
    groups = document.get("groups")
    if not isinstance(groups, list) or len(groups) > MAX_ROWS:
        raise Refusal("invalid_previous_rules")
    owned_groups = [
        group for group in groups
        if isinstance(group, dict)
        and (group.get("orgId"), group.get("name"), group.get("folder")) == (1, "service-telemetry", "agent-cloud")
    ]
    if len(owned_groups) != 1:
        raise Refusal("invalid_previous_rules")
    group = owned_groups[0]
    rules = group.get("rules")
    if not isinstance(rules, list) or len(rules) > MAX_ROWS:
        raise Refusal("invalid_previous_rules")
    owned: list[dict[str, Any]] = []
    for rule in rules:
        if not isinstance(rule, dict) or not str(rule.get("uid", "")).startswith("o11y_missing_"):
            continue
        uid = rule.get("uid")
        labels = rule.get("labels")
        if not isinstance(uid, str) or not EXPECTED_UID.fullmatch(uid):
            raise Refusal("invalid_previous_rules")
        rule_data = rule.get("data")
        if EXPECTED_GROUP_UID.fullmatch(uid):
            if rule.get("title") == _group_tombstone_title(uid):
                if not isinstance(labels, dict):
                    raise Refusal("invalid_previous_rules")
                service, environment, cluster = (labels.get(key) for key in ("service", "environment", "cluster"))
                tombstone_expr = expected_target_group_expression({"targets": [{
                    "uid": "o11y_missing_internal", "job": "tombstone", "instance": "tombstone:1",
                    "service": service, "environment": environment, "cluster": cluster,
                }]})
                group = _group_from_rule({"uid": uid, "labels": labels, "data": [
                    {"model": {"expr": tombstone_expr}}
                ]})
                expected_labels = {"severity": "warning", "service": service,
                                   "environment": environment, "cluster": cluster,
                                   "owner": "platform-operations"}
                if (
                    rule.get("isPaused") is True
                    and "notification_settings" not in rule
                    and rule.get("condition") == "B"
                    and _expected_target_queries_match(rule_data, "vector(0)")
                    and rule.get("noDataState") == "OK" and rule.get("execErrState") == "OK"
                    and rule.get("for") == "0s" and labels == expected_labels
                    and rule.get("annotations") == {
                        "summary": "Expected target group rule is paused.",
                        "description": (
                            "The group UID remains owned and inert for a reviewed rollback or disable transition."
                        ),
                    }
                ):
                    owned.append({**group, "_kind": "group", "_inert": True, "_retired": False})
                    continue
                raise Refusal("invalid_previous_rules")
            group = _group_from_rule(rule)
            expected_labels = {
                "severity": "critical", "job": "{{ $$labels.job }}", "instance": "{{ $$labels.instance }}",
                "service": group["service"], "owner": "platform-operations",
                "environment": group["environment"], "cluster": group["cluster"],
            }
            expected_annotations = {
                "summary": "Expected metrics have disappeared for {{ $labels.service }} at {{ $labels.instance }}.",
                "dashboard_url": f"/d/service-overview?var-service={group['service']}",
                "description": "Check the declared metrics target and its service onboarding receipt.",
            }
            active_group = (
                rule.get("title") == f"Missing telemetry group ({uid})"
                and labels == expected_labels and rule.get("condition") == "B"
                and _expected_target_queries_match(rule_data, group["_expr"])
                and rule.get("noDataState") == "OK" and rule.get("execErrState") == "Alerting"
                and rule.get("for") == "2m" and rule.get("isPaused") in (True, False)
                and rule.get("annotations") == expected_annotations
                and set(rule) == {
                    "uid", "title", "condition", "data", "noDataState", "execErrState", "for",
                    "annotations", "labels", "isPaused",
                } | ({"notification_settings"} if "notification_settings" in rule else set())
                and ("notification_settings" not in rule or rule["notification_settings"] == {
                    "receiver": "agent-cloud-ops", "group_by": ["service", "environment", "cluster", "alertname"],
                    "group_wait": "30s", "group_interval": "5m", "repeat_interval": "4h",
                })
            )
            if not active_group:
                raise Refusal("invalid_previous_rules")
            live_labels = {
                **expected_labels,
                "job": "{{ $labels.job }}",
                "instance": "{{ $labels.instance }}",
            }
            owned.append({**group, "_kind": "group", "_inert": False, "_retired": False,
                          "_title": rule["title"], "_labels": live_labels, "_is_paused": rule["isPaused"],
                          "_annotations": rule["annotations"],
                          "_notification_settings": rule.get("notification_settings")})
            continue
        old_inert_title = rule.get("title") in {
            "Expected metrics target previously declared", "Expected metrics target explicitly retired",
        }
        new_inert_title = rule.get("title") in {_tombstone_title(uid), _tombstone_title(uid, retired=True)}
        if (old_inert_title or new_inert_title) and rule.get("isPaused") is True:
            retired = rule.get("title") in {
                "Expected metrics target explicitly retired", _tombstone_title(uid, retired=True),
            }
            if labels == {"severity": "warning", "owner": "platform-operations", "environment": "disabled"}:
                identity = {}
            else:
                if not isinstance(labels, dict) or set(labels) != {
                    "severity", "job", "instance", "service", "cluster", "owner", "environment",
                }:
                    raise Refusal("invalid_previous_rules")
                identity = {field: labels.get(field) for field in EXPECTED_IDENTITY_FIELDS}
                try:
                    normalized, _ = validate_expected_target_declaration([{"uid": uid, **identity}])
                except Refusal:
                    raise Refusal("invalid_previous_rules") from None
                identity = normalized[0]
            title_ok = old_inert_title or rule.get("title") == _tombstone_title(uid, retired=retired)
            expected_annotations = {
                "summary": "Expected metrics target rule is disabled.",
                "description": "This owned UID was explicitly retired and remains inert." if retired
                else "No target identity is retained while the reviewed alert gate is disabled.",
            }
            if identity and not retired:
                expected_annotations["description"] = "The original target identity remains owned and inert."
            if (
                "notification_settings" not in rule and rule.get("condition") == "B"
                and _expected_target_queries_match(rule_data, "vector(0)")
                and rule.get("noDataState") == "OK" and rule.get("execErrState") == "OK"
                and rule.get("for") == "0s" and rule.get("annotations") == expected_annotations
                and title_ok and rule.get("title") in {_tombstone_title(uid), _tombstone_title(uid, retired=True)}
                or (old_inert_title and rule.get("title") in {
                    "Expected metrics target previously declared", "Expected metrics target explicitly retired",
                } and labels == {"severity": "warning", "owner": "platform-operations", "environment": "disabled"}
                and "notification_settings" not in rule and rule.get("condition") == "B"
                and _expected_target_queries_match(rule_data, "vector(0)") and rule.get("noDataState") == "OK"
                and rule.get("execErrState") == "OK" and rule.get("for") == "0s"
                and rule.get("annotations") == {
                    "summary": "Expected metrics target rule is disabled.",
                    "description": "This owned UID was explicitly retired and remains inert." if retired
                    else "No target identity is retained while the reviewed alert gate is disabled.",
                })
            ):
                owned.append({"uid": uid, "_kind": "target", **identity, "_inert": True, "_retired": retired,
                              "_title": rule.get("title"), "_labels": labels})
                continue
            raise Refusal("invalid_previous_rules")
        if not isinstance(labels, dict):
            raise Refusal("invalid_previous_rules")
        if set(labels) != {"severity", "job", "instance", "service", "owner", "environment", "cluster"}:
            raise Refusal("invalid_previous_rules")
        target = {"uid": uid, **{field: labels.get(field) for field in EXPECTED_IDENTITY_FIELDS}}
        try:
            normalized, _ = validate_expected_target_declaration([target])
        except Refusal:
            raise Refusal("invalid_previous_rules") from None
        value = normalized[0]
        rule_data = rule.get("data")
        expr = (
            rule_data[0].get("model", {}).get("expr")
            if isinstance(rule_data, list) and rule_data and isinstance(rule_data[0], dict)
            else None
        )
        expected_expr = (
            "absent_over_time(up{job=" + json.dumps(value["job"]) + ",instance=" + json.dumps(value["instance"])
            + ",service=" + json.dumps(value["service"]) + ",cluster=" + json.dumps(value["cluster"])
            + ",environment=" + json.dumps(value["environment"]) + "}[5m])"
        )
        paused_contract = False
        expected_annotations = {
            "summary": f"Expected metrics have disappeared for {value['service']} at {value['instance']}.",
            "dashboard_url": f"/d/service-overview?var-service={value['service']}",
            "description": "Check the declared metrics target and its service onboarding receipt.",
        }
        legacy_annotations = {
            **expected_annotations,
            "summary": f"Expected metrics have disappeared for {value['service']}.",
        }
        active_contract = (
            rule.get("title") == f"Missing telemetry for {value['service']} at {value['instance']}"
            and expr == expected_expr
            and rule.get("condition") == "B"
            and _expected_target_queries_match(rule_data, expected_expr)
            and rule.get("noDataState") == "OK"
            and rule.get("execErrState") == "Alerting"
            and rule.get("for") == "2m"
            and rule.get("isPaused") in (True, False)
            and set(rule) == {
                "uid", "title", "condition", "data", "noDataState", "execErrState", "for",
                "annotations", "labels", "isPaused",
            } | ({"notification_settings"} if "notification_settings" in rule else set())
            and rule.get("annotations") in (expected_annotations, legacy_annotations)
            and labels == {
                "severity": "critical", "job": value["job"], "instance": value["instance"],
                "service": value["service"], "owner": "platform-operations",
                "environment": value["environment"], "cluster": value["cluster"],
            }
            and ("notification_settings" not in rule or rule["notification_settings"] == {
                "receiver": "agent-cloud-ops", "group_by": ["service", "environment", "cluster", "alertname"],
                "group_wait": "30s", "group_interval": "5m", "repeat_interval": "4h",
            })
        )
        if not paused_contract and not active_contract:
            raise Refusal("invalid_previous_rules")
        owned.append({
            **value,
            "_kind": "target",
            "_inert": False,
            "_retired": False,
            "_title": rule["title"],
            "_expr": expr,
            "_labels": labels,
            "_is_paused": rule["isPaused"],
            "_notification_settings": rule.get("notification_settings"),
            "_annotations": rule["annotations"],
        })
    if len({row["uid"] for row in owned}) != len(owned):
        raise Refusal("invalid_previous_rules")
    return owned


def prepare_expected_target_transition(input_document: Any) -> dict[str, Any]:
    """Keep owned UIDs inert until explicitly restored; reject live ownership drift."""
    if not isinstance(input_document, dict):
        raise Refusal("invalid_transition")
    if input_document.get("managed") is False:
        return {
            "status": "prepared", "reason": "local_compatibility", "paused_uids": [],
            "retired_uids": [], "tombstones": [],
        }
    previous: list[dict[str, Any]] = []
    previous_raw = input_document.get("previous_rules")
    if previous_raw:
        try:
            previous_doc = yaml.safe_load(previous_raw)
        except Exception:
            raise Refusal("invalid_previous_rules") from None
        previous = _owned_expected_targets(previous_doc)
    check_mode = input_document.get("check_mode") is True
    pending: list[dict[str, Any]] = []
    pending_raw = input_document.get("pending_rules")
    if pending_raw:
        try:
            pending_doc = yaml.safe_load(pending_raw)
        except Exception:
            raise Refusal("invalid_pending_rules") from None
        pending = _owned_expected_targets(pending_doc)
        previous_by_uid = {row["uid"]: row for row in previous}
        pending_by_uid = {row["uid"]: row for row in pending}
        retired_uids = input_document.get("retired_uids", [])
        if not isinstance(retired_uids, list):
            raise Refusal("invalid_pending_rules")
        if input_document.get("enabled") is not True and set(pending_by_uid) != set(previous_by_uid):
            raise Refusal("invalid_pending_rules")
    if check_mode and input_document.get("reviewed") is True:
        validate_expected_target_declaration(
            input_document.get("targets"), input_document.get("retired_uids"), reviewed=True
        )
    live_raw = input_document.get("live_rules")
    grafana_running = input_document.get("grafana_running")
    stopped_preflight = bool(previous and not live_raw and not check_mode and grafana_running is False)
    if previous and not live_raw and not check_mode and not stopped_preflight:
        raise Refusal("live_rules_unavailable")
    if stopped_preflight:
        requested_retired = input_document.get("retired_uids", [])
        if (
            not isinstance(requested_retired, list)
            or any(not isinstance(uid, str) or not EXPECTED_UID.fullmatch(uid) for uid in requested_retired)
            or len(set(requested_retired)) != len(requested_retired)
        ):
            raise Refusal("stopped_grafana_retirement_drift")
        baselines = [previous, *([pending] if pending else [])]
        for baseline in baselines:
            baseline_by_uid = {row["uid"]: row for row in baseline}
            retired_before = {uid for uid, row in baseline_by_uid.items() if row["_retired"]}
            if set(requested_retired) - retired_before:
                raise Refusal("stopped_grafana_retirement_drift")
            if input_document.get("enabled") is True:
                current, _ = validate_expected_target_declaration(
                    input_document.get("targets"), requested_retired, reviewed=True
                )
                current_by_uid = {target["uid"]: target for target in current}
                if input_document.get("mode", "legacy") == "grouped":
                    active_groups = {
                        uid: row for uid, row in baseline_by_uid.items()
                        if row.get("_kind") == "group" and not row["_inert"] and not row["_retired"]
                    }
                    active_targets = {
                        uid: row for uid, row in baseline_by_uid.items()
                        if row.get("_kind") == "target" and not row["_inert"] and not row["_retired"]
                    }
                    if active_groups and not active_targets:
                        expected_groups = expected_target_groups(current)
                        if set(active_groups) != {row["uid"] for row in expected_groups}:
                            raise Refusal("stopped_grafana_target_drift")
                        current_identities = {
                            tuple(target[field] for field in EXPECTED_IDENTITY_FIELDS) for target in current
                        }
                        previous_identities = {
                            tuple(target[field] for field in EXPECTED_IDENTITY_FIELDS)
                            for row in active_groups.values() for target in row["targets"]
                        }
                        if current_identities != previous_identities:
                            raise Refusal("stopped_grafana_target_drift")
                    elif not active_groups and active_targets:
                        current_identities = {
                            tuple(target[field] for field in EXPECTED_IDENTITY_FIELDS) for target in current
                        }
                        previous_identities = {
                            tuple(row[field] for field in EXPECTED_IDENTITY_FIELDS)
                            for row in active_targets.values()
                        }
                        if current_identities != previous_identities:
                            raise Refusal("stopped_grafana_target_drift")
                    else:
                        raise Refusal("stopped_grafana_target_drift")
                    if any(
                        row.get("_kind") == "target" and row["_retired"]
                        for uid, row in baseline_by_uid.items() if uid in current_by_uid
                    ):
                        raise Refusal("stopped_grafana_reactivation")
                else:
                    if set(current_by_uid) & {
                        uid for uid, row in baseline_by_uid.items()
                        if row["_inert"] and not row["_retired"]
                    }:
                        raise Refusal("stopped_grafana_reactivation")
                    active_before = {
                        uid for uid, row in baseline_by_uid.items()
                        if not row["_inert"] and not row["_retired"]
                    }
                    if set(current_by_uid) != active_before:
                        raise Refusal("stopped_grafana_target_drift")
                    for uid, target in current_by_uid.items():
                        old = baseline_by_uid[uid]
                        if any(old[field] != target[field] for field in EXPECTED_IDENTITY_FIELDS):
                            raise Refusal("target_identity_changed")
    if (grafana_running is True and not check_mode) or live_raw:
        try:
            live = json.loads(live_raw)
            folders = json.loads(input_document.get("live_folders", ""))
        except (ValueError, TypeError):
            raise Refusal("invalid_live_rules") from None
        if (
            not isinstance(live, list) or len(live) > MAX_ROWS
            or not isinstance(folders, list) or len(folders) > MAX_ROWS
        ):
            raise Refusal("invalid_live_rules")
        matching_folders = [
            folder for folder in folders
            if isinstance(folder, dict) and folder.get("title") == "agent-cloud"
        ]
        if len(matching_folders) != 1 or not isinstance(matching_folders[0].get("uid"), str):
            raise Refusal("live_folder_drift")
        live_folder_uid = matching_folders[0]["uid"]
        previous_by_uid = {row["uid"]: row for row in previous}
        pending_by_uid = {row["uid"]: row for row in pending}
        live_targets = [
            rule for rule in live
            if isinstance(rule, dict) and isinstance(rule.get("uid"), str)
            and rule["uid"].startswith("o11y_missing_")
        ]
        if len({rule["uid"] for rule in live_targets}) != len(live_targets):
            raise Refusal("invalid_live_rules")
        accepted_live_sets = [set(previous_by_uid)]
        if pending_raw:
            accepted_live_sets.append(set(pending_by_uid))
        if {rule["uid"] for rule in live_targets} not in accepted_live_sets:
            raise Refusal("live_target_drift")
        matches_previous = True
        matches_pending = bool(pending)
        for rule in live_targets:
            target = previous_by_uid.get(rule["uid"])
            pending_target = pending_by_uid.get(rule["uid"])
            if target is None and pending_target is None:
                raise Refusal("live_target_drift")
            if rule.get("ruleGroup") != "service-telemetry" or rule.get("folderUID") != live_folder_uid:
                raise Refusal("live_target_drift")
            if target is None or not _live_rule_matches_owned(rule, target, live_folder_uid):
                matches_previous = False
            if pending_target is None or not _live_rule_matches_owned(rule, pending_target, live_folder_uid):
                matches_pending = False
        if not matches_previous and not matches_pending:
            raise Refusal("live_target_drift")
    retired = input_document.get("retired_uids", [])
    mode = input_document.get("mode", "legacy")
    group_retired = input_document.get("group_retired_uids", [])
    if (
        not isinstance(retired, list)
        or any(not isinstance(uid, str) or not EXPECTED_UID.fullmatch(uid) for uid in retired)
        or len(set(retired)) != len(retired)
        or not isinstance(group_retired, list)
        or any(
            not isinstance(uid, str) or not EXPECTED_GROUP_UID.fullmatch(uid)
            for uid in group_retired
        )
        or len(set(group_retired)) != len(group_retired)
        or mode not in {"grouped", "legacy"}
    ):
        raise Refusal("invalid_transition")
    previous_by_uid = {row["uid"]: row for row in previous}
    if input_document.get("enabled") is not True:
        if set(retired) - set(previous_by_uid):
            raise Refusal("invalid_transition")
        retired_ids = {row["uid"] for row in previous if row["_retired"]} | set(retired)
        tombstone_rows = [
            _as_tombstone(row, retired=row["uid"] in retired_ids)
            for row in previous
        ]
        return {
            "status": "prepared", "reason": "check_mode_unverified" if check_mode else "disabled",
            "paused_uids": [uid for uid in previous_by_uid if uid not in retired_ids],
            "retired_uids": sorted(retired_ids), "tombstones": tombstone_rows,
        }

    current, retired = validate_expected_target_declaration(
        input_document.get("targets"), retired, reviewed=True
    )
    groups = expected_target_groups(current)
    current_target_uids = {row["uid"] for row in current}
    current_group_uids = {row["uid"] for row in groups}
    previous_targets = {row["uid"]: row for row in previous if row.get("_kind") == "target"}
    previous_groups = {row["uid"]: row for row in previous if row.get("_kind") == "group"}
    current_groups_by_uid = {row["uid"]: row for row in groups}
    for uid, previous_group in previous_groups.items():
        current_group = current_groups_by_uid.get(uid)
        if current_group and any(
            previous_group[field] != current_group[field] for field in ("service", "environment", "cluster")
        ):
            raise Refusal("group_uid_collision")
    if any(
        uid in previous_targets and previous_targets[uid].get("job")
        and any(previous_targets[uid][field] != target[field] for field in EXPECTED_IDENTITY_FIELDS)
        for target in current for uid in [target["uid"]]
    ):
        raise Refusal("target_identity_changed")
    already_retired = {uid for uid, row in previous_targets.items() if row["_retired"]}
    if current_target_uids & already_retired:
        raise Refusal("retired_target_reuse")
    if set(retired) - set(previous_targets):
        raise Refusal("unowned_retired_target")
    if set(previous_targets) - current_target_uids - set(retired) - already_retired:
        raise Refusal("unretired_previous_target")
    if mode == "grouped" and group_retired:
        raise Refusal("unexpected_group_retirement")
    if mode == "legacy":
        active_previous_groups = {uid for uid, row in previous_groups.items() if not row["_inert"]}
        if (
            not active_previous_groups.issubset(set(group_retired))
            or set(group_retired) - set(previous_groups)
        ):
            raise Refusal("group_rollback_not_explicit")
        previous_target_identities = {
            tuple(row[field] for field in EXPECTED_IDENTITY_FIELDS)
            for row in previous_targets.values() if row.get("job") and not row["_retired"]
        }
        current_identities = {tuple(row[field] for field in EXPECTED_IDENTITY_FIELDS) for row in current}
        if previous_groups and previous_target_identities and previous_target_identities != current_identities:
            raise Refusal("group_rollback_target_drift")
    else:
        if set(group_retired) - set(previous_groups):
            raise Refusal("unowned_group_retirement")

    retired_ids = already_retired | set(retired)
    if mode == "grouped":
        tombstone_rows = [_as_tombstone({"_kind": "target", **target}) for target in current]
        paused_ids = set(current_target_uids)
        for uid, row in previous_groups.items():
            if uid not in current_group_uids:
                paused_ids.add(uid)
                tombstone_rows.append(_as_tombstone(row))
        # A previously inert group in the current identity may be safely reactivated.
        tombstone_rows.extend(_as_tombstone(row, retired=True) for uid, row in previous_targets.items()
                              if uid in retired_ids and uid not in current_target_uids)
    else:
        tombstone_rows = [_as_tombstone(row) for row in previous_groups.values()]
        paused_ids = set(previous_groups)
        tombstone_rows.extend(_as_tombstone(row, retired=True) for uid, row in previous_targets.items()
                              if uid in retired_ids)
    # Preserve already-retired UIDs and never omit their inert owners.
    for uid, row in previous_targets.items():
        if uid in retired_ids and uid not in {item["uid"] for item in tombstone_rows}:
            tombstone_rows.append(_as_tombstone(row, retired=True))
    return {
        "status": "prepared", "reason": "check_mode_unverified" if check_mode else "enabled",
        "paused_uids": sorted(paused_ids), "retired_uids": sorted(retired_ids),
        "tombstones": sorted(tombstone_rows, key=lambda row: row["uid"]),
    }


def expected_target_coverage(targets_raw: bytes | str, declaration: Any) -> dict[str, Any]:
    """Compare validated expected identities to all active target identities."""
    empty = {
        "expected_count": 0,
        "active_target_count": 0,
        "matched_count": 0,
        "missing_count": 0,
        "unexpected_count": 0,
        "unhealthy_count": 0,
    }
    if not isinstance(declaration, dict) or declaration.get("reviewed") is not True:
        return {"status": "unverified", "reason": "declaration_unreviewed", **empty}
    if declaration.get("targets") == []:
        return {"status": "unverified", "reason": "no_expected_targets", **empty}
    try:
        expected, _retired = validate_expected_target_declaration(
            declaration.get("targets"), reviewed=declaration.get("reviewed")
        )
    except Refusal:
        return {"status": "unverified", "reason": "invalid_declaration", **empty}
    expected = [target for target in expected if target.get("source", "prometheus") == "prometheus"]
    if not expected:
        return {"status": "unverified", "reason": "no_expected_targets", **empty}

    try:
        response = _json_document(targets_raw, "malformed_targets")
        data = response.get("data")
        active = data.get("activeTargets") if isinstance(data, dict) else None
        if not isinstance(active, list) or len(active) > MAX_ROWS:
            raise Refusal("malformed_targets")

        expected_ids = {
            tuple(target[field] for field in EXPECTED_IDENTITY_FIELDS)
            for target in expected
        }
        active_ids: set[tuple[str, ...]] = set()
        unhealthy_count = 0
        for target in active:
            if not isinstance(target, dict) or target.get("health") not in {"up", "down", "unknown"}:
                raise Refusal("malformed_targets")
            labels = target.get("labels")
            if not isinstance(labels, dict):
                raise Refusal("malformed_targets")
            identity = tuple(labels.get(field) for field in EXPECTED_IDENTITY_FIELDS)
            if any(not isinstance(value, str) or not SAFE_IDENTITY_VALUE.fullmatch(value) for value in identity):
                raise Refusal("malformed_identity")
            if identity in active_ids:
                raise Refusal("ambiguous_target_identity")
            active_ids.add(identity)
            unhealthy_count += target["health"] != "up"

        matched = len(expected_ids & active_ids)
        missing = len(expected_ids - active_ids)
        unexpected = len(active_ids - expected_ids)
        identity_drift = missing > 0 or unexpected > 0
        if identity_drift and unhealthy_count:
            reason = "identity_drift_and_unhealthy_targets"
        elif identity_drift:
            reason = "identity_drift"
        elif unhealthy_count:
            reason = "unhealthy_targets"
        else:
            reason = "exact_match"
        return {
            "status": "observed",
            "reason": reason,
            "expected_count": len(expected_ids),
            "active_target_count": len(active_ids),
            "matched_count": matched,
            "missing_count": missing,
            "unexpected_count": unexpected,
            "unhealthy_count": unhealthy_count,
        }
    except Refusal as error:
        reason = "ambiguous_target_identity" if error.category == "ambiguous_target_identity" else "malformed_targets"
        return {"status": "unavailable", "reason": reason, **empty}
    except Exception:
        return {"status": "unavailable", "reason": "survey_unavailable", **empty}


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _public_job(job: str) -> str:
    if job in JOB_ALIASES:
        return JOB_ALIASES[job]
    if HOST_JOB.fullmatch(job):
        return "host-node"
    raise Refusal("unrecognized_job")


def _identity(labels: Any, category: str) -> tuple[str, str]:
    if not isinstance(labels, dict):
        raise Refusal(category)
    job, instance = labels.get("job"), labels.get("instance")
    if not isinstance(job, str) or not job or not isinstance(instance, str) or not instance:
        raise Refusal(category)
    return job, instance


def _json_document(raw: bytes | str, category: str) -> dict[str, Any]:
    if not isinstance(raw, (bytes, str)) or not raw or len(raw) > MAX_API_BYTES:
        raise Refusal(category)
    try:
        value = json.loads(
            raw,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
            object_pairs_hook=_unique_object,
        )
    except (ValueError, TypeError, json.JSONDecodeError):
        raise Refusal(category) from None
    if not isinstance(value, dict) or value.get("status") != "success":
        raise Refusal(category)
    for field in ("warnings", "infos"):
        if field in value and value[field]:
            raise Refusal(category)
    return value


def _api_vector(raw: bytes, category: str) -> list[dict[str, Any]]:
    response = _json_document(raw, category)
    data = response.get("data")
    if not isinstance(data, dict) or data.get("resultType") != "vector":
        raise Refusal(category)
    rows = data.get("result")
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        raise Refusal(category)
    return rows


def _declared_instance(instance: str) -> tuple[str, int]:
    try:
        if not instance or "\\" in instance or any(char.isspace() or ord(char) < 0x20 for char in instance):
            raise ValueError
        parsed = urlsplit("http://" + instance)
        if parsed.username is not None or parsed.password is not None:
            raise ValueError
        host = parsed.hostname
        port = parsed.port
        if not host or port is None or not 1 <= port <= 65535 or parsed.path or parsed.query or parsed.fragment:
            raise ValueError
        return _normal_host(host), port
    except (ValueError, UnicodeError):
        raise Refusal("unsafe_target_url") from None


def _normal_host(host: str) -> str:
    if "%" in host or host.endswith("."):
        raise ValueError("noncanonical host")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if NUMERIC_HOST.fullmatch(host):
            raise ValueError("noncanonical numeric host") from None
        return host.encode("idna").decode("ascii").lower()
    canonical = address.compressed.lower()
    if str(address) != host and (address.version == 4 or canonical != host.lower()):
        raise ValueError("noncanonical IP address")
    return canonical


def _is_loopback_host(host: str) -> bool:
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
        return address.is_loopback or (
            isinstance(address, ipaddress.IPv6Address)
            and address.ipv4_mapped is not None
            and address.ipv4_mapped.is_loopback
        )
    except ValueError:
        return False


def validate_target_url(url: Any, instance: str) -> tuple[str, int, str]:
    """Require a plain HTTP URL whose authority exactly matches instance."""
    if not isinstance(url, str) or not url or len(url) > 2048:
        raise Refusal("unsafe_target_url")
    if "\\" in url or any(char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F for char in url):
        raise Refusal("unsafe_target_url")
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme.lower() != "http"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or not parsed.path.startswith("/")
        ):
            raise ValueError
        host = parsed.hostname
        port = parsed.port
        if not host:
            raise ValueError
        normalized_host = _normal_host(host)
        normalized_port = port if port is not None else 80
        if not 1 <= normalized_port <= 65535:
            raise ValueError
        if (normalized_host, normalized_port) != _declared_instance(instance):
            raise ValueError
        # Keep URL private; return normalized authority for duplicate detection only.
        return normalized_host, normalized_port, parsed.path
    except (ValueError, UnicodeError):
        raise Refusal("unsafe_target_url") from None


def _targets(raw: bytes) -> dict[tuple[str, str], tuple[str, str]]:
    response = _json_document(raw, "malformed_targets")
    data = response.get("data")
    active = data.get("activeTargets") if isinstance(data, dict) else None
    if not isinstance(active, list) or len(active) > MAX_ROWS:
        raise Refusal("malformed_targets")
    if not active:
        raise Refusal("no_active_targets")

    result: dict[tuple[str, str], tuple[str, str]] = {}
    urls: set[tuple[str, int, str]] = set()
    for target in active:
        if not isinstance(target, dict):
            raise Refusal("malformed_targets")
        job, instance = _identity(target.get("labels"), "malformed_targets")
        category = _public_job(job)
        if target.get("health") != "up":
            raise Refusal("unhealthy_target")
        original_url = target.get("scrapeUrl")
        url_key = validate_target_url(original_url, instance)
        probe_url = original_url
        if _is_loopback_host(url_key[0]):
            if (
                job == "prometheus"
                and instance == PROMETHEUS_SELF_INSTANCE
                and original_url == PROMETHEUS_SELF_URL
            ):
                probe_url = PROMETHEUS_SELF_PROBE_URL
            else:
                raise Refusal("unsafe_target_url")
        key = (job, instance)
        if key in result or url_key in urls:
            raise Refusal("ambiguous_targets")
        result[key] = (category, probe_url)
        urls.add(url_key)
    return result


def _positive_integer(value: Any, category: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise Refusal(category)
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise Refusal(category) from None
    if not number.is_finite() or number <= 0 or number != number.to_integral_value():
        raise Refusal(category)
    if len(number.as_tuple().digits) > MAX_INTEGER_DIGITS or number.adjusted() >= MAX_INTEGER_DIGITS:
        raise Refusal(category)
    return int(number)


def _samples(raw: bytes, targets: dict[tuple[str, str], tuple[str, str]]) -> dict[tuple[str, str], int]:
    result: dict[tuple[str, str], int] = {}
    for row in _api_vector(raw, "malformed_samples"):
        if not isinstance(row, dict):
            raise Refusal("malformed_samples")
        labels = row.get("metric")
        job, instance = _identity(labels, "malformed_samples")
        key = (job, instance)
        if key not in targets:
            continue  # 24-hour data for a target that is no longer active.
        value = row.get("value")
        if not isinstance(value, list) or len(value) != 2:
            raise Refusal("malformed_samples")
        if key in result:
            raise Refusal("ambiguous_metric")
        result[key] = _positive_integer(value[1], "malformed_samples")
    if set(result) != set(targets):
        raise Refusal("missing_samples")
    return result


def _label_series(raw: bytes, key: tuple[str, str]) -> tuple[int, int, int]:
    response = _json_document(raw, "malformed_series")
    rows = response.get("data")
    if not isinstance(rows, list) or not rows or len(rows) > MAX_ROWS:
        raise Refusal("missing_series" if rows == [] else "malformed_series")

    seen: set[tuple[tuple[str, str], ...]] = set()
    max_count = 0
    max_name_bytes = 0
    max_value_bytes = 0
    for labels in rows:
        if not isinstance(labels, dict) or not labels:
            raise Refusal("malformed_series")
        if labels.get("job") != key[0] or labels.get("instance") != key[1]:
            raise Refusal("ambiguous_series")
        if not isinstance(labels.get("__name__"), str) or not labels["__name__"]:
            raise Refusal("malformed_series")
        if any(not isinstance(name, str) or not name or not isinstance(value, str) for name, value in labels.items()):
            raise Refusal("malformed_series")
        identity = tuple(sorted(labels.items()))
        if identity in seen:
            raise Refusal("ambiguous_series")
        seen.add(identity)
        try:
            max_count = max(max_count, len(labels))
            max_name_bytes = max(max_name_bytes, *(len(name.encode("utf-8")) for name in labels))
            max_value_bytes = max(max_value_bytes, *(len(value.encode("utf-8")) for value in labels.values()))
        except UnicodeEncodeError:
            raise Refusal("malformed_series") from None
    if min(max_count, max_name_bytes, max_value_bytes) <= 0:
        raise Refusal("malformed_series")
    return max_count, max_name_bytes, max_value_bytes


def _series_selector(job: str, instance: str) -> str:
    return "{job=" + json.dumps(job, ensure_ascii=True) + ",instance=" + json.dumps(instance, ensure_ascii=True) + "}"


def _series_config_url(job: str, instance: str, start: int, end: int) -> str:
    query = urlencode(
        [
            ("match[]", _series_selector(job, instance)),
            ("start", str(start)),
            ("end", str(end)),
        ]
    )
    return PROMETHEUS + "/api/v1/series?" + query


def _private_api_config(url: str) -> bytes:
    lines = [
        "url = " + json.dumps(url),
        'header = "Accept-Encoding: identity"',
        'proxy = ""',
        'noproxy = "*"',
        'proto = "=http"',
        "globoff",
        f"connect-timeout = {CONNECT_TIMEOUT}",
        'max-time = 15',
        f"max-filesize = {MAX_API_BYTES}",
        "max-redirs = 0",
        'output = "-"',
        'write-out = "%{http_code}"',
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _run_series_query(job: str, instance: str, start: int, end: int) -> bytes:
    config = _private_api_config(_series_config_url(job, instance, start, end))
    argv = [ENGINE, "exec", "-i", CONTAINER, "curl", "--disable", "--silent", "--show-error", "--fail", "--config", "-"]
    try:
        process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        raise Refusal("series_unavailable") from None
    assert process.stdin is not None and process.stdout is not None
    try:
        process.stdin.write(config)
        process.stdin.close()
    except OSError:
        process.kill()
        process.wait()
        raise Refusal("series_unavailable") from None

    state: dict[str, Any] = {"output": bytearray(), "overflow": False, "error": False}

    def consume() -> None:
        try:
            while True:
                chunk = process.stdout.read(64 * 1024)
                if not chunk:
                    return
                remaining = MAX_API_BYTES + 3 - len(state["output"])
                if len(chunk) > remaining:
                    state["overflow"] = True
                    process.kill()
                    return
                state["output"].extend(chunk)
        except OSError:
            state["error"] = True

    reader = threading.Thread(target=consume, daemon=True)
    reader.start()
    reader.join(20)
    if reader.is_alive():
        process.kill()
        reader.join(1)
        state["error"] = True
    try:
        return_code = process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        return_code = -1
    output = bytes(state["output"])
    if state["overflow"] or state["error"] or return_code != 0 or len(output) < 3:
        raise Refusal("series_unavailable")
    status_text = output[-3:]
    if not status_text.isdigit() or not 200 <= int(status_text) < 300:
        raise Refusal("series_unavailable")
    return output[:-3]


def _response_headers(raw: bytes) -> tuple[int, dict[str, list[str]], bytes]:
    marker = raw.find(b"\r\n\r\n")
    if marker < 0:
        raise Refusal("probe_unavailable")
    block = raw[:marker]
    if len(block) > MAX_RESPONSE_HEADERS:
        raise Refusal("probe_unavailable")
    lines = block.split(b"\r\n")
    status_match = STATUS_LINE.fullmatch(lines[0]) if lines else None
    if status_match is None:
        raise Refusal("probe_unavailable")
    status = int(status_match.group(1))
    headers: dict[str, list[str]] = {}
    for line in lines[1:]:
        if b":" not in line:
            raise Refusal("probe_unavailable")
        name, value = line.split(b":", 1)
        try:
            key = name.decode("ascii").strip().lower()
            text = value.decode("latin-1").strip().lower()
        except UnicodeDecodeError:
            raise Refusal("probe_unavailable") from None
        if not key:
            raise Refusal("probe_unavailable")
        headers.setdefault(key, []).append(text)
    return status, headers, raw[marker + 4 :]


def _curl_config(url: str) -> bytes:
    # URL is deliberately supplied through stdin, never through process argv.
    lines = [
        "url = " + json.dumps(url),
        'header = "Accept-Encoding: identity"',
        'proxy = ""',
        'noproxy = "*"',
        'proto = "=http"',
        "globoff",
        f"connect-timeout = {CONNECT_TIMEOUT}",
        f"max-time = {TRANSFER_TIMEOUT}",
        f"max-filesize = {MAX_RESPONSE_BYTES}",
        "max-redirs = 0",
        "dump-header = \"-\"",
        'output = "-"',
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _probe_command() -> list[str]:
    return [ENGINE, "exec", "-i", CONTAINER, "curl", "--disable", "--config", "-"]


def _run_probe(url: str) -> tuple[int, int]:
    config = _curl_config(url)
    try:
        process = subprocess.Popen(
            _probe_command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        raise Refusal("probe_unavailable") from None
    assert process.stdin is not None and process.stdout is not None
    try:
        process.stdin.write(config)
        process.stdin.close()
    except OSError:
        process.kill()
        process.wait()
        raise Refusal("probe_unavailable") from None

    state: dict[str, Any] = {"headers": bytearray(), "body_bytes": 0, "status": None, "error": None}

    def consume() -> None:
        try:
            while True:
                chunk = process.stdout.read(64 * 1024)
                if not chunk:
                    break
                if state["status"] is None:
                    header_bytes = state["headers"]
                    header_bytes.extend(chunk)
                    marker = header_bytes.find(b"\r\n\r\n")
                    if marker < 0:
                        if len(header_bytes) > MAX_RESPONSE_HEADERS:
                            state["error"] = "probe_unavailable"
                            process.kill()
                            return
                        continue
                    status, headers, body = _response_headers(bytes(header_bytes))
                    state["status"] = status
                    state["headers"] = bytearray()
                    encodings = headers.get("content-encoding", [])
                    lengths = headers.get("content-length", [])
                    transfers = headers.get("transfer-encoding", [])
                    if status < 200 or status >= 300:
                        state["error"] = "probe_http_status"
                        process.kill()
                        return
                    if len(encodings) > 1 or any(value not in ("", "identity") for value in encodings):
                        state["error"] = "probe_compressed"
                        process.kill()
                        return
                    if len(transfers) > 1 or any(value != "chunked" for value in transfers):
                        state["error"] = "probe_compressed"
                        process.kill()
                        return
                    if len(lengths) > 1 or (lengths and not DECIMAL.fullmatch(lengths[0])):
                        state["error"] = "probe_unavailable"
                        process.kill()
                        return
                    if lengths and int(lengths[0]) > MAX_RESPONSE_BYTES:
                        state["error"] = "probe_too_large"
                        process.kill()
                        return
                    state["body_bytes"] = len(body)
                else:
                    state["body_bytes"] += len(chunk)
                if state["body_bytes"] > MAX_RESPONSE_BYTES:
                    state["error"] = "probe_too_large"
                    process.kill()
                    return
        except (OSError, Refusal):
            state["error"] = "probe_unavailable"
            try:
                process.kill()
            except OSError:
                pass

    reader = threading.Thread(target=consume, daemon=True)
    reader.start()
    reader.join(TRANSFER_TIMEOUT + CONNECT_TIMEOUT + 2)
    if reader.is_alive():
        process.kill()
        reader.join(1)
        state["error"] = "probe_timeout"
    try:
        return_code = process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        return_code = -1
    if state["error"]:
        raise Refusal(state["error"])
    if state["status"] is None or return_code != 0:
        if state["body_bytes"] >= MAX_RESPONSE_BYTES:
            raise Refusal("probe_too_large")
        raise Refusal("probe_unavailable")
    if state["body_bytes"] > MAX_RESPONSE_BYTES:
        raise Refusal("probe_too_large")
    if state["body_bytes"] == 0:
        raise Refusal("missing_probe_data")
    return state["status"], state["body_bytes"]


def summarize(
    targets_raw: bytes | str,
    samples_raw: bytes | str,
    probe: Callable[[str], tuple[int, int]],
    series_query: Callable[[str, str, int, int], bytes],
    series_start: int,
    series_end: int,
) -> dict[str, Any]:
    if (
        isinstance(series_start, bool)
        or isinstance(series_end, bool)
        or not isinstance(series_start, int)
        or not isinstance(series_end, int)
        or series_end - series_start != 86400
    ):
        raise Refusal("malformed_series")
    if isinstance(targets_raw, str):
        targets_raw = targets_raw.encode()
    if isinstance(samples_raw, str):
        samples_raw = samples_raw.encode()
    target_map = _targets(targets_raw)
    sample_index = _samples(samples_raw, target_map)
    label_index: dict[tuple[str, str], tuple[int, int, int]] = {}
    for job, instance in target_map:
        series_raw = series_query(job, instance, series_start, series_end)
        label_index[(job, instance)] = _label_series(series_raw, (job, instance))

    totals: dict[str, dict[str, Any]] = {}
    for key, (public_job, url) in target_map.items():
        status, response_bytes = probe(url)
        if status < 200 or status >= 300:
            raise Refusal("probe_http_status")
        if isinstance(response_bytes, bool) or not isinstance(response_bytes, int):
            raise Refusal("probe_unavailable")
        if response_bytes == 0:
            raise Refusal("missing_probe_data")
        if response_bytes < 0 or response_bytes > MAX_RESPONSE_BYTES:
            raise Refusal("probe_too_large")
        row = totals.setdefault(
            public_job,
            {
                "job": public_job,
                "active_target_count": 0,
                "health": {"healthy": 0, "unhealthy": 0},
                "max_scrape_samples_scraped_24h": 0,
                "max_scrape_response_body_bytes_point_in_time": 0,
                "max_stored_series_label_count_24h": 0,
                "max_stored_series_label_name_bytes_24h": 0,
                "max_stored_series_label_value_bytes_24h": 0,
            },
        )
        row["active_target_count"] += 1
        row["health"]["healthy"] += 1
        row["max_scrape_samples_scraped_24h"] = max(row["max_scrape_samples_scraped_24h"], sample_index[key])
        row["max_scrape_response_body_bytes_point_in_time"] = max(
            row["max_scrape_response_body_bytes_point_in_time"], response_bytes
        )
        row["max_stored_series_label_count_24h"] = max(
            row["max_stored_series_label_count_24h"], label_index[key][0]
        )
        row["max_stored_series_label_name_bytes_24h"] = max(
            row["max_stored_series_label_name_bytes_24h"], label_index[key][1]
        )
        row["max_stored_series_label_value_bytes_24h"] = max(
            row["max_stored_series_label_value_bytes_24h"], label_index[key][2]
        )
    return {
        "status": "observed",
        "active_target_count": len(target_map),
        "jobs": [totals[name] for name in sorted(totals)],
    }


def _run_api(
    path: str, unavailable_reason: str = "query_unavailable", *,
    deadline: float | None = None, max_response_bytes: int = MAX_API_BYTES,
) -> bytes:
    remaining = 15.0 if deadline is None else deadline - time.monotonic()
    if (
        not isinstance(max_response_bytes, int) or isinstance(max_response_bytes, bool)
        or not 1 <= max_response_bytes <= MAX_API_BYTES or remaining <= 0
    ):
        raise Refusal(unavailable_reason)
    request_seconds = min(15.0, remaining)
    argv = [
        ENGINE,
        "exec",
        CONTAINER,
        "curl",
        "--disable",
        "--silent",
        "--show-error",
        "--fail",
        "--connect-timeout",
        str(CONNECT_TIMEOUT),
        "--max-time",
        f"{request_seconds:.3f}",
        "--max-filesize",
        str(max_response_bytes),
        "--proxy",
        "",
        "--noproxy",
        "*",
        "--write-out",
        "%{http_code}",
        PROMETHEUS + path,
    ]
    try:
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        raise Refusal(unavailable_reason) from None

    assert process.stdout is not None
    state: dict[str, Any] = {"output": bytearray(), "overflow": False, "error": False}

    def consume() -> None:
        try:
            while True:
                chunk = process.stdout.read(64 * 1024)
                if not chunk:
                    return
                remaining_bytes = max_response_bytes + 3 - len(state["output"])
                if len(chunk) > remaining_bytes:
                    state["overflow"] = True
                    process.kill()
                    return
                state["output"].extend(chunk)
        except OSError:
            state["error"] = True

    reader = threading.Thread(target=consume, daemon=True)
    reader.start()
    reader.join(min(20.0, request_seconds))
    if reader.is_alive():
        process.kill()
        reader.join(1)
        state["error"] = True
    try:
        return_code = process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        return_code = -1
    output = bytes(state["output"])
    if state["overflow"] or state["error"] or return_code != 0 or len(output) < 3:
        raise Refusal(unavailable_reason)
    status_text = output[-3:]
    if not status_text.isdigit() or not 200 <= int(status_text) < 300:
        raise Refusal(unavailable_reason)
    return output[:-3]


def _run_native_api_batch(
    paths: list[str], *, deadline: float | None = None,
    max_total_bytes: int = MAX_API_BYTES,
) -> list[bytes]:
    """Read the fixed native vectors under one aggregate request/byte/time budget."""
    if not isinstance(paths, list) or not 1 <= len(paths) <= 6 or any(
        not isinstance(path, str) or not path.startswith("/api/v1/query?") or len(path) > 4096
        for path in paths
    ):
        raise Refusal("native_samples_unavailable")
    deadline = time.monotonic() + MAX_AVAILABILITY_READBACK_SECONDS if deadline is None else deadline
    if (
        isinstance(max_total_bytes, bool) or not isinstance(max_total_bytes, int)
        or not 1 <= max_total_bytes <= MAX_API_BYTES or deadline <= time.monotonic()
    ):
        raise Refusal("native_samples_unavailable")
    remaining_bytes = max_total_bytes
    responses: list[bytes] = []
    for path in paths:
        response = _run_api(
            path, "native_samples_unavailable", deadline=deadline,
            max_response_bytes=remaining_bytes,
        )
        if len(response) > remaining_bytes:
            raise Refusal("native_samples_unavailable")
        responses.append(response)
        remaining_bytes -= len(response)
        if time.monotonic() >= deadline:
            raise Refusal("native_samples_unavailable")
    return responses


def _status_data(path: str, unavailable_reason: str) -> dict[str, Any]:
    response = _json_document(_run_api(path, unavailable_reason), unavailable_reason)
    data = response.get("data")
    if not isinstance(data, dict):
        raise Refusal(unavailable_reason)
    return data


def _runtime_signature(data: Any) -> tuple[str, str]:
    if not isinstance(data, dict) or data.get("reloadConfigSuccess") is not True:
        raise Refusal("runtime_config_unavailable")
    start_time = data.get("startTime")
    config_time = data.get("lastConfigTime")
    if not isinstance(start_time, str) or not isinstance(config_time, str):
        raise Refusal("runtime_config_unavailable")
    try:
        start = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
        config = datetime.fromisoformat(config_time.replace("Z", "+00:00"))
    except ValueError:
        raise Refusal("runtime_config_unavailable") from None
    if start.tzinfo is None or config.tzinfo is None:
        raise Refusal("runtime_config_unavailable")
    return start_time, config_time


def _active_target_pools(targets_raw: bytes | str) -> list[dict[str, str]]:
    response = _json_document(targets_raw, "malformed_targets")
    data = response.get("data")
    active = data.get("activeTargets") if isinstance(data, dict) else None
    if not isinstance(active, list) or not active or len(active) > MAX_ROWS:
        raise Refusal("malformed_targets")
    pools: dict[str, str] = {}
    for target in active:
        if not isinstance(target, dict):
            raise Refusal("malformed_targets")
        pool = target.get("scrapePool")
        labels = target.get("labels")
        job = labels.get("job") if isinstance(labels, dict) else None
        if not isinstance(pool, str) or not pool or not isinstance(job, str) or not job:
            raise Refusal("ambiguous_target_pool")
        try:
            pool_category = _public_job(pool)
            job_category = _public_job(job)
        except Refusal:
            raise Refusal("ambiguous_target_pool") from None
        if pool_category != job_category or (pool in pools and pools[pool] != job):
            raise Refusal("ambiguous_target_pool")
        pools[pool] = job
    return [{"pool": pool, "job": pools[pool]} for pool in sorted(pools)]


def _loaded_limit_readback(targets_raw: bytes | str) -> dict[str, Any]:
    build = _status_data("/api/v1/status/buildinfo", "buildinfo_unavailable")
    if build.get("version") != "3.1.0":
        raise Refusal("prometheus_version_mismatch")

    runtime_before = _runtime_signature(
        _status_data("/api/v1/status/runtimeinfo", "runtime_config_unavailable")
    )
    config_before = _status_data("/api/v1/status/config", "loaded_config_unavailable").get("yaml")
    runtime_middle = _runtime_signature(
        _status_data("/api/v1/status/runtimeinfo", "runtime_config_unavailable")
    )
    config_after = _status_data("/api/v1/status/config", "loaded_config_unavailable").get("yaml")
    runtime_after = _runtime_signature(
        _status_data("/api/v1/status/runtimeinfo", "runtime_config_unavailable")
    )

    for config_yaml in (config_before, config_after):
        if not isinstance(config_yaml, str) or not config_yaml:
            raise Refusal("loaded_config_unavailable")
        try:
            config_size = len(config_yaml.encode("utf-8"))
        except UnicodeError:
            raise Refusal("loaded_config_unavailable") from None
        if config_size > MAX_API_BYTES:
            raise Refusal("loaded_config_unavailable")

    if (
        config_before != config_after
        or runtime_before != runtime_middle
        or runtime_middle != runtime_after
    ):
        raise Refusal("unstable_loaded_config")

    return {
        "status": "observed",
        "reason": "stable_loaded_config",
        "config_yaml": config_after,
        "target_pools": _active_target_pools(targets_raw),
    }


def _alert_input_family(
    raw: bytes,
    metric_name: str,
    job: str,
    active_targets: set[tuple[str, str]],
) -> dict[str, str]:
    rows = _api_vector(raw, "malformed_alert_response")
    if not rows:
        return {"status": "missing_or_stale", "reason": "no_samples"}

    seen: set[tuple[tuple[str, str], ...]] = set()
    values: list[Decimal] = []
    for row in rows:
        if not isinstance(row, dict):
            raise Refusal("malformed_alert_response")
        labels = row.get("metric")
        if (
            not isinstance(labels, dict)
            or labels.get("__name__") != metric_name
            or any(not isinstance(name, str) or not isinstance(value, str) for name, value in labels.items())
        ):
            raise Refusal("malformed_alert_response")
        key = (labels.get("job"), labels.get("instance"))
        if not all(isinstance(value, str) and value for value in key):
            raise Refusal("malformed_alert_response")
        if key[0] != job or key not in active_targets:
            raise Refusal("ambiguous_alert_target")
        identity = tuple(sorted(labels.items()))
        if identity in seen:
            raise Refusal("ambiguous_alert_series")
        seen.add(identity)

        sample = row.get("value")
        if not isinstance(sample, list) or len(sample) != 2 or isinstance(sample[0], bool):
            raise Refusal("malformed_alert_sample")
        try:
            evaluation_timestamp = Decimal(str(sample[0]))
            value = Decimal(str(sample[1]))
        except (InvalidOperation, ValueError):
            raise Refusal("malformed_alert_sample") from None
        if not evaluation_timestamp.is_finite() or not value.is_finite() or value < 0:
            raise Refusal("malformed_alert_sample")
        values.append(value)

    return {
        "status": "observed_nonzero" if any(value > 0 for value in values) else "observed_zero",
        "reason": "samples_present",
    }


def _alert_input_availability(
    targets_raw: bytes | str, *, native_alloy_verified: bool = False,
) -> dict[str, Any]:
    try:
        targets = _targets(targets_raw.encode() if isinstance(targets_raw, str) else targets_raw)
    except Exception:
        return {
            "status": "observed",
            "families": [
                {"family": metric_name, "status": "unavailable", "reason": "target_unavailable"}
                for metric_name, _ in ALERT_INPUTS
            ],
        }
    active_by_job: dict[str, set[tuple[str, str]]] = {}
    for job, instance in targets:
        if job in {source_job for _, source_job in ALERT_INPUTS}:
            active_by_job.setdefault(job, set()).add((job, instance))

    families: list[dict[str, str]] = []
    for metric_name, job in ALERT_INPUTS:
        try:
            expected_targets = active_by_job.get(job, set())
            if job == "alloy" and native_alloy_verified:
                expected_targets = {("alloy", "alloy:12345")}
            if not expected_targets:
                raise Refusal("alert_target_unavailable")
            query = f'{metric_name}{{job="{job}"}}'
            params = {"query": query, "lookback_delta": "5m"}
            raw = _run_api("/api/v1/query?" + urlencode(params), "alert_query_unavailable")
            result = _alert_input_family(raw, metric_name, job, expected_targets)
        except Refusal as error:
            reason = {
                "malformed_alert_response": "malformed_response",
                "malformed_alert_sample": "malformed_sample",
                "ambiguous_alert_target": "ambiguous_target",
                "ambiguous_alert_series": "ambiguous_series",
                "alert_target_unavailable": "target_unavailable",
                "alert_query_unavailable": "api_unavailable",
            }.get(error.category, "malformed_response")
            result = {"status": "unavailable", "reason": reason}
        except Exception:
            result = {"status": "unavailable", "reason": "api_unavailable"}
        families.append({"family": metric_name, **result})
    return {"status": "observed", "families": families}


def _alloy_typed_value(value: Any) -> Any:
    """Decode the Alloy DETAIL value envelope without flattening its types."""
    if not isinstance(value, dict) or set(value) != {"type", "value"}:
        raise Refusal("native_detail_invalid")
    kind, raw = value["type"], value["value"]
    if kind == "string":
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_AVAILABILITY_DETAIL_BYTES:
            raise Refusal("native_detail_invalid")
        return raw
    if kind == "number":
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise Refusal("native_detail_invalid")
        return raw
    if kind == "bool":
        if not isinstance(raw, bool):
            raise Refusal("native_detail_invalid")
        return raw
    if kind == "capsule":
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_AVAILABILITY_DETAIL_BYTES:
            raise Refusal("native_detail_invalid")
        return raw
    if kind == "array":
        if not isinstance(raw, list) or len(raw) > MAX_AVAILABILITY_DETAIL_ROWS:
            raise Refusal("native_detail_invalid")
        return [_alloy_typed_value(item) for item in raw]
    if kind == "object":
        if not isinstance(raw, list) or len(raw) > MAX_AVAILABILITY_DETAIL_ROWS:
            raise Refusal("native_detail_invalid")
        result: dict[str, Any] = {}
        for item in raw:
            if not isinstance(item, dict) or set(item) != {"key", "value"}:
                raise Refusal("native_detail_invalid")
            key = item["key"]
            if not isinstance(key, str) or not key or len(key) > 256 or key in result:
                raise Refusal("native_detail_invalid")
            result[key] = _alloy_typed_value(item["value"])
        return result
    raise Refusal("native_detail_invalid")


def _alloy_detail_rows(rows: Any, row_type: str) -> dict[str, Any]:
    if not isinstance(rows, list) or len(rows) > MAX_AVAILABILITY_DETAIL_ROWS:
        raise Refusal("native_detail_invalid")
    result: dict[str, Any] = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("type") != row_type:
            raise Refusal("native_detail_invalid")
        name = row.get("name")
        if not isinstance(name, str) or not name or len(name) > 256 or name in result:
            raise Refusal("native_detail_invalid")
        if row_type == "attr":
            if set(row) != {"name", "type", "value"}:
                raise Refusal("native_detail_invalid")
            result[name] = _alloy_typed_value(row["value"])
        else:
            if set(row) != {"name", "type", "body"}:
                raise Refusal("native_detail_invalid")
            result[name] = _alloy_detail_rows(row["body"], "attr")
    return result


def _alloy_component_detail(raw: bytes, expected_id: str, expected_name: str) -> dict[str, Any]:
    if not isinstance(raw, bytes) or len(raw) > MAX_AVAILABILITY_DETAIL_BYTES:
        raise Refusal("native_detail_unavailable")
    status, headers, body = _response_headers(raw)
    if status == 404:
        raise Refusal("native_component_absent")
    content_types = headers.get("content-type", [])
    lengths = headers.get("content-length", [])
    encodings = headers.get("content-encoding", [])
    transfers = headers.get("transfer-encoding", [])
    if (
        status != 200 or len(content_types) != 1
        or content_types[0].split(";", 1)[0].strip().lower() != "application/json"
        or len(body) > MAX_AVAILABILITY_DETAIL_BYTES
        or len(lengths) > 1
        or (lengths and (not DECIMAL.fullmatch(lengths[0]) or int(lengths[0]) != len(body)))
        or len(encodings) > 1 or any(value not in {"", "identity"} for value in encodings)
        or len(transfers) > 1 or any(value != "chunked" for value in transfers)
    ):
        raise Refusal("native_detail_unavailable")
    try:
        detail = json.loads(
            body,
            object_pairs_hook=_unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        raise Refusal("native_detail_invalid") from None
    if (
        not isinstance(detail, dict) or set(detail) != ALLOY_DETAIL_FIELDS
        or detail.get("localID") != expected_id
        or detail.get("name") != expected_name
        or detail.get("type") != "block"
        or not isinstance(detail.get("label"), str)
        or not isinstance(detail.get("moduleID"), str)
        or detail.get("original") != ""
        or not isinstance(detail.get("exports"), list)
        or not isinstance(detail.get("referencedBy"), list)
        or not isinstance(detail.get("referencesTo"), list)
        or len(detail["referencedBy"]) > MAX_AVAILABILITY_DETAIL_ROWS
        or len(detail["referencesTo"]) > MAX_AVAILABILITY_DETAIL_ROWS
        or any(
            not isinstance(component_id, str)
            or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,160}", component_id)
            for component_id in detail["referencedBy"] + detail["referencesTo"]
        )
        or len(set(detail["referencedBy"])) != len(detail["referencedBy"])
        or len(set(detail["referencesTo"])) != len(detail["referencesTo"])
    ):
        raise Refusal("native_detail_invalid")
    health = detail.get("health")
    if (
        not isinstance(health, dict) or set(health) != {"message", "state", "updatedTime"}
        or health.get("state") not in {"healthy", "unhealthy"}
        or not isinstance(health.get("message"), str)
        or not isinstance(health.get("updatedTime"), str)
    ):
        raise Refusal("native_detail_invalid")
    argument_rows = detail.get("arguments")
    if not isinstance(argument_rows, list) or len(argument_rows) > MAX_AVAILABILITY_DETAIL_ROWS:
        raise Refusal("native_detail_invalid")
    if any(not isinstance(row, dict) or row.get("type") not in {"attr", "block"} for row in argument_rows):
        raise Refusal("native_detail_invalid")
    attr_rows = [row for row in argument_rows if row["type"] == "attr"]
    block_rows = [row for row in argument_rows if row["type"] == "block"]
    if len({row.get("name") for row in argument_rows}) != len(argument_rows):
        raise Refusal("native_detail_invalid")
    return {
        "local_id": expected_id,
        "name": expected_name,
        "health": health["state"],
        "arguments": _alloy_detail_rows(attr_rows, "attr"),
        "argument_blocks": _alloy_detail_rows(block_rows, "block"),
        "debug_info": _alloy_detail_rows(detail.get("debugInfo"), "block"),
        "referenced_by": detail["referencedBy"],
        "references_to": detail["referencesTo"],
    }


def _detail_object_rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > MAX_AVAILABILITY_DETAIL_ROWS:
        raise Refusal("native_detail_invalid")
    if any(not isinstance(item, dict) for item in value):
        raise Refusal("native_detail_invalid")
    return value


def _parse_alloy_bytes(value: Any) -> int:
    if not isinstance(value, str):
        raise Refusal("native_detail_invalid")
    match = re.fullmatch(r"([1-9][0-9]{0,11})(B|KiB|MiB|GiB)", value)
    if match is None:
        raise Refusal("native_detail_invalid")
    factor = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}[match.group(2)]
    number = int(match.group(1)) * factor
    if number > 2**63 - 1:
        raise Refusal("native_detail_invalid")
    return number


def _expected_blackbox_module(target: dict[str, Any]) -> dict[str, Any]:
    capsule = target.get("blackbox_config")
    if not isinstance(capsule, str) or len(capsule.encode("utf-8")) > MAX_AVAILABILITY_DETAIL_BYTES:
        raise Refusal("native_declaration_invalid")
    try:
        value = json.loads(capsule, object_pairs_hook=_unique_object)
    except (ValueError, TypeError, json.JSONDecodeError):
        raise Refusal("native_declaration_invalid") from None
    if not isinstance(value, dict) or set(value) != {"modules"}:
        raise Refusal("native_declaration_invalid")
    return value


def _decode_alloy_blackbox_capsule(capsule: Any) -> dict[str, Any]:
    if not isinstance(capsule, str) or len(capsule.encode("utf-8")) > MAX_AVAILABILITY_DETAIL_BYTES:
        raise Refusal("native_detail_invalid")
    try:
        serialized = json.loads(capsule, object_pairs_hook=_unique_object)
        if not isinstance(serialized, str) or len(serialized.encode("utf-8")) > MAX_AVAILABILITY_DETAIL_BYTES:
            raise ValueError
        decoded = yaml.load(serialized, Loader=_SourceUniqueKeyLoader)
    except (ValueError, TypeError, UnicodeError, yaml.YAMLError, json.JSONDecodeError):
        raise Refusal("native_detail_invalid") from None
    if not isinstance(decoded, dict) or set(decoded) != {"modules"}:
        raise Refusal("native_detail_invalid")
    return decoded


def _alloy_scrape_arguments(
    detail: dict[str, Any], *, job: str, target: dict[str, Any] | None,
    shared_limits: dict[str, Any], self_scrape: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    args = detail.get("arguments")
    blocks = detail.get("argument_blocks")
    if not isinstance(args, dict) or not isinstance(blocks, dict):
        raise Refusal("native_detail_invalid")
    required = {
        "targets", "forward_to", "job_name", "scrape_protocols", "body_size_limit",
        "sample_limit", "label_limit", "label_name_length_limit", "label_value_length_limit",
        "extra_metrics",
    }
    optional = {"scrape_interval", "scrape_timeout"}
    if set(args) - (required | optional) or not required.issubset(args) or blocks:
        raise Refusal("native_detail_invalid")
    if args.get("job_name") != job or args.get("extra_metrics") is not True:
        raise Refusal("native_detail_invalid")
    if args.get("forward_to") != ['capsule("storage.Appendable")']:
        raise Refusal("native_detail_invalid")
    if not isinstance(args.get("scrape_protocols"), list) or not args["scrape_protocols"]:
        raise Refusal("native_detail_invalid")
    expected_body = shared_limits.get("body_size_limit_bytes")
    expected_sample = target.get("samples_per_scrape") if target is not None else shared_limits.get("sample_limit")
    expected_limits = {
        "body_size_limit": expected_body,
        "sample_limit": expected_sample,
        "label_limit": shared_limits.get("label_limit"),
        "label_name_length_limit": shared_limits.get("label_name_length_limit"),
        "label_value_length_limit": shared_limits.get("label_value_length_limit"),
    }
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in expected_limits.values()
    ):
        raise Refusal("native_declaration_invalid")
    try:
        actual_limits = {
            "body_size_limit": _parse_alloy_bytes(args["body_size_limit"]),
            "sample_limit": args["sample_limit"],
            "label_limit": args["label_limit"],
            "label_name_length_limit": args["label_name_length_limit"],
            "label_value_length_limit": args["label_value_length_limit"],
        }
    except Refusal:
        raise
    if any(
        isinstance(args[field], bool) or not isinstance(args[field], int)
        for field in ("sample_limit", "label_limit", "label_name_length_limit", "label_value_length_limit")
    ) or actual_limits != expected_limits:
        raise Refusal("native_limits_mismatch")
    if target is None:
        expected_interval, expected_timeout = "15s", "10s"
        interval_default, timeout_default = False, True
        expected_target = {
            "__address__": "127.0.0.1:12345",
            "cluster": shared_limits.get("cluster"),
            "environment": shared_limits.get("environment"),
            "instance": "alloy:12345",
            "service": "o11y/alloy",
            "target_id": "service:o11y",
        }
    else:
        expected_interval = f"{target.get('interval_seconds')}s"
        expected_timeout = f"{target.get('scrape_timeout_seconds')}s"
        interval_default = target.get("interval_seconds") == 60
        timeout_default = target.get("scrape_timeout_seconds") == 10
        module_name = target.get("module_name")
        target_id = target.get("target_id")
        if (
            not isinstance(module_name, str) or not re.fullmatch(r"health_[0-9]{2}", module_name)
            or not isinstance(target_id, str) or not re.fullmatch(r"service:[A-Za-z0-9_.:/@-]{1,120}", target_id)
        ):
            raise Refusal("native_declaration_invalid")
        expected_target = {
            "__address__": "alloy.internal:12345",
            "__meta_component_id": f"prometheus.exporter.blackbox.{module_name}",
            "__meta_component_name": "prometheus.exporter.blackbox",
            "__metrics_path__": f"/api/v0/component/prometheus.exporter.blackbox.{module_name}/metrics",
            "__param_module": module_name,
            "__param_target": target.get("endpoint"),
            "__scheme__": "http",
            "cluster": target.get("cluster"),
            "environment": target.get("environment"),
            "job": f"integrations/blackbox/{target_id}",
            "service": target.get("service_identity"),
            "target_id": target_id,
        }
    if any(not isinstance(value, str) or not value for value in expected_target.values()):
        raise Refusal("native_declaration_invalid")
    if args.get("scrape_interval", "60s" if interval_default else None) != expected_interval:
        raise Refusal("native_limits_mismatch")
    if args.get("scrape_timeout", "10s" if timeout_default else None) != expected_timeout:
        raise Refusal("native_limits_mismatch")
    targets = args["targets"]
    if not isinstance(targets, list) or len(targets) != 1 or not isinstance(targets[0], dict):
        raise Refusal("native_target_mismatch")
    observed_target = targets[0]
    expected_target_keys = set(expected_target)
    if target is not None:
        expected_target_keys.add("instance")
    if (
        set(observed_target) != expected_target_keys
        or any(observed_target.get(key) != value for key, value in expected_target.items())
    ):
        raise Refusal("native_target_mismatch")
    if target is not None:
        instance = observed_target.get("instance")
        hostname_pattern = (
            r"(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)"
            r"(?:\.(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?))*"
        )
        if not isinstance(instance, str) or not re.fullmatch(hostname_pattern, instance):
            raise Refusal("native_target_mismatch")
    return actual_limits, observed_target


def validate_availability_native_details(
    details: dict[str, dict[str, Any]], availability: dict[str, Any],
    shared_limits: dict[str, Any], *, environment: str, cluster: str,
    oldest_allowed: Decimal | None = None, now: int | None = None, self_only: bool = False,
) -> dict[str, Any]:
    """Validate every fixed native component and return endpoint-free budget evidence."""
    targets = availability.get("targets") if isinstance(availability, dict) else None
    limits = availability.get("limits") if isinstance(availability, dict) else None
    if (
        not isinstance(availability, dict)
        or (not self_only and availability.get("enabled") is not True)
        or not isinstance(targets, list)
        or (not self_only and not 1 <= len(targets) <= 64)
        or (self_only and targets != [])
        or not isinstance(limits, dict) or not isinstance(shared_limits, dict)
    ):
        raise Refusal("native_declaration_invalid")
    expected_ids = {"prometheus.scrape.alloy_self"}
    expected_ids.update(
        f"prometheus.exporter.blackbox.{target.get('module_name')}" for target in targets
    )
    expected_ids.update(f"prometheus.scrape.{target.get('module_name')}" for target in targets)
    if set(details) != expected_ids:
        raise Refusal("native_detail_invalid")
    shared = dict(shared_limits)
    shared["environment"] = environment
    shared["cluster"] = cluster
    budgets: list[dict[str, Any]] = []
    for target in targets:
        module_name = target.get("module_name")
        target_id = target.get("target_id")
        exporter_id = f"prometheus.exporter.blackbox.{module_name}"
        scrape_id = f"prometheus.scrape.{module_name}"
        exporter = details[exporter_id]
        if exporter.get("health") != "healthy":
            raise Refusal("native_component_unhealthy")
        arguments = exporter.get("arguments")
        blocks = exporter.get("argument_blocks")
        if not isinstance(arguments, dict) or set(arguments) != {"config"}:
            raise Refusal("native_detail_invalid")
        if not isinstance(blocks, dict) or set(blocks) != {"target"}:
            raise Refusal("native_detail_invalid")
        target_block = blocks["target"]
        expected_labels = {
            "service": target.get("service_identity"), "target_id": target_id,
            "environment": target.get("environment"), "cluster": target.get("cluster"),
            "instance": target_id,
        }
        if target_block != {
            "name": target_id, "address": target.get("endpoint"), "module": module_name,
            "labels": expected_labels,
        }:
            raise Refusal("native_target_mismatch")
        expected_module = _expected_blackbox_module(target)
        loaded_module = _decode_alloy_blackbox_capsule(arguments["config"])
        if loaded_module != expected_module:
            raise Refusal("native_module_mismatch")
        if set(expected_module["modules"]) != {module_name}:
            raise Refusal("native_module_mismatch")
        scrape = details[scrape_id]
        if scrape.get("health") != "healthy" or scrape.get("name") != "prometheus.scrape":
            raise Refusal("native_component_unhealthy")
        if (
            set(scrape.get("references_to", [])) != {
                exporter_id, f"prometheus.relabel.availability_{module_name}",
            }
            or scrape.get("referenced_by") != []
            or exporter.get("references_to") != []
            or exporter.get("referenced_by") != [scrape_id]
        ):
            raise Refusal("native_detail_invalid")
        target_limits, _scrape_target = _alloy_scrape_arguments(
            scrape, job="o11y-availability", target=target,
            shared_limits=shared, self_scrape=False,
        )
        debug = scrape.get("debug_info")
        if not isinstance(debug, dict) or set(debug) != {"target"}:
            raise Refusal("native_debug_unavailable")
        observed = debug["target"]
        if (
            observed.get("job") != "o11y-availability"
            or observed.get("health") not in {"up", "down", "unknown"}
            or observed.get("labels", {}).get("job") != f"integrations/blackbox/{target_id}"
            or observed.get("labels", {}).get("target_id") != target_id
            or observed.get("labels", {}).get("service") != target.get("service_identity")
            or observed.get("labels", {}).get("environment") != target.get("environment")
            or observed.get("labels", {}).get("cluster") != target.get("cluster")
        ):
            raise Refusal("native_debug_mismatch")
        last_scrape = _parse_native_time(observed.get("last_scrape"))
        if oldest_allowed is not None and (
            last_scrape <= oldest_allowed or now is None or last_scrape > now
            or now - last_scrape > target.get("freshness_seconds", 0)
        ):
            raise Refusal("native_debug_stale")
        budgets.append({
            "target_id": target_id, "status": "observed",
            "probe_health": observed["health"], "limits": target_limits,
        })
    self_id = "prometheus.scrape.alloy_self"
    self_detail = details[self_id]
    if self_detail.get("health") != "healthy" or self_detail.get("name") != "prometheus.scrape":
        raise Refusal("native_component_unhealthy")
    if (
        self_detail.get("references_to") != ["prometheus.relabel.bounded_labels"]
        or self_detail.get("referenced_by") != []
    ):
        raise Refusal("native_detail_invalid")
    self_limits, _ = _alloy_scrape_arguments(
        self_detail, job="alloy", target=None, shared_limits=shared,
        self_scrape=True,
    )
    self_debug = self_detail.get("debug_info")
    if not isinstance(self_debug, dict) or set(self_debug) != {"target"}:
        raise Refusal("native_debug_unavailable")
    self_observed = self_debug["target"]
    self_labels = self_observed.get("labels")
    if (
        self_observed.get("job") != "alloy"
        or self_observed.get("health") not in {"up", "down", "unknown"}
        or not isinstance(self_labels, dict)
        or any(self_labels.get(key) != value for key, value in {
            "job": "alloy", "instance": "alloy:12345", "service": "o11y/alloy",
            "target_id": "service:o11y", "environment": environment, "cluster": cluster,
        }.items())
    ):
        raise Refusal("native_debug_mismatch")
    self_last_scrape = _parse_native_time(self_observed.get("last_scrape"))
    if oldest_allowed is not None and (
        self_last_scrape <= oldest_allowed or now is None or self_last_scrape > now
        or now - self_last_scrape > 300
    ):
        raise Refusal("native_debug_stale")
    return {
        "status": "observed", "reason": "fixed_native_components_loaded",
        "target_count": len(targets), "targets": budgets,
        "alloy_self": {"status": "observed", "limits": self_limits},
    }


def _parse_native_time(value: Any) -> Decimal:
    if not isinstance(value, str) or len(value) > 64:
        raise Refusal("native_debug_invalid")
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.([0-9]{1,9}))?(Z|[+-]\d{2}:\d{2})", value)
    if match is None:
        raise Refusal("native_debug_invalid")
    try:
        parsed = datetime.fromisoformat(match.group(1) + match.group(3).replace("Z", "+00:00"))
    except ValueError:
        raise Refusal("native_debug_invalid") from None
    if parsed.tzinfo is None:
        raise Refusal("native_debug_invalid")
    fraction = Decimal("0." + match.group(2)) if match.group(2) else Decimal(0)
    return Decimal(int(parsed.timestamp())) + fraction


def _read_alloy_component_detail(
    component_id: str, deadline: float, *, allow_absent: bool = False,
) -> bytes:
    if not re.fullmatch(
        r"prometheus\.(?:exporter\.blackbox\.health_[0-9]{2}|scrape\.(?:health_[0-9]{2}|alloy_self))",
        component_id,
    ):
        raise Refusal("native_declaration_invalid")
    path = AVAILABILITY_COMPONENT_DETAIL_PATH + component_id
    script = (
        "exec 3<>/dev/tcp/127.0.0.1/12345 || exit 10; "
        f"printf 'GET {path} HTTP/1.0\\r\\nHost: localhost\\r\\nAccept: application/json\\r\\n"
        "Connection: close\\r\\n\\r\\n' >&3; "
        "while IFS= read -r line || [[ -n $line ]]; do printf '%s\\n' \"$line\"; done <&3"
    )
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise Refusal("native_readback_timeout")
    raw = _availability_command(
        ["podman", "exec", "o11y-alloy", "bash", "-c", script],
        timeout=min(10, remaining),
    )
    if len(raw) > MAX_AVAILABILITY_DETAIL_BYTES:
        raise Refusal("native_detail_unavailable")
    if allow_absent:
        try:
            status, _headers, _body = _response_headers(raw)
        except Refusal:
            raise Refusal("native_detail_unavailable") from None
        if status == 404:
            raise Refusal("native_source_absent")
    return raw


def _read_alloy_root_components(deadline: float) -> tuple[bytes, bytes]:
    """Read only fixed ready and complete root-component metadata routes."""
    responses = []
    for path in (AVAILABILITY_READY_PATH, AVAILABILITY_COMPONENTS_PATH):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Refusal("native_readback_timeout")
        script = (
            "exec 3<>/dev/tcp/127.0.0.1/12345 || exit 10; "
            f"printf 'GET {path} HTTP/1.0\\r\\nHost: localhost\\r\\nAccept: application/json\\r\\n"
            "Connection: close\\r\\n\\r\\n' >&3; "
            "while IFS= read -r line || [[ -n $line ]]; do printf '%s\\n' \"$line\"; done <&3"
        )
        raw = _availability_command(
            ["podman", "exec", "o11y-alloy", "bash", "-c", script],
            timeout=min(10, remaining),
        )
        if len(raw) > MAX_AVAILABILITY_DETAIL_BYTES:
            raise Refusal("native_detail_unavailable")
        status, headers, body = _response_headers(raw)
        if (
            status != 200
            or headers.get("content-encoding", ["identity"]) != ["identity"]
            or "transfer-encoding" in headers
            or len(headers.get("content-length", [])) > 1
            or (headers.get("content-length") and (
                not DECIMAL.fullmatch(headers["content-length"][0])
                or int(headers["content-length"][0]) != len(body)
            ))
        ):
            raise Refusal("native_detail_unavailable")
        if path == AVAILABILITY_READY_PATH:
            if body != b"Alloy is ready.\n":
                raise Refusal("native_detail_unavailable")
        else:
            responses.append(body)
    if time.monotonic() > deadline or len(responses) != 1:
        raise Refusal("native_readback_timeout")
    return b"Alloy is ready.\n", responses[0]


def _validate_alloy_root_component_list(raw: bytes) -> dict[str, Any]:
    """Validate the complete bounded root slice without following module edges."""
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_AVAILABILITY_DETAIL_BYTES:
        raise Refusal("native_detail_unavailable")
    try:
        rows = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (ValueError, TypeError, json.JSONDecodeError):
        raise Refusal("native_detail_invalid") from None
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_AVAILABILITY_DETAIL_ROWS:
        raise Refusal("native_detail_invalid")

    identities: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != ALLOY_DETAIL_FIELDS:
            raise Refusal("native_detail_invalid")
        string_fields = ("label", "localID", "moduleID", "name", "original", "type")
        if any(not isinstance(row.get(field), str) for field in string_fields):
            raise Refusal("native_detail_invalid")
        component_id, name = row["localID"], row["name"]
        label, health = row["label"], row["health"]
        if (
            row["moduleID"] != "" or row["type"] != "block"
            or name not in ALLOY_ROOT_COMPONENT_NAMES
            or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,160}", component_id)
            or component_id != f"{name}.{label}"
            or not isinstance(row["arguments"], list)
            or not isinstance(row["debugInfo"], list)
            or not isinstance(row["exports"], list)
            or not isinstance(row["referencedBy"], list)
            or not isinstance(row["referencesTo"], list)
            or any(not isinstance(edge, str) or len(edge) > 160 for edge in row["referencedBy"] + row["referencesTo"])
            or not isinstance(health, dict)
            or set(health) != {"state", "message", "updatedTime"}
            or any(not isinstance(health.get(field), str) for field in ("state", "message", "updatedTime"))
        ):
            raise Refusal("native_detail_invalid")
        if component_id in identities:
            raise Refusal("native_detail_invalid")
        identities[component_id] = row

    if not ALLOY_ROOT_COMPONENT_SENTINELS.issubset(identities):
        raise Refusal("native_detail_invalid")
    if any(identities[component_id]["health"]["state"] != "healthy" for component_id in ALLOY_ROOT_COMPONENT_SENTINELS):
        raise Refusal("native_component_unhealthy")
    all_ids = set(identities)
    for row in identities.values():
        if not set(row["referencesTo"]).issubset(all_ids) or not set(row["referencedBy"]).issubset(all_ids):
            raise Refusal("native_detail_invalid")

    forbidden = any(
        row["name"] == "prometheus.exporter.blackbox"
        or row["localID"] == "prometheus.scrape.alloy_self"
        or row["localID"].startswith(("prometheus.scrape.health_", "prometheus.relabel.availability_health_"))
        for row in identities.values()
    )
    if forbidden:
        raise Refusal("native_source_ambiguous")
    return {"status": "observed", "reason": "root_graph_valid", "component_count": len(rows)}


def _require_native_components_absent(availability: dict[str, Any]) -> dict[str, Any]:
    """Check forbidden root components after the caller binds a fresh flat candidate process."""
    if not isinstance(availability, dict) or not isinstance(availability.get("targets"), list):
        raise Refusal("native_declaration_invalid")
    deadline = time.monotonic() + MAX_AVAILABILITY_READBACK_SECONDS
    _ready, component_list = _read_alloy_root_components(deadline)
    graph = _validate_alloy_root_component_list(component_list)
    return {
        "status": "observed", "reason": "native_components_absent",
        "component_count": graph["component_count"],
    }


def read_availability_native_details(
    availability: dict[str, Any], shared_limits: dict[str, Any], *, environment: str, cluster: str,
    process_started_at: Decimal | int | None = None, preapply_bound: int | None = None,
    now: int | None = None, self_only: bool = False,
    deadline: float | None = None, max_total_bytes: int = MAX_AVAILABILITY_READBACK_BYTES,
) -> dict[str, Any]:
    targets = availability.get("targets") if isinstance(availability, dict) else None
    if (
        not isinstance(targets, list)
        or (not self_only and not 1 <= len(targets) <= 64)
        or (self_only and targets != [])
    ):
        raise Refusal("native_declaration_invalid")
    component_ids = ["prometheus.scrape.alloy_self"]
    for target in targets:
        module = target.get("module_name") if isinstance(target, dict) else None
        if not isinstance(module, str) or not re.fullmatch(r"health_[0-9]{2}", module):
            raise Refusal("native_declaration_invalid")
        component_ids.extend((f"prometheus.exporter.blackbox.{module}", f"prometheus.scrape.{module}"))
    if len(set(component_ids)) != len(component_ids):
        raise Refusal("native_declaration_invalid")
    deadline = time.monotonic() + MAX_AVAILABILITY_READBACK_SECONDS if deadline is None else deadline
    if (
        isinstance(max_total_bytes, bool) or not isinstance(max_total_bytes, int)
        or not 1 <= max_total_bytes <= MAX_AVAILABILITY_READBACK_BYTES
        or deadline <= time.monotonic()
    ):
        raise Refusal("native_declaration_invalid")
    details: dict[str, dict[str, Any]] = {}
    consumed = 0
    for component_id in component_ids:
        raw = _read_alloy_component_detail(
            component_id, deadline, allow_absent=(component_id == "prometheus.scrape.alloy_self"),
        )
        consumed += len(raw)
        if consumed > max_total_bytes:
            raise Refusal("native_detail_unavailable")
        name = "prometheus.exporter.blackbox" if ".exporter." in component_id else "prometheus.scrape"
        details[component_id] = _alloy_component_detail(raw, component_id, name)
    if time.monotonic() > deadline:
        raise Refusal("native_readback_timeout")
    if (process_started_at is None) != (now is None):
        raise Refusal("native_declaration_invalid")
    oldest_allowed = None
    if process_started_at is not None:
        if (
            isinstance(process_started_at, bool) or not isinstance(process_started_at, (int, Decimal))
            or process_started_at <= 0 or isinstance(now, bool) or not isinstance(now, int) or now <= 0
            or (preapply_bound is not None and (
                isinstance(preapply_bound, bool) or not isinstance(preapply_bound, int) or preapply_bound <= 0
            ))
        ):
            raise Refusal("native_declaration_invalid")
        freshness_values = [target.get("freshness_seconds") for target in targets] or [300]
        if any(
            isinstance(value, bool) or not isinstance(value, int) or not 60 <= value <= 86400
            for value in freshness_values
        ):
            raise Refusal("native_declaration_invalid")
        oldest_allowed = max(
            Decimal(process_started_at), Decimal(preapply_bound or 0), Decimal(now - min(freshness_values)),
        )
    projection = validate_availability_native_details(
        details, availability, shared_limits, environment=environment, cluster=cluster,
        oldest_allowed=oldest_allowed, now=now, self_only=self_only,
    )
    return {**projection, "_detail_bytes": consumed}


def _availability_query_path(
    metric_names: tuple[str, ...], *, job: str, environment: str, cluster: str,
    instance: str | None = None, target_id: str | None = None, timestamp: bool = False,
) -> str:
    if (
        not metric_names or len(metric_names) > 6
        or any(
            not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z_:][a-zA-Z0-9_:]*", name)
            for name in metric_names
        )
        or not isinstance(job, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", job)
        or not isinstance(environment, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{1,31}", environment)
        or not isinstance(cluster, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", cluster)
        or (instance is not None and (not isinstance(instance, str) or not SAFE_IDENTITY_VALUE.fullmatch(instance)))
        or (target_id is not None and (not isinstance(target_id, str) or not SAFE_IDENTITY_VALUE.fullmatch(target_id)))
    ):
        raise Refusal("native_declaration_invalid")
    matchers = [f"__name__=~{json.dumps('^(?:' + '|'.join(metric_names) + ')$')}"]
    matchers.extend((
        f"job={json.dumps(job)}", f"environment={json.dumps(environment)}",
        f"cluster={json.dumps(cluster)}",
    ))
    if instance is not None:
        matchers.append(f"instance={json.dumps(instance)}")
    if target_id is not None:
        matchers.append(f"target_id={json.dumps(target_id)}")
    selector = "{" + ",".join(matchers) + "}"
    expression = f"timestamp({selector})" if timestamp else selector
    return PROMETHEUS + "/api/v1/query?" + urlencode({"query": expression})


def _native_metric_rows(
    raw: bytes, *, families: set[str], expected: dict[str, dict[str, str]],
) -> dict[tuple[str, str], Decimal]:
    result: dict[tuple[str, str], Decimal] = {}
    for row in _api_vector(raw, "native_samples_invalid"):
        if not isinstance(row, dict) or not isinstance(row.get("metric"), dict):
            raise Refusal("native_samples_invalid")
        labels = row["metric"]
        family = labels.get("__name__")
        target_id = labels.get("target_id")
        if not isinstance(family, str) or family not in families or not isinstance(target_id, str):
            raise Refusal("native_samples_invalid")
        identity = expected.get(target_id)
        if identity is None:
            raise Refusal("native_samples_ambiguous")
        if labels != {"__name__": family, **identity}:
            raise Refusal("native_samples_ambiguous")
        sample = row.get("value")
        if not isinstance(sample, list) or len(sample) != 2 or isinstance(sample[0], bool):
            raise Refusal("native_samples_invalid")
        try:
            value = Decimal(str(sample[1]))
        except (InvalidOperation, ValueError):
            raise Refusal("native_samples_invalid") from None
        if not value.is_finite() or value < 0:
            raise Refusal("native_samples_invalid")
        key = (target_id, family)
        if key in result:
            raise Refusal("native_samples_ambiguous")
        result[key] = value
    return result


def collect_availability_native_samples(
    availability: dict[str, Any], shared_limits: dict[str, Any], *, environment: str, cluster: str,
    process_started_at: Decimal | int, preapply_bound: int | None = None,
    now: int | None = None, freshness_seconds: int = 300, self_only: bool = False,
    deadline: float | None = None, max_total_bytes: int = MAX_API_BYTES,
) -> dict[str, Any]:
    """Read bounded raw metric timestamps and values for the fixed Alloy producer."""
    targets = availability.get("targets") if isinstance(availability, dict) else None
    if (
        not isinstance(availability, dict)
        or (not self_only and availability.get("enabled") is not True)
        or not isinstance(targets, list)
        or (not self_only and not 1 <= len(targets) <= 64)
        or (self_only and targets != [])
        or not isinstance(environment, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{1,31}", environment)
        or not isinstance(cluster, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", cluster)
        or isinstance(process_started_at, bool)
        or not isinstance(process_started_at, (int, Decimal)) or process_started_at <= 0
        or (preapply_bound is not None and (
            isinstance(preapply_bound, bool) or not isinstance(preapply_bound, int) or preapply_bound <= 0
        ))
        or isinstance(freshness_seconds, bool)
        or not isinstance(freshness_seconds, int) or not 60 <= freshness_seconds <= 86400
    ):
        raise Refusal("native_declaration_invalid")
    now = int(time.time()) if now is None else now
    if isinstance(now, bool) or not isinstance(now, int) or now <= 0:
        raise Refusal("native_declaration_invalid")
    oldest_allowed = max(Decimal(process_started_at), Decimal(preapply_bound or 0), Decimal(now - freshness_seconds))
    self_identity = {
        "job": "alloy", "instance": "alloy:12345", "service": "o11y/alloy",
        "cluster": cluster, "environment": environment, "target_id": "service:o11y",
    }
    target_identities: dict[str, dict[str, str]] = {}
    for target in targets:
        if not isinstance(target, dict):
            raise Refusal("native_declaration_invalid")
        target_id = target.get("target_id")
        expected = {
            "job": "o11y-availability", "instance": target_id,
            "service": target.get("service_identity"), "cluster": target.get("cluster"),
            "environment": target.get("environment"), "target_id": target_id,
        }
        if (
            not isinstance(target_id, str) or target_id in target_identities
            or target.get("environment") != environment or target.get("cluster") != cluster
            or any(not isinstance(value, str) or not value for value in expected.values())
        ):
            raise Refusal("native_declaration_invalid")
        target_identities[target_id] = expected

    value_families = set(ALLOY_HEALTH_METRICS)
    self_families = set(ALLOY_SELF_METRICS)
    self_timestamp_path = _availability_query_path(
        ALLOY_SELF_METRICS, job="alloy", environment=environment, cluster=cluster,
        instance="alloy:12345", target_id="service:o11y", timestamp=True,
    ).removeprefix(PROMETHEUS)
    self_value_path = _availability_query_path(
        ALLOY_SELF_METRICS, job="alloy", environment=environment, cluster=cluster,
        instance="alloy:12345", target_id="service:o11y",
    ).removeprefix(PROMETHEUS)
    if targets:
        timestamp_path = _availability_query_path(
            ALLOY_HEALTH_METRICS, job="o11y-availability", environment=environment,
            cluster=cluster, timestamp=True,
        ).removeprefix(PROMETHEUS)
        values_path = _availability_query_path(
            ALLOY_HEALTH_METRICS, job="o11y-availability", environment=environment,
            cluster=cluster,
        ).removeprefix(PROMETHEUS)
        query_paths = [timestamp_path, values_path, self_timestamp_path, self_value_path]
    else:
        query_paths = [self_timestamp_path, self_value_path]
    query_responses = _run_native_api_batch(
        query_paths, deadline=deadline, max_total_bytes=max_total_bytes,
    )
    health_values: dict[tuple[str, str], Decimal] = {}
    health_timestamps: dict[tuple[str, str], Decimal] = {}
    if targets:
        health_timestamps = _native_metric_rows(
            query_responses[0], families=value_families, expected=target_identities,
        )
        health_values = _native_metric_rows(
            query_responses[1], families=value_families, expected=target_identities,
        )
        self_timestamp_raw, self_values_raw = query_responses[2:]
    else:
        self_timestamp_raw, self_values_raw = query_responses
    self_expected = {"service:o11y": self_identity}
    self_timestamps = _native_metric_rows(
        self_timestamp_raw,
        families=self_families, expected=self_expected,
    )
    self_values = _native_metric_rows(
        self_values_raw,
        families=self_families, expected=self_expected,
    )

    def require_fresh(
        values: dict[tuple[str, str], Decimal], timestamps: dict[tuple[str, str], Decimal],
        target_id: str, required: tuple[str, ...],
    ) -> dict[str, Decimal]:
        selected: dict[str, Decimal] = {}
        for family in required:
            key = (target_id, family)
            value, sample_time = values.get(key), timestamps.get(key)
            if value is None or sample_time is None:
                raise Refusal("native_samples_missing")
            if sample_time <= oldest_allowed or sample_time > now:
                raise Refusal("native_samples_stale")
            selected[family] = value
        return selected

    self_values_by_family = require_fresh(self_values, self_timestamps, "service:o11y", ("up",))
    if self_values_by_family["up"] not in {Decimal(0), Decimal(1)}:
        raise Refusal("native_samples_invalid")
    self_up = int(self_values_by_family["up"])
    self_receipt = {"status": "observed", "scrape_up": self_up}
    if self_up == 1:
        self_values_by_family = require_fresh(
            self_values, self_timestamps, "service:o11y", ALLOY_SELF_METRICS,
        )
        self_receipt.update({
            "scrape_samples_scraped": _bounded_native_counter(
                self_values_by_family["scrape_samples_scraped"], shared_limits.get("sample_limit")
            ),
            "scrape_body_size_bytes": _bounded_native_counter(
                self_values_by_family["scrape_body_size_bytes"], shared_limits.get("body_size_limit_bytes")
            ),
        })
    health_receipts: list[dict[str, Any]] = []
    expected_keys: set[tuple[str, str]] = set()
    for target in targets:
        target_id = target["target_id"]
        up_key = (target_id, "up")
        if up_key not in health_values or up_key not in health_timestamps:
            raise Refusal("native_samples_missing")
        up_values = require_fresh(health_values, health_timestamps, target_id, ("up",))
        if up_values["up"] not in {Decimal(0), Decimal(1)}:
            raise Refusal("native_samples_invalid")
        if up_values["up"] == 0:
            health_receipts.append({"target_id": target_id, "status": "scrape_failed"})
            expected_keys.update((target_id, family) for family in ALLOY_HEALTH_METRICS)
            continue
        values = require_fresh(health_values, health_timestamps, target_id, ALLOY_HEALTH_METRICS)
        probe_success = values["probe_success"]
        if probe_success not in {Decimal(0), Decimal(1)}:
            raise Refusal("native_samples_invalid")
        body = _bounded_native_counter(
            values["scrape_body_size_bytes"], shared_limits.get("body_size_limit_bytes")
        )
        samples = _bounded_native_counter(
            values["scrape_samples_scraped"], target.get("samples_per_scrape")
        )
        status_code = values["probe_http_status_code"]
        if status_code != status_code.to_integral_value() or not 0 <= status_code <= 599:
            raise Refusal("native_samples_invalid")
        duration = values["probe_duration_seconds"]
        if duration > Decimal(str(target["probe_timeout_seconds"])):
            raise Refusal("native_samples_invalid")
        if probe_success == 1 and int(status_code) not in target["expected_status_codes"]:
            raise Refusal("native_samples_invalid")
        health_receipts.append({
            "target_id": target_id, "status": "observed",
            "scrape_up": 1, "probe_success": int(probe_success),
            "probe_http_status_code": int(status_code),
            "probe_duration_seconds": str(duration),
            "scrape_samples_scraped": samples, "scrape_body_size_bytes": body,
        })
        expected_keys.update((target_id, family) for family in ALLOY_HEALTH_METRICS)
    unexpected_values = set(health_values) - expected_keys
    unexpected_timestamps = set(health_timestamps) - expected_keys
    if unexpected_values or unexpected_timestamps:
        raise Refusal("native_samples_ambiguous")
    return {
        "status": "observed", "reason": "fresh_native_samples",
        "freshness_seconds": freshness_seconds,
        "alloy_self": self_receipt, "targets": health_receipts,
    }


def read_availability_native_source(
    availability: dict[str, Any], shared_limits: dict[str, Any], *, environment: str, cluster: str,
    process_started_at: Decimal | int, preapply_bound: int | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    """Join loaded native DETAIL and fresh samples under one aggregate read budget."""
    now = int(time.time()) if now is None else now
    if not isinstance(availability, dict) or not isinstance(availability.get("targets"), list):
        raise Refusal("native_declaration_invalid")
    targets = availability["targets"]
    self_only = not targets
    freshness = min(
        [target.get("freshness_seconds", 300) for target in targets if isinstance(target, dict)]
        or [300]
    )
    deadline = time.monotonic() + MAX_AVAILABILITY_READBACK_SECONDS
    detail = read_availability_native_details(
        availability, shared_limits, environment=environment, cluster=cluster,
        process_started_at=process_started_at, preapply_bound=preapply_bound,
        now=now, self_only=self_only, deadline=deadline,
    )
    detail_bytes = detail.pop("_detail_bytes", None)
    if (
        isinstance(detail_bytes, bool) or not isinstance(detail_bytes, int)
        or not 0 < detail_bytes < MAX_AVAILABILITY_READBACK_BYTES
    ):
        raise Refusal("native_detail_unavailable")
    samples = collect_availability_native_samples(
        availability, shared_limits, environment=environment, cluster=cluster,
        process_started_at=process_started_at, preapply_bound=preapply_bound,
        now=now, freshness_seconds=freshness, self_only=self_only,
        deadline=deadline, max_total_bytes=MAX_AVAILABILITY_READBACK_BYTES - detail_bytes,
    )
    if samples.get("alloy_self", {}).get("scrape_up") != 1:
        raise Refusal("native_samples_invalid")
    return {
        "status": "observed", "source": "alloy",
        "detail": detail, "samples": samples,
    }


def _bounded_native_counter(value: Any, maximum: Any) -> int:
    if (
        not isinstance(value, Decimal) or value != value.to_integral_value() or value < 1
        or isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 1 or value > maximum
    ):
        raise Refusal("native_samples_invalid")
    return int(value)


def _availability_self_identity(environment: str, cluster: str) -> dict[str, str]:
    if (
        not isinstance(environment, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{1,31}", environment)
        or not isinstance(cluster, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", cluster)
    ):
        raise Refusal("native_declaration_invalid")
    return {
        "job": "alloy", "instance": "alloy:12345", "service": "o11y/alloy",
        "target_id": "service:o11y", "environment": environment, "cluster": cluster,
    }


def _active_prometheus_self_target(
    targets_raw: bytes | str, *, environment: str, cluster: str,
) -> dict[str, Any]:
    response = _json_document(targets_raw, "native_source_unavailable")
    data = response.get("data")
    active = data.get("activeTargets") if isinstance(data, dict) else None
    if not isinstance(active, list) or len(active) > MAX_ROWS:
        raise Refusal("native_source_unavailable")
    identity = _availability_self_identity(environment, cluster)
    matches = []
    for target in active:
        if not isinstance(target, dict) or not isinstance(target.get("labels"), dict):
            raise Refusal("native_source_unavailable")
        labels = target["labels"]
        if labels.get("job") == "alloy" and labels.get("instance") == "alloy:12345":
            if labels != identity:
                raise Refusal("native_source_ambiguous")
            matches.append(target)
    if len(matches) != 1:
        raise Refusal("native_source_absent" if not matches else "native_source_ambiguous")
    return matches[0]


def _validate_prometheus_alloy_loaded_limits(
    readback: dict[str, Any], shared_limits: dict[str, Any], *, environment: str, cluster: str,
) -> None:
    if (
        not isinstance(readback, dict) or readback.get("status") != "observed"
        or readback.get("reason") != "stable_loaded_config"
        or not isinstance(readback.get("config_yaml"), str)
    ):
        raise Refusal("native_source_limits_unavailable")
    try:
        documents = list(yaml.load_all(readback["config_yaml"], Loader=_SourceUniqueKeyLoader))
    except (UnicodeError, ValueError, yaml.YAMLError):
        raise Refusal("native_source_limits_unavailable") from None
    if len(documents) != 1 or not isinstance(documents[0], dict):
        raise Refusal("native_source_limits_unavailable")
    jobs = documents[0].get("scrape_configs")
    alloy = (
        [job for job in jobs if isinstance(job, dict) and job.get("job_name") == "alloy"]
        if isinstance(jobs, list) else []
    )
    if len(alloy) != 1:
        raise Refusal("native_source_limits_unavailable")
    job = alloy[0]
    expected = {
        "body_size_limit": shared_limits.get("body_size_limit_bytes"),
        "sample_limit": shared_limits.get("sample_limit"),
        "label_limit": shared_limits.get("label_limit"),
        "label_name_length_limit": shared_limits.get("label_name_length_limit"),
        "label_value_length_limit": shared_limits.get("label_value_length_limit"),
    }
    try:
        body_limit = _parse_alloy_bytes(job.get("body_size_limit"))
    except Refusal:
        raise Refusal("native_source_limits_unavailable") from None
    loaded = {"body_size_limit": body_limit}
    for field in ("sample_limit", "label_limit", "label_name_length_limit", "label_value_length_limit"):
        value = job.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise Refusal("native_source_limits_unavailable")
        loaded[field] = value
    if loaded != expected:
        raise Refusal("native_source_limits_mismatch")
    static = job.get("static_configs")
    if not isinstance(static, list) or len(static) != 1 or not isinstance(static[0], dict):
        raise Refusal("native_source_limits_unavailable")
    static_identity = _availability_self_identity(environment, cluster)
    static_labels = {
        key: static_identity[key] for key in ("service", "target_id", "cluster", "environment")
    }
    if static[0].get("targets") != ["alloy:12345"] or static[0].get("labels") != static_labels:
        raise Refusal("native_source_limits_mismatch")


def collect_prometheus_self_source(
    targets_raw: bytes | str, loaded_readback: dict[str, Any], shared_limits: dict[str, Any], *,
    environment: str, cluster: str, now: int | None = None, lower_bound: int | None = None,
    freshness_seconds: int = 300,
) -> dict[str, Any]:
    """Prove the legacy Prometheus producer without claiming an absent body counter."""
    now = int(time.time()) if now is None else now
    if (
        isinstance(now, bool) or not isinstance(now, int) or now <= 0
        or (
            lower_bound is not None and (
                isinstance(lower_bound, bool) or not isinstance(lower_bound, int) or lower_bound <= 0
            )
        )
        or isinstance(freshness_seconds, bool) or not isinstance(freshness_seconds, int)
        or not 60 <= freshness_seconds <= 86400
    ):
        raise Refusal("native_declaration_invalid")
    target = _active_prometheus_self_target(targets_raw, environment=environment, cluster=cluster)
    last_scrape = _parse_native_time(target.get("lastScrape"))
    if (
        target.get("health") != "up" or target.get("scrapeUrl") != "http://alloy:12345/metrics"
        or last_scrape > now or now - last_scrape > freshness_seconds
        or (lower_bound is not None and last_scrape <= lower_bound)
    ):
        raise Refusal("native_source_stale")
    identity = _availability_self_identity(environment, cluster)
    _validate_prometheus_alloy_loaded_limits(
        loaded_readback, shared_limits, environment=environment, cluster=cluster,
    )
    families = ("up", "scrape_samples_scraped")
    values_path = _availability_query_path(
        families, job="alloy", environment=environment, cluster=cluster,
        instance="alloy:12345", target_id="service:o11y",
    ).removeprefix(PROMETHEUS)
    timestamp_path = _availability_query_path(
        families, job="alloy", environment=environment, cluster=cluster,
        instance="alloy:12345", target_id="service:o11y", timestamp=True,
    ).removeprefix(PROMETHEUS)
    timestamp_raw, values_raw = _run_native_api_batch([timestamp_path, values_path])
    timestamps = _native_metric_rows(timestamp_raw, families=set(families), expected={"service:o11y": identity})
    values = _native_metric_rows(values_raw, families=set(families), expected={"service:o11y": identity})
    oldest_allowed = max(Decimal(now - freshness_seconds), Decimal(lower_bound or 0))
    projection: dict[str, int] = {}
    for family in families:
        key = ("service:o11y", family)
        sample, raw_timestamp = values.get(key), timestamps.get(key)
        if sample is None or raw_timestamp is None:
            raise Refusal("native_samples_missing")
        if raw_timestamp <= oldest_allowed or raw_timestamp > now:
            raise Refusal("native_samples_stale")
        if family == "up":
            if sample != 1:
                raise Refusal("native_samples_invalid")
            projection[family] = 1
        else:
            projection[family] = _bounded_native_counter(sample, shared_limits.get("sample_limit"))
    return {
        "status": "observed", "source": "prometheus",
        "alloy_self": projection,
        "scrape_body_size_bytes": {"status": "unavailable", "reason": "legacy_extra_scrape_metrics_not_enabled"},
    }


def _current_alloy_identity() -> tuple[dict[str, Any], dict[str, Any]]:
    provider_context = _availability_provider_context()
    identities = provider_context[5]
    podman_identity = identities.get("podman") if isinstance(identities, dict) else None
    podman_path = podman_identity.get("path") if isinstance(podman_identity, dict) else None
    if not isinstance(podman_path, str) or not os.path.isabs(podman_path):
        raise Refusal("native_source_unavailable")
    records = _availability_inspect_all(
        time.monotonic() + MAX_AVAILABILITY_INSPECT_SECONDS, podman_path,
    )
    candidates = [record for record in records if record.get("service") == "alloy"]
    if len(candidates) != 1:
        raise Refusal("native_source_unavailable")
    record = candidates[0]
    if not record.get("running") or not record.get("started_at") or not record.get("pid"):
        raise Refusal("native_source_unavailable")
    process = _availability_process_identity(record["pid"])
    _parse_native_time(record["started_at"])
    return record, process


def collect_current_availability_source(
    targets_raw: bytes | str, loaded_readback: dict[str, Any], availability: dict[str, Any],
    shared_limits: dict[str, Any], *, environment: str, cluster: str,
    intended_source: str, now: int | None = None, lower_bound: int | None = None,
    freshness_seconds: int = 300, require_health_targets: bool = False,
    candidate_config_sha256: str | None = None, previous_source: str | None = None,
    previous_container_sha256: str | None = None,
) -> dict[str, Any]:
    """Prove exactly one actual producer and label the reviewed source transition."""
    if intended_source not in EXPECTED_SOURCE_VALUES:
        raise Refusal("native_declaration_invalid")
    now = int(time.time()) if now is None else now
    try:
        _active_prometheus_self_target(targets_raw, environment=environment, cluster=cluster)
        prometheus_present = True
    except Refusal as error:
        if error.category == "native_source_absent":
            prometheus_present = False
        else:
            raise

    before_record, before_process = _current_alloy_identity()
    started_at = _parse_native_time(before_record["started_at"])
    native_projection: dict[str, Any] | None
    try:
        # Producer identity is the fixed Alloy self-scrape. Candidate health
        # components are required only after apply, when their loaded config exists.
        native_projection = read_availability_native_source(
            {"enabled": False, "targets": [], "limits": availability.get("limits", {})},
            shared_limits, environment=environment, cluster=cluster,
            process_started_at=started_at, preapply_bound=lower_bound, now=now,
        )
        native_present = True
    except Refusal as error:
        if error.category == "native_source_absent":
            native_projection = None
            native_present = False
        else:
            raise
    after_record, after_process = _current_alloy_identity()
    stable_fields = ("id", "image", "pid", "started_at", "running", "network_mode", "ports", "project", "service")
    if (
        any(before_record.get(field) != after_record.get(field) for field in stable_fields)
        or before_process != after_process
    ):
        raise Refusal("native_source_unavailable")
    if prometheus_present and native_present:
        raise Refusal("native_source_ambiguous")
    if not prometheus_present and not native_present:
        raise Refusal("native_source_absent")
    container_sha256 = hashlib.sha256(before_record["id"].encode("ascii")).hexdigest()
    config_binding: str | None = None
    current_source = "prometheus" if prometheus_present else "alloy"
    if require_health_targets and (
        availability.get("enabled") is True or intended_source == "alloy" or previous_source == "alloy"
    ):
        if candidate_config_sha256 is None:
            raise Refusal("native_source_unavailable")
        if (
            previous_source is not None and previous_source != current_source
            and (
                lower_bound is None or previous_container_sha256 is None
                or container_sha256 == previous_container_sha256 or started_at <= lower_bound
            )
        ):
            raise Refusal("native_source_unavailable")
        if lower_bound is not None and started_at <= lower_bound:
            raise Refusal("native_source_unavailable")
        config_binding = _verify_loaded_candidate_config(
            before_record, before_process, candidate_config_sha256,
            availability_enabled=availability.get("enabled") is True,
            lower_bound=lower_bound,
        )
    if prometheus_present:
        source_projection = collect_prometheus_self_source(
            targets_raw, loaded_readback, shared_limits,
            environment=environment, cluster=cluster, now=now,
            lower_bound=lower_bound, freshness_seconds=freshness_seconds,
        )
        if require_health_targets and previous_source == "alloy":
            if config_binding is None or lower_bound is None or previous_container_sha256 is None:
                raise Refusal("native_source_unavailable")
            source_projection["native_components"] = _require_native_components_absent(availability)
        source = "prometheus"
    else:
        assert native_projection is not None
        if require_health_targets and availability.get("enabled") is True:
            source_projection = read_availability_native_source(
                availability, shared_limits, environment=environment, cluster=cluster,
                process_started_at=started_at, preapply_bound=lower_bound, now=now,
            )
        else:
            source_projection = native_projection
        source = "alloy"
    if config_binding is not None:
        final_record, final_process = _current_alloy_identity()
        if (
            final_record.get("id") != before_record.get("id")
            or final_record.get("image") != before_record.get("image")
            or final_record.get("pid") != before_record.get("pid")
            or final_record.get("started_at") != before_record.get("started_at")
            or final_process != before_process
            or _verify_loaded_candidate_config(
                final_record, final_process, candidate_config_sha256,
                availability_enabled=availability.get("enabled") is True,
                lower_bound=lower_bound,
            ) != config_binding
        ):
            raise Refusal("native_source_unavailable")
    return {
        "status": "observed", "source": source, "intended_source": intended_source,
        "matches_intended": source == intended_source, "evidence": source_projection,
        "container_identity_sha256": container_sha256,
    }


def _require_curl_version() -> None:
    try:
        completed = subprocess.run(
            [ENGINE, "exec", CONTAINER, "curl", "--disable", "--version"],
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise Refusal("curl_unsupported") from None
    match = VERSION.search(completed.stdout) if completed.returncode == 0 else None
    if match is None or tuple(int(piece) for piece in match.groups()) < CURL_MIN_VERSION:
        raise Refusal("curl_unsupported")


def collect() -> dict[str, Any]:
    _require_curl_version()
    targets_raw = _run_api("/api/v1/targets?state=active")
    samples_raw = _run_api("/api/v1/query?query=max_over_time%28scrape_samples_scraped%5B24h%5D%29")
    series_end = int(time.time())
    baseline = summarize(
        targets_raw,
        samples_raw,
        _run_probe,
        _run_series_query,
        series_end - 86400,
        series_end,
    )
    try:
        loaded_limits = _loaded_limit_readback(targets_raw)
    except Refusal as error:
        loaded_limits = {"status": "unavailable", "reason": error.category}
    except Exception:
        loaded_limits = {"status": "unavailable", "reason": "loaded_readback_unavailable"}
    source_context = None
    source_result = None
    source_text = os.environ.get("O11Y_AVAILABILITY_SOURCE_JSON", "")
    if source_text:
        try:
            source_context = _validate_availability_source_payload(_json_input_bytes(source_text))
            source_result = collect_current_availability_source(
                targets_raw, loaded_limits, source_context["availability"], source_context["shared_limits"],
                environment=source_context["environment"], cluster=source_context["cluster"],
                intended_source=source_context["intended_source"],
                lower_bound=source_context["lower_bound"],
                freshness_seconds=source_context["freshness_seconds"],
                require_health_targets=source_context["require_health_targets"],
                candidate_config_sha256=source_context["candidate_config_sha256"],
                previous_source=source_context["previous_source"],
                previous_container_sha256=source_context["previous_container_sha256"],
            )
        except Refusal as error:
            source_context = source_context or {"emit": True}
            source_result = {
                "status": "unavailable", "reason": error.category,
                "source": "unknown", "intended_source": "unknown", "matches_intended": False,
            }
        except Exception:
            source_context = source_context or {"emit": True}
            source_result = {
                "status": "unavailable", "reason": "native_source_unavailable",
                "source": "unknown", "intended_source": "unknown", "matches_intended": False,
            }
    try:
        alert_inputs = _alert_input_availability(
            targets_raw,
            native_alloy_verified=(
                isinstance(source_result, dict) and source_result.get("status") == "observed"
                and source_result.get("source") == "alloy"
            ),
        )
    except Exception:
        alert_inputs = {
            "status": "observed",
            "families": [
                {"family": metric_name, "status": "unavailable", "reason": "api_unavailable"}
                for metric_name, _ in ALERT_INPUTS
            ],
        }
    try:
        source_limits = _source_scrape_limit_readback(os.environ.get("O11Y_SCRAPE_CONFIG_DIR", ""))
    except Refusal as error:
        source_limits = {"status": "unavailable", "reason": error.category}
    except Exception:
        source_limits = {"status": "unavailable", "reason": "source_readback_unavailable"}
    result = {
        "baseline": baseline,
        "loaded_limit_readback": loaded_limits,
        "source_limit_readback": source_limits,
        "alert_input_availability": alert_inputs,
    }
    if source_context is not None:
        result["native_availability_source"] = source_result
    return result


def _input_document() -> dict[str, Any]:
    raw = sys.stdin.buffer.read(1_048_577)
    if len(raw) > 1_048_576:
        raise Refusal("invalid_declaration")
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (ValueError, TypeError, json.JSONDecodeError):
        raise Refusal("invalid_declaration") from None
    if not isinstance(value, dict):
        raise Refusal("invalid_declaration")
    return value


def _json_input_bytes(raw: str) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw or len(raw.encode("utf-8")) > 1_048_576:
        raise Refusal("native_declaration_invalid")
    try:
        value = json.loads(
            raw, object_pairs_hook=_unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        raise Refusal("native_declaration_invalid") from None
    if not isinstance(value, dict):
        raise Refusal("native_declaration_invalid")
    return value


def _validate_availability_source_payload(payload: dict[str, Any]) -> dict[str, Any]:
    required = {
        "reviewed", "targets", "availability", "shared_limits", "environment", "cluster",
        "lower_bound", "freshness_seconds", "require_health_targets",
    }
    if (
        not required.issubset(payload)
        or set(payload) - required - {
            "candidate_config_sha256", "previous_source", "previous_container_sha256",
        }
        or payload.get("reviewed") is not True
        or not isinstance(payload.get("require_health_targets"), bool)
    ):
        raise Refusal("native_declaration_invalid")
    candidate_sha256 = payload.get("candidate_config_sha256")
    if candidate_sha256 not in (None, "") and (
        not isinstance(candidate_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", candidate_sha256)
    ):
        raise Refusal("native_declaration_invalid")
    previous_source = payload.get("previous_source")
    if previous_source not in (None, "prometheus", "alloy"):
        raise Refusal("native_declaration_invalid")
    previous_container_sha256 = payload.get("previous_container_sha256")
    if previous_container_sha256 is not None and (
        not isinstance(previous_container_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", previous_container_sha256)
    ):
        raise Refusal("native_declaration_invalid")
    targets, _retired = validate_expected_target_declaration(
        payload.get("targets"), reviewed=payload.get("reviewed"),
    )
    self_rows = [row for row in targets if row.get("job") == "alloy" and row.get("instance") == "alloy:12345"]
    if len(self_rows) > 1:
        raise Refusal("native_declaration_invalid")
    environment, cluster = payload.get("environment"), payload.get("cluster")
    availability, shared_limits = payload.get("availability"), payload.get("shared_limits")
    if (
        not isinstance(availability, dict) or not isinstance(shared_limits, dict)
        or not isinstance(environment, str) or not isinstance(cluster, str)
    ):
        raise Refusal("native_declaration_invalid")
    intended_source = self_rows[0].get("source", "prometheus") if self_rows else "prometheus"
    if self_rows and (
        self_rows[0].get("service") != "o11y/alloy"
        or self_rows[0].get("environment") != environment
        or self_rows[0].get("cluster") != cluster
    ):
        raise Refusal("native_declaration_invalid")
    freshness = payload.get("freshness_seconds")
    if isinstance(freshness, bool) or not isinstance(freshness, int) or not 60 <= freshness <= 86400:
        raise Refusal("native_declaration_invalid")
    lower_bound = payload.get("lower_bound")
    if lower_bound is not None and (
        isinstance(lower_bound, bool) or not isinstance(lower_bound, int) or lower_bound <= 0
    ):
        raise Refusal("native_declaration_invalid")
    return {
        "availability": availability, "shared_limits": shared_limits,
        "environment": environment, "cluster": cluster,
        "intended_source": intended_source, "freshness_seconds": freshness,
        "lower_bound": lower_bound, "require_health_targets": payload["require_health_targets"],
        "candidate_config_sha256": candidate_sha256 or None, "previous_source": previous_source,
        "previous_container_sha256": previous_container_sha256,
    }


def _expected_target_coverage_action() -> dict[str, Any]:
    declaration = _input_document()
    if declaration.get("reviewed") is not True:
        return expected_target_coverage(b"", declaration)
    try:
        validate_expected_target_declaration(
            declaration.get("targets"), reviewed=declaration.get("reviewed")
        )
    except Refusal:
        return expected_target_coverage(b"", declaration)
    try:
        targets_raw = _run_api("/api/v1/targets?state=active", "api_unavailable")
    except Refusal:
        return {
            "status": "unavailable",
            "reason": "api_unavailable",
            "expected_count": 0,
            "active_target_count": 0,
            "matched_count": 0,
            "missing_count": 0,
            "unexpected_count": 0,
            "unhealthy_count": 0,
        }
    return expected_target_coverage(targets_raw, declaration)


def _availability_source_action() -> dict[str, Any]:
    source_context = _validate_availability_source_payload(_input_document())
    environment, cluster = source_context["environment"], source_context["cluster"]
    targets_raw = _run_api("/api/v1/targets?state=active", "native_source_unavailable")
    loaded = _loaded_limit_readback(targets_raw)
    return collect_current_availability_source(
        targets_raw, loaded, source_context["availability"], source_context["shared_limits"],
        environment=environment, cluster=cluster, intended_source=source_context["intended_source"],
        lower_bound=source_context["lower_bound"], freshness_seconds=source_context["freshness_seconds"],
        require_health_targets=source_context["require_health_targets"],
        candidate_config_sha256=source_context["candidate_config_sha256"],
        previous_source=source_context["previous_source"],
        previous_container_sha256=source_context["previous_container_sha256"],
    )


def main() -> int:
    if sys.argv[1:2] == ["--verify-availability-namespace"] and sys.argv[2:] in ([], ["--candidate"]):
        try:
            result = verify_availability_namespace(validate_candidate=sys.argv[2:] == ["--candidate"])
            code = 0
        except Exception:
            result = {"status": "unverified", "reason": "namespace_unverified"}
            code = 2
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return code
    if len(sys.argv) == 2 and sys.argv[1] == "--validate-active-expected-target-rules":
        try:
            result = validate_active_expected_target_rules(_input_document())
            code = 0
        except Exception:
            result = {"status": "refused", "reason": "unsafe_alert_readback"}
            code = 2
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return code
    if len(sys.argv) == 2 and sys.argv[1] == "--prepare-expected-target-transition":
        try:
            result = prepare_expected_target_transition(_input_document())
            code = 0
        except Refusal:
            result = {"status": "refused", "reason": "unsafe_alert_transition"}
            code = 2
        except Exception:
            result = {"status": "refused", "reason": "unsafe_alert_transition"}
            code = 2
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return code
    if len(sys.argv) == 2 and sys.argv[1] == "--validate-expected-targets":
        try:
            declaration = _input_document()
            targets, _retired = validate_expected_target_declaration(
                declaration.get("targets"),
                declaration.get("retired_uids"),
                reviewed=declaration.get("reviewed"),
            )
            result = {"status": "valid", "reason": "declaration_valid"}
            code = 0
        except Refusal:
            result = {"status": "refused", "reason": "invalid_declaration"}
            code = 2
        except Exception:
            result = {"status": "refused", "reason": "invalid_declaration"}
            code = 2
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return code
    if len(sys.argv) == 2 and sys.argv[1] == "--group-expected-targets":
        try:
            declaration = _input_document()
            targets, _retired = validate_expected_target_declaration(
                declaration.get("targets"), declaration.get("retired_uids"),
                reviewed=declaration.get("reviewed"),
            )
            groups = expected_target_groups(targets)
            for group in groups:
                group["expr"] = expected_target_group_expression(group)
            result = {"groups": groups}
            code = 0
        except Exception:
            result = {"groups": []}
            code = 2
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return code
    if len(sys.argv) == 2 and sys.argv[1] == "--expected-target-coverage":
        try:
            result = _expected_target_coverage_action()
        except Exception:
            result = {
                "status": "unavailable",
                "reason": "survey_unavailable",
                "expected_count": 0,
                "active_target_count": 0,
                "matched_count": 0,
                "missing_count": 0,
                "unexpected_count": 0,
                "unhealthy_count": 0,
            }
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    if len(sys.argv) == 2 and sys.argv[1] == "--verify-availability-source":
        try:
            result = _availability_source_action()
            code = 0
        except Refusal as error:
            result = {
                "status": "unavailable", "reason": error.category,
                "source": "unknown", "intended_source": "unknown",
                "matches_intended": False,
            }
            code = 2
        except Exception:
            result = {
                "status": "unavailable", "reason": "native_source_unavailable",
                "source": "unknown", "intended_source": "unknown",
                "matches_intended": False,
            }
            code = 2
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return code
    try:
        receipt = collect()
    except Refusal as error:
        print(json.dumps({"status": "unavailable", "reason": error.category}, sort_keys=True))
        return 2
    except Exception:
        # Never print subprocess details, target input, or an exception chain.
        print(json.dumps({"status": "unavailable", "reason": "survey_unavailable"}, sort_keys=True))
        return 2
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
