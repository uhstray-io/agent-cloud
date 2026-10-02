"""Every test the mistakes ledger cites exists (docs/MISTAKES.md 8.2).

The ledger's "Enforced by" lines are claims: a reader follows them to the test that guards the
rule. A cited test that does not exist, or a test name its file never defines, reads as a guard
and guards nothing. 8.2 records exactly that, twice, enforced only by convention.

Parse rule. Fenced code blocks are dropped. The rest is split into claims: blank-line-separated
paragraphs, except that each Markdown table row (a line starting with `|`) is its own claim, so a
row's citations resolve against that row alone. Nothing depends on the Index table's columns or
order. Inside each claim, every inline code span (single backticks, whitespace collapsed, so a
span wrapped across lines still counts) is classified:

1. FILE  -- `[dir/]test_<x>.py` or `[dir/]<x>.bats`, optionally followed by `::<name>` or by
   `:<line>[-<line>]`. A path containing `/` must exist relative to the repo root; a bare file
   name must match some file in the repo. With `::<name>`, that file must define `<name>`
   (`def <name>(` or `class <name>`).
2. NAME  -- a bare `test_<x>` identifier. If the claim also cites FILE spans, the name must be
   defined in one of those files (or be one's stem); otherwise in any `test_*.py` in the repo
   (or be the stem of a test file).
3. BATS NAME -- in a claim that cites at least one `.bats` file: a span of five or more words
   that starts with a lowercase letter and contains none of `| $ = ( ) { } [ ] < >` and no
   word starting with `-` (that excludes commands, output and YAML). It must equal, after
   whitespace collapsing, an `@test "..."` name in one of that claim's cited `.bats` files.

Anything else -- commands, playbooks, prose -- is not a test citation and is not checked.

A citation that cannot resolve because the test was deleted or renamed after the entry was
written stays in the append-only ledger. It is listed in `ledger_citation_ratchet.txt` as
`<entry> <citation> # <reason>`, where `<entry>` is the first word of the nearest heading above
it (`8.2` for `### 8.2 ...`). The comparison is a multiset: one ratchet line covers one
occurrence in one entry, so the same missing name cited again -- in a new entry or a second
time in the old one -- is red. The list may only shrink: a line that resolves again, or is no
longer cited, fails this test until it is deleted.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LEDGER = REPO / "docs" / "MISTAKES.md"
RATCHET = Path(__file__).with_name("ledger_citation_ratchet.txt")

_FILE = re.compile(
    r"(?P<path>(?:[\w.-]+/)*(?:test_\w+\.py|[\w-]+\.bats))"
    r"(?:::(?P<name>\w+)|:\d+(?:-\d+)?)?"
)
_NAME = re.compile(r"test_\w+")
_FENCE = re.compile(r"(?ms)^[ \t]*```.*?^[ \t]*```[^\n]*$")
_SPAN = re.compile(r"`([^`]+)`")
_CODE_CHARS = set("|$=(){}[]<>")
_SKIP_DIRS = {".git", "node_modules", ".venv", "__pycache__"}


def claims(text: str) -> list[tuple[str, str]]:
    """(entry, claim) pairs: paragraphs of `text`, each table row split out as its own claim.

    `entry` is the first word of the nearest heading above, trailing dot dropped: `8.2` for
    `### 8.2 Invented ...`, `Index` for `## Index`.
    """
    out: list[tuple[str, str]] = []
    entry = ""
    for para in re.split(r"\n[ \t]*\n", _FENCE.sub("", text)):
        rest: list[str] = []
        for line in para.splitlines() + ["# "]:  # the sentinel heading flushes the paragraph
            heading = re.match(r"#{1,6}\s+(\S*)", line)
            if heading:
                if any(r.strip() for r in rest):
                    out.append((entry, "\n".join(rest)))
                rest = []
                entry = heading[1].rstrip(".") or entry
            elif line.lstrip().startswith("|"):
                out.append((entry, line))
            else:
                rest.append(line)
    return out


def spans(claim: str) -> list[str]:
    return [re.sub(r"\s+", " ", s).strip() for s in _SPAN.findall(claim)]


def _looks_like_bats_name(span: str) -> bool:
    words = span.split(" ")
    return (
        len(words) >= 5
        and span[:1].islower()
        and not (_CODE_CHARS & set(span))
        and not any(w.startswith("-") for w in words)
    )


def _repo_files(root: Path) -> dict[str, list[Path]]:
    by_name: dict[str, list[Path]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for f in filenames:
            by_name.setdefault(f, []).append(Path(dirpath) / f)
    return by_name


def _defines(path: Path, name: str) -> bool:
    pat = re.compile(rf"^\s*(?:async\s+)?(?:def|class)\s+{re.escape(name)}\b", re.M)
    return bool(pat.search(path.read_text(errors="replace")))


def _bats_names(path: Path) -> set[str]:
    text = path.read_text(errors="replace")
    found = re.findall(r"""^\s*@test\s+(?:"((?:[^"\\]|\\.)*)"|'([^']*)')""", text, re.M)
    return {re.sub(r"\s+", " ", dq or sq).strip() for dq, sq in found}


