#!/usr/bin/env python3
"""Collect private scrape evidence and emit only a bounded public receipt."""

from __future__ import annotations

import ipaddress
import json
import re
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
SAFE_IDENTITY_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@-]{0,127}\Z")
MAX_EXPECTED_TARGETS = 100

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
    return normalized, list(retired_uids)


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
    if target["_inert"]:
        retired = target["_retired"]
        return (
            rule.get("title") == (
                "Expected metrics target explicitly retired"
                if retired else "Expected metrics target previously declared"
            )
            and rule.get("condition") == "B"
            and rule.get("isPaused") is True
            and rule.get("labels") == {
                "severity": "warning", "owner": "platform-operations", "environment": "disabled",
            }
            and _expected_target_queries_match(rule.get("data"), "vector(0)")
            and rule.get("noDataState") == "OK"
            and rule.get("execErrState") == "OK"
            and rule.get("for") == "0s"
            and rule.get("annotations") == {
                "summary": "Expected metrics target rule is disabled.",
                "description": (
                    "This owned UID was explicitly retired and remains inert."
                    if retired else "No target identity is retained while the reviewed alert gate is disabled."
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
    """Prove declared active targets and explicit inert tombstones separately."""
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
    try:
        rules = json.loads(input_document.get("live_rules", ""))
        folders = json.loads(input_document.get("live_folders", ""))
    except (ValueError, TypeError):
        raise Refusal("invalid_live_rules") from None
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
    target_uids = {target["uid"] for target in targets}
    if len(by_uid) != len(rules) or set(by_uid) != target_uids | set(retired):
        raise Refusal("live_target_drift")
    active_contract = _expected_target_contracts(targets)
    for target in targets:
        rule = by_uid[target["uid"]]
        if not _live_rule_matches_owned(rule, active_contract[target["uid"]], folder_uid):
            raise Refusal("live_target_drift")
        if rule.get("isPaused") is not False or rule.get("notification_settings") != {
            "receiver": "agent-cloud-ops", "group_by": ["service", "environment", "cluster", "alertname"],
            "group_wait": "30s", "group_interval": "5m", "repeat_interval": "4h",
        }:
            raise Refusal("live_target_drift")
    for uid in retired:
        rule = by_uid[uid]
        if not _live_rule_matches_owned(rule, {"_inert": True, "_retired": True}, folder_uid):
            raise Refusal("live_target_drift")
    return {"status": "validated", "active_count": len(targets), "retired_count": len(retired)}


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
                "summary": f"Expected metrics have disappeared for {target['service']}.",
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
        if (
            rule.get("title") in {
                "Expected metrics target previously declared",
                "Expected metrics target explicitly retired",
            }
            and rule.get("isPaused") is True
            and "notification_settings" not in rule
            and set(rule) == {
                "uid", "title", "condition", "data", "noDataState", "execErrState", "for",
                "annotations", "labels", "isPaused",
            }
            and labels == {"severity": "warning", "owner": "platform-operations", "environment": "disabled"}
            and rule.get("condition") == "B"
            and _expected_target_queries_match(rule_data, "vector(0)")
            and rule.get("noDataState") == "OK"
            and rule.get("execErrState") == "OK"
            and rule.get("for") == "0s"
            and rule.get("annotations") == {
                "summary": "Expected metrics target rule is disabled.",
                "description": (
                    "This owned UID was explicitly retired and remains inert."
                    if rule.get("title") == "Expected metrics target explicitly retired"
                    else "No target identity is retained while the reviewed alert gate is disabled."
                ),
            }
        ):
            owned.append({
                "uid": uid,
                "_inert": True,
                "_retired": rule["title"] == "Expected metrics target explicitly retired",
            })
            continue
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
            and rule.get("annotations") == {
                "summary": f"Expected metrics have disappeared for {value['service']}.",
                "dashboard_url": f"/d/service-overview?var-service={value['service']}",
                "description": "Check the declared metrics target and its service onboarding receipt.",
            }
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
        same_previous = pending_by_uid == previous_by_uid
        valid_paused_transition = (
            set(pending_by_uid) == set(previous_by_uid)
            and isinstance(retired_uids, list)
            and all(isinstance(uid, str) and EXPECTED_UID.fullmatch(uid) for uid in retired_uids)
            and len(set(retired_uids)) == len(retired_uids)
            and not (set(retired_uids) - set(previous_by_uid))
            and all(
                pending_by_uid[uid]["_inert"]
                and pending_by_uid[uid]["_retired"] == (previous_by_uid[uid]["_retired"] or uid in retired_uids)
                for uid in previous_by_uid
            )
        )
        if (
            not previous
            or (not same_previous and not valid_paused_transition)
        ):
            raise Refusal("invalid_pending_rules")
    if check_mode and input_document.get("reviewed") is True:
        validate_expected_target_declaration(
            input_document.get("targets"), input_document.get("retired_uids"), reviewed=True
        )
    live_raw = input_document.get("live_rules")
    if previous and not live_raw and not check_mode:
        raise Refusal("live_rules_unavailable")
    if live_raw:
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
        if {rule["uid"] for rule in live_targets} != set(previous_by_uid):
            raise Refusal("live_target_drift")
        matches_previous = True
        matches_pending = bool(pending)
        for rule in live_targets:
            target = previous_by_uid.get(rule["uid"])
            if target is None or rule.get("ruleGroup") != "service-telemetry":
                raise Refusal("live_target_drift")
            if rule.get("folderUID") != live_folder_uid:
                raise Refusal("live_target_drift")
            matches_previous = matches_previous and _live_rule_matches_owned(rule, target, live_folder_uid)
            matches_pending = matches_pending and _live_rule_matches_owned(
                rule, pending_by_uid[rule["uid"]], live_folder_uid
            )
        if not matches_previous and not matches_pending:
            raise Refusal("live_target_drift")
    if input_document.get("enabled") is True:
        current, retired = validate_expected_target_declaration(
            input_document.get("targets"), input_document.get("retired_uids"), reviewed=True
        )
        current_uids = {row["uid"] for row in current}
        previous_uids = {row["uid"] for row in previous}
        previous_active = {
            row["uid"]: row for row in previous if not row["_inert"]
        }
        if any(
            any(target[field] != previous_active[target["uid"]][field] for field in EXPECTED_IDENTITY_FIELDS)
            for target in current if target["uid"] in previous_active
        ):
            raise Refusal("target_identity_changed")
        already_retired = {row["uid"] for row in previous if row["_retired"]}
        if current_uids.intersection(already_retired):
            raise Refusal("retired_target_reuse")
        if set(retired) - previous_uids:
            raise Refusal("unowned_retired_target")
        if previous_uids - current_uids - set(retired) - already_retired:
            raise Refusal("unretired_previous_target")
        retired_tombstones = list(dict.fromkeys([*already_retired, *retired]))
        return {
            "status": "prepared", "reason": "check_mode_unverified" if check_mode else "enabled", "paused_uids": [],
            "retired_uids": retired_tombstones,
        }
    retired = input_document.get("retired_uids", [])
    if not isinstance(retired, list) or any(
        not isinstance(uid, str) or not EXPECTED_UID.fullmatch(uid) for uid in retired
    ):
        raise Refusal("invalid_transition")
    previous_by_uid = {row["uid"]: row for row in previous}
    if len(set(retired)) != len(retired) or set(retired) - set(previous_by_uid):
        raise Refusal("invalid_transition")
    return {
        "status": "prepared",
        "reason": "check_mode_unverified" if check_mode else "disabled",
        "paused_uids": [row["uid"] for row in previous if not row["_retired"] and row["uid"] not in retired],
        "retired_uids": list(dict.fromkeys([*(row["uid"] for row in previous if row["_retired"]), *retired])),
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
    return {
        "baseline": baseline,
        "loaded_limit_readback": loaded_limits,
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
            validate_expected_target_declaration(
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
