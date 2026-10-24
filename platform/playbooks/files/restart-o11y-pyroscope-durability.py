#!/usr/bin/env python3
"""Prove a prior bounded CPU profile sample survives one Pyroscope restart."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import re
import sys
import time
from collections.abc import Callable
from pathlib import Path

SCHEMA = {
    "schema_version": 1, "status": "unavailable", "reason": "invalid_input",
    "service": "o11y/alloy", "profile_type": "CPU", "acceptance_task_id": 0,
    "acceptance_sha": "", "receiver_sha": "", "window_start": 0, "window_end": 0,
    "query_before": "unverified", "restarted": "unverified", "ready": "unverified",
    "query_after": "unverified", "container_identity": "unverified", "volume_metadata": "unverified",
    "sample_fingerprint_sha256": "",
}
REASONS = {
    "invalid_input", "config_not_disabled", "baseline_ambiguous", "receiver_unavailable",
    "profile_not_queryable_before_restart", "restart_failed", "pyroscope_not_ready",
    "container_identity_changed", "container_metadata_changed", "volume_metadata_changed",
    "profile_not_queryable_after_restart", "sample_changed", "operation_failed",
}
MAX_INPUT_BYTES = 1_200_000
MAX_CONFIG_BYTES = 1_048_576
READY_ATTEMPTS = 12
READY_INTERVAL_SECONDS = 5
QUERY_PREFIX = (
    "/pyroscope/render?query=process_cpu%3Acpu%3Ananoseconds%3Acpu%3Ananoseconds"
    "%7Bservice_name%3D%22o11y%2Falloy%22%7D&from="
)
RESTART_TIMEOUT = 45
RESTART_STOP_GRACE_SECONDS = 10
RECOVERY_TIMEOUT = 15


class Refusal(Exception):
    """A fixed refusal that never includes command output or profile data."""

    def __init__(self, reason: str):
        super().__init__(reason if reason in REASONS else "operation_failed")
        self.reason = reason if reason in REASONS else "operation_failed"


def _pilot_module():
    path = Path(__file__).with_name("o11y-profile-pilot-operation.py")
    spec = importlib.util.spec_from_file_location("o11y_profile_pilot_operation", path)
    if spec is None or spec.loader is None:
        raise Refusal("operation_failed")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(module, argv: list[str], runner: Callable | None, *, timeout: int | None = None) -> bytes:
    try:
        if timeout is None:
            return module._run(argv, runner)
        return module._run(argv, runner, timeout=timeout)
    except Exception as exc:
        if argv[:2] == ["podman", "restart"]:
            try:
                module._run(["podman", "start", "o11y-pyroscope"], runner, timeout=RECOVERY_TIMEOUT)
            except Exception:
                pass
            raise Refusal("restart_failed") from exc
        if isinstance(exc, module.Refusal):
            raise Refusal("receiver_unavailable") from exc
        raise


def _ready(module, runner: Callable | None) -> None:
    _run(module, [
        "podman", "exec", "o11y-grafana", "wget", "-q", "-O", "/dev/null",
        "http://pyroscope:4040/ready",
    ], runner)


def _wait_ready(module, runner, sleep_fn: Callable) -> None:
    for index in range(READY_ATTEMPTS):
        try:
            _ready(module, runner)
            return
        except Refusal:
            if index + 1 == READY_ATTEMPTS:
                raise Refusal("pyroscope_not_ready") from None
            sleep_fn(READY_INTERVAL_SECONDS)


def _query(module, start: int, end: int, runner: Callable | None) -> tuple[str, bool]:
    query = f"{QUERY_PREFIX}{start}&until={end}"
    try:
        output = _run(module, [
            "podman", "exec", "o11y-grafana", "wget", "-q", "-O", "-",
            f"http://pyroscope:4040{query}",
        ], runner)
        document = json.loads(output)
    except (Refusal, UnicodeDecodeError, json.JSONDecodeError):
        return "", False
    timeline = document.get("timeline") if isinstance(document, dict) else None
    if not isinstance(timeline, dict):
        return "", False
    start_time, step, samples = timeline.get("startTime"), timeline.get("durationDelta"), timeline.get("samples")
    if (not isinstance(start_time, int) or isinstance(start_time, bool)
            or not isinstance(step, int) or isinstance(step, bool) or step <= 0 or not isinstance(samples, list)):
        return "", False
    positive = any(
        isinstance(sample, (int, float)) and not isinstance(sample, bool)
        and math.isfinite(sample) and sample > 0
        and start <= start_time + index * step <= end
        for index, sample in enumerate(samples)
    )
    if not positive:
        return "", False
    fingerprint = hashlib.sha256(json.dumps(
        timeline, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()
    return fingerprint, True


def evaluate(
    payload: object, *, runner: Callable | None = None, sleep_fn: Callable = time.sleep,
) -> dict:
    result = dict(SCHEMA)
    if not isinstance(payload, dict) or set(payload) != {
        "config_path", "receiver_sha", "disabled", "acceptance",
    }:
        return result
    path, receiver_sha = payload["config_path"], payload["receiver_sha"]
    disabled, acceptance = payload["disabled"], payload["acceptance"]
    if (not isinstance(path, str) or not os.path.isabs(path) or not path.endswith("/config/config.alloy")
            or not isinstance(receiver_sha, str) or re.fullmatch(r"[0-9a-f]{40}", receiver_sha) is None
            or not isinstance(disabled, str) or len(disabled.encode()) > MAX_CONFIG_BYTES
            or not isinstance(acceptance, dict)
            or set(acceptance) != {
                "schema_version", "status", "reason", "task_id", "acceptance_sha", "receiver_sha",
                "window_start", "window_end",
            }
            or acceptance.get("schema_version") != 1
            or acceptance.get("status") != "verified" or acceptance.get("reason") != "acceptance_task_receipt_verified"
            or not isinstance(acceptance.get("task_id"), int) or isinstance(acceptance.get("task_id"), bool)
            or acceptance["task_id"] <= 0
            or not isinstance(acceptance.get("acceptance_sha"), str)
            or re.fullmatch(r"[0-9a-f]{40}", acceptance["acceptance_sha"]) is None
            or acceptance.get("receiver_sha") != receiver_sha
            or not isinstance(acceptance.get("window_start"), int) or isinstance(acceptance.get("window_start"), bool)
            or not isinstance(acceptance.get("window_end"), int) or isinstance(acceptance.get("window_end"), bool)
            or not 0 < acceptance["window_end"] - acceptance["window_start"] <= 601):
        return result
    result.update(
        acceptance_task_id=acceptance["task_id"], acceptance_sha=acceptance["acceptance_sha"],
        receiver_sha=receiver_sha, window_start=acceptance["window_start"], window_end=acceptance["window_end"],
    )
    module = _pilot_module()
    try:
        config, _inode = module._read_config(path)
        if config != disabled.encode():
            raise Refusal("config_not_disabled")
        before_digest, before_state = module._container_snapshot(path, runner)
        before_volumes, before_volume_names = module._volume_snapshot(runner)
        if (not isinstance(before_state.get("pyroscope_id"), str) or not before_state["pyroscope_id"]
                or before_state.get("pyroscope_volume") not in before_volume_names):
            raise Refusal("receiver_unavailable")
        _wait_ready(module, runner, sleep_fn)
        before_fingerprint, before_found = _query(
            module, acceptance["window_start"], acceptance["window_end"], runner,
        )
        if not before_found:
            raise Refusal("profile_not_queryable_before_restart")
        result.update(query_before="observed", ready="observed")

        _run(module, [
            "podman", "restart", "--time", str(RESTART_STOP_GRACE_SECONDS), "o11y-pyroscope",
        ], runner, timeout=RESTART_TIMEOUT)
        result["restarted"] = "completed"
        _wait_ready(module, runner, sleep_fn)
        after_digest, after_state = module._container_snapshot(path, runner)
        after_volumes, after_volume_names = module._volume_snapshot(runner)
        if after_state.get("pyroscope_id") != before_state["pyroscope_id"]:
            raise Refusal("container_identity_changed")
        if before_digest != after_digest:
            raise Refusal("container_metadata_changed")
        if before_volumes != after_volumes or before_volume_names != after_volume_names:
            raise Refusal("volume_metadata_changed")
        after_fingerprint, after_found = _query(
            module, acceptance["window_start"], acceptance["window_end"], runner,
        )
        if not after_found:
            raise Refusal("profile_not_queryable_after_restart")
        if before_fingerprint != after_fingerprint:
            raise Refusal("sample_changed")
        result.update(
            status="verified", reason="prior_sample_queryable_after_pyroscope_restart",
            restarted="completed", ready="observed", query_after="observed",
            container_identity="preserved", volume_metadata="preserved",
            sample_fingerprint_sha256=before_fingerprint,
        )
    except Refusal as exc:
        result["reason"] = exc.reason
    except Exception:
        result["reason"] = "operation_failed"
    return result


def main() -> int:
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        result = dict(SCHEMA)
        result["reason"] = "invalid_input"
    else:
        try:
            result = evaluate(json.loads(raw))
        except (json.JSONDecodeError, UnicodeError):
            result = dict(SCHEMA)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
