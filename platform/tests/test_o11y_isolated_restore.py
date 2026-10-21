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
import o11y_pbs_receipts as pbs_receipts  # noqa: E402

PUBLIC_SHA = "a" * 40
PRIVATE_SHA = "b" * 40
PROJECT_ID = 7
BACKUP_UPID = "UPID:pve1:00000001:00000002:00000003:vzdump:120:root@pam:"
VERIFY_UPID = "UPID:pve1:00000004:00000005:00000006:pbs-verify:120:root@pam:"
SYNC_UPID = "UPID:pve-copy:00000007:00000008:00000009:pbs-sync:120:root@pam:"
CONTRACTS = restore.PREREQUISITE_CONTRACTS


def _chain_fixture(*, old_backup: bool = False):
    now = datetime.now(UTC).replace(microsecond=0)
    backup_epoch = int((now - timedelta(hours=25 if old_backup else 1)).timestamp())
    manifest = {
        "backup-type": "vm",
        "backup-id": "120",
        "backup-time": backup_epoch,
        "files": [
            {"filename": "drive-scsi0.img.fidx", "crypt-mode": "none", "size": 100, "csum": "1" * 64},
            {"filename": "qemu-server.conf.blob", "crypt-mode": "none", "size": 200, "csum": "2" * 64},
        ],
    }
    artifact = pbs_receipts.artifact_fingerprint(manifest)
    snapshots = {"backup_type": "vm", "backup_id": "120", "backup_time": backup_epoch}
    evidence = [
        {
            "backup_task_id": 100,
            "backup_upid": BACKUP_UPID,
            "snapshot": snapshots,
            "source_vmid": "120",
            "source_node": "pve1",
            "artifact_fingerprint": artifact,
            "playbook": pbs_receipts.CONTRACTS[CONTRACTS[0]]["playbook"],
        },
        {
            "backup_task_id": 100,
            "backup_upid": BACKUP_UPID,
            "verification_upid": VERIFY_UPID,
            "verification_status": "OK",
            "snapshot": snapshots,
            "source_vmid": "120",
            "artifact_fingerprint": artifact,
            "playbook": pbs_receipts.CONTRACTS[CONTRACTS[1]]["playbook"],
        },
        {
            "backup_task_id": 100,
            "verify_task_id": 101,
            "backup_upid": BACKUP_UPID,
            "verification_upid": VERIFY_UPID,
            "sync_upid": SYNC_UPID,
            "verification_status": "OK",
            "snapshot": snapshots,
            "artifact_fingerprint": artifact,
            "remove_vanished": False,
            "prune_enabled": False,
            "playbook": pbs_receipts.CONTRACTS[CONTRACTS[2]]["playbook"],
        },
        {
            "sync_task_id": 102,
            "snapshot": snapshots,
            "artifact_fingerprint": artifact,
            "readback_status": "readable",
            "acl_separation": "verified",
            "pull_direction": "destination_initiated",
            "deletion_isolation": "verified",
            "maintenance_isolation": "verified",
            "playbook": pbs_receipts.CONTRACTS[CONTRACTS[3]]["playbook"],
        },
    ]
    task_proofs = {}
    launches = {}
    task_ids = {}
    for task_id, contract, items in zip((100, 101, 102, 103), CONTRACTS, evidence, strict=True):
        started = now - timedelta(minutes=20 - task_id % 10)
        ended = started + timedelta(minutes=3)
        launch = {"expected_repository_sha": PUBLIC_SHA, "site_config_base_sha": PRIVATE_SHA}
        body = pbs_receipts.seal_receipt(
            {
                "schema_version": 1,
                "contract": contract,
                "status": "complete",
                "reason": "none",
                "public_sha": PUBLIC_SHA,
                "private_sha": PRIVATE_SHA,
                "inventory_provenance": "exact_bytes_match",
                "inventory_fingerprint": "3" * 64,
                "source_fingerprint": "4" * 64,
                "artifact_fingerprint": artifact,
                "observed_start": started.isoformat().replace("+00:00", "Z"),
                "observed_end": ended.isoformat().replace("+00:00", "Z"),
                "role_context": pbs_receipts.CONTRACTS[contract]["roles"],
                "evidence": items,
            }
        )
        task = {
            "id": task_id,
            "project_id": PROJECT_ID,
            "template_id": 55,
            "commit_hash": PUBLIC_SHA,
            "status": "success",
            "created": (started - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
            "start": started.isoformat().replace("+00:00", "Z"),
            "end": ended.isoformat().replace("+00:00", "Z"),
            "environment": json.dumps(launch),
            "playbook": "",
            "params": {"diff": False, "dry_run": False},
        }
        output_rows = [
            {
                "id": 1000 + task_id,
                "task_id": task_id,
                "stage_id": None,
                "time": (ended - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
                "output": json.dumps(body, indent=2),
            }
        ]
        task_proofs[contract] = {"task": task, "output_rows": output_rows}
        launches[contract] = launch
        task_ids[contract] = task_id
    return {
        "public_sha": PUBLIC_SHA,
        "private_sha": PRIVATE_SHA,
        "expected_project_id": PROJECT_ID,
        "expected_task_ids": task_ids,
        "expected_launches": launches,
        "task_proofs": task_proofs,
        "manifest": json.dumps(manifest),
    }


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


def test_task_proof_adapter_delegates_only_the_authenticated_closed_fields(monkeypatch):
    proof = {
        "task": {"id": 123},
        "output_rows": [{"task_id": 123, "id": 1}],
        "contract": "o11y-pbs-backup/v1",
        "public_sha": "a" * 40,
        "private_sha": "b" * 40,
        "playbook": "platform/playbooks/backup-o11y-to-pbs.yml",
        "expected_project_id": 7,
        "expected_task_id": 123,
        "expected_launch": {"expected_repository_sha": "a" * 40},
        "expected_artifact_fingerprint": None,
    }
    captured = {}

    def validate(task, output_rows, **kwargs):
        captured.update(task=task, output_rows=output_rows, **kwargs)
        return {"receipt": {"contract": kwargs["contract"]}, "task_metadata": {"id": task["id"]}}

    monkeypatch.setattr(restore, "validate_completed_task", validate)
    assert restore.verify_task_proof(proof) == {
        "receipt": {"contract": "o11y-pbs-backup/v1"},
        "task_metadata": {"id": 123},
    }
    assert captured["expected_project_id"] == 7
    assert captured["expected_task_id"] == 123
    forged = dict(proof, repository={"git_url": "caller supplied"})
    with pytest.raises(restore.Refusal):
        restore.verify_task_proof(forged)


def test_prerequisite_chain_authenticates_task_order_links_and_artifact():
    result = restore.verify_prerequisite_chain(_chain_fixture())
    assert result["status"] == "verified"
    assert result["task_ids"] == [100, 101, 102, 103]
    assert result["task_metadata"][CONTRACTS[3]]["project_id"] == PROJECT_ID


def test_prerequisite_chain_rejects_link_forgery_duplicate_tasks_and_stale_artifact():
    broken_link = _chain_fixture()
    broken_link["task_proofs"][CONTRACTS[2]]["output_rows"][0]["output"] = broken_link[
        "task_proofs"
    ][CONTRACTS[2]]["output_rows"][0]["output"].replace('"verify_task_id": 101', '"verify_task_id": 999')
    with pytest.raises(restore.Refusal):
        restore.verify_prerequisite_chain(broken_link)

    repeated_task = _chain_fixture()
    repeated_task["expected_task_ids"][CONTRACTS[3]] = 102
    with pytest.raises(restore.Refusal):
        restore.verify_prerequisite_chain(repeated_task)

    stale = _chain_fixture(old_backup=True)
    with pytest.raises(pbs_receipts.Refusal, match="receipt_stale"):
        restore.verify_prerequisite_chain(stale)
