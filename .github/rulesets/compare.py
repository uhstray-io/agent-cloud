#!/usr/bin/env python3
"""Compare a live GitHub repository ruleset with its checked-in JSON.

Read-only. Used by check-drift.sh (and the scheduled ruleset-drift workflow) to
catch config-as-code drift: docs/MISTAKES.md 10.21 records protect-main.json
declaring `active` while the live ruleset sat in `evaluate`, leaving `main`
unprotected with nothing reading the live object back.

Compared fields are exactly the body parameters of "Update a repository ruleset"
(https://docs.github.com/en/rest/repos/rules#update-a-repository-ruleset):
name, target, enforcement, bypass_actors, conditions, rules. Every other key of
the live object (id, node_id, source, source_type, created_at, updated_at,
_links, current_user_can_bypass, ...) is read-only response data and ignored.

Inside those fields the comparison is full in both directions: a key the JSON
declares must match, and a key only the live object carries is drift unless its
value is an empty default (false, 0, "", null, [], {}), which is what the API
echoes for parameters nobody set. So a live-only non-empty
pull_request.required_reviewers is drift. Lists are order-insensitive. Rules
pair by type; a rule on one side only is drift, and a type appearing twice on
either side is reported as drift rather than collapsed.

bypass_actors is returned by the API only to a caller with write access to the
ruleset. When the live object has no bypass_actors key, that field is reported
as not visible (a warning) instead of compared.

Usage: compare.py DECLARED.json LIVE.json   (exit 0 match, 1 drift, 2 usage)
"""

from __future__ import annotations

import json
import sys

# Body parameters of "Update a repository ruleset" (URL in the module docstring);
# "rules" is compared separately below.
TOP_FIELDS = ("name", "target", "enforcement", "conditions", "bypass_actors")
EMPTY_DEFAULTS = (False, 0, "", None, [], {})

# Live-only rule parameters that are NOT body parameters of "Update a repository
# ruleset" (checked against the docs 2026-10-03), so no JSON can set them through
# apply.sh. Reported as warnings, never as a match nor as drift. Seen live on
# protect-main: pull_request.require_extra_approval_for_unattributed_changes.
UNDOCUMENTED_LIVE_PARAMS = frozenset({"require_extra_approval_for_unattributed_changes"})


def _is_empty_default(value) -> bool:
    """An unset value as the API echoes it, including nested objects of them."""
    if isinstance(value, dict):
        return all(_is_empty_default(v) for v in value.values())
    return any(value == d and type(value) is type(d) for d in EMPTY_DEFAULTS)


def _canon(value):
    """Sort lists (by their JSON form) so order never counts as drift."""
    if isinstance(value, dict):
        return {k: _canon(v) for k, v in value.items()}
    if isinstance(value, list):
        items = [_canon(v) for v in value]
        return sorted(items, key=lambda v: json.dumps(v, sort_keys=True))
    return value


def _subset_diff(path, declared, live, out, warnings=None):
    """Record where `live` does not carry every declared key/value."""
    if isinstance(declared, dict) and isinstance(live, dict):
        for key, dval in declared.items():
            if key not in live:
                out.append(f"{path}.{key}: declared {json.dumps(dval)}, live missing")
            else:
                _subset_diff(f"{path}.{key}", dval, live[key], out, warnings)
        for key in sorted(live.keys() - declared.keys()):
            lval = live[key]
            if key in UNDOCUMENTED_LIVE_PARAMS:
                if warnings is not None:
                    warnings.append(
                        f"{path}.{key}: live {json.dumps(lval)}, not settable via the documented API; not compared"
                    )
            elif not _is_empty_default(lval):
                out.append(f"{path}.{key}: not declared, live {json.dumps(lval, sort_keys=True)}")
        return
    if _canon(declared) != _canon(live):
        out.append(
            f"{path}: declared {json.dumps(_canon(declared), sort_keys=True)}, "
            f"live {json.dumps(_canon(live), sort_keys=True)}"
        )


def _rules_by_type(rules, side, diffs):
    out = {}
    for rule in rules or []:
        rtype = rule["type"]
        if rtype in out:
            diffs.append(f"rules[{rtype}]: appears more than once in {side}")
        out[rtype] = rule.get("parameters", {})
    return out


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
        _subset_diff(field, declared[field], live[field], diffs, warnings)

    want = _rules_by_type(declared.get("rules"), "declared", diffs)
    have = _rules_by_type(live.get("rules"), "live", diffs)
    for rtype in sorted(want.keys() - have.keys()):
        diffs.append(f"rules[{rtype}]: declared, missing from live")
    for rtype in sorted(have.keys() - want.keys()):
        diffs.append(f"rules[{rtype}]: present live, not declared")
    for rtype in sorted(want.keys() & have.keys()):
        _subset_diff(f"rules[{rtype}].parameters", want[rtype], have[rtype], diffs, warnings)
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
