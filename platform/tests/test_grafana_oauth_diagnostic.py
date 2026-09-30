"""Focused checks for the secret-safe Grafana OAuth diagnostic."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPT = Path(__file__).parents[1] / "playbooks" / "files" / "diagnose-grafana-oauth.py"
SPEC = importlib.util.spec_from_file_location("grafana_oauth_diagnostic", SCRIPT)
DIAGNOSTIC = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(DIAGNOSTIC)


@pytest.mark.parametrize(
    ("message", "expected_category"),
    (
        ("failed to fetch userinfo", "userinfo_rejected"),
        ("failed to exchange authorization code for token", "token_exchange_failed"),
        ("user is not a member of any allowed groups", "group_claim_rejected"),
    ),
)
def test_diagnostic_classifies_oauth_error_and_emits_no_log_data(
    capsys, message, expected_category
):
    raw_line = (
        f'logger=auth.generic_oauth level=error msg="{message}" '
        'url="https://auth.example/application/o/token" user="private@example.test" '
        'code=oauth-private-code access_token=private-token'
    )
    completed = subprocess.CompletedProcess(
        args=["podman", "logs"], returncode=0, stdout=raw_line, stderr=""
    )

    with patch.object(DIAGNOSTIC.subprocess, "run", return_value=completed):
        DIAGNOSTIC.main()

    output = capsys.readouterr().out
    assert output == f"{expected_category}\n"
    assert "auth.example" not in output
    assert "private@example.test" not in output
    assert "oauth-private-code" not in output
    assert "private-token" not in output


def test_diagnostic_classifies_stderr_only_oauth_error_without_exposing_it(capsys):
    raw_line = (
        'logger=auth.generic_oauth level=error msg="failed to fetch userinfo" '
        'url="https://auth.example/application/o/token" code=private-code'
    )
    completed = subprocess.CompletedProcess(
        args=["podman", "logs"], returncode=0, stdout="", stderr=raw_line
    )

    with patch.object(DIAGNOSTIC.subprocess, "run", return_value=completed):
        DIAGNOSTIC.main()

    output = capsys.readouterr().out
    assert output == "userinfo_rejected\n"
    assert "auth.example" not in output
    assert "private-code" not in output
