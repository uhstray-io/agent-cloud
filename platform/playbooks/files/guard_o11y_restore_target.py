#!/usr/bin/env python3
"""Validate fixed OpenBao state before managed QEMU writers perform side effects."""

from __future__ import annotations

import json
import re
import sys
from typing import Any

from o11y_restore_coordination import Refusal, declaration_fingerprint, read_snapshot, target_identity_fingerprint


def _candidate_vmid(value: object) -> int:
    if isinstance(value, str):
        if re.fullmatch(r"(?:0|[1-9][0-9]{0,8})", value) is None:
            raise Refusal("invalid_input")
        value = int(value)
    if type(value) is not int or not 100 <= value <= 999_999_999:
        raise Refusal("invalid_input")
    return value


def check_reserved_target(
    metadata_status: object,
    metadata_payload: object,
    data_status: object,
    data_payload: object,
    declaration: object,
    candidates: object,
) -> dict[str, Any]:
    """Allow unrelated VMIDs only when the fixed coordination state is trustworthy."""
    record, _version = read_snapshot(metadata_status, metadata_payload, data_status, data_payload)
    if not isinstance(candidates, list) or not candidates:
        raise Refusal("invalid_input")
    normalized_candidates = {_candidate_vmid(value) for value in candidates}
    if declaration is None:
        if record is not None:
            raise Refusal("record_binding_mismatch")
        return {"result": "allowed", "reason": "coordination_absent"}

    declaration_fp = declaration_fingerprint(declaration)
    target_vmid = _candidate_vmid(declaration["vmid"])
    target_ref = {"node": declaration["node"], "vmid": target_vmid}
    target_fp = target_identity_fingerprint(target_ref)
    if record is not None and (
        record["declaration_fingerprint"] != declaration_fp or record["target_fingerprint"] != target_fp
    ):
        raise Refusal("record_binding_mismatch")
    if target_vmid in normalized_candidates:
        raise Refusal("record_binding_mismatch")
    return {"result": "allowed", "reason": "candidate_not_reserved"}


def _main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict) or payload.get("mode") != "guard":
            raise Refusal("invalid_input")
        result = check_reserved_target(
            payload.get("metadata_status"),
            payload.get("metadata"),
            payload.get("data_status"),
            payload.get("secret"),
            payload.get("declaration"),
            payload.get("candidates"),
        )
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except Refusal as exc:
        print(json.dumps({"result": "refused", "reason": exc.reason}, sort_keys=True, separators=(",", ":")))
        return 2
    except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        print(json.dumps({"result": "refused", "reason": "invalid_input"}, sort_keys=True, separators=(",", ":")))
        return 2


if __name__ == "__main__":
    raise SystemExit(_main())
