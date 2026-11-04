#!/usr/bin/env python3
"""Run the canonical Semaphore inventory sync with an operator token from stdin."""

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLAYBOOK = ROOT / "platform" / "semaphore" / "sync-inventory.yml"
DIFF_SUMMARY = "Validated inventory differs; the existing record will be updated."
MATCH_SUMMARY = "Validated inventory already matches; no write is needed."
CHILD_TIMEOUT_SECONDS = 300
CHILD_CLEANUP_TIMEOUT_SECONDS = 5
UNCERTAIN = "The write outcome is uncertain; reconcile the existing record with read-only access before any retry."


def ansible_child_environment() -> dict[str, str]:
    """Keep the sync child from leaking credentials through Ansible diagnostics."""
    child_env = os.environ.copy()
    child_env.pop("SEMAPHORE_TOKEN", None)
    child_env.pop("ANSIBLE_LOG_PATH", None)
    child_env.pop("ANSIBLE_CALLBACKS_ENABLED", None)
    child_env["ANSIBLE_CONFIG"] = str(ROOT / "ansible.cfg")
    child_env["ANSIBLE_BECOME"] = "False"
    child_env["ANSIBLE_DEBUG"] = "False"
    child_env["ANSIBLE_STDOUT_CALLBACK"] = "redact_requests"
    child_env["ANSIBLE_CALLBACK_PLUGINS"] = str(ROOT / "callback_plugins")
    child_env["ANSIBLE_PIPELINING"] = "True"
    child_env["ANSIBLE_KEEP_REMOTE_FILES"] = "False"
    return child_env


