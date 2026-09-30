#!/usr/bin/env python3
"""Classify a bounded Grafana OAuth log tail without returning raw log data."""

from __future__ import annotations

import re
import subprocess

OAUTH_CONTEXT = re.compile(
    r"(?:\blogger\s*=\s*[\"']?(?:auth|oauth)\.generic_oauth\b|"
    r"\bclient\s*=\s*[\"']?auth\.client\.generic_oauth\b|"
    r"\[auth\.oauth\.[a-z0-9_.-]+\]|"
    r"[\"'](?:logger|client)[\"']\s*:\s*[\"'](?:auth|oauth)\.generic_oauth\b|"
    r"[\"']client[\"']\s*:\s*[\"']auth\.client\.generic_oauth\b)",
    re.IGNORECASE,
)
ERROR_LEVEL = re.compile(
    r"(?:\blevel\s*=\s*[\"']?(?:error|warn)\b|"
    r"[\"']level[\"']\s*:\s*[\"'](?:error|warn)\b)",
    re.IGNORECASE,
)
TOKEN_FAILURE = re.compile(
    r"(?:\[auth\.oauth\.token\.exchange\]|"
    r"failed\s+to\s+exchange\s+(?:the\s+)?(?:authorization\s+)?code\s+to\s+token|"
    r"failed\s+to\s+get\s+token\s+from\s+provider|cannot\s+fetch\s+token)",
    re.IGNORECASE,
)
USERINFO_FAILURE = re.compile(
    r"(?:\[auth\.oauth\.userinfo\.[a-z0-9_.-]+\]|"
    r"failed\s+to\s+get\s+user\s+info|error\s+getting\s+(?:user\s+)?(?:info|email)|"
    r"required\s+attribute\s+email\s+was\s+not\s+provided)",
    re.IGNORECASE,
)
GROUP_FAILURE = re.compile(
    r"(?:user\s+(?:is\s+)?not\s+(?:a\s+)?member\s+of\s+(?:any\s+)?(?:allowed\s+|required\s+)?groups?|"
    r"(?:no|missing|empty)\s+(?:allowed\s+|required\s+)?groups?\s+(?:claim|found|provided)|"
    r"groups?\s+(?:claim|attribute)\s+(?:is\s+)?(?:missing|empty|not\s+found|rejected))",
    re.IGNORECASE,
)
AUTH_FAILURE = re.compile(
    r"(?:failed\s+to\s+authenticate\s+request|\[auth\.oauth\.[a-z0-9_.-]+\])",
    re.IGNORECASE,
)


def classify_lines(lines: list[str]) -> str:
    """Return the newest recognized OAuth failure as one fixed category."""
    for line in reversed(lines):
        if not OAUTH_CONTEXT.search(line) or not ERROR_LEVEL.search(line):
            continue
        if GROUP_FAILURE.search(line):
            return "group_claim_rejected"
        if USERINFO_FAILURE.search(line):
            return "userinfo_rejected"
        if TOKEN_FAILURE.search(line):
            return "token_exchange_failed"
        if AUTH_FAILURE.search(line):
            return "oauth_failure_unclassified"
    return "no_oauth_failure_in_window"


def diagnose() -> str:
    """Read recent container logs privately and return an allowlisted label."""
    try:
        result = subprocess.run(
            ["podman", "logs", "--since", "15m", "--tail", "500", "o11y-grafana"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
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
        return classify_lines(result.stdout.splitlines())
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
