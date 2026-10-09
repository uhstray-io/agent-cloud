#!/usr/bin/env python3
"""Report only whether the selected journal contains the pilot container."""

import json
import subprocess
import sys

CONTAINER = "o11y-alloy"


def probe(directory, run=subprocess.run):
    try:
        result = run(
            [
                "journalctl",
                f"--directory={directory}",
                "--no-pager",
                "--quiet",
                "--output=json",
                "--output-fields=CONTAINER_NAME",
                "--since=-15min",
                "--lines=1",
                f"CONTAINER_NAME={CONTAINER}",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"status": "unavailable", "matching_entries": 0}

    if result.returncode != 0:
        return {"status": "unavailable", "matching_entries": 0}

    count = 0
    for line in result.stdout.splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            return {"status": "unavailable", "matching_entries": 0}
        if not isinstance(entry, dict):
            return {"status": "unavailable", "matching_entries": 0}
        names = entry.get("CONTAINER_NAME")
        if isinstance(names, str):
            names = [names]
        if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
            return {"status": "unavailable", "matching_entries": 0}
        count += CONTAINER in names

    return {
        "status": "observed" if count else "unavailable",
        "matching_entries": count,
    }


def main():
    if len(sys.argv) != 2:
        print(json.dumps({"status": "unavailable", "matching_entries": 0}))
        return 1
    report = probe(sys.argv[1])
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "observed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
