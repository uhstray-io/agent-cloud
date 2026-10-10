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


class Refusal(Exception):
    """A bounded fail-closed workflow refusal."""


def verify_checkout(path: Path, expected_sha: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{40}", expected_sha):
        raise Refusal("expected_revision_invalid")
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise Refusal("checkout_unavailable") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise Refusal("checkout_unavailable")
    environment = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"}
    try:
        root = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10, check=False, env=environment,
        )
        revision = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--verify", "HEAD"],
            capture_output=True, text=True, timeout=10, check=False, env=environment,
        )
        status = subprocess.run(
            ["git", "-C", str(path), "status", "--porcelain=v1", "--untracked-files=all"],
            capture_output=True, text=True, timeout=10, check=False, env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Refusal("checkout_read_failed") from exc
    if root.returncode or Path(root.stdout.strip()).resolve() != path.resolve():
        raise Refusal("checkout_root_mismatch")
    head = revision.stdout.strip()
    if revision.returncode or not re.fullmatch(r"[0-9a-f]{40}", head):
        raise Refusal("checkout_revision_unavailable")
    if head != expected_sha:
        raise Refusal("checkout_revision_mismatch")
    if status.returncode or status.stdout:
        raise Refusal("checkout_dirty_or_unknown")
    return {"status": "verified", "revision": head, "clean": True}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("verify-checkout",))
    parser.add_argument("--checkout")
    parser.add_argument("--expected-sha")
    args = parser.parse_args(argv)
    try:
        if args.action == "verify-checkout" and args.checkout and args.expected_sha:
            result = verify_checkout(Path(args.checkout), args.expected_sha)
        else:
            raise Refusal("arguments_invalid")
    except Refusal as exc:
        print(json.dumps({"status": "refused", "reason": str(exc)}, separators=(",", ":")))
        return 1
    except (OSError, TypeError, ValueError):
        print(json.dumps({"status": "refused", "reason": "workflow_operation_failed"}, separators=(",", ":")))
        return 1
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
