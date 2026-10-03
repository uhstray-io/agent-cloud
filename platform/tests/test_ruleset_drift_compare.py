"""Comparison logic of .github/rulesets/compare.py (docs/MISTAKES.md 10.21).

Fixtures are built from the real protect-main.json so the test tracks the file.
"""

import copy
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RULESETS = ROOT / ".github" / "rulesets"

_spec = importlib.util.spec_from_file_location("ruleset_compare", RULESETS / "compare.py")
compare_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(compare_mod)
compare = compare_mod.compare


def _declared():
    return json.loads((RULESETS / "protect-main.json").read_text())


def _live():
    """A live API response: the declared object plus fields the API adds."""
    live = copy.deepcopy(_declared())
    live.update({"id": 17752539, "source_type": "Repository", "node_id": "x"})
    for rule in live["rules"]:
        if rule["type"] == "pull_request":
            rule["parameters"]["required_reviewers"] = []
    live["rules"].reverse()  # order must not matter
    return live


def test_match():
    assert compare(_declared(), _live()) == ([], [])


def test_enforcement_drift():
    live = _live()
    live["enforcement"] = "evaluate"
    diffs, _ = compare(_declared(), live)
    assert diffs == ['enforcement: declared "active", live "evaluate"']


def test_extra_live_rule():
    live = _live()
    live["rules"].append({"type": "required_linear_history"})
    diffs, _ = compare(_declared(), live)
    assert diffs == ["rules[required_linear_history]: present live, not declared"]


def test_missing_live_rule():
    live = _live()
    live["rules"] = [r for r in live["rules"] if r["type"] != "deletion"]
    diffs, _ = compare(_declared(), live)
    assert diffs == ["rules[deletion]: declared, missing from live"]


def test_parameter_drift():
    live = _live()
    for rule in live["rules"]:
        if rule["type"] == "pull_request":
            rule["parameters"]["allowed_merge_methods"] = ["squash"]
    diffs, _ = compare(_declared(), live)
    assert len(diffs) == 1
    assert diffs[0].startswith("rules[pull_request].parameters.allowed_merge_methods:")


def test_bypass_actors_hidden_is_warning_not_match_claim():
    live = _live()
    del live["bypass_actors"]
    diffs, warnings = compare(_declared(), live)
    assert diffs == []
    assert len(warnings) == 1 and warnings[0].startswith("bypass_actors:")


def test_bypass_actors_drift():
    live = _live()
    live["bypass_actors"].append({"actor_id": 1, "actor_type": "Integration", "bypass_mode": "always"})
    diffs, _ = compare(_declared(), live)
    assert len(diffs) == 1 and diffs[0].startswith("bypass_actors:")


def test_cli_exit_codes(tmp_path):
    declared = tmp_path / "d.json"
    live = tmp_path / "l.json"
    declared.write_text(json.dumps(_declared()))
    live.write_text(json.dumps(_live()))
    assert compare_mod.main(["compare.py", str(declared), str(live)]) == 0
    drifted = _live()
    drifted["enforcement"] = "evaluate"
    live.write_text(json.dumps(drifted))
    assert compare_mod.main(["compare.py", str(declared), str(live)]) == 1
