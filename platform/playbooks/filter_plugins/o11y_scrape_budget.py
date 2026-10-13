"""Pure, sanitized compatibility preview for observed scrape budgets."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

PUBLIC_JOBS = frozenset(
    {
        "agentgateway",
        "alloy",
        "dgx-spark",
        "grafana",
        "host-node",
        "loki",
        "prometheus",
        "pyroscope",
        "receiver-host",
        "tempo",
    }
)

OBSERVED_FIELDS = {
    "max_scrape_samples_scraped_24h": 99_999_999_999_999_999_999,
    "max_scrape_response_body_bytes_point_in_time": 167_772_160,
    "max_stored_series_label_count_24h": 16_777_216,
    "max_stored_series_label_name_bytes_24h": 16_777_216,
    "max_stored_series_label_value_bytes_24h": 16_777_216,
}

CANDIDATE_FIELDS = {
    "body_size_limit_bytes": 167_772_160,
    "sample_limit": 100_000,
    "label_limit": 640,
    "label_name_length_limit": 1_280,
    "label_value_length_limit": 20_480,
}

MAX_ACTIVE_TARGETS = 10_000
MAX_HOST_SAMPLE_LIMIT = 20_000


def _integer(value: Any, maximum: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= maximum


def _unavailable(reason: str) -> dict[str, Any]:
    return {"status": "unavailable", "reason": reason, "jobs": []}


def _valid_receipt(receipt: Any) -> bool:
    if not isinstance(receipt, Mapping) or set(receipt) != {"status", "active_target_count", "jobs"}:
        return False
    if receipt["status"] != "observed" or not _integer(receipt["active_target_count"], MAX_ACTIVE_TARGETS):
        return False

    jobs = receipt["jobs"]
    if not isinstance(jobs, list) or not 1 <= len(jobs) <= len(PUBLIC_JOBS):
        return False

    seen: set[str] = set()
    target_total = 0
    for item in jobs:
        required = {"job", "active_target_count", "health", *OBSERVED_FIELDS}
        if not isinstance(item, Mapping) or set(item) != required:
            return False
        job = item["job"]
        if not isinstance(job, str) or job not in PUBLIC_JOBS or job in seen:
            return False
        seen.add(job)

        count = item["active_target_count"]
        if not _integer(count, MAX_ACTIVE_TARGETS):
            return False
        target_total += count

        health = item["health"]
        if (
            not isinstance(health, Mapping)
            or set(health) != {"healthy", "unhealthy"}
            or not isinstance(health["healthy"], int)
            or isinstance(health["healthy"], bool)
            or not isinstance(health["unhealthy"], int)
            or isinstance(health["unhealthy"], bool)
            or health["healthy"] != count
            or health["unhealthy"] != 0
        ):
            return False

        if any(not _integer(item[field], maximum) for field, maximum in OBSERVED_FIELDS.items()):
            return False

    return target_total == receipt["active_target_count"]


def _valid_candidates(limits: Any, sample_cap: Any) -> bool:
    if not isinstance(limits, Mapping) or set(limits) != set(CANDIDATE_FIELDS):
        return False
    if any(not _integer(limits[field], maximum) for field, maximum in CANDIDATE_FIELDS.items()):
        return False
    return _integer(sample_cap, MAX_HOST_SAMPLE_LIMIT)


def o11y_scrape_budget_preview(
    receipt: Any,
    limits: Any,
    limits_reviewed: Any = False,
    sample_cap: Any = 2_000,
) -> dict[str, Any]:
    """Compare a public baseline with declared candidates without returning values."""
    if limits_reviewed is not True:
        return _unavailable("candidate_limits_unavailable")
    if limits is None or limits == {}:
        return _unavailable("candidate_limits_unavailable")
    if not _valid_receipt(receipt):
        return _unavailable("invalid_baseline_receipt")
    if not _valid_candidates(limits, sample_cap):
        return _unavailable("invalid_candidate_limits")

    results = []
    for item in receipt["jobs"]:
        sample_limit = limits["sample_limit"]
        if item["job"] in {"receiver-host", "host-node"}:
            sample_limit = min(sample_limit, sample_cap)

        comparisons = (
            (item["max_scrape_samples_scraped_24h"], sample_limit),
            (
                item["max_scrape_response_body_bytes_point_in_time"],
                limits["body_size_limit_bytes"],
            ),
            (item["max_stored_series_label_count_24h"], limits["label_limit"]),
            (
                item["max_stored_series_label_name_bytes_24h"],
                limits["label_name_length_limit"],
            ),
            (
                item["max_stored_series_label_value_bytes_24h"],
                limits["label_value_length_limit"],
            ),
        )
        compatible = all(observed < candidate for observed, candidate in comparisons)
        results.append(
            {
                "job": item["job"],
                "status": "compatible" if compatible else "incompatible",
            }
        )

    compatible = all(item["status"] == "compatible" for item in results)
    return {
        "status": "compatible" if compatible else "incompatible",
        "reason": "strict_headroom_available" if compatible else "strict_headroom_missing",
        "jobs": results,
    }


class FilterModule:
    """Expose the pure preview as an Ansible filter."""

    def filters(self) -> dict[str, Any]:
        return {"o11y_scrape_budget_preview": o11y_scrape_budget_preview}
