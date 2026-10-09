#!/usr/bin/env python3
"""Survey only the two standard host journal directories for the pilot."""

import json
import os
import subprocess

IMAGE = "docker.io/grafana/alloy:v1.5.1"
TARGET = "o11y-alloy"
MOUNT = "/var/log/journal"
DIRECTORIES = ("/var/log/journal", "/run/log/journal")
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
    except (OSError, subprocess.TimeoutExpired):
        return None


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
    print(json.dumps(survey(), sort_keys=True))


if __name__ == "__main__":
    main()
