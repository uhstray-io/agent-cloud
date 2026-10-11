#!/usr/bin/env python3
"""Collect bounded, read-only Linux and systemd context for the o11y receiver."""

from __future__ import annotations

import json
import os
import re
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path

MAX_OUTPUT = 64 * 1024
PROBE_TIMEOUT = 4
SERVICE_USER = re.compile(r"^[a-z_][a-z0-9_.-]{0,31}$")
REASONS = {
    "observed", "probe_unavailable", "probe_failed", "probe_timeout",
    "probe_oversized", "probe_malformed", "probe_missing", "not_read",
}


class ProbeError(Exception):
    def __init__(self, reason: str):
        self.reason = reason if reason in REASONS else "probe_failed"


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _run(argv: list[str]) -> str:
    """Run one fixed command with bounded output, time, and no stderr capture."""
    try:
        process = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True,
        )
    except OSError as exc:
        raise ProbeError("probe_unavailable") from exc

    assert process.stdout is not None
    output = bytearray()
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + PROBE_TIMEOUT
    reason = None
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                reason = "probe_timeout"
                break
            if not selector.select(remaining):
                reason = "probe_timeout"
                break
            chunk = os.read(process.stdout.fileno(), min(8192, MAX_OUTPUT + 1 - len(output)))
            if not chunk:
                selector.unregister(process.stdout)
                continue
            output.extend(chunk)
            if len(output) > MAX_OUTPUT:
                reason = "probe_oversized"
                break
        if reason:
            _kill_group(process.pid)
        try:
            return_code = process.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            _kill_group(process.pid)
            process.wait()
            reason = reason or "probe_timeout"
            return_code = 1
    finally:
        selector.close()
        process.stdout.close()
    if reason:
        raise ProbeError(reason)
    if return_code != 0:
        raise ProbeError("probe_failed")
    try:
        return output.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ProbeError("probe_malformed") from exc


def _read_mountinfo() -> str:
    try:
        with Path("/proc/self/mountinfo").open("rb") as source:
            raw = source.read(MAX_OUTPUT + 1)
    except OSError as exc:
        raise ProbeError("probe_unavailable") from exc
    if len(raw) > MAX_OUTPUT:
        raise ProbeError("probe_oversized")
    try:
        return raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ProbeError("probe_malformed") from exc


def _observation(value: str = "unknown", reason: str = "probe_missing") -> dict[str, str]:
    if reason == "observed":
        return {"status": "observed", "value": value, "reason": "observed"}
    return {"status": "unknown", "value": "unknown", "reason": reason}


def _probe(callable_probe, parse) -> dict[str, str]:
    try:
        raw = callable_probe()
    except ProbeError as exc:
        return _observation(reason=exc.reason)
    except (OSError, RuntimeError, TypeError, ValueError):
        return _observation(reason="probe_failed")
    try:
        value = parse(raw)
    except (AttributeError, IndexError, TypeError, ValueError):
        return _observation(reason="probe_malformed")
    if value is None:
        return _observation(reason="probe_missing")
    return _observation(value, "observed")


def _version_band(raw: str) -> str | None:
    if not raw.strip():
        return None
    match = re.match(r"^(\d+)\.(\d+)(?:\.|[-+]|$)", raw.strip())
    if not match:
        raise ValueError("malformed version")
    major = int(match.group(1))
    return f"{major}.x" if major in (5, 6) else "other"


def _systemd_band(raw: str) -> str | None:
    if not raw.strip():
        return None
    first_line = raw.splitlines()[0] if raw.splitlines() else ""
    match = re.fullmatch(r"systemd (\d+)(?: [^\r\n]*)?", first_line)
    if not match:
        raise ValueError("malformed version")
    version = int(match.group(1))
    return "2xx" if 200 <= version < 300 else "other"


def _cgroup_mode(raw: str) -> str | None:
    filesystems = set()
    for line in raw.splitlines():
        fields = line.split(" - ", 1)
        if len(fields) == 2:
            fs_type = fields[1].split(" ", 1)[0]
            if fs_type in {"cgroup", "cgroup2"}:
                filesystems.add(fs_type)
    if filesystems == {"cgroup"}:
        return "v1"
    if filesystems == {"cgroup2"}:
        return "v2"
    if filesystems == {"cgroup", "cgroup2"}:
        return "hybrid"
    return None


