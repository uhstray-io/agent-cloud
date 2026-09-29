#!/usr/bin/env python3
"""Plan and verify a narrow Proxmox backup-job membership update."""

from __future__ import annotations

import copy
import json
import re
import sys
from collections.abc import Mapping

JOB_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
VMID_LIST = re.compile(r"[1-9][0-9]*(?:,[1-9][0-9]*)*")
VMID = re.compile(r"[1-9][0-9]*")
TRANSIENT_FIELDS = {"next-run"}


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
    return {key: copy.deepcopy(value) for key, value in job.items() if key not in TRANSIENT_FIELDS}


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


def main() -> int:
    try:
        operation = sys.argv[1]
        payload = json.load(sys.stdin)
        if operation == "plan":
            result = prepare_plan(payload)
        elif operation == "inspect":
            result = inspect_candidates(payload)
        elif operation == "recheck":
            result = assert_unchanged(payload["plan"], payload["detail"])
        elif operation == "verify":
            result = verify_readback(payload["plan"], payload["detail"])
        else:
            raise ValueError("Unsupported reconciliation operation.")
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"Backup-job reconciliation refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
