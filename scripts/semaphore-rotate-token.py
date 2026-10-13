#!/usr/bin/env python3
"""Rotate the calling Semaphore user's API token without exposing token material."""

import argparse
import fcntl
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

import semaphore_seed as seed

EXPECTED_VERSION = "2.19.11"
ROTATION_TEMPLATE = "Rotate Semaphore Operator Token"
OLD_INPUT = "SEMAPHORE_OLD_TOKEN"
NEW_INPUT = "SEMAPHORE_NEW_TOKEN"
WILDCARDS = re.compile(r"[%_]")


class RotationRefusal(seed.Refusal):
    """An operator-safe failure that never carries credential or ID values."""


def safe_token(value):
    return isinstance(value, str) and len(value) > 8 and "\n" not in value and "\r" not in value


def info(api):
    value = api("/info", project_scoped=False)
    version = value.get("version") if isinstance(value, dict) else None
    if not isinstance(version, str) or version.split("-", 1)[0].removeprefix("v") != EXPECTED_VERSION:
        raise RotationRefusal("Semaphore backend version is unverified; refusing token changes")
    return version.split("-", 1)[0].removeprefix("v")


def token_ids(api):
    rows = api("/user/tokens", project_scoped=False)
    if not isinstance(rows, list):
        raise RotationRefusal("Semaphore token inventory is unreadable")
    ids = []
    for row in rows:
        value = row.get("id") if isinstance(row, dict) else None
        if not isinstance(value, str) or len(value) != 8:
            raise RotationRefusal("Semaphore token inventory has an unverified identifier")
        if WILDCARDS.search(value):
            raise RotationRefusal("Semaphore token inventory contains a SQL wildcard; refusing deletion")
        ids.append(value)
    if len(ids) != len(set(ids)):
        raise RotationRefusal("Semaphore token inventory is ambiguous")
    return set(ids)


def exact_id(ids, token):
    prefix = token[:8]
    if WILDCARDS.search(prefix):
        raise RotationRefusal("Token identifier contains a SQL wildcard; refusing deletion")
    matches = {item for item in ids if item.startswith(prefix)}
    if matches != {prefix}:
        raise RotationRefusal("Token identifier is absent or ambiguous; refusing deletion")
    return prefix


