"""The retirement audit counts exact usernames and never returns identities."""

import importlib.util
import io
import json
from pathlib import Path
import sys


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
