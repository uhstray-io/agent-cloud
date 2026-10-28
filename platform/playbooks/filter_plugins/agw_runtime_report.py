"""Decode the agentgateway runtime adapter's closed, safe stdout protocol."""

import json

REASONS = {
    "matched",
    "gateway-state-unavailable",
    "gateway-not-running",
    "inputs-not-matched",
    "project-enumeration-unavailable",
    "image-not-matched",
    "readiness-unproven",
    "diagnostic-unavailable",
}
ERRORS = {
    "none",
    "setup-failed",
    "invocation-failed",
    "timeout",
    "output-limit",
    "capture-failed",
    "cleanup-failed",
    "invalid-output",
}
FIELDS = {"error", "pass_marker", "rc", "reason", "result"}
MAX_REPORT_CHARS = 4096


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def agw_runtime_report(raw):
    """Return a validated report, or fixed refusal metadata for unusable stdout."""
    try:
        if not isinstance(raw, str) or len(raw) > MAX_REPORT_CHARS:
            raise ValueError
        report = json.loads(raw, object_pairs_hook=_unique_object)
        if not isinstance(report, dict) or set(report) != FIELDS:
            raise ValueError
        if report["rc"] is not None and (
            not isinstance(report["rc"], int) or isinstance(report["rc"], bool)
        ):
            raise ValueError
        if report["result"] not in {"pass", "refused", "unavailable"}:
            raise ValueError
        if report["reason"] not in REASONS or report["error"] not in ERRORS:
            raise ValueError
        if not isinstance(report["pass_marker"], bool):
            raise ValueError
    except (RecursionError, TypeError, ValueError, json.JSONDecodeError):
        return {
            "rc": None,
            "result": "unavailable",
            "reason": "diagnostic-unavailable",
            "pass_marker": False,
            "error": "invalid-output",
        }
    return report


class FilterModule:
    def filters(self):
        return {"agw_runtime_report": agw_runtime_report}
