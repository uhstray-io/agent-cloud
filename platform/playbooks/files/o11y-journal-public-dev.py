#!/usr/bin/env python3
"""Require the fixed public Dev ref to match the reviewed journal SHA."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

PUBLIC_DEV_REMOTE = "https://github.com/uhstray-io/agent-cloud.git"
GIT_EXECUTABLE = "/usr/bin/git"
ASKPASS_EXECUTABLE = "/usr/bin/false"


def verify_public_dev(expected_sha: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{40}", expected_sha):
        raise ValueError("expected_revision_invalid")
    argv = [
        GIT_EXECUTABLE, "-c", "credential.helper=", "-c",
        f"core.askPass={ASKPASS_EXECUTABLE}", "-c", "core.fsmonitor=false",
        "-c", "http.sslVerify=true", "-c", "http.proxy=", "ls-remote",
        "--exit-code", "--refs", PUBLIC_DEV_REMOTE, "refs/heads/dev",
    ]
    environment = {
        "HOME": os.devnull,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": ASKPASS_EXECUTABLE,
        "SSH_ASKPASS": ASKPASS_EXECUTABLE,
    }
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=15, check=False,
            env=environment, cwd="/",
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("public_dev_read_failed") from exc
    if result.returncode or result.stdout != f"{expected_sha}\trefs/heads/dev\n":
        raise RuntimeError("public_dev_revision_mismatch")
    return {"status": "verified", "public_dev_revision": expected_sha}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-sha", required=True)
    args = parser.parse_args(argv)
    try:
        result = verify_public_dev(args.expected_sha)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        reason = str(exc) if str(exc) in {
            "expected_revision_invalid", "public_dev_read_failed", "public_dev_revision_mismatch",
        } else "public_dev_check_failed"
        print(json.dumps({"status": "refused", "reason": reason}, separators=(",", ":")))
        return 1
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
