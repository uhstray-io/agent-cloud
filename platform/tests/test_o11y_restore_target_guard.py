"""Tests for the fixed-path reserved restore-target guard and writer wiring."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml
from jinja2 import Environment

ROOT = Path(__file__).parents[2]
PLAYBOOKS = ROOT / "platform" / "playbooks"
sys.path.insert(0, str(PLAYBOOKS / "files"))

import guard_o11y_restore_target as guard  # noqa: E402
import o11y_restore_coordination as coordination  # noqa: E402

DECLARATION = {
    "vmid": 410,
    "node": "pve-node",
    "storage_id": "vm-lvms",
    "network_mode": "disconnected",
    "headroom_percent": 30,
}
TARGET_REF = {"node": DECLARATION["node"], "vmid": DECLARATION["vmid"]}


def _record():
    declaration = {
        "declaration_fingerprint": coordination.declaration_fingerprint(DECLARATION),
        "target_fingerprint": coordination.target_identity_fingerprint(TARGET_REF),
        "artifact_fingerprint": "c" * 64,
        "public_sha": "a" * 40,
        "private_inventory_sha": "b" * 64,
    }
    record = {
        **declaration,
        "schema_version": 1,
        "operation_id": "0" * 64,
        "generation": 1,
        "phase": "reserved",
        "restore_nonce": "d" * 64,
        "active_nonce": None,
        "action": None,
        "upid": None,
        "receipt_fingerprint": None,
        "prior_authenticated_task_id": None,
    }
    record["operation_id"] = coordination.operation_identity(record)
    return record


def _snapshot(record):
    return (
        200,
        {"current_version": 1, "versions": {"1": {"deletion_time": "", "destroyed": False}}},
        200,
        {"data": {"data": record, "metadata": {"version": 1}}},
    )


def test_missing_record_only_blocks_declared_target_and_keeps_other_vmids_usable():
    assert guard.check_reserved_target(404, None, 404, None, DECLARATION, [411]) == {
        "result": "allowed",
        "reason": "candidate_not_reserved",
    }
    with pytest.raises(coordination.Refusal):
        guard.check_reserved_target(404, None, 404, None, DECLARATION, [410])
    assert guard.check_reserved_target(404, None, 404, None, None, [411])["result"] == "allowed"


def test_valid_record_keeps_target_reserved_even_after_cleaned_phase():
    record = _record()
    record.update(
        phase="cleaned",
        active_nonce="e" * 64,
        action=None,
        upid="UPID:pve-node:1:2:3:qmdestroy:410:root@pam:",
        receipt_fingerprint="f" * 64,
        prior_authenticated_task_id=77,
    )
    record["operation_id"] = coordination.operation_identity(record)
    args = _snapshot(record)
    assert guard.check_reserved_target(*args, DECLARATION, [411])["result"] == "allowed"
    with pytest.raises(coordination.Refusal):
        guard.check_reserved_target(*args, DECLARATION, [410])


def test_changed_declaration_removed_target_or_untrusted_status_refuses():
    record = _record()
    args = _snapshot(record)
    changed = {**DECLARATION, "storage_id": "other-store"}
    with pytest.raises(coordination.Refusal):
        guard.check_reserved_target(*args, changed, [411])
    with pytest.raises(coordination.Refusal):
        guard.check_reserved_target(*args, None, [411])
    with pytest.raises(coordination.Refusal):
        guard.check_reserved_target(403, {}, 404, None, DECLARATION, [411])
    with pytest.raises(coordination.Refusal):
        guard.check_reserved_target(200, {"malformed": True}, 200, {}, DECLARATION, [411])
    tombstone = {"current_version": 1, "versions": {"1": {"deletion_time": "2026-10-21T00:00:00Z", "destroyed": False}}}
    with pytest.raises(coordination.Refusal):
        guard.check_reserved_target(200, tombstone, 404, None, DECLARATION, [411])


@pytest.mark.parametrize(
    "value",
    [True, False, 410.0, "0410", "410.0", 99, 1_000_000_000],
)
def test_noncanonical_or_wrong_typed_vmids_refuse(value):
    with pytest.raises(coordination.Refusal):
        guard.check_reserved_target(404, None, 404, None, DECLARATION, [value])


def test_target_fingerprint_uses_normalized_declaration_and_rejects_bool_integer():
    string_vmid = coordination.declaration_fingerprint({**DECLARATION, "vmid": "410"})
    int_vmid = coordination.declaration_fingerprint(DECLARATION)
    assert string_vmid == int_vmid
    with pytest.raises(coordination.Refusal):
        coordination.declaration_fingerprint({**DECLARATION, "vmid": True})
    with pytest.raises(coordination.Refusal):
        coordination.declaration_fingerprint({**DECLARATION, "headroom_percent": True})


def test_rendered_guard_include_uses_only_fixed_get_paths_and_protected_candidate_handoff():
    tasks = yaml.safe_load((PLAYBOOKS / "tasks" / "guard-o11y-restore-target.yml").read_text())
    assert all(task.get("tags") == ["always"] for task in tasks)
    uri_tasks = [task["ansible.builtin.uri"] for task in tasks if "ansible.builtin.uri" in task]
    assert [task["method"] for task in uri_tasks] == ["POST", "GET", "GET"]
    assert all(task.get("follow_redirects") == "none" for task in uri_tasks)
    env = Environment()
    env.filters["to_json"] = lambda value: json.dumps(value, separators=(",", ":"))
    env.globals["lookup"] = lambda kind, key: {
        ("env", "BAO_ROLE_ID"): "fixture-role-id",
        ("env", "BAO_SECRET_ID"): "fixture-secret-id",
    }[(kind, key)]
    context = {
        "_o11y_rt_bao_url": "https://bao.private.example",
        "_o11y_rt_login": {"json": {"auth": {"client_token": "protected-token"}}},
        "_o11y_rt_metadata": {"status": 404, "json": {}},
        "_o11y_rt_secret": {"status": 404, "json": {}},
        "_o11y_rt_declaration": DECLARATION,
        "_o11y_restore_target_guard_candidates": [411, 410],
    }
    rendered = [
        {
            "url": env.from_string(task["url"]).render(context),
            "method": task["method"],
            "body": {
                key: env.from_string(value).render(context)
                for key, value in task.get("body", {}).items()
            }
            if isinstance(task.get("body"), dict)
            else None,
            "headers": {
                name: env.from_string(value).render(context)
                for name, value in task.get("headers", {}).items()
            },
        }
        for task in uri_tasks
    ]
    assert [item["url"] for item in rendered] == [
        "https://bao.private.example/v1/auth/approle/login",
        "https://bao.private.example/v1/secret/metadata/coordination/o11y-restore-target",
        "https://bao.private.example/v1/secret/data/coordination/o11y-restore-target",
    ]
    assert rendered[1]["headers"] == {"X-Vault-Token": "protected-token"}
    assert rendered[0]["body"] == {"role_id": "fixture-role-id", "secret_id": "fixture-secret-id"}
    command_task = next(task["ansible.builtin.command"] for task in tasks if "ansible.builtin.command" in task)
    handoff = json.loads(env.from_string(command_task["stdin"]).render(context))
    assert handoff["candidates"] == [411, 410]
    assert handoff["declaration"] == DECLARATION
    assert command_task["argv"] == ["python3", "{{ playbook_dir }}/files/guard_o11y_restore_target.py"]
    for task in tasks:
        if "ansible.builtin.uri" in task or "ansible.builtin.command" in task:
            assert task.get("no_log") is True
        if "ansible.builtin.assert" in task:
            assert task.get("no_log") is not True


def _flatten(tasks):
    for task in tasks:
        yield task
        for key in ("block", "rescue", "always"):
            yield from _flatten(task.get(key, []))


@pytest.mark.parametrize(
    "filename",
    [
        "provision-vm.yml",
        "provision-template.yml",
        "provision-pbs-debian-template.yml",
        "resize-vm.yml",
        "destroy-vm.yml",
        "snapshot-vm.yml",
        "attach-pbs-data-disk.yml",
    ],
)
def test_each_managed_mutating_group_is_preceded_by_guard(filename):
    plays = yaml.safe_load((PLAYBOOKS / filename).read_text())
    flattened = [item for play in plays for item in _flatten(play.get("tasks", []))]
    guard_positions = [
        index
        for index, task in enumerate(flattened)
        if task.get("ansible.builtin.include_tasks") == "tasks/guard-o11y-restore-target.yml"
    ]
    assert guard_positions, filename
    writes = []
    for index, task in enumerate(flattened):
        uri = task.get("ansible.builtin.uri", {})
        if (
            uri.get("method") in {"POST", "PUT", "DELETE"}
            and "/api2/json/" in str(uri.get("url", ""))
            and "agent/ping" not in str(uri.get("url", ""))
        ):
            writes.append(index)
    assert writes, filename
    previous_write = -1
    for write in writes:
        preceding_guards = [guard for guard in guard_positions if previous_write < guard < write]
        assert preceding_guards, f"{filename}: no refreshed guard before mutating task {flattened[write].get('name')}"
        previous_write = write


def test_template_seed_build_and_upload_fallback_are_guarded_separately():
    plays = yaml.safe_load((PLAYBOOKS / "provision-template.yml").read_text())
    flattened = [item for play in plays for item in _flatten(play.get("tasks", []))]
    guard_positions = [
        index
        for index, task in enumerate(flattened)
        if task.get("ansible.builtin.include_tasks") == "tasks/guard-o11y-restore-target.yml"
    ]
    build_position = next(
        index for index, task in enumerate(flattened) if task.get("name") == "Build autoinstall seed ISO"
    )
    fallback_position = next(
        index
        for index, task in enumerate(flattened)
        if task.get("name") == "Refresh reserved-target guard before seed-upload fallback"
    )
    assert any(guard < build_position for guard in guard_positions)
    assert fallback_position in guard_positions
    assert flattened[fallback_position + 1]["name"] == "Upload seed ISO via curl (fallback)"


def test_pbs_wrapper_exact_checkout_pins_guard_dependencies():
    plays = yaml.safe_load((PLAYBOOKS / "provision-pbs-vm.yml").read_text())
    paths = plays[1]["vars"]["_reviewed_checkout_payload_paths"]
    assert "platform/playbooks/tasks/guard-o11y-restore-target.yml" in paths
    assert "platform/playbooks/files/guard_o11y_restore_target.py" in paths
    assert "platform/playbooks/files/o11y_restore_coordination.py" in paths


def test_policy_grants_only_the_fixed_coordination_capabilities():
    policy = (ROOT / "platform/services/openbao/deployment/config/policies/semaphore-read.hcl").read_text()
    data_rule = (
        'path "secret/data/coordination/o11y-restore-target" {\n'
        '  capabilities = ["create", "read", "update"]\n}'
    )
    assert data_rule in policy
    assert 'path "secret/metadata/coordination/o11y-restore-target" {\n  capabilities = ["read"]\n}' in policy
    assert 'secret/data/coordination/*' not in policy
    assert 'secret/metadata/coordination/*' not in policy
