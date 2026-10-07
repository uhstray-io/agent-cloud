#!/usr/bin/env python3
"""Reduce a one-shot `podman stats` readback to container names and MiB.

Input (stdin JSON): `listing_rc` and `containers` are the exit code and the
`<Names> <State>` lines of the compose working-directory selector
(tasks/list-service-containers.yml, stopped containers included); `stats_rc` and
`stats` are the exit code and stdout of `podman stats --no-stream --no-reset
--format '{{.Name}} {{.ContainerStats.MemUsage}}' <names>`. `ContainerStats.MemUsage` is
the raw byte count podman takes from the cgroup (on cgroup v2 it is
memory.current minus inactive_file), so no human-formatted size is re-parsed.

Fails closed: every listed container must be running and have exactly one
integer reading, and nothing but names and MiB values is ever printed.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
BYTES = re.compile(r"^[0-9]+$")
MIB = 1024 * 1024


def summarize(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    if payload.get("listing_rc") != 0:
        return 1, {"status": "unavailable", "reason": "container_listing_unreadable"}
    listing = payload.get("containers")
    rows = [str(row).split() for row in listing] if isinstance(listing, list) else []
    containers = [row[0] for row in rows]
    if (not rows or not all(len(row) == 2 and NAME.fullmatch(row[0]) for row in rows)
            or len(set(containers)) != len(containers)):
        return 1, {"status": "unavailable", "reason": "container_listing_invalid"}
    stopped = sorted(name for name, state in rows if state != "running")
    if stopped:
        return 1, {"status": "unavailable", "reason": "container_not_running",
                   "stopped_containers": stopped}
    if payload.get("stats_rc") != 0:
        return 1, {"status": "unavailable", "reason": "stats_unreadable"}

    readings: dict[str, int] = {}
    for line in str(payload.get("stats", "")).splitlines():
        if not line.strip():
            continue
        fields = line.split()
        if (len(fields) != 2 or fields[0] not in containers or fields[0] in readings
                or not BYTES.fullmatch(fields[1])):
            return 1, {"status": "unavailable", "reason": "stats_output_unrecognized"}
        readings[fields[0]] = int(fields[1])

    missing = sorted(set(containers) - set(readings))
    if missing:
        return 1, {"status": "unavailable", "reason": "container_reading_missing",
                   "missing_containers": missing}
    return 0, {"status": "observed",
               "containers": [{"name": name, "memory_mib": round(readings[name] / MIB, 1)}
                              for name in sorted(readings)]}


if __name__ == "__main__":
    try:
        input_data = json.load(sys.stdin)
    except (TypeError, ValueError):
        input_data = {}
    code, result = summarize(input_data if isinstance(input_data, dict) else {})
    print(json.dumps(result, sort_keys=True))
    sys.exit(code)
