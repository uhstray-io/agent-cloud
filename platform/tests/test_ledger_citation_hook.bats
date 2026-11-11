#!/usr/bin/env bats
# The ledger-citations pre-commit hook (.pre-commit-config.yaml, id ledger-citations) is
# wired to the citation test, test_ledger_test_citations.py (docs/MISTAKES.md 8.2).
# Origin: 2026-11-04, a ledger edit with a dead citation was pushed under SKIP_TESTS=1
# (docs/MISTAKES.md 5.2). This reads the hook block as text, the way
# test_graph_artifact_guard.bats reads the graph hook, so a deleted files line, a dropped
# skip or a retargeted entry turns it red.

load assert_helpers

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  CFG="$REPO_ROOT/.pre-commit-config.yaml"
}

# The block from its id line to its stages line, so no other hook's keys can satisfy an
# assertion here.
ledger_block() {
  sed -n '/id: ledger-citations/,/stages:/p' "$CFG"
}

@test "ledger citations hook: entry runs the citation test" {
  local blk
  blk=$(ledger_block)
  assert_grep -qF 'python3 -m pytest -q -p no:cacheprovider platform/tests/test_ledger_test_citations.py' <<<"$blk"
}

@test "ledger citations hook: files regex covers the ledger and its citation ratchet" {
  local blk
  blk=$(ledger_block)
  assert_grep -qF 'files: ^(docs/MISTAKES\.md|platform/tests/ledger_citation_ratchet\.txt)$' <<<"$blk"
}

@test "ledger citations hook: passes no filenames and prints its output when it passes" {
  local blk
  blk=$(ledger_block)
  assert_grep -qF 'pass_filenames: false' <<<"$blk"
  assert_grep -qF 'verbose: true' <<<"$blk"
}

@test "ledger citations hook: a missing pytest skips with a message, a real failure still fails" {
  local blk
  blk=$(ledger_block)
  assert_grep -qF 'python3 -c "import pytest" 2>/dev/null || {' <<<"$blk"
  assert_grep -qF 'pytest is not installed for python3; skipped (CI still runs it)' <<<"$blk"
  assert_grep -qF 'exit 0; };' <<<"$blk"
  assert_grep -qF 'exec python3 -m pytest' <<<"$blk"
}
