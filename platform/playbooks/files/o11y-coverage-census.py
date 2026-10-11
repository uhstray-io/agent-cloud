#!/usr/bin/env python3
"""Build a sanitized, read-only census of repository candidates and estate receipts."""

import hashlib
import json
import math
import re
import sys
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

TARGET_ID = re.compile(r"^(service|agent|vm|guest|runner|network|dgx):[a-z][a-z0-9_-]{1,63}$")
SERVICE_ID = re.compile(r"^[a-z][a-z0-9_-]*(/[a-z][a-z0-9_-]*)?$")
TARGET_TYPES = {"service", "agent", "vm", "guest", "runner", "network", "dgx"}
LIFECYCLE = {"deployed", "planned", "retired", "excluded"}
SIGNALS = {"logs", "metrics", "traces", "health"}
OPTIONAL_SIGNALS = {"profiles"}
PROFILE_TYPE_ID = "process_cpu:cpu:nanoseconds:cpu:nanoseconds"
PROFILE_STEP_MS = 60_000
MAX_OBSERVATION_BYTES = 1_048_576
MAX_EVIDENCE_COUNT = 1000
BINDING_FIELDS = {"task_id", "template_id", "observation_window_seconds"}


def fail(message):
    print(json.dumps({"status": "invalid_declaration", "error": message}))
    raise SystemExit(2)


