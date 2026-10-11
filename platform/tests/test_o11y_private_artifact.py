import copy
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "platform/playbooks/files"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import o11y_private_artifact as private_artifact  # noqa: E402
from o11y_private_artifact import (  # noqa: E402
    DRIFT_REFUSAL,
    FRESHNESS_REFUSAL,
    INVENTORY_REFUSAL,
    SAFE_REFUSAL,
    SCHEMA_REFUSAL,
    _catalog_from_payload,
    _insert_selector,
    _validate_catalog,
    catalog_path,
    propose,
    safe_refusal,
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


def test_capture_main_emits_only_allowlisted_sanitized_rows(monkeypatch, capsys):
    catalog, census = catalog_fixture()
    request = {
        "census": census,
        "target": catalog["target"],
        "agent_cloud_sha": catalog["agent_cloud_sha"],
        "site_config_base_sha": catalog["site_config_base_sha"],
    }
    monkeypatch.setattr(private_artifact.sys, "stdin", io.StringIO(json.dumps(request)))
    monkeypatch.setattr(private_artifact.sys, "argv", ["helper", "capture"])

    assert private_artifact.main() == 0
    output = json.loads(capsys.readouterr().out)
    visible_rows = output["rows"]
    allowed = {
        "row",
        "backend",
        "format",
        "creation_time_epoch",
        "size_bytes",
        "proxmox_protected_flag",
    }
    assert output["catalog"]["schema_version"] == 1
    assert visible_rows and all(set(row) == allowed for row in visible_rows)
    rendered_rows = json.dumps(visible_rows)
    assert all(storage["storage_id"] not in rendered_rows for storage in output["catalog"]["storages"])
    assert all(row["volid"] not in rendered_rows for row in output["catalog"]["rows"])
    assert "storage_id" not in rendered_rows and "volid" not in rendered_rows


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


@pytest.mark.parametrize("message", [SAFE_REFUSAL, FRESHNESS_REFUSAL, DRIFT_REFUSAL, SCHEMA_REFUSAL, INVENTORY_REFUSAL])
def test_main_preserves_only_fixed_value_error_refusals(monkeypatch, capsys, message):
    monkeypatch.setattr(private_artifact.sys, "stdin", io.StringIO('{"catalog":{}}'))
    monkeypatch.setattr(private_artifact.sys, "argv", ["helper", "freshness"])
    monkeypatch.setattr(private_artifact, "_validate_catalog", lambda _: (_ for _ in ()).throw(ValueError(message)))

    assert private_artifact.main() == 2
    assert json.loads(capsys.readouterr().out) == {"refusal": message}


@pytest.mark.parametrize("message", ["private path /secret/backup", "Unexpected helper failure: sensitive-value"])
def test_main_replaces_unknown_value_errors_with_generic_refusal(monkeypatch, capsys, message):
    monkeypatch.setattr(private_artifact.sys, "stdin", io.StringIO('{"catalog":{}}'))
    monkeypatch.setattr(private_artifact.sys, "argv", ["helper", "freshness"])
    monkeypatch.setattr(private_artifact, "_validate_catalog", lambda _: (_ for _ in ()).throw(ValueError(message)))

    assert private_artifact.main() == 2
    output = capsys.readouterr().out
    assert json.loads(output) == {"refusal": SAFE_REFUSAL}
    assert message not in output


@pytest.mark.parametrize("stdout", [None, "", "not-json", "{}", '{"refusal":"private value"}'])
def test_safe_refusal_rejects_missing_malformed_and_unlisted_output(stdout):
    assert safe_refusal(stdout) == SAFE_REFUSAL


def test_safe_refusal_returns_only_a_parsed_allowlisted_message():
    assert safe_refusal(json.dumps({"refusal": INVENTORY_REFUSAL})) == INVENTORY_REFUSAL


def test_refusal_operation_prints_only_allowlisted_or_generic_message(monkeypatch, capsys):
    private_output = json.dumps({"refusal": DRIFT_REFUSAL, "details": "/private/backup/path"})
    monkeypatch.setattr(private_artifact.sys, "stdin", io.StringIO(json.dumps({"stdout": private_output})))
    monkeypatch.setattr(private_artifact.sys, "argv", ["helper", "refusal"])

    assert private_artifact.main() == 0
    assert capsys.readouterr().out.strip() == DRIFT_REFUSAL


def test_helper_command_failures_are_normalized_before_visible_refusal():
    operation = yaml.safe_load((ROOT / "platform/playbooks/tasks/o11y-private-artifact-operation.yml").read_text())
    block = next(task["block"] for task in operation if "block" in task)
    helper_tasks = [
        task
        for task in block
        if task.get("ansible.builtin.command", {}).get("argv", [None, None])[0] == "python3"
        and task["ansible.builtin.command"]["argv"][1].endswith("o11y_private_artifact.py")
        and task["ansible.builtin.command"]["argv"][2] != "refusal"
    ]
    assert len(helper_tasks) == 7
    for helper in helper_tasks:
        register = helper["register"]
        assert helper["failed_when"] is False
        assert helper["no_log"] is True
        normalizer = next(task for task in block if task.get("register") == f"{register}_refusal")
        assert normalizer["failed_when"] is False
        assert normalizer["no_log"] is True
        assert f"{register}.stdout" in normalizer["ansible.builtin.command"]["stdin"]
        visible = next(
            task for task in block if f"{register}_refusal.stdout" in str(task.get("ansible.builtin.fail", {}))
        )
        message = visible["ansible.builtin.fail"]["msg"]
        assert "Private artifact selection refused because input did not match the reviewed schema." in message
        assert "The private artifact catalog is malformed or unsupported." in message
        assert f"{register}.stdout" not in message


@pytest.mark.parametrize("register", ["_selection", "_freshness"])
def test_skipped_new_proposal_helpers_skip_normalizer_and_visible_failure(register):
    operation = yaml.safe_load((ROOT / "platform/playbooks/tasks/o11y-private-artifact-operation.yml").read_text())
    block = next(task["block"] for task in operation if "block" in task)
    helper = next(task for task in block if task.get("register") == register)
    proposal_guard = [
        "operation == 'propose'",
        "_sc_existing_remote.stdout | default('') | length == 0",
    ]
    assert helper["when"] == proposal_guard

    dependent_guard = proposal_guard + [f"{register}.rc | default(1) != 0"]
    normalizer = next(task for task in block if task.get("register") == f"{register}_refusal")
    visible = next(
        task
        for task in block
        if "ansible.builtin.fail" in task
        and f"{register}_refusal.stdout" in task["ansible.builtin.fail"]["msg"]
    )
    assert normalizer["when"] == dependent_guard
    assert visible["when"] == dependent_guard


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


def test_inventory_inserts_before_following_sibling_group_at_host_field_indent():
    catalog, _ = catalog_fixture()
    source = (
        "agent_cloud:\n  children:\n    o11y_svc:\n      hosts:\n"
        "        o11y-test:\n          vm_node: node-test\n          vm_vmid: '100'\n"
        "    loki_svc:\n      hosts:\n        loki-test:\n          ansible_host: loki-test\n"
    )
    updated = _insert_selector(source, catalog, 1)
    sibling_at = source.index("    loki_svc:")
    assert updated.index("          o11y_backup_artifact_selector:") < updated.index("    loki_svc:")
    assert updated[: updated.index("    loki_svc:")].startswith(source[:sibling_at])
    assert updated.endswith(source[sibling_at:])
    assert yaml.safe_load(updated)["agent_cloud"]["children"]["loki_svc"] == {
        "hosts": {"loki-test": {"ansible_host": "loki-test"}}
    }


def test_inventory_read_preserves_terminal_newline_in_ansible_command():
    operation = yaml.safe_load(
        (ROOT / "platform/playbooks/tasks/o11y-private-artifact-operation.yml").read_text()
    )
    block = next(task["block"] for task in operation if task.get("name") == "Enter the private branch operation")
    task = next(task for task in block if task["name"] == "Read exact base inventory bytes")
    assert task["ansible.builtin.command"]["strip_empty_ends"] is False
    source = (
        "agent_cloud:\n  children:\n    o11y_svc:\n      hosts:\n        o11y-test:\n"
        "          vm_node: node-test\n          vm_vmid: '100'\n"
    )
    catalog, _ = catalog_fixture()
    updated = _insert_selector(source, catalog, 1)
    assert source.endswith("\n") and updated.endswith("\n")
    assert updated.startswith(source)
    without_terminal_newline = _insert_selector(source.rstrip("\n"), catalog, 1)
    assert without_terminal_newline.startswith(source.rstrip("\n"))


def test_census_identity_fact_is_hidden_and_private_vmids_are_strings():
    census = yaml.safe_load((ROOT / "platform/playbooks/tasks/o11y-backup-artifact-census.yml").read_text())
    assert census[0]["no_log"] is True
    operation = yaml.safe_load(
        (ROOT / "platform/playbooks/tasks/o11y-private-artifact-operation.yml").read_text()
    )
    block = next(task["block"] for task in operation if task.get("name") == "Enter the private branch operation")
    propose_name = "Validate freshness, explicit row, and complete fresh census"
    propose = next(task for task in block if task["name"] == propose_name)
    capture = next(task for task in block if task["name"] == "Prepare a private capture catalog")
    assert "hostvars['localhost']._vmid | string" in propose["ansible.builtin.command"]["stdin"]
    assert "hostvars['localhost']._vmid | string" in capture["ansible.builtin.command"]["stdin"]


def test_guarded_push_requires_one_exact_sha_and_ref_record():
    push = yaml.safe_load((ROOT / "platform/playbooks/tasks/site-config-push.yml").read_text())
    task = next(task for task in push if task["name"] == "Require unchanged integration tip and absent proposal branch")
    assertions = task["ansible.builtin.assert"]["that"]
    assert "_sc_before_push_base.stdout_lines | length == 1" in assertions
    assert "_sc_before_push_base.stdout.split() | length == 2" in assertions
    assert "_sc_before_push_base.stdout.split()[0] == _sc_reviewed_base_sha" in assertions
    assert "_sc_before_push_base.stdout.split()[1] == 'refs/heads/' ~ _sc_guarded_base_branch" in assertions


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
