"""Mutation tests for the mistakes ledger's repeat metadata guards."""

import importlib.util
import unittest
from pathlib import Path

_VALIDATOR_PATH = Path(__file__).with_name("check_mistakes_ledger.py")
_SPEC = importlib.util.spec_from_file_location("check_mistakes_ledger", _VALIDATOR_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError(f"cannot load ledger validator from {_VALIDATOR_PATH}")
_VALIDATOR = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_VALIDATOR)
validate_ledger = _VALIDATOR.validate_ledger


def ledger(summary, class_name, body):
    return f"""## Index
| # | Mistake | Class | Enforced by |
|---|---|---|---|
| 1.1 | {summary} | {class_name} | Convention |

### 1.1 Example
{body}
"""


class MistakesLedgerTests(unittest.TestCase):
    def test_occurrences_two_requires_summary_repeat_marker(self):
        text = ledger("Repeated issue", "Process", "**Occurrences: 2**")
        self.assertTrue(any("1.1" in error and "x2" in error for error in validate_ledger(text)))

    def test_repeat_marker_must_match_occurrence_count(self):
        text = ledger("Repeated issue — **x3**", "Process", "**Occurrences: 2**")
        self.assertTrue(any("1.1" in error and "x2" in error for error in validate_ledger(text)))

    def test_repeat_marker_is_not_allowed_in_class_column(self):
        text = ledger("Repeated issue", "**x2** Process", "**Occurrences: 2**")
        self.assertTrue(any("1.1" in error and "Class" in error for error in validate_ledger(text)))

    def test_three_occurrences_reject_convention_without_named_proposal(self):
        text = ledger(
            "Repeated issue — **x3**",
            "Process",
            """**Occurrences: 3**
**Enforced by.** Convention.""",
        )
        self.assertTrue(any("1.1" in error and "proposal" in error.lower() for error in validate_ledger(text)))

    def test_unlabelled_proposal_and_test_narrative_does_not_satisfy_escalation(self):
        text = ledger(
            "Repeated issue — **x3**",
            "Process",
            """**Occurrences: 3**
**Enforced by.** Convention.
A proposal would be to add a test hook for this issue.""",
        )
        self.assertTrue(any("1.1" in error and "proposal" in error.lower() for error in validate_ledger(text)))

    def test_convention_plus_existing_backticked_test_is_mechanical_enforcement(self):
        text = ledger(
            "Repeated issue — **x3**",
            "Process",
            """**Occurrences: 3**
**Enforced by.** Convention, plus a test that pins the explicit escaping form
— `every play that resolves an OpenBao URL includes the transport guard`
in `platform/tests/test_credential_leaks.bats`.""",
        )
        self.assertEqual([], validate_ledger(text))

    def test_concrete_precommit_hook_proposal_names_its_config_target(self):
        text = ledger(
            "Repeated issue — **x3**",
            "Process",
            """**Occurrences: 3**
**Enforced by.** Convention, which three occurrences make insufficient. Concrete proposal: a local
pre-commit hook in `.pre-commit-config.yaml` that fails when a commit adds a protected path.""",
        )
        self.assertEqual([], validate_ledger(text))

    def test_vague_future_precommit_hook_proposal_is_not_concrete(self):
        text = ledger(
            "Repeated issue — **x3**",
            "Process",
            """**Occurrences: 3**
**Enforced by.** Convention.
Concrete proposal: perhaps add a pre-commit hook someday.""",
        )
        self.assertTrue(any("1.1" in error and "proposal" in error.lower() for error in validate_ledger(text)))

    def test_convention_plus_named_test_enforcement_needs_no_proposal(self):
        text = ledger(
            "Repeated issue — **x3**",
            "Process",
            """**Occurrences: 3**
**Enforced by.** Convention + test (`guard_test`).""",
        )
        self.assertEqual([], validate_ledger(text))

    def test_missing_body_enforcement_fails_when_index_says_convention(self):
        text = ledger("Repeated issue — **x3**", "Process", "**Occurrences: 3**")
        self.assertTrue(any("1.1" in error and "Enforced by" in error for error in validate_ledger(text)))

    def test_single_occurrence_needs_no_marker(self):
        text = ledger("One issue", "Process", "**Occurrences: 1**\n**Enforced by.** Convention.")
        self.assertEqual([], validate_ledger(text))

    def test_three_occurrences_pass_with_mechanical_guard(self):
        text = ledger(
            "Repeated issue — **x3**",
            "Process",
            """**Occurrences: 3**
**Enforced by.** Test (`test_guard`).""",
        )
        self.assertEqual([], validate_ledger(text))

    def test_three_occurrences_pass_with_named_hook_proposal(self):
        text = ledger(
            "Repeated issue — **x3**",
            "Process",
            """**Occurrences: 3**
**Enforced by.** Convention.
**Proposal (count ≥ 3).** A PreToolUse hook on Grep warns on empty results.""",
        )
        self.assertEqual([], validate_ledger(text))


if __name__ == "__main__":
    unittest.main()
