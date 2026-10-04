"""Ratchet: no playbook or task takes an end element of an unordered set-operation result.

ansible-core's `intersect`, `difference`, `union` and `symmetric_difference` filters are set
operations: for hashable items the result order follows per-process string hash randomisation
(ansible/plugins/filter/mathstuff.py). Taking the first (or last) element of one picks a different
item run to run. Production conformance runs (Semaphore tasks 2614, 2681, 2698) tested different
models for exactly this reason. Use an order-preserving form instead, e.g.
`allowed | select('in', declared) | list`, or impose an order with `sort` first.

The scan is pipeline-aware. From a set filter it walks the following filter chain:
  - `sort` (any arguments) imposes an order: safe, the walk stops;
  - order-neutral filters (`list`, `map`, `select`, `unique`, ...) keep the result unordered;
  - `first`, `last`, `[0]` or `[-1]` on a still-unordered result is an offender;
  - anything else (`length`, `join`, a non-end index, ...) stops the walk without a finding.
A closing `)` is stepped over, so `(a | difference(b))[0]` is caught.

A variable whose definition ends unordered (a set filter with no `sort` after it) is followed to
its uses, and each use is walked the same way. Variable resolution is scoped to the defining play
when the file is a playbook (top-level items carrying `hosts:`); a task file is one scope, because
a `set_fact` in one task is read by later tasks. Limit: a variable defined in one file and read in
another (an included task file, inventory) is not followed.
"""
from __future__ import annotations

import re

import playbook_yaml
import pytest

REPO = playbook_yaml.REPO
SET_OP = re.compile(r"\|\s*(intersect|difference|union|symmetric_difference)\b")
FILTER = re.compile(r"\|\s*([A-Za-z_][A-Za-z0-9_]*)")
NEUTRAL = {"list", "map", "select", "reject", "selectattr", "rejectattr", "unique", "flatten",
           "default", "d", "difference", "intersect", "union", "symmetric_difference"}
END = {"first", "last"}
DEFN = re.compile(r"^(?P<ind>[ \t]*)(?:- )?(?P<name>[A-Za-z_][A-Za-z0-9_]*):(?P<val>.*)$")

# Known offenders, each with the reason it is tolerated. Empty: keep it that way.
RATCHET: dict[str, str] = {}


def _balanced(s: str, i: int, open_: str, close: str) -> int:
    """Index just past the bracket group starting at s[i] == open_."""
    depth = 0
    while i < len(s):
        if s[i] == open_:
            depth += 1
        elif s[i] == close:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return i


def walk(s: str, i: int) -> str:
    """Walk the filter chain from s[i] on an unordered value: 'offender', 'safe' or 'end'."""
    while True:
        while i < len(s) and (s[i].isspace() or s[i] == ")"):
            i += 1
        if i >= len(s) or s.startswith("}}", i):
            return "end"
        if s[i] == "[":
            j = _balanced(s, i, "[", "]")
            return "offender" if s[i + 1:j - 1].strip() in ("0", "-1") else "safe"
        m = FILTER.match(s, i)
        if not m:
            return "safe"
        name, i = m[1], m.end()
        if i < len(s) and s[i] == "(":
            i = _balanced(s, i, "(", ")")
        if name in END:
            return "offender"
        if name not in NEUTRAL:  # `sort` imposes an order; `length`, `join`, ... end the chain
            return "safe"


def _after_call(s: str, m: re.Match) -> int:
    i = m.end()
    return _balanced(s, i, "(", ")") if i < len(s) and s[i] == "(" else i


def _definitions(text: str):
    lines = text.splitlines()
    for n, line in enumerate(lines):
        m = DEFN.match(line)
        if not m:
            continue
        val, ind = [m["val"]], len(m["ind"])
        for nxt in lines[n + 1:]:
            if nxt.strip() and len(nxt) - len(nxt.lstrip()) <= ind:
                break
            val.append(nxt)
        # A key whose own value holds the expression, not a parent mapping of further keys.
        if not any(DEFN.match(v) for v in val[1:]):
            yield m["name"], "\n".join(val)