def canonical_inventory_revision(targets):
    """Hash private declarations without including their embedded revision."""
    payload = json.dumps(
        {"targets": targets}, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def candidate_names(repo, path):
    directory = repo / path
    if not directory.is_dir():
        fail(f"candidate root {path} is missing")
    return sorted(p.name for p in directory.iterdir() if p.is_dir() and not p.name.startswith("."))


def validate_receipt_bindings(targets, receipt_bindings):
    """Validate exact target/signal pointers without treating them as evidence."""
    if not isinstance(receipt_bindings, dict):
        fail("receipt_bindings must map exact target IDs to signal mappings")
    targets_by_id = {target["target_id"]: target for target in targets}
    for target_id, signals in receipt_bindings.items():
        target = targets_by_id.get(target_id)
        if target is None:
            fail("receipt_bindings contains an unrecognized target ID")
        if not isinstance(signals, dict):
            fail("each receipt_bindings target entry must map signals to bindings")
        for signal, binding in signals.items():
            declaration = target["signals"].get(signal)
            if declaration is None or not declaration["applicable"]:
                fail("receipt_bindings contains an undeclared or inapplicable signal")
            if not isinstance(binding, dict) or set(binding) != BINDING_FIELDS:
                fail("each receipt binding must contain only task_id, template_id, and observation_window_seconds")
            if any(
                not isinstance(binding[field], int) or isinstance(binding[field], bool) or binding[field] <= 0
                for field in BINDING_FIELDS
            ):
                fail("receipt binding values must be positive integers")

    for target in targets:
        legacy_references = target.get("receipt_reference", {})
        for signal, declaration in target["signals"].items():
            if not declaration["applicable"]:
                continue
            nested_present = "receipt_reference" in declaration
            target_present = signal in legacy_references
            nested_reference = declaration.get("receipt_reference")
            target_reference = legacy_references.get(signal)
            if nested_present != target_present:
                fail(f"{signal} legacy receipt pointers must be both present or both omitted")
            if nested_present:
                match = re.fullmatch(r"semaphore/task/([1-9][0-9]*)", nested_reference) if isinstance(
                    nested_reference, str
                ) else None
                if match is None or target_reference != nested_reference:
                    fail(f"{signal} legacy receipt pointers must match a Semaphore task reference")
                binding = receipt_bindings.get(target["target_id"], {}).get(signal)
                if binding is not None and binding["task_id"] != int(match.group(1)):
                    fail(f"{signal} receipt binding conflicts with the legacy task reference")


def validate(targets, receipt_bindings=None):
    ids = set()
    identities = {}
    required = {
        "target_id", "target_type", "lifecycle", "owner", "runtime",
        "service_identity", "environment", "signals", "collection_method",
        "budget", "template_references",
    }
    for target in targets:
        if not isinstance(target, dict) or required - target.keys():
            fail("each target must include every required coverage declaration field")
        target_id = target["target_id"]
        identity = target["service_identity"]
        if not isinstance(target_id, str) or not TARGET_ID.fullmatch(target_id):
            fail("target_id must be a stable non-address identifier")
        if target_id in ids:
            fail("duplicate target_id in estate declarations")
        ids.add(target_id)
        if target["target_type"] not in TARGET_TYPES or target["lifecycle"] not in LIFECYCLE:
            fail("target_type or lifecycle is outside the public schema")
        if target_id.split(":", 1)[0] != target["target_type"]:
            fail("target_id prefix must match target_type")
        if not isinstance(target["owner"], str) or not re.fullmatch(r"[a-z][a-z0-9_-]{1,63}", target["owner"]):
            fail("owner must be a bounded team label, not a person or address")
        environment_pattern = r"[a-z][a-z0-9_-]{1,31}"
        if not isinstance(target["environment"], str) or not re.fullmatch(environment_pattern, target["environment"]):
            fail("environment must be a bounded inventory label")
        if not isinstance(identity, str) or not SERVICE_ID.fullmatch(identity):
            fail("service_identity must use the canonical service identity format")
        identities.setdefault(identity, []).append(target_id)
        if not isinstance(target["signals"], dict) or set(target["signals"]) - SIGNALS - OPTIONAL_SIGNALS:
            fail("signals must be a mapping of supported signal names")
        if (
            not isinstance(target["runtime"], str)
            or not target["runtime"]
            or not isinstance(target["collection_method"], str)
            or not target["collection_method"]
        ):
            fail("runtime and collection_method are required labels")
        if not isinstance(target["budget"], dict) or not target["budget"]:
            fail("budget must be a non-empty mapping")
        legacy_references = target.get("receipt_reference", {})
        if not isinstance(legacy_references, dict):
            fail("receipt_reference must map signals to their receipt references")
        if set(legacy_references) - target["signals"].keys():
            fail("receipt_reference contains an undeclared signal")
        template_refs = target.get("template_references", [])
        if not isinstance(template_refs, list) or any(not isinstance(ref, str) for ref in template_refs):
            fail("template_references must be a list of Semaphore template names")
        for signal, declaration in target["signals"].items():
            if not isinstance(declaration, dict) or not isinstance(declaration.get("applicable"), bool):
                fail(f"{signal} must declare applicability")
            if not declaration["applicable"]:
                exception = declaration.get("exception")
                if not isinstance(exception, dict) or not all(
                    exception.get(field) for field in ("reason", "reference")
                ):
                    fail(f"{signal} requires a reviewed exception reason and reference when not applicable")
                if not isinstance(exception["reference"], str) or not re.fullmatch(
                    r"(?:review/[1-9][0-9]*|semaphore/task/[1-9][0-9]*)", exception["reference"]
                ):
                    fail(f"{signal} exception reference must identify a review or Semaphore task")
                if signal == "traces" and not (exception.get("alternative") or exception.get("manual_plan")):
                    fail("an excluded trace signal requires an approved alternative or manual instrumentation plan")
            if declaration["applicable"] and any(
                not declaration.get(field) for field in ("selector", "collection_method", "freshness_seconds")
            ):
                fail(f"{signal} must declare its selector, method, and freshness")
            if declaration["applicable"] and (
                not isinstance(declaration["freshness_seconds"], int)
                or isinstance(declaration["freshness_seconds"], bool)
                or not 60 <= declaration["freshness_seconds"] <= 86400
            ):
                fail(f"{signal} freshness_seconds must be between 60 and 86400")
            if signal == "profiles" and declaration["applicable"] and (
                declaration.get("profile_type_id") != PROFILE_TYPE_ID
                or declaration.get("selector") != f'{{target_id="{target_id}"}}'
            ):
                fail("profiles must use the pinned CPU profile type and an exact target_id selector")
    validate_receipt_bindings(targets, {} if receipt_bindings is None else receipt_bindings)
    return ids, {key: values for key, values in identities.items() if len(values) > 1}


def receipt_status(target, signal, inventory_revision, repository_sha, now,
                   receipt_bindings, verified_receipts):
    """Accept only a bounded receipt matching the exact declared Semaphore pointer."""
    binding = receipt_bindings.get(target["target_id"], {}).get(signal)
    target_receipts = verified_receipts.get(target["target_id"])
    receipt = target_receipts.get(signal) if isinstance(target_receipts, dict) else None
    if not isinstance(binding, dict) or not isinstance(receipt, dict):
        return "incomplete"
    if set(receipt) != {
        "status", "target_id", "signal", "task_id", "template_id", "repository_sha",
        "inventory_revision", "observed_at", "expires_at",
    }:
        return "incomplete"
    if (
        receipt["status"] != "verified"
        or receipt["target_id"] != target["target_id"]
        or receipt["signal"] != signal
        or not isinstance(receipt["task_id"], int)
        or isinstance(receipt["task_id"], bool)
        or not isinstance(receipt["template_id"], int)
        or isinstance(receipt["template_id"], bool)
        or receipt["task_id"] != binding["task_id"]
        or receipt["template_id"] != binding["template_id"]
        or receipt["repository_sha"] != repository_sha
        or receipt["inventory_revision"] != inventory_revision
    ):
        return "incomplete"
    try:
        expires_at = datetime.fromisoformat(receipt["expires_at"].replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return "incomplete"
    if expires_at.tzinfo is None or expires_at.utcoffset() != UTC.utcoffset(expires_at) or now >= expires_at:
        return "incomplete"
    return "verified"


def template_reference_summary(references, template_names):
    present_count = sum(1 for reference in references if reference in template_names)
    return {
        "declared_count": len(references),
        "present_count": present_count,
        "missing_count": len(references) - present_count,
    }


def build_report(repo, inventory, repository_sha, now=None, *, verified_receipts=None):
    targets = inventory.get("targets", [])
    inventory_revision = inventory.get("inventory_revision")
    template_names = set(inventory.get("template_names", []))
    if not isinstance(targets, list):
        fail("targets must be a list")
    if not isinstance(inventory.get("template_names", []), list):
        fail("template_names must be a list")
    if "receipt_bindings" in inventory and not isinstance(inventory["receipt_bindings"], dict):
        fail("receipt_bindings must map exact target IDs to signal mappings")
    if "verified_receipts" in inventory:
        fail("verified_receipts are not accepted from inventory input")
    ids, conflicts = validate(targets, inventory.get("receipt_bindings", {}))
    if inventory_revision is not None:
        if not isinstance(inventory_revision, str) or not re.fullmatch(r"[0-9a-f]{64}", inventory_revision):
            fail("inventory revision must be a lowercase SHA-256 declaration digest")
        if inventory_revision != canonical_inventory_revision(targets):
            fail("inventory revision does not match the canonical target declaration digest")
    now = now or datetime.now(UTC)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() != UTC.utcoffset(now):
        fail("report time must be UTC")
    now = now.astimezone(UTC)
    receipt_bindings = inventory.get("receipt_bindings", {})
    verified_receipts = {} if verified_receipts is None else verified_receipts
    if not isinstance(verified_receipts, dict):
        fail("verified receipts must map targets to signals")
    declared = {}
    for target in targets:
        declared.setdefault(target["service_identity"], []).append(target)
    entries = []
    matched_target_ids = set()
    for kind, dirname in (("service", "platform/services"), ("agent", "agents")):
        for name in candidate_names(repo, dirname):
            identity = name
            if identity in conflicts:
                entries.append({
                    "target_id": f"candidate:{kind}:{name}",
                    "source_candidate": f"{dirname}/{name}",
                    "target_type": kind,
                    "lifecycle": "unclassified",
                    "service_identity": identity,
                    "coverage": "unverified",
                    "identity_conflict": True,
                    "signals": dict.fromkeys(sorted(SIGNALS), "identity_conflict"),
                })
                continue
            matches = [target for target in declared.get(identity, []) if target["target_type"] == kind]
            if len(matches) != 1:
                entries.append({
                    "target_id": f"candidate:{kind}:{name}",
                    "source_candidate": f"{dirname}/{name}",
                    "target_type": kind,
                    "lifecycle": "unclassified",
                    "service_identity": identity,
                    "coverage": "unverified",
                    "signals": {},
                })
                continue
            target = matches[0]
            matched_target_ids.add(target["target_id"])
            statuses = {
                signal: (
                    "undeclared" if signal not in target["signals"] else
                    "excepted" if not target["signals"][signal]["applicable"] else
                    receipt_status(
                        target, signal, inventory_revision, repository_sha, now,
                        receipt_bindings, verified_receipts,
                    )
                )
                for signal in sorted(SIGNALS | target["signals"].keys())
            }
            deployed = target["lifecycle"] == "deployed"
            coverage = "verified" if deployed and statuses and all(
                state in {"verified", "excepted"} for state in statuses.values()
            ) else "incomplete" if deployed else "not_deployed"
            references = target.get("template_references", [])
            missing_templates = [ref for ref in references if ref not in template_names]
            if deployed and (not references or missing_templates):
                coverage = "incomplete"
            entries.append({
                "target_id": target["target_id"],
                "source_candidate": f"{dirname}/{name}",
                "target_type": target["target_type"],
                "lifecycle": target["lifecycle"],
                "service_identity": identity,
                "coverage": coverage,
                "signals": statuses,
                "template_references": template_reference_summary(references, template_names),
            })
    for target in targets:
        if target["service_identity"] not in conflicts:
            continue
        entries.append({
            "target_id": target["target_id"],
            "source_candidate": None,
            "target_type": target["target_type"],
            "lifecycle": target["lifecycle"],
            "service_identity": target["service_identity"],
            "coverage": "unverified",
            "identity_conflict": True,
            "signals": dict.fromkeys(sorted(SIGNALS), "identity_conflict"),
            "template_references": template_reference_summary(target.get("template_references", []), template_names),
        })
    for target in targets:
        if target["target_id"] in matched_target_ids or target["service_identity"] in conflicts:
            continue
        statuses = {
            signal: (
                "undeclared" if signal not in target["signals"] else
                "excepted" if not target["signals"][signal]["applicable"] else
                receipt_status(
                    target, signal, inventory_revision, repository_sha, now,
                    receipt_bindings, verified_receipts,
                )
            )
            for signal in sorted(SIGNALS | target["signals"].keys())
        }
        deployed = target["lifecycle"] == "deployed"
        references = target.get("template_references", [])
        missing_templates = [ref for ref in references if ref not in template_names]
        coverage = "verified" if deployed and statuses and all(
            state in {"verified", "excepted"} for state in statuses.values()
        ) and references and not missing_templates else "incomplete" if deployed else "not_deployed"
        entries.append({
            "target_id": target["target_id"],
            "source_candidate": None,
            "target_type": target["target_type"],
            "lifecycle": target["lifecycle"],
            "service_identity": target["service_identity"],
            "coverage": coverage,
            "signals": statuses,
            "template_references": template_reference_summary(references, template_names),
        })
    conflicting_ids = sorted(target_id for values in conflicts.values() for target_id in values)
    return {
        "status": "complete" if inventory_revision else "inventory_missing",
        "inventory_revision": inventory_revision,
        "candidate_count": sum(1 for entry in entries if entry["source_candidate"]),
        "declared_target_count": len(targets),
        "duplicate_identity_count": len(conflicts),
        "duplicate_identity_targets": conflicting_ids,
        "unverified_target_count": sum(1 for entry in entries if entry["coverage"] != "verified"),
        "targets": entries,
    }


def validate_profile_receipt(payload):
    """Return only a bounded status for the pinned SelectSeries response shape."""
    invalid = {"status": "invalid_response", "sample_count": 0}
    if not isinstance(payload, dict):
        return invalid
    target_id = payload.get("target_id")
    start_ms = payload.get("start_ms")
    end_ms = payload.get("end_ms")
    freshness_seconds = payload.get("freshness_seconds")
    if (
        not isinstance(target_id, str)
        or not TARGET_ID.fullmatch(target_id)
        or any(not isinstance(value, int) or isinstance(value, bool) for value in (start_ms, end_ms, freshness_seconds))
        or start_ms >= end_ms
        or not 60 <= freshness_seconds <= 86400
        or end_ms - start_ms > 3600_000
    ):
        return invalid
    try:
        response = payload["response"]
        if isinstance(response, str):
            response = json.loads(response)
        if (
            not isinstance(response, dict)
            or not isinstance(response.get("series"), list)
            or len(response["series"]) > 1
        ):
            return invalid
        samples = 0
        for series in response["series"]:
            if (
                not isinstance(series, dict)
                or not isinstance(series.get("labels"), list)
                or not isinstance(series.get("points"), list)
                or len(series["points"]) > 62
            ):
                return invalid
            if any(
                not isinstance(label, dict)
                or not isinstance(label.get("name"), str)
                or not isinstance(label.get("value"), str)
                for label in series["labels"]
            ):
                return invalid
            target_labels = [
                label.get("value") for label in series["labels"]
                if label["name"] == "target_id"
            ]
            if target_labels != [target_id]:
                return invalid
            for point in series["points"]:
                if not isinstance(point, dict):
                    return invalid
                timestamp = point.get("timestamp")
                if isinstance(timestamp, str) and re.fullmatch(r"[0-9]+", timestamp):
                    timestamp = int(timestamp)
                value = point.get("value", 0)
                if (
                    not isinstance(timestamp, int)
                    or isinstance(timestamp, bool)
                    or not isinstance(value, (int, float))
                    or isinstance(value, bool)
                    or not math.isfinite(value)
                ):
                    return invalid
                earliest_bucket_end = max(start_ms, end_ms - freshness_seconds * 1000) + PROFILE_STEP_MS
                if earliest_bucket_end <= timestamp <= end_ms and value > 0:
                    samples += 1
        return {"status": "verified" if samples else "no_fresh_sample", "sample_count": samples}
    except (KeyError, TypeError, ValueError, OverflowError, json.JSONDecodeError):
        return invalid


def _observation_result(status, reason, count=0):
    return {"status": status, "reason": reason, "evidence_count": min(count, MAX_EVIDENCE_COUNT)}


def _decimal(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise ValueError("not a decimal")
    try:
        number = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError("not a decimal") from error
    if not number.is_finite():
        raise ValueError("not finite")
    return number


def _json_response(value):
    if isinstance(value, str):
        if len(value.encode("utf-8")) > MAX_OBSERVATION_BYTES:
            raise OverflowError
        value = json.loads(
            value,
            object_pairs_hook=unique_object,
            parse_float=Decimal,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonstandard number")),
        )
    else:
        try:
            encoded = json.dumps(
                value, ensure_ascii=True, separators=(",", ":"), default=str, allow_nan=False
            ).encode("utf-8")
        except RecursionError as error:
            raise ValueError("response nesting is too deep") from error
        if len(encoded) > MAX_OBSERVATION_BYTES:
            raise OverflowError
    if not isinstance(value, dict):
        raise ValueError("response is not an object")
    return value


def _observation_interval(payload):
    names = (
        "query_start", "query_end", "observed_at", "window_seconds",
        "requested_freshness_seconds", "declared_freshness_seconds",
    )
    values = tuple(payload.get(name) for name in names)
    if any(not isinstance(value, int) or isinstance(value, bool) for value in values):
        raise ValueError
    start, end, observed, window, requested_freshness, declared_freshness = values
    if (
        window < 60 or window > 3600 or requested_freshness <= window
        or requested_freshness > 86400 or requested_freshness != declared_freshness
        or start >= end or end - start != window
        or observed < end or observed >= end + requested_freshness - window
    ):
        raise ValueError
    return start, end, observed, requested_freshness


def _in_observation_bounds(timestamp, start, end, observed, freshness, scale=1):
    return max(start, observed - freshness) * scale <= timestamp <= end * scale


def _validate_loki(response, target_id, start, end, observed, freshness):
    if response.get("status") != "success":
        return "backend_status", 0
    data = response.get("data")
    if not isinstance(data, dict) or data.get("resultType") != "streams" or not isinstance(data.get("result"), list):
        return "invalid_response", 0
    count = 0
    for stream in data["result"]:
        if not isinstance(stream, dict) or not isinstance(stream.get("stream"), dict):
            return "invalid_response", 0
        if stream["stream"].get("target_id") != target_id:
            return "identity_mismatch", 0
        values = stream.get("values")
        if not isinstance(values, list):
            return "invalid_response", 0
        for entry in values:
            if (
                not isinstance(entry, list) or len(entry) not in (2, 3)
                or not isinstance(entry[0], str) or not re.fullmatch(r"[0-9]{1,20}", entry[0])
                or not isinstance(entry[1], str)
                or (len(entry) == 3 and not isinstance(entry[2], dict))
            ):
                return "invalid_response", 0
            timestamp = int(entry[0])
            if not _in_observation_bounds(timestamp, start, end, observed, freshness, 1_000_000_000):
                return "timestamp_out_of_bounds", 0
            count += 1
    return ("verified", count) if count else ("no_evidence", 0)


def _prom_timestamp(value):
    return _decimal(value)


def _validate_prometheus(response, target_id, signal, start, end, observed, freshness):
    if response.get("status") != "success":
        return "backend_status", 0
    data = response.get("data")
    if not isinstance(data, dict) or data.get("resultType") != "matrix" or not isinstance(data.get("result"), list):
        return "invalid_response", 0
    evidence_count = 0
    lower = Decimal(max(start, observed - freshness))
    upper = Decimal(end)
    for series in data["result"]:
        if not isinstance(series, dict) or not isinstance(series.get("metric"), dict):
            return "invalid_response", 0
        if series["metric"].get("target_id") != target_id:
            return "identity_mismatch", 0
        latest = None
        latest_float = None
        seen_timestamps = set()
        for field, is_float in (("values", True), ("histograms", False)):
            samples = series.get(field, [])
            if not isinstance(samples, list):
                return "invalid_response", 0
            for sample in samples:
                if (
                    not isinstance(sample, list) or len(sample) != 2
                    or (is_float and not isinstance(sample[1], str))
                    or (not is_float and (
                        not isinstance(sample[1], dict)
                        or not {"count", "sum", "buckets"}.issubset(sample[1])
                        or not isinstance(sample[1]["buckets"], list)
                    ))
                ):
                    return "invalid_response", 0
                try:
                    timestamp = _prom_timestamp(sample[0])
                    if is_float:
                        _decimal(sample[1])
                    else:
                        _decimal(sample[1]["count"])
                        _decimal(sample[1]["sum"])
                except ValueError:
                    return "invalid_response", 0
                if timestamp in seen_timestamps:
                    return "invalid_response", 0
                seen_timestamps.add(timestamp)
                if timestamp < lower or timestamp > upper:
                    return "timestamp_out_of_bounds", 0
                if latest is None or timestamp > latest:
                    latest = timestamp
                    latest_float = sample[1] if is_float else None
                evidence_count += 1
        if latest is None:
            return "no_evidence", 0
        if signal == "health" and (latest_float != "1"):
            return "unhealthy_latest_sample", 0
    return ("verified", evidence_count) if evidence_count else ("no_evidence", 0)


def _tempo_target_value(attribute):
    value = attribute.get("value")
    return value.get("stringValue") if isinstance(value, dict) else None


def _validate_tempo(response, target_id, start, end, observed, freshness):
    traces = response.get("traces")
    if not isinstance(traces, list):
        return "invalid_response", 0
    count = 0
    for trace in traces:
        if not isinstance(trace, dict):
            return "invalid_response", 0
        span_sets = trace.get("spanSets", [])
        if not isinstance(span_sets, list):
            return "invalid_response", 0
        for span_set in span_sets:
            if not isinstance(span_set, dict) or not isinstance(span_set.get("spans"), list):
                return "invalid_response", 0
            for span in span_set["spans"]:
                if not isinstance(span, dict):
                    return "invalid_response", 0
                span_id = span.get("spanID")
                timestamp = span.get("startTimeUnixNano")
                attributes = span.get("attributes")
                if (
                    not isinstance(span_id, str) or not span_id
                    or not isinstance(timestamp, str) or not re.fullmatch(r"[0-9]{1,20}", timestamp)
                    or not isinstance(attributes, list)
                ):
                    return "invalid_response", 0
                targets = [item for item in attributes if isinstance(item, dict) and item.get("key") == "target_id"]
                if len(targets) != 1:
                    return "identity_mismatch", 0
                if _tempo_target_value(targets[0]) != target_id:
                    return "identity_mismatch", 0
                if not _in_observation_bounds(int(timestamp), start, end, observed, freshness, 1_000_000_000):
                    return "timestamp_out_of_bounds", 0
                count += 1
    return ("verified", count) if count else ("no_evidence", 0)


def _validate_pyroscope(response, target_id, start, end, observed, freshness):
    series_list = response.get("series")
    if not isinstance(series_list, list) or len(series_list) != 1:
        return "invalid_response", 0
    series = series_list[0]
    if (
        not isinstance(series, dict)
        or not isinstance(series.get("labels"), list)
        or not isinstance(series.get("points"), list)
        or len(series["points"]) > 62
    ):
        return "invalid_response", 0
    labels = series["labels"]
    if any(
        not isinstance(label, dict)
        or not isinstance(label.get("name"), str)
        or not isinstance(label.get("value"), str)
        for label in labels
    ):
        return "invalid_response", 0
    targets = [label["value"] for label in labels if label["name"] == "target_id"]
    if targets != [target_id]:
        return "identity_mismatch", 0
    lower_bucket = max(start, observed - freshness) * 1000
    end_ms = end * 1000
    count = 0
    for point in series["points"]:
        if not isinstance(point, dict):
            return "invalid_response", 0
        timestamp = point.get("timestamp")
        if isinstance(timestamp, str) and re.fullmatch(r"[0-9]{1,20}", timestamp):
            timestamp = int(timestamp)
        value = point.get("value", 0)
        try:
            finite_value = _decimal(value).is_finite()
        except ValueError:
            return "invalid_response", 0
        if (
            not isinstance(timestamp, int) or isinstance(timestamp, bool)
            or not isinstance(value, (int, float, Decimal)) or isinstance(value, bool)
            or not finite_value
        ):
            return "invalid_response", 0
        if not start * 1000 <= timestamp <= end_ms:
            return "timestamp_out_of_bounds", 0
        if timestamp - PROFILE_STEP_MS >= lower_bucket and value > 0:
            count += 1
    return ("verified", count) if count else ("no_evidence", 0)


def validate_signal_observation(payload):
    """Validate one bounded backend response and return only sanitized evidence metadata."""
    if not isinstance(payload, dict):
        return _observation_result("refused", "invalid_input")
    target_id = payload.get("target_id")
    signal = payload.get("signal")
    command_rc = payload.get("command_rc")
    if (
        not isinstance(target_id, str) or not TARGET_ID.fullmatch(target_id)
        or not isinstance(signal, str) or signal not in {"logs", "metrics", "health", "traces", "profiles"}
        or not isinstance(command_rc, int) or isinstance(command_rc, bool)
    ):
        return _observation_result("refused", "invalid_input")
    if command_rc != 0:
        return _observation_result("refused", "transport_error")
    try:
        start, end, observed, freshness = _observation_interval(payload)
    except (TypeError, ValueError):
        return _observation_result("refused", "invalid_interval")
    try:
        raw_response = payload.get("response")
        if isinstance(raw_response, str) and len(raw_response.encode("utf-8")) > MAX_OBSERVATION_BYTES:
            return _observation_result("refused", "oversized_response")
        response = _json_response(raw_response)
    except OverflowError:
        return _observation_result("refused", "oversized_response")
    except (UnicodeError, ValueError, RecursionError, json.JSONDecodeError):
        return _observation_result("refused", "malformed_json")
    if signal == "logs":
        reason, count = _validate_loki(response, target_id, start, end, observed, freshness)
    elif signal in {"metrics", "health"}:
        reason, count = _validate_prometheus(response, target_id, signal, start, end, observed, freshness)
    elif signal == "traces":
        reason, count = _validate_tempo(response, target_id, start, end, observed, freshness)
    else:
        reason, count = _validate_pyroscope(response, target_id, start, end, observed, freshness)
    status = "verified" if reason == "verified" else "refused"
    return _observation_result(status, reason, count)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def main():
    if len(sys.argv) == 2 and sys.argv[1] == "--validate-profile-receipt":
        try:
            payload = json.load(sys.stdin)
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = None
        print(json.dumps(validate_profile_receipt(payload), sort_keys=True))
        return
    if len(sys.argv) == 2 and sys.argv[1] == "--validate-signal-observation":
        try:
            raw = sys.stdin.buffer.read(MAX_OBSERVATION_BYTES + 65_537)
            if len(raw) > MAX_OBSERVATION_BYTES + 65_536:
                raise OverflowError
            payload = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=unique_object,
                parse_float=Decimal,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonstandard number")),
            )
        except OverflowError:
            result = _observation_result("refused", "oversized_response")
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError, RecursionError):
            result = _observation_result("refused", "malformed_json")
        else:
            result = validate_signal_observation(payload)
        print(json.dumps(result, sort_keys=True))
        return
    try:
        inventory = json.load(sys.stdin, object_pairs_hook=unique_object)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        fail("inventory input must be JSON with unique object keys")
    if not isinstance(inventory, dict):
        fail("inventory input must be a JSON object")
    if "verified_receipts" in inventory:
        fail("verified_receipts are not accepted from inventory input")
    if "receipt_bindings" in inventory and not isinstance(inventory["receipt_bindings"], dict):
        fail("receipt_bindings must map exact target IDs to signal mappings")
    if len(sys.argv) == 2 and sys.argv[1] == "--inventory-revision":
        targets = inventory.get("targets")
        if not isinstance(targets, list):
            fail("targets must be a list")
        validate(targets, inventory.get("receipt_bindings", {}))
        print(canonical_inventory_revision(targets))
        return
    if len(sys.argv) != 2:
        fail(
            "usage: o11y-coverage-census.py <repo-root>, --inventory-revision, "
            "--validate-profile-receipt, or --validate-signal-observation"
        )
    report = build_report(Path(sys.argv[1]).resolve(), inventory, inventory.get("repository_sha", ""))
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
