"""Tests for bounded, output-safe subprocess execution in harness_sandbox."""

import sys
import time

import harness_sandbox
import pytest


def test_run_timeout_suppresses_captured_child_output(tmp_path, monkeypatch):
    # Exercise the wrapper as it runs on GitHub-hosted Linux, where there is no kernel sandbox.
    monkeypatch.setattr(harness_sandbox, "SANDBOX", None)
    secret_marker = "tempo-fixture-secret-must-not-escape"
    command = [
        sys.executable,
        "-c",
        "import sys, time; "
        f"print({secret_marker!r}, flush=True); "
        f"print({secret_marker!r}, file=sys.stderr, flush=True); "
        "time.sleep(5)",
    ]

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="test command timed out after 0.1s") as error:
        harness_sandbox.run(
            command,
            tmp_path,
            cwd=tmp_path,
            env=harness_sandbox.env_for(tmp_path),
            timeout=0.1,
        )

    assert "captured output suppressed" in str(error.value)
    assert secret_marker not in str(error.value)
    assert error.value.__suppress_context__ is True
    assert error.value.__cause__ is None
    assert time.monotonic() - started < 2
