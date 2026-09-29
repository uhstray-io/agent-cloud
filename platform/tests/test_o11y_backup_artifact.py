"""Sanitization and fail-closed checks for the o11y backup artifact survey."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "platform/playbooks/files"))

from inspect_o11y_backup_artifact import inspect, main  # noqa: E402


def payload():
    return {
        "vmid": "100",
        "node": "private-node",
        "vms": [{"type": "qemu", "vmid": 100, "node": "private-node", "name": "private-host"}],
        "storages": [
            {"storage": "private-store", "type": "dir", "path": "/private/backups"},
        ],
        "content_reads": [{
            "item": {"storage": "private-store"},
            "status": 200,
            "json": {"data": [{
                "volid": "private-store:backup/vzdump-qemu-100-2026_09_28-01_00_00.vma.zst",
                "vmid": 100,
                "format": "vma.zst",
                "size": 987654,
                "ctime": 1_790_553_600,
                "protected": 1,
                "notes": "private operator text",
            }]},
        }],
        "vm_config": {
            "status": 200,
            "data": {
                "scsi0": "private-store:vm-100-disk-0,size=120G,discard=on",
                "virtio1": "private-store:vm-100-disk-1,size=8G,backup=0",
                "ide2": "private-store:iso/installer.iso,media=cdrom",
                "efidisk0": "private-store:vm-100-disk-2,size=528K",
                "tpmstate0": "private-store:vm-100-disk-3,size=4M,version=v2.0",
            },
        },
    }


def test_inspection_reports_decision_facts_without_storage_or_free_form_values():
    result = inspect(payload())

    assert result["candidate_artifact_count"] == 1
    candidate = result["candidates"][0]
    assert candidate["backend_class"] == "non-pbs"
    assert candidate["format"] == "vma.zst"
    assert candidate["size_bytes"] == 987654
    assert candidate["creation_time_epoch"] == 1_790_553_600
    assert candidate["proxmox_protected_flag"] == "protected"
    assert candidate["immutability_verified"] is False
    assert result["source_disk_layout"] == [
        {"device": "efidisk0", "size_bytes": 528 * 1024, "included_in_backup": None},
        {"device": "scsi0", "size_bytes": 120 * 1024**3, "included_in_backup": None},
        {"device": "tpmstate0", "size_bytes": 4 * 1024**2, "included_in_backup": None},
        {"device": "virtio1", "size_bytes": 8 * 1024**3, "included_in_backup": False},
    ]
    assert result["source_disk_layout_complete"] is True
    rendered = str(result)
    assert all(value not in rendered for value in (
        "private-store", "/private/backups", "private-node", "private-host", "operator text", "vm-100-disk"
    ))
    assert result["artifact_immutability_verified"] is False
    assert result["isolated_restore_target_verified"] is False
    assert result["restore_test_verified"] is False
    assert set(result) == {
        "survey", "target_vm_verified", "candidate_artifact_count", "candidates",
        "source_disk_count", "source_disk_layout", "source_disk_layout_complete",
        "artifact_immutability_verified", "isolated_restore_target_verified", "restore_test_verified",
    }
    assert set(candidate) == {
        "backend_class", "format", "size_bytes", "creation_time_epoch",
        "proxmox_protected_flag", "immutability_verified",
    }
    assert all(set(disk) == {"device", "size_bytes", "included_in_backup"}
               for disk in result["source_disk_layout"])
    assert "artifact_identity_sha256" not in str(result)
    assert "volid" not in str(result)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda data: data.update(vmid=""), "VM identity"),
        (lambda data: data.update(vms=[]), "missing or ambiguous"),
        (lambda data: data.update(storages=[{"storage": "private-store", "type": " "}]), "backend type"),
        (lambda data: data["content_reads"][0].update(status=403), "incomplete"),
        (lambda data: data["content_reads"][0]["json"]["data"][0].update(size=-1), "malformed identity"),
        (lambda data: data["content_reads"][0]["json"]["data"][0].update(vmid=101), "does not match"),
        (lambda data: data["content_reads"][0]["json"]["data"][0].update(
            volid="private-store:backup/vzdump-qemu-100-2026_09_28-01_00_00.tar",
            format="tar",
        ), "malformed identity"),
        (lambda data: data["content_reads"][0]["json"]["data"][0].update(
            volid="other-store:backup/vzdump-qemu-100-2026_09_28-01_00_00.vma.zst"
        ), "source storage"),
        (lambda data: data["vm_config"]["data"].update(scsi0="private-store:vm-100-disk-0,size=badG"), "disk size"),
        (lambda data: data.update(content_reads=[]), "incomplete"),
        (lambda data: data["content_reads"][0]["json"]["data"].append(
            dict(data["content_reads"][0]["json"]["data"][0])
        ), "duplicate backup artifact identities"),
    ],
)
def test_malformed_or_incomplete_inputs_fail_closed(mutate, message):
    data = payload()
    mutate(data)
    with pytest.raises(ValueError, match=message):
        inspect(data)


def test_pbs_vm_snapshot_format_and_volume_id_are_supported():
    data = payload()
    data["storages"] = [{"storage": "pbs-store", "type": "pbs"}]
    data["content_reads"][0]["item"]["storage"] = "pbs-store"
    data["content_reads"][0]["json"]["data"] = [{
        "volid": "pbs-store:backup/vm/100/2026-09-29T00:00:00Z",
        "vmid": 100,
        "format": "pbs-vm",
        "size": 123456,
        "ctime": 1_790_640_000,
    }]

    result = inspect(data)
    assert result["candidates"] == [{
        "backend_class": "pbs",
        "format": "pbs-vm",
        "size_bytes": 123456,
        "creation_time_epoch": 1_790_640_000,
        "proxmox_protected_flag": "unknown",
        "immutability_verified": False,
    }]


@pytest.mark.parametrize(
    "update",
    [
        {"format": "vma.zst"},
        {"ctime": 1_790_640_001},
        {"volid": "other-store:backup/vm/100/2026-09-29T00:00:00Z"},
    ],
)
def test_pbs_artifact_type_source_and_timestamp_must_match(update):
    data = payload()
    data["storages"] = [{"storage": "pbs-store", "type": "pbs"}]
    data["content_reads"][0]["item"]["storage"] = "pbs-store"
    candidate = {
        "volid": "pbs-store:backup/vm/100/2026-09-29T00:00:00Z",
        "vmid": 100,
        "format": "pbs-vm",
        "size": 123456,
        "ctime": 1_790_640_000,
    }
    candidate.update(update)
    data["content_reads"][0]["json"]["data"] = [candidate]
    with pytest.raises(ValueError):
        inspect(data)


def test_missing_protection_flag_remains_unknown_and_explicit_false_is_not_protected():
    data = payload()
    candidate = data["content_reads"][0]["json"]["data"][0]
    candidate.pop("protected")
    assert inspect(data)["candidates"][0]["proxmox_protected_flag"] == "unknown"
    candidate["protected"] = False
    assert inspect(data)["candidates"][0]["proxmox_protected_flag"] == "not-protected"


@pytest.mark.parametrize("device", ["efidisk0", "tpmstate0"])
def test_efi_and_tpm_disk_sizes_participate_in_layout_completeness(device):
    data = payload()
    data["vm_config"]["data"][device] = "private-store:vm-100-state-disk"

    result = inspect(data)
    state_disk = next(disk for disk in result["source_disk_layout"] if disk["device"] == device)
    assert state_disk["size_bytes"] is None
    assert result["source_disk_layout_complete"] is False


def test_cli_refusal_does_not_echo_private_proxmox_values(monkeypatch, capsys):
    data = payload()
    data["content_reads"][0]["json"]["data"][0]["volid"] = "private-store:secret-path"
    monkeypatch.setattr(sys, "argv", ["inspect_o11y_backup_artifact.py", "inspect"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(data)))

    assert main() == 2
    captured = capsys.readouterr()
    assert "private-store" not in captured.out
    assert "secret-path" not in captured.out
    assert "private operator text" not in captured.out
    assert captured.err == ""


def test_playbook_is_dev_bound_read_only_and_keeps_raw_api_results_hidden():
    plays = yaml.safe_load((ROOT / "platform/playbooks/survey-o11y-backup-artifact.yml").read_text())
    play = next(item for item in plays if item.get("name") == "Survey o11y backup artifact details")
    tasks = play["tasks"]
    api_reads = [task for task in tasks if "ansible.builtin.uri" in task]
    assert len(api_reads) == 7
    assert all(task["ansible.builtin.uri"]["method"] == "GET" for task in api_reads)
    assert all(task.get("no_log") is True for task in api_reads)
    assert all(task.get("check_mode") is False for task in api_reads)
    assert all("Authorization" in task["ansible.builtin.uri"]["headers"] for task in api_reads)
    assert all("secret/data/services/proxmox" in str(task) for task in tasks
               if task["name"] == "Read Proxmox API credentials from OpenBao")
    assert any(task.get("ansible.builtin.include_tasks") == "tasks/assert-bao-transport.yml" for task in tasks)
    helper = next(task for task in tasks if task["name"] == "Build the sanitized artifact and disk-layout inspection")
    assert helper.get("no_log") is True
    assert "_inspector" in str(helper)
    report = next(
        task for task in tasks
        if task["name"] == "Report private artifact decision facts without storage names or paths"
    )
    assert "_artifact_receipt" in str(report)
    assert not any(task["ansible.builtin.uri"]["method"] in {"POST", "PUT", "DELETE"} for task in api_reads)


def test_semaphore_template_is_dev_bound_and_requires_the_reviewed_sha():
    templates = yaml.safe_load((ROOT / "platform/semaphore/templates.yml").read_text())["templates"]
    template = next(item for item in templates if item["name"] == "Survey o11y Backup Artifact Details (Dev)")
    assert template["repository"] == "agent-cloud dev"
    assert template["playbook"] == "platform/playbooks/survey-o11y-backup-artifact.yml"
    assert template["survey_vars"] == [{
        "name": "expected_repository_sha",
        "title": "Reviewed commit SHA",
        "description": "Exact reviewed Dev revision for the read-only artifact detail survey",
        "type": "string",
        "required": True,
    }]
