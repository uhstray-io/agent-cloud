"""Pure tests for artifact admission and authenticated task evidence."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "playbooks" / "files"))
import o11y_isolated_restore as restore  # noqa: E402


def test_exact_artifact_config_requires_source_vmid_included_disks_and_hashes():
    config = {
        "status": 200,
        "data": (
            "vmid: 700\nagent: enabled=1\n"
            "scsi0: pbs:backup/vm/700,backup=1,size=20G\n"
            "net0: virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0\n"
        ),
    }
    result = restore.exact_artifact_config(config, source_vmid=700)
    assert result["disk_count"] == 1
    assert result["nic_keys"] == ["net0"]
    assert result["config_sha256"] == hashlib.sha256(config["data"].encode()).hexdigest()


@pytest.mark.parametrize(
    "line",
    [
        "hookscript: local:snippets/restore-hook.pl",
        "args: -device host-pci",
        "hostpci0: 0000:03:00.0",
        "usb0: host=1-1",
        "virtiofs0: shared",
        "mp0: /srv/share,mp=/host",
        "smbios1: uuid=abc",
        "cicustom: user=local:snippets/custom.yaml",
        "unused0: local:orphan-disk",
        "ide2: local:iso/debian.iso,media=cdrom",
        "scsi0: pbs:archive,size=20G,backup=0",
        "scsi0: pbs:archive,size=20G,backup=0\nscsi0: pbs:archive,size=20G,backup=1",
    ],
)
def test_unsafe_passthrough_shared_hooks_or_excluded_disks_refuse(line):
    config = {"status": 200, "data": "vmid: 700\nscsi0: pbs:backup/vm/700,backup=1,size=20G\n" + line + "\n"}
    with pytest.raises(restore.Refusal):
        restore.exact_artifact_config(config, source_vmid=700)


def test_artifact_config_requires_exact_source_identity_and_bounded_bytes():
    with pytest.raises(restore.Refusal):
        restore.exact_artifact_config(
            {"status": 200, "data": "vmid: 701\nagent: enabled=1\nscsi0: pbs:backup,size=20G\n"}, source_vmid=700
        )
    with pytest.raises(restore.Refusal):
        restore.exact_artifact_config({"status": 200, "data": "x" * (256 * 1024 + 1)}, source_vmid=700)


def test_target_readback_requires_disconnected_owned_stopped_config():
    good = {
        "status": 200,
        "data": {
            "vmid": 910001,
            "onboot": 0,
            "description": "o11y-restore:abc",
            "scsi0": "local:disk",
        },
    }
    assert (
        restore.validate_target_config(
            good,
            target_vmid=910001,
            marker="o11y-restore:abc",
            isolated=True,
            expected_disk_keys=["scsi0"],
            storage_id="local",
        )["nic_count"]
        == 0
    )
    for key, value in (
        ("net0", "virtio=aa,bridge=vmbr0"),
        ("hostpci0", "0000:02:00.0"),
        ("onboot", 1),
        ("startup", "order=1"),
        ("description", "other"),
        ("unused0", "local:orphan-disk"),
        ("scsi1", "local:unexpected-disk"),
        ("scsi0", "shared:disk"),
    ):
        bad = copy.deepcopy(good)
        bad["data"][key] = value
        with pytest.raises(restore.Refusal):
            restore.validate_target_config(
                bad,
                target_vmid=910001,
                marker="o11y-restore:abc",
                isolated=True,
                expected_disk_keys=["scsi0"],
                storage_id="local",
            )


def task_payload(receipt):
    ended = datetime.now(UTC).replace(microsecond=0)
    started = ended - timedelta(minutes=1)
    created = ended - timedelta(minutes=2)
    expected = {
        "task_id": 123,
        "project_id": 1,
        "environment_id": 2,
        "inventory_id": 3,
        "template_id": 4,
        "repository_id": 5,
        "expected_commit": "a" * 40,
        "expected_template_name": "fixture template",
        "expected_playbook": "fixture/playbook.yml",
        "expected_repository_url": "https://github.com/uhstray-io/agent-cloud.git",
        "expected_receipt_type": "o11y_backup_task/v1",
        "expected_receipt_bindings": {"artifact_fingerprint": "f" * 64},
    }
    task = {
        "id": 123,
        "project_id": 1,
        "environment_id": 2,
        "inventory_id": 3,
        "template_id": 4,
        "repository_id": 5,
        "status": "success",
        "commit_hash": "a" * 40,
        "playbook": "fixture/playbook.yml",
        "arguments": "",
        "git_branch": "",
        "limit": "",
        "secret": "",
        "schedule_id": None,
        "workflow_run_id": None,
    }
    task.update(
        created=created.isoformat().replace("+00:00", "Z"),
        start=started.isoformat().replace("+00:00", "Z"),
        end=ended.isoformat().replace("+00:00", "Z"),
    )
    template = {
        "id": 4,
        "project_id": 1,
        "name": expected["expected_template_name"],
        "playbook": expected["expected_playbook"],
        "environment_id": 2,
        "inventory_id": 3,
        "repository_id": 5,
    }
    repo = {"id": 5, "project_id": 1, "name": "agent-cloud dev", "git_url": expected["expected_repository_url"]}
    return {
        **expected,
        "task": task,
        "output": [
            {
                "id": 1,
                "task_id": 123,
                "time": ended.isoformat().replace("+00:00", "Z"),
                "output": "TASK CUSTOM STATS\n" + json.dumps(receipt),
            }
        ],
        "template": template,
        "repository": repo,
    }


def test_task_authentication_requires_historical_bindings_and_one_full_receipt():
    receipt = {"type": "o11y_backup_task/v1", "status": "verified", "bindings": {"artifact_fingerprint": "f" * 64}}
    receipt["receipt_fingerprint"] = restore.receipt_fingerprint(receipt)
    proof = restore.verify_task_proof(task_payload(receipt))
    assert proof["status"] == "verified"
    missing_history = task_payload(receipt)
    del missing_history["task"]["environment_id"]
    with pytest.raises(restore.Refusal, match="task_binding_unknown"):
        restore.verify_task_proof(missing_history)


def test_task_must_be_successful_exact_revision_and_bound_to_dev_environment_inventory():
    receipt = {"type": "o11y_backup_task/v1", "status": "verified", "bindings": {"artifact_fingerprint": "f" * 64}}
    receipt["receipt_fingerprint"] = restore.receipt_fingerprint(receipt)
    for mutate in (
        lambda p: p["task"].update(status="running"),
        lambda p: p["task"].update(commit_hash="b" * 40),
        lambda p: p["task"].update(inventory_id=99),
        lambda p: p["template"].update(playbook="other.yml"),
        lambda p: p["repository"].update(git_url="https://example.invalid/repo.git"),
    ):
        payload = task_payload(receipt)
        mutate(payload)
        with pytest.raises(restore.Refusal):
            restore.verify_task_proof(payload)


def test_task_output_receipt_duplicate_or_changed_content_fails_closed():
    receipt = {"type": "o11y_backup_task/v1", "status": "verified", "bindings": {"artifact_fingerprint": "f" * 64}}
    receipt["receipt_fingerprint"] = restore.receipt_fingerprint(receipt)
    payload = task_payload(receipt)
    payload["output"].append({"id": 2, "task_id": 123, "output": json.dumps(receipt)})
    with pytest.raises(restore.Refusal):
        restore.verify_task_proof(payload)
    payload = task_payload(receipt)
    payload["output"][0]["output"] = payload["output"][0]["output"].replace("verified", "failed")
    with pytest.raises(restore.Refusal):
        restore.verify_task_proof(payload)
