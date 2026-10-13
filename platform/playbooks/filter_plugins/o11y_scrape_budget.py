"""Pure, sanitized compatibility preview for observed scrape budgets."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

import yaml

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
MAX_CONFIG_BYTES = 16 * 1024 * 1024
MAX_CONFIG_NESTING = 64

LOADED_LIMIT_FIELDS = {
    "body_size_limit": ("body_size_limit_bytes", 167_772_160),
    "sample_limit": ("sample_limit", 100_000),
    "label_limit": ("label_limit", 640),
    "label_name_length_limit": ("label_name_length_limit", 1_280),
    "label_value_length_limit": ("label_value_length_limit", 20_480),
}
PUBLIC_JOB_ALIASES = {
    "prometheus": "prometheus",
    "receiver-host": "receiver-host",
    "grafana": "grafana",
    "loki": "loki",
    "alloy": "alloy",
    "tempo": "tempo",
    "pyroscope": "pyroscope",
    "agentgateway": "agentgateway",
    "dgx-spark": "dgx-spark",
    "dgx-spark-node": "dgx-spark",
    "dgx-spark-gpu": "dgx-spark",
    "dgx-spark-vllm": "dgx-spark",
    "host-node": "host-node",
}
HOST_JOB = re.compile(r"host-node-[a-z0-9][a-z0-9_-]{0,31}\Z")
BYTE_MULTIPLIERS = {
    "B": 1,
    "KiB": 1024,
    "MiB": 1024**2,
    "GiB": 1024**3,
    "TiB": 1024**4,
    "PiB": 1024**5,
    "EiB": 1024**6,
}
BYTE_UNITS = tuple(BYTE_MULTIPLIERS)
BYTE_SEGMENT = re.compile(r"([1-9][0-9]{0,3})(EiB|PiB|TiB|GiB|MiB|KiB|B)")
MAX_BYTE_SIZE_TEXT = len(BYTE_UNITS) * 7
MAX_BASE2_BYTES = (1 << 63) - 1
READBACK_REASONS = frozenset(
    {
        "buildinfo_unavailable",
        "runtime_config_unavailable",
        "loaded_config_unavailable",
        "prometheus_version_mismatch",
        "unstable_loaded_config",
        "ambiguous_target_pool",
        "malformed_targets",
        "loaded_readback_unavailable",
    }
)


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe loader that refuses aliases, deep nesting, and duplicate keys."""

    def __init__(self, stream: Any) -> None:
        super().__init__(stream)
        self._collection_depth = 0

    def compose_node(self, parent: Any, index: Any) -> yaml.Node:
        if self.check_event(yaml.AliasEvent):
            raise yaml.YAMLError("YAML aliases are not accepted")
        if not self.check_event(yaml.MappingStartEvent, yaml.SequenceStartEvent):
            return super().compose_node(parent, index)

        self._collection_depth += 1
        try:
            if self._collection_depth > MAX_CONFIG_NESTING:
                raise yaml.YAMLError("YAML nesting limit exceeded")
            return super().compose_node(parent, index)
        finally:
            self._collection_depth -= 1


def _unique_yaml_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ValueError("duplicate key")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_yaml_mapping)


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


def _loaded_unavailable(reason: str) -> dict[str, Any]:
    return {"status": "unavailable", "reason": reason, "jobs": []}


def _public_job(name: Any) -> str | None:
    if not isinstance(name, str):
        return None
    if name in PUBLIC_JOB_ALIASES:
        return PUBLIC_JOB_ALIASES[name]
    return "host-node" if HOST_JOB.fullmatch(name) else None


