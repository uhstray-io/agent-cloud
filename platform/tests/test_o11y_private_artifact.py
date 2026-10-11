import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "platform/playbooks/files"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from o11y_private_artifact import (  # noqa: E402
    INVENTORY_REFUSAL,
    _catalog_from_payload,
    _insert_selector,
    _validate_catalog,
    catalog_path,
    propose,
    verify_capture,
    verify_proposal,
)
from test_o11y_backup_artifact import payload  # noqa: E402


def catalog_fixture():
    data = payload()
    data["node"] = "node-test"
    data["vms"][0]["node"] = "node-test"
    data["storages"][0]["content"] = "backup"
    candidate = dict(data["content_reads"][0]["json"]["data"][0])
    candidate.update(
        volid="private-store:backup/vzdump-qemu-100-2026_09_29-01_00_00.vma.zst",
        ctime=1_790_640_000,
        size=123456,
    )
    data["content_reads"][0]["json"]["data"].append(candidate)
    target = {"host": "o11y-test", "node": "node-test", "vmid": "100"}
    # The existing synthetic fixture intentionally uses private-looking identifiers.
    return _catalog_from_payload(data, target, "a" * 40, "b" * 40, 1_800_000_000), data


def test_capture_catalog_is_sorted_and_maps_rows_without_public_identifiers():
    catalog, _ = catalog_fixture()
    assert catalog["schema_version"] == 1
    assert [row["volid"] for row in catalog["rows"]] == sorted(row["volid"] for row in catalog["rows"])
    assert catalog["rows"][0]["backend"] == "dir"
    assert "volid" not in {"backend", "format", "size_bytes"}


def test_catalog_schema_and_fixed_path_refuse_unknown_content_and_symlinks(tmp_path):
    catalog, _ = catalog_fixture()
    malformed = copy.deepcopy(catalog)
    malformed["unreviewed"] = True
    with pytest.raises(ValueError, match="catalog is malformed"):
        _validate_catalog(malformed)
    safe = catalog_path(str(tmp_path))
    assert safe["path"].endswith("catalogs/o11y/backup-artifacts.json")
    second = tmp_path / "second"
    second.mkdir()
    (tmp_path / "catalogs").rename(second / "catalogs")
    (tmp_path / "catalogs").symlink_to(second / "catalogs")
    with pytest.raises(ValueError):
        catalog_path(str(tmp_path))


def test_proposal_accepts_reordered_complete_census_and_returns_exact_selector():
    catalog, data = catalog_fixture()
    data = copy.deepcopy(data)
    data["content_reads"][0]["json"]["data"].reverse()
    result = propose(catalog, data, "c" * 40, 1, 1_800_000_030, "o11y-test")
    assert result["row_number"] == 1
    assert set(result["selector"]) == {"volid", "backend", "format", "creation_time_epoch", "size_bytes"}


def test_proposal_refuses_stale_future_and_changed_census():
    catalog, data = catalog_fixture()
    with pytest.raises(ValueError, match="freshness window"):
        propose(catalog, data, "c" * 40, 1, catalog["observed_at_epoch"] + 1801, "o11y-test")
    with pytest.raises(ValueError, match="freshness window"):
        propose(catalog, data, "c" * 40, 1, catalog["observed_at_epoch"] - 1, "o11y-test")
    changed = copy.deepcopy(data)
    changed["content_reads"][0]["json"]["data"][0]["size"] += 1
    with pytest.raises(ValueError, match="differs from the private artifact capture"):
        propose(catalog, changed, "c" * 40, 1, catalog["observed_at_epoch"] + 1, "o11y-test")
    with pytest.raises(ValueError, match="differs from the private artifact capture"):
        propose(catalog, data, "c" * 40, 1, catalog["observed_at_epoch"] + 1, "other-host")


def test_inventory_inserts_only_selector_and_preserves_existing_bytes():
    catalog, _ = catalog_fixture()
    source = (
        "agent_cloud:\n  children:\n    o11y_svc:\n      hosts:\n        o11y-test:\n"
        "          vm_node: node-test\n          vm_vmid: '100'\n"
    )
    updated = _insert_selector(source, catalog, 1)
    assert updated.startswith(source)
    assert updated.count("o11y_backup_artifact_selector:") == 1
    assert 'volid: "private-store:' in updated
    assert "private-store" not in INVENTORY_REFUSAL


@pytest.mark.parametrize(
    "source",
    [
        "all: {children: {}}\n",
        (
            "agent_cloud:\n  children:\n    o11y_svc:\n      hosts:\n"
            "        o11y-test:\n          vm_node: wrong\n          vm_vmid: '100'\n"
        ),
        (
            "agent_cloud:\n  children:\n    o11y_svc:\n      hosts:\n"
            "        o11y-test:\n          vm_node: node-test\n"
            "          vm_node: duplicate\n          vm_vmid: '100'\n"
        ),
        (
            "agent_cloud: &x\n  children:\n    o11y_svc:\n      hosts:\n"
            "        o11y-test:\n          vm_node: node-test\n"
            "          vm_vmid: '100'\nother: *x\n"
        ),
    ],
)
def test_inventory_mismatch_duplicate_or_alias_refuses(source):
    catalog, _ = catalog_fixture()
    with pytest.raises(ValueError, match="private inventory does not match"):
        _insert_selector(source, catalog, 1)


