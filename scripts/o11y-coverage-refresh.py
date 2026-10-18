#!/usr/bin/env python3
"""Launch exact Dev-bound observability receipts and census them without storing task IDs."""

import argparse
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PROJECT_ID = 1
INVENTORY_ID = 2
RECEIPT_TEMPLATE = "Verify o11y Target Receipt"
RECEIPT_PLAYBOOK = "platform/playbooks/verify-o11y-service.yml"
CENSUS_TEMPLATE = "Verify o11y Coverage Census"
CENSUS_PLAYBOOK = "platform/playbooks/census-o11y-coverage.yml"
REPOSITORY_NAME = "agent-cloud dev"
REPOSITORY_URL = "https://github.com/uhstray-io/agent-cloud.git"
MAX_COHORT = 64

_launcher_spec = importlib.util.spec_from_file_location(
    "semaphore_launcher", ROOT / "scripts/semaphore-launch.py"
)
_launcher = importlib.util.module_from_spec(_launcher_spec)
_launcher_spec.loader.exec_module(_launcher)

_census_spec = importlib.util.spec_from_file_location(
    "o11y_coverage_census", ROOT / "platform/playbooks/files/o11y-coverage-census.py"
)
_census = importlib.util.module_from_spec(_census_spec)
_census_spec.loader.exec_module(_census)


class Refusal(Exception):
    """A safe refusal that does not include inventory contents or credentials."""


def _git(*args):
    try:
        return subprocess.run(
            ["git", "-C", str(ROOT), *args], text=True, capture_output=True, check=True,
        ).stdout.strip()
    except subprocess.CalledProcessError:
        raise Refusal("local_dev_checkout_unavailable") from None