def _loaded_integer(value: Any, maximum: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        return None
    return value


def _loaded_body_bytes(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value if value > 0 else None
    if not isinstance(value, str) or not value or len(value) > MAX_BYTE_SIZE_TEXT:
        return None

    size = 0
    position = 0
    previous_rank = len(BYTE_UNITS)
    while position < len(value):
        match = BYTE_SEGMENT.match(value, position)
        if match is None:
            return None
        amount = int(match.group(1))
        unit = match.group(2)
        rank = BYTE_UNITS.index(unit)
        if amount >= 1024 or rank >= previous_rank:
            return None
        size += amount * BYTE_MULTIPLIERS[unit]
        if size > MAX_BASE2_BYTES:
            return None
        previous_rank = rank
        position = match.end()
    return size if size > 0 else None


def _parse_loaded_yaml(config_yaml: Any) -> tuple[dict[str, Any], bool] | None:
    if not isinstance(config_yaml, str) or not config_yaml or len(config_yaml.encode("utf-8")) > MAX_CONFIG_BYTES:
        return None
    try:
        documents = list(yaml.load_all(config_yaml, Loader=_UniqueKeyLoader))
    except (yaml.YAMLError, TypeError, ValueError, UnicodeError):
        return None
    if len(documents) != 1 or not isinstance(documents[0], Mapping):
        return None
    config = documents[0]
    jobs = config.get("scrape_configs")
    if not isinstance(jobs, list) or not jobs:
        return None
    split_files = config.get("scrape_config_files", [])
    if not isinstance(split_files, list) or any(not isinstance(path, str) or not path for path in split_files):
        return None
    return dict(config), bool(split_files)


def _o11y_loaded_scrape_limit_coverage(
    readback: Any,
    baseline: Any,
    limits: Any,
    limits_reviewed: Any = False,
    sample_cap: Any = 2_000,
) -> dict[str, Any]:
    """Compare protected loaded inline limits, emitting only fixed public categories."""
    if limits_reviewed is not True or not _valid_candidates(limits, sample_cap):
        return _loaded_unavailable("candidate_limits_unavailable")
    if not isinstance(readback, Mapping) or readback.get("status") != "observed":
        if (
            isinstance(readback, Mapping)
            and readback.get("status") == "unavailable"
            and readback.get("reason") in READBACK_REASONS
        ):
            return _loaded_unavailable(readback["reason"])
        return _loaded_unavailable("loaded_readback_unavailable")
    if set(readback) != {"status", "reason", "config_yaml", "target_pools"}:
        return _loaded_unavailable("loaded_readback_unavailable")
    if readback.get("reason") != "stable_loaded_config":
        return _loaded_unavailable("loaded_readback_unavailable")

    parsed = _parse_loaded_yaml(readback.get("config_yaml"))
    if parsed is None:
        return _loaded_unavailable("malformed_loaded_config")
    config, split_files = parsed
    inline_jobs = config["scrape_configs"]
    pools = readback.get("target_pools")
    if not isinstance(pools, list) or len(pools) > MAX_ACTIVE_TARGETS:
        return _loaded_unavailable("ambiguous_target_pool")

    active_pools: dict[str, str] = {}
    for pool in pools:
        if not isinstance(pool, Mapping) or set(pool) != {"pool", "job"}:
            return _loaded_unavailable("ambiguous_target_pool")
        pool_name, job_name = pool["pool"], pool["job"]
        if (
            not isinstance(pool_name, str)
            or not pool_name
            or len(pool_name) > 128
            or not isinstance(job_name, str)
            or not job_name
            or len(job_name) > 128
            or pool_name in active_pools
            or _public_job(pool_name) is None
            or _public_job(job_name) is None
            or _public_job(pool_name) != _public_job(job_name)
        ):
            return _loaded_unavailable("ambiguous_target_pool")
        active_pools[pool_name] = job_name

    config_jobs: dict[str, tuple[str, dict[str, int]]] = {}
    category_results: dict[str, list[str]] = {}
    try:
        for item in inline_jobs:
            if not isinstance(item, Mapping):
                return _loaded_unavailable("malformed_loaded_config")
            name = item.get("job_name")
            category = _public_job(name)
            if category is None or name in config_jobs:
                return _loaded_unavailable("malformed_loaded_config")
            if not isinstance(name, str) or len(name) > 128:
                return _loaded_unavailable("malformed_loaded_config")
            values: dict[str, int] = {}
            for config_key, (candidate_key, maximum) in LOADED_LIMIT_FIELDS.items():
                raw_value = item.get(config_key)
                if config_key == "body_size_limit":
                    loaded = _loaded_body_bytes(raw_value)
                else:
                    loaded = _loaded_integer(raw_value, maximum)
                if loaded is None or loaded > maximum:
                    return _loaded_unavailable("invalid_loaded_limit")
                expected = limits[candidate_key]
                if category == "receiver-host" and config_key == "sample_limit":
                    expected = min(expected, sample_cap)
                if loaded != expected:
                    category_results.setdefault(category, []).append("mismatch")
                else:
                    category_results.setdefault(category, []).append("matched")
                values[config_key] = loaded
            config_jobs[name] = (category, values)
    except (TypeError, ValueError, OverflowError):
        return _loaded_unavailable("malformed_loaded_config")

    unmatched_categories: set[str] = set()
    for pool_name, job_name in active_pools.items():
        if pool_name not in config_jobs:
            category = _public_job(pool_name)
            if not split_files or category is None:
                return _loaded_unavailable("ambiguous_target_pool")
            unmatched_categories.add(category)
        elif _public_job(job_name) != config_jobs[pool_name][0]:
            return _loaded_unavailable("ambiguous_target_pool")

    if not isinstance(baseline, Mapping) or baseline.get("status") != "observed":
        return _loaded_unavailable("loaded_readback_unavailable")
    baseline_jobs = baseline.get("jobs")
    if not isinstance(baseline_jobs, list):
        return _loaded_unavailable("loaded_readback_unavailable")
    for item in baseline_jobs:
        if not isinstance(item, Mapping) or _public_job(item.get("job")) is None:
            return _loaded_unavailable("loaded_readback_unavailable")
        category = _public_job(item["job"])
        if category not in {entry[0] for entry in config_jobs.values()}:
            if split_files:
                unmatched_categories.add(category)
            else:
                return _loaded_unavailable("ambiguous_target_pool")

    categories = set(category_results) | unmatched_categories
    results = []
    for category in sorted(categories):
        if "mismatch" in category_results.get(category, []):
            results.append({"job": category, "status": "mismatch", "reason": "loaded_limit_mismatch"})
        elif category in unmatched_categories:
            results.append({"job": category, "status": "unverified", "reason": "split_scrape_file_job"})
        else:
            results.append({"job": category, "status": "matched", "reason": "limits_match_reviewed_inventory"})

    if any(item["status"] == "mismatch" for item in results):
        status, reason = "mismatch", "loaded_limit_mismatch"
    elif split_files or unmatched_categories:
        status, reason = "unverified", "split_scrape_file_jobs_unverified"
    else:
        status, reason = "matched", "inline_limits_match_reviewed_inventory"
    return {"status": status, "reason": reason, "jobs": results}


def o11y_loaded_scrape_limit_coverage(
    readback: Any,
    baseline: Any,
    limits: Any,
    limits_reviewed: Any = False,
    sample_cap: Any = 2_000,
) -> dict[str, Any]:
    """Fail closed to a fixed result instead of raising on protected API data."""
    try:
        return _o11y_loaded_scrape_limit_coverage(readback, baseline, limits, limits_reviewed, sample_cap)
    except Exception:
        return _loaded_unavailable("loaded_readback_unavailable")


class FilterModule:
    """Expose the pure preview as an Ansible filter."""

    def filters(self) -> dict[str, Any]:
        return {
            "o11y_scrape_budget_preview": o11y_scrape_budget_preview,
            "o11y_loaded_scrape_limit_coverage": o11y_loaded_scrape_limit_coverage,
        }
