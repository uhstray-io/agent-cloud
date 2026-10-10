#!/usr/bin/env python3
"""Fail-closed checkout and durable journal lifecycle admission helpers."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import secrets
import stat
import subprocess
import sys
from pathlib import Path

MAX_RECORD = 4096
OPERATIONS = {"survey", "prove-direct", "apply", "repair-positions", "stop", "rollback"}
RECEIVER_OPERATIONS = {"receiver-deploy", "receiver-clean"}


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


def _directory(path: Path, create: bool = False) -> None:
    try:
        if create:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = path.lstat()
    except OSError as exc:
        raise Refusal("admission_directory_unavailable") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise Refusal("admission_directory_refused")


def _locked(path: Path, create_directory: bool = False):
    _directory(path.parent, create=create_directory)
    lock_path = path.with_suffix(".lock")
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError as exc:
        raise Refusal("admission_lock_refused") from exc
    try:
        metadata = os.fstat(fd)
        named = lock_path.lstat()
    except OSError as exc:
        os.close(fd)
        raise Refusal("admission_lock_refused") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or (metadata.st_dev, metadata.st_ino) != (named.st_dev, named.st_ino)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        os.close(fd)
        raise Refusal("admission_lock_refused")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError as exc:
        os.close(fd)
        raise Refusal("admission_lock_refused") from exc
    return fd


def _read(path: Path) -> tuple[dict, os.stat_result] | None:
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise Refusal("admission_state_unavailable") from exc
    if not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid() or stat.S_IMODE(before.st_mode) != 0o600:
        raise Refusal("admission_state_refused")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise Refusal("admission_state_refused") from exc
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise Refusal("admission_state_refused")
        raw = os.read(fd, MAX_RECORD + 1)
        if len(raw) > MAX_RECORD:
            raise Refusal("admission_state_invalid")
    finally:
        os.close(fd)
    try:
        record = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Refusal("admission_state_invalid") from exc
    if not isinstance(record, dict) or record.get("schema") != 1:
        raise Refusal("admission_state_invalid")
    required = {"schema", "state", "operation", "owner_id", "task_id", "nonce"}
    if set(record) != required:
        raise Refusal("admission_state_invalid")
    if (
        record["state"] not in {"intent", "complete"}
        or record["operation"] not in OPERATIONS
        or not isinstance(record["owner_id"], str)
        or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", record["owner_id"])
        or not isinstance(record["task_id"], int)
        or isinstance(record["task_id"], bool)
        or record["task_id"] < 1
        or not isinstance(record["nonce"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", record["nonce"])
    ):
        raise Refusal("admission_state_invalid")
    return record, before


def _write(path: Path, record: dict) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            raw = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("ascii")
            view = memoryview(raw)
            while view:
                count = os.write(fd, view)
                if count <= 0:
                    raise Refusal("admission_persist_refused")
                view = view[count:]
            os.fsync(fd)
            identity = os.fstat(fd)
        finally:
            os.close(fd)
        os.replace(temporary, path)
        parent_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        saved, saved_identity = _read(path)  # verify both bytes and private inode identity
        if saved != record or (identity.st_dev, identity.st_ino) != (saved_identity.st_dev, saved_identity.st_ino):
            raise Refusal("admission_readback_refused")
    except Refusal:
        raise
    except OSError as exc:
        raise Refusal("admission_persist_refused") from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def begin(path: Path, operation: str, owner_id: str, task_id: int, *, authenticated: bool) -> dict:
    """Persist intent before mutation; the same owner can finish from another Ansible task."""
    if operation not in OPERATIONS or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", owner_id):
        raise Refusal("admission_arguments_invalid")
    if not authenticated or not isinstance(task_id, int) or isinstance(task_id, bool) or task_id < 1:
        raise Refusal("task_attribution_unverified")
    fd = _locked(path)
    try:
        current = _read(path)
        if current is not None and current[0]["state"] != "complete":
            raise Refusal("admission_intent_unresolved")
        record = {
            "schema": 1,
            "state": "intent",
            "operation": operation,
            "owner_id": owner_id,
            "task_id": task_id,
            "nonce": secrets.token_hex(32),
        }
        _write(path, record)
    finally:
        os.close(fd)
    return {"status": "intent", "nonce": record["nonce"]}


def complete(path: Path, owner_id: str, task_id: int, nonce: str) -> dict:
    """Mark intent complete without deleting its durable evidence."""
    fd = _locked(path)
    try:
        current = _read(path)
        if current is None:
            raise Refusal("admission_state_absent")
        record, _ = current
        if record["owner_id"] != owner_id or record["task_id"] != task_id or record["nonce"] != nonce:
            raise Refusal("admission_owner_mismatch")
        if record["state"] != "intent":
            raise Refusal("admission_state_invalid")
        _write(path, {**record, "state": "complete"})
    finally:
        os.close(fd)
    return {"status": "complete"}


def receiver_check(path: Path, operation: str) -> dict:
    """Refuse lifecycle entry while attribution and reset preservation are not proven."""
    if operation not in RECEIVER_OPERATIONS:
        raise Refusal("admission_operation_invalid")
    fd = _locked(path, create_directory=True)
    try:
        current = _read(path)
        if current is not None and current[0]["state"] == "intent":
            raise Refusal("admission_intent_unresolved")
        if operation == "receiver-clean":
            raise Refusal("receiver_reset_preservation_unverified")
        raise Refusal("task_attribution_unverified")
    finally:
        os.close(fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("verify-checkout", "receiver-check"))
    parser.add_argument("--checkout")
    parser.add_argument("--expected-sha")
    parser.add_argument("--operation", choices=tuple(sorted(RECEIVER_OPERATIONS)))
    args = parser.parse_args(argv)
    state_path = Path.home() / ".local/state/agent-cloud/o11y-journal/admission.json"
    try:
        if args.action == "verify-checkout" and args.checkout and args.expected_sha:
            result = verify_checkout(Path(args.checkout), args.expected_sha)
        elif args.action == "receiver-check" and args.operation:
            result = receiver_check(state_path, args.operation)
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
