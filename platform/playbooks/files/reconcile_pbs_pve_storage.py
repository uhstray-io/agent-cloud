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


def _validate_access_proofs(*, api_base: Any, storage_id: str, expected_nodes: list[str], proofs: Any) -> None:
    """Require a controller-authenticated response for each declared node/storage path."""
    if not isinstance(proofs, list):
        _refuse("PVE storage credential access has not been verified.")
    if not proofs:
        _refuse("PVE storage credential access has not been verified.")
    if not isinstance(api_base, str) or not re.fullmatch(r"https://[^/]+", api_base):
        _refuse("PVE storage-access endpoint is not a fixed HTTPS origin.")
    observed: dict[str, Any] = {}
    for proof in proofs:
        if not isinstance(proof, dict):
            _refuse("PVE storage credential access returned an unsupported schema.")
        node = proof.get("node", proof.get("item"))
        if not isinstance(node, str) or node in observed:
            _refuse("PVE storage credential access has malformed or duplicate node results.")
        expected_url = f"{api_base}/api2/json/nodes/{node}/storage/{storage_id}/content?content=backup"
        if proof.get("url") != expected_url or proof.get("status") != 200:
            _refuse("PVE storage credential access did not verify the exact node and storage path.")
        data = proof.get("data", proof.get("json", {}).get("data") if isinstance(proof.get("json"), dict) else None)
        if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
            _refuse("PVE storage credential access returned an unsupported content-list schema.")
        observed[node] = True
    if set(observed) != set(expected_nodes):
        _refuse("PVE storage credential access is missing a declared node.")


def preflight_storage(
    *, storage_id: Any, rows: Any, current: Any, expected: Any,
) -> dict[str, Any]:
    """Validate existing configuration except a redacted secret before probing it."""
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
    if sid not in ids:
        if current not in (None, {}):
            _refuse("PVE target storage is absent from the list but a configuration object exists.")
        return {"action": "create", "storage_id": sid}
    if not isinstance(current, dict) or current.get("storage", sid) != sid:
        _refuse("PVE target storage configuration is missing or identifies a different ID.")
    actual = dict(current)
    actual.pop("password", None)
    target.pop("password", None)
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
    unknown = set(actual) - set(target) - {"storage", "digest"}
    if unknown:
        _refuse("PVE target storage has undeclared settings; preserve and inspect it manually.")
    return {"action": "verify_access", "storage_id": sid}


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
    if not node_list or any(
        not isinstance(node, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", node)
        for node in node_list
    ):
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
    nodes_after: Any = None, api_base: Any = None, access_proofs: Any = None,
    original_rows: Any = None,
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
        nodes = target["nodes"].split(",")
        _validate_access_proofs(
            api_base=api_base, storage_id=sid, expected_nodes=nodes, proofs=access_proofs,
        )
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
        comparison_rows = original_rows if original_rows is not None else rows
        if not isinstance(comparison_rows, list):
            _refuse("Original PVE storage snapshot has an unsupported schema.")
        before_existing = [row for row in comparison_rows if isinstance(row, dict) and row.get("storage") != sid]
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
    api_base: Any = None, access_proofs: Any = None,
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
        api_base=api_base,
        access_proofs=access_proofs,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "prewrite", "preflight"))
    args = parser.parse_args()
    try:
        payload = json.load(sys.stdin)
        if args.action == "preflight":
            result = preflight_storage(
                storage_id=payload.get("storage_id"), rows=payload.get("rows"),
                current=payload.get("current"), expected=payload.get("expected"),
            )
        elif args.action == "plan":
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
                api_base=payload.get("api_base"),
                access_proofs=payload.get("access_proofs"),
                original_rows=payload.get("original_rows"),
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
                api_base=payload.get("api_base"),
                access_proofs=payload.get("access_proofs"),
            )
        print(json.dumps(result, sort_keys=True))
        return 0
    except (Refusal, json.JSONDecodeError, UnicodeDecodeError) as exc:
        print(
            json.dumps(
                {"refusal": str(exc) if isinstance(exc, Refusal) else "PVE storage input is not valid JSON."},
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
