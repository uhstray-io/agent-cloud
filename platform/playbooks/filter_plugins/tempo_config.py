"""Validate Tempo's effective multi-document configuration in Ansible memory."""

from collections.abc import Mapping

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode

MAX_CONFIG_BYTES = 4 * 1024 * 1024
EXPECTED_REMOTE_WRITE_URL = "http://prometheus:9090/api/v1/write"
EXPECTED_WAL_PATH = "/var/tempo/generator/wal"
EXPECTED_MAX_ACTIVE_SERIES = 2000


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """Safe YAML loader that refuses duplicate mapping keys instead of taking the last value."""

    def construct_mapping(self, node, deep=False):
        if not isinstance(node, MappingNode):
            return super().construct_mapping(node, deep=deep)
        self.flatten_mapping(node)
        keys = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in keys
                keys.add(key)
            except TypeError as error:
                raise ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    "found an unhashable key",
                    key_node.start_mark,
                ) from error
            if duplicate:
                raise ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    "found a duplicate key",
                    key_node.start_mark,
                )
        return super().construct_mapping(node, deep=deep)


def _mapping_at(value, *keys):
    """Return a nested mapping value, or None when a parent is not a mapping."""
    for key in keys:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def _refusal(reason, document_count=None, candidate_count=0):
    """Build a sanitized refusal result with every config check closed."""
    return {
        "status": "refused",
        "valid": False,
        "reason": reason,
        "document_count": document_count,
        "candidate_count": candidate_count,
        "processors_match": False,
        "max_active_series_match": False,
        "remote_write_match": False,
        "wal_path_match": False,
    }


def tempo_metrics_config_check(raw, enabled=False):
    """Return only sanitized scalar results; parsed config and parser errors stay in memory."""
    document_count = 0
    candidate_count = 0
    try:
        if not isinstance(raw, str):
            return _refusal("invalid_input")
        if len(raw.encode("utf-8")) > MAX_CONFIG_BYTES:
            return _refusal("config_size_limit_exceeded")

        candidate = None
        for document in yaml.load_all(raw, Loader=_UniqueKeySafeLoader):
            document_count += 1
            if (
                isinstance(document, Mapping)
                and "metrics_generator" in document
                and "overrides" in document
            ):
                candidate_count += 1
                if candidate_count == 1:
                    candidate = document
    except yaml.YAMLError:
        return _refusal("invalid_yaml", document_count, candidate_count)
    except Exception:
        # Never let parser or conversion exception text include raw status/config data.
        return _refusal("filter_error", document_count, candidate_count)

    if candidate_count != 1:
        reason = "no_matching_config" if candidate_count == 0 else "ambiguous_config"
        return _refusal(reason, document_count, candidate_count)

    processors = _mapping_at(
        candidate, "overrides", "defaults", "metrics_generator", "processors"
    )
    expected_processors = {"service-graphs", "span-metrics"} if enabled else set()
    processors_match = (
        (not enabled and processors in (None, []))
        or (
            isinstance(processors, list)
            and all(isinstance(processor, str) for processor in processors)
            and len(processors) == len(expected_processors)
            and set(processors) == expected_processors
        )
    )

    max_active_series = _mapping_at(
        candidate, "overrides", "defaults", "metrics_generator", "max_active_series"
    )
    max_active_series_match = (
        isinstance(max_active_series, int)
        and not isinstance(max_active_series, bool)
        and max_active_series == EXPECTED_MAX_ACTIVE_SERIES
    )

    remote_write = _mapping_at(candidate, "metrics_generator", "storage", "remote_write")
    remote_write_match = (
        isinstance(remote_write, list)
        and len(remote_write) == 1
        and isinstance(remote_write[0], Mapping)
        and remote_write[0].get("url") == EXPECTED_REMOTE_WRITE_URL
    )
    wal_path = _mapping_at(candidate, "metrics_generator", "storage", "path")
    wal_path_match = wal_path == EXPECTED_WAL_PATH

    checks = {
        "processors_match": bool(processors_match),
        "max_active_series_match": bool(max_active_series_match),
        "remote_write_match": bool(remote_write_match),
        "wal_path_match": bool(wal_path_match),
    }
    valid = all(checks.values())
    return {
        "status": "verified" if valid else "refused",
        "valid": valid,
        "reason": None if valid else "configuration_mismatch",
        "document_count": document_count,
        "candidate_count": candidate_count,
        **checks,
    }


class FilterModule:
    """Register the Tempo configuration validator with Ansible."""

    def filters(self):
        """Expose the validator as a Jinja filter."""
        return {"tempo_metrics_config_check": tempo_metrics_config_check}
