"""Pure tests for artifact admission and authenticated task evidence."""

from __future__ import annotations

import copy
import hashlib
import sys
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
