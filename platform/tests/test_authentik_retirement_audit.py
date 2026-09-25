"""The retirement audit counts exact usernames and never returns identities."""

import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / "services/authentik/deployment/audit-users.py"
spec = importlib.util.spec_from_file_location("authentik_retirement_audit", MODULE)
audit_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit_module)


def test_exact_match_and_name_free_summary():
    result = audit_module.audit(
        ["retired-account", "already-gone"],
        lambda _: [{"username": "retired-account-extra"}, {"username": "retired-account"}]
        if _ == "retired-account" else [],
    )
    assert result == {"retirements_declared": 2, "retirements_present": 1,
                      "retirements_absent": 1}
    assert "retired-account" not in str(result)


def test_prefixed_stdin_is_parsed_without_leaking_names(monkeypatch, capsys):
    monkeypatch.setenv("AUTHENTIK_BOOTSTRAP_TOKEN", "fixture-token")
    monkeypatch.setattr(sys, "stdin", io.StringIO('audit:["retired-account"]'))
    monkeypatch.setattr(audit_module, "get_users", lambda name, token: [{"username": name}])
    assert audit_module.main() == 0
    output = capsys.readouterr().out
    assert json.loads(output) == {"retirements_declared": 1, "retirements_present": 1,
                                  "retirements_absent": 0}
    assert "retired-account" not in output


def test_static_runtime_probe_loads_script_without_querying_accounts():
    result = subprocess.run(
        [sys.executable, "-c", MODULE.read_text()], input="audit-probe", text=True,
        capture_output=True, check=False,
        env={**os.environ, "AUTHENTIK_BOOTSTRAP_TOKEN": "fixture-token"},
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "authentik_retirement_audit_ready"
    assert result.stderr == ""

    absent_token = subprocess.run(
        [sys.executable, "-c", MODULE.read_text()], input="audit-probe", text=True,
        capture_output=True, check=False,
        env={key: value for key, value in os.environ.items()
             if key != "AUTHENTIK_BOOTSTRAP_TOKEN"},
    )
    assert absent_token.returncode == 2
    assert absent_token.stdout.strip() == (
        "authentik_retirement_audit_failed:bootstrap_token_unavailable")


def test_unexpected_error_details_stay_hidden(monkeypatch, capsys):
    monkeypatch.setenv("AUTHENTIK_BOOTSTRAP_TOKEN", "fixture-token")
    monkeypatch.setattr(sys, "stdin", io.StringIO('audit:["private-name"]'))

    def fail_lookup(name, token):
        raise ValueError("private-name fixture-token")

    monkeypatch.setattr(audit_module, "get_users", fail_lookup)
    assert audit_module.main() == 2
    assert capsys.readouterr().out.strip() == "authentik_retirement_audit_failed"
