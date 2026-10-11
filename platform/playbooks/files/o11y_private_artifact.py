#!/usr/bin/env python3
"""Private artifact catalog and fail-closed inventory proposal primitives."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path

import yaml
from inspect_o11y_backup_artifact import (
    PBS_VM_VOLID,
    VZDUMP_VOLID,
    _inspect_candidate,
    _storage_classes,
    inspect,
)
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

CATALOG_PATH = "catalogs/o11y/backup-artifacts.json"
INVENTORY_PATH = "inventory/production.yml"
SELECTOR_FIELDS = {"volid", "backend", "format", "creation_time_epoch", "size_bytes"}
CATALOG_FIELDS = {
    "schema_version",
    "observed_at_epoch",
    "agent_cloud_sha",
    "site_config_base_sha",
    "target",
    "storages",
    "rows",
}
SHA = re.compile(r"[0-9a-f]{40}")
HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,252}")
NODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
VOLID = re.compile(r"[A-Za-z0-9._-]{1,64}:.{1,255}")
SAFE_REFUSAL = "Private artifact selection refused because input did not match the reviewed schema."
FRESHNESS_REFUSAL = "The private artifact capture is outside the allowed freshness window. Recapture before proposing."
DRIFT_REFUSAL = "The fresh Proxmox census differs from the private artifact capture. Recapture before proposing."
SCHEMA_REFUSAL = "The private artifact catalog is malformed or unsupported."
INVENTORY_REFUSAL = "The private inventory does not match the reviewed selector insertion contract."


def require(condition: bool, message: str = SAFE_REFUSAL) -> None:
    if not condition:
        raise ValueError(message)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, SCHEMA_REFUSAL)
        result[key] = value
    return result


def _safe_backend(value: object) -> str:
    require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value) is not None)
    return value


def _catalog_from_payload(
    payload: Mapping[str, object], target: Mapping[str, object], agent_sha: str, site_sha: str, now: int
) -> dict[str, object]:
    inspect(payload)
    require(SHA.fullmatch(agent_sha) is not None and SHA.fullmatch(site_sha) is not None)
    require(set(target) == {"host", "node", "vmid"})
    require(isinstance(target["host"], str) and HOST.fullmatch(target["host"]) is not None)
    require(isinstance(target["node"], str) and NODE.fullmatch(target["node"]) is not None)
    require(isinstance(target["vmid"], str) and re.fullmatch(r"[1-9][0-9]{0,8}", target["vmid"]) is not None)
    require(str(payload.get("vmid")) == target["vmid"] and payload.get("node") == target["node"])
    storages = payload["storages"]
    classes = _storage_classes(storages)
    backend_by_id = {item["storage"]: _safe_backend(item["type"]) for item in storages}
    content_by_id = {}
    for item in storages:
        content = item.get("content")
        require(isinstance(content, str) and content != "")
        values = content.split(",")
        require(all(re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", value) for value in values))
        require(len(values) == len(set(values)))
        content_by_id[item["storage"]] = sorted(values)
    row_values = []
    for read in payload["content_reads"]:
        storage = read.get("storage", read.get("item", {}).get("storage"))
        for candidate in read.get("json", {}).get("data", []):
            volid, summary = _inspect_candidate(candidate, int(target["vmid"]), storage, classes)
            protected = summary["proxmox_protected_flag"]
            row_values.append(
                {
                    "volid": volid,
                    "backend": backend_by_id[storage],
                    "format": summary["format"],
                    "creation_time_epoch": summary["creation_time_epoch"],
                    "size_bytes": summary["size_bytes"],
                    "proxmox_protected_flag": protected,
                }
            )
    row_values.sort(key=lambda row: row["volid"])
    require(len(row_values) > 0 and len({row["volid"] for row in row_values}) == len(row_values))
    return {
        "schema_version": 1,
        "observed_at_epoch": now,
        "agent_cloud_sha": agent_sha,
        "site_config_base_sha": site_sha,
        "target": dict(target),
        "storages": [
            {"storage_id": storage, "backend": backend_by_id[storage], "content": content_by_id[storage]}
            for storage in sorted(backend_by_id)
        ],
        "rows": row_values,
    }


def _validate_catalog(value: object) -> dict[str, object]:
    require(isinstance(value, dict) and set(value) == CATALOG_FIELDS, SCHEMA_REFUSAL)
    require(type(value["schema_version"]) is int and value["schema_version"] == 1, SCHEMA_REFUSAL)
    require(type(value["observed_at_epoch"]) is int and value["observed_at_epoch"] > 0, SCHEMA_REFUSAL)
    require(
        all(
            isinstance(value[key], str) and SHA.fullmatch(value[key])
            for key in ("agent_cloud_sha", "site_config_base_sha")
        ),
        SCHEMA_REFUSAL,
    )
    target = value["target"]
    require(isinstance(target, dict) and set(target) == {"host", "node", "vmid"}, SCHEMA_REFUSAL)
    require(isinstance(target["host"], str) and HOST.fullmatch(target["host"]), SCHEMA_REFUSAL)
    require(isinstance(target["node"], str) and NODE.fullmatch(target["node"]), SCHEMA_REFUSAL)
    require(isinstance(target["vmid"], str) and re.fullmatch(r"[1-9][0-9]{0,8}", target["vmid"]), SCHEMA_REFUSAL)
    storages = value["storages"]
    require(isinstance(storages, list) and len(storages) > 0, SCHEMA_REFUSAL)
    ids = []
    for item in storages:
        require(isinstance(item, dict) and set(item) == {"storage_id", "backend", "content"}, SCHEMA_REFUSAL)
        require(
            isinstance(item["storage_id"], str) and re.fullmatch(r"[A-Za-z0-9._-]{1,64}", item["storage_id"]),
            SCHEMA_REFUSAL,
        )
        _safe_backend(item["backend"])
        require(isinstance(item["content"], list) and bool(item["content"]), SCHEMA_REFUSAL)
        require(
            all(isinstance(c, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", c) for c in item["content"]),
            SCHEMA_REFUSAL,
        )
        require(item["content"] == sorted(set(item["content"])), SCHEMA_REFUSAL)
        ids.append(item["storage_id"])
    require(ids == sorted(set(ids)), SCHEMA_REFUSAL)
    rows = value["rows"]
    require(isinstance(rows, list) and rows, SCHEMA_REFUSAL)
    volids = []
    storage_map = {item["storage_id"]: item for item in storages}
    for row in rows:
        require(
            isinstance(row, dict)
            and set(row)
            == {"volid", "backend", "format", "creation_time_epoch", "size_bytes", "proxmox_protected_flag"},
            SCHEMA_REFUSAL,
        )
        require(isinstance(row["volid"], str) and VOLID.fullmatch(row["volid"]), SCHEMA_REFUSAL)
        storage = row["volid"].split(":", 1)[0]
        require(storage in storage_map and row["backend"] == storage_map[storage]["backend"], SCHEMA_REFUSAL)
        require(isinstance(row["backend"], str), SCHEMA_REFUSAL)
        require(
            isinstance(row["format"], str) and row["format"] in {"vma", "vma.zst", "vma.gz", "vma.lzo", "pbs-vm"},
            SCHEMA_REFUSAL,
        )
        if row["backend"].strip().lower() == "pbs":
            match = PBS_VM_VOLID.fullmatch(row["volid"])
            require(
                match is not None
                and match.group("storage") == storage
                and match.group("vmid") == target["vmid"]
                and row["format"] == "pbs-vm",
                SCHEMA_REFUSAL,
            )
        else:
            match = VZDUMP_VOLID.fullmatch(row["volid"])
            require(
                match is not None
                and match.group("storage") == storage
                and match.group("vmid") == target["vmid"]
                and match.group("format") == row["format"],
                SCHEMA_REFUSAL,
            )
        require(type(row["creation_time_epoch"]) is int and row["creation_time_epoch"] > 0, SCHEMA_REFUSAL)
        require(type(row["size_bytes"]) is int and row["size_bytes"] > 0, SCHEMA_REFUSAL)
        require(
            isinstance(row["proxmox_protected_flag"], str)
            and row["proxmox_protected_flag"] in {"protected", "not-protected", "unknown"},
            SCHEMA_REFUSAL,
        )
        volids.append(row["volid"])
    require(volids == sorted(set(volids)), SCHEMA_REFUSAL)
    return value


def _strict_yaml(source: str):
    try:
        require(
            not any(type(token).__name__ in {"AnchorToken", "AliasToken", "TagToken"} for token in yaml.scan(source)),
            INVENTORY_REFUSAL,
        )
        root = yaml.compose(source, Loader=yaml.SafeLoader)
    except yaml.YAMLError:
        raise ValueError(INVENTORY_REFUSAL) from None
    require(isinstance(root, MappingNode) and root.flow_style is False, INVENTORY_REFUSAL)
    seen_nodes = set()

    def visit(node):
        require(id(node) not in seen_nodes, INVENTORY_REFUSAL)
        seen_nodes.add(id(node))
        require(
            node.tag
            in {
                "tag:yaml.org,2002:map",
                "tag:yaml.org,2002:seq",
                "tag:yaml.org,2002:str",
                "tag:yaml.org,2002:int",
                "tag:yaml.org,2002:bool",
                "tag:yaml.org,2002:null",
            },
            INVENTORY_REFUSAL,
        )
        if isinstance(node, MappingNode):
            require(node.flow_style is False, INVENTORY_REFUSAL)
            keys = []
            for key, val in node.value:
                require(
                    isinstance(key, ScalarNode) and key.tag == "tag:yaml.org,2002:str" and key.style is None,
                    INVENTORY_REFUSAL,
                )
                require(key.value != "<<" and key.value not in keys, INVENTORY_REFUSAL)
                keys.append(key.value)
                visit(key)
                visit(val)
        elif isinstance(node, SequenceNode):
            require(node.flow_style is False, INVENTORY_REFUSAL)
            for item in node.value:
                visit(item)

    visit(root)
    try:
        value = yaml.safe_load(source)
    except yaml.YAMLError:
        raise ValueError(INVENTORY_REFUSAL) from None
    return root, value


def _node_map(node: MappingNode) -> dict[str, object]:
    return {key.value: val for key, val in node.value}


def _insert_selector(source: str, catalog: Mapping[str, object], row_number: int) -> str:
    catalog = _validate_catalog(dict(catalog))
    require(type(row_number) is int and 1 <= row_number <= len(catalog["rows"]), SCHEMA_REFUSAL)
    root_node, document = _strict_yaml(source)
    require(isinstance(document, dict), INVENTORY_REFUSAL)
    path = ["agent_cloud", "children", "o11y_svc", "hosts"]
    node = root_node
    current = document
    for key in path:
        require(isinstance(current, dict) and key in current, INVENTORY_REFUSAL)
        node = _node_map(node).get(key)
        current = current[key]
        require(isinstance(node, MappingNode) and isinstance(current, dict), INVENTORY_REFUSAL)
    require(len(current) == 1 and next(iter(current)) == catalog["target"]["host"], INVENTORY_REFUSAL)
    host_name = next(iter(current))
    host_node = _node_map(node)[host_name]
    host_doc = current[host_name]
    require(isinstance(host_node, MappingNode) and isinstance(host_doc, dict), INVENTORY_REFUSAL)
    require(
        host_doc.get("vm_node") == catalog["target"]["node"]
        and str(host_doc.get("vm_vmid")) == catalog["target"]["vmid"],
        INVENTORY_REFUSAL,
    )
    selector_count = 0

    def find_selector(value):
        nonlocal selector_count
        if isinstance(value, dict):
            selector_count += int("o11y_backup_artifact_selector" in value)
            for child in value.values():
                find_selector(child)
        elif isinstance(value, list):
            for child in value:
                find_selector(child)

    find_selector(document)
    require(selector_count == 0, INVENTORY_REFUSAL)
    row = catalog["rows"][row_number - 1]
    selector = {
        "volid": row["volid"],
        "backend": "pbs" if row["format"] == "pbs-vm" else "non-pbs",
        "format": row["format"],
        "creation_time_epoch": row["creation_time_epoch"],
        "size_bytes": row["size_bytes"],
    }
    lines = source.splitlines(keepends=True)
    end_line = host_node.end_mark.line
    require(end_line <= len(lines) and bool(lines), INVENTORY_REFUSAL)
    line = lines[min(end_line, len(lines) - 1)]
    indent = len(line) - len(line.lstrip(" "))
    insertion = min(end_line + 1, len(lines))
    block = " " * indent + "o11y_backup_artifact_selector:\n"
    for key in ("volid", "backend", "format", "creation_time_epoch", "size_bytes"):
        block += (
            " " * (indent + 2)
            + f"{key}: "
            + (json.dumps(selector[key]) if isinstance(selector[key], str) else str(selector[key]))
            + "\n"
        )
    lines.insert(insertion, block)
    result = "".join(lines)
    _, after = _strict_yaml(result)
    expected = dict(document)
    expected_children = dict(expected["agent_cloud"]["children"])
    expected_o11y = dict(expected_children["o11y_svc"])
    expected_hosts = dict(expected_o11y["hosts"])
    expected_host = dict(expected_hosts[host_name])
    expected_host["o11y_backup_artifact_selector"] = selector
    expected_hosts[host_name] = expected_host
    expected_o11y["hosts"] = expected_hosts
    expected_children["o11y_svc"] = expected_o11y
    expected["agent_cloud"] = {**expected["agent_cloud"], "children": expected_children}
    require(after == expected, INVENTORY_REFUSAL)
    return result


def _census_rows(
    payload: Mapping[str, object], target: Mapping[str, object]
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    inspect(payload)
    classes = _storage_classes(payload["storages"])
    backend_by_id = {item["storage"]: _safe_backend(item["type"]) for item in payload["storages"]}
    storage_records = []
    for item in payload["storages"]:
        content = item["content"].split(",")
        storage_records.append(
            {"storage_id": item["storage"], "backend": backend_by_id[item["storage"]], "content": sorted(content)}
        )
    rows = []
    for read in payload["content_reads"]:
        storage = read.get("storage", read.get("item", {}).get("storage"))
        for candidate in read.get("json", {}).get("data", []):
            volid, summary = _inspect_candidate(candidate, int(target["vmid"]), storage, classes)
            rows.append(
                {
                    "volid": volid,
                    "backend": backend_by_id[storage],
                    "format": summary["format"],
                    "creation_time_epoch": summary["creation_time_epoch"],
                    "size_bytes": summary["size_bytes"],
                    "proxmox_protected_flag": summary["proxmox_protected_flag"],
                }
            )
    return sorted(storage_records, key=lambda item: item["storage_id"]), sorted(rows, key=lambda item: item["volid"])


def propose(
    catalog: object,
    payload: Mapping[str, object],
    capture_sha: str,
    row_number: int,
    now: int,
    inventory_host: str,
) -> dict[str, object]:
    catalog = _validate_catalog(catalog)
    require(
        SHA.fullmatch(capture_sha) is not None and type(row_number) is int and 1 <= row_number <= len(catalog["rows"]),
        SCHEMA_REFUSAL,
    )
    age = now - catalog["observed_at_epoch"]
    require(0 <= age <= 1800, FRESHNESS_REFUSAL)
    target = catalog["target"]
    require(inventory_host == target["host"], DRIFT_REFUSAL)
    require(str(payload.get("vmid")) == target["vmid"] and payload.get("node") == target["node"], DRIFT_REFUSAL)
    storages, rows = _census_rows(payload, target)
    require(storages == catalog["storages"] and rows == catalog["rows"], DRIFT_REFUSAL)
    selected = catalog["rows"][row_number - 1]
    return {
        "selector": {
            "volid": selected["volid"],
            "backend": "pbs" if selected["format"] == "pbs-vm" else "non-pbs",
            "format": selected["format"],
            "creation_time_epoch": selected["creation_time_epoch"],
            "size_bytes": selected["size_bytes"],
        },
        "row_number": row_number,
        "age_seconds": age,
    }


def _git(repo: str, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=False)
    require(result.returncode == 0, SAFE_REFUSAL)
    return result.stdout.strip()


def _catalog_at(repo: str, commit: str) -> dict[str, object]:
    raw = subprocess.run(["git", "show", f"{commit}:{CATALOG_PATH}"], cwd=repo, capture_output=True, check=False)
    require(raw.returncode == 0, SCHEMA_REFUSAL)
    try:
        return _validate_catalog(json.loads(raw.stdout, object_pairs_hook=_unique_object))
    except (ValueError, json.JSONDecodeError):
        raise ValueError(SCHEMA_REFUSAL) from None


def _require_regular_git_path(repo: str, commit: str, path: str) -> None:
    current = ""
    components = path.split("/")
    for index, component in enumerate(components):
        current = f"{current}/{component}" if current else component
        result = subprocess.run(
            ["git", "ls-tree", commit, current], cwd=repo, text=True, capture_output=True, check=False
        )
        require(result.returncode == 0, SCHEMA_REFUSAL)
        entry = result.stdout.split()
        require(len(entry) >= 4, SCHEMA_REFUSAL)
        if index < len(components) - 1:
            require(entry[0] == "040000" and entry[1] == "tree", SCHEMA_REFUSAL)
        else:
            require(entry[0] in {"100644", "100600"} and entry[1] == "blob", SCHEMA_REFUSAL)


def verify_capture(repo: str, capture_sha: str, base_sha: str, agent_sha: str) -> dict[str, object]:
    require(
        SHA.fullmatch(capture_sha) is not None
        and SHA.fullmatch(base_sha) is not None
        and SHA.fullmatch(agent_sha) is not None
    )
    parents = _git(repo, "rev-list", "--parents", "-n", "1", capture_sha).split()
    changed = _git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", capture_sha).splitlines()
    _require_regular_git_path(repo, capture_sha, CATALOG_PATH)
    catalog = _catalog_at(repo, capture_sha)
    require(len(parents) == 2 and parents[1] == base_sha and changed == [CATALOG_PATH], SCHEMA_REFUSAL)
    require(catalog["site_config_base_sha"] == base_sha and catalog["agent_cloud_sha"] == agent_sha, SCHEMA_REFUSAL)
    return catalog


def verify_proposal(
    repo: str, branch: str, base_sha: str, capture_sha: str, agent_sha: str, row_number: int, expected_inventory: str
) -> dict[str, object]:
    expected_branch = f"proposal/o11y-artifact-{capture_sha}-row-{row_number}"
    require(
        branch == expected_branch
        and SHA.fullmatch(base_sha) is not None
        and SHA.fullmatch(capture_sha) is not None
        and SHA.fullmatch(agent_sha) is not None,
        SCHEMA_REFUSAL,
    )
    head = _git(repo, "rev-parse", "HEAD")
    _require_regular_git_path(repo, head, INVENTORY_PATH)
    parents = _git(repo, "rev-list", "--parents", "-n", "1", head).split()
    changed = _git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", head).splitlines()
    body = _git(repo, "show", "-s", "--format=%B", head)
    require(
        _git(repo, "branch", "--show-current") == branch
        and len(parents) == 2
        and parents[1] == base_sha
        and changed == [INVENTORY_PATH],
        SCHEMA_REFUSAL,
    )
    for token in (
        f"capture={capture_sha}",
        f"row={row_number}",
        f"site-config-base={base_sha}",
        f"agent-cloud={agent_sha}",
    ):
        require(token in body, SCHEMA_REFUSAL)
    actual = subprocess.run(["git", "show", f"{head}:{INVENTORY_PATH}"], cwd=repo, capture_output=True, check=False)
    require(actual.returncode == 0 and actual.stdout.decode("utf-8") == expected_inventory, SCHEMA_REFUSAL)
    remote = _git(repo, "ls-remote", "--heads", "origin", f"refs/heads/{branch}").split()
    require(len(remote) == 2 and remote[0] == head, SCHEMA_REFUSAL)
    return {"branch": branch, "commit": head, "existing_proposal": True}


def catalog_path(root: str) -> dict[str, object]:
    base = Path(root)
    require(base.is_dir() and not base.is_symlink())
    current = base
    parts = Path(CATALOG_PATH).parts
    for part in parts[:-1]:
        current = current / part
        require(not current.is_symlink())
        if not current.exists():
            current.mkdir(mode=0o700)
        require(current.is_dir())
    target = current / parts[-1]
    require(not target.is_symlink())
    if target.exists():
        require(target.is_file())
        try:
            _validate_catalog(json.loads(target.read_text(), object_pairs_hook=_unique_object))
        except (ValueError, OSError, json.JSONDecodeError):
            raise ValueError(SCHEMA_REFUSAL) from None
    return {"path": str(target)}


def main() -> int:
    try:
        request = json.load(sys.stdin, object_pairs_hook=_unique_object)
        operation = sys.argv[1]
        if operation == "capture":
            result = _catalog_from_payload(
                request["census"],
                request["target"],
                request["agent_cloud_sha"],
                request["site_config_base_sha"],
                int(time.time()),
            )
            output = {
                "catalog": result,
                "rows": [
                    {
                        "row": index,
                        "backend": "pbs" if row["format"] == "pbs-vm" else "non-pbs",
                        "format": row["format"],
                        "creation_time_epoch": row["creation_time_epoch"],
                        "size_bytes": row["size_bytes"],
                        "proxmox_protected_flag": row["proxmox_protected_flag"],
                    }
                    for index, row in enumerate(result["rows"], 1)
                ],
            }
        elif operation == "propose":
            output = propose(
                request["catalog"],
                request["census"],
                request["capture_sha"],
                request["row_number"],
                int(time.time()),
                request["inventory_host"],
            )
        elif operation == "freshness":
            catalog = _validate_catalog(request["catalog"])
            age = int(time.time()) - catalog["observed_at_epoch"]
            require(0 <= age <= 1800, FRESHNESS_REFUSAL)
            output = {"age_seconds": age}
        elif operation == "inventory":
            output = {"inventory": _insert_selector(request["inventory"], request["catalog"], request["row_number"])}
        elif operation == "catalog-path":
            output = catalog_path(request["root"])
        elif operation == "verify-capture":
            output = {
                "catalog": verify_capture(
                    request["repo"], request["capture_sha"], request["base_sha"], request["agent_sha"]
                )
            }
        elif operation == "verify-proposal":
            output = verify_proposal(
                request["repo"],
                request["branch"],
                request["base_sha"],
                request["capture_sha"],
                request["agent_sha"],
                request["row_number"],
                request["expected_inventory"],
            )
        else:
            raise ValueError(SAFE_REFUSAL)
        print(json.dumps(output, separators=(",", ":")))
        return 0
    except Exception:
        print(json.dumps({"refusal": SAFE_REFUSAL}, separators=(",", ":")))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
