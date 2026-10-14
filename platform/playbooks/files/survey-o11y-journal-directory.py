#!/usr/bin/env python3
"""Survey only the two standard host journal directories for the pilot."""

import json
import os
import subprocess

IMAGE = "docker.io/grafana/alloy:v1.5.1"
TARGET = "o11y-alloy"
COLLECTOR = "o11y-journal-collector"
MOUNT = "/var/log/journal"
DIRECTORIES = ("/var/log/journal", "/run/log/journal")
MAX_DIAGNOSTIC_ENTRIES = 80
PERMISSION_TARGETS = {
    "journal": ("/var/log/journal", "/run/log/journal", "journald"),
    "positions": ("/var/lib/alloy/data", "/alloy-state", "positions"),
    "config": ("/etc/alloy/journal.alloy", "journal.alloy", "config"),
}
READ_SCRIPT = (
    "shopt -s globstar nullglob; "
    "files=(/var/log/journal/**/*.journal); "
    'for file in "${files[@]}"; do '
    'if [[ -r "$file" ]] && read -r -N 1 _ < "$file"; then exit 0; fi; '
    "done; exit 1"
)


def _call(run, argv):
    try:
        return run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=12,
        )
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        return None


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def permission_diagnostic(run=subprocess.run):
    unavailable = {"entry_count": 0, "permission_denied_target": "unavailable"}
    result = _call(
        run,
        [
            "journalctl",
            "--no-pager",
            "--quiet",
            "--output=json",
            "--output-fields=CONTAINER_NAME,MESSAGE",
            "--since=-24h",
            f"--lines={MAX_DIAGNOSTIC_ENTRIES}",
            f"CONTAINER_NAME={COLLECTOR}",
        ],
    )
    if result is None or result.returncode != 0 or not isinstance(result.stdout, str):
        return unavailable

    lines = result.stdout.splitlines()
    if len(lines) > MAX_DIAGNOSTIC_ENTRIES:
        return unavailable

    permission_targets = set()
    for line in lines:
        try:
            entry = json.loads(line, object_pairs_hook=_unique_object)
        except ValueError:
            return unavailable
        if (
            not isinstance(entry, dict)
            or entry.get("CONTAINER_NAME") != COLLECTOR
            or not isinstance(entry.get("MESSAGE"), str)
        ):
            return unavailable

        message = entry["MESSAGE"].lower()
        if "permission denied" not in message and "permission_denied" not in message:
            continue
        matches = [
            category
            for category, markers in PERMISSION_TARGETS.items()
            if any(marker in message for marker in markers)
        ]
        permission_targets.add(matches[0] if len(matches) == 1 else "other")

    if not permission_targets:
        target = "none"
    elif len(permission_targets) == 1:
        target = permission_targets.pop()
    else:
        target = "other"
    return {"entry_count": len(lines), "permission_denied_target": target}


def _has_target_entry(result):
    if result is None or result.returncode != 0:
        return False
    for line in result.stdout.splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            return False
        names = entry.get("CONTAINER_NAME") if isinstance(entry, dict) else None
        if isinstance(names, str):
            names = [names]
        if isinstance(names, list) and TARGET in names:
            return True
    return False


def _candidate_is_viable(directory, run, isdir, access):
    if not isdir(directory) or not access(directory, os.R_OK | os.X_OK):
        return False
    journal = _call(
        run,
        [
            "journalctl",
            f"--directory={directory}",
            "--no-pager",
            "--quiet",
            "--output=json",
            "--output-fields=CONTAINER_NAME",
            "--since=-15min",
            "--lines=1",
            f"CONTAINER_NAME={TARGET}",
        ],
    )
    if not _has_target_entry(journal):
        return False

    readable = _call(
        run,
        [
            "podman",
            "run",
            "--pull=never",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--user",
            "0:0",
            "--mount",
            f"type=bind,src={directory},dst={MOUNT},ro",
            "--entrypoint",
            "bash",
            IMAGE,
            "-c",
            READ_SCRIPT,
        ],
    )
    return readable is not None and readable.returncode == 0


def _image_is_available(run):
    exists = _call(run, ["podman", "image", "exists", IMAGE])
    return exists is not None and exists.returncode == 0


def survey(run=subprocess.run, isdir=os.path.isdir, access=os.access):
    rootless = _call(run, ["podman", "info", "--format={{.Host.Security.Rootless}}"])
    if rootless is None or rootless.returncode != 0 or rootless.stdout.strip().lower() != "true":
        return {"result": "none"}
    if not _image_is_available(run):
        return {"result": "probe_unavailable"}

    viable = [directory for directory in DIRECTORIES if _candidate_is_viable(directory, run, isdir, access)]
    if len(viable) == 1:
        return {"result": viable[0]}
    return {"result": "ambiguous" if len(viable) > 1 else "none"}


def main():
    report = survey()
    report.update(permission_diagnostic())
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
