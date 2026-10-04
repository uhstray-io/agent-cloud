"""Ratchet: no playbook or task takes `| first` / `[0]` of a set-operation result.

ansible-core's `intersect`, `difference`, `union` and `symmetric_difference` filters are set
operations: for hashable items the result order follows per-process string hash randomisation
(ansible/plugins/filter/mathstuff.py). Taking the first element of one picks a different item run
to run. Production conformance runs (Semaphore tasks 2614, 2681, 2698) tested different models for
exactly this reason. Use an order-preserving form instead, e.g.
`allowed | select('in', declared) | list`.

Two shapes are caught, by scanning the YAML text of every playbook/task file under platform/:
  1. one Jinja expression that applies a set filter and then `first` / `[0]`;
  2. a variable whose definition applies a set filter, then `<var> | first` / `<var>[0]` anywhere
     in the same file.
"""
from __future__ import annotations

import re

import playbook_yaml

REPO = playbook_yaml.REPO
SET_OPS = r"\|\s*(?:intersect|difference|union|symmetric_difference)\b"
FIRST = r"(?:\|\s*first\b|\[\s*0\s*\])"
INLINE = re.compile(SET_OPS + r".*?" + FIRST, re.S)
# `name: value` (vars / set_fact), the value running over its more-indented continuation lines.
DEFN = re.compile(r"^(?P<ind>[ \t]*)(?:- )?(?P<name>[A-Za-z_][A-Za-z0-9_]*):(?P<val>.*)$")


def _definitions(text: str):
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = DEFN.match(line)
        if not m:
            continue
        val, ind = [m["val"]], len(m["ind"])
        for nxt in lines[i + 1:]:
            if nxt.strip() and len(nxt) - len(nxt.lstrip()) <= ind:
                break
            val.append(nxt)
        yield m["name"], "\n".join(val)

# Known offenders, each with the reason it is tolerated. Empty: keep it that way.
RATCHET: dict[str, str] = {}


def offenders_in(text: str) -> list[str]:
    found = []
    for expr in re.findall(r"\{\{(.*?)\}\}", text, re.S):
        if INLINE.search(expr):
            found.append("inline: " + " ".join(expr.split())[:120])
    for name, val in _definitions(text):
        # A key whose own value directly applies a set filter (not a parent mapping holding one).
        own = re.search(SET_OPS, val) and not any(DEFN.match(v) for v in val.splitlines()[1:])
        if own and re.search(r"\b" + re.escape(name) + r"\s*" + FIRST, text):
            found.append("via variable: " + name)
    return sorted(set(found))


def _files():
    for p in sorted((REPO / "platform").rglob("*")):
        rel = p.relative_to(REPO).parts
        if p.suffix in (".yml", ".yaml") and p.is_file() and "tests" not in rel:
            yield p


def test_no_first_element_of_a_set_operation_outside_the_ratchet():
    hits = {}
    for p in _files():
        found = offenders_in(p.read_text(errors="replace"))
        if found:
            hits[str(p.relative_to(REPO))] = found
    assert set(hits) <= set(RATCHET), {k: v for k, v in hits.items() if k not in RATCHET}
    stale = set(RATCHET) - set(hits)
    assert not stale, f"ratchet entries no longer offend, remove them: {sorted(stale)}"


def test_the_scanner_catches_both_shapes_and_passes_the_order_preserving_form():
    inline = "x: \"{{ a | intersect(b) | first }}\"\n"
    indexed = "x: \"{{ (a | difference(b))[0] }}\"\n"
    via = "vars:\n  _m: >-\n    {{ a\n       | union(b) }}\n  _pick: \"{{ _m | first | default('') }}\"\n"
    ok = ("vars:\n  _m: >-\n    {{ a | select('in', b) | list }}\n  _pick: \"{{ _m | first }}\"\n"
          "  _n: \"{{ a | intersect(b) | length }}\"\n")
    assert offenders_in(inline) and offenders_in(indexed) and offenders_in(via)
    assert offenders_in(ok) == []
