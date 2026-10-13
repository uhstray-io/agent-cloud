#!/usr/bin/env python3
"""Emit a bounded, sanitized health receipt for the fixed o11y Alloy container."""

from __future__ import annotations

import json
import os
import re
import selectors
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime

CONTAINER = "o11y-alloy"
MAX_INSPECT_BYTES = 1024 * 1024
RUN_STATES = {
    "configured",
    "created",
    "running",
    "stopped",
    "exited",
    "paused",
    "unknown",
}
HEALTH_STATES = {"healthy", "unhealthy", "starting", "none", "unknown"}
ZERO_STARTED_AT = re.compile(r"0001-01-01T00:00:00(?:\.0+)?(?:Z|\+00:00)")


class Refusal(Exception):
    """Fixed safe refusal category; never include runtime data."""


def _terminate_and_reap(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        if process.poll() is None:
            process.kill()
    process.wait()


def _read_bounded_stdout(
    argv: list[str], *, timeout: float = 8, max_bytes: int = MAX_INSPECT_BYTES
) -> str:
    process: subprocess.Popen[bytes] | None = None
    selector = selectors.DefaultSelector()
    completed = False
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        assert process.stdout is not None
        os.set_blocking(process.stdout.fileno(), False)
        selector.register(process.stdout, selectors.EVENT_READ)
        output = bytearray()
        deadline = time.monotonic() + timeout
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Refusal("inspect_failed")
            for key, _ in selector.select(remaining):
                room = max_bytes + 1 - len(output)
                chunk = os.read(key.fd, min(65536, room))
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                output.extend(chunk)
                if len(output) > max_bytes:
                    raise Refusal("inspect_malformed")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Refusal("inspect_failed")
        try:
            return_code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            raise Refusal("inspect_failed") from exc
        if return_code != 0:
            raise Refusal("inspect_failed")
        completed = True
        try:
            return output.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise Refusal("inspect_malformed") from exc
    except OSError as exc:
        raise Refusal("inspect_failed") from exc
    finally:
        selector.close()
        if process is not None:
            if not completed:
                _terminate_and_reap(process)
            if process.stdout is not None:
                process.stdout.close()


def _age_bucket(value: object, now: datetime | None = None) -> str | None:
    if value is None or value == "" or (isinstance(value, str) and ZERO_STARTED_AT.fullmatch(value)):
        return None
    if not isinstance(value, str):
        raise Refusal("inspect_malformed")
    try:
        started = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise Refusal("inspect_malformed") from exc
    if started.tzinfo is None:
        raise Refusal("inspect_malformed")
    age = ((now or datetime.now(UTC)) - started.astimezone(UTC)).total_seconds()
    if age < 0:
        raise Refusal("inspect_malformed")
    if age < 3600:
        return "under_1h"
    if age < 86400:
        return "1_to_24h"
    if age < 604800:
        return "1_to_7d"
    return "7d_or_more"


def _project(data: object) -> dict[str, object]:
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise Refusal("inspect_ambiguous")
    container = data[0]
    name = container.get("Name")
    if not isinstance(name, str) or name not in {CONTAINER, f"/{CONTAINER}"}:
        raise Refusal("inspect_ambiguous")
    state = container.get("State")
    if not isinstance(state, dict):
        raise Refusal("inspect_malformed")
    run_status = state.get("Status")
    health = state.get("Health")
    if not isinstance(run_status, str) or run_status not in RUN_STATES:
        raise Refusal("inspect_malformed")
    if health is None:
        health_status = "none"
        health_log = []
    elif isinstance(health, dict):
        health_status = health.get("Status")
        health_log = health.get("Log", [])
        if health_status == "":
            health_status = "none"
        if health_log is None:
            health_log = []
        if not isinstance(health_status, str) or health_status not in HEALTH_STATES:
            raise Refusal("inspect_malformed")
    else:
        raise Refusal("inspect_malformed")
    if not isinstance(health_log, list):
        raise Refusal("inspect_malformed")
    last_exit: int | None = None
    if health_log:
        latest = health_log[-1]
        if not isinstance(latest, dict):
            raise Refusal("inspect_malformed")
        exit_code = latest.get("ExitCode")
        if type(exit_code) is not int or not 0 <= exit_code <= 255:
            raise Refusal("inspect_malformed")
        last_exit = exit_code
    restart_count = container.get("RestartCount")
    oom_killed = state.get("OOMKilled")
    if type(restart_count) is not int or restart_count < 0 or type(oom_killed) is not bool:
        raise Refusal("inspect_malformed")
    return {
        "status": "observed",
        "container_present": True,
        "run_status": run_status,
        "health_status": health_status,
        "restart_count": restart_count,
        "oom_killed": oom_killed,
        "last_health_exit_code": last_exit,
        "started_age_bucket": _age_bucket(state.get("StartedAt")),
    }


def inspect() -> dict[str, object]:
    try:
        exists = subprocess.run(
            ["podman", "container", "exists", CONTAINER],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=8,
        )
    except subprocess.TimeoutExpired as exc:
        raise Refusal("inspect_failed") from exc
    except OSError as exc:
        raise Refusal("inspect_failed") from exc
    if exists.returncode == 1:
        raise Refusal("container_missing")
    if exists.returncode != 0:
        raise Refusal("inspect_failed")
    try:
        output = _read_bounded_stdout(
            ["podman", "inspect", "--type", "container", CONTAINER],
            timeout=8,
            max_bytes=MAX_INSPECT_BYTES,
        )
    except Refusal:
        raise
    try:
        data = json.loads(output)
    except (TypeError, json.JSONDecodeError) as exc:
        raise Refusal("inspect_malformed") from exc
    return _project(data)


def main() -> int:
    try:
        report = inspect()
        status = 0
    except Refusal as exc:
        report = {"status": "unavailable", "reason": str(exc)}
        status = 1
    except Exception:
        report = {"status": "unavailable", "reason": "survey_failed"}
        status = 1
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return status


if __name__ == "__main__":
    sys.exit(main())
