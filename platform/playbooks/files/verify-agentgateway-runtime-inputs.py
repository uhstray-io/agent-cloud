#!/usr/bin/env python3
"""Run the fixed agentgateway read-only comparison and emit safe metadata only."""

import json
import os
import selectors
import signal
import subprocess
import sys
import time

MAX_OUTPUT = 65536
TIMEOUT_SECONDS = 45
REASONS = {
    "matched",
    "gateway-state-unavailable",
    "gateway-not-running",
    "inputs-not-matched",
    "project-enumeration-unavailable",
    "image-not-matched",
    "readiness-unproven",
}


def result(rc, status, reason, pass_marker, error):
    return {
        "rc": rc,
        "result": status,
        "reason": reason,
        "pass_marker": pass_marker,
        "error": error,
    }


def emit(value):
    sys.stdout.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")


def stop_child(child):
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        pass
    try:
        child.wait(timeout=2)
    except subprocess.TimeoutExpired:
        return False
    except OSError:
        return False
    return True


def run():
    try:
        selector = selectors.DefaultSelector()
    except Exception:
        return result(None, "unavailable", "diagnostic-unavailable", False, "setup-failed")
    try:
        child = subprocess.Popen(
            ["bash", "deploy.sh", "--verify-only"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=os.getcwd(),
            env=os.environ.copy(),
            start_new_session=True,
        )
    except Exception:
        try:
            selector.close()
        except Exception:
            pass
        return result(None, "unavailable", "diagnostic-unavailable", False, "invocation-failed")

    captured = {child.stdout: bytearray(), child.stderr: bytearray()}
    deadline = time.monotonic() + TIMEOUT_SECONDS
    total = 0
    error = "none"
    try:
        for stream in captured:
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                error = "timeout"
                break
            for key, _ in selector.select(min(remaining, 0.25)):
                chunk = os.read(key.fd, 8192)
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                total += len(chunk)
                if total > MAX_OUTPUT:
                    error = "output-limit"
                    break
                captured[key.fileobj].extend(chunk)
            if error != "none":
                break
        if error == "none":
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                error = "timeout"
            else:
                try:
                    child.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    error = "timeout"
    except Exception:
        error = "capture-failed"
    finally:
        try:
            selector.close()
        except Exception:
            if error == "none":
                error = "capture-failed"
        if error != "none" and not stop_child(child):
            error = "cleanup-failed"
        for stream in (child.stdout, child.stderr):
            if not stream.closed:
                try:
                    stream.close()
                except Exception:
                    if error == "none":
                        error = "capture-failed"

    if error != "none":
        return result(None, "unavailable", "diagnostic-unavailable", False, error)

    rc = child.returncode
    try:
        stdout = captured[child.stdout].decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return result(rc, "unavailable", "diagnostic-unavailable", False, "invalid-output")

    pass_marker = "runtime-inputs-result: pass" in stdout
    refused_marker = "runtime-inputs-result: refused" in stdout
    reason_lines = [
        line for line in stdout.splitlines() if "runtime-inputs-reason:" in line
    ]
    reason = "diagnostic-unavailable"
    if len(reason_lines) == 1:
        prefix = "runtime-inputs-reason: "
        line = reason_lines[0]
        if line.startswith(prefix):
            candidate = line[len(prefix) :]
            if candidate in REASONS:
                reason = candidate

    if rc == 0 and pass_marker:
        if reason != "matched" or refused_marker:
            reason = "diagnostic-unavailable"
        return result(rc, "pass", reason, True, "none")
    if refused_marker:
        if rc == 0 or reason == "matched":
            reason = "diagnostic-unavailable"
        return result(rc, "refused", reason, pass_marker, "none")
    return result(rc, "unavailable", "diagnostic-unavailable", pass_marker, "none")


if __name__ == "__main__":
    emit(run())
