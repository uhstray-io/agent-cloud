"""Focused checks for the secret-safe Grafana OAuth diagnostic."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

SCRIPT = Path(__file__).parents[1] / "playbooks" / "files" / "diagnose-grafana-oauth.py"
SPEC = importlib.util.spec_from_file_location("grafana_oauth_diagnostic", SCRIPT)
DIAGNOSTIC = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(DIAGNOSTIC)


@pytest.mark.parametrize(
    ("message", "expected_category"),
    (
        (
            'error="[auth.oauth.userinfo.error] failed to get user info"',
            "userinfo_rejected",
        ),
        (
            'error="[auth.oauth.token.exchange] failed to exchange code to token"',
            "token_exchange_failed",
        ),
        ("user is not a member of any allowed groups", "group_claim_rejected"),
    ),
)
def test_diagnostic_classifies_oauth_error_and_emits_no_log_data(
    capsys, message, expected_category
):
    raw_line = (
        f'logger=authn.service level=error msg="Failed to authenticate request" '
        f'client=auth.client.generic_oauth {message} '
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
        'logger=authn.service level=error msg="Failed to authenticate request" '
        'client=auth.client.generic_oauth error="[auth.oauth.userinfo.error] failed to get user info" '
        'url="https://auth.example/application/o/token" code=private-code'
    )
    completed = subprocess.CompletedProcess(
        args=["podman", "logs"], returncode=0, stdout=raw_line, stderr=None
    )

    with patch.object(DIAGNOSTIC.subprocess, "run", return_value=completed) as run:
        DIAGNOSTIC.main()

    output = capsys.readouterr().out
    assert output == "userinfo_rejected\n"
    assert "auth.example" not in output
    assert "private-code" not in output
    assert run.call_args.kwargs["stderr"] == DIAGNOSTIC.subprocess.STDOUT


@pytest.mark.parametrize(
    "line",
    (
        'logger=database.cleanup level=error msg="failed to refresh token claim group"',
        'logger=context level=error path=/oauth/callback msg="missing group claim"',
        'logger=authn.service level=info client=auth.client.generic_oauth msg="scope token group"',
    ),
)
def test_unrelated_or_non_error_token_group_claim_lines_do_not_match(line):
    assert DIAGNOSTIC.classify_lines([line]) == "no_oauth_failure_in_window"


def test_no_oauth_failure_is_not_an_actionable_playbook_success():
    playbook = yaml.safe_load(
        (Path(__file__).parents[1] / "playbooks" / "diagnose-o11y-grafana-auth.yml").read_text()
    )
    diagnostic_play = next(play for play in playbook if play.get("hosts") == "o11y_svc")

    assert "no_oauth_failure_in_window" not in diagnostic_play["vars"]["_allowed_categories"]
    final_assert = diagnostic_play["tasks"][-1]["ansible.builtin.assert"]
    assert final_assert["that"] == "_oauth_category in _allowed_categories"
