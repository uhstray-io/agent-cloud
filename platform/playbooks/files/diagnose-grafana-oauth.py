#!/usr/bin/env python3
"""Classify a bounded Grafana OAuth log tail without returning raw log data."""

from __future__ import annotations

import re
import subprocess

FAILURE = re.compile(
    r"\b(?:fail(?:ed|ure)?|error|invalid|denied|reject(?:ed|ion)?|"
    r"unauthori[sz]ed|forbidden|unable|cannot|could not|missing|"
    r"no\s+groups|not\s+(?:a\s+)?member)\b",
    re.IGNORECASE,
)
OAUTH_CONTEXT = ("oauth", "generic_oauth", "oauth2")
GROUP_CONTEXT = ("group", "groups", "claim")
USERINFO_CONTEXT = ("userinfo", "user info", "user_info")
TOKEN_CONTEXT = ("token", "exchange", "authorization code")


def classify_lines(lines: list[str]) -> str:
    """Return the newest recognized OAuth failure as one fixed category."""
    for line in reversed(lines):
        normalized = line.lower()
        group_failure = any(term in normalized for term in GROUP_CONTEXT)
        userinfo_failure = any(term in normalized for term in USERINFO_CONTEXT)
        token_failure = any(term in normalized for term in TOKEN_CONTEXT)
        oauth_failure = any(term in normalized for term in OAUTH_CONTEXT)
        if not FAILURE.search(normalized) or not (
            group_failure or userinfo_failure or token_failure or oauth_failure
        ):
            continue
        if group_failure:
            return "group_claim_rejected"
        if userinfo_failure:
            return "userinfo_rejected"
        if token_failure:
            return "token_exchange_failed"
        return "oauth_failure_unclassified"
    return "no_matching_failure"


def diagnose() -> str:
    """Read recent container logs privately and return an allowlisted label."""
    try:
        result = subprocess.run(
            ["podman", "logs", "--since", "15m", "--tail", "500", "o11y-grafana"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return "diagnostic_unavailable"
    if result.returncode != 0:
        return "diagnostic_unavailable"
    try:
        captured_lines = result.stdout.splitlines() + result.stderr.splitlines()
        return classify_lines(captured_lines)
    except Exception:
        return "diagnostic_unavailable"


def main() -> None:
    try:
        category = diagnose()
    except Exception:
        category = "diagnostic_unavailable"
    print(category)


if __name__ == "__main__":
    main()
