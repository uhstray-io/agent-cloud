# Mistakes Ledger Checks Implementation Plan

Author: Joseph A. Wisneski IV
Date: 2026-09-28
Status: Implemented; awaiting PR review

> **For agentic workers:** Implement the bounded test and index changes below. The parent retains review and PR authority.

**Goal:** Make the repository's established four-column `docs/MISTAKES.md` Index repeat convention and three-strikes escalation mechanically checkable without rewriting historical entry prose.

**Architecture:** A small Python validator reads the Index and numbered entry bodies. The existing BATS suite calls it so CI exercises it; focused Python tests mutate synthetic ledgers to demonstrate both refusal paths. The three existing metadata inconsistencies on `dev` are corrected in Index rows only.

**Tech Stack:** Python 3 standard library, BATS, Markdown.

## Scope and steps

1. Add `platform/tests/test_mistakes_ledger.py` with synthetic four-column ledger fixtures. Test an entry with `**Occurrences: 2**` but no summary `**x2**`, a mismatched `**x3**`, and a repeat marker stranded in the Class column. Test an entry at three occurrences with `**Enforced by.** Convention.` and no concrete guard/proposal. Include passing cases for single occurrences, a mechanical guard, and a convention entry with a clearly labelled hook proposal. Run the focused tests before implementing the validator; confirm failure because the implementation is absent.
2. Add a stdlib-only `platform/tests/check_mistakes_ledger.py` that checks every Index entry's summary repeat marker against its body's numeric occurrence count (no explicit count means one). Require `**xN**` only in the summary for N ≥ 2. Fail a body with N ≥ 3 when its enforcement is only Convention and it has no named proposal in the body. Emit numbered, actionable errors and return nonzero on failure. Keep other legacy entry layouts unchanged.
3. Add one BATS test to `platform/tests/test_mistakes_doc.bats` invoking the validator against `docs/MISTAKES.md`. Expect an initially red result for the three existing inconsistent Index rows; correct only the Index rows for 2.15, 6.3, and 10.9 (including removal of the misplaced Class marker from 2.15). Do not invent a new mistake entry for this preventative guard.
4. Run `python3 -m unittest platform.tests.test_mistakes_ledger` (or the precise executable test command the new file supports), `bats platform/tests/test_mistakes_doc.bats`, and the repo's relevant linters. Confirm a mutation makes each guard fail before accepting the green result. Review diff and staged files for secrets, names, machine paths, and unrelated changes.
5. Commit only the validator, its tests, the corrected Index rows, and this plan. The parent will independently verify results and, with the operator's authorization, push the feature branch and open a PR targeting `dev`. Do not merge.

## Local verification (2026-09-28)

- The validator's 13 focused fixture tests pass, including rejection and acceptance of the legacy escalation forms.
- Full Python suite: 785 passed, 2 skipped, 26 subtests passed; full BATS suite: 700 passed.
- Ruff, ShellCheck, `git diff --check`, and the validator against the current ledger pass.
- CI and human review remain pending; this plan does not assert a merged outcome.

## Boundaries

- The public ledger remains four columns with `**xN**` in the Index summary; no Count-column migration.
- Historical `What happened`, `Root cause`, and `The rule` paragraphs remain untouched.
- Do not infer an incident from the addition of preventative tests. A new mistake entry requires an actual qualifying occurrence and its evidence.
