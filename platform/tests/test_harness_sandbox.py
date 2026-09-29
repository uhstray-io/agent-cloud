"""Tests for bounded, output-safe subprocess execution in harness_sandbox."""

import sys
import time

import harness_sandbox


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
    result = harness_sandbox.run(
        command,
        tmp_path,
        cwd=tmp_path,
        env=harness_sandbox.env_for(tmp_path),
        timeout=0.1,
    )

    assert result.returncode == 124
    assert result.stdout == ""
    assert "timed out after 0.1s" in result.stderr
    assert "captured output suppressed" in result.stderr
    assert secret_marker not in result.stderr
    assert secret_marker not in result.stdout
    assert time.monotonic() - started < 2
