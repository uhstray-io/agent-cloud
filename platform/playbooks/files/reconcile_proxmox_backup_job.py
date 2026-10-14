#!/usr/bin/env python3
"""Plan and verify a narrow Proxmox backup-job membership update."""

from __future__ import annotations

import copy
import json
import re
import sys
import time
from collections.abc import Mapping
from urllib.parse import quote

JOB_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
VMID_LIST = re.compile(r"[1-9][0-9]*(?:,[1-9][0-9]*)*")
VMID = re.compile(r"[1-9][0-9]*")
NODE_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?")
TRANSIENT_FIELDS = {"next-run"}
SAFE_REFUSALS = {
    "The declared backup job ID is missing or malformed.",
    "The declared VMID is missing or malformed.",
    "The declared VMID is outside the Proxmox range.",
    "Proxmox returned a malformed backup-job list.",
    "Proxmox returned a malformed backup job.",
    "Proxmox returned a backup job with a malformed ID.",
    "Proxmox returned duplicate backup-job IDs.",
    "The declared backup job is missing or ambiguous.",
    "The selected backup job identity changed.",
    "The selected job is not a vzdump backup job.",
    "The selected backup job is disabled or has malformed enabled state.",
    "The selected backup job uses an unsupported all selector.",
    "The selected backup job uses an unsupported pool or exclude selector.",
    "The selected backup job has malformed explicit VMID membership.",
    "The selected backup job has duplicate VMID members.",
    "The backup-job list and detail read do not match.",
    "The selected backup job changed after the initial inspection.",
    "The backup-job VMID readback does not match the requested state.",
    "The backup-job readback shows an unexpected option change.",
    "Unsupported reconciliation operation.",
}
FALLBACK_REFUSAL = "Backup-job reconciliation refused because Proxmox returned invalid data."
HISTORY_PAGE_SIZE = 50
HISTORY_MAX_ROWS = 250
HISTORY_LOOKBACK_SECONDS = 30 * 24 * 60 * 60
HISTORY_RECENCY_SECONDS = 26 * 60 * 60
NODE_TASK_LOOKBACK_SECONDS = 72 * 60 * 60
TASK_LOG_MAX_LINES = 100
TASK_LOG_MAX_LINE_BYTES = 4096
TASK_LOG_MAX_TOTAL_LINES = 10_000_000
TASK_LOG_REASONS = {
    "permission_unverified", "node_history_unavailable", "no_recent_unique_failed_task",
    "ambiguous_latest_task", "metadata_invalid", "log_unavailable", "log_invalid",
}
HISTORY_REASONS = {
    "permission_unverified",
    "response_invalid",
    "request_failed",
    "pagination_incomplete",
    "pagination_limit_exceeded",
    "task_schema_invalid",
    "task_identity_invalid",
    "task_status_unavailable",
}
SCHEDULER_REASONS = {
    "permission_unverified",
    "request_failed",
    "response_invalid",
    "service_identity_invalid",
    "state_unavailable",
}
NODE_TASK_REASONS = {
    "permission_unverified", "request_failed", "response_invalid",
    "pagination_incomplete", "pagination_limit_exceeded", "task_schema_invalid",
    "task_identity_invalid", "task_status_unavailable",
}
SCHEDULER_ACTIVE_STATES = {
    "active", "inactive", "failed", "activating", "deactivating", "maintenance",
    "reloading", "refreshing", "unknown",
}
SCHEDULER_STATES = {
    "dead", "condition", "start-pre", "start", "start-post", "running", "exited",
    "reload", "reload-signal", "reload-notify", "mounting", "stop", "stop-watchdog",
    "stop-sigterm", "stop-sigkill", "stop-post", "final-watchdog", "final-sigterm",
    "final-sigkill", "failed", "dead-before-auto-restart", "failed-before-auto-restart",
    "dead-resources-pinned", "auto-restart", "auto-restart-queued", "cleaning", "unknown",
}
SCHEDULER_UNIT_STATES = {
    "enabled", "enabled-runtime", "linked", "linked-runtime", "alias", "masked",
    "masked-runtime", "static", "disabled", "indirect", "generated", "transient",
    "bad", "not-found", "unknown",
}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _validate_job_id(value: object) -> str:
    _require(isinstance(value, str) and JOB_ID.fullmatch(value) is not None,
             "The declared backup job ID is missing or malformed.")
    return value


def _parse_vmid(value: object) -> int:
    _require(isinstance(value, str) and VMID.fullmatch(value) is not None,
             "The declared VMID is missing or malformed.")
    parsed = int(value)
    _require(100 <= parsed <= 999_999_999, "The declared VMID is outside the Proxmox range.")
    return parsed


