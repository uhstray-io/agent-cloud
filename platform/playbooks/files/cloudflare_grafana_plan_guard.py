#!/usr/bin/env python3
"""Fail-closed receipt for the reviewed Grafana Cloudflare ruleset insertion."""

import argparse
import hashlib
import json
import re
import sys

RESOURCE = "cloudflare_ruleset.custom_firewall"
RULE_REF = "o11y-api-bypass-challenge"
RULE_INDEX = 5
EXPECTED_RULE = {
    "ref": RULE_REF,
    "action": "skip",
    "enabled": True,
    "description": "Grafana API - bypass challenge for token-bearing non-browser clients",
    "expression": (
        '(http.host eq "o11y.uhstray.io" and starts_with(http.request.uri.path, "/api/") '
        'and len(http.request.headers["authorization"]) > 0)'
    ),
    "action_parameters": {"phases": ["http_request_sbfm"], "products": ["bic", "securityLevel"]},
    "logging": {"enabled": True},
}
RECEIPT_FIELDS = (
    "safe", "status", "reason", "digest", "resource", "rule_ref", "position", "actions", "digest_matches"
)


class UnsafePlan(ValueError):
    def __init__(self, reason):
        self.reason = reason


def _reject(reason):
    raise UnsafePlan(reason)


def _unknown_paths(value, prefix=""):
    if value is True:
        return [prefix]
    if value in (False, None):
        return []
    if isinstance(value, dict):
        return [p for key, child in value.items() for p in _unknown_paths(child, f"{prefix}.{key}" if prefix else key)]
    if isinstance(value, list):
        return [p for i, child in enumerate(value) for p in _unknown_paths(child, f"{prefix}[{i}]")]
    return [prefix]


def _contract_matches(rule):
    if not isinstance(rule, dict) or any(rule.get(key) != value for key, value in EXPECTED_RULE.items()):
        return False
    # Provider-normalized null/empty fields do not alter this rule. Reject populated
    # unreviewed properties; `id` is the provider-computed identifier, covered below.
    for key, value in rule.items():
        if key not in EXPECTED_RULE and key != "id" and value not in (None, [], {}):
            return False
    return True


def _validate_unknowns(change):
    paths = _unknown_paths(change.get("after_unknown", {}))
    allowed = [f"rules[{RULE_INDEX}].id"] if change.get("actions") == ["update"] else []
    if paths not in ([], allowed):
        _reject("unknown_values")


def _digest(change):
    encoded = json.dumps(change, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _resources(plan):
    if plan.get("errored") is True or plan.get("applyable") is False or plan.get("complete") is False:
        _reject("plan_not_applyable")
    if plan.get("deferred_changes") or plan.get("output_changes"):
        _reject("unreviewed_plan_changes")
    if plan.get("resource_drift"):
        _reject("resource_drift")
    changes = plan.get("resource_changes")
    if not isinstance(changes, list):
        _reject("missing_resource_changes")
    targets = [entry for entry in changes if entry.get("address") == RESOURCE]
    if len(targets) != 1:
        _reject("target_resource_count")
    target = targets[0]
    others = [entry for entry in changes if entry is not target]
    if any(not _resource_is_unchanged(entry) for entry in others):
        _reject("other_resource_changes")
    return target, others


def _resource_is_unchanged(resource_change):
    change = resource_change.get("change", {})
    before, after = change.get("before"), change.get("after")
    return (
        change.get("actions") == ["no-op"]
        and isinstance(before, dict)
        and before == after
        and not _unknown_paths(change.get("after_unknown", {}))
    )


def _target_state(target):
    change = target.get("change")
    if not isinstance(change, dict):
        _reject("missing_target_change")
    _validate_unknowns(change)
    before, after = change.get("before"), change.get("after")
    if not isinstance(before, dict) or not isinstance(after, dict):
        _reject("missing_resource_values")
    if before.keys() != after.keys() or {k: v for k, v in before.items() if k != "rules"} != {
        k: v for k, v in after.items() if k != "rules"
    }:
        _reject("root_field_change")
    before_rules, after_rules = before.get("rules"), after.get("rules")
    if not isinstance(before_rules, list) or not isinstance(after_rules, list):
        _reject("missing_rules")
    return change, before_rules, after_rules


def inspect_plan(plan, action, reviewed_digest=""):
    try:
        if action not in ("plan", "apply", "verify"):
            _reject("invalid_action")
        if not isinstance(plan, dict):
            _reject("invalid_plan")
        target, _ = _resources(plan)
        change, before_rules, after_rules = _target_state(target)
        actions = change.get("actions")
        digest = _digest(target)
        insertion = actions == ["update"]
        noop = actions == ["no-op"]
        if insertion:
            if (len(after_rules) != len(before_rules) + 1
                    or before_rules[:RULE_INDEX] != after_rules[:RULE_INDEX]
                    or before_rules[RULE_INDEX:] != after_rules[RULE_INDEX + 1:]
                    or not _contract_matches(after_rules[RULE_INDEX])
                    or any(rule.get("ref") == RULE_REF for rule in before_rules if isinstance(rule, dict))):
                _reject("not_exact_rule_insertion")
            status = "insert"
        elif noop:
            if (before_rules != after_rules or len(after_rules) <= RULE_INDEX
                    or not _contract_matches(after_rules[RULE_INDEX])
                    or sum(rule.get("ref") == RULE_REF for rule in after_rules if isinstance(rule, dict)) != 1):
                _reject("rule_not_present_at_position")
            status = "present"
        else:
            _reject("target_action_not_update")

        if action == "verify" and (status != "present" or any(
            entry.get("change", {}).get("actions") != ["no-op"] for entry in plan["resource_changes"]
        )):
            _reject("post_apply_not_zero_diff")
        matches = bool(re.fullmatch(r"sha256:[0-9a-f]{64}", reviewed_digest)) and reviewed_digest == digest
        if action == "apply" and (status != "insert" or not matches):
            _reject("reviewed_digest_missing_or_mismatch")
        return _receipt(True, status, "exact_rule_insertion" if status == "insert" else "zero_diff", digest,
                        actions, matches)
    except UnsafePlan as exc:
        reason = exc.reason
        return _receipt(False, "rejected", reason, None, [], False)
    except (AttributeError, KeyError, TypeError):
        # Exception details may contain plan values; only fixed categories leave this boundary.
        return _receipt(False, "rejected", "invalid_plan_structure", None, [], False)


def _receipt(safe, status, reason, digest, actions, matches):
    return dict(zip(RECEIPT_FIELDS, (
        safe, status, reason, digest, RESOURCE, RULE_REF, RULE_INDEX, actions, matches
    ), strict=True))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--action", choices=("plan", "apply", "verify"), required=True)
    parser.add_argument("--reviewed-digest", default="")
    args = parser.parse_args()
    try:
        plan = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError):
        receipt = _receipt(False, "rejected", "invalid_plan_json", None, [], False)
    else:
        receipt = inspect_plan(plan, args.action, args.reviewed_digest)
    print(json.dumps(receipt, separators=(",", ":")))
    return 0 if receipt["safe"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
