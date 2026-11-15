from __future__ import annotations

import importlib.util
from pathlib import Path

import playbook_yaml
import pytest

FILE = Path(__file__).resolve().parents[1] / "playbooks/files/inspect_o11y_pbs_provenance.py"
PROVENANCE_TASK = Path(__file__).resolve().parents[1] / "playbooks/tasks/o11y-pbs-inventory-provenance.yml"
TRIM = "Take the Semaphore base URL without a trailing slash (hidden: it comes from the secret read)"
GUARD = "Refuse cleartext Semaphore transport"
PRESENCE = "Require OpenBao values without revealing them"
SEMAPHORE_READS = (
    "Read current production inventory records",
    "Read current template records",
    "Read current repository records",
)
RAW_URL = "_pbs_prov_semaphore_secret.url"
SPEC = importlib.util.spec_from_file_location("inspect_o11y_pbs_provenance", FILE)
provenance = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(provenance)

PUBLIC = "a" * 40
PRIVATE = "b" * 40
INVENTORY = "all:\n  vars:\n    vm_vmid: '120'\n"


def payload() -> dict[str, object]:
    return {
        "public_sha": PUBLIC,
        "private_sha": PRIVATE,
        "private_tip_sha": PRIVATE,
        "private_tree_entry": f"100644 blob {'c' * 40}\tinventory/production.yml",
        "private_inventory": INVENTORY,
        "runtime_inventory": INVENTORY,
        "inventory_source_count": 1,
        "project_id": 1,
        "repository_id": 2,
        "template_name": "Backup o11y to PBS (Dev)",
        "playbook": "platform/playbooks/backup-o11y-to-pbs.yml",
        "semaphore_statuses": [200, 200, 200],
        "inventories": [
            {"id": 3, "project_id": 1, "name": "production", "type": "static-yaml", "inventory": INVENTORY}
        ],
        "templates": [{
            "id": 4,
            "project_id": 1,
            "name": "Backup o11y to PBS (Dev)",
            "inventory_id": 3,
            "repository_id": 2,
            "playbook": "platform/playbooks/backup-o11y-to-pbs.yml",
        }],
        "repositories": [{
            "id": 2,
            "project_id": 1,
            "name": "agent-cloud dev",
            "git_url": "https://github.com/uhstray-io/agent-cloud.git",
            "git_branch": "dev",
        }],
    }


def test_exact_reviewed_bytes_and_current_config_return_only_hashes() -> None:
    result = provenance.evaluate(payload())
    assert result["status"] == "observed"
    assert result["reason"] == "exact_bytes_match"
    assert result["inventory_provenance"] == "exact_bytes_match"
    assert len(result["inventory_fingerprint"]) == 64
    assert len(result["source_fingerprint"]) == 64
    assert INVENTORY not in str(result)


def test_provenance_task_requires_one_total_source_and_preserves_file_bytes() -> None:
    task_path = Path(__file__).resolve().parents[1] / "playbooks/tasks/o11y-pbs-inventory-provenance.yml"
    task = task_path.read_text()
    assert "ansible_inventory_sources | length == 1" in task
    assert "ansible_inventory_sources[0] is file" in task
    assert "query('ansible.builtin.file', ansible_inventory_sources[0], rstrip=false)" in task


@pytest.mark.parametrize(
    ("update", "reason", "status"),
    [
        (lambda value: value.update(private_tip_sha="d" * 40), "branch_tip_mismatch", "unavailable"),
        (lambda value: value.update(inventory_source_count=2), "inventory_source_unknown", "unavailable"),
        (
            lambda value: value.update(
                private_tree_entry="120000 blob " + "c" * 40 + "\tinventory/production.yml"
            ),
            "inventory_source_unknown",
            "unavailable",
        ),
        (
            lambda value: value.update(runtime_inventory=INVENTORY + "# changed\n"),
            "inventory_bytes_differ",
            "mismatch",
        ),
        (
            lambda value: value.update(private_inventory="all:\n  vars:\n    x: 1\n    x: 2\n"),
            "malformed_yaml",
            "unavailable",
        ),
        (lambda value: value.update(semaphore_statuses=[200, 500, 200]), "semaphore_api_error", "unavailable"),
    ],
)
def test_provenance_refusals_are_fixed_and_private_bytes_are_not_echoed(update, reason, status) -> None:
    candidate = payload()
    update(candidate)
    result = provenance.evaluate(candidate)
    assert result["status"] == status
    assert result["reason"] == reason
    assert INVENTORY not in str(result)


def test_duplicate_semaphore_identities_refuse() -> None:
    candidate = payload()
    candidate["templates"] = candidate["templates"] * 2
    result = provenance.evaluate(candidate)
    assert result["reason"] == "identity_ambiguous"


def _leaves() -> list[dict]:
    return [task for task in playbook_yaml.tasks(playbook_yaml.load(PROVENANCE_TASK)) if "block" not in task]


def test_provenance_takes_the_trimmed_semaphore_url_once_and_hidden() -> None:
    trim = [task for task in _leaves() if task.get("name") == TRIM]
    assert len(trim) == 1
    assert trim[0]["no_log"] is True
    expression = trim[0]["ansible.builtin.set_fact"]["_pbs_prov_semaphore_url"]
    assert "| trim" in expression
    assert "regex_replace('/+$', '')" in expression
    # The raw stored url is read only inside the trim; the presence check, the guard and every request use the trim.
    assert [task["name"] for task in _leaves() if RAW_URL in repr(task)] == [TRIM]


def test_provenance_trims_before_its_presence_check_guard_and_requests() -> None:
    names = [task.get("name") for task in _leaves()]
    assert names.index(TRIM) < names.index(PRESENCE) < names.index(GUARD)
    assert names.index(GUARD) < min(names.index(name) for name in SEMAPHORE_READS)


def test_provenance_guard_and_every_semaphore_request_use_the_trimmed_url() -> None:
    leaves = _leaves()
    guard = next(task for task in leaves if task.get("name") == GUARD)
    assert guard["ansible.builtin.include_tasks"] == "assert-bao-transport.yml"
    assert guard["vars"]["_assert_bao_url"] == "{{ _pbs_prov_semaphore_url }}"
    requests = [task for task in leaves if "/api/project/" in str(task.get("ansible.builtin.uri", {}).get("url", ""))]
    assert sorted(task["name"] for task in requests) == sorted(SEMAPHORE_READS)
    for task in requests:
        assert task["ansible.builtin.uri"]["url"].startswith("{{ _pbs_prov_semaphore_url }}/api/project/"), task["name"]
