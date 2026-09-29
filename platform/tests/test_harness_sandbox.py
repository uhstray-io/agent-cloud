"""Tests for bounded, output-safe subprocess execution in harness_sandbox."""

import subprocess
import sys
import time
import traceback

import harness_sandbox
import pytest


def test_run_timeout_suppresses_captured_child_output(tmp_path, monkeypatch):
    # Exercise the wrapper as it runs on GitHub-hosted Linux, where there is no kernel sandbox.
    monkeypatch.setattr(harness_sandbox, "SANDBOX", None)
    secret_marker = b"tempo-fixture-secret-must-not-escape"
    command = ["fixture-command"]

    def timeout_with_secret_output(cmd, **kwargs):
        raise subprocess.TimeoutExpired(
            cmd,
            kwargs["timeout"],
            output=secret_marker,
            stderr=secret_marker,
        )

    monkeypatch.setattr(harness_sandbox.subprocess, "run", timeout_with_secret_output)
    with pytest.raises(RuntimeError, match="test command timed out after 0.1s") as error:
        harness_sandbox.run(
            command,
            tmp_path,
            cwd=tmp_path,
            env=harness_sandbox.env_for(tmp_path),
            timeout=0.1,
        )

    assert "captured output suppressed" in str(error.value)
    assert secret_marker.decode() not in "".join(traceback.format_exception(error.value))
    assert error.value.__suppress_context__ is True
    assert error.value.__cause__ is None


def test_run_enforces_subprocess_timeout(tmp_path, monkeypatch):
    # Exercise the actual child-process timeout independently of the deterministic no-leak test.
    monkeypatch.setattr(harness_sandbox, "SANDBOX", None)
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="test command timed out after 0.1s"):
        harness_sandbox.run(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            tmp_path,
            cwd=tmp_path,
            env=harness_sandbox.env_for(tmp_path),
            timeout=0.1,
        )
    assert time.monotonic() - started < 2
