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
            return None
        values = item.get("Names")
        if isinstance(values, str):
            values = [values]
        if (
            not isinstance(values, list)
            or not values
            or any(not isinstance(value, str) or not value for value in values)
        ):
            return None
        names.extend(value for value in values if value.startswith(O11Y_PREFIX))
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
        return None
    state = container.get("State")
    if not isinstance(state, dict) or state.get("Running") is not True:
        return None
    mounts = container.get("Mounts")
    if not isinstance(mounts, list):
        return None
    uncertain = False
    for mount in mounts:
        if not isinstance(mount, dict):
            return None
        destination = mount.get("Destination")
        if destination not in JOURNAL_DESTINATIONS:
            continue
        writable = mount.get("RW")
        if writable is True:
            continue
        if writable is not False:
            uncertain = True
            continue
        source = mount.get("Source")
        if not isinstance(source, str) or not source:
            uncertain = True
            continue
        rc, output = run(
            [
                "podman",
                "exec",
                "o11y-alloy",
                "sh",
                "-c",
                f'if [ ! -d {destination} ]; then printf missing; '
                f'elif [ ! -r {destination} ]; then printf unreadable; '
                "else printf readable; fi",
            ]
        )
        if rc is None or rc != 0:
            uncertain = True
            continue
        probe = output.strip()
        if probe == "readable":
            return True
        if probe not in {"missing", "unreadable"}:
            uncertain = True
    if uncertain:
        return None
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
        if container is None:
            return {"status": "unavailable", "reason": "container_metadata_unavailable"}
        value = log_driver(container)
        drivers[name] = value
        counts[value] += 1
    journal, journal_count = journal_status(names, drivers)
    alloy_mounted = alloy_source()
    if alloy_mounted is None:
        return {"status": "unavailable", "reason": "alloy_source_unverified"}
    return {
        "status": "observed",
        "rootless_default_log_driver": default,
        "running_o11y_container_count": len(names),
        "running_log_driver_counts": counts,
        "journald_metadata_read": journal,
        "journald_metadata_entry_count": journal_count,
        "alloy_read_only_journal_source_mounted": alloy_mounted,
    }


if __name__ == "__main__":
    try:
        print(json.dumps(survey(), sort_keys=True))
    except Exception:
        print(json.dumps({"status": "unavailable", "reason": "survey_failed"}, sort_keys=True))
        sys.exit(1)