def stop_child_group(child: subprocess.Popen[bytes]) -> None:
    """Stop the owned local child session and reap its direct child within a bound."""
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        try:
            child.kill()
        except OSError:
            pass

    # Descendants may inherit these pipes. Closing our ends avoids an unbounded drain.
    for stream in (child.stdout, child.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass
    try:
        child.wait(timeout=CHILD_CLEANUP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            child.kill()
        except OSError:
            pass
        try:
            child.wait(timeout=CHILD_CLEANUP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            pass


def write_payload(fifo_path: str, payload: bytes) -> None:
    """Hand the sync payload to the child through its FIFO; a failed write is never printed."""
    try:
        # O_WRONLY without O_CREAT: if the FIFO was already unlinked, nothing is created to hold the token.
        fd = os.open(fifo_path, os.O_WRONLY)
        with os.fdopen(fd, "wb") as pipe:
            pipe.write(payload)
    except OSError:
        pass


def release_payload_writer(fifo_path: str, writer: threading.Thread) -> None:
    """Unblock a writer whose FIFO no child opened, then reap it within a bound."""
    if writer.is_alive():
        try:
            fd = os.open(fifo_path, os.O_RDONLY | os.O_NONBLOCK)
            os.close(fd)
        except OSError:
            pass
    writer.join(timeout=CHILD_CLEANUP_TIMEOUT_SECONDS)


def _raise_interrupt(_signum: int, _frame: object) -> None:
    """Turn a termination signal into the KeyboardInterrupt arm that stops the child."""
    raise KeyboardInterrupt


def install_interrupt_handlers() -> dict[int, object]:
    """Route SIGTERM and SIGHUP to the Ctrl-C path and return the handlers they replace.

    The child has its own session, so neither signal reaches it from the terminal or a supervisor.
    signal.signal works only on the main thread; an in-process caller on another thread keeps its handlers.
    """
    previous: dict[int, object] = {}
    if threading.current_thread() is not threading.main_thread():
        return previous
    for name in ("SIGTERM", "SIGHUP"):
        signum = getattr(signal, name, None)
        if signum is not None:
            previous[signum] = signal.signal(signum, _raise_interrupt)
    return previous


def fail(apply: bool, check_text: str, apply_lead: str, code: int = 1) -> int:
    """Print the one fixed failure message for this mode and return its exit code."""
    print(check_text if not apply else f"{apply_lead} {UNCERTAIN}", file=sys.stderr)
    return code


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

    if not args.url:
        print("Semaphore URL is required; no synchronization was started.", file=sys.stderr)
        return 2
    if sys.stdin.isatty():
        print("Operator token must be provided on stdin; no synchronization was started.", file=sys.stderr)
        return 2

    token = sys.stdin.read()
    if token.endswith("\n"):
        token = token[:-1]
        if token.endswith("\r"):
            token = token[:-1]
    if (
        not token
        or "\n" in token
        or "\r" in token
        or any(ord(char) < 32 or ord(char) == 127 for char in token)
        or any(marker in token for marker in ("{{", "{%", "{#"))
    ):
        print("Invalid operator token input; no synchronization was started.", file=sys.stderr)
        return 2

    payload = {
        "sync_inventory_token": token,
        "semaphore_url": args.url,
        "semaphore_project_id": args.project,
        "inventory_name": args.name,
        "inventory_source": str(Path(args.inventory).expanduser().resolve()),
    }
    fifo_dir = None
    fifo_path = None
    writer = None
    previous_handlers: dict[int, object] = {}
    try:
        try:
            fifo_dir = tempfile.mkdtemp(prefix="semaphore-sync-inventory-")
            # ansible-core resolves symlinks in `--extra-vars @path`, and Linux's /dev/stdin is a
            # symlink to an unopenable pipe. A FIFO in this 0700 directory keeps the token off argv,
            # the environment and disk.
            fifo_path = os.path.join(fifo_dir, "extra-vars.json")
            os.mkfifo(fifo_path, 0o600)
        except OSError:
            print("Unable to start the inventory synchronization play; it did not start.", file=sys.stderr)
            return 1

        argv = ["ansible-playbook", "-i", "localhost,", str(PLAYBOOK), "--extra-vars", "@" + fifo_path]
        if not args.apply:
            argv.append("--check")
        child_env = ansible_child_environment()
        previous_handlers = install_interrupt_handlers()
        try:
            child = subprocess.Popen(
                argv,
                cwd=ROOT,
                env=child_env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            print("Unable to start the inventory synchronization play; it did not start.", file=sys.stderr)
            return 1

        try:
            writer = threading.Thread(
                target=write_payload, args=(fifo_path, json.dumps(payload).encode()), daemon=True
            )
            writer.start()
            stdout, _ = child.communicate(timeout=CHILD_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            stop_child_group(child)
            return fail(
                args.apply,
                "Inventory check exceeded its fixed deadline; no write was requested.",
                "Inventory synchronization exceeded its fixed deadline.",
            )
        except OSError:
            stop_child_group(child)
            return fail(
                args.apply,
                "Inventory check failed; no write was requested.",
                "Inventory synchronization did not complete.",
            )
        except KeyboardInterrupt:
            # The child has its own session, so SIGINT, SIGTERM and SIGHUP never reach it directly. Stop it here.
            stop_child_group(child)
            return fail(
                args.apply,
                "Inventory check was interrupted; no write was requested.",
                "Inventory synchronization was interrupted.",
                130,
            )
        except BaseException:
            stop_child_group(child)
            raise

        if child.returncode != 0:
            return fail(
                args.apply,
                "Inventory check failed; no write was requested.",
                "Inventory synchronization did not complete.",
            )

        if DIFF_SUMMARY.encode() in stdout:
            outcome = (
                "Inventory check completed; the existing record differs."
                if not args.apply
                else "Inventory synchronization completed; updated content was verified."
            )
        elif MATCH_SUMMARY.encode() in stdout:
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
    finally:
        # Release the writer before unlinking: a writer still waiting to open must be unblocked by path.
        if writer is not None:
            release_payload_writer(fifo_path, writer)
        if fifo_path is not None:
            try:
                os.unlink(fifo_path)
            except FileNotFoundError:
                pass
        if fifo_dir is not None:
            try:
                os.rmdir(fifo_dir)
            except OSError:
                pass
        for signum, handler in previous_handlers.items():
            if handler is not None:
                signal.signal(signum, handler)


if __name__ == "__main__":
    sys.exit(main())
