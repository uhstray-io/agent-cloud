#!/usr/bin/env python3
"""Verify the exact clean receiver checkout before journal actions."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

PUBLIC_DEV_REMOTE = "https://github.com/uhstray-io/agent-cloud.git"
GIT_EXECUTABLE = Path("/usr/bin/git")
ASKPASS_EXECUTABLE = "/usr/bin/false"


class Refusal(Exception):
    """A bounded fail-closed workflow refusal."""


def _git_environment() -> dict[str, str]:
    return {
        "HOME": os.devnull,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": ASKPASS_EXECUTABLE,
        "SSH_ASKPASS": ASKPASS_EXECUTABLE,
    }


def _git_argv(*arguments: str) -> list[str]:
    if not GIT_EXECUTABLE.is_file() or not os.access(GIT_EXECUTABLE, os.X_OK):
        raise Refusal("git_unavailable")
    return [
        str(GIT_EXECUTABLE), "-c", "credential.helper=", "-c",
        f"core.askPass={ASKPASS_EXECUTABLE}", "-c", "core.fsmonitor=false", *arguments,
    ]


def verify_checkout(path: Path, expected_sha: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{40}", expected_sha):
        raise Refusal("expected_revision_invalid")
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise Refusal("checkout_unavailable") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise Refusal("checkout_unavailable")
    environment = _git_environment()
    try:
        root = subprocess.run(
            _git_argv("-C", str(path), "rev-parse", "--show-toplevel"),
            capture_output=True, text=True, timeout=10, check=False, env=environment,
        )
        if root.returncode or root.stdout != f"{path.resolve()}\n":
            raise Refusal("checkout_root_mismatch")
        revision = subprocess.run(
            _git_argv("-C", str(path), "rev-parse", "--verify", "HEAD"),
            capture_output=True, text=True, timeout=10, check=False, env=environment,
        )
        head = revision.stdout[:-1] if revision.stdout.endswith("\n") else ""
        if revision.returncode or not re.fullmatch(r"[0-9a-f]{40}", head):
            raise Refusal("checkout_revision_unavailable")
        if head != expected_sha:
            raise Refusal("checkout_revision_mismatch")
        origin_dev = subprocess.run(
            _git_argv("-C", str(path), "rev-parse", "--verify", "refs/remotes/origin/dev^{commit}"),
            capture_output=True, text=True, timeout=10, check=False, env=environment,
        )
        origin_dev_sha = origin_dev.stdout[:-1] if origin_dev.stdout.endswith("\n") else ""
        if origin_dev.returncode or not re.fullmatch(r"[0-9a-f]{40}", origin_dev_sha):
            raise Refusal("checkout_dev_ref_unavailable")
        if origin_dev_sha != expected_sha:
            raise Refusal("checkout_dev_ref_mismatch")
        status = subprocess.run(
            _git_argv("-C", str(path), "status", "--porcelain=v1", "--untracked-files=all"),
            capture_output=True, text=True, timeout=10, check=False, env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Refusal("checkout_read_failed") from exc
    if status.returncode or status.stdout:
        raise Refusal("checkout_dirty_or_unknown")
    return {"status": "verified", "revision": head, "origin_dev_revision": origin_dev_sha, "clean": True}


def verify_dev_source(path: Path, expected_sha: str) -> dict:
    result = verify_checkout(path, expected_sha)
    environment = _git_environment()
    try:
        remote = subprocess.run(
            _git_argv(
                "-c", "http.sslVerify=true", "-c", "http.proxy=",
                "ls-remote", "--exit-code", "--refs", PUBLIC_DEV_REMOTE, "refs/heads/dev",
            ),
            capture_output=True, text=True, timeout=15, check=False, env=environment, cwd="/",
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Refusal("public_dev_read_failed") from exc
    if remote.returncode or remote.stdout != f"{expected_sha}\trefs/heads/dev\n":
        raise Refusal("public_dev_revision_mismatch")
    return {**result, "public_dev_revision": expected_sha}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("verify-checkout", "verify-dev-source"))
    parser.add_argument("--checkout")
    parser.add_argument("--expected-sha")
    args = parser.parse_args(argv)
    try:
        if args.action in ("verify-checkout", "verify-dev-source") and args.checkout and args.expected_sha:
            verifier = verify_dev_source if args.action == "verify-dev-source" else verify_checkout
            result = verifier(Path(args.checkout), args.expected_sha)
        else:
            raise Refusal("arguments_invalid")
    except Refusal as exc:
        print(json.dumps({"status": "refused", "reason": str(exc)}, separators=(",", ":")))
        return 1
    except (OSError, RuntimeError, TypeError, ValueError):
        print(json.dumps({"status": "refused", "reason": "workflow_operation_failed"}, separators=(",", ":")))
        return 1
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