def atomic_private_write(path, content):
    path = Path(path)
    if not path.parent.is_dir():
        raise RotationRefusal("Output directory does not exist")
    if path.is_symlink():
        raise RotationRefusal("Refusing a symlink output path")
    if path.exists() and not stat.S_ISREG(path.stat().st_mode):
        raise RotationRefusal("Output path is not a regular file")
    fd, name = tempfile.mkstemp(prefix=".semaphore-rotation-", dir=path.parent)
    temp = Path(name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        os.chmod(path, 0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode != 0o600 or path.read_bytes() != content:
            raise RotationRefusal("Private output verification failed")
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def save_state(path, state):
    atomic_private_write(path, (json.dumps(state, sort_keys=True) + "\n").encode())


def load_state(path):
    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        raise RotationRefusal("Rotation state must be a regular file with mode 0600")
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError):
        raise RotationRefusal("Rotation state is unreadable") from None
    if not isinstance(state, dict) or state.get("schema") != 1:
        raise RotationRefusal("Rotation state schema is unrecognized")
    return state


def replace_backup(path, token):
    atomic_private_write(path, (token + "\n").encode())


def api_for(args, token):
    try:
        return seed.API(args.url, args.project, token)
    except seed.Refusal:
        raise RotationRefusal("Semaphore URL or operator token was refused") from None


def authenticate(api, expected_user_id):
    info(api)
    user = api("/user/", project_scoped=False)
    if not isinstance(user, dict) or user.get("id") != expected_user_id:
        raise RotationRefusal("Semaphore operator identity differs; refusing token changes")
    return token_ids(api)


def verify_old_invalid(args, old_token):
    old_api = api_for(args, old_token)
    try:
        token_ids(old_api)
    except seed.Refusal as error:
        if "HTTP 401" in str(error):
            return True
        raise RotationRefusal("Old token validity is unknown; preserve rotation state") from None
    raise RotationRefusal("Old token still authenticates; rotation is incomplete")


def validate_state(state, old_token):
    required = {"schema", "phase", "old_id", "old_token", "before_ids", "new_id", "new_token"}
    if not required.issubset(state):
        raise RotationRefusal("Rotation state is incomplete")
    if (not safe_token(old_token) or state["old_token"] != old_token
            or state["old_id"] != old_token[:8]):
        raise RotationRefusal("Supplied operator token does not match rotation state")
    if state["phase"] == "create_started":
        raise RotationRefusal("Create outcome is unknown; reconcile the token inventory before retrying")
    if not safe_token(state.get("new_token")) or state.get("new_id") != state["new_token"][:8]:
        raise RotationRefusal("Rotation state has no recoverable replacement token")
    if not isinstance(state.get("before_ids"), list) or len(set(state["before_ids"])) != len(state["before_ids"]):
        raise RotationRefusal("Rotation state token inventory is invalid")
    if (not isinstance(state["old_id"], str) or len(state["old_id"]) != 8
            or WILDCARDS.search(state["old_id"]) or state["old_id"] not in state["before_ids"]
            or any(not isinstance(value, str) or len(value) != 8 or WILDCARDS.search(value)
                   for value in state["before_ids"])):
        raise RotationRefusal("Rotation state token inventory is invalid")
    if state["new_id"] in state["before_ids"]:
        raise RotationRefusal("Replacement identifier collides with an existing token; refusing deletion")
    if state["phase"] not in {"created", "verified", "reconciled", "backup_replaced",
                               "revoke_started", "revoke_unknown"}:
        raise RotationRefusal("Rotation phase is not safe to resume")


def resume_input_token(args, supplied):
    state = load_state(args.state)
    if state is None:
        return supplied
    if supplied == state.get("old_token"):
        return supplied
    if supplied == state.get("new_token") and safe_token(state.get("old_token")):
        return state["old_token"]
    raise RotationRefusal("Stdin token does not match the private rotation state")


def verify_replacement(args, token, expected_ids):
    new_api = api_for(args, token)
    ids = authenticate(new_api, args.expected_user_id)
    if ids != expected_ids:
        raise RotationRefusal("Token inventory changed during rotation; refusing further changes")
    return new_api, ids


def delete_old(args, new_api, state, old_token):
    old_id = state["old_id"]
    before = set(state["before_ids"])
    new_id = state["new_id"]
    current = token_ids(new_api)
    if old_id in current:
        if current != before | {new_id}:
            raise RotationRefusal("Token inventory changed; refusing old-token revocation")
        exact_id(current, old_token)
        authenticate(api_for(args, old_token), args.expected_user_id)
        state["phase"] = "revoke_started"
        save_state(args.state, state)
        try:
            new_api("/user/tokens/" + seed.urllib.parse.quote(old_id, safe=""),
                    project_scoped=False, method="DELETE")
        except seed.Refusal:
            state["phase"] = "revoke_unknown"
            save_state(args.state, state)
            raise RotationRefusal("Revocation outcome is unknown; reconcile before retrying") from None
        state["phase"] = "revoke_unknown"
        save_state(args.state, state)
    elif current != (before - {old_id}) | {new_id}:
        raise RotationRefusal("Token inventory changed; refusing to infer revocation")

    expected_after = (before - {old_id}) | {new_id}
    if token_ids(new_api) != expected_after:
        raise RotationRefusal("Old-token revocation is not confirmed; preserve rotation state")
    verify_old_invalid(args, old_token)


def rotate_locked(args, old_token):
    if not safe_token(old_token):
        raise RotationRefusal("A single-line operator token is required on stdin")
    prior = load_state(args.state)
    if prior is None:
        old_api = api_for(args, old_token)
        before_ids = authenticate(old_api, args.expected_user_id)
        old_id = exact_id(before_ids, old_token)
        state = {"schema": 1, "phase": "create_started", "old_id": old_id, "old_token": old_token,
                 "before_ids": sorted(before_ids), "new_id": None, "new_token": None}
        # This durable marker intentionally treats a process interruption before the POST
        # as uncertain. The API offers no idempotency key or read-back of a full token.
        save_state(args.state, state)
        try:
            created = old_api("/user/tokens", project_scoped=False, method="POST")
        except seed.Refusal:
            raise RotationRefusal("Create outcome is unknown; preserve state and reconcile before retrying") from None
        replacement = created.get("id") if isinstance(created, dict) else None
        if (not safe_token(replacement) or replacement == old_token
                or created.get("user_id") != args.expected_user_id
                or created.get("expired") is not False):
            raise RotationRefusal("Create response did not contain a recoverable replacement token")
        state.update(phase="created", new_id=replacement[:8], new_token=replacement)
        save_state(args.state, state)
        if state["new_id"] in state["before_ids"]:
            raise RotationRefusal("Replacement identifier collides with an existing token; preserve state")
    else:
        validate_state(prior, old_token)
        state = prior
        replacement = state["new_token"]

    new_id = exact_id(set(state["before_ids"]) | {state["new_id"]}, replacement)
    expected_after_create = set(state["before_ids"]) | {new_id}
    if state["phase"] in {"revoke_started", "revoke_unknown"}:
        allowed = (expected_after_create,
                   (set(state["before_ids"]) - {state["old_id"]}) | {new_id})
        observed = token_ids(api_for(args, replacement))
        if observed not in allowed:
            raise RotationRefusal("Token inventory changed; refusing to infer revocation")
        new_api, _ = verify_replacement(args, replacement, observed)
    else:
        new_api, _ = verify_replacement(args, replacement, expected_after_create)
    if state["phase"] == "created":
        state["phase"] = "verified"
        save_state(args.state, state)

    if state["phase"] in {"verified", "reconciled"}:
        decl = seed.declaration(ROTATION_TEMPLATE)
        target = seed.seed_target(new_api, decl, "dev", args.inventory)
        seed.stage_and_seed(new_api, args.project, target,
                            {OLD_INPUT: old_token, NEW_INPUT: replacement},
                            message="Compare and conditionally rotate the Semaphore controller token")
        state["phase"] = "reconciled"
        save_state(args.state, state)

    if state["phase"] == "reconciled":
        replace_backup(args.backup, replacement)
        state["phase"] = "backup_replaced"
        save_state(args.state, state)

    if state["phase"] not in {"backup_replaced", "revoke_started", "revoke_unknown"}:
        raise RotationRefusal("Rotation phase is not safe to resume")
    delete_old(args, new_api, state, old_token)
    Path(args.state).unlink()
    print("Semaphore operator token rotation completed; replacement verified and old token rejected.")


def rotate(args, old_token):
    lock_path = args.backup.parent / ".semaphore-token-rotation.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RotationRefusal("Another local token rotation is already running") from None
        return rotate_locked(args, old_token)
    finally:
        os.close(fd)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="Semaphore HTTPS origin")
    parser.add_argument("--project", required=True, type=int, help="Semaphore project ID")
    parser.add_argument("--expected-user-id", required=True, type=int, help="Approved operator user ID")
    parser.add_argument("--inventory", required=True, type=int, help="Approved Dev inventory ID")
    parser.add_argument("--backup", required=True, type=Path, help="Local operator token file to replace")
    parser.add_argument("--state", required=True, type=Path, help="Private resumable state file, mode 0600")
    args = parser.parse_args(argv)
    reserved_lock = args.backup.parent / ".semaphore-token-rotation.lock"
    if args.backup.resolve() == args.state.resolve() or args.state.resolve() == reserved_lock.resolve():
        parser.error("backup, state, and lock paths must be different files")
    return args


def main(argv=None):
    args = parse_args(argv)
    if sys.stdin.isatty():
        print("Refused: supply the current operator token on stdin.", file=sys.stderr)
        return 2
    raw = sys.stdin.read(8193)
    if len(raw) > 8192 or raw.count("\n") > 1 or raw.count("\r") > 1:
        print("Refused: stdin must contain exactly one token line.", file=sys.stderr)
        return 2
    try:
        old_token = resume_input_token(args, raw.rstrip("\r\n"))
        rotate(args, old_token)
    except RotationRefusal as error:
        print(f"Refused: {error}", file=sys.stderr)
        return 2
    except Exception:
        # Never render an unexpected traceback: HTTP libraries and serializers can
        # attach request or response data to exception text.
        print("Rotation stopped unexpectedly; preserve private state and reconcile before retrying.",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
