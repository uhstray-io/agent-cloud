#!/usr/bin/env python3
"""Validate repeat metadata and escalation in docs/MISTAKES.md."""

import re
import sys
from pathlib import Path

_INDEX_ROW = re.compile(r"^\|\s*(\d+\.\d+)\s*\|((?:\\\||[^|])*)\|((?:\\\||[^|])*)\|((?:\\\||[^|])*)\|\s*$")
_ENTRY_HEADING = re.compile(r"^### (\d+\.\d+) ", re.MULTILINE)
_REPEAT_MARKER = re.compile(r"\*\*x(\d+)\*\*")
_OCCURRENCES = re.compile(r"\*\*Occurrences:\s*(\d+)\*\*")
_PROPOSAL_LABEL = re.compile(r"(?:\*\*Proposal[^*]*\*\*|\bProposal:)", re.IGNORECASE)
_NAMED_MECHANISM = re.compile(
    r"\b(?:PreToolUse|PostToolUse)\s+hook\b|`[^`]+`\s+(?:test|hook|guard|playbook|check|lint)\b"
    r"|\bpre-commit hook in `[^`]+`",
    re.IGNORECASE,
)
_MECHANICAL_ENFORCEMENT = re.compile(
    r"\+\s*(?:test|hook|guard|playbook|check|lint)\s*\(\s*[^)]*\w[^)]*\)", re.IGNORECASE
)
_NAMED_TEST_ENFORCEMENT = re.compile(
    r"\bConvention,\s+plus a test\b.*?`[^`]+`.*?`[^`]*tests?/[^`]+`", re.IGNORECASE | re.DOTALL
)


def _index_rows(document):
    try:
        index = document.split("## Index", 1)[1].split("\n---", 1)[0]
    except IndexError:
        return [], ["mistakes ledger: missing ## Index"]

    rows = []
    errors = []
    for line_number, line in enumerate(index.splitlines(), start=1):
        if not line.startswith("|") or line.startswith(("| #", "|---")):
            continue
        match = _INDEX_ROW.match(line)
        if match:
            rows.append((match.group(1), *match.groups()[1:]))
        elif re.match(r"^\|\s*\d+\.\d+\s*\|", line):
            errors.append(f"Index line {line_number}: malformed four-column row")
    return rows, errors


def validate_ledger(document):
    """Return actionable errors for repeat-count and three-strikes mismatches."""
    rows, errors = _index_rows(document)
    if not rows and not errors:
        return ["mistakes ledger: no Index rows found"]

    headings = list(_ENTRY_HEADING.finditer(document))
    bodies = {}
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(document)
        bodies[heading.group(1)] = document[heading.start() : end]

    for number, summary, class_name, _enforcement in rows:
        body = bodies.get(number)
        if body is None:
            errors.append(f"{number}: Index row has no matching numbered entry body")
            continue

        occurrences = _OCCURRENCES.search(body)
        count = int(occurrences.group(1)) if occurrences else 1
        summary_markers = [int(value) for value in _REPEAT_MARKER.findall(summary)]
        class_markers = _REPEAT_MARKER.findall(class_name)
        if class_markers:
            errors.append(f"{number}: repeat marker belongs in the summary, not the Class column")
        expected = [] if count == 1 else [count]
        if summary_markers != expected:
            required = "no repeat marker" if count == 1 else f"exactly **x{count}**"
            errors.append(f"{number}: summary must contain {required} for {count} occurrence(s)")

        body_enforcement = re.search(r"\*\*Enforced by\.\*\*\s*([^\n]+)", body)
        if count >= 3 and not body_enforcement:
            errors.append(f"{number}: {count} occurrences have no body Enforced by declaration")
            continue

        if count >= 3 and body_enforcement:
            enforcement_value = body_enforcement.group(1)
            convention_only = re.match(r"\s*Convention(?:\.|\b)", enforcement_value, re.IGNORECASE)
            enforcement_end = body.find("\n\n", body_enforcement.start())
            if enforcement_end < 0:
                enforcement_end = len(body)
            enforcement_paragraph = body[body_enforcement.start() : enforcement_end]
            has_mechanical_enforcement = _MECHANICAL_ENFORCEMENT.search(
                enforcement_value
            ) or _NAMED_TEST_ENFORCEMENT.search(enforcement_paragraph)
            if convention_only and not has_mechanical_enforcement:
                proposal_text = ""
                paragraph_end = body.find("\n\n", body_enforcement.start())
                if paragraph_end < 0:
                    paragraph_end = len(body)
                for proposal in _PROPOSAL_LABEL.finditer(body):
                    proposal_paragraph = body[proposal.start() :].split("\n\n", 1)[0]
                    is_labelled_section = proposal.group(0).startswith("**")
                    is_enforcement_proposal = body_enforcement.start() <= proposal.start() < paragraph_end
                    if is_labelled_section or is_enforcement_proposal:
                        proposal_text = proposal_paragraph
                        break
                if not _NAMED_MECHANISM.search(proposal_text):
                    errors.append(
                        f"{number}: {count} occurrences enforced only by Convention; "
                        "add a named concrete proposal or a mechanical guard"
                    )

    return errors


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    path = Path(args[0]) if args else Path(__file__).parents[2] / "docs" / "MISTAKES.md"
    try:
        document = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"mistakes ledger: cannot read {path}: {exc}", file=sys.stderr)
        return 2

    errors = validate_ledger(document)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print(f"OK: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
