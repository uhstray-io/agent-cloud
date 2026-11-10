#!/usr/bin/env python3
"""Publish one complete, sanitized receiver capacity sample for node-exporter."""

import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

INSPECTOR = "/usr/local/libexec/o11y/inspect-o11y-volume-capacity.py"
TEXTFILE = Path("/var/lib/node_exporter/textfile/o11y_capacity.prom")
VOLUME_BACKENDS = {
    "prometheus-data": "prometheus",
    "loki-data": "loki",
    "tempo-data": "tempo",
    "grafana-data": "grafana",
    "pyroscope-data": "pyroscope",
}
BACKENDS = set(VOLUME_BACKENDS.values())


def metrics_from_report(report: object, sampled_at: int) -> str | None:
    if not isinstance(report, dict) or report.get("status") != "observed":
        return None
    volumes = report.get("volumes")
    if not isinstance(volumes, list) or len(volumes) != len(BACKENDS):
        return None
    sizes: dict[str, int] = {}
    for item in volumes:
        if not isinstance(item, dict):
            return None
        volume, size = item.get("volume"), item.get("stored_bytes")
        if (
            not isinstance(volume, str)
            or volume not in VOLUME_BACKENDS
            or volume in sizes
            or type(size) is not int
            or size < 0
        ):
            return None
        sizes[VOLUME_BACKENDS[volume]] = size
    if set(sizes) != BACKENDS:
        return None
    lines = [
        "# HELP o11y_backend_stored_bytes Bytes stored in each fixed o11y backend volume.",
        "# TYPE o11y_backend_stored_bytes gauge",
        "# HELP o11y_capacity_sample_timestamp_seconds Unix time of the last complete capacity sample.",
        "# TYPE o11y_capacity_sample_timestamp_seconds gauge",
        f"o11y_capacity_sample_timestamp_seconds {sampled_at}",
    ]
    lines.extend(f'o11y_backend_stored_bytes{{backend="{name}"}} {sizes[name]}' for name in sorted(BACKENDS))
    return "\n".join(lines) + "\n"


def atomic_write(target: Path, contents: str) -> None:
    parent = target.parent
    if not stat.S_ISDIR(os.lstat(parent).st_mode):
        raise OSError("textfile directory is not a directory")
    fd, temporary = tempfile.mkstemp(prefix=".o11y_capacity.", dir=parent)
    try:
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "w", encoding="ascii", newline="\n") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> int:
    try:
        proc = subprocess.run(
            [sys.executable, INSPECTOR, "podman"],
            check=False,
            capture_output=True,
            text=True,
            timeout=540,
        )
        report = json.loads(proc.stdout) if proc.returncode == 0 else None
        contents = metrics_from_report(report, int(time.time()))
        if contents is None:
            raise ValueError("incomplete capacity observation")
        atomic_write(TEXTFILE, contents)
    except (OSError, ValueError, subprocess.TimeoutExpired, json.JSONDecodeError):
        print("o11y capacity sample unavailable", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
