#!/usr/bin/env python3
"""Read Request A metadata only; obtain authorization separately before live use.

Uses existing SEMAPHORE_URL, SEMAPHORE_TOKEN and SEMAPHORE_PROJECT_ID environment
values. Never loads credentials, launches tasks, follows redirects or accesses VMs.
Like the adjacent Semaphore configuration tools, this is operator-side tooling.
"""

import json
import os
import sys
from datetime import UTC, datetime
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

import yaml

LIMIT = 2 * 1024 * 1024
ENDPOINTS = ("templates", "repositories", "inventory", "keys")
TEMPLATE = "Check Discovery Pipeline"


class Refusal(Exception):
    """Only fixed, non-secret reason codes belong in this exception."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Refusal("redirect_refused")


def record_id(value):
    if type(value) is not int or not 0 < value < 2**31:
        raise Refusal("invalid_record_id")
    return value


def unique(rows, field, value):
    matches = [row for row in rows if row.get(field) == value]
    if len(matches) != 1:
        raise Refusal("missing_or_ambiguous_record")
    return matches[0]


def group_present(inventory):
    if inventory.get("type") != "static-yaml":
        raise Refusal("unsupported_inventory_type")
    data = yaml.safe_load(inventory.get("inventory", ""))
    pending = [data]
    seen = set()
    # Inspect only inventory groups/children, never vars or plugin execution.
    while pending:
        groups = pending.pop()
        if not isinstance(groups, dict):
            raise Refusal("invalid_inventory_shape")
        if id(groups) in seen:
            continue
        seen.add(id(groups))
        if len(seen) > 1000:
            raise Refusal("inventory_too_complex")
        if "netbox_svc" in groups and isinstance(groups["netbox_svc"], dict):
            return True
        for group in groups.values():
            if isinstance(group, dict) and "children" in group:
                pending.append(group["children"])
    return False


def inspect(env):
    url = env.get("SEMAPHORE_URL", "").rstrip("/")
    token = env.get("SEMAPHORE_TOKEN", "")
    project = env.get("SEMAPHORE_PROJECT_ID", "")
    if not url or not token or not project:
        raise Refusal("missing_existing_auth_or_project")
    parts = urlsplit(url)
    if (parts.scheme != "https" or not parts.hostname or parts.username is not None
            or parts.password is not None or parts.query or parts.fragment or parts.path):
        raise Refusal("https_origin_required")
    if not project.isascii() or not project.isdecimal() or len(project) > 10:
        raise Refusal("invalid_project_id")
    project_id = record_id(int(project))
    if len(token) > 8192 or any(ord(c) < 33 or ord(c) > 126 for c in token):
        raise Refusal("invalid_auth_format")
    # Do not forward bearer authentication through ambient workstation proxies.
    client = build_opener(ProxyHandler({}), NoRedirect())

    def read(endpoint):
        if endpoint not in ENDPOINTS:
            raise Refusal("endpoint_refused")
        request = Request(f"{url}/api/project/{project_id}/{endpoint}",
                          headers={"Authorization": f"Bearer {token}"}, method="GET")
        with client.open(request, timeout=10) as response:
            if response.status != 200:
                raise Refusal("http_read_failed")
            body = response.read(LIMIT + 1)
        if len(body) > LIMIT:
            raise Refusal("response_too_large")
        rows = json.loads(body)
        if not isinstance(rows, list) or len(rows) > 10000 or any(not isinstance(r, dict) for r in rows):
            raise Refusal("invalid_record_list")
        return rows

    template = unique(read("templates"), "name", TEMPLATE)
    template_id = record_id(template.get("id"))
    repo_id = record_id(template.get("repository_id"))
    inventory_id = record_id(template.get("inventory_id"))
    environment_id = record_id(template.get("environment_id"))
    repository = unique(read("repositories"), "id", repo_id)
    inventory = unique(read("inventory"), "id", inventory_id)
    keys = read("keys")
    key_refs = {}
    for label, record, field in (
        ("repository", repository, "ssh_key_id"),
        ("inventory", inventory, "ssh_key_id"),
        ("become", inventory, "become_key_id"),
    ):
        key_id = record.get(field)
        if key_id is None or type(key_id) is int and key_id == 0:
            key_refs[label] = {"id": None, "type": "unbound"}
        else:
            key_id = record_id(key_id)
            key = unique(keys, "id", key_id)
            kind = key.get("type")
            key_refs[label] = {"id": key_id, "type": kind if kind in ("none", "ssh", "login_password") else "unknown"}
    # Never echo free-form labels, URLs, inventory, key payloads or server errors.
    branch = repository.get("git_branch")
    return {
        "observed_at": datetime.now(UTC).isoformat(),
        "project_id": project_id,
        "template_id": template_id,
        "checker_playbook_matches": template.get("playbook") == "platform/playbooks/check-discovery.yml",
        "repository_id": repo_id,
        "repository_branch": branch if branch in ("main", "dev") else "other",
        "inventory_id": inventory_id,
        "environment_id": environment_id,
        "netbox_group_present": group_present(inventory),
        "key_references": key_refs,
        "key_provenance": "unverified",
        "key_validity": "unverified",
        "executed_revision": "unverified",
    }


def main():
    try:
        if len(sys.argv) != 1:
            raise Refusal("arguments_not_supported")
        result = inspect(os.environ)
    except Refusal as error:
        print(json.dumps({"status": "blocked", "reason": str(error)}))
        return 1
    except Exception:
        # Parser/HTTP exception strings may contain response bodies or credentials.
        print(json.dumps({"status": "blocked", "reason": "metadata_read_failed"}))
        return 1
    print(json.dumps({"status": "metadata_only", **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
