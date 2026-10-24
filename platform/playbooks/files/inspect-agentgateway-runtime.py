#!/usr/bin/env python3
"""Emit safe metadata from the rendered and running agentgateway deployment.

No rendered config, environment values, request data, or container inspect output is
printed. This is the read-only first stage of observability-estate task 5.7.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

IMAGE = "cr.agentgateway.dev/agentgateway:v1.5.0"
FRONTEND_POLICIES = re.compile(r"^frontendPolicies:\s*(?:#.*)?$")
TRACING_BLOCK = re.compile(r"^  tracing:\s*(?:#.*)?$")
SAMPLING_KEY = re.compile(r"^([ \t]*)(randomSampling|clientSampling)\s*:\s*(.*)$")
SAMPLING_VALUE = re.compile(r"^(false|[0-9]+(?:\.[0-9]+)?)(?:\s+#.*)?$")
# The pinned v1.5.0 schema defines both sampling fields as StringBoolFloat, so
# clientSampling:false is a valid explicit way to disable parent-based sampling.
REVISION = re.compile(r"^[0-9a-f]{40}$")
ACCESS_LOG_LABELS = {
    "service": "'\"agentgateway\"'",
    "signal": "'\"access-log\"'",
}


def _key_candidates(lines: list[str], start: int, end: int, key: str) -> list[dict[str, object]]:
    """Find a YAML-like key without interpreting or returning its value."""
    candidates = []
    pattern = re.compile(r"^([ \t]*)(?:([A-Za-z_][A-Za-z0-9_-]*)|(['\"])([^'\"]+)\3)(.*)$")
    for index in range(start, end):
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = pattern.fullmatch(line)
        if not match:
            continue
        bare_key, quoted_key = match.group(2), match.group(4)
        actual_key = bare_key if bare_key is not None else quoted_key
        if actual_key != key:
            continue
        remainder = match.group(5)
        has_colon = remainder.startswith(":")
        value = remainder[1:].strip() if has_colon else ""
        if value.startswith("#"):
            value = ""
        leading = match.group(1)
        candidates.append({
            "index": index,
            "indent": len(leading) if "\t" not in leading else -1,
            "canonical_key": bare_key is not None,
            "has_colon": has_colon,
            "value": value,
        })
    return candidates


def _mapping_block(
    lines: list[str], start: int, end: int, key: str, expected_indent: int,
) -> tuple[tuple[int, int] | None, str | None]:
    """Return one canonical mapping block, or a fixed malformed/ambiguous category."""
    entries = _key_candidates(lines, start, end, key)
    if not entries:
        return None, None
    if len(entries) != 1:
        return None, "ambiguous"
    entry = entries[0]
    if (entry["indent"] != expected_indent or not entry["canonical_key"]
            or not entry["has_colon"] or entry["value"]):
        return None, "malformed"

    block_start = int(entry["index"])
    block_end = end
    for index in range(block_start + 1, end):
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        leading = line[:len(line) - len(line.lstrip(" \t"))]
        if "\t" in leading:
            return None, "malformed"
        if len(leading) <= expected_indent:
            block_end = index
            break
    return (block_start, block_end), None


def _has_access_label_keys(lines: list[str], start: int, end: int) -> bool:
    return any(_key_candidates(lines, start, end, key) for key in ACCESS_LOG_LABELS)


def _has_unresolved_yaml_structure(lines: list[str], start: int, end: int) -> bool:
    """Reject merge keys and aliases in the policy scope this parser does not parse."""
    for line in lines[start:end]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if re.match(r"^<<\s*:", stripped):
            return True
        # This is intentionally conservative: the inspector only understands the
        # rendered scalar subset and must not interpret YAML anchors or aliases.
        if re.search(r"(?:^|:\s|\s)[*&][A-Za-z0-9_-]+(?:\s|$)", stripped):
            return True
    return False


def _unknown_access_log_metadata() -> dict[str, str | bool | None]:
    return {
        "state": "unknown",
        "service_field_matches": None,
        "signal_field_matches": None,
    }


def _absent_access_log_metadata() -> dict[str, str | bool | None]:
    return {
        "state": "absent",
        "service_field_matches": False,
        "signal_field_matches": False,
    }


def _field_match(
    entries: list[dict[str, object]], key: str, lines: list[str], end: int,
) -> tuple[bool | None, bool]:
    """Return (match, malformed); only compare safe, single-line scalar text."""
    matches = [entry for entry in entries if entry.get("key") == key]
    if not matches:
        return False, False
    if len(matches) != 1:
        return None, True
    entry = matches[0]
    raw = entry.get("value")
    if (entry.get("indent") != 10 or not entry.get("canonical_key")
            or not entry.get("has_colon") or not isinstance(raw, str) or not raw):
        return None, True
    for line in lines[int(entry["index"]) + 1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        leading = line[:len(line) - len(line.lstrip(" \t"))]
        if "\t" in leading or len(leading) > 10:
            return None, True
        break
    # YAML aliases, anchors, collections, tags and block scalars are not interpreted.
    if raw.startswith(("*", "&", "!", "[", "{", "|", ">")) or raw in {"~", "null", "Null", "NULL"}:
        return None, True
    if (raw.startswith("'") and not raw.endswith("'")) or (raw.startswith('"') and not raw.endswith('"')):
        return None, True
    if " #" in raw:
        raw = raw.split(" #", 1)[0].rstrip()
    return raw == ACCESS_LOG_LABELS[key], False


def parse_access_log_otlp(config: str) -> dict[str, str | bool | None]:
    """Classify only the canonical accessLog.otlp structure and fixed bounded labels."""
    lines = config.splitlines()
    policy_candidates = [
        entry for entry in _key_candidates(lines, 0, len(lines), "frontendPolicies")
        if entry["indent"] == 0
    ]
    if (len(policy_candidates) != 1 or not policy_candidates[0]["canonical_key"]
            or not policy_candidates[0]["has_colon"] or policy_candidates[0]["value"]):
        return _unknown_access_log_metadata()
    policy_start = int(policy_candidates[0]["index"])
    policy_end = next((index for index in range(policy_start + 1, len(lines))
                       if lines[index].strip() and not lines[index].lstrip().startswith("#")
                       and len(lines[index]) == len(lines[index].lstrip(" "))), len(lines))
    if _has_unresolved_yaml_structure(lines, policy_start + 1, policy_end):
        return _unknown_access_log_metadata()

    all_access_logs = _key_candidates(lines, 0, len(lines), "accessLog")
    access_block, error = _mapping_block(lines, policy_start + 1, policy_end, "accessLog", 2)
    if error:
        return _unknown_access_log_metadata()
    if access_block is None:
        # A related key without its canonical parent is malformed/ambiguous, not proof
        # that access-log OTLP is absent.
        misplaced_keys = any(
            _key_candidates(lines, policy_start + 1, policy_end, key)
            for key in ("otlp", "fields", "service", "signal")
        )
        if all_access_logs or misplaced_keys:
            return _unknown_access_log_metadata()
        return _absent_access_log_metadata()
    access_start, access_end = access_block

    # An otlp key at the wrong level (including a would-be sibling of accessLog)
    # is ambiguous rather than evidence that the exporter is absent.
    all_otlp = _key_candidates(lines, policy_start + 1, policy_end, "otlp")
    otlp_block, error = _mapping_block(lines, access_start + 1, access_end, "otlp", 4)
    if error:
        return _unknown_access_log_metadata()
    if otlp_block is None:
        related_keys = any(
            _key_candidates(lines, access_start + 1, access_end, key)
            for key in ("fields", "service", "signal")
        ) or _has_access_label_keys(lines, access_start + 1, access_end)
        return _unknown_access_log_metadata() if all_otlp or related_keys else _absent_access_log_metadata()
    otlp_start, otlp_end = otlp_block
    if len(all_otlp) != 1 or int(all_otlp[0]["index"]) != otlp_start:
        return _unknown_access_log_metadata()

    fields_candidates = _key_candidates(lines, access_start + 1, access_end, "fields")
    fields_block, error = _mapping_block(lines, otlp_start + 1, otlp_end, "fields", 6)
    if error:
        return _unknown_access_log_metadata()
    if fields_block is None:
        if fields_candidates or _has_access_label_keys(lines, otlp_start + 1, otlp_end):
            return _unknown_access_log_metadata()
        return {
            "state": "present",
            "service_field_matches": False,
            "signal_field_matches": False,
        }
    fields_start, fields_end = fields_block
    if (len(fields_candidates) != 1 or int(fields_candidates[0]["index"]) != fields_start):
        return _unknown_access_log_metadata()

    all_add = _key_candidates(lines, otlp_start + 1, otlp_end, "add")
    add_candidates = _key_candidates(lines, fields_start + 1, fields_end, "add")
    add_block, error = _mapping_block(lines, fields_start + 1, fields_end, "add", 8)
    if error:
        return _unknown_access_log_metadata()
    if add_block is None:
        if add_candidates or all_add or _has_access_label_keys(lines, fields_start + 1, fields_end):
            return _unknown_access_log_metadata()
        return {
            "state": "present",
            "service_field_matches": False,
            "signal_field_matches": False,
        }
    add_start, add_end = add_block
    if (len(add_candidates) != 1 or int(add_candidates[0]["index"]) != add_start
            or len(all_add) != 1 or int(all_add[0]["index"]) != add_start):
        return _unknown_access_log_metadata()

    all_fields = [
        entry
        for key in ACCESS_LOG_LABELS
        for entry in _key_candidates(lines, otlp_start + 1, otlp_end, key)
    ]
    field_entries = []
    for key in ACCESS_LOG_LABELS:
        for entry in _key_candidates(lines, add_start + 1, add_end, key):
            entry["key"] = key
            field_entries.append(entry)
    if (len(all_fields) != len(field_entries) or any(
        int(entry["index"]) not in range(add_start + 1, add_end) for entry in all_fields
    )):
        return _unknown_access_log_metadata()
    service_match, service_error = _field_match(field_entries, "service", lines, add_end)
    signal_match, signal_error = _field_match(field_entries, "signal", lines, add_end)
    if service_error or signal_error:
        return _unknown_access_log_metadata()
    return {
        "state": "present",
        "service_field_matches": service_match,
        "signal_field_matches": signal_match,
    }


def parse_sampling(config: str) -> tuple[dict[str, float | bool], str | None]:
    """Read exactly one sampling declaration per field from frontendPolicies.tracing."""
    lines = config.splitlines()
    policy_starts = [index for index, line in enumerate(lines) if FRONTEND_POLICIES.fullmatch(line)]
    if len(policy_starts) != 1:
        return {}, "rendered frontendPolicies block is missing or duplicated"

    policy_start = policy_starts[0] + 1
    policy_end = next((index for index in range(policy_start, len(lines))
                       if lines[index].strip() and not lines[index].lstrip().startswith("#")
                       and len(lines[index]) == len(lines[index].lstrip(" "))), len(lines))
    tracing_starts = [index for index in range(policy_start, policy_end)
                      if TRACING_BLOCK.fullmatch(lines[index])]
    if len(tracing_starts) != 1:
        return {}, "rendered frontendPolicies.tracing block is missing or duplicated"

    tracing_start = tracing_starts[0] + 1
    tracing_end = next((index for index in range(tracing_start, policy_end)
                        if lines[index].strip() and not lines[index].lstrip().startswith("#")
                        and len(lines[index]) - len(lines[index].lstrip(" ")) <= 2), policy_end)
    declarations: dict[str, list[tuple[str, str]]] = {"randomSampling": [], "clientSampling": []}
    for line in lines[tracing_start:tracing_end]:
        if line.lstrip().startswith("#"):
            continue
        match = SAMPLING_KEY.fullmatch(line)
        if match:
            declarations[match.group(2)].append((match.group(1), match.group(3)))

    duplicates = [key for key, values in declarations.items() if len(values) > 1]
    if duplicates:
        return {}, "rendered trace sampling fields are duplicated"
    if any(len(values) != 1 for values in declarations.values()):
        return {}, "rendered trace sampling fields are incomplete"

    samples: dict[str, float | bool] = {}
    for key, values in declarations.items():
        indent, raw_value = values[0]
        if indent != "    ":
            return {}, "rendered trace sampling field has invalid indentation"
        match = SAMPLING_VALUE.fullmatch(raw_value)
        if not match:
            return {}, "rendered trace sampling field has an invalid value"
        value = match.group(1)
        samples[key] = False if value == "false" else float(value)
    return samples, None


def main() -> int:
    try:
        data = json.load(sys.stdin)
        env = Path(data["env_path"]).read_text()
        config = Path(data["config_path"]).read_bytes()
        checkout_revision = str(data["checkout_revision"]).strip()
    except (KeyError, OSError, ValueError, TypeError):
        print(json.dumps({"status": "refused", "reason": "runtime inputs unavailable"}))
        return 2

    images = re.findall(r"^AGW_IMAGE=(.+)$", env, re.M)
    if len(images) != 1 or images[0] != IMAGE:
        print(json.dumps({"status": "refused", "reason": "rendered image is not the required v1.5.0 pin"}))
        return 2
    samples, sampling_error = parse_sampling(config.decode("utf-8", "replace"))
    if sampling_error:
        print(json.dumps({"status": "refused", "reason": sampling_error}))
        return 2
    random_sampling = samples["randomSampling"]
    client_sampling = samples["clientSampling"]
    if isinstance(random_sampling, bool) or not 0 < random_sampling <= 0.1:
        print(json.dumps({"status": "refused", "reason": "rendered random sampling is outside the reviewed range"}))
        return 2
    if client_sampling is not False:
        print(json.dumps({"status": "refused", "reason": "rendered client sampling must be disabled"}))
        return 2
    if not REVISION.fullmatch(checkout_revision):
        print(json.dumps({"status": "refused", "reason": "checkout revision is unavailable"}))
        return 2

    access_log_otlp = parse_access_log_otlp(config.decode("utf-8", "replace"))

    print(json.dumps({
        "status": "pass",
        "image": IMAGE,
        "random_sampling": random_sampling,
        "client_sampling": client_sampling,
        "access_log_otlp": access_log_otlp,
        "rendered_config_sha256": hashlib.sha256(config).hexdigest(),
        "checkout_revision": checkout_revision,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
