#!/usr/bin/env python3
"""Validate the unique live Semaphore template binding for profile acceptance."""

from __future__ import annotations

import json
import sys

SCHEMA = {"schema_version": 1, "status": "unavailable", "reason": "invalid_input"}
EXPECTED_TEMPLATE_NAME = "Accept o11y Profile Pilot (Dev)"
EXPECTED_PLAYBOOK = "platform/playbooks/accept-o11y-profile-pilot.yml"
EXPECTED_REPOSITORY_NAME = "agent-cloud dev"
EXPECTED_REPOSITORY_URL = "https://github.com/uhstray-io/agent-cloud.git"
EXPECTED_BRANCH = "dev"
EXPECTED_INVENTORY_NAME = "production"
MAX_METADATA_BYTES = 8 * 1024 * 1024


def _positive_id(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _rows(value: object) -> list[dict] | None:
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        return None
    return value


def evaluate(payload: object) -> dict:
    result = dict(SCHEMA)
    if not isinstance(payload, dict) or set(payload) != {
        "project_id", "repository_id", "inventories", "templates", "repositories",
    }:
        return result

    project_id, repository_id = payload.get("project_id"), payload.get("repository_id")
    inventories = _rows(payload.get("inventories"))
    templates = _rows(payload.get("templates"))
    repositories = _rows(payload.get("repositories"))
    if (not all(_positive_id(value) for value in (project_id, repository_id))
            or inventories is None or templates is None or repositories is None):
        return result

    production = [row for row in inventories if row.get("name") == EXPECTED_INVENTORY_NAME]
    acceptance = [row for row in templates if row.get("playbook") == EXPECTED_PLAYBOOK]
    repository = [row for row in repositories if row.get("name") == EXPECTED_REPOSITORY_NAME]
    if len(production) != 1 or len(acceptance) != 1 or len(repository) != 1:
        result["reason"] = "metadata_ambiguous"
        return result

    inventory = production[0]
    template = acceptance[0]
    repo = repository[0]
    if (not _positive_id(inventory.get("id")) or not _positive_id(inventory.get("project_id"))
            or inventory.get("project_id") != project_id
            or inventory.get("type") != "static-yaml"):
        result["reason"] = "inventory_binding_mismatch"
        return result
    if (not _positive_id(template.get("id")) or template.get("name") != EXPECTED_TEMPLATE_NAME
            or not _positive_id(template.get("project_id"))
            or not _positive_id(template.get("repository_id"))
            or not _positive_id(template.get("inventory_id"))
            or template.get("project_id") != project_id
            or template.get("repository_id") != repository_id
            or template.get("inventory_id") != inventory["id"]):
        result["reason"] = "acceptance_template_mismatch"
        return result
    if (not _positive_id(repo.get("id")) or not _positive_id(repo.get("project_id"))
            or repo.get("id") != repository_id or repo.get("project_id") != project_id
            or repo.get("git_url") != EXPECTED_REPOSITORY_URL
            or repo.get("git_branch") != EXPECTED_BRANCH):
        result["reason"] = "repository_binding_mismatch"
        return result

    result.update(status="verified", reason="unique_acceptance_template_bound_to_production_inventory")
    return result


def main() -> int:
    raw = sys.stdin.buffer.read(MAX_METADATA_BYTES + 1)
    if len(raw) > MAX_METADATA_BYTES:
        result = dict(SCHEMA)
        result["reason"] = "metadata_too_large"
    else:
        try:
            result = evaluate(json.loads(raw))
        except (json.JSONDecodeError, UnicodeError):
            result = dict(SCHEMA)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
