#!/usr/bin/env python3
"""Compare one reviewed private inventory with Semaphore's named production copy."""

from __future__ import annotations

import json
import re
import sys

import yaml

SCHEMA = {
    "schema_version": 1,
    "status": "unavailable",
    "reason": "invalid_input",
    "public_sha": "",
    "private_sha": "",
}
EXPECTED_TEMPLATE = "Survey o11y Profile Pilot Inventory Provenance (Dev)"
EXPECTED_REPOSITORY = "agent-cloud dev"
EXPECTED_REPOSITORY_URL = "https://github.com/uhstray-io/agent-cloud.git"
INTEGRATION_BRANCH = "dev"
MAX_INVENTORY_BYTES = 4 * 1024 * 1024


class Refusal(Exception):
    """A fixed refusal reason that never includes untrusted input."""

    def __init__(self, reason: str):
        self.reason = reason


class UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate keys instead of silently replacing them."""


def _unique_mapping(loader: UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    loader.flatten_mapping(node)
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError:
            raise Refusal("malformed_yaml") from None
        if duplicate:
            raise Refusal("malformed_yaml")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def _positive_id(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _single_named(rows: object, name: str) -> dict:
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise Refusal("semaphore_api_error")
    matches = [row for row in rows if row.get("name") == name]
    if len(matches) != 1:
        raise Refusal("identity_ambiguous")
    return matches[0]


def _parse_inventory(text: object) -> object:
    if not isinstance(text, str) or not text or len(text.encode("utf-8")) > MAX_INVENTORY_BYTES:
        raise Refusal("inventory_source_unknown")
    try:
        value = yaml.load(text, Loader=UniqueKeyLoader)
    except Refusal:
        raise
    except (yaml.YAMLError, UnicodeError, ValueError, TypeError):
        raise Refusal("malformed_yaml") from None
    if not isinstance(value, dict):
        raise Refusal("malformed_yaml")
    return value


def evaluate(payload: object) -> dict:
    result = dict(SCHEMA)
    if not isinstance(payload, dict):
        return result

    public_sha = payload.get("public_sha")
    private_sha = payload.get("private_sha")
    if isinstance(public_sha, str) and re.fullmatch(r"[0-9a-f]{40}", public_sha):
        result["public_sha"] = public_sha
    if isinstance(private_sha, str) and re.fullmatch(r"[0-9a-f]{40}", private_sha):
        result["private_sha"] = private_sha

    try:
        if result["public_sha"] != public_sha or result["private_sha"] != private_sha:
            raise Refusal("invalid_input")
        if payload.get("private_tip_sha") != private_sha:
            raise Refusal("branch_tip_mismatch")
        if not re.fullmatch(
            r"(?:100644|100755) blob [0-9a-f]{40}\tinventory/production\.yml",
            payload.get("private_tree_entry", ""),
        ):
            raise Refusal("inventory_source_unknown")

        project_id = payload.get("project_id")
        repository_id = payload.get("repository_id")
        if not _positive_id(project_id) or not _positive_id(repository_id):
            raise Refusal("invalid_input")

        semaphore_statuses = payload.get("semaphore_statuses")
        if (not isinstance(semaphore_statuses, list) or len(semaphore_statuses) != 3
                or any(type(status) is not int or status != 200 for status in semaphore_statuses)):
            raise Refusal("semaphore_api_error")

        inventory = _single_named(payload.get("inventories"), "production")
        if not _positive_id(inventory.get("id")) or inventory.get("project_id") != project_id:
            raise Refusal("identity_ambiguous")
        if inventory.get("type") != "static-yaml":
            raise Refusal("inventory_source_unknown")

        template = _single_named(payload.get("templates"), EXPECTED_TEMPLATE)
        if (not _positive_id(template.get("id")) or template.get("project_id") != project_id
                or template.get("inventory_id") != inventory["id"]
                or template.get("repository_id") != repository_id
                or template.get("playbook") != "platform/playbooks/survey-o11y-inventory-provenance.yml"):
            raise Refusal("identity_ambiguous")

        repository = _single_named(payload.get("repositories"), EXPECTED_REPOSITORY)
        if (repository.get("id") != repository_id or repository.get("project_id") != project_id
                or repository.get("git_url") != EXPECTED_REPOSITORY_URL
                or repository.get("git_branch") != INTEGRATION_BRANCH):
            raise Refusal("identity_ambiguous")

        stored = inventory.get("inventory")
        private = payload.get("private_inventory")
        _parse_inventory(stored)
        _parse_inventory(private)
        result["status"] = "observed" if stored == private else "mismatch"
        result["reason"] = "exact_bytes_match" if stored == private else "inventory_bytes_differ"
    except Refusal as refusal:
        result["status"] = "unavailable" if refusal.reason not in ("inventory_bytes_differ",) else "mismatch"
        result["reason"] = refusal.reason
    except (KeyError, TypeError, ValueError, UnicodeError):
        result["status"] = "unavailable"
        result["reason"] = "invalid_input"
    return result


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeError):
        payload = None
    print(json.dumps(evaluate(payload), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
