#!/usr/bin/env python3
"""Bounded, non-writing consistency check for a pinned Git checkout."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import selectors
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

GIT = "/usr/bin/git"
ASKPASS = "/usr/bin/false"
COMMAND_TIMEOUT = 10.0
TOTAL_TIMEOUT = 30.0
MAX_METADATA = 1024 * 1024
MAX_ENTRIES = 8192
MAX_BLOB = 4 * 1024 * 1024
MAX_TOTAL_BLOBS = 64 * 1024 * 1024
MAX_RESULT = 12 * 1024 * 1024
MAX_INDEX_FILE = 8 * 1024 * 1024


class Refusal(Exception):
    """A fixed-category refusal with no untrusted detail."""


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise Refusal("arguments_invalid")


def _deadline_signal(_signum: int, _frame: object) -> None:
    raise Refusal("checkout_deadline")


def _environment() -> dict[str, str]:
    return {
        "HOME": os.devnull,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": ASKPASS,
        "SSH_ASKPASS": ASKPASS,
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_ATTR_NOSYSTEM": "1",
    }


def _git_argv(root: Path, *args: str) -> list[str]:
    if not os.path.isfile(GIT) or not os.access(GIT, os.X_OK):
        raise Refusal("git_unavailable")
    return [GIT, "--no-optional-locks", "--no-replace-objects", "-C", str(root), "-c", "core.fsmonitor=false", *args]


def _terminate(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _run_git(
    root: Path,
    args: tuple[str, ...],
    *,
    stdin: bytes | None = None,
    output_limit: int = MAX_METADATA,
    deadline: float,
) -> bytes:
    remaining = min(COMMAND_TIMEOUT, deadline - time.monotonic())
    if remaining <= 0:
        raise Refusal("checkout_deadline")
    process = subprocess.Popen(
        _git_argv(root, *args),
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=_environment(),
        start_new_session=True,
        close_fds=True,
    )
    selector = selectors.DefaultSelector()
    output = bytearray()
    input_offset = 0
    completed = False
    try:
        assert process.stdout is not None
        os.set_blocking(process.stdout.fileno(), False)
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        if stdin is not None:
            assert process.stdin is not None
            os.set_blocking(process.stdin.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
        command_deadline = min(deadline, time.monotonic() + remaining)
        while selector.get_map() or process.poll() is None:
            wait_for = command_deadline - time.monotonic()
            if wait_for <= 0:
                raise Refusal("checkout_timeout")
            for key, _ in selector.select(min(wait_for, 0.1)):
                if key.data == "stdin":
                    pipe = key.fileobj
                    assert stdin is not None
                    if input_offset == len(stdin):
                        selector.unregister(pipe)
                        pipe.close()
                        continue
                    try:
                        input_offset += os.write(pipe.fileno(), stdin[input_offset : input_offset + 65536])
                    except BrokenPipeError:
                        selector.unregister(pipe)
                        pipe.close()
                else:
                    pipe = key.fileobj
                    chunk = os.read(pipe.fileno(), 65536)
                    if not chunk:
                        selector.unregister(pipe)
                        pipe.close()
                        continue
                    output.extend(chunk)
                    if len(output) > output_limit:
                        raise Refusal("checkout_output_limit")
        if process.wait(timeout=0.1) != 0:
            raise Refusal("checkout_git_failed")
        completed = True
        return bytes(output)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Refusal("checkout_read_failed") from exc
    finally:
        selector.close()
        if not completed:
            _terminate(process)
        for pipe in (process.stdin, process.stdout):
            if pipe is not None and not pipe.closed:
                pipe.close()


def _nul_records(output: bytes) -> list[bytes]:
    if not output.endswith(b"\0"):
        raise Refusal("checkout_metadata_invalid")
    records = output[:-1].split(b"\0") if output else []
    if len(records) > MAX_ENTRIES:
        raise Refusal("checkout_entry_limit")
    return records


def _refuse_split_or_unsupported_index(root: Path, deadline: float) -> None:
    raw_path = _run_git(root, ("rev-parse", "--git-path", "index"), output_limit=4096, deadline=deadline)
    path = Path(os.fsdecode(raw_path.rstrip(b"\n")))
    if not path.is_absolute():
        path = root / path
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_INDEX_FILE or before.st_size < 32:
            raise Refusal("checkout_index_unsupported")
        data = bytearray()
        while len(data) <= MAX_INDEX_FILE:
            chunk = os.read(descriptor, min(65536, MAX_INDEX_FILE + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        after = os.fstat(descriptor)
        if not _same_stat(before, after) or len(data) != before.st_size:
            raise Refusal("checkout_index_race")
    finally:
        os.close(descriptor)
    if bytes(data[:4]) != b"DIRC" or len(data) < 32:
        raise Refusal("checkout_index_invalid")
    if hashlib.sha1(data[:-20], usedforsecurity=False).digest() != data[-20:]:
        raise Refusal("checkout_index_invalid")
    version = int.from_bytes(data[4:8], "big")
    count = int.from_bytes(data[8:12], "big")
    if version not in (2, 3) or count > MAX_ENTRIES:
        raise Refusal("checkout_index_unsupported")
    offset = 12
    for _ in range(count):
        start = offset
        fixed = 40 + 20 + 2
        if offset + fixed > len(data) - 20:
            raise Refusal("checkout_index_invalid")
        flags = int.from_bytes(data[offset + 60 : offset + 62], "big")
        offset += fixed
        if version >= 3 and flags & 0x4000:
            offset += 2
        terminator = data.find(0, offset, len(data) - 20)
        if terminator < 0:
            raise Refusal("checkout_index_invalid")
        offset = ((terminator + 1 - start + 7) // 8) * 8 + start
    trailer = len(data) - 20
    while offset < trailer:
        if offset + 8 > trailer:
            raise Refusal("checkout_index_invalid")
        signature = bytes(data[offset : offset + 4])
        size = int.from_bytes(data[offset + 4 : offset + 8], "big")
        offset += 8
        if size > trailer - offset:
            raise Refusal("checkout_index_invalid")
        if signature == b"link":
            raise Refusal("checkout_split_index_unsupported")
        if signature[:1].islower():
            raise Refusal("checkout_index_extension_unsupported")
        offset += size
    if offset != trailer:
        raise Refusal("checkout_index_invalid")


def _tree(root: Path, revision: str, deadline: float) -> dict[bytes, tuple[bytes, bytes]]:
    output = _run_git(
        root,
        ("ls-tree", "-r", "-z", "--full-tree", revision),
        output_limit=MAX_METADATA,
        deadline=deadline,
    )
    entries: dict[bytes, tuple[bytes, bytes]] = {}
    for record in _nul_records(output):
        try:
            metadata, path = record.split(b"\t", 1)
            mode, kind, oid = metadata.split(b" ")
        except ValueError as exc:
            raise Refusal("checkout_metadata_invalid") from exc
        if (
            not path
            or path.startswith(b"/")
            or any(part in (b"", b".", b"..") for part in path.split(b"/"))
            or path in entries
            or kind != b"blob"
            or mode not in (b"100644", b"100755", b"120000")
            or re.fullmatch(rb"[0-9a-f]{40,64}", oid) is None
        ):
            raise Refusal("checkout_tree_unsupported")
        entries[path] = (mode, oid)
    return entries


def _index(root: Path, tree: dict[bytes, tuple[bytes, bytes]], deadline: float) -> None:
    records = _nul_records(_run_git(root, ("ls-files", "-v", "--stage", "-z"), deadline=deadline))
    entries: dict[bytes, tuple[bytes, bytes]] = {}
    for record in records:
        if len(record) < 3 or record[1:2] != b" ":
            raise Refusal("checkout_index_invalid")
        flag = record[:1]
        if flag.islower() or flag == b"S":
            raise Refusal("checkout_index_flags")
        try:
            metadata, path = record[2:].split(b"\t", 1)
            mode, oid, stage = metadata.split(b" ")
        except ValueError as exc:
            raise Refusal("checkout_index_invalid") from exc
        if stage != b"0" or path in entries:
            raise Refusal("checkout_index_mismatch")
        entries[path] = (mode, oid)
    if entries != tree:
        raise Refusal("checkout_index_mismatch")


def _blob_bytes(root: Path, tree: dict[bytes, tuple[bytes, bytes]], deadline: float) -> dict[bytes, bytes]:
    total = 0
    for mode, _ in tree.values():
        if mode not in (b"100644", b"100755", b"120000"):
            raise Refusal("checkout_tree_unsupported")
    request = b"".join(oid + b"\n" for _, oid in tree.values())
    response = _run_git(
        root,
        ("cat-file", "--batch"),
        stdin=request,
        output_limit=MAX_TOTAL_BLOBS + MAX_METADATA,
        deadline=deadline,
    )
    blobs: dict[bytes, bytes] = {}
    offset = 0
    for path, (_, oid) in tree.items():
        end = response.find(b"\n", offset)
        if end < 0:
            raise Refusal("checkout_blob_invalid")
        try:
            got_oid, kind, raw_size = response[offset:end].split(b" ")
            size = int(raw_size)
        except (ValueError, TypeError) as exc:
            raise Refusal("checkout_blob_invalid") from exc
        if got_oid != oid or kind != b"blob" or size < 0 or size > MAX_BLOB:
            raise Refusal("checkout_blob_limit")
        total += size
        if total > MAX_TOTAL_BLOBS:
            raise Refusal("checkout_total_blob_limit")
        start = end + 1
        stop = start + size
        if stop >= len(response) or response[stop : stop + 1] != b"\n":
            raise Refusal("checkout_blob_invalid")
        blobs[path] = response[start:stop]
        offset = stop + 1
    if offset != len(response):
        raise Refusal("checkout_blob_invalid")
    return blobs


def _same_stat(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) == (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )


def _read_worktree(root_fd: int, path: bytes, expected_mode: bytes, expected: bytes, deadline: float) -> None:
    parts = path.split(b"/")
    if not parts or any(part in (b"", b".", b"..") for part in parts):
        raise Refusal("checkout_path_unsafe")
    parent_fd = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            next_fd = os.open(
                part,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
            os.close(parent_fd)
            parent_fd = next_fd
        name = parts[-1]
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if expected_mode == b"120000":
            if not stat.S_ISLNK(before.st_mode):
                raise Refusal("checkout_worktree_type")
            actual = os.fsencode(os.readlink(name, dir_fd=parent_fd))
            after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        else:
            if not stat.S_ISREG(before.st_mode):
                raise Refusal("checkout_worktree_type")
            executable = bool(before.st_mode & 0o111)
            if executable != (expected_mode == b"100755"):
                raise Refusal("checkout_worktree_mode")
            file_fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
            try:
                opened = os.fstat(file_fd)
                if not _same_stat(before, opened):
                    raise Refusal("checkout_worktree_race")
                chunks = bytearray()
                while True:
                    if time.monotonic() >= deadline:
                        raise Refusal("checkout_deadline")
                    chunk = os.read(file_fd, min(65536, MAX_BLOB + 1 - len(chunks)))
                    if not chunk:
                        break
                    chunks.extend(chunk)
                    if len(chunks) > MAX_BLOB:
                        raise Refusal("checkout_blob_limit")
                after = os.fstat(file_fd)
                if not _same_stat(opened, after):
                    raise Refusal("checkout_worktree_race")
                actual = bytes(chunks)
            finally:
                os.close(file_fd)
        if not _same_stat(before, after):
            raise Refusal("checkout_worktree_race")
        if actual != expected:
            raise Refusal("checkout_worktree_bytes")
    finally:
        os.close(parent_fd)


def verify_checkout(checkout: Path, expected_sha: str, payload_paths: list[str], require_dev_ref: bool) -> dict:
    if re.fullmatch(r"[0-9a-f]{40}", expected_sha) is None:
        raise Refusal("expected_revision_invalid")
    if len(payload_paths) > 8 or any(
        not isinstance(name, str)
        or not name
        or name.startswith("/")
        or any(part in ("", ".", "..") for part in name.split("/"))
        for name in payload_paths
    ):
        raise Refusal("payload_paths_invalid")
    root = checkout.resolve(strict=True)
    if not root.is_dir():
        raise Refusal("checkout_unavailable")
    deadline = time.monotonic() + TOTAL_TIMEOUT
    root_result = _run_git(root, ("rev-parse", "--show-toplevel"), deadline=deadline)
    if root_result != os.fsencode(str(root) + "\n"):
        raise Refusal("checkout_root_mismatch")
    revision = _run_git(root, ("rev-parse", "--verify", "HEAD^{commit}"), deadline=deadline)
    if revision != (expected_sha + "\n").encode("ascii"):
        raise Refusal("checkout_revision_mismatch")
    _refuse_split_or_unsupported_index(root, deadline)
    tree = _tree(root, expected_sha, deadline)
    _index(root, tree, deadline)
    blobs = _blob_bytes(root, tree, deadline)
    root_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        for path, (mode, _) in tree.items():
            _read_worktree(root_fd, path, mode, blobs[path], deadline)
    finally:
        os.close(root_fd)

    # Enumerate untracked paths without invoking status's clean-filter conversions.
    untracked = _run_git(
        root,
        ("ls-files", "--others", "--exclude-standard", "-z"),
        deadline=deadline,
    )
    if untracked:
        raise Refusal("checkout_dirty_or_unknown")

    dev_ref = None
    if require_dev_ref:
        try:
            dev_ref = _run_git(
                root,
                ("rev-parse", "--verify", "refs/remotes/origin/dev^{commit}"),
                deadline=deadline,
            )
        except Refusal as exc:
            if str(exc) == "checkout_git_failed":
                raise Refusal("checkout_dev_ref_unavailable") from exc
            raise
        if dev_ref != (expected_sha + "\n").encode("ascii"):
            raise Refusal("checkout_dev_ref_mismatch")

    payloads: dict[str, str] = {}
    payload_total = 0
    for name in payload_paths:
        path = os.fsencode(name)
        if path not in blobs:
            raise Refusal("payload_missing")
        payload_total += len(blobs[path])
        if len(blobs[path]) > MAX_BLOB or payload_total > 8 * 1024 * 1024:
            raise Refusal("payload_limit")
        try:
            blobs[path].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise Refusal("payload_encoding_invalid") from exc
        payloads[name] = base64.b64encode(blobs[path]).decode("ascii")
    result = {
        "status": "checkout_consistent",
        "revision": expected_sha,
        "index_matches_revision": True,
        "tracked_bytes_match_revision": True,
        "payloads": payloads,
    }
    if require_dev_ref:
        result["local_dev_ref_matches_revision"] = True
    encoded = json.dumps(result, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_RESULT:
        raise Refusal("result_limit")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = _ArgumentParser(add_help=False)
    parser.add_argument("--checkout", required=True)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--payload-paths", default="[]")
    parser.add_argument("--require-dev-ref", action="store_true")
    try:
        args = parser.parse_args(argv)
        payload_paths = json.loads(args.payload_paths)
        if not isinstance(payload_paths, list) or any(not isinstance(value, str) for value in payload_paths):
            raise Refusal("payload_paths_invalid")
        previous_handler = signal.signal(signal.SIGALRM, _deadline_signal)
        signal.setitimer(signal.ITIMER_REAL, TOTAL_TIMEOUT)
        result = verify_checkout(Path(args.checkout), args.expected_sha, payload_paths, args.require_dev_ref)
    except Refusal as exc:
        print(json.dumps({"status": "refused", "reason": str(exc)}, separators=(",", ":")))
        return 1
    except BaseException:
        print('{"status":"refused","reason":"checkout_operation_failed"}')
        return 1
    finally:
        if "previous_handler" in locals():
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