def test_capture_and_deterministic_proposal_provenance_are_exact(tmp_path):
    catalog, _ = catalog_fixture()
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)

    def git(*args):
        return subprocess.run(["git", *args], cwd=repo, check=True, text=True, capture_output=True).stdout.strip()

    git("config", "user.name", "Synthetic Test")
    git("config", "user.email", "synthetic@example.invalid")
    git("switch", "-q", "-c", "dev")
    inventory = (
        "agent_cloud:\n  children:\n    o11y_svc:\n      hosts:\n        o11y-test:\n"
        "          vm_node: node-test\n          vm_vmid: '100'\n"
    )
    inv_path = repo / "inventory/production.yml"
    inv_path.parent.mkdir(parents=True)
    inv_path.write_text(inventory)
    git("add", ".")
    git("commit", "-q", "-m", "base")
    base = git("rev-parse", "HEAD")
    catalog["site_config_base_sha"] = base
    catalog_path = repo / "catalogs/o11y/backup-artifacts.json"
    catalog_path.parent.mkdir(parents=True)
    catalog_path.write_text(json.dumps(catalog) + "\n")
    git("add", "catalogs/o11y/backup-artifacts.json")
    git("commit", "-q", "-m", "capture")
    capture = git("rev-parse", "HEAD")
    assert verify_capture(str(repo), capture, base, "a" * 40) == catalog
    git("switch", "-q", "-c", "o11y/capture")
    git("remote", "add", "origin", str(remote))
    git("push", "-q", "origin", "dev", "o11y/capture")

    branch = f"proposal/o11y-artifact-{capture}-row-1"
    git("switch", "-q", "-c", branch, base)
    expected = _insert_selector(inventory, catalog, 1)
    inv_path.write_text(expected)
    git("add", "inventory/production.yml")
    git(
        "commit",
        "-q",
        "-m",
        "proposal",
        "-m",
        f"capture={capture} row=1 site-config-base={base} agent-cloud={'a' * 40}",
    )
    git("push", "-q", "origin", branch)
    receipt = verify_proposal(str(repo), branch, base, capture, "a" * 40, 1, expected)
    assert receipt["existing_proposal"] is True
    with pytest.raises(ValueError):
        verify_proposal(str(repo), branch, base, capture, "a" * 40, 1, expected + "# drift\n")


def test_shared_git_helpers_keep_legacy_branch_mode_and_guard_new_mode():
    clone = yaml.safe_load((ROOT / "platform/playbooks/tasks/site-config-clone.yml").read_text())
    push = yaml.safe_load((ROOT / "platform/playbooks/tasks/site-config-push.yml").read_text())
    fresh = next(task for task in clone if task["name"] == "Start a fresh branch for this backup")
    assert "_sc_reviewed_base_sha is not defined" in str(fresh["when"])
    assert any(task["name"] == "Require the reviewed site-config base for new work" for task in clone)
    push_names = [task["name"] for task in push]
    assert push_names.index("Require unchanged integration tip and absent proposal branch") < push_names.index(
        "Push the branch"
    )
    assert push_names.index("Verify guarded branch and integration tip after push") > push_names.index(
        "Push the branch"
    )
    assert "--force" not in (ROOT / "platform/playbooks/tasks/site-config-push.yml").read_text()


def test_semaphore_exposes_distinct_capture_and_proposal_surveys():
    templates = yaml.safe_load((ROOT / "platform/semaphore/templates.yml").read_text())["templates"]
    capture = next(item for item in templates if item["name"] == "Capture o11y Backup Artifact Catalog (Dev)")
    proposal = next(item for item in templates if item["name"] == "Propose o11y Backup Artifact Selector (Dev)")
    assert capture["playbook"] == "platform/playbooks/capture-o11y-backup-artifacts.yml"
    assert proposal["playbook"] == "platform/playbooks/propose-o11y-backup-artifact.yml"
    assert {item["name"] for item in proposal["survey_vars"]} >= {
        "expected_repository_sha",
        "site_config_base_branch",
        "site_config_base_sha",
        "capture_commit_sha",
        "artifact_row",
    }


def test_existing_proposal_verification_precedes_conditional_get_only_census():
    operation = yaml.safe_load((ROOT / "platform/playbooks/tasks/o11y-private-artifact-operation.yml").read_text())
    block = next(task["block"] for task in operation if "block" in task)
    names = [task["name"] for task in block]
    assert names.index("Verify and return an existing deterministic proposal without creating a commit") < names.index(
        "Run the bounded GET-only census only when work is needed"
    )
    census = yaml.safe_load((ROOT / "platform/playbooks/tasks/o11y-backup-artifact-census.yml").read_text())
    requests = [task for task in census if "ansible.builtin.uri" in task]
    assert len(requests) == 4
    assert all(task["ansible.builtin.uri"]["method"] == "GET" and task["no_log"] is True for task in requests)
    assert "_sc_existing_remote.stdout" in str(
        block[names.index("Run the bounded GET-only census only when work is needed")]["when"]
    )
