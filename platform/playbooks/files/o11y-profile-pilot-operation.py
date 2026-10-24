#!/usr/bin/env python3
"""Run a bounded Alloy-only profile acceptance and restore the disabled config."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import re
import selectors
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Callable

SCHEMA = {
    "schema_version": 1,
    "status": "unavailable",
    "reason": "invalid_input",
    "service": "o11y/alloy",
    "sampling": "unverified",
    "delivery": "unverified",
    "restored": "unverified",
    "post_disable_query": "unverified",
    "disabled_config_sha256": "",
    "enabled_config_sha256": "",
    "receiver_sha": "",
}
REASONS = {
    "invalid_input", "receiver_unavailable", "baseline_ambiguous", "mount_mismatch",
    "container_state_changed", "volume_state_changed", "config_write_failed",
    "reload_failed", "profile_component_unhealthy", "profile_not_observed",
    "rollback_failed", "post_disable_query_not_observed", "operation_failed",
    "operation_interrupted",
}
REQUIRED_CONTAINERS = {
    "o11y-prometheus", "o11y-loki", "o11y-alloy", "o11y-tempo",
    "o11y-pyroscope", "o11y-grafana", "o11y-node-exporter",
}
HEALTHY_CONTAINERS = {"o11y-prometheus", "o11y-loki", "o11y-alloy", "o11y-grafana"}
MAX_CONFIG_BYTES = 1_048_576
MAX_COMMAND_BYTES = 1_048_576
COMMAND_TIMEOUT = 15
PROFILE_QUERY_PREFIX = (
    "/pyroscope/render?query=process_cpu%3Acpu%3Ananoseconds%3Acpu%3Ananoseconds"
    "%7Bservice_name%3D%22o11y%2Falloy%22%7D&from="
)
RELOAD_SCRIPT = (
    "exec 3<>/dev/tcp/127.0.0.1/12345 || exit 10; "
    "printf 'POST /-/reload HTTP/1.0\\r\\nHost: localhost\\r\\nContent-Length: 0\\r\\n\\r\\n' >&3; "
    "IFS= read -r line <&3 || exit 11; printf '%s\\n' \"$line\""
)
COMPONENTS_SCRIPT = (
    "exec 3<>/dev/tcp/127.0.0.1/12345 || exit 10; "
    "printf 'GET /api/v0/web/components HTTP/1.0\\r\\nHost: localhost\\r\\n\\r\\n' >&3; "
    "while IFS= read -r line || [[ -n $line ]]; do printf '%s\\n' \"$line\"; done <&3"
)


class Refusal(Exception):
    """A fixed refusal reason that never includes runtime output."""

    def __init__(self, reason: str):
        super().__init__(reason if reason in REASONS else "operation_failed")
        self.reason = reason if reason in REASONS else "operation_failed"


def _candidate_module(path: str):
    spec = importlib.util.spec_from_file_location("o11y_profile_candidates", path)
    if spec is None or spec.loader is None:
        raise Refusal("invalid_input")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    try:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except PermissionError:
                process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    process.kill()
                process.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Refusal("receiver_unavailable") from exc


def _run(argv: list[str], runner: Callable | None = None, *, timeout: int = COMMAND_TIMEOUT) -> bytes:
    if runner is not None and runner is not subprocess.run:
        try:
            result = runner(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=timeout, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise Refusal("receiver_unavailable") from exc
        output = result.stdout if isinstance(result.stdout, bytes) else str(result.stdout or "").encode()
        if result.returncode != 0 or len(output) > MAX_COMMAND_BYTES:
            raise Refusal("receiver_unavailable")
        return output

    process: subprocess.Popen[bytes] | None = None
    selector = selectors.DefaultSelector()
    output = bytearray()
    try:
        process = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True,
        )
        if process.stdout is None:
            raise Refusal("receiver_unavailable")
        os.set_blocking(process.stdout.fileno(), False)
        selector.register(process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Refusal("receiver_unavailable")
            for key, _ in selector.select(remaining):
                chunk = os.read(key.fd, min(65_536, MAX_COMMAND_BYTES + 1 - len(output)))
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                output.extend(chunk)
                if len(output) > MAX_COMMAND_BYTES:
                    raise Refusal("receiver_unavailable")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Refusal("receiver_unavailable")
        return_code = process.wait(timeout=remaining)
        if return_code != 0:
            raise Refusal("receiver_unavailable")
        return bytes(output)
    except Refusal:
        raise
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Refusal("receiver_unavailable") from exc
    finally:
        selector.close()
        if process is not None:
            try:
                _stop_process(process)
            finally:
                if process.stdout is not None:
                    process.stdout.close()


def _decode_json(data: bytes) -> object:
    try:
        return json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Refusal("receiver_unavailable") from exc


def _container_name(row: object) -> str:
    if not isinstance(row, dict):
        raise Refusal("receiver_unavailable")
    name = row.get("Name")
    if not isinstance(name, str):
        names = row.get("Names")
        name = names[0] if isinstance(names, list) and len(names) == 1 else ""
    return name.lstrip("/") if isinstance(name, str) else ""


def _container_snapshot(config_path: str, runner: Callable) -> tuple[str, dict]:
    raw_names = _run(["podman", "ps", "--all", "--format", "{{.Names}}"], runner).decode("utf-8")
    names = sorted(
        line.strip().lstrip("/") for line in raw_names.splitlines()
        if line.strip().lstrip("/").startswith("o11y-")
    )
    if len(names) != len(set(names)) or not REQUIRED_CONTAINERS.issubset(names):
        raise Refusal("receiver_unavailable")
    inspected = _decode_json(_run(["podman", "container", "inspect", *names], runner))
    if not isinstance(inspected, list) or len(inspected) != len(names):
        raise Refusal("receiver_unavailable")
    config_source = os.path.realpath(config_path)
    containers = []
    alloy = None
    pyroscope = None
    for row in inspected:
        if not isinstance(row, dict):
            raise Refusal("receiver_unavailable")
        name = _container_name(row)
        state = row.get("State")
        mounts = row.get("Mounts")
        if (name not in names or not isinstance(state, dict) or state.get("Status") != "running"
                or not isinstance(mounts, list)):
            raise Refusal("receiver_unavailable")
        health = state.get("Health")
        health_status = health.get("Status") if isinstance(health, dict) else "unreported"
        if name in HEALTHY_CONTAINERS and health_status != "healthy":
            raise Refusal("receiver_unavailable")
        projected_mounts = []
        for mount in mounts:
            if not isinstance(mount, dict):
                raise Refusal("receiver_unavailable")
            projected_mounts.append({
                "type": mount.get("Type"), "source": mount.get("Source"),
                "destination": mount.get("Destination"), "rw": mount.get("RW"),
                "name": mount.get("Name", ""),
            })
        projected_mounts.sort(key=lambda item: (str(item["destination"]), str(item["source"])))
        normalized = {
            "name": name, "id": row.get("Id"), "image": row.get("Image"),
            "status": state.get("Status"), "health": health_status, "mounts": projected_mounts,
        }
        containers.append(normalized)
        if name == "o11y-alloy":
            alloy = normalized
        if name == "o11y-pyroscope":
            pyroscope = normalized
    if (len({row["name"] for row in containers}) != len(names)
            or not isinstance(alloy, dict) or not isinstance(pyroscope, dict)):
        raise Refusal("receiver_unavailable")
    config_mounts = [mount for mount in alloy["mounts"] if mount["destination"] == "/etc/alloy/config.alloy"]
    if (len(config_mounts) != 1 or config_mounts[0]["type"] != "bind"
            or config_mounts[0]["source"] != config_source or config_mounts[0]["rw"] is not False):
        raise Refusal("mount_mismatch")
    pyroscope_mounts = [mount for mount in pyroscope["mounts"] if mount["destination"] == "/data"]
    if (len(pyroscope_mounts) != 1 or pyroscope_mounts[0]["type"] != "volume"
            or not isinstance(pyroscope_mounts[0]["name"], str) or not pyroscope_mounts[0]["name"]
            or pyroscope_mounts[0]["rw"] is not True):
        raise Refusal("mount_mismatch")
    containers.sort(key=lambda item: item["name"])
    return _digest(containers), {
        "containers": containers,
        "config_source": config_source,
        "pyroscope_volume": pyroscope_mounts[0]["name"],
        "pyroscope_id": pyroscope["id"],
    }


def _volume_snapshot(runner: Callable) -> tuple[str, set[str]]:
    raw_names = _run(["podman", "volume", "ls", "--quiet"], runner).decode("utf-8")
    names = sorted(line.strip() for line in raw_names.splitlines() if line.strip())
    if len(names) != len(set(names)) or not names:
        raise Refusal("receiver_unavailable")
    details = _decode_json(_run(["podman", "volume", "inspect", *names], runner))
    if (not isinstance(details, list) or len(details) != len(names)
            or any(not isinstance(item, dict) for item in details)
            or sorted(item.get("Name", "") for item in details) != names):
        raise Refusal("receiver_unavailable")
    details.sort(key=lambda item: item["Name"])
    return _digest(details), set(names)


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _read_config(path: str) -> tuple[bytes, tuple[int, int]]:
    try:
        before = os.stat(path, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise Refusal("baseline_ambiguous")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise Refusal("baseline_ambiguous")
            chunks = []
            size = 0
            while size <= MAX_CONFIG_BYTES:
                chunk = os.read(fd, min(65_536, MAX_CONFIG_BYTES + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            data = b"".join(chunks)
        finally:
            os.close(fd)
    except Refusal:
        raise
    except OSError as exc:
        raise Refusal("baseline_ambiguous") from exc
    if len(data) > MAX_CONFIG_BYTES:
        raise Refusal("baseline_ambiguous")
    return data, (before.st_dev, before.st_ino)


def _write_config_in_place(path: str, content: bytes, expected_inode: tuple[int, int]) -> None:
    # The running container's file bind mount keeps the original inode open.
    if len(content) > MAX_CONFIG_BYTES:
        raise Refusal("config_write_failed")
    try:
        fd = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
        try:
            current = os.fstat(fd)
            if (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1
                    or (current.st_dev, current.st_ino) != expected_inode):
                raise Refusal("config_write_failed")
            os.lseek(fd, 0, os.SEEK_SET)
            view = memoryview(content)
            written = 0
            while written < len(view):
                count = os.write(fd, view[written:])
                if count <= 0:
                    raise Refusal("config_write_failed")
                written += count
            os.ftruncate(fd, len(content))
            os.fsync(fd)
            after = os.fstat(fd)
            if (after.st_dev, after.st_ino) != expected_inode:
                raise Refusal("config_write_failed")
        finally:
            os.close(fd)
    except Refusal:
        raise
    except OSError as exc:
        raise Refusal("config_write_failed") from exc


def _http_status(output: bytes) -> int:
    line = output.splitlines()[0].decode("ascii")
    match = re.fullmatch(r"HTTP/1\.[01] ([0-9]{3})(?: .*)?", line)
    if match is None:
        raise Refusal("reload_failed")
    return int(match.group(1))


def _reload(runner: Callable) -> None:
    output = _run(["podman", "exec", "o11y-alloy", "bash", "-c", RELOAD_SCRIPT], runner)
    if _http_status(output) != 200:
        raise Refusal("reload_failed")


def _api_body(script: str, runner: Callable) -> tuple[bytes, bytes]:
    output = _run(["podman", "exec", "o11y-alloy", "bash", "-c", script], runner)
    headers, separator, body = output.partition(b"\r\n\r\n")
    if not separator:
        headers, separator, body = output.partition(b"\n\n")
    if not separator:
        raise Refusal("receiver_unavailable")
    return headers, body


def _component_state(candidate_module, expected: str, runner: Callable) -> None:
    try:
        headers, body = _api_body(COMPONENTS_SCRIPT, runner)
        components = candidate_module._parse_component_response(headers, body)
        if expected == "healthy":
            state = candidate_module._project_component_health(components, "pyroscope.scrape.alloy_self_profile")
            if state != "healthy":
                raise Refusal("profile_component_unhealthy")
        elif candidate_module._project_disabled_components(components) != "loaded_without_profile":
            raise Refusal("rollback_failed")
    except Refusal:
        raise
    except Exception as exc:
        raise Refusal("receiver_unavailable") from exc


def _query_profile(activation_start: int, runner: Callable) -> bool:
    try:
        query = f"{PROFILE_QUERY_PREFIX}{activation_start}&until=now"
        output = _run([
            "podman", "exec", "o11y-grafana", "wget", "-q", "-O", "-",
            f"http://pyroscope:4040{query}",
        ], runner)
        document = _decode_json(output)
        timeline = document.get("timeline") if isinstance(document, dict) else None
        if not isinstance(timeline, dict):
            return False
        start_time, step, samples = (
            timeline.get("startTime"), timeline.get("durationDelta"), timeline.get("samples")
        )
        if (not isinstance(start_time, int) or isinstance(start_time, bool)
                or not isinstance(step, int) or isinstance(step, bool) or step <= 0
                or not isinstance(samples, list)):
            return False
        return any(
            isinstance(sample, (int, float)) and not isinstance(sample, bool)
            and math.isfinite(sample) and sample > 0
            and start_time + index * step >= activation_start
            for index, sample in enumerate(samples)
        )
    except (Refusal, UnicodeDecodeError, IndexError):
        return False


def _wait_for_profile(
    activation_start: int, runner: Callable, sleep_fn: Callable, attempts: int,
    interrupted: Callable[[], bool] = lambda: False,
) -> bool:
    for index in range(attempts):
        if interrupted():
            return False
        if _query_profile(activation_start, runner):
            return True
        if interrupted():
            return False
        if index + 1 < attempts:
            sleep_fn(10)
    return False


def evaluate(
    payload: object, *, runner: Callable | None = None, sleep_fn: Callable = time.sleep,
    interrupted: Callable[[], bool] = lambda: False,
) -> dict:
    result = dict(SCHEMA)
    if not isinstance(payload, dict) or set(payload) != {
        "config_path", "receiver_sha", "disabled", "enabled", "candidate_helper",
    }:
        return result
    path, receiver_sha = payload.get("config_path"), payload.get("receiver_sha")
    disabled = payload.get("disabled")
    enabled = payload.get("enabled")
    candidate_helper = payload.get("candidate_helper")
    if (not isinstance(path, str) or not os.path.isabs(path)
            or not path.endswith("/config/config.alloy")
            or not isinstance(receiver_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", receiver_sha)
            or not isinstance(disabled, str) or not isinstance(enabled, str)
            or not isinstance(candidate_helper, str)
            or not os.path.isfile(candidate_helper)):
        return result
    disabled_bytes, enabled_bytes = disabled.encode("utf-8"), enabled.encode("utf-8")
    if (max(len(disabled_bytes), len(enabled_bytes)) > MAX_CONFIG_BYTES or disabled_bytes == enabled_bytes):
        return result
    result["disabled_config_sha256"] = hashlib.sha256(disabled_bytes).hexdigest()
    result["enabled_config_sha256"] = hashlib.sha256(enabled_bytes).hexdigest()
    result["receiver_sha"] = receiver_sha
    changed = False
    reason = "operation_failed"
    candidate_module = None
    before_containers = before_volumes = None
    inode = None
    sampling = delivery = restored = post_disable_query = False
    temporary_enable_attempted = False
    try:
        candidate_module = _candidate_module(candidate_helper)
        before, inode = _read_config(path)
        before_containers, before_state = _container_snapshot(path, runner)
        before_volumes, before_volume_names = _volume_snapshot(runner)
        if before_state["pyroscope_volume"] not in before_volume_names:
            raise Refusal("mount_mismatch")
        if interrupted():
            raise Refusal("operation_interrupted")
        if before == enabled_bytes:
            changed = True
            raise Refusal("baseline_ambiguous")
        if before != disabled_bytes:
            raise Refusal("baseline_ambiguous")
        changed = True
        temporary_enable_attempted = True
        activation_start = int(time.time())
        _write_config_in_place(path, enabled_bytes, inode)
        if interrupted():
            raise Refusal("operation_interrupted")
        current, current_inode = _read_config(path)
        if current != enabled_bytes or current_inode != inode:
            raise Refusal("config_write_failed")
        _reload(runner)
        _component_state(candidate_module, "healthy", runner)
        delivery = _wait_for_profile(
            activation_start, runner, sleep_fn, attempts=19, interrupted=interrupted
        )
        if interrupted():
            raise Refusal("operation_interrupted")
        sampling = delivery
        if not delivery:
            raise Refusal("profile_not_observed")
        reason = "verified"
    except Refusal as refusal:
        reason = refusal.reason
    except Exception:
        reason = "operation_failed"
    finally:
        if changed and inode is not None:
            try:
                _write_config_in_place(path, disabled_bytes, inode)
                _reload(runner)
                current, current_inode = _read_config(path)
                if current != disabled_bytes or current_inode != inode:
                    raise Refusal("rollback_failed")
                _component_state(candidate_module, "disabled", runner)
                restored = True
                if temporary_enable_attempted and delivery:
                    post_disable_query = _wait_for_profile(
                        activation_start, runner, sleep_fn, attempts=7, interrupted=interrupted
                    )
                    if not post_disable_query:
                        reason = "post_disable_query_not_observed"
            except Exception:
                reason = "rollback_failed"
                restored = False
                post_disable_query = False
        if changed and before_containers is not None and before_volumes is not None:
            try:
                after_containers, _ = _container_snapshot(path, runner)
                if after_containers != before_containers and reason != "rollback_failed":
                    reason = "container_state_changed"
            except Exception:
                if reason not in ("rollback_failed", "container_state_changed"):
                    reason = "receiver_unavailable"
            try:
                after_volumes, _ = _volume_snapshot(runner)
                if (after_volumes != before_volumes
                        and reason not in ("rollback_failed", "container_state_changed")):
                    reason = "volume_state_changed"
            except Exception:
                if reason not in ("rollback_failed", "container_state_changed"):
                    reason = "receiver_unavailable"
    preserved_failure = {
        "rollback_failed", "container_state_changed", "volume_state_changed", "receiver_unavailable",
    }
    if interrupted() and reason not in preserved_failure:
        reason = "operation_interrupted"
    if reason == "verified" and not (sampling and delivery and restored and post_disable_query):
        reason = "operation_failed"
    if reason == "verified":
        result.update({
            "status": "verified", "reason": "sampled_delivered_restored_post_disable_query_observed",
            "sampling": "observed", "delivery": "observed", "restored": "disabled",
            "post_disable_query": "observed",
        })
    else:
        result.update({
            "status": "unavailable", "reason": reason,
            "sampling": "observed" if sampling else "unverified",
            "delivery": "observed" if delivery else "unverified",
            "restored": "disabled" if restored else "unverified",
            "post_disable_query": "observed" if post_disable_query else "unverified",
        })
    return result


def _evaluate_with_signal_handlers(payload: object) -> dict:
    pending_signals: set[int] = set()

    def defer(signum, _frame):
        pending_signals.add(signum)

    previous = {
        signum: signal.signal(signum, defer)
        for signum in (signal.SIGTERM, signal.SIGINT)
    }
    try:
        result = evaluate(payload, interrupted=lambda: bool(pending_signals))
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    if pending_signals and result["status"] == "verified":
        result.update(status="unavailable", reason="operation_interrupted")
    return result


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeError):
        payload = None
    try:
        result = _evaluate_with_signal_handlers(payload)
    except Exception:
        result = dict(SCHEMA)
        result["reason"] = "operation_failed"
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