def _scopes(text: str) -> list[str]:
    items = re.split(r"(?m)^(?=- )", text)
    if any(re.search(r"(?m)^(?:- |  )hosts:", it) for it in items):
        return items
    return [text]


def offenders_in(text: str) -> list[str]:
    found = []
    for scope in _scopes(text):
        for m in SET_OP.finditer(scope):
            if walk(scope, _after_call(scope, m)) == "offender":
                line = scope[:m.start()].rsplit("\n", 1)[-1] + scope[m.start():].split("\n", 1)[0]
                found.append("inline: " + " ".join(line.split())[:120])
        for name, val in _definitions(scope):
            if not any(walk(val, _after_call(val, m)) == "end" for m in SET_OP.finditer(val)):
                continue
            for use in re.finditer(r"(?<![\w.])" + re.escape(name) + r"\b(?!\s*:)", scope):
                if walk(scope, use.end()) == "offender":
                    found.append("via variable: " + name)
    return sorted(set(found))


def _files():
    for p in sorted((REPO / "platform").rglob("*")):
        rel = p.relative_to(REPO).parts
        if p.suffix in (".yml", ".yaml") and p.is_file() and "tests" not in rel:
            yield p


def test_no_end_element_of_a_set_operation_outside_the_ratchet():
    hits = {}
    for p in _files():
        found = offenders_in(p.read_text(errors="replace"))
        if found:
            hits[str(p.relative_to(REPO))] = found
    assert set(hits) <= set(RATCHET), {k: v for k, v in hits.items() if k not in RATCHET}
    stale = set(RATCHET) - set(hits)
    assert not stale, f"ratchet entries no longer offend, remove them: {sorted(stale)}"


def _var(expr_def: str, use: str) -> str:
    return f"- hosts: all\n  vars:\n    _m: >-\n      {{{{ {expr_def} }}}}\n    _pick: \"{{{{ {use} }}}}\"\n"


CAUGHT = [
    "x: \"{{ a | intersect(b) | first }}\"\n",
    "x: \"{{ (a | difference(b))[0] }}\"\n",
    "x: \"{{ a | union(b) | last }}\"\n",
    "x: \"{{ (a | symmetric_difference(b))[-1] }}\"\n",
    "x: \"{{ a | intersect(b) | list | first }}\"\n",
    "x: \"{{ a | intersect(b) | map('lower') | unique | first }}\"\n",
    _var("a\n       | union(b)", "_m | first | default('')"),
    _var("a | union(b)", "_m | list | first"),
    _var("a | union(b)", "_m | map(attribute='n') | first"),
    _var("a | union(b)", "_m | last"),
    _var("a | union(b)", "_m[-1]"),
    _var("a | union(b) | list", "_m[0]"),
]
ALLOWED = [
    "x: \"{{ a | intersect(b) | sort | first }}\"\n",
    "x: \"{{ a | intersect(b) | sort(attribute='name') | map(attribute='name') | first }}\"\n",
    "x: \"{{ a | intersect(b) | length }}\"\n",
    "x: \"{{ a | select('in', b) | list | first }}\"\n",
    _var("a | union(b) | sort", "_m | first"),
    _var("a | union(b)", "_m | sort | first"),
    _var("a | union(b)", "_m | length"),
    # Same variable name in another play: the second play's _m is ordered.
    _var("a | union(b)", "_m | length") + _var("a | select('in', b) | list", "_m | first"),
]


@pytest.mark.parametrize("text", CAUGHT)
def test_the_scanner_catches(text):
    assert offenders_in(text), text


@pytest.mark.parametrize("text", ALLOWED)
def test_the_scanner_allows(text):
    assert offenders_in(text) == [], text