def _stable_config(job: Mapping[str, object]) -> dict[str, object]:
    stable = {key: copy.deepcopy(value) for key, value in job.items() if key not in TRANSIENT_FIELDS}
    all_value = stable.get("all")
    if all_value is None or all_value is False or (type(all_value) is int and all_value == 0):
        stable.pop("all", None)
    for selector in ("pool", "exclude"):
        if stable.get(selector) in (None, ""):
            stable.pop(selector, None)
    return stable


def _safe_refusal(exc: BaseException) -> str:
    message = str(exc)
    return message if message in SAFE_REFUSALS else FALLBACK_REFUSAL


def inspect_candidates(payload: Mapping[str, object]) -> dict[str, object]:
    """Return a small allow-listed view of the backup-job list for survey mode."""
    target_vmid = _parse_vmid(str(payload.get("vmid", "")))
    jobs = payload.get("jobs")
    _require(isinstance(jobs, list), "Proxmox returned a malformed backup-job list.")
    _require(all(isinstance(job, Mapping) for job in jobs),
             "Proxmox returned a malformed backup-job list.")

    candidates = []
    for job in jobs:
        job_id = job.get("id")
        _require(isinstance(job_id, str) and JOB_ID.fullmatch(job_id) is not None,
                 "Proxmox returned a backup job with a malformed ID.")
        enabled = job.get("enabled", True)
        if enabled is True or (type(enabled) is int and enabled == 1):
            enabled_status = "enabled"
        elif enabled is False or (type(enabled) is int and enabled == 0):
            enabled_status = "disabled"
        else:
            enabled_status = "unknown"

        raw_vmids = job.get("vmid")
        if isinstance(raw_vmids, str) and VMID_LIST.fullmatch(raw_vmids) is not None:
            member_ids = [_parse_vmid(value) for value in raw_vmids.split(",")]
            explicit_membership = len(member_ids) == len(set(member_ids))
        else:
            member_ids = []
            explicit_membership = False

        if job.get("all") is True or job.get("all") == 1:
            selector = "all"
        elif job.get("pool") not in (None, ""):
            selector = "pool"
        elif job.get("exclude") not in (None, ""):
            selector = "exclude"
        elif explicit_membership:
            selector = "explicit-vmid"
        else:
            selector = "malformed-or-unspecified"

        candidates.append({
            "id": job_id,
            "type": job.get("type") if isinstance(job.get("type"), str) else "vzdump",
            "enabled": enabled_status,
            "selector": selector,
            "member_count": len(member_ids) if explicit_membership else None,
            "includes_declared_o11y_vmid": target_vmid in member_ids if explicit_membership else None,
            "schedule": job.get("schedule") if isinstance(job.get("schedule"), str) else None,
        })
    _require(len({candidate["id"] for candidate in candidates}) == len(candidates),
             "Proxmox returned duplicate backup-job IDs.")
    return {"candidate_count": len(candidates), "candidates": candidates}


def _validated_membership(job: object, job_id: str) -> list[int]:
    _require(isinstance(job, Mapping), "Proxmox returned a malformed backup job.")
    _require(job.get("id") == job_id, "The selected backup job identity changed.")
    _require(job.get("type", "vzdump") == "vzdump", "The selected job is not a vzdump backup job.")

    enabled = job.get("enabled", True)  # Proxmox documents enabled=true as the default.
    _require(enabled is True or (type(enabled) is int and enabled == 1),
             "The selected backup job is disabled or has malformed enabled state.")

    all_mode = job.get("all")
    _require(all_mode is None or all_mode is False or (type(all_mode) is int and all_mode == 0),
             "The selected backup job uses an unsupported all selector.")
    for selector in ("pool", "exclude"):
        value = job.get(selector)
        _require(selector not in job or value is None or value == "",
                 "The selected backup job uses an unsupported pool or exclude selector.")

    raw_vmids = job.get("vmid")
    _require(isinstance(raw_vmids, str) and VMID_LIST.fullmatch(raw_vmids) is not None,
             "The selected backup job has malformed explicit VMID membership.")
    members = [_parse_vmid(value) for value in raw_vmids.split(",")]
    _require(len(members) == len(set(members)), "The selected backup job has duplicate VMID members.")
    return members


