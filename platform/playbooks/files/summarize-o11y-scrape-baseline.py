#!/usr/bin/env python3
"""Collect private scrape evidence and emit only a bounded public receipt."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode, urlsplit

import yaml

ENGINE = "podman"
CONTAINER = "o11y-grafana"
PROMETHEUS = "http://prometheus:9090"
PROMETHEUS_SELF_INSTANCE = "localhost:9090"
PROMETHEUS_SELF_URL = "http://localhost:9090/metrics"
PROMETHEUS_SELF_PROBE_URL = "http://prometheus:9090/metrics"
SAMPLES_QUERY = "max_over_time(scrape_samples_scraped[24h])"
MAX_API_BYTES = 16 * 1024 * 1024
MAX_RESPONSE_BYTES = 160 * 1024 * 1024
MAX_RESPONSE_HEADERS = 64 * 1024
CONNECT_TIMEOUT = 3
TRANSFER_TIMEOUT = 10
CURL_MIN_VERSION = (8, 4, 0)
MAX_ROWS = 10000
MAX_INTEGER_DIGITS = 20
ALERT_INPUTS = (
    ("otelcol_receiver_refused_log_records_total", "alloy"),
    ("otelcol_receiver_refused_spans_total", "alloy"),
    ("otelcol_exporter_enqueue_failed_spans_total", "alloy"),
    ("otelcol_exporter_send_failed_spans_total", "alloy"),
    ("loki_discarded_samples_total", "loki"),
    ("tempo_discarded_spans_total", "tempo"),
)
EXPECTED_IDENTITY_FIELDS = ("job", "instance", "service", "cluster", "environment")
EXPECTED_UID = re.compile(r"^o11y_missing_[a-z0-9_]{1,27}\Z")
EXPECTED_GROUP_UID = re.compile(r"^o11y_missing_g[a-f0-9]{26}\Z")
SAFE_IDENTITY_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@-]{0,127}\Z")
MAX_EXPECTED_TARGETS = 100
MAX_SOURCE_CONFIG_BYTES = 1024 * 1024
SOURCE_CONFIG_SUFFIX = ("platform", "services", "o11y", "deployment", "config", "scrape.d")
SOURCE_CONFIG_FILES = {
    "agentgateway.yml": {"agentgateway"},
    "dgx-spark.yml": {"dgx-spark-node", "dgx-spark-gpu", "dgx-spark-vllm"},
}
SOURCE_LIMIT_FIELDS = (
    "body_size_limit",
    "sample_limit",
    "label_limit",
    "label_name_length_limit",
    "label_value_length_limit",
)

JOB_ALIASES = {
    "prometheus": "prometheus",
    "receiver-host": "receiver-host",
    "grafana": "grafana",
    "loki": "loki",
    "alloy": "alloy",
    "tempo": "tempo",
    "pyroscope": "pyroscope",
    "agentgateway": "agentgateway",
    "dgx-spark-node": "dgx-spark",
    "dgx-spark-gpu": "dgx-spark",
    "dgx-spark-vllm": "dgx-spark",
}
HOST_JOB = re.compile(r"host-node-[a-z0-9][a-z0-9_-]{0,31}\Z")
VERSION = re.compile(rb"^curl (\d+)\.(\d+)\.(\d+)", re.MULTILINE)
STATUS_LINE = re.compile(rb"^HTTP/\d(?:\.\d)? ([0-9]{3})(?: .*)?\r?$", re.IGNORECASE)
DECIMAL = re.compile(r"^[0-9]{1,20}$")
NUMERIC_HOST = re.compile(r"(?:[0-9]+|0[xX][0-9a-fA-F]+)(?:\.(?:[0-9]+|0[xX][0-9a-fA-F]+))*\Z")


class Refusal(ValueError):
    """A fixed refusal category that contains no target-controlled text."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


class _SourceUniqueKeyLoader(yaml.SafeLoader):
    """Safe, bounded YAML loader for the two generated scrape fragments."""

    def __init__(self, stream: Any) -> None:
        super().__init__(stream)
        self._collection_depth = 0

    def compose_node(self, parent: Any, index: Any) -> yaml.Node:
        if self.check_event(yaml.AliasEvent):
            raise yaml.YAMLError("aliases are refused")
        if not self.check_event(yaml.MappingStartEvent, yaml.SequenceStartEvent):
            return super().compose_node(parent, index)
        self._collection_depth += 1
        try:
            if self._collection_depth > 64:
                raise yaml.YAMLError("nesting limit exceeded")
            return super().compose_node(parent, index)
        finally:
            self._collection_depth -= 1


def _source_unique_mapping(
    loader: _SourceUniqueKeyLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ValueError("duplicate YAML key")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_SourceUniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _source_unique_mapping)


def _open_source_directory(path: str) -> int:
    """Open the exact expected directory chain without following any symlink."""
    if not isinstance(path, str) or not path.startswith("/") or "\x00" in path:
        raise Refusal("source_path_refused")
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise Refusal("source_path_refused")
    parts = path.split("/")[1:]
    suffix = tuple(parts[-len(SOURCE_CONFIG_SUFFIX):])
    if len(parts) < len(SOURCE_CONFIG_SUFFIX) or suffix != SOURCE_CONFIG_SUFFIX:
        raise Refusal("source_path_refused")
    if any(part in {"", ".", ".."} for part in parts):
        raise Refusal("source_path_refused")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open("/", flags)
    try:
        for part in parts:
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise Refusal("source_path_refused")
        return fd
    except Exception:
        os.close(fd)
        raise


