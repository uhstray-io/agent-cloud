#!/usr/bin/env python3
"""Emit safe diagnostics for o11y sample-limit and Prometheus readbacks."""

from __future__ import annotations

import json
import re
import sys
from typing import Any


INTEGER = re.compile(r"^[0-9]+$")


def _nonnegative_integer(value: Any) -> int | None:
    text = str(value).strip()
    if not INTEGER.fullmatch(text):
        return None
    return int(text)


def diagnose(payload: dict[str, Any]) -> dict[str, Any]:
    expected_sample_limit = _nonnegative_integer(payload.get("expected_sample_limit"))
    observed_sample_limit = _nonnegative_integer(payload.get("sample_limit"))

    try:
        response = json.loads(str(payload.get("head_series", "")))
    except (TypeError, ValueError):
        response = {}

    data = response.get("data") if isinstance(response, dict) else None
    result = data.get("result", []) if isinstance(data, dict) else []
    if not isinstance(result, list):
        result = []

    series_value: int | None = None
    if len(result) == 1 and isinstance(result[0], dict):
        value = result[0].get("value")
        if isinstance(value, list) and len(value) == 2:
            series_value = _nonnegative_integer(value[1])

    return {
        "sample_limit_matches": (
            expected_sample_limit is not None
            and observed_sample_limit is not None
            and observed_sample_limit == expected_sample_limit
        ),
        "sample_limit_expected": expected_sample_limit if expected_sample_limit is not None else "invalid",
        "sample_limit_observed": observed_sample_limit if observed_sample_limit is not None else "invalid",
        "head_series_count_matches": len(result) == 1,
        "head_series_count_observed": len(result),
        "positive_head_series": series_value is not None and series_value > 0,
        "head_series_observed": series_value if series_value is not None else "invalid_or_unavailable",
    }


if __name__ == "__main__":
    try:
        input_data = json.load(sys.stdin)
        diagnostics = diagnose(input_data if isinstance(input_data, dict) else {})
    except (TypeError, ValueError):
        diagnostics = diagnose({})
    print(json.dumps(diagnostics, sort_keys=True))
