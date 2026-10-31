"""Pure, sanitized compatibility preview for observed scrape budgets."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
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
NATIVE_HEALTH_SAMPLE_LIMITS = {
    "scrape_samples_scraped": "sample_limit",
    "scrape_body_size_bytes": "body_size_limit",
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
SOURCE_READBACK_REASONS = frozenset(
    {
        "source_path_refused",
        "source_file_unavailable",
        "source_file_refused",
        "source_yaml_refused",
        "source_job_refused",
        "source_limit_refused",
        "source_readback_unavailable",
    }
)
NATIVE_SOURCE_REFUSAL_REASONS = frozenset({
    "native_declaration_invalid", "native_source_unavailable", "native_source_absent",
    "native_source_ambiguous", "native_source_stale", "native_source_limits_unavailable",
    "native_source_limits_mismatch", "native_samples_unavailable", "native_samples_invalid",
    "native_samples_missing", "native_samples_stale", "native_samples_ambiguous",
    "native_detail_unavailable", "native_detail_invalid", "native_component_absent",
    "native_component_unhealthy", "native_debug_unavailable", "native_debug_invalid",
    "native_debug_stale", "native_debug_mismatch", "native_target_mismatch",
    "native_module_mismatch", "native_limits_mismatch", "namespace_unverified",
})
SOURCE_GATE_REFUSAL_REASONS = READBACK_REASONS | SOURCE_READBACK_REASONS | NATIVE_SOURCE_REFUSAL_REASONS
SOURCE_JOB_CATEGORIES = {
    "agentgateway": "agentgateway",
    "dgx-spark-node": "dgx-spark",
    "dgx-spark-gpu": "dgx-spark",
    "dgx-spark-vllm": "dgx-spark",
}


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
    if re.fullmatch(r"[1-9][0-9]{0,18}B", value):
        size = int(value[:-1])
        return size if size <= MAX_BASE2_BYTES else None

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


def _o11y_source_scrape_limit_coverage(
    readback: Any,
    limits: Any,
    limits_reviewed: Any = False,
) -> dict[str, Any]:
    """Compare protected on-disk split-file values and expose only fixed categories."""
    if limits_reviewed is not True or not _valid_candidates(limits, 2_000):
        return _loaded_unavailable("candidate_limits_unavailable")
    if not isinstance(readback, Mapping):
        return _loaded_unavailable("source_readback_unavailable")
    if readback.get("status") == "unavailable" and readback.get("reason") in SOURCE_READBACK_REASONS:
        return _loaded_unavailable(readback["reason"])
    if (
        set(readback) != {"status", "reason", "jobs"}
        or readback.get("status") != "observed"
        or readback.get("reason") != "source_files_read"
    ):
        return _loaded_unavailable("source_readback_unavailable")
    jobs = readback.get("jobs")
    if not isinstance(jobs, list) or not 2 <= len(jobs) <= 4:
        return _loaded_unavailable("source_readback_unavailable")

    seen: set[str] = set()
    categories: dict[str, list[str]] = {"agentgateway": [], "dgx-spark": []}
    try:
        for item in jobs:
            if not isinstance(item, Mapping) or set(item) != {"job_name", "limits"}:
                return _loaded_unavailable("source_readback_unavailable")
            name = item.get("job_name")
            category = SOURCE_JOB_CATEGORIES.get(name) if isinstance(name, str) else None
            if category is None or name in seen:
                return _loaded_unavailable("source_readback_unavailable")
            seen.add(name)
            values = item.get("limits")
            if not isinstance(values, Mapping) or set(values) != set(LOADED_LIMIT_FIELDS):
                return _loaded_unavailable("source_readback_unavailable")
            comparisons = []
            for config_key, (candidate_key, maximum) in LOADED_LIMIT_FIELDS.items():
                if config_key == "body_size_limit":
                    loaded = _loaded_body_bytes(values[config_key])
                else:
                    loaded = _loaded_integer(values[config_key], maximum)
                if loaded is None or loaded > maximum:
                    return _loaded_unavailable("source_limit_refused")
                comparisons.append(loaded == limits[candidate_key])
            categories[category].append("matched" if all(comparisons) else "mismatch")
    except Exception:
        return _loaded_unavailable("source_readback_unavailable")

    if not {"agentgateway", "dgx-spark-node", "dgx-spark-vllm"}.issubset(seen):
        return _loaded_unavailable("source_readback_unavailable")
    results = [
        {
            "job": category,
            "status": "mismatch" if "mismatch" in categories[category] else "matched",
            "reason": "source_limit_mismatch" if "mismatch" in categories[category]
            else "source_limits_match_reviewed_inventory",
        }
        for category in ("agentgateway", "dgx-spark")
    ]
    mismatch = any(item["status"] == "mismatch" for item in results)
    return {
        "status": "mismatch" if mismatch else "matched",
        "reason": "source_limit_mismatch" if mismatch else "source_limits_match_reviewed_inventory",
        "jobs": results,
    }


def o11y_source_scrape_limit_coverage(
    readback: Any,
    limits: Any,
    limits_reviewed: Any = False,
) -> dict[str, Any]:
    """Fail closed to fixed output for any malformed protected helper result."""
    try:
        return _o11y_source_scrape_limit_coverage(readback, limits, limits_reviewed)
    except Exception:
        return _loaded_unavailable("source_readback_unavailable")


def _valid_availability_limits(value: Any) -> bool:
    return (
        isinstance(value, Mapping)
        and set(value) == set(LOADED_LIMIT_FIELDS)
        and all(_integer(value[field], maximum) for field, (_config_field, maximum) in LOADED_LIMIT_FIELDS.items())
    )


def _valid_native_duration(value: Any) -> bool:
    if (
        not isinstance(value, str) or len(value) > 32
        or not re.fullmatch(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?", value)
    ):
        return False
    try:
        duration = Decimal(value)
    except InvalidOperation:
        return False
    return duration.is_finite() and Decimal(0) <= duration <= Decimal(10)


def o11y_availability_source_coverage(readback: Any, allow_self_only: bool = False) -> dict[str, Any]:
    """Consume the source-specific availability evidence without returning private labels."""
    if (
        isinstance(readback, Mapping) and readback.get("status") == "unavailable"
        and set(readback) == {"status", "reason", "source", "intended_source", "matches_intended"}
        and readback.get("reason") in NATIVE_SOURCE_REFUSAL_REASONS and readback.get("source") == "unknown"
        and readback.get("intended_source") == "unknown" and readback.get("matches_intended") is False
    ):
        return {"status": "unavailable", "reason": readback["reason"], "source": "unknown"}
    if (
        not isinstance(readback, Mapping)
        or set(readback) not in (
            {"status", "source", "intended_source", "matches_intended", "evidence"},
            {"status", "source", "intended_source", "matches_intended", "evidence", "container_identity_sha256"},
        )
        or readback.get("status") != "observed"
        or readback.get("source") not in {"prometheus", "alloy"}
        or readback.get("intended_source") not in {"prometheus", "alloy"}
        or not isinstance(readback.get("matches_intended"), bool)
        or readback["matches_intended"] is not (readback["source"] == readback["intended_source"])
        or (
            "container_identity_sha256" in readback
            and (not isinstance(readback.get("container_identity_sha256"), str)
                 or not re.fullmatch(r"[0-9a-f]{64}", readback["container_identity_sha256"]))
        )
    ):
        return {"status": "unavailable", "reason": "source_readback_unavailable", "source": "unknown"}
    evidence = readback["evidence"]
    if readback["source"] == "prometheus":
        has_native_absence = isinstance(evidence, Mapping) and set(evidence) == {
            "status", "source", "alloy_self", "scrape_body_size_bytes", "native_components",
        }
        native_components = evidence.get("native_components") if has_native_absence else None
        if (
            not isinstance(evidence, Mapping)
            or set(evidence) not in (
                {"status", "source", "alloy_self", "scrape_body_size_bytes"},
                {"status", "source", "alloy_self", "scrape_body_size_bytes", "native_components"},
            )
            or evidence.get("status") != "observed" or evidence.get("source") != "prometheus"
            or not isinstance(evidence.get("alloy_self"), Mapping)
            or set(evidence["alloy_self"]) != {"up", "scrape_samples_scraped"}
            or isinstance(evidence["alloy_self"].get("up"), bool)
            or not isinstance(evidence["alloy_self"].get("up"), int)
            or evidence["alloy_self"]["up"] != 1
            or isinstance(evidence["alloy_self"].get("scrape_samples_scraped"), bool)
            or not isinstance(evidence["alloy_self"].get("scrape_samples_scraped"), int)
            or evidence["alloy_self"]["scrape_samples_scraped"] < 1
            or evidence.get("scrape_body_size_bytes") != {
                "status": "unavailable", "reason": "legacy_extra_scrape_metrics_not_enabled",
            }
            or (has_native_absence and (
                not isinstance(native_components, Mapping)
                or set(native_components) != {"status", "reason", "component_count"}
                or native_components.get("status") != "observed"
                or native_components.get("reason") != "native_components_absent"
                or isinstance(native_components.get("component_count"), bool)
                or not isinstance(native_components.get("component_count"), int)
                or not 1 <= native_components["component_count"] <= 256
            ))
        ):
            return {"status": "unavailable", "reason": "source_readback_unavailable", "source": "unknown"}
        projection = {
            "status": "observed", "source": "prometheus",
            "matches_intended": readback["matches_intended"],
            "sample_count": 1, "probe_failed_count": 0, "scrape_failed_count": 0,
            "body_counter": "not_exposed_by_legacy_prometheus",
        }
        if has_native_absence:
            projection["native_components"] = "absent"
        return projection
    if (
        not isinstance(evidence, Mapping) or set(evidence) != {"status", "source", "detail", "samples"}
        or evidence.get("status") != "observed" or evidence.get("source") != "alloy"
    ):
        return {"status": "unavailable", "reason": "source_readback_unavailable", "source": "unknown"}
    detail, samples = evidence["detail"], evidence["samples"]
    self_only = (
        allow_self_only is True
        and isinstance(detail, Mapping) and detail.get("target_count") == 0
        and detail.get("targets") == []
        and isinstance(samples, Mapping) and samples.get("targets") == []
    )
    if (
        not isinstance(detail, Mapping) or not isinstance(samples, Mapping)
        or detail.get("status") != "observed" or samples.get("status") != "observed"
        or set(detail) != {"status", "reason", "target_count", "targets", "alloy_self"}
        or set(samples) != {"status", "reason", "freshness_seconds", "alloy_self", "targets"}
        or detail.get("reason") != "fixed_native_components_loaded"
        or samples.get("reason") != "fresh_native_samples"
        or isinstance(samples.get("freshness_seconds"), bool)
        or not isinstance(samples.get("freshness_seconds"), int)
        or not 60 <= samples["freshness_seconds"] <= 86400
        or not isinstance(detail.get("target_count"), int) or isinstance(detail.get("target_count"), bool)
        or not (detail["target_count"] == 0 if self_only else 1 <= detail["target_count"] <= 64)
        or not isinstance(detail.get("targets"), list) or len(detail["targets"]) != detail["target_count"]
        or not isinstance(samples.get("targets"), list) or len(samples["targets"]) != detail["target_count"]
    ):
        return {"status": "unavailable", "reason": "source_readback_unavailable", "source": "unknown"}
    loaded_self, sampled_self = detail.get("alloy_self"), samples.get("alloy_self")
    if (
        not isinstance(loaded_self, Mapping) or set(loaded_self) != {"status", "limits"}
        or loaded_self.get("status") != "observed" or not _valid_availability_limits(loaded_self.get("limits"))
        or not isinstance(sampled_self, Mapping)
        or set(sampled_self) != {"status", "scrape_up", "scrape_samples_scraped", "scrape_body_size_bytes"}
        or sampled_self.get("status") != "observed"
        or isinstance(sampled_self.get("scrape_up"), bool)
        or not isinstance(sampled_self.get("scrape_up"), int)
        or sampled_self["scrape_up"] != 1
        or any(
            isinstance(sampled_self.get(field), bool) or not isinstance(sampled_self.get(field), int)
            or not 1 <= sampled_self[field] <= loaded_self["limits"][limit_field]
            for field, limit_field in NATIVE_HEALTH_SAMPLE_LIMITS.items()
        )
    ):
        return {"status": "unavailable", "reason": "source_readback_unavailable", "source": "unknown"}
    detail_ids: list[str] = []
    probe_failed = scrape_failed = 0
    for detail_row, sample_row in zip(detail["targets"], samples["targets"], strict=True):
        if (
            not isinstance(detail_row, Mapping) or set(detail_row) != {"target_id", "status", "probe_health", "limits"}
            or detail_row.get("status") != "observed" or detail_row.get("probe_health") not in {"up", "down", "unknown"}
            or not isinstance(detail_row.get("target_id"), str)
            or not re.fullmatch(r"service:[A-Za-z0-9_.:/@-]{1,120}", detail_row["target_id"])
            or not _valid_availability_limits(detail_row.get("limits"))
            or not isinstance(sample_row, Mapping) or sample_row.get("target_id") != detail_row["target_id"]
        ):
            return {"status": "unavailable", "reason": "source_readback_unavailable", "source": "unknown"}
        detail_ids.append(detail_row["target_id"])
        if sample_row.get("status") == "scrape_failed" and set(sample_row) == {"target_id", "status"}:
            scrape_failed += 1
        elif (
            sample_row.get("status") == "observed"
            and set(sample_row) == {
                "target_id", "status", "scrape_up", "probe_success", "probe_http_status_code",
                "probe_duration_seconds", "scrape_samples_scraped", "scrape_body_size_bytes",
            }
            and isinstance(sample_row.get("scrape_up"), int)
            and not isinstance(sample_row.get("scrape_up"), bool)
            and sample_row["scrape_up"] == 1
            and isinstance(sample_row.get("probe_success"), int)
            and not isinstance(sample_row.get("probe_success"), bool)
            and sample_row["probe_success"] in {0, 1}
            and isinstance(sample_row.get("probe_http_status_code"), int)
            and not isinstance(sample_row.get("probe_http_status_code"), bool)
            and 0 <= sample_row["probe_http_status_code"] <= 599
            and _valid_native_duration(sample_row.get("probe_duration_seconds"))
            and isinstance(sample_row.get("scrape_samples_scraped"), int)
            and not isinstance(sample_row.get("scrape_samples_scraped"), bool)
            and 1 <= sample_row["scrape_samples_scraped"] <= detail_row["limits"]["sample_limit"]
            and isinstance(sample_row.get("scrape_body_size_bytes"), int)
            and not isinstance(sample_row.get("scrape_body_size_bytes"), bool)
            and 1 <= sample_row["scrape_body_size_bytes"] <= detail_row["limits"]["body_size_limit"]
        ):
            probe_failed += sample_row["probe_success"] == 0
        else:
            return {"status": "unavailable", "reason": "source_readback_unavailable", "source": "unknown"}
    if len(set(detail_ids)) != len(detail_ids):
        return {"status": "unavailable", "reason": "source_readback_unavailable", "source": "unknown"}
    return {
        "status": "observed", "source": "alloy",
        "matches_intended": readback["matches_intended"],
        "sample_count": 1 if self_only else detail["target_count"],
        "probe_failed_count": probe_failed, "scrape_failed_count": scrape_failed,
        "body_counter": "observed_positive",
    }


def o11y_availability_source_gate_diagnostic(result: Any) -> str:
    """Reduce a protected Ansible command result to one fixed diagnostic category."""
    fallback = "producer_evidence_invalid"
    try:
        if not isinstance(result, Mapping):
            return "producer_process_unexpected"
        rc, stdout, stderr = result.get("rc"), result.get("stdout"), result.get("stderr")
        if isinstance(rc, bool) or not isinstance(rc, int) or rc not in {0, 2}:
            return "producer_process_unexpected"
        if not isinstance(stdout, str):
            return "producer_process_unexpected"
        if len(stdout.encode("utf-8")) > 65536:
            return "producer_stdout_oversized"

        def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            value: dict[str, Any] = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError("duplicate JSON key")
                value[key] = item
            return value

        def reject_nonfinite(value: str) -> Any:
            raise ValueError("non-finite JSON number")

        try:
            readback = json.loads(
                stdout, object_pairs_hook=unique_object, parse_float=Decimal, parse_constant=reject_nonfinite,
            )
        except (ValueError, TypeError, InvalidOperation, RecursionError):
            return "producer_json_invalid"
        if (
            rc == 2 and isinstance(readback, Mapping)
            and set(readback) == {"status", "reason", "source", "intended_source", "matches_intended"}
            and readback.get("status") == "unavailable"
            and readback.get("reason") in SOURCE_GATE_REFUSAL_REASONS
            and readback.get("source") == "unknown"
            and readback.get("intended_source") == "unknown"
            and readback.get("matches_intended") is False
        ):
            return readback["reason"]
        coverage = o11y_availability_source_coverage(readback, allow_self_only=True)
        if stderr != "":
            return "producer_stderr_present"
        if rc == 0 and coverage.get("status") == "observed":
            return "producer_observed" if coverage.get("matches_intended") is True else "producer_mismatch"
        if rc == 2:
            return "producer_exit_code_mismatch" if coverage.get("status") == "observed" else fallback
        return fallback
    except Exception:
        return "producer_evidence_invalid"


class FilterModule:
    """Expose the pure preview as an Ansible filter."""

    def filters(self) -> dict[str, Any]:
        return {
            "o11y_scrape_budget_preview": o11y_scrape_budget_preview,
            "o11y_loaded_scrape_limit_coverage": o11y_loaded_scrape_limit_coverage,
            "o11y_source_scrape_limit_coverage": o11y_source_scrape_limit_coverage,
            "o11y_availability_source_coverage": o11y_availability_source_coverage,
            "o11y_availability_source_gate_diagnostic": o11y_availability_source_gate_diagnostic,
        }