def _positive_id(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def parse_target_signals(values):
    selected = []
    seen = set()
    for value in values:
        target_id, separator, signal = value.rpartition(":")
        pair = (target_id, signal)
        if (not separator or not _census.TARGET_ID.fullmatch(target_id)
                or signal not in _census.SIGNALS | _census.OPTIONAL_SIGNALS or pair in seen):
            raise Refusal("target_signal_selection_invalid")
        seen.add(pair)
        selected.append(pair)
    if not selected or len(selected) > MAX_COHORT:
        raise Refusal("target_signal_cohort_limit")
    return selected


def _named(items, name):
    if not isinstance(items, list):
        raise Refusal("semaphore_binding_mismatch")
    matches = [item for item in items if isinstance(item, dict) and item.get("name") == name]
    if len(matches) != 1 or not _positive_id(matches[0].get("id")):
        raise Refusal("semaphore_binding_mismatch")
    return matches[0]


def _expected_template(api, name, playbook, repository_id):
    listed = _named(api("/templates"), name)
    template = api(f"/templates/{listed['id']}")
    if (not isinstance(template, dict) or template.get("id") != listed["id"]
            or template.get("project_id") != PROJECT_ID
            or template.get("repository_id") != repository_id
            or template.get("inventory_id") != INVENTORY_ID
            or template.get("name") != name or template.get("playbook") != playbook
            or template.get("app") != "ansible" or template.get("arguments") not in (None, "")
            or template.get("allow_override_args_in_task", False) is not False
            or template.get("git_branch") not in (None, "")):
        raise Refusal("semaphore_binding_mismatch")
    return template


def reviewed_declarations(api, expected_dev_sha):
    if (not re.fullmatch(r"[0-9a-f]{40}", expected_dev_sha)
            or _git("branch", "--show-current") != "dev"
            or _git("rev-parse", "HEAD") != expected_dev_sha
            or _git("status", "--porcelain", "--untracked-files=all")):
        raise Refusal("local_dev_revision_mismatch")
    templates = api("/templates")
    receipt_listed = _named(templates, RECEIPT_TEMPLATE)
    census_listed = _named(templates, CENSUS_TEMPLATE)
    receipt = api(f"/templates/{receipt_listed['id']}")
    census = api(f"/templates/{census_listed['id']}")
    if (not isinstance(receipt, dict) or not isinstance(census, dict)
            or receipt.get("id") != 315 or receipt.get("name") != RECEIPT_TEMPLATE
            or receipt.get("project_id") != PROJECT_ID or receipt.get("inventory_id") != INVENTORY_ID
            or receipt.get("playbook") != RECEIPT_PLAYBOOK or receipt.get("app") != "ansible"
            or receipt.get("arguments") not in (None, "")
            or receipt.get("allow_override_args_in_task", False) is not False
            or receipt.get("git_branch") not in (None, "")
            or not _positive_id(receipt.get("repository_id"))):
        raise Refusal("semaphore_binding_mismatch")
    repository_id = receipt["repository_id"]
    repository = _named(api("/repositories"), REPOSITORY_NAME)
    repository = api(f"/repositories/{repository['id']}")
    if (not isinstance(repository, dict) or repository.get("id") != repository_id
            or repository.get("project_id") != PROJECT_ID or repository.get("name") != REPOSITORY_NAME
            or repository.get("git_url") != REPOSITORY_URL or repository.get("git_branch") != "dev"):
        raise Refusal("semaphore_binding_mismatch")
    if census.get("id") != census_listed["id"] or census.get("project_id") != PROJECT_ID:
        raise Refusal("semaphore_binding_mismatch")
    census = _expected_template(api, CENSUS_TEMPLATE, CENSUS_PLAYBOOK, repository_id)
    if _expected_template(api, RECEIPT_TEMPLATE, RECEIPT_PLAYBOOK, repository_id).get("id") != 315:
        raise Refusal("semaphore_binding_mismatch")
    inventory = api(f"/inventory/{INVENTORY_ID}")
    if (not isinstance(inventory, dict) or inventory.get("id") != INVENTORY_ID
            or inventory.get("project_id") != PROJECT_ID or inventory.get("type") != "static-yaml"
            or not isinstance(inventory.get("inventory"), str)):
        raise Refusal("semaphore_binding_mismatch")
    try:
        document = yaml.safe_load(inventory["inventory"])
        declarations = document["all"]["vars"]["estate_coverage_inventory"]
        targets = declarations["targets"]
        revision = declarations["revision"]
        _census.validate(targets, declarations.get("receipt_bindings", {}))
    except (KeyError, TypeError, yaml.YAMLError, SystemExit):
        raise Refusal("private_coverage_declaration_invalid") from None
    if (not isinstance(revision, str) or revision != _census.canonical_inventory_revision(targets)):
        raise Refusal("private_coverage_digest_mismatch")
    return receipt, census, declarations, revision


def launch_cohort(api, selected, expected_dev_sha):
    receipt_template, census_template, inventory, revision = reviewed_declarations(api, expected_dev_sha)
    targets = {target["target_id"]: target for target in inventory["targets"]}
    bindings = inventory.get("receipt_bindings", {})
    fresh_ids = {}
    for target_id, signal in selected:
        target = targets.get(target_id)
        binding = bindings.get(target_id, {}).get(signal)
        declaration = target.get("signals", {}).get(signal) if isinstance(target, dict) else None
        if (not isinstance(binding, dict) or not isinstance(declaration, dict)
                or declaration.get("applicable") is not True
                or binding.get("template_id") != receipt_template["id"]
                or not _positive_id(binding.get("observation_window_seconds"))):
            raise Refusal("selected_target_signal_not_bound")
    for target_id, signal in selected:
        target = targets[target_id]
        declaration = target["signals"][signal]
        binding = bindings[target_id][signal]
        _task_id, status = _launcher.launch(api, RECEIPT_TEMPLATE, {
            "o11y_verification_mode": "strict",
            "expected_repository_sha": expected_dev_sha,
            "expected_inventory_revision": revision,
            "expected_target_id": target_id,
            "expected_signal": signal,
            "signal_selector": declaration["selector"],
            "freshness_seconds": declaration["freshness_seconds"],
            "observation_window_seconds": binding["observation_window_seconds"],
        }, dry_run=False, block=True)
        if not _positive_id(_task_id):
            raise Refusal("receipt_task_id_invalid_after_launch; inspect Semaphore before retrying")
        if status != "success":
            raise Refusal(f"receipt_task_failed; inspect task {_task_id} before retrying")
        fresh_ids.setdefault(target_id, {})[signal] = _task_id
    _task_id, status = _launcher.launch(api, CENSUS_TEMPLATE, {
        "expected_repository_sha": expected_dev_sha,
        "receipt_task_ids": json.dumps(fresh_ids, sort_keys=True, separators=(",", ":")),
    }, dry_run=False, block=True)
    if not _positive_id(_task_id):
        raise Refusal("census_task_id_invalid_after_launch; inspect Semaphore before retrying")
    if status != "success":
        raise Refusal(f"coverage_census_failed; inspect task {_task_id} before retrying")
    return len(selected), _task_id


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="Semaphore HTTPS origin")
    parser.add_argument("--project", type=int, default=PROJECT_ID)
    parser.add_argument("--reviewed-dev-sha", required=True)
    parser.add_argument("--target-signal", action="append", required=True, help="target_id:signal; repeat up to 64")
    args = parser.parse_args()
    try:
        if args.project != PROJECT_ID:
            raise Refusal("semaphore_binding_mismatch")
        selected = parse_target_signals(args.target_signal)
        api = _launcher.API(args.url, args.project, sys.stdin.read().strip())
        count, census_id = launch_cohort(api, selected, args.reviewed_dev_sha)
        print(f"Coverage census task {census_id} succeeded after {count} fresh receipt task(s).")
        return 0
    except (Refusal, _launcher.Refusal) as error:
        print(str(error), file=sys.stderr)
        return 2
    except Exception:
        print("coverage_refresh_failed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
