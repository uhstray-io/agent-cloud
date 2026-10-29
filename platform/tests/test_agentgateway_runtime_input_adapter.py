"""Command-boundary tests for safe agentgateway runtime comparison diagnostics."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ADAPTER = ROOT / "platform/playbooks/files/verify-agentgateway-runtime-inputs.py"


def invoke(
    tmp_path: Path,
    script: str,
    *,
    short_timeout: bool = False,
    empty_path: bool = False,
) -> tuple[dict, str]:
    (tmp_path / "deploy.sh").write_text(script)
    adapter_source = ADAPTER.read_text()
    if short_timeout:
        adapter_source = adapter_source.replace("TIMEOUT_SECONDS = 45", "TIMEOUT_SECONDS = 0.2")
    env = os.environ.copy()
    if empty_path:
        env["PATH"] = ""
    adapter_path = tmp_path / "adapter.py"
    adapter_path.write_text(adapter_source)
    completed = subprocess.run(
        [sys.executable, str(adapter_path)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
        env=env,
    )
    assert completed.returncode == 0
    return json.loads(completed.stdout), completed.stderr


def test_valid_reason_is_allowlisted_and_child_output_is_never_forwarded(tmp_path):
    report, stderr = invoke(
        tmp_path,
        "#!/bin/sh\necho 'private-container-name secret-value'\n"
        "echo 'runtime-inputs-reason: image-not-matched'\n"
        "echo 'runtime-inputs-result: refused'\n"
        "echo 'private-stderr secret-value' >&2\nexit 1\n",
    )
    assert report == {
        "rc": 1,
        "result": "refused",
        "reason": "image-not-matched",
        "pass_marker": False,
        "error": "none",
    }
    assert stderr == ""
    assert "private-container-name" not in json.dumps(report)
    assert "secret-value" not in json.dumps(report)


@pytest.mark.parametrize(
    "reason_lines",
    [
        "",
        "echo 'runtime-inputs-reason: invented-reason'",
        "echo 'runtime-inputs-reason: image-not-matched'\n"
        "echo 'runtime-inputs-reason: readiness-unproven'",
        "echo 'prefix runtime-inputs-reason: image-not-matched'",
    ],
)
def test_success_without_one_valid_reason_remains_accepted_as_unavailable(tmp_path, reason_lines):
    report, _ = invoke(
        tmp_path,
        f"#!/bin/sh\n{reason_lines}\necho 'runtime-inputs-result: pass'\nexit 0\n",
    )
    assert report["rc"] == 0
    assert report["result"] == "pass"
    assert report["pass_marker"] is True
    assert report["reason"] == "diagnostic-unavailable"


def test_legacy_success_without_reason_still_passes(tmp_path):
    report, _ = invoke(tmp_path, "#!/bin/sh\necho 'runtime-inputs-result: pass'\nexit 0\n")
    assert report["result"] == "pass"
    assert report["reason"] == "diagnostic-unavailable"


@pytest.mark.parametrize(
    ("rc", "reason", "markers", "expected_result"),
    [
        (1, "matched", "runtime-inputs-result: refused", "refused"),
        (0, "inputs-not-matched", "runtime-inputs-result: pass", "pass"),
        (
            0,
            "matched",
            "runtime-inputs-result: pass\nruntime-inputs-result: refused",
            "pass",
        ),
    ],
)
def test_reason_must_agree_with_pass_or_refusal(rc, reason, markers, expected_result, tmp_path):
    report, _ = invoke(
        tmp_path,
        f"#!/bin/sh\necho 'runtime-inputs-reason: {reason}'\n"
        + "\n".join(f"echo '{line}'" for line in markers.splitlines())
        + f"\nexit {rc}\n",
    )
    assert report["result"] == expected_result
    assert report["reason"] == "diagnostic-unavailable"


@pytest.mark.parametrize(
    ("rc", "markers", "expected"),
    [
        (1, "runtime-inputs-result: pass", "unavailable"),
        (0, "runtime-inputs-result: refused", "refused"),
        (1, "runtime-inputs-result: refused", "refused"),
    ],
)
def test_exit_and_pass_marker_must_both_accept(rc, markers, expected, tmp_path):
    report, _ = invoke(tmp_path, f"#!/bin/sh\necho '{markers}'\nexit {rc}\n")
    assert report["result"] == expected
    assert not (report["rc"] == 0 and report["pass_marker"] is True and expected != "pass")


def test_timeout_returns_only_fixed_error_and_does_not_forward_child_output(tmp_path):
    start = time.monotonic()
    report, stderr = invoke(
        tmp_path,
        "#!/bin/sh\necho 'secret-before-timeout' >&2\nsleep 30\n",
        short_timeout=True,
    )
    assert time.monotonic() - start < 3
    assert report["result"] == "unavailable"
    assert report["error"] == "timeout"
    assert report["reason"] == "diagnostic-unavailable"
    assert "secret-before-timeout" not in json.dumps(report) + stderr


def test_output_limit_returns_only_fixed_error_and_suppresses_child_bytes(tmp_path):
    report, stderr = invoke(
        tmp_path,
        "#!/bin/sh\npython3 -c 'import sys; sys.stdout.write(\"secret-bytes\" * 7000)'\n",
    )
    assert report["result"] == "unavailable"
    assert report["error"] == "output-limit"
    assert report["reason"] == "diagnostic-unavailable"
    assert "secret-bytes" not in json.dumps(report) + stderr


def test_invocation_failure_returns_fixed_metadata_without_exception_text(tmp_path):
    report, stderr = invoke(tmp_path, "#!/bin/sh\nexit 0\n", empty_path=True)
    assert report == {
        "rc": None,
        "result": "unavailable",
        "reason": "diagnostic-unavailable",
        "pass_marker": False,
        "error": "invocation-failed",
    }
    assert stderr == ""
