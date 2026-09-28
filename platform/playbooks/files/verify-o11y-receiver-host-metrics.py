#!/usr/bin/env python3
"""Fail-closed, privacy-safe acceptance checks for receiver-host capacity metrics."""

import json
import math
import re
import sys


def fail(reason: str) -> int:
    print(json.dumps({"ok": False, "reason": reason}, sort_keys=True))
    return 1


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        response = json.loads(payload["prometheus"])
        df_lines = payload["guest_df"].splitlines()
        guest_values = [int(value) for value in df_lines[-1].split()]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, IndexError):
        return fail("invalid_readback")

    data = response.get("data") if isinstance(response, dict) else None
    if (
        not isinstance(response, dict)
        or response.get("status") != "success"
        or not isinstance(data, dict)
        or data.get("resultType") != "vector"
    ):
        return fail("prometheus_query_failed")
    if len(guest_values) != 2:
        return fail("guest_filesystem_readback_invalid")

    series = data.get("result")
    if not isinstance(series, list):
        return fail("prometheus_series_missing")

    required = {
        "node_cpu_seconds_total": lambda labels: labels.get("mode") == "idle",
        "node_memory_MemAvailable_bytes": lambda _labels: True,
        "node_memory_MemTotal_bytes": lambda _labels: True,
        "node_load1": lambda _labels: True,
        "node_filesystem_size_bytes": lambda labels: labels.get("mountpoint") == "/",
        "node_filesystem_avail_bytes": lambda labels: labels.get("mountpoint") == "/",
    }
    found = {name: [] for name in required}
    for item in series:
        if not isinstance(item, dict):
            return fail("prometheus_series_invalid")
        labels = item.get("metric", {})
        if not isinstance(labels, dict):
            return fail("prometheus_series_invalid")
        name = labels.get("__name__")
        if name not in required or not required[name](labels):
            continue
        if any(label in labels for label in ("device", "client_id", "session_id", "email", "api_key")):
            return fail("forbidden_label_present")
        if labels.get("job") != "receiver-host" or labels.get("service") != "o11y/receiver-host":
            return fail("receiver_identity_missing")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", str(labels.get("cluster", ""))):
            return fail("receiver_scope_labels_missing")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", str(labels.get("environment", ""))):
            return fail("receiver_scope_labels_missing")
        found[name].append(item)

    for name, items in found.items():
        if len(items) < 1:
            return fail("required_metric_missing")
        if name != "node_cpu_seconds_total" and len(items) != 1:
            return fail("required_metric_ambiguous")

    def metric_value(name: str) -> float:
        try:
            value = float(found[name][0]["value"][1])
        except (KeyError, TypeError, ValueError, IndexError):
            raise ValueError from None
        if not math.isfinite(value) or value < 0:
            raise ValueError
        return value

    try:
        host_size = metric_value("node_filesystem_size_bytes")
        host_avail = metric_value("node_filesystem_avail_bytes")
        mem_total = metric_value("node_memory_MemTotal_bytes")
        mem_avail = metric_value("node_memory_MemAvailable_bytes")
        metric_value("node_load1")
        metric_value("node_cpu_seconds_total")
    except ValueError:
        return fail("required_metric_value_invalid")

    # `df -B1 --output=size,avail /` reports guest-visible root values in bytes.
    if host_size <= 0 or mem_total <= 0 or host_avail > host_size:
        return fail("capacity_values_invalid")
    if int(host_size) != guest_values[0]:
        return fail("host_guest_root_filesystem_mismatch")
    # Prometheus samples can trail the guest-side df read while TSDB writes use
    # space. Allow a small bounded delta while still rejecting a different root.
    avail_tolerance = max(64 * 1024 * 1024, int(host_size * 0.01))
    if abs(int(host_avail) - guest_values[1]) > avail_tolerance:
        return fail("host_guest_root_filesystem_mismatch")
    if mem_avail > mem_total:
        return fail("memory_values_invalid")

    print(json.dumps(
        {"ok": True, "root_size_bytes": int(host_size), "root_available_bytes": int(host_avail)},
        sort_keys=True,
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