def _read_source_file(directory_fd: int, filename: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(filename, flags, dir_fd=directory_fd)
    except OSError:
        raise Refusal("source_file_unavailable") from None
    try:
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_mode & 0o022
            or not 0 < metadata.st_size <= MAX_SOURCE_CONFIG_BYTES
        ):
            raise Refusal("source_file_refused")
        chunks = bytearray()
        while len(chunks) <= MAX_SOURCE_CONFIG_BYTES:
            chunk = os.read(fd, min(65536, MAX_SOURCE_CONFIG_BYTES + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
        if len(chunks) > MAX_SOURCE_CONFIG_BYTES or len(chunks) != metadata.st_size:
            raise Refusal("source_file_refused")
        after = os.fstat(fd)
        before_signature = (
            metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns
        )
        after_signature = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        if before_signature != after_signature:
            raise Refusal("source_file_refused")
        return bytes(chunks)
    finally:
        os.close(fd)


def _source_scrape_limit_readback(directory: str) -> dict[str, Any]:
    """Read only the two generated fragments and retain only job limits privately."""
    directory_fd = _open_source_directory(directory)
    jobs: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        for filename, allowed_jobs in SOURCE_CONFIG_FILES.items():
            raw = _read_source_file(directory_fd, filename)
            try:
                documents = list(yaml.load_all(raw.decode("utf-8"), Loader=_SourceUniqueKeyLoader))
            except (UnicodeError, yaml.YAMLError, ValueError, TypeError):
                raise Refusal("source_yaml_refused") from None
            if len(documents) != 1 or not isinstance(documents[0], dict) or set(documents[0]) != {"scrape_configs"}:
                raise Refusal("source_yaml_refused")
            configured = documents[0]["scrape_configs"]
            if not isinstance(configured, list) or not configured:
                raise Refusal("source_yaml_refused")
            for item in configured:
                if not isinstance(item, dict):
                    raise Refusal("source_yaml_refused")
                job_name = item.get("job_name")
                if not isinstance(job_name, str) or job_name not in allowed_jobs or job_name in seen:
                    raise Refusal("source_job_refused")
                seen.add(job_name)
                limits = {field: item.get(field) for field in SOURCE_LIMIT_FIELDS}
                if any(type(value) not in (str, int) for value in limits.values()):
                    raise Refusal("source_limit_refused")
                jobs.append({"job_name": job_name, "limits": limits})
    finally:
        os.close(directory_fd)
    if "agentgateway" not in seen or not {"dgx-spark-node", "dgx-spark-vllm"}.issubset(seen):
        raise Refusal("source_job_refused")
    return {"status": "observed", "reason": "source_files_read", "jobs": jobs}


def validate_expected_target_declaration(
    targets: Any,
    retired_uids: Any = None,
    *,
    reviewed: Any = True,
    other_rule_uids: Any = None,
) -> tuple[list[dict[str, str]], list[str]]:
    """Validate the bounded private declaration without exposing its values."""
    retired_uids = [] if retired_uids is None else retired_uids
    other_rule_uids = [] if other_rule_uids is None else other_rule_uids
    if reviewed is not True or not isinstance(targets, list) or not isinstance(retired_uids, list):
        raise Refusal("invalid_declaration")
    if len(targets) > MAX_EXPECTED_TARGETS or len(retired_uids) > MAX_EXPECTED_TARGETS:
        raise Refusal("invalid_declaration")
    if not targets and not retired_uids:
        raise Refusal("invalid_declaration")
    if not isinstance(other_rule_uids, list) or len(other_rule_uids) > MAX_ROWS:
        raise Refusal("invalid_declaration")

    normalized: list[dict[str, str]] = []
    identities: set[tuple[str, ...]] = set()
    target_uids: list[str] = []
    for target in targets:
        if not isinstance(target, dict) or set(target) != {"uid", *EXPECTED_IDENTITY_FIELDS}:
            raise Refusal("invalid_declaration")
        uid = target.get("uid")
        if not isinstance(uid, str) or not EXPECTED_UID.fullmatch(uid):
            raise Refusal("invalid_declaration")
        values: dict[str, str] = {}
        for field in EXPECTED_IDENTITY_FIELDS:
            value = target.get(field)
            if not isinstance(value, str) or not SAFE_IDENTITY_VALUE.fullmatch(value):
                raise Refusal("invalid_declaration")
            values[field] = value
        identity = tuple(values[field] for field in EXPECTED_IDENTITY_FIELDS)
        if uid in target_uids or identity in identities:
            raise Refusal("invalid_declaration")
        target_uids.append(uid)
        identities.add(identity)
        normalized.append({"uid": uid, **values})

    if len(set(retired_uids)) != len(retired_uids):
        raise Refusal("invalid_declaration")
    if any(not isinstance(uid, str) or not EXPECTED_UID.fullmatch(uid) for uid in retired_uids):
        raise Refusal("invalid_declaration")
    if not isinstance(other_rule_uids, list) or any(
        not isinstance(uid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", uid) for uid in other_rule_uids
    ):
        raise Refusal("invalid_declaration")
    all_target_uids = set(target_uids) | set(retired_uids)
    if len(all_target_uids) != len(target_uids) + len(retired_uids):
        raise Refusal("invalid_declaration")
    if all_target_uids.intersection(other_rule_uids):
        raise Refusal("invalid_declaration")
    group_uids = [group["uid"] for group in expected_target_groups(normalized)]
    if len(set(group_uids)) != len(group_uids) or set(group_uids) & all_target_uids:
        raise Refusal("invalid_declaration")
    if set(group_uids).intersection(other_rule_uids):
        raise Refusal("invalid_declaration")
    return normalized, list(retired_uids)


def expected_target_groups(targets: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Group targets by stable routing identity and assign a deterministic rule UID."""
    grouped: dict[tuple[str, str, str], list[dict[str, str]]] = {}
    for target in targets:
        key = (target["service"], target["environment"], target["cluster"])
        grouped.setdefault(key, []).append(target)
    result = []
    for (service, environment, cluster), members in sorted(grouped.items()):
        canonical_key = json.dumps([service, environment, cluster], ensure_ascii=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical_key.encode("utf-8")).hexdigest()[:26]
        uid = f"o11y_missing_g{digest}"
        result.append({
            "uid": uid,
            "service": service,
            "environment": environment,
            "cluster": cluster,
            "targets": sorted(members, key=lambda target: (target["job"], target["instance"])),
        })
    return result


def expected_target_group_expression(group: dict[str, Any]) -> str:
    """Emit one absent series per exact expected target selector."""
    expressions = []
    for target in group["targets"]:
        matchers = ",".join(
            f"{field}={json.dumps(target[field], ensure_ascii=True)}"
            for field in ("job", "instance", "service", "cluster", "environment")
        )
        expressions.append(f"absent_over_time(up{{{matchers}}}[5m])")
    return " or ".join(expressions)


def _tombstone_title(uid: str, *, retired: bool = False) -> str:
    state = "retired" if retired else "paused"
    return f"Expected target {state} ({uid})"


def _group_tombstone_title(uid: str) -> str:
    return f"Expected target group paused ({uid})"


def _as_tombstone(row: dict[str, Any], *, retired: bool = False) -> dict[str, Any]:
    if row.get("_kind") == "group" or (
        isinstance(row.get("uid"), str) and EXPECTED_GROUP_UID.fullmatch(row["uid"])
    ):
        return {
            "uid": row["uid"], "title": _group_tombstone_title(row["uid"]),
            "summary": "Expected target group rule is paused.",
            "description": "The group UID remains owned and inert for a reviewed rollback or disable transition.",
            "labels": {"severity": "warning", "service": row["service"],
                       "environment": row["environment"], "cluster": row["cluster"]},
        }
    identity = {field: row[field] for field in EXPECTED_IDENTITY_FIELDS if row.get(field)}
    labels = {"severity": "warning", **identity,
              "environment": identity.get("environment", "disabled")}
    return {
        "uid": row["uid"], "title": _tombstone_title(row["uid"], retired=retired),
        "summary": "Expected metrics target rule is disabled.",
        "description": "This owned UID was explicitly retired and remains inert." if retired
        else ("The original target identity remains owned and inert." if identity else
              "No target identity is retained while the reviewed alert gate is disabled."),
        "labels": labels,
    }


def _group_from_rule(rule: dict[str, Any]) -> dict[str, Any]:
    """Validate the generated group identity and recover its exact selector members."""
    uid = rule.get("uid")
    labels = rule.get("labels")
    if not isinstance(uid, str) or not EXPECTED_UID.fullmatch(uid) or not isinstance(labels, dict):
        raise Refusal("invalid_previous_rules")
    service, environment, cluster = (labels.get(key) for key in ("service", "environment", "cluster"))
    if any(not isinstance(value, str) or not SAFE_IDENTITY_VALUE.fullmatch(value)
           for value in (service, environment, cluster)):
        raise Refusal("invalid_previous_rules")
    expected_group = expected_target_groups([{
        "uid": "o11y_missing_internal",
        "job": "internal", "instance": "internal:1", "service": service,
        "environment": environment, "cluster": cluster,
    }])[0]
    if uid != expected_group["uid"]:
        raise Refusal("invalid_previous_rules")
    data = rule.get("data")
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise Refusal("invalid_previous_rules")
    expression = data[0].get("model", {}).get("expr")
    if not isinstance(expression, str) or len(expression) > 32768:
        raise Refusal("invalid_previous_rules")
    member_rows = []
    for term in expression.split(" or "):
        match = re.fullmatch(
            r'absent_over_time\(up\{job="([A-Za-z0-9][A-Za-z0-9_.:/@-]{0,127})",'
            r'instance="([A-Za-z0-9][A-Za-z0-9_.:/@-]{0,127})",'
            r'service="([A-Za-z0-9][A-Za-z0-9_.:/@-]{0,127})",'
            r'cluster="([A-Za-z0-9][A-Za-z0-9_.:/@-]{0,127})",'
            r'environment="([A-Za-z0-9][A-Za-z0-9_.:/@-]{0,127})"\}\[5m\]\)',
            term,
        )
        if not match:
            raise Refusal("invalid_previous_rules")
        member = dict(zip(EXPECTED_IDENTITY_FIELDS, (match[1], match[2], match[3], match[4], match[5]), strict=True))
        if (member["service"], member["environment"], member["cluster"]) != (service, environment, cluster):
            raise Refusal("invalid_previous_rules")
        member_rows.append(member)
    member_identities = {tuple(row[field] for field in EXPECTED_IDENTITY_FIELDS) for row in member_rows}
    if not member_rows or len(member_identities) != len(member_rows):
        raise Refusal("invalid_previous_rules")
    if expression != " or ".join(
        f'absent_over_time(up{{job={json.dumps(row["job"])},instance={json.dumps(row["instance"])},'
        f'service={json.dumps(row["service"])},cluster={json.dumps(row["cluster"])},'
        f'environment={json.dumps(row["environment"])}}}[5m])'
        for row in member_rows
    ):
        raise Refusal("invalid_previous_rules")
    return {"uid": uid, "service": service, "environment": environment, "cluster": cluster,
            "targets": member_rows, "_expr": expression}


def _expected_target_queries_match(data: Any, expression: str) -> bool:
    """Validate the complete A -> B rule, allowing only Grafana's API metadata."""
    if not isinstance(data, list) or len(data) != 2:
        return False
    expected_rows = (
        (
            "A", "prometheus", {"from": 300 if expression == "vector(0)" else 600, "to": 0},
            {
                "editorMode": "code", "expr": expression, "instant": True, "range": False,
                "intervalMs": 1000, "maxDataPoints": 43200, "refId": "A",
            },
            "prometheus",
        ),
        (
            "B", "__expr__", {"from": 0, "to": 0},
            {
                "expression": "A", "type": "threshold",
                "conditions": [{
                    "evaluator": {"type": "gt", "params": [0.5]},
                    "operator": {"type": "and"}, "query": {"params": ["B"]},
                    "reducer": {"type": "last", "params": []}, "type": "query",
                }],
                "intervalMs": 1000, "maxDataPoints": 43200, "refId": "B",
            },
            "__expr__",
        ),
    )
    for index, (ref_id, datasource_uid, time_range, expected_model, datasource_type) in enumerate(expected_rows):
        row = data[index]
        if not isinstance(row, dict) or set(row) - {
            "refId", "datasourceUid", "relativeTimeRange", "model", "queryType",
        }:
            return False
        if (
            row.get("refId") != ref_id
            or row.get("datasourceUid") != datasource_uid
            or ("queryType" in row and row["queryType"] != "")
        ):
            return False
        actual_range = row.get("relativeTimeRange", {"from": 0, "to": 0} if ref_id == "B" else None)
        if actual_range != time_range:
            return False
        model = row.get("model")
        if not isinstance(model, dict) or set(model) - (set(expected_model) | {"datasource", "hide"}):
            return False
        if any(model.get(key) != value for key, value in expected_model.items()):
            return False
        if "hide" in model and model["hide"] is not False:
            return False
        if "datasource" in model and model["datasource"] != {
            "type": datasource_type, "uid": datasource_uid,
        }:
            return False
    return True


def _live_rule_matches_owned(rule: Any, target: dict[str, Any], folder_uid: str) -> bool:
    """Compare an API readback with one validated provisioning owner."""
    if not isinstance(rule, dict) or rule.get("folderUID") != folder_uid:
        return False
    if target.get("_kind") == "group":
        if target["_inert"]:
            return (
                rule.get("title") == _group_tombstone_title(target["uid"])
                and rule.get("condition") == "B" and rule.get("isPaused") is True
                and rule.get("labels") == {
                    "severity": "warning", "service": target["service"],
                    "environment": target["environment"], "cluster": target["cluster"],
                    "owner": "platform-operations",
                }
                and _expected_target_queries_match(rule.get("data"), "vector(0)")
                and rule.get("noDataState") == "OK" and rule.get("execErrState") == "OK"
                and rule.get("for") == "0s"
                and rule.get("annotations") == {
                    "summary": "Expected target group rule is paused.",
                    "description": (
                        "The group UID remains owned and inert for a reviewed rollback or disable transition."
                    ),
                }
                and rule.get("notification_settings") in (None, {})
            )
        return (
            rule.get("title") == target["_title"]
            and rule.get("condition") == "B"
            and rule.get("labels") == target["_labels"]
            and _expected_target_queries_match(rule.get("data"), target["_expr"])
            and rule.get("annotations") == target["_annotations"]
            and rule.get("isPaused") is target["_is_paused"]
            and rule.get("notification_settings") == target["_notification_settings"]
            and rule.get("noDataState") == "OK" and rule.get("execErrState") == "Alerting"
            and rule.get("for") == "2m"
        )
    if target["_inert"]:
        retired = target["_retired"]
        return (
            rule.get("title") == (
                target.get("_title") or _tombstone_title(target["uid"], retired=retired)
            )
            and rule.get("condition") == "B"
            and rule.get("isPaused") is True
            and rule.get("labels") == target.get("_labels", {
                "severity": "warning", "owner": "platform-operations", "environment": "disabled",
            })
            and _expected_target_queries_match(rule.get("data"), "vector(0)")
            and rule.get("noDataState") == "OK"
            and rule.get("execErrState") == "OK"
            and rule.get("for") == "0s"
            and rule.get("annotations") == {
                "summary": "Expected metrics target rule is disabled.",
                "description": (
                    "This owned UID was explicitly retired and remains inert."
                    if retired else ("The original target identity remains owned and inert."
                                     if target.get("job") else
                                     "No target identity is retained while the reviewed alert gate is disabled.")
                ),
            }
            and rule.get("notification_settings") in (None, {})
        )
    labels = rule.get("labels")
    return (
        isinstance(labels, dict)
        and rule.get("condition") == "B"
        and all(labels.get(field) == target[field] for field in EXPECTED_IDENTITY_FIELDS)
        and labels == target["_labels"]
        and rule.get("title") == target["_title"]
        and _expected_target_queries_match(rule.get("data"), target["_expr"])
        and rule.get("annotations") == target["_annotations"]
        and rule.get("isPaused") is target["_is_paused"]
        and rule.get("notification_settings") == target["_notification_settings"]
        and rule.get("noDataState") == "OK"
        and rule.get("execErrState") == "Alerting"
        and rule.get("for") == "2m"
    )


def validate_active_expected_target_rules(input_document: Any) -> dict[str, Any]:
    """Prove active targets, gate-off owners, and explicit inert tombstones separately."""
    if not isinstance(input_document, dict):
        raise Refusal("invalid_live_rules")
    if input_document.get("targets") == [] and input_document.get("retired_uids") == []:
        targets, retired = [], []
        if input_document.get("reviewed") is not True:
            raise Refusal("invalid_declaration")
    else:
        targets, retired = validate_expected_target_declaration(
            input_document.get("targets"), input_document.get("retired_uids"),
            reviewed=input_document.get("reviewed"),
        )
    mode = input_document.get("mode", "legacy")
    if mode not in {"grouped", "legacy"}:
        raise Refusal("invalid_declaration")
    groups = expected_target_groups(targets)
    active_target_rows = targets if mode == "legacy" else []
    active_group_rows = groups if mode == "grouped" else []
    paused = input_document.get("paused_uids", [])
    tombstones = input_document.get("tombstones", [])
    active_uids = {row["uid"] for row in [*active_target_rows, *active_group_rows]}
    if (
        not isinstance(paused, list)
        or any(not isinstance(uid, str) or not EXPECTED_UID.fullmatch(uid) for uid in paused)
        or len(set(paused)) != len(paused)
        or set(paused) & (active_uids | set(retired))
    ):
        raise Refusal("invalid_declaration")
    rules = input_document.get("live_rules")
    folders = input_document.get("live_folders")
    if (
        not isinstance(rules, list) or len(rules) > MAX_ROWS
        or not isinstance(folders, list) or len(folders) > MAX_ROWS
    ):
        raise Refusal("invalid_live_rules")
    matching_folders = [
        folder for folder in folders
        if isinstance(folder, dict) and folder.get("title") == "agent-cloud"
    ]
    if len(matching_folders) != 1 or not isinstance(matching_folders[0].get("uid"), str):
        raise Refusal("live_folder_drift")
    folder_uid = matching_folders[0]["uid"]
    by_uid = {rule.get("uid"): rule for rule in rules if isinstance(rule, dict)}
    expected_tombstones = {row.get("uid"): row for row in tombstones if isinstance(row, dict)}
    for target in targets:
        if mode == "grouped":
            expected_tombstones[target["uid"]] = _as_tombstone({"_kind": "target", **target})
    for uid in retired:
        expected_tombstones.setdefault(uid, _as_tombstone({"uid": uid}, retired=True))
    expected_ids = active_uids | set(paused) | set(retired)
    if len(by_uid) != len(rules) or set(by_uid) != expected_ids:
        raise Refusal("live_target_drift")
    active_target_contracts = _expected_target_contracts(active_target_rows)
    active_group_contracts = _expected_group_contracts(active_group_rows)
    for target in active_target_rows:
        rule = by_uid[target["uid"]]
        if not _live_rule_matches_owned(rule, active_target_contracts[target["uid"]], folder_uid):
            raise Refusal("live_target_drift")
        if rule.get("isPaused") is not False or rule.get("notification_settings") != {
            "receiver": "agent-cloud-ops", "group_by": ["service", "environment", "cluster", "alertname"],
            "group_wait": "30s", "group_interval": "5m", "repeat_interval": "4h",
        }:
            raise Refusal("live_target_drift")
    for group in active_group_rows:
        if not _live_rule_matches_owned(by_uid[group["uid"]], active_group_contracts[group["uid"]], folder_uid):
            raise Refusal("live_target_drift")
        if by_uid[group["uid"]].get("isPaused") is not False or by_uid[group["uid"]].get("notification_settings") != {
            "receiver": "agent-cloud-ops", "group_by": ["service", "environment", "cluster", "alertname"],
            "group_wait": "30s", "group_interval": "5m", "repeat_interval": "4h",
        }:
            raise Refusal("live_target_drift")
    for uid in set(paused) | set(retired):
        rule = by_uid[uid]
        tombstone = expected_tombstones.get(uid)
        if tombstone is None:
            raise Refusal("live_target_drift")
        if EXPECTED_GROUP_UID.fullmatch(tombstone["uid"]):
            expected = {
                "uid": uid, "_kind": "group", "_inert": True, "_retired": False,
                "service": tombstone["labels"]["service"],
                "environment": tombstone["labels"]["environment"],
                "cluster": tombstone["labels"]["cluster"],
            }
        else:
            expected = {"uid": uid, "_kind": "target", **{
                field: tombstone["labels"].get(field) for field in EXPECTED_IDENTITY_FIELDS
                if tombstone["labels"].get(field)
            }, "_title": tombstone["title"], "_labels": {
                **tombstone["labels"], "owner": "platform-operations",
            }, "_inert": True, "_retired": uid in retired}
        if not _live_rule_matches_owned(rule, expected, folder_uid):
            raise Refusal("live_target_drift")
    return {"status": "validated", "active_count": len(active_uids), "retired_count": len(retired)}


def _expected_group_contracts(groups: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    contracts = {}
    for group in groups:
        expr = expected_target_group_expression(group)
        contracts[group["uid"]] = {
            **group, "_kind": "group", "_inert": False, "_retired": False,
            "_title": f"Missing telemetry group ({group['uid']})", "_expr": expr,
            "_labels": {
                "severity": "critical", "job": "{{ $labels.job }}", "instance": "{{ $labels.instance }}",
                "service": group["service"], "owner": "platform-operations",
                "environment": group["environment"], "cluster": group["cluster"],
            },
            "_annotations": {
                "summary": "Expected metrics have disappeared for {{ $labels.service }} at {{ $labels.instance }}.",
                "dashboard_url": f"/d/service-overview?var-service={group['service']}",
                "description": "Check the declared metrics target and its service onboarding receipt.",
            },
            "_is_paused": False,
            "_notification_settings": {
                "receiver": "agent-cloud-ops", "group_by": ["service", "environment", "cluster", "alertname"],
                "group_wait": "30s", "group_interval": "5m", "repeat_interval": "4h",
            },
        }
    return contracts


def _expected_target_contracts(targets: list[dict[str, str]]) -> dict[str, dict[str, Any]]:
    """Build the validated alert contract from the reviewed declaration."""
    contracts = {}
    for target in targets:
        expr = (
            "absent_over_time(up{job=" + json.dumps(target["job"]) + ",instance=" + json.dumps(target["instance"])
            + ",service=" + json.dumps(target["service"]) + ",cluster=" + json.dumps(target["cluster"])
            + ",environment=" + json.dumps(target["environment"]) + "}[5m])"
        )
        contracts[target["uid"]] = {
            **target,
            "_inert": False,
            "_retired": False,
            "_title": f"Missing telemetry for {target['service']} at {target['instance']}",
            "_expr": expr,
            "_labels": {
                "severity": "critical", "job": target["job"], "instance": target["instance"],
                "service": target["service"], "owner": "platform-operations",
                "environment": target["environment"], "cluster": target["cluster"],
            },
            "_annotations": {
                "summary": f"Expected metrics have disappeared for {target['service']} at {target['instance']}.",
                "dashboard_url": f"/d/service-overview?var-service={target['service']}",
                "description": "Check the declared metrics target and its service onboarding receipt.",
            },
            "_is_paused": False,
            "_notification_settings": {
                "receiver": "agent-cloud-ops", "group_by": ["service", "environment", "cluster", "alertname"],
                "group_wait": "30s", "group_interval": "5m", "repeat_interval": "4h",
            },
        }
    return contracts


def _owned_expected_targets(document: Any) -> list[dict[str, Any]]:
    """Read only exact generated expected-target rules from our provisioning group."""
    if not isinstance(document, dict) or document.get("apiVersion") != 1:
        raise Refusal("invalid_previous_rules")
    groups = document.get("groups")
    if not isinstance(groups, list) or len(groups) > MAX_ROWS:
        raise Refusal("invalid_previous_rules")
    owned_groups = [
        group for group in groups
        if isinstance(group, dict)
        and (group.get("orgId"), group.get("name"), group.get("folder")) == (1, "service-telemetry", "agent-cloud")
    ]
    if len(owned_groups) != 1:
        raise Refusal("invalid_previous_rules")
    group = owned_groups[0]
    rules = group.get("rules")
    if not isinstance(rules, list) or len(rules) > MAX_ROWS:
        raise Refusal("invalid_previous_rules")
    owned: list[dict[str, Any]] = []
    for rule in rules:
        if not isinstance(rule, dict) or not str(rule.get("uid", "")).startswith("o11y_missing_"):
            continue
        uid = rule.get("uid")
        labels = rule.get("labels")
        if not isinstance(uid, str) or not EXPECTED_UID.fullmatch(uid):
            raise Refusal("invalid_previous_rules")
        rule_data = rule.get("data")
        if EXPECTED_GROUP_UID.fullmatch(uid):
            if rule.get("title") == _group_tombstone_title(uid):
                if not isinstance(labels, dict):
                    raise Refusal("invalid_previous_rules")
                service, environment, cluster = (labels.get(key) for key in ("service", "environment", "cluster"))
                tombstone_expr = expected_target_group_expression({"targets": [{
                    "uid": "o11y_missing_internal", "job": "tombstone", "instance": "tombstone:1",
                    "service": service, "environment": environment, "cluster": cluster,
                }]})
                group = _group_from_rule({"uid": uid, "labels": labels, "data": [
                    {"model": {"expr": tombstone_expr}}
                ]})
                expected_labels = {"severity": "warning", "service": service,
                                   "environment": environment, "cluster": cluster,
                                   "owner": "platform-operations"}
                if (
                    rule.get("isPaused") is True
                    and "notification_settings" not in rule
                    and rule.get("condition") == "B"
                    and _expected_target_queries_match(rule_data, "vector(0)")
                    and rule.get("noDataState") == "OK" and rule.get("execErrState") == "OK"
                    and rule.get("for") == "0s" and labels == expected_labels
                    and rule.get("annotations") == {
                        "summary": "Expected target group rule is paused.",
                        "description": (
                            "The group UID remains owned and inert for a reviewed rollback or disable transition."
                        ),
                    }
                ):
                    owned.append({**group, "_kind": "group", "_inert": True, "_retired": False})
                    continue
                raise Refusal("invalid_previous_rules")
            group = _group_from_rule(rule)
            expected_labels = {
                "severity": "critical", "job": "{{ $$labels.job }}", "instance": "{{ $$labels.instance }}",
                "service": group["service"], "owner": "platform-operations",
                "environment": group["environment"], "cluster": group["cluster"],
            }
            expected_annotations = {
                "summary": "Expected metrics have disappeared for {{ $labels.service }} at {{ $labels.instance }}.",
                "dashboard_url": f"/d/service-overview?var-service={group['service']}",
                "description": "Check the declared metrics target and its service onboarding receipt.",
            }
            active_group = (
                rule.get("title") == f"Missing telemetry group ({uid})"
                and labels == expected_labels and rule.get("condition") == "B"
                and _expected_target_queries_match(rule_data, group["_expr"])
                and rule.get("noDataState") == "OK" and rule.get("execErrState") == "Alerting"
                and rule.get("for") == "2m" and rule.get("isPaused") in (True, False)
                and rule.get("annotations") == expected_annotations
                and set(rule) == {
                    "uid", "title", "condition", "data", "noDataState", "execErrState", "for",
                    "annotations", "labels", "isPaused",
                } | ({"notification_settings"} if "notification_settings" in rule else set())
                and ("notification_settings" not in rule or rule["notification_settings"] == {
                    "receiver": "agent-cloud-ops", "group_by": ["service", "environment", "cluster", "alertname"],
                    "group_wait": "30s", "group_interval": "5m", "repeat_interval": "4h",
                })
            )
            if not active_group:
                raise Refusal("invalid_previous_rules")
            live_labels = {
                **expected_labels,
                "job": "{{ $labels.job }}",
                "instance": "{{ $labels.instance }}",
            }
            owned.append({**group, "_kind": "group", "_inert": False, "_retired": False,
                          "_title": rule["title"], "_labels": live_labels, "_is_paused": rule["isPaused"],
                          "_annotations": rule["annotations"],
                          "_notification_settings": rule.get("notification_settings")})
            continue
        old_inert_title = rule.get("title") in {
            "Expected metrics target previously declared", "Expected metrics target explicitly retired",
        }
        new_inert_title = rule.get("title") in {_tombstone_title(uid), _tombstone_title(uid, retired=True)}
        if (old_inert_title or new_inert_title) and rule.get("isPaused") is True:
            retired = rule.get("title") in {
                "Expected metrics target explicitly retired", _tombstone_title(uid, retired=True),
            }
            if labels == {"severity": "warning", "owner": "platform-operations", "environment": "disabled"}:
                identity = {}
            else:
                if not isinstance(labels, dict) or set(labels) != {
                    "severity", "job", "instance", "service", "cluster", "owner", "environment",
                }:
                    raise Refusal("invalid_previous_rules")
                identity = {field: labels.get(field) for field in EXPECTED_IDENTITY_FIELDS}
                try:
                    normalized, _ = validate_expected_target_declaration([{"uid": uid, **identity}])
                except Refusal:
                    raise Refusal("invalid_previous_rules") from None
                identity = normalized[0]
            title_ok = old_inert_title or rule.get("title") == _tombstone_title(uid, retired=retired)
            expected_annotations = {
                "summary": "Expected metrics target rule is disabled.",
                "description": "This owned UID was explicitly retired and remains inert." if retired
                else "No target identity is retained while the reviewed alert gate is disabled.",
            }
            if identity and not retired:
                expected_annotations["description"] = "The original target identity remains owned and inert."
            if (
                "notification_settings" not in rule and rule.get("condition") == "B"
                and _expected_target_queries_match(rule_data, "vector(0)")
                and rule.get("noDataState") == "OK" and rule.get("execErrState") == "OK"
                and rule.get("for") == "0s" and rule.get("annotations") == expected_annotations
                and title_ok and rule.get("title") in {_tombstone_title(uid), _tombstone_title(uid, retired=True)}
                or (old_inert_title and rule.get("title") in {
                    "Expected metrics target previously declared", "Expected metrics target explicitly retired",
                } and labels == {"severity": "warning", "owner": "platform-operations", "environment": "disabled"}
                and "notification_settings" not in rule and rule.get("condition") == "B"
                and _expected_target_queries_match(rule_data, "vector(0)") and rule.get("noDataState") == "OK"
                and rule.get("execErrState") == "OK" and rule.get("for") == "0s"
                and rule.get("annotations") == {
                    "summary": "Expected metrics target rule is disabled.",
                    "description": "This owned UID was explicitly retired and remains inert." if retired
                    else "No target identity is retained while the reviewed alert gate is disabled.",
                })
            ):
                owned.append({"uid": uid, "_kind": "target", **identity, "_inert": True, "_retired": retired,
                              "_title": rule.get("title"), "_labels": labels})
                continue
            raise Refusal("invalid_previous_rules")
        if not isinstance(labels, dict):
            raise Refusal("invalid_previous_rules")
        if set(labels) != {"severity", "job", "instance", "service", "owner", "environment", "cluster"}:
            raise Refusal("invalid_previous_rules")
        target = {"uid": uid, **{field: labels.get(field) for field in EXPECTED_IDENTITY_FIELDS}}
        try:
            normalized, _ = validate_expected_target_declaration([target])
        except Refusal:
            raise Refusal("invalid_previous_rules") from None
        value = normalized[0]
        rule_data = rule.get("data")
        expr = (
            rule_data[0].get("model", {}).get("expr")
            if isinstance(rule_data, list) and rule_data and isinstance(rule_data[0], dict)
            else None
        )
        expected_expr = (
            "absent_over_time(up{job=" + json.dumps(value["job"]) + ",instance=" + json.dumps(value["instance"])
            + ",service=" + json.dumps(value["service"]) + ",cluster=" + json.dumps(value["cluster"])
            + ",environment=" + json.dumps(value["environment"]) + "}[5m])"
        )
        paused_contract = False
        expected_annotations = {
            "summary": f"Expected metrics have disappeared for {value['service']} at {value['instance']}.",
            "dashboard_url": f"/d/service-overview?var-service={value['service']}",
            "description": "Check the declared metrics target and its service onboarding receipt.",
        }
        legacy_annotations = {
            **expected_annotations,
            "summary": f"Expected metrics have disappeared for {value['service']}.",
        }
        active_contract = (
            rule.get("title") == f"Missing telemetry for {value['service']} at {value['instance']}"
            and expr == expected_expr
            and rule.get("condition") == "B"
            and _expected_target_queries_match(rule_data, expected_expr)
            and rule.get("noDataState") == "OK"
            and rule.get("execErrState") == "Alerting"
            and rule.get("for") == "2m"
            and rule.get("isPaused") in (True, False)
            and set(rule) == {
                "uid", "title", "condition", "data", "noDataState", "execErrState", "for",
                "annotations", "labels", "isPaused",
            } | ({"notification_settings"} if "notification_settings" in rule else set())
            and rule.get("annotations") in (expected_annotations, legacy_annotations)
            and labels == {
                "severity": "critical", "job": value["job"], "instance": value["instance"],
                "service": value["service"], "owner": "platform-operations",
                "environment": value["environment"], "cluster": value["cluster"],
            }
            and ("notification_settings" not in rule or rule["notification_settings"] == {
                "receiver": "agent-cloud-ops", "group_by": ["service", "environment", "cluster", "alertname"],
                "group_wait": "30s", "group_interval": "5m", "repeat_interval": "4h",
            })
        )
        if not paused_contract and not active_contract:
            raise Refusal("invalid_previous_rules")
        owned.append({
            **value,
            "_kind": "target",
            "_inert": False,
            "_retired": False,
            "_title": rule["title"],
            "_expr": expr,
            "_labels": labels,
            "_is_paused": rule["isPaused"],
            "_notification_settings": rule.get("notification_settings"),
            "_annotations": rule["annotations"],
        })
    if len({row["uid"] for row in owned}) != len(owned):
        raise Refusal("invalid_previous_rules")
    return owned


def prepare_expected_target_transition(input_document: Any) -> dict[str, Any]:
    """Keep owned UIDs inert until explicitly restored; reject live ownership drift."""
    if not isinstance(input_document, dict):
        raise Refusal("invalid_transition")
    if input_document.get("managed") is False:
        return {"status": "prepared", "reason": "local_compatibility", "paused_uids": [], "retired_uids": []}
    previous: list[dict[str, Any]] = []
    previous_raw = input_document.get("previous_rules")
    if previous_raw:
        try:
            previous_doc = yaml.safe_load(previous_raw)
        except Exception:
            raise Refusal("invalid_previous_rules") from None
        previous = _owned_expected_targets(previous_doc)
    check_mode = input_document.get("check_mode") is True
    pending: list[dict[str, Any]] = []
    pending_raw = input_document.get("pending_rules")
    if pending_raw:
        try:
            pending_doc = yaml.safe_load(pending_raw)
        except Exception:
            raise Refusal("invalid_pending_rules") from None
        pending = _owned_expected_targets(pending_doc)
        previous_by_uid = {row["uid"]: row for row in previous}
        pending_by_uid = {row["uid"]: row for row in pending}
        retired_uids = input_document.get("retired_uids", [])
        if not isinstance(retired_uids, list):
            raise Refusal("invalid_pending_rules")
        if input_document.get("enabled") is not True and set(pending_by_uid) != set(previous_by_uid):
            raise Refusal("invalid_pending_rules")
    if check_mode and input_document.get("reviewed") is True:
        validate_expected_target_declaration(
            input_document.get("targets"), input_document.get("retired_uids"), reviewed=True
        )
    live_raw = input_document.get("live_rules")
    grafana_running = input_document.get("grafana_running")
    stopped_preflight = bool(previous and not live_raw and not check_mode and grafana_running is False)
    if previous and not live_raw and not check_mode and not stopped_preflight:
        raise Refusal("live_rules_unavailable")
    if stopped_preflight:
        requested_retired = input_document.get("retired_uids", [])
        if (
            not isinstance(requested_retired, list)
            or any(not isinstance(uid, str) or not EXPECTED_UID.fullmatch(uid) for uid in requested_retired)
            or len(set(requested_retired)) != len(requested_retired)
        ):
            raise Refusal("stopped_grafana_retirement_drift")
        baselines = [previous, *([pending] if pending else [])]
        for baseline in baselines:
            baseline_by_uid = {row["uid"]: row for row in baseline}
            retired_before = {uid for uid, row in baseline_by_uid.items() if row["_retired"]}
            if set(requested_retired) - retired_before:
                raise Refusal("stopped_grafana_retirement_drift")
            if input_document.get("enabled") is True:
                current, _ = validate_expected_target_declaration(
                    input_document.get("targets"), requested_retired, reviewed=True
                )
                current_by_uid = {target["uid"]: target for target in current}
                if input_document.get("mode", "legacy") == "grouped":
                    active_groups = {
                        uid: row for uid, row in baseline_by_uid.items()
                        if row.get("_kind") == "group" and not row["_inert"] and not row["_retired"]
                    }
                    active_targets = {
                        uid: row for uid, row in baseline_by_uid.items()
                        if row.get("_kind") == "target" and not row["_inert"] and not row["_retired"]
                    }
                    if active_groups and not active_targets:
                        expected_groups = expected_target_groups(current)
                        if set(active_groups) != {row["uid"] for row in expected_groups}:
                            raise Refusal("stopped_grafana_target_drift")
                        current_identities = {
                            tuple(target[field] for field in EXPECTED_IDENTITY_FIELDS) for target in current
                        }
                        previous_identities = {
                            tuple(target[field] for field in EXPECTED_IDENTITY_FIELDS)
                            for row in active_groups.values() for target in row["targets"]
                        }
                        if current_identities != previous_identities:
                            raise Refusal("stopped_grafana_target_drift")
                    elif not active_groups and active_targets:
                        current_identities = {
                            tuple(target[field] for field in EXPECTED_IDENTITY_FIELDS) for target in current
                        }
                        previous_identities = {
                            tuple(row[field] for field in EXPECTED_IDENTITY_FIELDS)
                            for row in active_targets.values()
                        }
                        if current_identities != previous_identities:
                            raise Refusal("stopped_grafana_target_drift")
                    else:
                        raise Refusal("stopped_grafana_target_drift")
                    if any(
                        row.get("_kind") == "target" and row["_retired"]
                        for uid, row in baseline_by_uid.items() if uid in current_by_uid
                    ):
                        raise Refusal("stopped_grafana_reactivation")
                else:
                    if set(current_by_uid) & {
                        uid for uid, row in baseline_by_uid.items()
                        if row["_inert"] and not row["_retired"]
                    }:
                        raise Refusal("stopped_grafana_reactivation")
                    active_before = {
                        uid for uid, row in baseline_by_uid.items()
                        if not row["_inert"] and not row["_retired"]
                    }
                    if set(current_by_uid) != active_before:
                        raise Refusal("stopped_grafana_target_drift")
                    for uid, target in current_by_uid.items():
                        old = baseline_by_uid[uid]
                        if any(old[field] != target[field] for field in EXPECTED_IDENTITY_FIELDS):
                            raise Refusal("target_identity_changed")
    if (grafana_running is True and not check_mode) or live_raw:
        try:
            live = json.loads(live_raw)
            folders = json.loads(input_document.get("live_folders", ""))
        except (ValueError, TypeError):
            raise Refusal("invalid_live_rules") from None
        if (
            not isinstance(live, list) or len(live) > MAX_ROWS
            or not isinstance(folders, list) or len(folders) > MAX_ROWS
        ):
            raise Refusal("invalid_live_rules")
        matching_folders = [
            folder for folder in folders
            if isinstance(folder, dict) and folder.get("title") == "agent-cloud"
        ]
        if len(matching_folders) != 1 or not isinstance(matching_folders[0].get("uid"), str):
            raise Refusal("live_folder_drift")
        live_folder_uid = matching_folders[0]["uid"]
        previous_by_uid = {row["uid"]: row for row in previous}
        pending_by_uid = {row["uid"]: row for row in pending}
        live_targets = [
            rule for rule in live
            if isinstance(rule, dict) and isinstance(rule.get("uid"), str)
            and rule["uid"].startswith("o11y_missing_")
        ]
        if len({rule["uid"] for rule in live_targets}) != len(live_targets):
            raise Refusal("invalid_live_rules")
        accepted_live_sets = [set(previous_by_uid)]
        if pending_raw:
            accepted_live_sets.append(set(pending_by_uid))
        if {rule["uid"] for rule in live_targets} not in accepted_live_sets:
            raise Refusal("live_target_drift")
        matches_previous = True
        matches_pending = bool(pending)
        for rule in live_targets:
            target = previous_by_uid.get(rule["uid"])
            pending_target = pending_by_uid.get(rule["uid"])
            if target is None and pending_target is None:
                raise Refusal("live_target_drift")
            if rule.get("ruleGroup") != "service-telemetry" or rule.get("folderUID") != live_folder_uid:
                raise Refusal("live_target_drift")
            if target is None or not _live_rule_matches_owned(rule, target, live_folder_uid):
                matches_previous = False
            if pending_target is None or not _live_rule_matches_owned(rule, pending_target, live_folder_uid):
                matches_pending = False
        if not matches_previous and not matches_pending:
            raise Refusal("live_target_drift")
    retired = input_document.get("retired_uids", [])
    mode = input_document.get("mode", "legacy")
    group_retired = input_document.get("group_retired_uids", [])
    if (
        not isinstance(retired, list)
        or any(not isinstance(uid, str) or not EXPECTED_UID.fullmatch(uid) for uid in retired)
        or len(set(retired)) != len(retired)
        or not isinstance(group_retired, list)
        or any(
            not isinstance(uid, str) or not EXPECTED_GROUP_UID.fullmatch(uid)
            for uid in group_retired
        )
        or len(set(group_retired)) != len(group_retired)
        or mode not in {"grouped", "legacy"}
    ):
        raise Refusal("invalid_transition")
    previous_by_uid = {row["uid"]: row for row in previous}
    if input_document.get("enabled") is not True:
        if set(retired) - set(previous_by_uid):
            raise Refusal("invalid_transition")
        retired_ids = {row["uid"] for row in previous if row["_retired"]} | set(retired)
        tombstone_rows = [
            _as_tombstone(row, retired=row["uid"] in retired_ids)
            for row in previous
        ]
        return {
            "status": "prepared", "reason": "check_mode_unverified" if check_mode else "disabled",
            "paused_uids": [uid for uid in previous_by_uid if uid not in retired_ids],
            "retired_uids": sorted(retired_ids), "tombstones": tombstone_rows,
        }

    current, retired = validate_expected_target_declaration(
        input_document.get("targets"), retired, reviewed=True
    )
    groups = expected_target_groups(current)
    current_target_uids = {row["uid"] for row in current}
    current_group_uids = {row["uid"] for row in groups}
    previous_targets = {row["uid"]: row for row in previous if row.get("_kind") == "target"}
    previous_groups = {row["uid"]: row for row in previous if row.get("_kind") == "group"}
    current_groups_by_uid = {row["uid"]: row for row in groups}
    for uid, previous_group in previous_groups.items():
        current_group = current_groups_by_uid.get(uid)
        if current_group and any(
            previous_group[field] != current_group[field] for field in ("service", "environment", "cluster")
        ):
            raise Refusal("group_uid_collision")
    if any(
        uid in previous_targets and previous_targets[uid].get("job")
        and any(previous_targets[uid][field] != target[field] for field in EXPECTED_IDENTITY_FIELDS)
        for target in current for uid in [target["uid"]]
    ):
        raise Refusal("target_identity_changed")
    already_retired = {uid for uid, row in previous_targets.items() if row["_retired"]}
    if current_target_uids & already_retired:
        raise Refusal("retired_target_reuse")
    if set(retired) - set(previous_targets):
        raise Refusal("unowned_retired_target")
    if set(previous_targets) - current_target_uids - set(retired) - already_retired:
        raise Refusal("unretired_previous_target")
    if mode == "grouped" and group_retired:
        raise Refusal("unexpected_group_retirement")
    if mode == "legacy":
        active_previous_groups = {uid for uid, row in previous_groups.items() if not row["_inert"]}
        if (
            not active_previous_groups.issubset(set(group_retired))
            or set(group_retired) - set(previous_groups)
        ):
            raise Refusal("group_rollback_not_explicit")
        previous_target_identities = {
            tuple(row[field] for field in EXPECTED_IDENTITY_FIELDS)
            for row in previous_targets.values() if row.get("job") and not row["_retired"]
        }
        current_identities = {tuple(row[field] for field in EXPECTED_IDENTITY_FIELDS) for row in current}
        if previous_groups and previous_target_identities and previous_target_identities != current_identities:
            raise Refusal("group_rollback_target_drift")
    else:
        if set(group_retired) - set(previous_groups):
            raise Refusal("unowned_group_retirement")

    retired_ids = already_retired | set(retired)
    if mode == "grouped":
        tombstone_rows = [_as_tombstone({"_kind": "target", **target}) for target in current]
        paused_ids = set(current_target_uids)
        for uid, row in previous_groups.items():
            if uid not in current_group_uids:
                paused_ids.add(uid)
                tombstone_rows.append(_as_tombstone(row))
        # A previously inert group in the current identity may be safely reactivated.
        tombstone_rows.extend(_as_tombstone(row, retired=True) for uid, row in previous_targets.items()
                              if uid in retired_ids and uid not in current_target_uids)
    else:
        tombstone_rows = [_as_tombstone(row) for row in previous_groups.values()]
        paused_ids = set(previous_groups)
        tombstone_rows.extend(_as_tombstone(row, retired=True) for uid, row in previous_targets.items()
                              if uid in retired_ids)
    # Preserve already-retired UIDs and never omit their inert owners.
    for uid, row in previous_targets.items():
        if uid in retired_ids and uid not in {item["uid"] for item in tombstone_rows}:
            tombstone_rows.append(_as_tombstone(row, retired=True))
    return {
        "status": "prepared", "reason": "check_mode_unverified" if check_mode else "enabled",
        "paused_uids": sorted(paused_ids), "retired_uids": sorted(retired_ids),
        "tombstones": sorted(tombstone_rows, key=lambda row: row["uid"]),
    }


def expected_target_coverage(targets_raw: bytes | str, declaration: Any) -> dict[str, Any]:
    """Compare validated expected identities to all active target identities."""
    empty = {
        "expected_count": 0,
        "active_target_count": 0,
        "matched_count": 0,
        "missing_count": 0,
        "unexpected_count": 0,
        "unhealthy_count": 0,
    }
    if not isinstance(declaration, dict) or declaration.get("reviewed") is not True:
        return {"status": "unverified", "reason": "declaration_unreviewed", **empty}
    if declaration.get("targets") == []:
        return {"status": "unverified", "reason": "no_expected_targets", **empty}
    try:
        expected, _retired = validate_expected_target_declaration(
            declaration.get("targets"), reviewed=declaration.get("reviewed")
        )
    except Refusal:
        return {"status": "unverified", "reason": "invalid_declaration", **empty}
    if not expected:
        return {"status": "unverified", "reason": "no_expected_targets", **empty}

    try:
        response = _json_document(targets_raw, "malformed_targets")
        data = response.get("data")
        active = data.get("activeTargets") if isinstance(data, dict) else None
        if not isinstance(active, list) or len(active) > MAX_ROWS:
            raise Refusal("malformed_targets")

        expected_ids = {
            tuple(target[field] for field in EXPECTED_IDENTITY_FIELDS)
            for target in expected
        }
        active_ids: set[tuple[str, ...]] = set()
        unhealthy_count = 0
        for target in active:
            if not isinstance(target, dict) or target.get("health") not in {"up", "down", "unknown"}:
                raise Refusal("malformed_targets")
            labels = target.get("labels")
            if not isinstance(labels, dict):
                raise Refusal("malformed_targets")
            identity = tuple(labels.get(field) for field in EXPECTED_IDENTITY_FIELDS)
            if any(not isinstance(value, str) or not SAFE_IDENTITY_VALUE.fullmatch(value) for value in identity):
                raise Refusal("malformed_identity")
            if identity in active_ids:
                raise Refusal("ambiguous_target_identity")
            active_ids.add(identity)
            unhealthy_count += target["health"] != "up"

        matched = len(expected_ids & active_ids)
        missing = len(expected_ids - active_ids)
        unexpected = len(active_ids - expected_ids)
        identity_drift = missing > 0 or unexpected > 0
        if identity_drift and unhealthy_count:
            reason = "identity_drift_and_unhealthy_targets"
        elif identity_drift:
            reason = "identity_drift"
        elif unhealthy_count:
            reason = "unhealthy_targets"
        else:
            reason = "exact_match"
        return {
            "status": "observed",
            "reason": reason,
            "expected_count": len(expected_ids),
            "active_target_count": len(active_ids),
            "matched_count": matched,
            "missing_count": missing,
            "unexpected_count": unexpected,
            "unhealthy_count": unhealthy_count,
        }
    except Refusal as error:
        reason = "ambiguous_target_identity" if error.category == "ambiguous_target_identity" else "malformed_targets"
        return {"status": "unavailable", "reason": reason, **empty}
    except Exception:
        return {"status": "unavailable", "reason": "survey_unavailable", **empty}


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _public_job(job: str) -> str:
    if job in JOB_ALIASES:
        return JOB_ALIASES[job]
    if HOST_JOB.fullmatch(job):
        return "host-node"
    raise Refusal("unrecognized_job")


def _identity(labels: Any, category: str) -> tuple[str, str]:
    if not isinstance(labels, dict):
        raise Refusal(category)
    job, instance = labels.get("job"), labels.get("instance")
    if not isinstance(job, str) or not job or not isinstance(instance, str) or not instance:
        raise Refusal(category)
    return job, instance


def _json_document(raw: bytes | str, category: str) -> dict[str, Any]:
    if not isinstance(raw, (bytes, str)) or not raw or len(raw) > MAX_API_BYTES:
        raise Refusal(category)
    try:
        value = json.loads(
            raw,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
            object_pairs_hook=_unique_object,
        )
    except (ValueError, TypeError, json.JSONDecodeError):
        raise Refusal(category) from None
    if not isinstance(value, dict) or value.get("status") != "success":
        raise Refusal(category)
    for field in ("warnings", "infos"):
        if field in value and value[field]:
            raise Refusal(category)
    return value


def _api_vector(raw: bytes, category: str) -> list[dict[str, Any]]:
    response = _json_document(raw, category)
    data = response.get("data")
    if not isinstance(data, dict) or data.get("resultType") != "vector":
        raise Refusal(category)
    rows = data.get("result")
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        raise Refusal(category)
    return rows


def _declared_instance(instance: str) -> tuple[str, int]:
    try:
        if not instance or "\\" in instance or any(char.isspace() or ord(char) < 0x20 for char in instance):
            raise ValueError
        parsed = urlsplit("http://" + instance)
        if parsed.username is not None or parsed.password is not None:
            raise ValueError
        host = parsed.hostname
        port = parsed.port
        if not host or port is None or not 1 <= port <= 65535 or parsed.path or parsed.query or parsed.fragment:
            raise ValueError
        return _normal_host(host), port
    except (ValueError, UnicodeError):
        raise Refusal("unsafe_target_url") from None


def _normal_host(host: str) -> str:
    if "%" in host or host.endswith("."):
        raise ValueError("noncanonical host")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if NUMERIC_HOST.fullmatch(host):
            raise ValueError("noncanonical numeric host") from None
        return host.encode("idna").decode("ascii").lower()
    canonical = address.compressed.lower()
    if str(address) != host and (address.version == 4 or canonical != host.lower()):
        raise ValueError("noncanonical IP address")
    return canonical


def _is_loopback_host(host: str) -> bool:
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
        return address.is_loopback or (
            isinstance(address, ipaddress.IPv6Address)
            and address.ipv4_mapped is not None
            and address.ipv4_mapped.is_loopback
        )
    except ValueError:
        return False


def validate_target_url(url: Any, instance: str) -> tuple[str, int, str]:
    """Require a plain HTTP URL whose authority exactly matches instance."""
    if not isinstance(url, str) or not url or len(url) > 2048:
        raise Refusal("unsafe_target_url")
    if "\\" in url or any(char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F for char in url):
        raise Refusal("unsafe_target_url")
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme.lower() != "http"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or not parsed.path.startswith("/")
        ):
            raise ValueError
        host = parsed.hostname
        port = parsed.port
        if not host:
            raise ValueError
        normalized_host = _normal_host(host)
        normalized_port = port if port is not None else 80
        if not 1 <= normalized_port <= 65535:
            raise ValueError
        if (normalized_host, normalized_port) != _declared_instance(instance):
            raise ValueError
        # Keep URL private; return normalized authority for duplicate detection only.
        return normalized_host, normalized_port, parsed.path
    except (ValueError, UnicodeError):
        raise Refusal("unsafe_target_url") from None


def _targets(raw: bytes) -> dict[tuple[str, str], tuple[str, str]]:
    response = _json_document(raw, "malformed_targets")
    data = response.get("data")
    active = data.get("activeTargets") if isinstance(data, dict) else None
    if not isinstance(active, list) or len(active) > MAX_ROWS:
        raise Refusal("malformed_targets")
    if not active:
        raise Refusal("no_active_targets")

    result: dict[tuple[str, str], tuple[str, str]] = {}
    urls: set[tuple[str, int, str]] = set()
    for target in active:
        if not isinstance(target, dict):
            raise Refusal("malformed_targets")
        job, instance = _identity(target.get("labels"), "malformed_targets")
        category = _public_job(job)
        if target.get("health") != "up":
            raise Refusal("unhealthy_target")
        original_url = target.get("scrapeUrl")
        url_key = validate_target_url(original_url, instance)
        probe_url = original_url
        if _is_loopback_host(url_key[0]):
            if (
                job == "prometheus"
                and instance == PROMETHEUS_SELF_INSTANCE
                and original_url == PROMETHEUS_SELF_URL
            ):
                probe_url = PROMETHEUS_SELF_PROBE_URL
            else:
                raise Refusal("unsafe_target_url")
        key = (job, instance)
        if key in result or url_key in urls:
            raise Refusal("ambiguous_targets")
        result[key] = (category, probe_url)
        urls.add(url_key)
    return result


def _positive_integer(value: Any, category: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise Refusal(category)
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise Refusal(category) from None
    if not number.is_finite() or number <= 0 or number != number.to_integral_value():
        raise Refusal(category)
    if len(number.as_tuple().digits) > MAX_INTEGER_DIGITS or number.adjusted() >= MAX_INTEGER_DIGITS:
        raise Refusal(category)
    return int(number)


def _samples(raw: bytes, targets: dict[tuple[str, str], tuple[str, str]]) -> dict[tuple[str, str], int]:
    result: dict[tuple[str, str], int] = {}
    for row in _api_vector(raw, "malformed_samples"):
        if not isinstance(row, dict):
            raise Refusal("malformed_samples")
        labels = row.get("metric")
        job, instance = _identity(labels, "malformed_samples")
        key = (job, instance)
        if key not in targets:
            continue  # 24-hour data for a target that is no longer active.
        value = row.get("value")
        if not isinstance(value, list) or len(value) != 2:
            raise Refusal("malformed_samples")
        if key in result:
            raise Refusal("ambiguous_metric")
        result[key] = _positive_integer(value[1], "malformed_samples")
    if set(result) != set(targets):
        raise Refusal("missing_samples")
    return result


def _label_series(raw: bytes, key: tuple[str, str]) -> tuple[int, int, int]:
    response = _json_document(raw, "malformed_series")
    rows = response.get("data")
    if not isinstance(rows, list) or not rows or len(rows) > MAX_ROWS:
        raise Refusal("missing_series" if rows == [] else "malformed_series")

    seen: set[tuple[tuple[str, str], ...]] = set()
    max_count = 0
    max_name_bytes = 0
    max_value_bytes = 0
    for labels in rows:
        if not isinstance(labels, dict) or not labels:
            raise Refusal("malformed_series")
        if labels.get("job") != key[0] or labels.get("instance") != key[1]:
            raise Refusal("ambiguous_series")
        if not isinstance(labels.get("__name__"), str) or not labels["__name__"]:
            raise Refusal("malformed_series")
        if any(not isinstance(name, str) or not name or not isinstance(value, str) for name, value in labels.items()):
            raise Refusal("malformed_series")
        identity = tuple(sorted(labels.items()))
        if identity in seen:
            raise Refusal("ambiguous_series")
        seen.add(identity)
        try:
            max_count = max(max_count, len(labels))
            max_name_bytes = max(max_name_bytes, *(len(name.encode("utf-8")) for name in labels))
            max_value_bytes = max(max_value_bytes, *(len(value.encode("utf-8")) for value in labels.values()))
        except UnicodeEncodeError:
            raise Refusal("malformed_series") from None
    if min(max_count, max_name_bytes, max_value_bytes) <= 0:
        raise Refusal("malformed_series")
    return max_count, max_name_bytes, max_value_bytes


def _series_selector(job: str, instance: str) -> str:
    return "{job=" + json.dumps(job, ensure_ascii=True) + ",instance=" + json.dumps(instance, ensure_ascii=True) + "}"


def _series_config_url(job: str, instance: str, start: int, end: int) -> str:
    query = urlencode(
        [
            ("match[]", _series_selector(job, instance)),
            ("start", str(start)),
            ("end", str(end)),
        ]
    )
    return PROMETHEUS + "/api/v1/series?" + query


def _private_api_config(url: str) -> bytes:
    lines = [
        "url = " + json.dumps(url),
        'header = "Accept-Encoding: identity"',
        'proxy = ""',
        'noproxy = "*"',
        'proto = "=http"',
        "globoff",
        f"connect-timeout = {CONNECT_TIMEOUT}",
        'max-time = 15',
        f"max-filesize = {MAX_API_BYTES}",
        "max-redirs = 0",
        'output = "-"',
        'write-out = "%{http_code}"',
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _run_series_query(job: str, instance: str, start: int, end: int) -> bytes:
    config = _private_api_config(_series_config_url(job, instance, start, end))
    argv = [ENGINE, "exec", "-i", CONTAINER, "curl", "--disable", "--silent", "--show-error", "--fail", "--config", "-"]
    try:
        process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        raise Refusal("series_unavailable") from None
    assert process.stdin is not None and process.stdout is not None
    try:
        process.stdin.write(config)
        process.stdin.close()
    except OSError:
        process.kill()
        process.wait()
        raise Refusal("series_unavailable") from None

    state: dict[str, Any] = {"output": bytearray(), "overflow": False, "error": False}

    def consume() -> None:
        try:
            while True:
                chunk = process.stdout.read(64 * 1024)
                if not chunk:
                    return
                remaining = MAX_API_BYTES + 3 - len(state["output"])
                if len(chunk) > remaining:
                    state["overflow"] = True
                    process.kill()
                    return
                state["output"].extend(chunk)
        except OSError:
            state["error"] = True

    reader = threading.Thread(target=consume, daemon=True)
    reader.start()
    reader.join(20)
    if reader.is_alive():
        process.kill()
        reader.join(1)
        state["error"] = True
    try:
        return_code = process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        return_code = -1
    output = bytes(state["output"])
    if state["overflow"] or state["error"] or return_code != 0 or len(output) < 3:
        raise Refusal("series_unavailable")
    status_text = output[-3:]
    if not status_text.isdigit() or not 200 <= int(status_text) < 300:
        raise Refusal("series_unavailable")
    return output[:-3]


def _response_headers(raw: bytes) -> tuple[int, dict[str, list[str]], bytes]:
    marker = raw.find(b"\r\n\r\n")
    if marker < 0:
        raise Refusal("probe_unavailable")
    block = raw[:marker]
    if len(block) > MAX_RESPONSE_HEADERS:
        raise Refusal("probe_unavailable")
    lines = block.split(b"\r\n")
    status_match = STATUS_LINE.fullmatch(lines[0]) if lines else None
    if status_match is None:
        raise Refusal("probe_unavailable")
    status = int(status_match.group(1))
    headers: dict[str, list[str]] = {}
    for line in lines[1:]:
        if b":" not in line:
            raise Refusal("probe_unavailable")
        name, value = line.split(b":", 1)
        try:
            key = name.decode("ascii").strip().lower()
            text = value.decode("latin-1").strip().lower()
        except UnicodeDecodeError:
            raise Refusal("probe_unavailable") from None
        if not key:
            raise Refusal("probe_unavailable")
        headers.setdefault(key, []).append(text)
    return status, headers, raw[marker + 4 :]


def _curl_config(url: str) -> bytes:
    # URL is deliberately supplied through stdin, never through process argv.
    lines = [
        "url = " + json.dumps(url),
        'header = "Accept-Encoding: identity"',
        'proxy = ""',
        'noproxy = "*"',
        'proto = "=http"',
        "globoff",
        f"connect-timeout = {CONNECT_TIMEOUT}",
        f"max-time = {TRANSFER_TIMEOUT}",
        f"max-filesize = {MAX_RESPONSE_BYTES}",
        "max-redirs = 0",
        "dump-header = \"-\"",
        'output = "-"',
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _probe_command() -> list[str]:
    return [ENGINE, "exec", "-i", CONTAINER, "curl", "--disable", "--config", "-"]


def _run_probe(url: str) -> tuple[int, int]:
    config = _curl_config(url)
    try:
        process = subprocess.Popen(
            _probe_command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        raise Refusal("probe_unavailable") from None
    assert process.stdin is not None and process.stdout is not None
    try:
        process.stdin.write(config)
        process.stdin.close()
    except OSError:
        process.kill()
        process.wait()
        raise Refusal("probe_unavailable") from None

    state: dict[str, Any] = {"headers": bytearray(), "body_bytes": 0, "status": None, "error": None}

    def consume() -> None:
        try:
            while True:
                chunk = process.stdout.read(64 * 1024)
                if not chunk:
                    break
                if state["status"] is None:
                    header_bytes = state["headers"]
                    header_bytes.extend(chunk)
                    marker = header_bytes.find(b"\r\n\r\n")
                    if marker < 0:
                        if len(header_bytes) > MAX_RESPONSE_HEADERS:
                            state["error"] = "probe_unavailable"
                            process.kill()
                            return
                        continue
                    status, headers, body = _response_headers(bytes(header_bytes))
                    state["status"] = status
                    state["headers"] = bytearray()
                    encodings = headers.get("content-encoding", [])
                    lengths = headers.get("content-length", [])
                    transfers = headers.get("transfer-encoding", [])
                    if status < 200 or status >= 300:
                        state["error"] = "probe_http_status"
                        process.kill()
                        return
                    if len(encodings) > 1 or any(value not in ("", "identity") for value in encodings):
                        state["error"] = "probe_compressed"
                        process.kill()
                        return
                    if len(transfers) > 1 or any(value != "chunked" for value in transfers):
                        state["error"] = "probe_compressed"
                        process.kill()
                        return
                    if len(lengths) > 1 or (lengths and not DECIMAL.fullmatch(lengths[0])):
                        state["error"] = "probe_unavailable"
                        process.kill()
                        return
                    if lengths and int(lengths[0]) > MAX_RESPONSE_BYTES:
                        state["error"] = "probe_too_large"
                        process.kill()
                        return
                    state["body_bytes"] = len(body)
                else:
                    state["body_bytes"] += len(chunk)
                if state["body_bytes"] > MAX_RESPONSE_BYTES:
                    state["error"] = "probe_too_large"
                    process.kill()
                    return
        except (OSError, Refusal):
            state["error"] = "probe_unavailable"
            try:
                process.kill()
            except OSError:
                pass

    reader = threading.Thread(target=consume, daemon=True)
    reader.start()
    reader.join(TRANSFER_TIMEOUT + CONNECT_TIMEOUT + 2)
    if reader.is_alive():
        process.kill()
        reader.join(1)
        state["error"] = "probe_timeout"
    try:
        return_code = process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        return_code = -1
    if state["error"]:
        raise Refusal(state["error"])
    if state["status"] is None or return_code != 0:
        if state["body_bytes"] >= MAX_RESPONSE_BYTES:
            raise Refusal("probe_too_large")
        raise Refusal("probe_unavailable")
    if state["body_bytes"] > MAX_RESPONSE_BYTES:
        raise Refusal("probe_too_large")
    if state["body_bytes"] == 0:
        raise Refusal("missing_probe_data")
    return state["status"], state["body_bytes"]


def summarize(
    targets_raw: bytes | str,
    samples_raw: bytes | str,
    probe: Callable[[str], tuple[int, int]],
    series_query: Callable[[str, str, int, int], bytes],
    series_start: int,
    series_end: int,
) -> dict[str, Any]:
    if (
        isinstance(series_start, bool)
        or isinstance(series_end, bool)
        or not isinstance(series_start, int)
        or not isinstance(series_end, int)
        or series_end - series_start != 86400
    ):
        raise Refusal("malformed_series")
    if isinstance(targets_raw, str):
        targets_raw = targets_raw.encode()
    if isinstance(samples_raw, str):
        samples_raw = samples_raw.encode()
    target_map = _targets(targets_raw)
    sample_index = _samples(samples_raw, target_map)
    label_index: dict[tuple[str, str], tuple[int, int, int]] = {}
    for job, instance in target_map:
        series_raw = series_query(job, instance, series_start, series_end)
        label_index[(job, instance)] = _label_series(series_raw, (job, instance))

    totals: dict[str, dict[str, Any]] = {}
    for key, (public_job, url) in target_map.items():
        status, response_bytes = probe(url)
        if status < 200 or status >= 300:
            raise Refusal("probe_http_status")
        if isinstance(response_bytes, bool) or not isinstance(response_bytes, int):
            raise Refusal("probe_unavailable")
        if response_bytes == 0:
            raise Refusal("missing_probe_data")
        if response_bytes < 0 or response_bytes > MAX_RESPONSE_BYTES:
            raise Refusal("probe_too_large")
        row = totals.setdefault(
            public_job,
            {
                "job": public_job,
                "active_target_count": 0,
                "health": {"healthy": 0, "unhealthy": 0},
                "max_scrape_samples_scraped_24h": 0,
                "max_scrape_response_body_bytes_point_in_time": 0,
                "max_stored_series_label_count_24h": 0,
                "max_stored_series_label_name_bytes_24h": 0,
                "max_stored_series_label_value_bytes_24h": 0,
            },
        )
        row["active_target_count"] += 1
        row["health"]["healthy"] += 1
        row["max_scrape_samples_scraped_24h"] = max(row["max_scrape_samples_scraped_24h"], sample_index[key])
        row["max_scrape_response_body_bytes_point_in_time"] = max(
            row["max_scrape_response_body_bytes_point_in_time"], response_bytes
        )
        row["max_stored_series_label_count_24h"] = max(
            row["max_stored_series_label_count_24h"], label_index[key][0]
        )
        row["max_stored_series_label_name_bytes_24h"] = max(
            row["max_stored_series_label_name_bytes_24h"], label_index[key][1]
        )
        row["max_stored_series_label_value_bytes_24h"] = max(
            row["max_stored_series_label_value_bytes_24h"], label_index[key][2]
        )
    return {
        "status": "observed",
        "active_target_count": len(target_map),
        "jobs": [totals[name] for name in sorted(totals)],
    }


def _run_api(path: str, unavailable_reason: str = "query_unavailable") -> bytes:
    argv = [
        ENGINE,
        "exec",
        CONTAINER,
        "curl",
        "--disable",
        "--silent",
        "--show-error",
        "--fail",
        "--connect-timeout",
        str(CONNECT_TIMEOUT),
        "--max-time",
        "15",
        "--max-filesize",
        str(MAX_API_BYTES),
        "--proxy",
        "",
        "--noproxy",
        "*",
        "--write-out",
        "%{http_code}",
        PROMETHEUS + path,
    ]
    try:
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        raise Refusal(unavailable_reason) from None

    assert process.stdout is not None
    state: dict[str, Any] = {"output": bytearray(), "overflow": False, "error": False}

    def consume() -> None:
        try:
            while True:
                chunk = process.stdout.read(64 * 1024)
                if not chunk:
                    return
                remaining = MAX_API_BYTES + 3 - len(state["output"])
                if len(chunk) > remaining:
                    state["overflow"] = True
                    process.kill()
                    return
                state["output"].extend(chunk)
        except OSError:
            state["error"] = True

    reader = threading.Thread(target=consume, daemon=True)
    reader.start()
    reader.join(20)
    if reader.is_alive():
        process.kill()
        reader.join(1)
        state["error"] = True
    try:
        return_code = process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        return_code = -1
    output = bytes(state["output"])
    if state["overflow"] or state["error"] or return_code != 0 or len(output) < 3:
        raise Refusal(unavailable_reason)
    status_text = output[-3:]
    if not status_text.isdigit() or not 200 <= int(status_text) < 300:
        raise Refusal(unavailable_reason)
    return output[:-3]


def _status_data(path: str, unavailable_reason: str) -> dict[str, Any]:
    response = _json_document(_run_api(path, unavailable_reason), unavailable_reason)
    data = response.get("data")
    if not isinstance(data, dict):
        raise Refusal(unavailable_reason)
    return data


def _runtime_signature(data: Any) -> tuple[str, str]:
    if not isinstance(data, dict) or data.get("reloadConfigSuccess") is not True:
        raise Refusal("runtime_config_unavailable")
    start_time = data.get("startTime")
    config_time = data.get("lastConfigTime")
    if not isinstance(start_time, str) or not isinstance(config_time, str):
        raise Refusal("runtime_config_unavailable")
    try:
        start = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
        config = datetime.fromisoformat(config_time.replace("Z", "+00:00"))
    except ValueError:
        raise Refusal("runtime_config_unavailable") from None
    if start.tzinfo is None or config.tzinfo is None:
        raise Refusal("runtime_config_unavailable")
    return start_time, config_time


def _active_target_pools(targets_raw: bytes | str) -> list[dict[str, str]]:
    response = _json_document(targets_raw, "malformed_targets")
    data = response.get("data")
    active = data.get("activeTargets") if isinstance(data, dict) else None
    if not isinstance(active, list) or not active or len(active) > MAX_ROWS:
        raise Refusal("malformed_targets")
    pools: dict[str, str] = {}
    for target in active:
        if not isinstance(target, dict):
            raise Refusal("malformed_targets")
        pool = target.get("scrapePool")
        labels = target.get("labels")
        job = labels.get("job") if isinstance(labels, dict) else None
        if not isinstance(pool, str) or not pool or not isinstance(job, str) or not job:
            raise Refusal("ambiguous_target_pool")
        try:
            pool_category = _public_job(pool)
            job_category = _public_job(job)
        except Refusal:
            raise Refusal("ambiguous_target_pool") from None
        if pool_category != job_category or (pool in pools and pools[pool] != job):
            raise Refusal("ambiguous_target_pool")
        pools[pool] = job
    return [{"pool": pool, "job": pools[pool]} for pool in sorted(pools)]


def _loaded_limit_readback(targets_raw: bytes | str) -> dict[str, Any]:
    build = _status_data("/api/v1/status/buildinfo", "buildinfo_unavailable")
    if build.get("version") != "3.1.0":
        raise Refusal("prometheus_version_mismatch")

    runtime_before = _runtime_signature(
        _status_data("/api/v1/status/runtimeinfo", "runtime_config_unavailable")
    )
    config_before = _status_data("/api/v1/status/config", "loaded_config_unavailable").get("yaml")
    runtime_middle = _runtime_signature(
        _status_data("/api/v1/status/runtimeinfo", "runtime_config_unavailable")
    )
    config_after = _status_data("/api/v1/status/config", "loaded_config_unavailable").get("yaml")
    runtime_after = _runtime_signature(
        _status_data("/api/v1/status/runtimeinfo", "runtime_config_unavailable")
    )

    for config_yaml in (config_before, config_after):
        if not isinstance(config_yaml, str) or not config_yaml:
            raise Refusal("loaded_config_unavailable")
        try:
            config_size = len(config_yaml.encode("utf-8"))
        except UnicodeError:
            raise Refusal("loaded_config_unavailable") from None
        if config_size > MAX_API_BYTES:
            raise Refusal("loaded_config_unavailable")

    if (
        config_before != config_after
        or runtime_before != runtime_middle
        or runtime_middle != runtime_after
    ):
        raise Refusal("unstable_loaded_config")

    return {
        "status": "observed",
        "reason": "stable_loaded_config",
        "config_yaml": config_after,
        "target_pools": _active_target_pools(targets_raw),
    }


def _alert_input_family(
    raw: bytes,
    metric_name: str,
    job: str,
    active_targets: set[tuple[str, str]],
) -> dict[str, str]:
    rows = _api_vector(raw, "malformed_alert_response")
    if not rows:
        return {"status": "missing_or_stale", "reason": "no_samples"}

    seen: set[tuple[tuple[str, str], ...]] = set()
    values: list[Decimal] = []
    for row in rows:
        if not isinstance(row, dict):
            raise Refusal("malformed_alert_response")
        labels = row.get("metric")
        if (
            not isinstance(labels, dict)
            or labels.get("__name__") != metric_name
            or any(not isinstance(name, str) or not isinstance(value, str) for name, value in labels.items())
        ):
            raise Refusal("malformed_alert_response")
        key = (labels.get("job"), labels.get("instance"))
        if not all(isinstance(value, str) and value for value in key):
            raise Refusal("malformed_alert_response")
        if key[0] != job or key not in active_targets:
            raise Refusal("ambiguous_alert_target")
        identity = tuple(sorted(labels.items()))
        if identity in seen:
            raise Refusal("ambiguous_alert_series")
        seen.add(identity)

        sample = row.get("value")
        if not isinstance(sample, list) or len(sample) != 2 or isinstance(sample[0], bool):
            raise Refusal("malformed_alert_sample")
        try:
            evaluation_timestamp = Decimal(str(sample[0]))
            value = Decimal(str(sample[1]))
        except (InvalidOperation, ValueError):
            raise Refusal("malformed_alert_sample") from None
        if not evaluation_timestamp.is_finite() or not value.is_finite() or value < 0:
            raise Refusal("malformed_alert_sample")
        values.append(value)

    return {
        "status": "observed_nonzero" if any(value > 0 for value in values) else "observed_zero",
        "reason": "samples_present",
    }


def _alert_input_availability(targets_raw: bytes | str) -> dict[str, Any]:
    try:
        targets = _targets(targets_raw.encode() if isinstance(targets_raw, str) else targets_raw)
    except Exception:
        return {
            "status": "observed",
            "families": [
                {"family": metric_name, "status": "unavailable", "reason": "target_unavailable"}
                for metric_name, _ in ALERT_INPUTS
            ],
        }
    active_by_job: dict[str, set[tuple[str, str]]] = {}
    for job, instance in targets:
        if job in {source_job for _, source_job in ALERT_INPUTS}:
            active_by_job.setdefault(job, set()).add((job, instance))

    families: list[dict[str, str]] = []
    for metric_name, job in ALERT_INPUTS:
        try:
            if not active_by_job.get(job):
                raise Refusal("alert_target_unavailable")
            query = f'{metric_name}{{job="{job}"}}'
            params = {"query": query, "lookback_delta": "5m"}
            raw = _run_api("/api/v1/query?" + urlencode(params), "alert_query_unavailable")
            result = _alert_input_family(raw, metric_name, job, active_by_job[job])
        except Refusal as error:
            reason = {
                "malformed_alert_response": "malformed_response",
                "malformed_alert_sample": "malformed_sample",
                "ambiguous_alert_target": "ambiguous_target",
                "ambiguous_alert_series": "ambiguous_series",
                "alert_target_unavailable": "target_unavailable",
                "alert_query_unavailable": "api_unavailable",
            }.get(error.category, "malformed_response")
            result = {"status": "unavailable", "reason": reason}
        except Exception:
            result = {"status": "unavailable", "reason": "api_unavailable"}
        families.append({"family": metric_name, **result})
    return {"status": "observed", "families": families}


def _require_curl_version() -> None:
    try:
        completed = subprocess.run(
            [ENGINE, "exec", CONTAINER, "curl", "--disable", "--version"],
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise Refusal("curl_unsupported") from None
    match = VERSION.search(completed.stdout) if completed.returncode == 0 else None
    if match is None or tuple(int(piece) for piece in match.groups()) < CURL_MIN_VERSION:
        raise Refusal("curl_unsupported")


def collect() -> dict[str, Any]:
    _require_curl_version()
    targets_raw = _run_api("/api/v1/targets?state=active")
    samples_raw = _run_api("/api/v1/query?query=max_over_time%28scrape_samples_scraped%5B24h%5D%29")
    series_end = int(time.time())
    baseline = summarize(
        targets_raw,
        samples_raw,
        _run_probe,
        _run_series_query,
        series_end - 86400,
        series_end,
    )
    try:
        loaded_limits = _loaded_limit_readback(targets_raw)
    except Refusal as error:
        loaded_limits = {"status": "unavailable", "reason": error.category}
    except Exception:
        loaded_limits = {"status": "unavailable", "reason": "loaded_readback_unavailable"}
    try:
        alert_inputs = _alert_input_availability(targets_raw)
    except Exception:
        alert_inputs = {
            "status": "observed",
            "families": [
                {"family": metric_name, "status": "unavailable", "reason": "api_unavailable"}
                for metric_name, _ in ALERT_INPUTS
            ],
        }
    try:
        source_limits = _source_scrape_limit_readback(os.environ.get("O11Y_SCRAPE_CONFIG_DIR", ""))
    except Refusal as error:
        source_limits = {"status": "unavailable", "reason": error.category}
    except Exception:
        source_limits = {"status": "unavailable", "reason": "source_readback_unavailable"}
    return {
        "baseline": baseline,
        "loaded_limit_readback": loaded_limits,
        "source_limit_readback": source_limits,
        "alert_input_availability": alert_inputs,
    }


def _input_document() -> dict[str, Any]:
    raw = sys.stdin.buffer.read(1_048_577)
    if len(raw) > 1_048_576:
        raise Refusal("invalid_declaration")
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (ValueError, TypeError, json.JSONDecodeError):
        raise Refusal("invalid_declaration") from None
    if not isinstance(value, dict):
        raise Refusal("invalid_declaration")
    return value


def _expected_target_coverage_action() -> dict[str, Any]:
    declaration = _input_document()
    if declaration.get("reviewed") is not True:
        return expected_target_coverage(b"", declaration)
    try:
        validate_expected_target_declaration(
            declaration.get("targets"), reviewed=declaration.get("reviewed")
        )
    except Refusal:
        return expected_target_coverage(b"", declaration)
    try:
        targets_raw = _run_api("/api/v1/targets?state=active", "api_unavailable")
    except Refusal:
        return {
            "status": "unavailable",
            "reason": "api_unavailable",
            "expected_count": 0,
            "active_target_count": 0,
            "matched_count": 0,
            "missing_count": 0,
            "unexpected_count": 0,
            "unhealthy_count": 0,
        }
    return expected_target_coverage(targets_raw, declaration)


def main() -> int:
    if len(sys.argv) == 2 and sys.argv[1] == "--validate-active-expected-target-rules":
        try:
            result = validate_active_expected_target_rules(_input_document())
            code = 0
        except Exception:
            result = {"status": "refused", "reason": "unsafe_alert_readback"}
            code = 2
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return code
    if len(sys.argv) == 2 and sys.argv[1] == "--prepare-expected-target-transition":
        try:
            result = prepare_expected_target_transition(_input_document())
            code = 0
        except Refusal:
            result = {"status": "refused", "reason": "unsafe_alert_transition"}
            code = 2
        except Exception:
            result = {"status": "refused", "reason": "unsafe_alert_transition"}
            code = 2
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return code
    if len(sys.argv) == 2 and sys.argv[1] == "--validate-expected-targets":
        try:
            declaration = _input_document()
            targets, _retired = validate_expected_target_declaration(
                declaration.get("targets"),
                declaration.get("retired_uids"),
                reviewed=declaration.get("reviewed"),
            )
            result = {"status": "valid", "reason": "declaration_valid"}
            code = 0
        except Refusal:
            result = {"status": "refused", "reason": "invalid_declaration"}
            code = 2
        except Exception:
            result = {"status": "refused", "reason": "invalid_declaration"}
            code = 2
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return code
    if len(sys.argv) == 2 and sys.argv[1] == "--group-expected-targets":
        try:
            declaration = _input_document()
            targets, _retired = validate_expected_target_declaration(
                declaration.get("targets"), declaration.get("retired_uids"),
                reviewed=declaration.get("reviewed"),
            )
            groups = expected_target_groups(targets)
            for group in groups:
                group["expr"] = expected_target_group_expression(group)
            result = {"groups": groups}
            code = 0
        except Exception:
            result = {"groups": []}
            code = 2
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return code
    if len(sys.argv) == 2 and sys.argv[1] == "--expected-target-coverage":
        try:
            result = _expected_target_coverage_action()
        except Exception:
            result = {
                "status": "unavailable",
                "reason": "survey_unavailable",
                "expected_count": 0,
                "active_target_count": 0,
                "matched_count": 0,
                "missing_count": 0,
                "unexpected_count": 0,
                "unhealthy_count": 0,
            }
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    try:
        receipt = collect()
    except Refusal as error:
        print(json.dumps({"status": "unavailable", "reason": error.category}, sort_keys=True))
        return 2
    except Exception:
        # Never print subprocess details, target input, or an exception chain.
        print(json.dumps({"status": "unavailable", "reason": "survey_unavailable"}, sort_keys=True))
        return 2
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