def prepare_plan(payload: Mapping[str, object]) -> dict[str, object]:
    job_id = _validate_job_id(payload.get("job_id"))
    target_vmid = _parse_vmid(str(payload.get("vmid", "")))
    jobs = payload.get("jobs")
    _require(isinstance(jobs, list), "Proxmox returned a malformed backup-job list.")
    _require(all(isinstance(job, Mapping) and isinstance(job.get("id"), str) for job in jobs),
             "Proxmox returned a malformed backup-job list.")
    listed_ids = [job["id"] for job in jobs]
    _require(len(listed_ids) == len(set(listed_ids)), "Proxmox returned duplicate backup-job IDs.")
    selected = [job for job in jobs if job["id"] == job_id]
    _require(len(selected) == 1, "The declared backup job is missing or ambiguous.")

    detail = payload.get("detail")
    members = _validated_membership(detail, job_id)
    _require(_stable_config(selected[0]) == _stable_config(detail),
             "The backup-job list and detail read do not match.")
    desired = members if target_vmid in members else [*members, target_vmid]
    return {
        "job_id": job_id,
        "vmid": target_vmid,
        "before": _stable_config(detail),
        "before_vmids": members,
        "desired_vmids": desired,
        "desired_vmid": ",".join(str(value) for value in desired),
        "changed": target_vmid not in members,
    }


def assert_unchanged(plan: Mapping[str, object], refreshed: object) -> dict[str, bool]:
    job_id = _validate_job_id(plan.get("job_id"))
    _validated_membership(refreshed, job_id)
    _require(_stable_config(refreshed) == plan.get("before"),
             "The selected backup job changed after the initial inspection.")
    return {"unchanged": True}


def verify_readback(plan: Mapping[str, object], refreshed: object) -> dict[str, object]:
    job_id = _validate_job_id(plan.get("job_id"))
    members = _validated_membership(refreshed, job_id)
    expected_members = plan["desired_vmids"] if plan["changed"] else plan["before_vmids"]
    _require(members == expected_members, "The backup-job VMID readback does not match the requested state.")

    expected_config = copy.deepcopy(plan["before"])
    expected_config["vmid"] = ",".join(str(value) for value in expected_members)
    _require(_stable_config(refreshed) == expected_config,
             "The backup-job readback shows an unexpected option change.")
    return {
        "verified": True,
        "member_count": len(members),
        "target_member": plan["vmid"] in members,
    }


def _history_unknown(reason: str) -> dict[str, object]:
    if reason not in HISTORY_REASONS:
        reason = "response_invalid"
    return {
        "outcome": "unknown",
        "reason": reason,
        "task_count": None,
        "job_attribution": "unavailable",
        "latest_success_within_26h": None,
    }


def _scheduler_unknown(reason: str) -> dict[str, object]:
    if reason not in SCHEDULER_REASONS:
        reason = "response_invalid"
    return {"status": "unknown", "reason": reason}


def summarize_scheduler_state(payload: Mapping[str, object]) -> dict[str, object]:
    """Return a fixed status for one permission-verified pvescheduler snapshot."""
    if not isinstance(payload, Mapping):
        return _scheduler_unknown("response_invalid")
    if payload.get("permissions_verified") is not True:
        return _scheduler_unknown("permission_unverified")

    response = payload.get("response")
    if not isinstance(response, Mapping) or type(response.get("status")) is not int:
        return _scheduler_unknown("response_invalid")
    if response["status"] != 200:
        return _scheduler_unknown("request_failed")

    body = response.get("json")
    data = body.get("data") if isinstance(body, Mapping) else None
    if not isinstance(data, Mapping):
        return _scheduler_unknown("response_invalid")

    expected_fields = {"service", "name", "desc", "active-state", "state", "unit-state"}
    if set(data) != expected_fields or any(not isinstance(data.get(key), str) for key in expected_fields):
        return _scheduler_unknown("response_invalid")
    if data["service"] != "pvescheduler" or data["name"] != "pvescheduler":
        return _scheduler_unknown("service_identity_invalid")

    active_state = data["active-state"]
    state = data["state"]
    unit_state = data["unit-state"]
    if (active_state not in SCHEDULER_ACTIVE_STATES
            or state not in SCHEDULER_STATES
            or unit_state not in SCHEDULER_UNIT_STATES):
        return _scheduler_unknown("state_unavailable")
    if unit_state in {"bad", "not-found"}:
        return _scheduler_unknown("state_unavailable")
    if active_state == "active" and state == "running":
        return {"status": "running", "reason": None}
    if (active_state, state) in {("inactive", "dead"), ("failed", "failed")}:
        return {"status": "not_running", "reason": None}
    return _scheduler_unknown("state_unavailable")


def _node_task_unknown(reason: str) -> dict[str, object]:
    if reason not in NODE_TASK_REASONS:
        reason = "response_invalid"
    return {
        "status": "unknown",
        "reason": reason,
        "job_attribution": "unavailable",
        "total_recent_node_vzdump_tasks": None,
        "target_vmid_task_id_count": None,
        "potential_multi_vm_or_unattributed_task_count": None,
        "latest_task_outcome": "unknown",
        "latest_task_recency": "unknown",
    }


