#!/usr/bin/env python3
"""Bind a Pyroscope restart proof to one successful profile acceptance task."""

from __future__ import annotations

import json
import math
import re
import sys
from datetime import UTC, datetime

SCHEMA = {"schema_version": 1, "status": "unavailable", "reason": "invalid_input"}
EXPECTED_TEMPLATE = "Accept o11y Profile Pilot (Dev)"
EXPECTED_PLAYBOOK = "platform/playbooks/accept-o11y-profile-pilot.yml"
EXPECTED_REPOSITORY = "agent-cloud dev"
EXPECTED_REPOSITORY_URL = "https://github.com/uhstray-io/agent-cloud.git"
MAX_INPUT_BYTES = 8 * 1024 * 1024
MAX_OUTPUT_BYTES = 1024 * 1024
MAX_OUTPUT_ROWS = 5000
MAX_WINDOW_SECONDS = 600
RECEIPT_FIELDS = {
    "schema_version", "status", "reason", "service", "sampling", "delivery", "restored",
    "post_disable_query", "disabled_config_sha256", "enabled_config_sha256", "receiver_sha",
    "public_sha", "private_sha", "inventory_provenance", "candidate", "resource_gate",
    "profile_config_state_after_run",
}


class Refusal(Exception):
    """Fixed safe refusal; never include task output or API response content."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _positive_id(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def _utc(value: object) -> datetime:
    if not isinstance(value, str):
        raise Refusal("task_time_invalid")
    match = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?(Z|\+00:00)", value)
    if match is None:
        raise Refusal("task_time_invalid")
    stamp = match.group(1) + ("." + match.group(2)[:6] if match.group(2) else "") + "+00:00"
    try:
        return datetime.fromisoformat(stamp).astimezone(UTC)
    except ValueError:
        raise Refusal("task_time_invalid") from None


def _objects(text: str):
    """Yield complete JSON objects embedded in one bounded task-output row."""
    start = None
    depth = 0
    quoted = escaped = False
    for index, char in enumerate(text):
        if start is None:
            if char == "{":
                start, depth, quoted, escaped = index, 1, False, False
            continue
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    value = json.loads(text[start:index + 1], object_pairs_hook=_unique_object)
                    yield value
                    if isinstance(value, dict):
                        for nested in value.values():
                            if isinstance(nested, str):
                                yield from _objects(nested)
                except (json.JSONDecodeError, UnicodeError):
                    pass
                start = None


def _receipt_objects(text: str) -> list[dict]:
    return [candidate for candidate in _objects(text)
            if isinstance(candidate, dict) and set(candidate) == RECEIPT_FIELDS]


def _receipt(rows: object, task_id: int, created: datetime, started: datetime, ended: datetime) -> dict:
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_OUTPUT_ROWS:
        raise Refusal("acceptance_output_invalid")
    used = 0
    previous_id = 0
    zero_id_rows = None
    previous_time = None
    validated_rows = []
    now = datetime.now(UTC)
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get("id"), int)
                or isinstance(row.get("id"), bool) or row["id"] < 0
                or not _positive_id(row.get("task_id")) or row.get("stage_id") is not None
                or not isinstance(row.get("output"), str)):
            raise Refusal("acceptance_output_invalid")
        is_zero_id = row["id"] == 0
        if (zero_id_rows is not None and zero_id_rows != is_zero_id
                or not is_zero_id and row["id"] <= previous_id
                or row["task_id"] != task_id):
            raise Refusal("acceptance_output_invalid")
        zero_id_rows = is_zero_id
        if not is_zero_id:
            previous_id = row["id"]
        stamp = _utc(row.get("time"))
        if not created <= stamp <= ended <= now or (previous_time is not None and stamp < previous_time):
            raise Refusal("acceptance_output_time_invalid")
        previous_time = stamp
        output = row["output"]
        used += len(output.encode("utf-8"))
        if used > MAX_OUTPUT_BYTES:
            raise Refusal("acceptance_output_too_large")
        validated_rows.append((output, stamp))
    joined_output = "\n".join(output for output, _stamp in validated_rows)
    if len(joined_output.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise Refusal("acceptance_output_too_large")
    joined_output = re.sub(r"\x1b\[[0-9;]*m", "", joined_output)
    matches = _receipt_objects(joined_output)
    if len(matches) != 1:
        raise Refusal("acceptance_receipt_ambiguous")
    receipt_rows = [stamp for output, stamp in validated_rows if "schema_version" in output]
    if not receipt_rows or any(stamp < started for stamp in receipt_rows):
        raise Refusal("acceptance_output_time_invalid")
    return matches[0]


def evaluate(payload: object) -> dict:
    result = dict(SCHEMA)
    try:
        if not isinstance(payload, dict) or set(payload) != {
            "task_id", "project_id", "expected_acceptance_sha", "expected_receiver_sha",
            "task", "output", "template", "repository",
        }:
            raise Refusal("invalid_input")
        task_id, project_id = payload["task_id"], payload["project_id"]
        expected_acceptance_sha = payload["expected_acceptance_sha"]
        receiver_sha = payload["expected_receiver_sha"]
        task, template, repository = payload["task"], payload["template"], payload["repository"]
        if (not _positive_id(task_id) or not _positive_id(project_id)
                or not isinstance(expected_acceptance_sha, str)
                or re.fullmatch(r"[0-9a-f]{40}", expected_acceptance_sha) is None
                or not isinstance(receiver_sha, str)
                or re.fullmatch(r"[0-9a-f]{40}", receiver_sha) is None):
            raise Refusal("invalid_input")
        if (not isinstance(task, dict) or not _positive_id(task.get("id")) or task["id"] != task_id
                or not _positive_id(task.get("project_id")) or task["project_id"] != project_id
                or not _positive_id(task.get("template_id")) or task.get("status") != "success"):
            raise Refusal("acceptance_task_mismatch")
        acceptance_sha = task.get("commit_hash")
        if acceptance_sha != expected_acceptance_sha:
            raise Refusal("acceptance_revision_invalid")
        if (task.get("playbook") != "" or task.get("arguments") not in (None, "")
                or task.get("git_branch") not in (None, "") or task.get("limit", "") != ""
                or task.get("secret", "") not in (None, "") or task.get("schedule_id") is not None
                or task.get("workflow_run_id") is not None
                or task.get("params") not in (None, {"dry_run": False, "diff": False})):
            raise Refusal("acceptance_task_override")
        task_inventory = task.get("inventory_id")
        if (not isinstance(template, dict) or not _positive_id(template.get("id"))
                or template["id"] != task["template_id"]
                or not _positive_id(template.get("project_id")) or template["project_id"] != project_id
                or template.get("name") != EXPECTED_TEMPLATE
                or template.get("playbook") != EXPECTED_PLAYBOOK
                or not _positive_id(template.get("repository_id"))
                or not _positive_id(template.get("inventory_id"))
                or template.get("app") != "ansible"
                or template.get("allow_override_args_in_task", False) is not False
                or template.get("task_params", {}) != {}
                or template.get("git_branch") not in (None, "")
                or (task_inventory is not None and task_inventory != template["inventory_id"])):
            raise Refusal("acceptance_template_mismatch")
        if task_inventory is not None and not _positive_id(task_inventory):
            raise Refusal("acceptance_template_mismatch")
        if (not isinstance(repository, dict) or not _positive_id(repository.get("id"))
                or repository["id"] != template["repository_id"]
                or not _positive_id(repository.get("project_id")) or repository["project_id"] != project_id
                or repository.get("name") != EXPECTED_REPOSITORY
                or repository.get("git_url") != EXPECTED_REPOSITORY_URL
                or repository.get("git_branch") != "dev"):
            raise Refusal("acceptance_repository_mismatch")
        try:
            launch = json.loads(task.get("environment", ""), object_pairs_hook=_unique_object)
        except (TypeError, json.JSONDecodeError):
            raise Refusal("acceptance_launch_invalid") from None
        if (not isinstance(launch, dict) or launch.get("expected_repository_sha") != acceptance_sha
                or launch.get("expected_receiver_sha") != receiver_sha):
            raise Refusal("acceptance_launch_mismatch")
        created, started, ended = (_utc(task.get(field)) for field in ("created", "start", "end"))
        now = datetime.now(UTC)
        if not created <= started < ended <= now:
            raise Refusal("acceptance_task_time_order")
        duration = (ended - started).total_seconds()
        if not 0 < duration <= MAX_WINDOW_SECONDS:
            raise Refusal("acceptance_window_invalid")
        receipt = _receipt(payload["output"], task_id, created, started, ended)
        if (receipt.get("schema_version") != 1 or receipt.get("status") != "verified"
                or receipt.get("reason") != "sampled_delivered_restored_post_disable_query_observed"
                or receipt.get("service") != "o11y/alloy" or receipt.get("sampling") != "observed"
                or receipt.get("delivery") != "observed" or receipt.get("restored") != "disabled"
                or receipt.get("post_disable_query") != "observed"
                or receipt.get("receiver_sha") != receiver_sha or receipt.get("public_sha") != acceptance_sha
                or not isinstance(receipt.get("private_sha"), str)
                or re.fullmatch(r"[0-9a-f]{40}", receipt["private_sha"]) is None
                or receipt.get("inventory_provenance") != "exact_bytes_match"
                or receipt.get("candidate") != "validated" or receipt.get("resource_gate") != "passed"
                or receipt.get("profile_config_state_after_run") != "disabled"):
            raise Refusal("acceptance_receipt_not_verified")
        if any(re.fullmatch(r"[0-9a-f]{64}", receipt.get(key, "")) is None
               for key in ("disabled_config_sha256", "enabled_config_sha256")):
            raise Refusal("acceptance_receipt_invalid")
        start_epoch = math.floor(started.timestamp())
        end_epoch = math.ceil(ended.timestamp())
        result.update(
            status="verified", reason="acceptance_task_receipt_verified", task_id=task_id,
            acceptance_sha=acceptance_sha, receiver_sha=receiver_sha,
            window_start=start_epoch, window_end=end_epoch,
        )
    except Refusal as exc:
        result["reason"] = exc.reason
    except Exception:
        result["reason"] = "acceptance_validation_failed"
    return result


def main() -> int:
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        result = dict(SCHEMA)
        result["reason"] = "input_too_large"
    else:
        try:
            result = evaluate(json.loads(raw))
        except (json.JSONDecodeError, UnicodeError):
            result = dict(SCHEMA)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