def _identity(current_name: str, service_user: str) -> str | None:
    value = current_name.strip()
    if not value:
        return None
    if not re.fullmatch(r"[a-z_][a-z0-9_.-]{0,31}", value):
        raise ValueError("malformed identity")
    return "matched" if value == service_user else "mismatch"


def _privilege(raw: str) -> str | None:
    value = raw.strip()
    if not value:
        return None
    if not re.fullmatch(r"[0-9]+", value):
        raise ValueError("malformed identity")
    return "rootful" if int(value) == 0 else "rootless"


def _linger(raw: str) -> str | None:
    if not raw.strip():
        return None
    match = re.fullmatch(r"Linger=(yes|no)\n?", raw)
    if not match:
        raise ValueError("malformed property")
    return {"yes": "enabled", "no": "disabled"}[match.group(1)]


def _restart_unit(raw: str) -> str | None:
    properties = {}
    for line in raw.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in {"LoadState", "ActiveState", "UnitFileState"}:
            properties[key] = value
    if set(properties) != {"LoadState", "ActiveState", "UnitFileState"}:
        return None
    if properties["LoadState"] != "loaded":
        return "not_loaded"
    if properties["UnitFileState"] == "enabled":
        return "enabled_active" if properties["ActiveState"] == "active" else "enabled_nonactive"
    if properties["UnitFileState"] == "disabled":
        return "disabled"
    return "loaded_other"


def collect(service_user: str) -> dict:
    if not SERVICE_USER.fullmatch(service_user):
        raise ProbeError("probe_malformed")

    identity = _probe(lambda: _run(["/usr/bin/id", "-un"]), lambda raw: _identity(raw, service_user))
    restart_context = (
        _probe(
            lambda: _run([
                "/usr/bin/systemctl", "--user", "show", "podman-restart.service",
                "--property=LoadState,ActiveState,UnitFileState",
            ]), _restart_unit,
        )
        if identity["status"] == "observed" and identity["value"] == "matched"
        else _observation(reason="not_read")
    )

    return {
        "schema_version": 1,
        "status": "observed",
        "observations": {
            "kernel_band": _probe(lambda: _run(["/usr/bin/uname", "-r"]), _version_band),
            "systemd_band": _probe(lambda: _run(["/usr/bin/systemctl", "--version"]), _systemd_band),
            "cgroup_mode": _probe(_read_mountinfo, _cgroup_mode),
            "receiver_identity": identity,
            "receiver_privilege": _probe(lambda: _run(["/usr/bin/id", "-u"]), _privilege),
            "linger": _probe(
                lambda: _run(["/usr/bin/loginctl", "show-user", service_user, "--property=Linger"]), _linger
            ),
            "restart_context": restart_context,
            "delegation": _observation(reason="not_read"),
            "container_restart_policy": _observation(reason="not_read"),
        },
        "actions": {"podman_store": "not_called", "service_mutation": "not_called"},
    }


def unknown_report(reason: str) -> dict:
    if reason not in REASONS:
        reason = "probe_failed"
    observations = {
        name: _observation(reason=reason)
        for name in (
            "kernel_band", "systemd_band", "cgroup_mode", "receiver_identity",
            "receiver_privilege", "linger", "restart_context", "delegation",
            "container_restart_policy",
        )
    }
    return {
        "schema_version": 1,
        "status": "unknown",
        "observations": observations,
        "actions": {"podman_store": "not_called", "service_mutation": "not_called"},
    }


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1 or not SERVICE_USER.fullmatch(arguments[0]):
        result = unknown_report("probe_malformed")
    else:
        try:
            result = collect(arguments[0])
        except ProbeError as exc:
            result = unknown_report(exc.reason)
        except (OSError, RuntimeError, TypeError, ValueError):
            result = unknown_report("probe_failed")
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