def _task_log_result(
    status: str, reason: str | None = None, failure_hint: str = "unknown",
) -> dict[str, object]:
    if not isinstance(status, str) or status not in {"unknown", "not_applicable", "diagnosed"}:
        status = "unknown"
    if reason is not None and (not isinstance(reason, str) or reason not in TASK_LOG_REASONS):
        reason = "log_invalid"
    if not isinstance(failure_hint, str) or failure_hint not in {
        "no_space", "storage_unavailable", "permission_denied", "guest_lock", "transport", "unknown",
    }:
        failure_hint = "unknown"
    return {"status": status, "reason": reason, "failure_hint": failure_hint}


def _task_log_path(node: str, upid: str, start: int, limit: int) -> str:
    return f"/api2/json/nodes/{node}/tasks/{quote(upid, safe='')}/log?start={start}&limit={limit}"


def _valid_node_task_upid(upid: object, node: str, starttime: object) -> bool:
    if (not isinstance(node, str) or NODE_NAME.fullmatch(node) is None
            or not isinstance(upid, str) or not upid.isascii() or len(upid) > 512):
        return False
    pattern = (
        rf"UPID:{re.escape(node)}:([0-9A-Fa-f]{{8}}):"
        r"([0-9A-Fa-f]{8,9}):([0-9A-Fa-f]{8}):vzdump:"
        r"([1-9][0-9]*|):([A-Za-z0-9._-]+@[A-Za-z0-9._-]+(?:![A-Za-z0-9._-]+)?):"
    )
    match = re.fullmatch(pattern, upid)
    return match is not None and type(starttime) is int and int(match.group(3), 16) == starttime


def prepare_node_task_query(payload: Mapping[str, object]) -> dict[str, object]:
    """Build the node-wide archive query after the existing exact-node Sys.Audit check."""
    node = payload.get("node")
    _require(isinstance(node, str) and NODE_NAME.fullmatch(node) is not None,
             "The declared VM node is malformed.")
    target_vmid = _parse_vmid(str(payload.get("vmid", "")))
    wrapper = payload.get("permissions_response")
    if not isinstance(wrapper, Mapping) or type(wrapper.get("status")) is not int or wrapper["status"] != 200:
        return {"read_allowed": False, "summary": _node_task_unknown("permission_unverified")}
    body = wrapper.get("json")
    data = body.get("data") if isinstance(body, Mapping) else None
    rights = data.get(f"/nodes/{node}") if isinstance(data, Mapping) else None
    audit = rights.get("Sys.Audit") if isinstance(rights, Mapping) else None
    if type(audit) is not int or audit != 1:
        return {"read_allowed": False, "summary": _node_task_unknown("permission_unverified")}
    until = payload.get("until", int(time.time()))
    if type(until) is not int or until <= 0:
        return {"read_allowed": False, "summary": _node_task_unknown("response_invalid")}
    return {
        "read_allowed": True,
        "node": node,
        "query": {
            "source": "archive", "typefilter": "vzdump",
            "since": max(0, until - NODE_TASK_LOOKBACK_SECONDS), "until": until,
            "start": 0, "limit": HISTORY_PAGE_SIZE,
        },
        "target_vmid": target_vmid,
        "summary": None,
    }


