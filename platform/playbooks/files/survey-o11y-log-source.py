#!/usr/bin/env python3
"""Return bounded receiver-host container log-source metadata only."""

import json
import subprocess
import sys

DRIVERS = {"journald", "k8s-file", "json-file", "none", "other", "unknown"}
JOURNAL_WINDOW = "-15min"
O11Y_PREFIX = "o11y-"
# shortcut: only the pinned Alloy journal destinations count; extend when config adopts another target.
JOURNAL_DESTINATIONS = {"/var/log/journal", "/run/log/journal"}


def run(argv, timeout=8):
    try:
        result = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode, result.stdout
    except (OSError, subprocess.TimeoutExpired):
        return None, ""


def decode_json(value):
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None


def driver(value):
    if not isinstance(value, str) or not value:
        return "unknown"
    normalized = value.lower()
    return normalized if normalized in DRIVERS - {"unknown", "other"} else "other"


def inspect(name):
    rc, output = run(["podman", "inspect", "--type", "container", "--format", "json", name])
    data = decode_json(output) if rc == 0 else None
    return data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else None


def log_driver(container):
    host = container.get("HostConfig")
    config = host.get("LogConfig") if isinstance(host, dict) else None
    value = config.get("Type") if isinstance(config, dict) else None
    return driver(value)


def default_driver():
    rc, output = run(["podman", "info", "--format", "json"])
    data = decode_json(output) if rc == 0 else None
    host = data.get("host") if isinstance(data, dict) else None
    return driver(host.get("logDriver") if isinstance(host, dict) else None)


def running_containers():
    rc, output = run(["podman", "ps", "--filter", "status=running", "--format", "json"])
    data = decode_json(output) if rc == 0 else None
    if not isinstance(data, list):
        return None
    names = []
    for item in data:
        if not isinstance(item, dict):
            continue
        values = item.get("Names", [])
        if isinstance(values, str):
            values = [values]
        if isinstance(values, list):
            names.extend(value for value in values if isinstance(value, str) and value.startswith(O11Y_PREFIX))
    return sorted(set(names))


def journal_entry_count(output, expected_name):
    count = 0
    for line in output.splitlines():
        if not line.strip():
            continue
        entry = decode_json(line)
        if not isinstance(entry, dict):
            return None
        names = entry.get("CONTAINER_NAME")
        if isinstance(names, str):
            names = [names]
        if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
            return None
        count += expected_name in names
    return count


def journal_status(names, drivers):
    journal_names = [name for name in names if drivers.get(name) == "journald"]
    if not journal_names:
        return "unsupported", 0
    count = 0
    for name in journal_names:
        rc, output = run(
            [
                "journalctl",
                "--user",
                "--no-pager",
                "--quiet",
                "--output=json",
                "--output-fields=CONTAINER_NAME",
                f"--since={JOURNAL_WINDOW}",
                "-n",
                "100",
                f"CONTAINER_NAME={name}",
            ],
            timeout=12,
        )
        if rc is None:
            return "unverified", 0
        if rc != 0:
            return "unreadable", 0
        entry_count = journal_entry_count(output, name)
        if entry_count is None:
            return "unverified", 0
        count += entry_count
    return ("readable" if count else "no_entries"), count


def alloy_source():
    container = inspect("o11y-alloy")
    if container is None:
        return False
    mounts = container.get("Mounts")
    if not isinstance(mounts, list):
        return False
    for mount in mounts:
        if not isinstance(mount, dict):
            continue
        destination = mount.get("Destination")
        source = mount.get("Source")
        if destination in JOURNAL_DESTINATIONS and isinstance(source, str) and source and mount.get("RW") is False:
            rc, _ = run(
                ["podman", "exec", "o11y-alloy", "sh", "-c", f"test -d {destination} && test -r {destination}"]
            )
            if rc == 0:
                return True
    return False


def survey():
    default = default_driver()
    names = running_containers()
    if names is None:
        return {"status": "unavailable", "reason": "container_metadata_unavailable"}
    if not names:
        return {"status": "unavailable", "reason": "no_running_o11y_containers"}
    counts = dict.fromkeys(sorted(DRIVERS), 0)
    drivers = {}
    for name in names:
        container = inspect(name)
        value = log_driver(container) if container is not None else "unknown"
        drivers[name] = value
        counts[value] += 1
    journal, journal_count = journal_status(names, drivers)
    return {
        "status": "observed",
        "rootless_default_log_driver": default,
        "running_o11y_container_count": len(names),
        "running_log_driver_counts": counts,
        "journald_metadata_read": journal,
        "journald_metadata_entry_count": journal_count,
        "alloy_read_only_journal_source_mounted": alloy_source(),
    }


if __name__ == "__main__":
    try:
        print(json.dumps(survey(), sort_keys=True))
    except Exception:
        print(json.dumps({"status": "unavailable", "reason": "survey_failed"}, sort_keys=True))
        sys.exit(1)
