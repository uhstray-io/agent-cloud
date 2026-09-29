"""Validate Tempo's multi-document status config without exposing its contents."""

import json
import sys
from collections.abc import Mapping

import yaml

MAX_CONFIG_BYTES = 4 * 1024 * 1024
EXPECTED_REMOTE_WRITE_URL = "http://prometheus:9090/api/v1/write"
EXPECTED_WAL_PATH = "/var/tempo/generator/wal"
EXPECTED_MAX_ACTIVE_SERIES = 2000


def _mapping_at(value, *keys):
    for key in keys:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def validate(raw, enabled):
    """Return only counts and booleans; never include parsed config or parser errors."""
    if len(raw.encode("utf-8")) > MAX_CONFIG_BYTES:
        return 2, {
            "status": "refused",
            "reason": "config_size_limit_exceeded",
            "document_count": None,
            "candidate_count": 0,
            "processors_match": False,
            "max_active_series_match": False,
            "remote_write_match": False,
            "wal_path_match": False,
        }

    document_count = 0
    candidate_count = 0
    candidate = None
    try:
        for document in yaml.safe_load_all(raw):
            document_count += 1
            if (
                isinstance(document, Mapping)
                and "metrics_generator" in document
                and "overrides" in document
            ):
                candidate_count += 1
                if candidate_count == 1:
                    candidate = document
    except (yaml.YAMLError, UnicodeError):
        return 2, {
            "status": "refused",
            "reason": "invalid_yaml",
            "document_count": document_count,
            "candidate_count": candidate_count,
            "processors_match": False,
            "max_active_series_match": False,
            "remote_write_match": False,
            "wal_path_match": False,
        }

    unique_candidate = candidate_count == 1
    processors = _mapping_at(
        candidate, "overrides", "defaults", "metrics_generator", "processors"
    ) if unique_candidate else None
    expected_processors = {"service-graphs", "span-metrics"} if enabled else set()
    processors_match = (
        not enabled and processors in (None, [])
    ) or (
        isinstance(processors, list)
        and all(isinstance(processor, str) for processor in processors)
        and len(processors) == len(expected_processors)
        and set(processors) == expected_processors
    )

    max_active_series = _mapping_at(
        candidate, "overrides", "defaults", "metrics_generator", "max_active_series"
    ) if unique_candidate else None
    max_active_series_match = (
        isinstance(max_active_series, int)
        and not isinstance(max_active_series, bool)
        and max_active_series == EXPECTED_MAX_ACTIVE_SERIES
    )

    remote_write = _mapping_at(candidate, "metrics_generator", "storage", "remote_write") if unique_candidate else None
    remote_write_match = (
        isinstance(remote_write, list)
        and len(remote_write) == 1
        and isinstance(remote_write[0], Mapping)
        and remote_write[0].get("url") == EXPECTED_REMOTE_WRITE_URL
    )
    wal_path = _mapping_at(candidate, "metrics_generator", "storage", "path") if unique_candidate else None
    wal_path_match = wal_path == EXPECTED_WAL_PATH

    checks = {
        "processors_match": processors_match,
        "max_active_series_match": max_active_series_match,
        "remote_write_match": remote_write_match,
        "wal_path_match": wal_path_match,
    }
    verified = unique_candidate and all(checks.values())
    return (0 if verified else 2), {
        "status": "verified" if verified else "refused",
        "reason": None if verified else "configuration_mismatch",
        "document_count": document_count,
        "candidate_count": candidate_count,
        **checks,
    }


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in {"true", "false"}:
        print(json.dumps({"status": "refused", "reason": "invalid_arguments"}, sort_keys=True))
        return 2
    raw = sys.stdin.read(MAX_CONFIG_BYTES + 1)
    code, result = validate(raw, enabled=sys.argv[1] == "true")
    print(json.dumps(result, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
