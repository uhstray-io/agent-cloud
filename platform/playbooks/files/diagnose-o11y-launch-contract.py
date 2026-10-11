#!/usr/bin/env python3
"""Report bounded task-local process facts without asserting launch authority."""

from __future__ import annotations

import json
import os
import sys

MAX_CGROUP_BYTES = 4096
PREREQUISITES = (
    "executor_identity",
    "reviewed_artifact_identity",
    "protected_installation",
    "effective_launch_configuration",
    "neighboring_task_token_isolation",
    "protected_launch_interception",
    "signer_issuance",
    "cancellation_fencing",
    "descendant_containment",
)


def _cgroup_mode(raw: bytes) -> str:
    lines = raw.decode("utf-8", errors="strict").splitlines()
    modes = set()
    for line in lines:
        hierarchy, separator, controllers = line.partition(":")
        if not separator or not hierarchy.isdecimal():
            raise ValueError("malformed cgroup row")
        controllers, separator, _path = controllers.partition(":")
        if not separator:
            raise ValueError("malformed cgroup row")
        modes.add("v2" if hierarchy == "0" and not controllers else "v1")
    if modes == {"v1"}:
        return "v1"
    if modes == {"v2"}:
        return "v2"
    if modes == {"v1", "v2"}:
        return "hybrid"
    return "unknown"


def _observation(value: str, reason: str = "observed") -> dict[str, str]:
    return {
        "status": "observed" if reason == "observed" else "refused",
        "value": value if reason == "observed" else "unknown",
        "reason": reason,
    }


def _local_observations() -> dict[str, dict[str, str]]:
    try:
        uid = _observation("root" if os.geteuid() == 0 else "nonroot")
    except OSError:
        uid = _observation("unknown", "probe_failed")
    try:
        with open("/proc/self/cgroup", "rb") as source:
            raw = source.read(MAX_CGROUP_BYTES + 1)
        if not raw:
            cgroup = _observation("unknown", "probe_missing")
        elif len(raw) > MAX_CGROUP_BYTES:
            cgroup = _observation("unknown", "probe_oversized")
        else:
            mode = _cgroup_mode(raw)
            cgroup = _observation(mode, "observed" if mode != "unknown" else "probe_malformed")
    except FileNotFoundError:
        cgroup = _observation("unknown", "probe_missing")
    except OSError:
        cgroup = _observation("unknown", "probe_unavailable")
    except (UnicodeDecodeError, ValueError):
        cgroup = _observation("unknown", "probe_malformed")
    return {"probe_process_uid": uid, "probe_process_cgroup_mode": cgroup}


def diagnose() -> dict:
    local = _local_observations()
    return {
        "schema_version": 1,
        "status": "unverified",
        "local_observations": local,
        "launch_prerequisites": dict.fromkeys(PREREQUISITES, "unverified"),
        "actions": {
            "api_requests": "not_called",
            "network_requests": "not_called",
            "target_probes": "not_called",
            "writes": "not_called",
        },
    }


def unknown_report(reason: str = "probe_failed") -> dict:
    if reason not in {"probe_failed", "invalid_input"}:
        reason = "probe_failed"
    return {
        "schema_version": 1,
        "status": "unverified",
        "local_observations": {
            "probe_process_uid": _observation("unknown", reason),
            "probe_process_cgroup_mode": _observation("unknown", reason),
        },
        "launch_prerequisites": dict.fromkeys(PREREQUISITES, "unverified"),
        "actions": {
            "api_requests": "not_called",
            "network_requests": "not_called",
            "target_probes": "not_called",
            "writes": "not_called",
        },
    }


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    result = unknown_report("invalid_input") if arguments else diagnose()
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
