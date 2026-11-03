#!/usr/bin/env python3
"""Check fixed OpenBao policy and effective-capability readbacks without echoing them."""

from __future__ import annotations

import json
import re
import sys
from typing import Any

DATA_PATH = "secret/data/coordination/o11y-restore-target"
METADATA_PATH = "secret/metadata/coordination/o11y-restore-target"
EXPECTED_CAPABILITIES = {
    DATA_PATH: frozenset({"create", "read", "update"}),
    METADATA_PATH: frozenset({"read"}),
}
INPUT_KEYS = {
    "reviewed_repository_sha",
    "expected_policy",
    "policy_status",
    "policy_response",
    "capabilities_status",
    "capabilities_response",
}


class Refusal(Exception):
    """A fixed refusal reason; never includes API or caller values."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _response_policy(value: object) -> str:
    if not isinstance(value, dict):
        raise Refusal("policy_response_invalid")
    data = value.get("data")
    if not isinstance(data, dict):
        raise Refusal("policy_response_invalid")
    policy = data.get("policy")
    if not isinstance(policy, str):
        raise Refusal("policy_response_invalid")
    # Some compatible OpenBao responses expose the same text under both documented
    # fields. Never choose between conflicting copies, and never use `rules` alone.
    if "rules" in data and data["rules"] != policy:
        raise Refusal("policy_response_ambiguous")
    if data.get("name", "semaphore-read") != "semaphore-read":
        raise Refusal("policy_response_ambiguous")
    return policy


def _response_capabilities(value: object) -> dict[str, frozenset[str]]:
    if not isinstance(value, dict) or not isinstance(value.get("data"), dict):
        raise Refusal("capabilities_response_invalid")
    data = value["data"]
    if set(data) != set(EXPECTED_CAPABILITIES):
        raise Refusal("capabilities_paths_mismatch")

    parsed: dict[str, frozenset[str]] = {}
    for path in EXPECTED_CAPABILITIES:
        grants = data[path]
        if (
            not isinstance(grants, list)
            or any(not isinstance(grant, str) or not grant for grant in grants)
            or len(grants) != len(set(grants))
        ):
            raise Refusal("capabilities_response_invalid")
        parsed[path] = frozenset(grants)
    return parsed


def verify(payload: object) -> dict[str, Any]:
    """Return a bounded result for one policy read and one two-path capability read."""
    if not isinstance(payload, dict) or set(payload) != INPUT_KEYS:
        raise Refusal("input_shape_invalid")
    revision = payload["reviewed_repository_sha"]
    if not isinstance(revision, str) or re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise Refusal("reviewed_sha_invalid")

    result: dict[str, Any] = {
        "status": "refused",
        "reason": "input_shape_invalid",
        "reviewed_repository_sha": revision,
        "policy_matches_source": False,
        "data_capabilities_match": False,
        "metadata_capabilities_match": False,
    }

    try:
        if type(payload["policy_status"]) is not int or payload["policy_status"] != 200:
            raise Refusal("policy_read_failed")
        if type(payload["capabilities_status"]) is not int or payload["capabilities_status"] != 200:
            raise Refusal("capabilities_read_failed")
        expected_policy = payload["expected_policy"]
        if not isinstance(expected_policy, str):
            raise Refusal("policy_source_invalid")

        stored_policy = _response_policy(payload["policy_response"])
        result["policy_matches_source"] = stored_policy == expected_policy
        if not result["policy_matches_source"]:
            raise Refusal("policy_mismatch")

        capabilities = _response_capabilities(payload["capabilities_response"])
        result["data_capabilities_match"] = capabilities[DATA_PATH] == EXPECTED_CAPABILITIES[DATA_PATH]
        result["metadata_capabilities_match"] = capabilities[METADATA_PATH] == EXPECTED_CAPABILITIES[METADATA_PATH]
        if not result["data_capabilities_match"] or not result["metadata_capabilities_match"]:
            raise Refusal("capabilities_mismatch")
    except Refusal as exc:
        result["reason"] = exc.reason
        return result

    result.update(status="verified", reason="verified")
    return result


def _main() -> int:
    try:
        payload = json.load(sys.stdin)
        result = verify(payload)
    except Refusal as exc:
        revision = payload.get("reviewed_repository_sha") if isinstance(payload, dict) else None
        result = {"status": "refused", "reason": exc.reason}
        if isinstance(revision, str) and re.fullmatch(r"[0-9a-f]{40}", revision):
            result.update(
                reviewed_repository_sha=revision,
                policy_matches_source=False,
                data_capabilities_match=False,
                metadata_capabilities_match=False,
            )
    except (json.JSONDecodeError, ValueError, TypeError):
        result = {"status": "refused", "reason": "input_invalid"}
    except Exception:
        result = {"status": "refused", "reason": "verification_failed"}

    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result.get("status") == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(_main())