def unresolved(text: str, root: Path) -> list[str]:
    """Every test citation in `text` that does not resolve against the tree at `root`, as `<entry> <citation>`."""
    by_name = _repo_files(root)
    test_files = [p for name, ps in by_name.items() if re.fullmatch(r"test_\w+\.py|[\w-]+\.bats", name) for p in ps]
    bad: list[str] = []
    for entry, claim in claims(text):
        bad_before = len(bad)
        ss = spans(claim)
        cited: list[Path] = []
        for s in ss:
            m = _FILE.fullmatch(s)
            if not m:
                continue
            rel = m["path"]
            found = [root / rel] if "/" in rel else by_name.get(rel, [])
            found = [p for p in found if p.is_file()]
            if not found:
                bad.append(s)
                continue
            cited.extend(found)
            if m["name"] and not any(_defines(p, m["name"]) for p in found):
                bad.append(s)
        cited_bats = [p for p in cited if p.suffix == ".bats"]
        for s in ss:
            if _NAME.fullmatch(s):
                pool = cited or test_files
                if not any(p.stem == s or (p.suffix == ".py" and _defines(p, s)) for p in pool):
                    bad.append(s)
            elif cited_bats and not _FILE.fullmatch(s) and _looks_like_bats_name(s):
                if not any(s in _bats_names(p) for p in cited_bats):
                    bad.append(s)
        bad[bad_before:] = [f"{entry} {s}" for s in bad[bad_before:]]
    return bad


def parse_ratchet(text: str) -> list[tuple[str, str]]:
    """(`<entry> <citation>`, reason) per line; one line per occurrence, so duplicates count."""
    entries: list[tuple[str, str]] = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, _, reason = line.partition(" # ")
        entries.append((re.sub(r"\s+", " ", key).strip(), reason.strip()))
    return entries


def ratchet_diff(found: list[str], ratchet: list[tuple[str, str]]) -> tuple[list[str], list[str]]:
    """(new, stale) as multisets: a second occurrence of a ratcheted citation is new, not covered."""
    have, allowed = Counter(found), Counter(k for k, _ in ratchet)
    return sorted((have - allowed).elements()), sorted((allowed - have).elements())


def test_every_cited_test_exists():
    new, _ = ratchet_diff(unresolved(LEDGER.read_text(), REPO), parse_ratchet(RATCHET.read_text()))
    assert not new, (
        "docs/MISTAKES.md cites tests that do not exist (8.2). Fix the citation, or, for a "
        f"test deleted or renamed since the entry was written, ratchet it with a reason: {new}"
    )


def test_ratchet_entries_carry_a_reason_and_still_fail():
    ratchet = parse_ratchet(RATCHET.read_text())
    assert all(r for _, r in ratchet), f"ratchet entries without a ' # reason': {[k for k, r in ratchet if not r]}"
    _, stale = ratchet_diff(unresolved(LEDGER.read_text(), REPO), ratchet)
    assert not stale, f"ratchet entries that now resolve or are no longer cited; delete them: {stale}"


def test_a_ratcheted_citation_repeated_in_a_new_entry_is_not_covered(tmp_path):
    (tmp_path / "platform" / "tests").mkdir(parents=True)
    old = "### 1.1 Old entry\n\n**Enforced by.** `test_renamed_away`.\n"
    ratchet = parse_ratchet("1.1 test_renamed_away # renamed by abc1234\n")
    assert ratchet_diff(unresolved(old, tmp_path), ratchet) == ([], [])
    repeated_new_entry = old + "\n### 1.2 New entry\n\n**Enforced by.** `test_renamed_away`.\n"
    assert ratchet_diff(unresolved(repeated_new_entry, tmp_path), ratchet) == (["1.2 test_renamed_away"], [])
    repeated_same_entry = old + "\n**Occurrence 2.** Still `test_renamed_away`.\n"
    assert ratchet_diff(unresolved(repeated_same_entry, tmp_path), ratchet) == (["1.1 test_renamed_away"], [])


def test_the_checker_catches_an_invented_test(tmp_path):
    tests = tmp_path / "platform" / "tests"
    tests.mkdir(parents=True)
    (tests / "test_real.py").write_text("def test_present():\n    pass\n")
    (tests / "test_real.bats").write_text('@test "real: this name exists in the file" {\n  true\n}\n')
    good = (
        "### 1.1 An entry\n\n**Enforced by.** `test_present` in `platform/tests/test_real.py`, and\n"
        "`real: this name exists in the file` in `platform/tests/test_real.bats`.\n\n"
        "```\nbats platform/tests/test_fenced_is_ignored.bats\n```\n\n"
        "| 1.1 | `test_real.py::test_present` | Class | 1 | Test |\n"
    )
    assert unresolved(good, tmp_path) == []
    invented = (
        "### 2.3 Another entry\n**Enforced by.** `test_absent` in `platform/tests/test_real.py`;\n"
        "`platform/tests/test_missing.py`; `test_real.py::test_nope`;\n"
        "`real: this name was never written down` in `platform/tests/test_real.bats`.\n"
    )
    assert sorted(unresolved(invented, tmp_path)) == sorted(
        [
            "2.3 test_absent",
            "2.3 platform/tests/test_missing.py",
            "2.3 test_real.py::test_nope",
            "2.3 real: this name was never written down",
        ]
    )
