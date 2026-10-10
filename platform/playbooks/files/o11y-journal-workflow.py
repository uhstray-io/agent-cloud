#!/usr/bin/env python3
"""Fail-closed host-local journal workflow reservation primitives."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import stat
import subprocess
import sys
from pathlib import Path

RESERVATION_PATH = Path("/run/lock/agent-cloud-o11y-journal/reservation.json")
MAX_RECORD = 4096


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


def _process_start(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except OSError as exc:
        raise Refusal("owner_identity_unavailable") from exc
    # The comm field may contain spaces and parentheses; fields after its final ')' are stable.
    fields = raw[raw.rfind(")") + 2 :].split()
    if len(fields) <= 19 or not fields[19].isdigit():
        raise Refusal("owner_identity_unavailable")
    return fields[19]


def _read(path: Path) -> tuple[dict, os.stat_result]:
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid() or stat.S_IMODE(before.st_mode) != 0o600:
            raise Refusal("reservation_identity_refused")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError as exc:
        raise Refusal("reservation_absent") from exc
    except OSError as exc:
        raise Refusal("reservation_read_refused") from exc
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise Refusal("reservation_identity_refused")
        raw = os.read(fd, MAX_RECORD + 1)
        if len(raw) > MAX_RECORD:
            raise Refusal("reservation_record_invalid")
    finally:
        os.close(fd)
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Refusal("reservation_record_invalid") from exc
    if not isinstance(value, dict):
        raise Refusal("reservation_record_invalid")
    return value, before


def _write_all(fd: int, raw: bytes) -> None:
    view = memoryview(raw)
    while view:
        count = os.write(fd, view)
        if count <= 0:
            raise Refusal("reservation_persist_refused")
        view = view[count:]


def _fsync_parent(path: Path) -> None:
    try:
        fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError as exc:
        raise Refusal("reservation_persist_refused") from exc
    try:
        try:
            os.fsync(fd)
        except OSError as exc:
            raise Refusal("reservation_persist_refused") from exc
    finally:
        os.close(fd)


def acquire(path: Path, action: str) -> dict:
    if action not in {"survey", "prove-direct", "apply", "repair-positions", "stop", "rollback"}:
        raise Refusal("action_invalid")
    parent = path.parent
    try:
        parent_meta = parent.lstat()
    except OSError as exc:
        raise Refusal("reservation_directory_unavailable") from exc
    if (
        not stat.S_ISDIR(parent_meta.st_mode)
        or parent_meta.st_uid != os.geteuid()
        or stat.S_IMODE(parent_meta.st_mode) != 0o700
    ):
        raise Refusal("reservation_directory_refused")

    try:
        old, _ = _read(path)
    except Refusal as exc:
        if str(exc) == "reservation_record_invalid":
            raise Refusal("reservation_stale_or_unknown") from exc
        if str(exc) != "reservation_absent":
            raise
    else:
        current = None
        try:
            current = _process_start(int(old.get("pid", -1)))
        except (Refusal, TypeError, ValueError):
            pass
        reason = "reservation_stale_or_unknown" if current != old.get("process_start") else "reservation_busy"
        raise Refusal(reason)

    record = {
        "schema": 1,
        "state": "active",
        "action": action,
        "nonce": secrets.token_hex(32),
        "uid": os.geteuid(),
        "pid": os.getpid(),
        "process_start": _process_start(os.getpid()),
        "task_attribution": "unverified",
    }
    raw = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("ascii")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError as exc:
        try:
            old, _ = _read(path)
        except Refusal as read_error:
            if str(read_error) == "reservation_record_invalid":
                raise Refusal("reservation_stale_or_unknown") from read_error
            raise
        current = None
        try:
            current = _process_start(int(old.get("pid", -1)))
        except (Refusal, TypeError, ValueError):
            pass
        reason = "reservation_stale_or_unknown" if current != old.get("process_start") else "reservation_busy"
        raise Refusal(reason) from exc
    except OSError as exc:
        raise Refusal("reservation_create_refused") from exc
    try:
        os.fchmod(fd, 0o600)
        _write_all(fd, raw)
        os.fsync(fd)
        created = os.fstat(fd)
    except OSError as exc:
        raise Refusal("reservation_persist_refused") from exc
    finally:
        os.close(fd)
    verified, readback = _read(path)
    if (created.st_dev, created.st_ino) != (readback.st_dev, readback.st_ino) or verified != record:
        raise Refusal("reservation_readback_refused")
    _fsync_parent(path)
    return {"status": "reserved", "nonce": record["nonce"], "task_attribution": "unverified"}


def release(path: Path, nonce: str) -> dict:
    record, identity = _read(path)
    if record.get("nonce") != nonce or record.get("uid") != os.geteuid():
        raise Refusal("reservation_owner_mismatch")
    if record.get("state") not in {"active", "complete"}:
        raise Refusal("reservation_record_invalid")
    if record.get("state") == "active":
        active_record = record
        record = {**record, "state": "complete", "completion": "reservation_released"}
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            _write_all(fd, json.dumps(record, sort_keys=True, separators=(",", ":")).encode("ascii"))
            os.fsync(fd)
            temp_identity = os.fstat(fd)
        finally:
            os.close(fd)
        current, current_identity = _read(path)
        if current != active_record or (identity.st_dev, identity.st_ino) != (
            current_identity.st_dev, current_identity.st_ino
        ):
            raise Refusal("reservation_owner_mismatch")
        try:
            os.replace(temporary, path)
        except OSError as exc:
            raise Refusal("reservation_persist_refused") from exc
        _fsync_parent(path)
        completed, completed_identity = _read(path)
        if completed != record or (temp_identity.st_dev, temp_identity.st_ino) != (
            completed_identity.st_dev, completed_identity.st_ino
        ):
            raise Refusal("reservation_readback_refused")
    else:
        completed = record
        completed_identity = identity
    latest, latest_identity = _read(path)
    if latest != completed:
        raise Refusal("reservation_readback_refused")
    if (completed_identity.st_dev, completed_identity.st_ino) != (latest_identity.st_dev, latest_identity.st_ino):
        raise Refusal("reservation_identity_refused")
    named = path.lstat()
    if (named.st_dev, named.st_ino) != (latest_identity.st_dev, latest_identity.st_ino):
        raise Refusal("reservation_identity_refused")
    try:
        path.unlink()
    except OSError as exc:
        raise Refusal("reservation_release_refused") from exc
    _fsync_parent(path)
    return {"status": "released", "completion": completed["completion"]}


def recover(_path: Path) -> dict:
    raise Refusal("recovery_disabled_pending_authenticated_api_readback")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("verify-checkout", "acquire", "release", "recover"))
    parser.add_argument("--checkout")
    parser.add_argument("--expected-sha")
    parser.add_argument(
        "--operation", choices=("survey", "prove-direct", "apply", "repair-positions", "stop", "rollback")
    )
    parser.add_argument("--nonce")
    args = parser.parse_args(argv)
    try:
        if args.action == "verify-checkout" and args.checkout and args.expected_sha:
            result = verify_checkout(Path(args.checkout), args.expected_sha)
        elif args.action == "acquire" and args.operation:
            result = acquire(RESERVATION_PATH, args.operation)
        elif args.action == "release" and args.nonce:
            result = release(RESERVATION_PATH, args.nonce)
        elif args.action == "recover":
            result = recover(RESERVATION_PATH)
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
