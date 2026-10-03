#!/usr/bin/env python3
"""Compare a live GitHub repository ruleset with its checked-in JSON.

Read-only. Used by check-drift.sh (and the scheduled ruleset-drift workflow) to
catch config-as-code drift: docs/MISTAKES.md 10.21 records protect-main.json
declaring `active` while the live ruleset sat in `evaluate`, leaving `main`
unprotected with nothing reading the live object back.

Compared fields: name, target, enforcement, conditions, bypass_actors, and the
rules keyed by type with their parameters. For a declared object, every declared
key must match; keys the API adds on its own (defaults such as extra
pull_request parameters, ids, links, timestamps) are ignored. Lists are compared
order-insensitively. A rule present on one side only is a difference.

bypass_actors is returned by the API only to a caller with write access to the
ruleset. When the live object has no bypass_actors key, that field is reported
as not visible (a warning) instead of compared.

Usage: compare.py DECLARED.json LIVE.json   (exit 0 match, 1 drift, 2 usage)
"""

from __future__ import annotations

import json
import sys

TOP_FIELDS = ("name", "target", "enforcement", "conditions", "bypass_actors")


def _canon(value):
    """Sort lists (by their JSON form) so order never counts as drift."""
    if isinstance(value, dict):
        return {k: _canon(v) for k, v in value.items()}
    if isinstance(value, list):
        items = [_canon(v) for v in value]
        return sorted(items, key=lambda v: json.dumps(v, sort_keys=True))
    return value


def _subset_diff(path, declared, live, out):
    """Record where `live` does not carry every declared key/value."""
    if isinstance(declared, dict) and isinstance(live, dict):
        for key, dval in declared.items():
            if key not in live:
                out.append(f"{path}.{key}: declared {json.dumps(dval)}, live missing")
            else:
                _subset_diff(f"{path}.{key}", dval, live[key], out)
        return
    if _canon(declared) != _canon(live):
        out.append(
            f"{path}: declared {json.dumps(_canon(declared), sort_keys=True)}, "
            f"live {json.dumps(_canon(live), sort_keys=True)}"
        )


def _rules_by_type(rules):
    return {r["type"]: r.get("parameters", {}) for r in rules or []}


def compare(declared: dict, live: dict) -> tuple[list[str], list[str]]:
    """Return (differences, warnings) between a declared and a live ruleset."""
    diffs: list[str] = []
    warnings: list[str] = []
    for field in TOP_FIELDS:
        if field not in declared:
            continue
        if field == "bypass_actors" and field not in live:
            warnings.append(
                "bypass_actors: not returned to this token (needs write access to the ruleset); not compared"
            )
            continue
        if field not in live:
            diffs.append(f"{field}: declared {json.dumps(declared[field])}, live missing")
            continue
        _subset_diff(field, declared[field], live[field], diffs)

    want = _rules_by_type(declared.get("rules"))
    have = _rules_by_type(live.get("rules"))
    for rtype in sorted(want.keys() - have.keys()):
        diffs.append(f"rules[{rtype}]: declared, missing from live")
    for rtype in sorted(have.keys() - want.keys()):
        diffs.append(f"rules[{rtype}]: present live, not declared")
    for rtype in sorted(want.keys() & have.keys()):
        _subset_diff(f"rules[{rtype}].parameters", want[rtype], have[rtype], diffs)
    return diffs, warnings


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    with open(argv[1], encoding="utf-8") as fh:
        declared = json.load(fh)
    with open(argv[2], encoding="utf-8") as fh:
        live = json.load(fh)
    diffs, warnings = compare(declared, live)
    name = declared.get("name", argv[1])
    for w in warnings:
        print(f"WARNING [{name}] {w}")
    for d in diffs:
        print(f"DRIFT [{name}] {d}")
    if diffs:
        return 1
    print(f"OK [{name}] live ruleset matches {argv[1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
