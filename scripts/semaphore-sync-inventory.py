#!/usr/bin/env python3
"""Run the canonical Semaphore inventory sync with an operator token from stdin."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLAYBOOK = ROOT / "platform" / "semaphore" / "sync-inventory.yml"
DIFF_SUMMARY = "Validated inventory differs; the existing record will be updated."
MATCH_SUMMARY = "Validated inventory already matches; no write is needed."


def ansible_child_environment() -> dict[str, str]:
    """Keep this stdin-token child from serializing URI arguments to temp files."""
    child_env = os.environ.copy()
    child_env.pop("SEMAPHORE_TOKEN", None)
    child_env["ANSIBLE_PIPELINING"] = "True"
    child_env["ANSIBLE_KEEP_REMOTE_FILES"] = "False"
    return child_env


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="Semaphore HTTPS or loopback HTTP origin")
    parser.add_argument("--inventory", required=True, help="inventory file to synchronize")
    parser.add_argument("--name", default="production", help="existing Semaphore inventory name")
    parser.add_argument("--project", type=int, default=1, help="Semaphore project ID")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="write a differing inventory and verify readback")
    mode.add_argument("--check", action="store_true", help="preview without updating the record (default)")
    args = parser.parse_args()

    token = sys.stdin.read()
    if token.endswith("\n"):
        token = token[:-1]
        if token.endswith("\r"):
            token = token[:-1]
    if not token or "\n" in token or "\r" in token:
        print("Invalid operator token input; no synchronization was started.", file=sys.stderr)
        return 2

    payload = {
        "sync_inventory_token": token,
        "semaphore_url": args.url,
        "semaphore_project_id": args.project,
        "inventory_name": args.name,
        "inventory_source": str(Path(args.inventory).expanduser().resolve()),
    }
    argv = ["ansible-playbook", "-i", "localhost,", str(PLAYBOOK), "--extra-vars", "@/dev/stdin"]
    if not args.apply:
        argv.append("--check")
    child_env = ansible_child_environment()
    try:
        result = subprocess.run(
            argv,
            cwd=ROOT,
            env=child_env,
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError:
        print("Unable to start the inventory synchronization play; it did not start.", file=sys.stderr)
        return 1

    if result.returncode != 0:
        if not args.apply:
            print("Inventory check failed; no write was requested.", file=sys.stderr)
            return 1
        print(
            "Inventory synchronization did not complete. The write outcome is uncertain; "
            "reconcile the existing record with read-only access before any retry.",
            file=sys.stderr,
        )
        return 1

    if DIFF_SUMMARY in result.stdout:
        outcome = (
            "Inventory check completed; the existing record differs."
            if not args.apply
            else "Inventory synchronization completed; updated content was verified."
        )
    elif MATCH_SUMMARY in result.stdout:
        outcome = (
            "Inventory check completed; the existing record already matches."
            if not args.apply
            else "Inventory synchronization completed; the existing record already matched."
        )
    else:
        print(
            "Inventory synchronization returned no safe summary; reconcile read-only before retrying.",
            file=sys.stderr,
        )
        return 1
    print(outcome)
    return 0


if __name__ == "__main__":
    sys.exit(main())
