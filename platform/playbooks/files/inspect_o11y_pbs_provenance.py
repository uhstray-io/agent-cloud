#!/usr/bin/env python3
"""Compare runtime inventory bytes with one reviewed site-config inventory."""

from __future__ import annotations

import hashlib
import json
import re
import sys

import yaml

MAX_BYTES = 4 * 1024 * 1024
REVISION = re.compile(r"[0-9a-f]{40}\Z")
TREE_ENTRY = re.compile(r"(?:100644|100755) blob [0-9a-f]{40}\tinventory/production\.yml\Z")
REASONS = {
    "invalid_input",
    "branch_tip_mismatch",
    "inventory_source_unknown",
    "inventory_bytes_differ",
    "malformed_yaml",
    "identity_ambiguous",
    "semaphore_api_error",
}
EXPECTED_REPOSITORY = "agent-cloud dev"
EXPECTED_REPOSITORY_URL = "https://github.com/uhstray-io/agent-cloud.git"
EXPECTED_BRANCH = "dev"


class Refusal(Exception):
    """A fixed refusal category that never includes private input."""

    def __init__(self, reason: str):
        self.reason = reason if reason in REASONS else "invalid_input"


class UniqueKeyLoader(yaml.SafeLoader):
    """Strict safe YAML loader that rejects duplicate keys."""


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
    return type(value) is int and value > 0


def _parse_yaml(text: object) -> object:
    if not isinstance(text, str) or not text or len(text.encode("utf-8")) > MAX_BYTES:
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


def _single_named(rows: object, name: str) -> dict:
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise Refusal("semaphore_api_error")
    matches = [row for row in rows if row.get("name") == name]
    if len(matches) != 1:
        raise Refusal("identity_ambiguous")
    return matches[0]


def evaluate(payload: object) -> dict[str, object]:
    """Return hashes and fixed categories only; never echo private inventory bytes."""
    result: dict[str, object] = {
        "schema_version": 1,
        "status": "unavailable",
        "reason": "invalid_input",
        "public_sha": "",
        "private_sha": "",
        "inventory_provenance": "unverified",
        "inventory_fingerprint": None,
        "source_fingerprint": None,
    }
    if not isinstance(payload, dict):
        return result
    for field in ("public_sha", "private_sha"):
        value = payload.get(field)
        if isinstance(value, str) and REVISION.fullmatch(value):
            result[field] = value
    try:
        public_sha, private_sha = payload.get("public_sha"), payload.get("private_sha")
        if result["public_sha"] != public_sha or result["private_sha"] != private_sha:
            raise Refusal("invalid_input")
        if payload.get("private_tip_sha") != private_sha:
            raise Refusal("branch_tip_mismatch")
        if not TREE_ENTRY.fullmatch(payload.get("private_tree_entry", "")):
            raise Refusal("inventory_source_unknown")
        project_id, repository_id = payload.get("project_id"), payload.get("repository_id")
        if not _positive_id(project_id) or not _positive_id(repository_id):
            raise Refusal("invalid_input")
        statuses = payload.get("semaphore_statuses")
        if (not isinstance(statuses, list) or len(statuses) != 3
                or any(type(status) is not int or status != 200 for status in statuses)):
            raise Refusal("semaphore_api_error")
        inventory = _single_named(payload.get("inventories"), "production")
        if (not _positive_id(inventory.get("id")) or inventory.get("project_id") != project_id
                or inventory.get("type") != "static-yaml"):
            raise Refusal("identity_ambiguous")
        template_name = payload.get("template_name")
        playbook = payload.get("playbook")
        if not isinstance(template_name, str) or not isinstance(playbook, str):
            raise Refusal("invalid_input")
        template = _single_named(payload.get("templates"), template_name)
        if (not _positive_id(template.get("id")) or template.get("project_id") != project_id
                or template.get("inventory_id") != inventory["id"]
                or template.get("repository_id") != repository_id
                or template.get("playbook") != playbook):
            raise Refusal("identity_ambiguous")
        repository = _single_named(payload.get("repositories"), EXPECTED_REPOSITORY)
        if (repository.get("id") != repository_id or repository.get("project_id") != project_id
                or repository.get("git_url") != EXPECTED_REPOSITORY_URL
                or repository.get("git_branch") != EXPECTED_BRANCH):
            raise Refusal("identity_ambiguous")
        private_inventory = payload.get("private_inventory")
        runtime_inventory = payload.get("runtime_inventory")
        configured_inventory = inventory.get("inventory")
        _parse_yaml(private_inventory)
        _parse_yaml(runtime_inventory)
        _parse_yaml(configured_inventory)
        if not (private_inventory == runtime_inventory == configured_inventory):
            raise Refusal("inventory_bytes_differ")
        private_bytes = private_inventory.encode("utf-8")
        result.update(
            status="observed",
            reason="exact_bytes_match",
            inventory_provenance="exact_bytes_match",
            inventory_fingerprint=hashlib.sha256(private_bytes).hexdigest(),
            source_fingerprint=hashlib.sha256(
                json.dumps(
                    {"public_sha": public_sha, "private_sha": private_sha,
                    "inventory_fingerprint": hashlib.sha256(private_bytes).hexdigest()},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
        )
    except Refusal as refusal:
        result["reason"] = refusal.reason
        result["status"] = "mismatch" if refusal.reason == "inventory_bytes_differ" else "unavailable"
    except (KeyError, TypeError, ValueError, UnicodeError):
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
