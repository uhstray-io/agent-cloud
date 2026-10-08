"""`dev` is deletion-protected by a committed ruleset (docs/MISTAKES.md 3.13).

The repository deletes a PR's head branch on merge. A dev -> main promotion has `dev` as its
head, so merging PR #447 deleted the long-lived integration branch. GitHub documents that
rules can prevent that automatic deletion, so `protect-dev.json` carries the `deletion` rule.
"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RULESETS = ROOT / ".github" / "rulesets"
SYNC = ROOT / ".github" / "workflows" / "sync-main-to-dev.yml"


def _dev():
    return json.loads((RULESETS / "protect-dev.json").read_text())


def _types(ruleset):
    return [r["type"] for r in ruleset["rules"]]


def test_targets_exactly_dev():
    ruleset = _dev()
    assert ruleset["target"] == "branch"
    assert ruleset["conditions"]["ref_name"]["include"] == ["refs/heads/dev"]
    assert ruleset["conditions"]["ref_name"]["exclude"] == []


def test_enforcing_and_blocks_deletion():
    ruleset = _dev()
    assert ruleset["enforcement"] == "active"
    assert "deletion" in _types(ruleset)


def test_blocks_force_push():
    assert "non_fast_forward" in _types(_dev())


def test_no_pull_request_or_check_rule():
    # sync-main-to-dev.yml pushes straight to dev with GITHUB_TOKEN; a PR or status-check
    # rule would refuse that push and stop main -> dev reconciliation.
    assert not {"pull_request", "required_status_checks"} & set(_types(_dev()))


def test_no_bypass_actor():
    assert _dev()["bypass_actors"] == []


def test_sync_workflow_push_is_not_a_force_push():
    # non_fast_forward would refuse a forced push, so the sync must never use one.
    pushes = re.findall(r"^\s*git push .*$", SYNC.read_text(), flags=re.M)
    assert pushes
    assert not any(re.search(r"--force|\s-f\b|\+HEAD", p) for p in pushes)