def summarize_node_task_history(payload: Mapping[str, object]) -> dict[str, object]:
    """Summarize bounded node-wide vzdump rows without exposing task identity or text."""
    plan = payload.get("plan")
    if not isinstance(plan, Mapping) or plan.get("read_allowed") is not True:
        return _node_task_unknown("permission_unverified")
    first_response = payload.get("first_page")
    if isinstance(first_response, Mapping) and first_response.get("status") != 200:
        return _node_task_unknown("request_failed")
    first = _valid_task_page(first_response)
    if first is None:
        return _node_task_unknown("response_invalid")
    total = first["total"]
    if total > HISTORY_MAX_ROWS:
        return _node_task_unknown("pagination_limit_exceeded")
    later = payload.get("later_pages", [])
    if not isinstance(later, list):
        return _node_task_unknown("pagination_incomplete")
    expected_pages = (total + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE
    if len(later) != max(0, expected_pages - 1):
        return _node_task_unknown("pagination_incomplete")
    pages = [first]
    for response in later:
        if isinstance(response, Mapping) and response.get("status") != 200:
            return _node_task_unknown("request_failed")
        page = _valid_task_page(response)
        if page is None or page["total"] != total:
            return _node_task_unknown("pagination_incomplete")
        pages.append(page)
    rows: list[Mapping[str, object]] = []
    for index, page in enumerate(pages):
        expected_count = min(HISTORY_PAGE_SIZE, max(0, total - index * HISTORY_PAGE_SIZE))
        if len(page["rows"]) != expected_count or any(not isinstance(row, Mapping) for row in page["rows"]):
            return _node_task_unknown("pagination_incomplete")
        rows.extend(page["rows"])
    if len(rows) != total:
        return _node_task_unknown("pagination_incomplete")

    query = plan.get("query")
    try:
        since, until, target_vmid = query["since"], query["until"], plan["target_vmid"]
    except (KeyError, TypeError):
        return _node_task_unknown("response_invalid")
    if (not isinstance(query, Mapping) or type(since) is not int or type(until) is not int
            or since < 0 or until <= 0 or since > until
            or type(target_vmid) is not int or target_vmid < 100):
        return _node_task_unknown("response_invalid")
    seen_upids: set[str] = set()
    target_count = missing_id_count = 0
    classified: list[tuple[int, str]] = []
    for row in rows:
        upid, task_type, starttime = row.get("upid"), row.get("type"), row.get("starttime")
        task_id, status = row.get("id"), row.get("status")
        if (not isinstance(upid, str) or not upid or upid in seen_upids
                or task_type != "vzdump" or type(starttime) is not int
                or not since <= starttime <= until):
            return _node_task_unknown("task_identity_invalid")
        if "endtime" in row and (type(row["endtime"]) is not int or row["endtime"] < starttime):
            return _node_task_unknown("task_schema_invalid")
        if task_id is None or task_id == "":
            missing_id_count += 1
        elif ((not isinstance(task_id, str) and type(task_id) is not int)
              or re.fullmatch(r"[1-9][0-9]*", str(task_id)) is None):
            return _node_task_unknown("task_identity_invalid")
        elif str(task_id) == str(target_vmid):
            target_count += 1
        if not isinstance(status, str) or not status:
            return _node_task_unknown("task_status_unavailable")
        classified.append((starttime, _classify_task_status(status)))
        seen_upids.add(upid)

    if not rows:
        latest_outcome, latest_recency = "none", "none"
    else:
        latest_time = max(starttime for starttime, _ in classified)
        outcomes = {outcome for starttime, outcome in classified if starttime == latest_time}
        latest_outcome = outcomes.pop() if len(outcomes) == 1 else "unknown"
        if latest_outcome == "unknown":
            return _node_task_unknown("task_status_unavailable")
        latest_recency = "within_26h" if 0 <= until - latest_time <= HISTORY_RECENCY_SECONDS else "older_than_26h"
    return {
        "status": "complete",
        "reason": None,
        "total_recent_node_vzdump_tasks": total,
        "target_vmid_task_id_count": target_count,
        "potential_multi_vm_or_unattributed_task_count": missing_id_count,
        "job_attribution": "unavailable",
        "latest_task_outcome": latest_outcome,
        "latest_task_recency": latest_recency,
    }


def prepare_history_query(payload: Mapping[str, object]) -> dict[str, object]:
    """Build a bounded archive query only with verified node-scoped Sys.Audit."""
    node = payload.get("node")
    vmid = payload.get("vmid")
    _require(isinstance(node, str) and re.fullmatch(r"[A-Za-z0-9._-]+", node) is not None,
             "The declared VM node is malformed.")
    target_vmid = _parse_vmid(str(vmid or ""))
    path = f"/nodes/{node}"
    wrapper = payload.get("permissions_response")
    if (not isinstance(wrapper, Mapping) or type(wrapper.get("status")) is not int
            or wrapper["status"] != 200):
        return {"read_allowed": False, "summary": _history_unknown("permission_unverified")}
    body = wrapper.get("json")
    data = body.get("data") if isinstance(body, Mapping) else None
    rights = data.get(path) if isinstance(data, Mapping) else None
    audit = rights.get("Sys.Audit") if isinstance(rights, Mapping) else None
    if type(audit) is not int or audit != 1:
        return {"read_allowed": False, "summary": _history_unknown("permission_unverified")}
    until = payload.get("until", int(time.time()))
    if type(until) is not int or until <= 0:
        return {"read_allowed": False, "summary": _history_unknown("response_invalid")}
    return {
        "read_allowed": True,
        "query": {
            "source": "archive",
            "typefilter": "vzdump",
            "vmid": target_vmid,
            "since": max(0, until - HISTORY_LOOKBACK_SECONDS),
            "until": until,
            "start": 0,
            "limit": HISTORY_PAGE_SIZE,
        },
        "summary": None,
    }


def plan_history_pages(payload: Mapping[str, object]) -> dict[str, object]:
    """Validate the first archive page and return bounded offsets or unknown."""
    if payload.get("read_allowed") is not True:
        return {"page_starts": [], "summary": _history_unknown("permission_unverified")}
    response = payload.get("first_page")
    if isinstance(response, Mapping) and response.get("status") != 200:
        return {"page_starts": [], "summary": _history_unknown("request_failed")}
    page = _valid_task_page(response)
    if page is None:
        return {"page_starts": [], "summary": _history_unknown("response_invalid")}
    total = page["total"]
    if total > HISTORY_MAX_ROWS:
        return {"page_starts": [], "summary": _history_unknown("pagination_limit_exceeded")}
    return {
        "page_starts": list(range(HISTORY_PAGE_SIZE, total, HISTORY_PAGE_SIZE)),
        "summary": None,
    }


def _valid_task_page(response: object) -> dict[str, object] | None:
    if not isinstance(response, Mapping) or response.get("status") != 200:
        return None
    body = response.get("json")
    if not isinstance(body, Mapping):
        return None
    rows = body.get("data")
    total = body.get("total")
    if isinstance(rows, list) and not rows and "total" not in body:
        total = 0
    if (not isinstance(rows, list) or type(total) is not int or total < 0
            or len(rows) > HISTORY_PAGE_SIZE):
        return None
    return {"rows": rows, "total": total}


def summarize_task_history(payload: Mapping[str, object]) -> dict[str, object]:
    """Classify the latest archived vzdump task without exposing row identity or text."""
    plan = payload.get("plan")
    if not isinstance(plan, Mapping) or plan.get("read_allowed") is not True:
        return _history_unknown("permission_unverified")
    first_response = payload.get("first_page")
    if isinstance(first_response, Mapping) and first_response.get("status") != 200:
        return _history_unknown("request_failed")
    first = _valid_task_page(first_response)
    if first is None:
        return _history_unknown("response_invalid")
    total = first["total"]
    if total > HISTORY_MAX_ROWS:
        return _history_unknown("pagination_limit_exceeded")
    later = payload.get("later_pages", [])
    if not isinstance(later, list):
        return _history_unknown("pagination_incomplete")
    expected_pages = (total + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE
    if len(later) != max(0, expected_pages - 1):
        return _history_unknown("pagination_incomplete")
    pages = [first]
    for response in later:
        if isinstance(response, Mapping) and response.get("status") != 200:
            return _history_unknown("request_failed")
        page = _valid_task_page(response)
        if page is None or page["total"] != total:
            return _history_unknown("pagination_incomplete")
        pages.append(page)
    rows: list[Mapping[str, object]] = []
    for index, page in enumerate(pages):
        offset = index * HISTORY_PAGE_SIZE
        expected_count = min(HISTORY_PAGE_SIZE, max(0, total - offset))
        page_rows = page["rows"]
        if len(page_rows) != expected_count or any(not isinstance(row, Mapping) for row in page_rows):
            return _history_unknown("pagination_incomplete")
        rows.extend(page_rows)
    if len(rows) != total:
        return _history_unknown("pagination_incomplete")

    query = plan.get("query")
    if not isinstance(query, Mapping):
        return _history_unknown("response_invalid")
    until = query.get("until")
    if type(until) is not int or until <= 0:
        return _history_unknown("response_invalid")
    try:
        vmid = str(query["vmid"])
        since = int(query["since"])
    except (KeyError, TypeError, ValueError):
        return _history_unknown("response_invalid")
    seen_upids: set[str] = set()
    for row in rows:
        upid, task_type, task_id = row.get("upid"), row.get("type"), row.get("id")
        starttime = row.get("starttime")
        if (not isinstance(upid, str) or not upid or upid in seen_upids
                or task_type != "vzdump" or str(task_id) != vmid
                or type(starttime) is not int or not since <= starttime <= until):
            return _history_unknown("task_identity_invalid")
        if "endtime" in row and (type(row["endtime"]) is not int or row["endtime"] < starttime):
            return _history_unknown("task_schema_invalid")
        seen_upids.add(upid)
    if not rows:
        return {
            "outcome": "no_archived_task_in_lookback",
            "reason": None,
            "task_count": 0,
            "job_attribution": "unavailable",
            "latest_success_within_26h": False,
        }
    latest_time = max(int(row["starttime"]) for row in rows)
    latest = [row for row in rows if row["starttime"] == latest_time]
    outcomes = {_classify_task_status(row.get("status")) for row in latest}
    outcome = outcomes.pop() if len(outcomes) == 1 else "unknown"
    reason = None if outcome != "unknown" else "task_status_unavailable"
    recent_success = None
    if outcome != "unknown":
        recent_success = outcome == "success" and 0 <= until - latest_time <= HISTORY_RECENCY_SECONDS
    return {
        "outcome": outcome,
        "reason": reason,
        "task_count": total,
        "job_attribution": "unavailable",
        "latest_success_within_26h": recent_success,
    }


def _classify_task_status(status: object) -> str:
    if not isinstance(status, str) or not status:
        return "unknown"
    if status == "OK":
        return "success"
    if re.fullmatch(r"WARNINGS:\s*[0-9]+", status):
        return "incomplete"
    if status == "unexpected status":
        return "unknown"
    # PVE reports task errors as arbitrary text; callers must keep this text private.
    return "failed"


def prepare_node_failure_plan(payload: Mapping[str, object]) -> dict[str, object]:
    """Select one recent failed node task from the already bounded, permission-checked listing."""
    plan = payload.get("plan")
    history = summarize_node_task_history(payload)
    if not isinstance(plan, Mapping) or plan.get("read_allowed") is not True:
        return {"diagnostic": _task_log_result("unknown", "permission_unverified"), "read_allowed": False}
    if history.get("status") != "complete":
        return {"diagnostic": _task_log_result("unknown", "node_history_unavailable"), "read_allowed": False}
    if history.get("latest_task_outcome") != "failed" or history.get("latest_task_recency") != "within_26h":
        return {"diagnostic": _task_log_result("not_applicable", "no_recent_unique_failed_task"), "read_allowed": False}

    rows = list(payload["first_page"]["json"]["data"])
    for response in payload.get("later_pages", []):
        rows.extend(response["json"]["data"])
    node = plan.get("node")
    if not isinstance(node, str) or NODE_NAME.fullmatch(node) is None:
        return {"diagnostic": _task_log_result("unknown", "node_history_unavailable"), "read_allowed": False}
    for row in rows:
        if (_classify_task_status(row.get("status")) == "unknown"
                or not _valid_node_task_upid(row.get("upid"), node, row.get("starttime"))):
            return {"diagnostic": _task_log_result("unknown", "node_history_unavailable"), "read_allowed": False}
    latest_time = max(row["starttime"] for row in rows)
    latest = [row for row in rows if row["starttime"] == latest_time]
    if len(latest) != 1:
        return {"diagnostic": _task_log_result("unknown", "ambiguous_latest_task"), "read_allowed": False}
    row = latest[0]
    upid = row.get("upid")
    return {
        "read_allowed": True,
        "diagnostic": None,
        "metadata_path": _task_log_path(node, upid, 0, 1),
    }


def plan_node_failure_log_page(payload: Mapping[str, object]) -> dict[str, object]:
    """Validate the one-line metadata read and plan at most the last 100 task-log lines."""
    if payload.get("read_allowed") is not True:
        supplied = payload.get("diagnostic")
        diagnostic = (
            _task_log_result(supplied.get("status"), supplied.get("reason"), supplied.get("failure_hint", "unknown"))
            if isinstance(supplied, Mapping)
            else _task_log_result("unknown", "node_history_unavailable")
        )
        return {"read_allowed": False, "diagnostic": diagnostic}
    response = payload.get("metadata_response")
    if not isinstance(response, Mapping) or type(response.get("status")) is not int or response["status"] != 200:
        return {"read_allowed": False, "diagnostic": _task_log_result("unknown", "metadata_invalid")}
    body = response.get("json")
    if not isinstance(body, Mapping) or type(body.get("total")) is not int:
        return {"read_allowed": False, "diagnostic": _task_log_result("unknown", "metadata_invalid")}
    total, rows = body["total"], body.get("data")
    if total < 0 or total > TASK_LOG_MAX_TOTAL_LINES or not isinstance(rows, list):
        return {"read_allowed": False, "diagnostic": _task_log_result("unknown", "metadata_invalid")}
    expected_count = min(1, total)
    if len(rows) != expected_count:
        return {"read_allowed": False, "diagnostic": _task_log_result("unknown", "metadata_invalid")}
    if rows and (
        not _valid_task_log_row(rows[0], 1)
        or rows[0]["t"].strip().lower() == "no content"
        or rows[0]["t"].lower().startswith("unable to open file")
    ):
        return {"read_allowed": False, "diagnostic": _task_log_result("unknown", "metadata_invalid")}
    if total == 0:
        return {"read_allowed": False, "diagnostic": _task_log_result("unknown", "log_invalid")}
    start = max(0, total - TASK_LOG_MAX_LINES)
    return {
        "read_allowed": True,
        "total": total,
        "start": start,
        "limit": min(TASK_LOG_MAX_LINES, total),
        "log_path": re.sub(
            r"start=0&limit=1$",
            f"start={start}&limit={min(TASK_LOG_MAX_LINES, total)}",
            payload["metadata_path"],
        ),
        "diagnostic": None,
    }


def _valid_task_log_row(row: object, expected_number: int) -> bool:
    if not isinstance(row, Mapping) or set(row) != {"n", "t"}:
        return False
    if type(row.get("n")) is not int or row["n"] != expected_number or not isinstance(row.get("t"), str):
        return False
    try:
        return len(row["t"].encode("utf-8")) <= TASK_LOG_MAX_LINE_BYTES
    except UnicodeError:
        return False


def summarize_node_failure_log(payload: Mapping[str, object]) -> dict[str, object]:
    """Classify bounded task-log text into fixed failure hints without returning its contents."""
    page_plan = payload.get("page_plan")
    if not isinstance(page_plan, Mapping) or page_plan.get("read_allowed") is not True:
        diagnostic = page_plan.get("diagnostic") if isinstance(page_plan, Mapping) else None
        if isinstance(diagnostic, Mapping):
            return _task_log_result(
                diagnostic.get("status"), diagnostic.get("reason"), diagnostic.get("failure_hint", "unknown"),
            )
        return _task_log_result("unknown", "log_unavailable")
    response = payload.get("log_response")
    if not isinstance(response, Mapping) or type(response.get("status")) is not int or response["status"] != 200:
        return _task_log_result("unknown", "log_unavailable")
    body = response.get("json")
    rows = body.get("data") if isinstance(body, Mapping) else None
    total = body.get("total") if isinstance(body, Mapping) else None
    if (not isinstance(body, Mapping) or type(total) is not int or total != page_plan.get("total")
            or not isinstance(rows, list) or len(rows) != page_plan.get("limit")):
        return _task_log_result("unknown", "log_invalid")
    start = page_plan.get("start")
    if type(start) is not int or any(not _valid_task_log_row(row, start + index + 1) for index, row in enumerate(rows)):
        return _task_log_result("unknown", "log_invalid")
    if len(rows) == 1 and total == 1:
        only_line = rows[0]["t"].strip().lower()
        if only_line == "no content" or only_line.startswith("unable to open file"):
            return _task_log_result("unknown", "log_invalid")
    text = "\n".join(row["t"] for row in rows).lower()
    try:
        oversized = len(text.encode("utf-8")) > TASK_LOG_MAX_LINES * TASK_LOG_MAX_LINE_BYTES
    except UnicodeError:
        oversized = True
    if oversized:
        return _task_log_result("unknown", "log_invalid")
    patterns = (
        ("no_space", ("no space left", "out of space", "no space")),
        ("storage_unavailable", ("storage unavailable", "storage is not available", "unable to activate storage")),
        ("permission_denied", ("permission denied", "not authorized", "permission check failed")),
        ("guest_lock", ("backup lock", "locked (backup)", "unable to acquire lock")),
        ("transport", ("connection timed out", "connection refused", "network is unreachable", "transport endpoint")),
    )
    hint = next((code for code, needles in patterns if any(needle in text for needle in needles)), "unknown")
    return _task_log_result("diagnosed", None, hint)


def main() -> int:
    operation = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        payload = json.load(sys.stdin)
        if (
            operation in {"node-failure-plan", "node-failure-pages", "node-failure-summary"}
            and not isinstance(payload, Mapping)
        ):
            raise TypeError("Invalid operation input.")
        if operation == "plan":
            result = prepare_plan(payload)
        elif operation == "inspect":
            result = inspect_candidates(payload)
        elif operation == "recheck":
            result = assert_unchanged(payload["plan"], payload["detail"])
        elif operation == "verify":
            result = verify_readback(payload["plan"], payload["detail"])
        elif operation == "history-query":
            result = prepare_history_query(payload)
        elif operation == "history-pages":
            result = plan_history_pages(payload)
        elif operation == "history-summary":
            result = summarize_task_history(payload)
        elif operation == "scheduler-state":
            result = summarize_scheduler_state(payload)
        elif operation == "node-history-query":
            result = prepare_node_task_query(payload)
        elif operation == "node-history-summary":
            result = summarize_node_task_history(payload)
        elif operation == "node-failure-plan":
            result = prepare_node_failure_plan(payload)
        elif operation == "node-failure-pages":
            result = plan_node_failure_log_page(payload)
        elif operation == "node-failure-summary":
            result = summarize_node_failure_log(payload)
        else:
            raise ValueError("Unsupported reconciliation operation.")
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        if operation == "node-failure-plan":
            result = {
                "read_allowed": False,
                "diagnostic": _task_log_result("unknown", "node_history_unavailable"),
            }
        elif operation == "node-failure-pages":
            result = {
                "read_allowed": False,
                "diagnostic": _task_log_result("unknown", "metadata_invalid"),
            }
        elif operation == "node-failure-summary":
            result = _task_log_result("unknown", "log_invalid")
        else:
            print(json.dumps({"refusal": _safe_refusal(exc)}, separators=(",", ":")))
            return 2
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
