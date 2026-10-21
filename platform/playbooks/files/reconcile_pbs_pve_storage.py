#!/usr/bin/env python3
"""Plan one fail-closed PVE PBS-storage create; never rewrites existing config."""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any


class Refusal(ValueError):
    pass


MANAGED_FIELDS = {
    "type",
    "server",
    "port",
    "datastore",
    "username",
    "password",
    "fingerprint",
    "content",
    "nodes",
    "prune-backups",
    "disable",
    "shared",
}


def _stable_nodes(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        _refuse("PVE node listing has an unsupported schema.")
    normalized = []
    for row in value:
        if not isinstance(row, dict) or not isinstance(row.get("node"), str):
            _refuse("PVE node listing has malformed or duplicate rows.")
        item = {"node": row["node"]}
        if "status" in row:
            if not isinstance(row["status"], str):
                _refuse("PVE node status has an unsupported schema.")
            item["status"] = row["status"]
        normalized.append(item)
    if len({row["node"] for row in normalized}) != len(normalized):
        _refuse("PVE node listing has malformed or duplicate rows.")
    return sorted(normalized, key=lambda row: row["node"])


def _refuse(message: str) -> None:
    raise Refusal(message)


def _safe_identity(value: Any, pattern: str, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        _refuse(f"Private {label} is malformed.")
    return value


def normalize_expected(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        _refuse("Expected PBS storage settings are malformed.")
    expected = {key: value.get(key) for key in MANAGED_FIELDS}
    _safe_identity(expected["server"], r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", "PBS server")
    _safe_identity(expected["datastore"], r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", "PBS datastore")
    _safe_identity(expected["username"], r"[A-Za-z0-9._-]+@pbs![A-Za-z0-9._-]+", "PBS token identity")
    _safe_identity(expected["fingerprint"], r"(?:[0-9A-Fa-f]{2}:?){32}", "PBS certificate fingerprint")
    _safe_identity(expected["content"], r"backup", "PVE content")
    nodes = expected["nodes"]
    if isinstance(nodes, str):
        node_list = nodes.split(",")
    elif isinstance(nodes, list):
        node_list = nodes
    else:
        _refuse("Private PVE node declaration is malformed.")
    if not node_list or any(not isinstance(node, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", node) for node in node_list):
        _refuse("Private PVE node declaration is malformed.")
    if len(set(node_list)) != len(node_list):
        _refuse("Private PVE node declaration contains duplicates.")
    expected.update({
        "type": "pbs",
        "port": 8007,
        "password": value.get("password"),
        "content": "backup",
        "nodes": ",".join(node_list),
        "prune-backups": "keep-all=1",
        "disable": "0",
        "shared": "0",
    })
    if not isinstance(expected["password"], str) or not expected["password"]:
        _refuse("OpenBao PBS token secret is missing.")
    return expected


def plan_storage(
    *, storage_id: Any, rows: Any, current: Any, expected: Any, jobs_before: Any,
    jobs_after: Any = None, rows_after: Any = None, nodes_before: Any = None,
    nodes_after: Any = None,
) -> dict[str, Any]:
    """Return create/noop only for an absent ID or a matching full managed config."""
    sid = _safe_identity(storage_id, r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", "PVE storage ID")
    target = normalize_expected(expected)
    if not isinstance(rows, list):
        _refuse("PVE storage list has an unsupported schema.")
    ids = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("storage"), str):
            _refuse("PVE storage list has a malformed row.")
        ids.append(row["storage"])
    if len(ids) != len(set(ids)):
        _refuse("PVE storage list duplicates an ID.")
    present = sid in ids
    if not present:
        if current not in (None, {}):
            _refuse("PVE target storage is absent from the list but a configuration object exists.")
        action = "create"
    else:
        if not isinstance(current, dict):
            _refuse("PVE target storage configuration is missing or malformed.")
        if current.get("storage", sid) != sid:
            _refuse("PVE target storage configuration identifies a different storage ID.")
        actual = dict(current)
        # PBS storage APIs redact the password on some supported PVE versions. If
        # returned, it must match exactly. If omitted/redacted, preserve the existing
        # object and rely on the separately certificate-pinned PBS auth proof.
        actual_password = actual.pop("password", None)
        expected_password = target.pop("password")
        if actual_password not in (None, "", "**********", "********") and actual_password != expected_password:
            _refuse("PVE target storage holds a different PBS token secret.")
        for key, value in target.items():
            observed = actual.get(key)
            if key == "nodes" and isinstance(observed, list):
                observed = ",".join(observed)
            if key == "nodes" and isinstance(observed, str):
                observed = ",".join(sorted(filter(None, observed.split(","))))
                value = ",".join(sorted(filter(None, value.split(","))))
            if key == "fingerprint" and isinstance(observed, str):
                observed = re.sub(r"[^0-9a-fA-F]", "", observed).lower()
                value = re.sub(r"[^0-9a-fA-F]", "", value).lower()
            if str(observed) != str(value):
                _refuse("PVE target storage differs from the declared PBS registration.")
        unknown = set(actual) - set(target) - {"storage", "digest", "password"}
        if unknown:
            _refuse("PVE target storage has undeclared settings; preserve and inspect it manually.")
        action = "noop"
    if not isinstance(jobs_before, list):
        _refuse("PVE backup-job listing has an unsupported schema.")
    if nodes_before is not None:
        stable_nodes = _stable_nodes(nodes_before)
        node_names = [row["node"] for row in stable_nodes]
        declared_nodes = target["nodes"].split(",")
        if not set(declared_nodes).issubset(set(node_names)):
            _refuse("A declared PVE storage node is not present in the live node listing.")
    if jobs_after is not None and jobs_after != jobs_before:
        _refuse("PVE backup-job configuration changed during storage registration.")
    if rows_after is not None:
        if not isinstance(rows_after, list):
            _refuse("PVE storage readback has an unsupported schema.")
        before_existing = [row for row in rows if isinstance(row, dict) and row.get("storage") != sid]
        after_existing = [row for row in rows_after if isinstance(row, dict) and row.get("storage") != sid]
        if before_existing != after_existing:
            _refuse("An existing PVE storage configuration changed during PBS registration.")
        final_ids = [row.get("storage") for row in rows_after if isinstance(row, dict)]
        if final_ids.count(sid) != 1:
            _refuse("PVE storage readback does not contain exactly one managed PBS storage.")
    if nodes_after is not None and _stable_nodes(nodes_after) != _stable_nodes(nodes_before):
        _refuse("PVE node configuration changed during PBS storage registration.")
    return {"action": action, "storage_id": sid, "backup_jobs_unchanged": True}


def validate_prewrite_snapshot(
    *, storage_id: Any, rows_before: Any, rows_now: Any, current: Any,
    expected: Any, jobs_before: Any, jobs_now: Any, nodes_before: Any, nodes_now: Any,
) -> dict[str, Any]:
    """Revalidate all source snapshots immediately before a create or no-op."""
    sid = _safe_identity(storage_id, r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", "PVE storage ID")
    if not isinstance(rows_before, list) or not isinstance(rows_now, list):
        _refuse("PVE storage snapshot has an unsupported schema.")
    before_other = [row for row in rows_before if isinstance(row, dict) and row.get("storage") != sid]
    now_other = [row for row in rows_now if isinstance(row, dict) and row.get("storage") != sid]
    if before_other != now_other:
        _refuse("An existing PVE storage configuration changed after preflight.")
    present = [row for row in rows_now if isinstance(row, dict) and row.get("storage") == sid]
    if len(present) > 1:
        _refuse("PVE pre-write storage list duplicates the managed ID.")
    if jobs_now != jobs_before:
        _refuse("PVE backup-job configuration changed after preflight.")
    if _stable_nodes(nodes_now) != _stable_nodes(nodes_before):
        _refuse("PVE node configuration changed after preflight.")
    if not present:
        if current not in (None, {}):
            _refuse("PVE target storage readback conflicts with its absent list entry.")
        return {"action": "create", "storage_id": sid}
    if not isinstance(current, dict):
        _refuse("PVE target storage exists but its exact configuration is unavailable.")
    return plan_storage(
        storage_id=sid,
        rows=rows_now,
        current=current,
        expected=expected,
        jobs_before=jobs_now,
        nodes_before=nodes_now,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "prewrite"))
    args = parser.parse_args()
    try:
        payload = json.load(sys.stdin)
        if args.action == "plan":
            result = plan_storage(
                storage_id=payload.get("storage_id"),
                rows=payload.get("rows"),
                current=payload.get("current"),
                expected=payload.get("expected"),
                jobs_before=payload.get("jobs_before"),
                jobs_after=payload.get("jobs_after"),
                rows_after=payload.get("rows_after"),
                nodes_before=payload.get("nodes_before"),
                nodes_after=payload.get("nodes_after"),
            )
        else:
            result = validate_prewrite_snapshot(
                storage_id=payload.get("storage_id"),
                rows_before=payload.get("rows_before"),
                rows_now=payload.get("rows_now"),
                current=payload.get("current"),
                expected=payload.get("expected"),
                jobs_before=payload.get("jobs_before"),
                jobs_now=payload.get("jobs_now"),
                nodes_before=payload.get("nodes_before"),
                nodes_now=payload.get("nodes_now"),
            )
        print(json.dumps(result, sort_keys=True))
        return 0
    except (Refusal, json.JSONDecodeError, UnicodeDecodeError) as exc:
        print(json.dumps({"refusal": str(exc) if isinstance(exc, Refusal) else "PVE storage input is not valid JSON."}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
