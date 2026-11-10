from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

FILE = Path(__file__).resolve().parents[1] / "playbooks/files/inspect_o11y_pbs_provenance.py"
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
