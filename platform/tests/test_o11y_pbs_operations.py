from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

FILES = Path(__file__).resolve().parents[1] / "playbooks" / "files"
sys.path.insert(0, str(FILES))
SPEC = importlib.util.spec_from_file_location("o11y_pbs_operations", FILES / "o11y_pbs_operations.py")
assert SPEC and SPEC.loader
operations = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(operations)


def _pbs_row(epoch: int = 1792598400, size: int = 1234) -> dict[str, object]:
    stamp = datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "volid": f"pbs-o11y:backup/vm/120/{stamp}",
        "vmid": 120,
        "format": "pbs-vm",
        "ctime": epoch,
        "size": size,
    }


def test_snapshot_selection_binds_one_new_pbs_snapshot_to_exact_operation_window():
    before = _pbs_row(epoch=1792598300)
    created = _pbs_row()
    selected = operations.snapshot_from_operation(
        [before, created],
        vmid="120",
        storage_id="pbs-o11y",
        started_epoch=1792598350,
        ended_epoch=1792598450,
        baseline_volids=[before["volid"]],
    )
    assert selected == {
        "backup_type": "vm",
        "backup_id": "120",
        "backup_time": 1792598400,
        "volid": created["volid"],
        "size_bytes": 1234,
    }


def test_backup_window_epoch_commands_remain_correct_under_non_utc_timezone():
    backup = Path(__file__).resolve().parents[1] / "playbooks" / "backup-o11y-to-pbs.yml"
    plays = yaml.safe_load(backup.read_text())
    tasks = [task for play in plays for task in play.get("tasks", [])]
    epoch_tasks = [task for task in tasks if task.get("name", "").startswith(
        "Read timezone-independent PVE operation "
    )]
    assert [task["name"] for task in epoch_tasks] == [
        "Read timezone-independent PVE operation start epoch",
        "Read timezone-independent PVE operation end epoch",
    ]

    environment = {**os.environ, "TZ": "Etc/GMT+8"}
    for task in epoch_tasks:
        assert task["changed_when"] is False
        assert task["check_mode"] is False
        result = subprocess.run(
            task["ansible.builtin.command"]["argv"],
            capture_output=True,
            check=False,
            env=environment,
            text=True,
        )
        assert result.returncode == 0
        observed_epoch = int(result.stdout.strip())
        assert abs(observed_epoch - int(time.time())) <= 2

        legacy = subprocess.run(
            [
                sys.executable,
                "-c",
                "from datetime import datetime; print(int(datetime.utcnow().strftime('%s')))"
            ],
            capture_output=True,
            check=True,
            env=environment,
            text=True,
        )
        assert abs(observed_epoch - int(legacy.stdout.strip())) >= 7 * 60 * 60


def test_snapshot_selection_refuses_ambiguous_or_stale_operation_output():
    first = _pbs_row()
    second = _pbs_row(epoch=1792598401)
    for rows, baseline in (([first, second], []), ([first], [first["volid"]])):
        with pytest.raises(operations.Refusal):
            operations.snapshot_from_operation(
                rows,
                vmid="120",
                storage_id="pbs-o11y",
                started_epoch=1792598350,
                ended_epoch=1792598450,
                baseline_volids=baseline,
            )


def test_destination_restore_locator_must_match_private_selector_byte_for_byte():
    row = _pbs_row()
    selector = {
        "volid": row["volid"],
        "backend": "pbs",
        "format": "pbs-vm",
        "creation_time_epoch": row["ctime"],
        "size_bytes": row["size"],
    }
    assert operations.select_copy_locator(
        [row],
        vmid="120",
        storage_id="pbs-o11y",
        snapshot_time=1792598400,
        selector=selector,
    ) == {**selector, "storage_id": "pbs-o11y"}
    selector["size_bytes"] = 999
    with pytest.raises(operations.Refusal):
        operations.select_copy_locator(
            [row],
            vmid="120",
            storage_id="pbs-o11y",
            snapshot_time=1792598400,
            selector=selector,
        )


def test_destination_locator_rejects_other_vm_or_duplicate_pbs_artifacts():
    row = _pbs_row()
    selector = {
        "volid": row["volid"],
        "backend": "pbs",
        "format": "pbs-vm",
        "creation_time_epoch": row["ctime"],
        "size_bytes": row["size"],
    }
    with pytest.raises(operations.Refusal):
        operations.select_copy_locator(
            [{**row, "vmid": 121}],
            vmid="120",
            storage_id="pbs-o11y",
            snapshot_time=1792598400,
            selector=selector,
        )
    with pytest.raises(operations.Refusal):
        operations.select_copy_locator(
            [row, row],
            vmid="120",
            storage_id="pbs-o11y",
            snapshot_time=1792598400,
            selector=selector,
        )


def test_manifest_files_cover_declared_source_disk_layout_without_backup_exclusions():
    result = operations.validate_manifest_disk_layout(
        ["qemu-server.conf.blob", "drive-scsi0.img.fidx"],
        {
            "scsi0": "local-lvm:vm-120-disk-0,size=32G",
            "efidisk0": "local-lvm:vm-120-disk-1,backup=0,size=4M",
            "ide2": "local:iso/example.iso,media=cdrom",
        },
    )
    assert result == {"source_disk_count": 1, "manifest_disk_count": 1}


def test_manifest_file_binding_refuses_missing_or_unexpected_disk_files():
    config = {"scsi0": "local-lvm:vm-120-disk-0,size=32G"}
    for files in (
        ["qemu-server.conf.blob"],
        ["qemu-server.conf.blob", "drive-scsi0.img.fidx", "drive-sata1.img.fidx"],
    ):
        with pytest.raises(operations.Refusal):
            operations.validate_manifest_disk_layout(files, config)


def test_backup_and_copy_playbooks_keep_exact_vm_and_non_destructive_sync_contracts():
    playbooks = FILES.parent
    backup = yaml.safe_load((playbooks / "backup-o11y-to-pbs.yml").read_text())
    start_backup = next(
        task
        for play in backup
        if isinstance(play, dict)
        for task in play.get("tasks", [])
        if task.get("name") == "Start one snapshot-mode backup to the existing primary PBS registration"
    )
    pve_request = start_backup["ansible.builtin.uri"]
    assert pve_request["method"] == "POST"
    assert pve_request["url"].endswith("/vzdump")
    assert pve_request["body"] == {
        "vmid": "{{ _pbs_vmid }}",
        "storage": "{{ _pbs_storage }}",
        "mode": "snapshot",
    }
    assert start_backup["no_log"] is True

    sync_source = (playbooks / "sync-o11y-protected-pbs-copy.yml").read_text()
    assert "'remove-vanished': false" in sync_source
    assert "'verified-only': true" in sync_source
    assert "'schedule': ''" in sync_source
    assert "'prune':" not in sync_source
