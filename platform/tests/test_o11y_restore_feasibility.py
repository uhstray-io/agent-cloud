"""Sanitized, fail-closed checks for o11y restore-target feasibility facts."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "platform/playbooks/files"))

from inspect_o11y_restore_feasibility import inspect, main  # noqa: E402

GIB = 1024**3
RESTORE_BYTES = 100 * GIB + 4 * 1024**2


def storage(identity, *, total=200 * GIB, available=180 * GIB, active=1, content="images,rootdir"):
    return {
        "storage": identity,
        "type": "lvmthin",
        "active": active,
        "content": content,
        "total": total,
        "avail": available,
    }


def payload():
    return {
        "artifact_receipt": {
            "target_vm_verified": True,
            "candidate_artifact_count": 1,
            "candidates": [{"backend_class": "non-pbs", "format": "vma.zst"}],
            "source_disk_count": 3,
            "source_disk_layout": [
                {"device": "scsi0", "size_bytes": 96 * GIB, "included_in_backup": None},
                {"device": "efidisk0", "size_bytes": 4 * GIB, "included_in_backup": None},
                {"device": "tpmstate0", "size_bytes": 4 * 1024**2, "included_in_backup": None},
            ],
        },
        "source_node": "private-node-a",
        "nodes": [
            {"node": "private-node-a", "status": "online"},
            {"node": "private-node-b", "status": "online"},
            {"node": "private-node-offline", "status": "offline"},
        ],
        "storage_reads": [
            {
                "item": "private-node-a",
                "status": 200,
                "json": {
                    "data": [
                        storage("private-local-enough"),
                        storage("private-tight", available=150 * GIB),
                        storage("private-shared", available=RESTORE_BYTES + 60 * GIB),
                        storage("private-inactive", active=0),
                        storage("private-backup-only", content="backup"),
                    ]
                },
            },
            {
                "item": "private-node-b",
                "status": 200,
                "json": {
                    "data": [
                        storage("private-shared", available=RESTORE_BYTES + 60 * GIB),
                        storage("private-other-enough", available=180 * GIB),
                    ]
                },
            },
        ],
        "nextid": {"status": 200, "json": {"data": 12345}},
    }


def test_reports_aggregate_api_facts_without_selection_or_private_identifiers():
    result = inspect(payload())

    assert result == {
        "survey": "read-only-restore-target-feasibility",
        "source_layout_parsed": True,
        "source_disk_sizes_complete": True,
        "backup_inclusion_verified": False,
        "storage_capacity_basis": "proxmox-reported",
        "storage_count_scope": "distinct-image-storage-ids-off-source-online-nodes",
        "image_storage_ids_off_source_meeting_reported_capacity_count": 2,
        "other_node_reported_capacity_sufficient": True,
        "unused_cluster_vmid_available_now": True,
        "vmid_reserved": False,
        "artifact_immutability_verified": False,
        "isolated_restore_target_verified": False,
        "restore_test_verified": False,
    }
    rendered = str(result)
    assert all(private not in rendered for private in (
        "private-node", "private-local", "private-tight", "private-shared", "private-inactive",
        "private-backup", "12345", "scsi0", "efidisk0", "tpmstate0", "200",
    ))
    assert set(result) == {
        "survey", "source_layout_parsed", "source_disk_sizes_complete", "backup_inclusion_verified",
        "storage_capacity_basis", "storage_count_scope",
        "image_storage_ids_off_source_meeting_reported_capacity_count",
        "other_node_reported_capacity_sufficient",
        "unused_cluster_vmid_available_now",
        "vmid_reserved", "artifact_immutability_verified", "isolated_restore_target_verified",
        "restore_test_verified",
    }


def test_headroom_threshold_is_inclusive_and_counts_storage_ids_once():
    data = payload()
    reads = data["storage_reads"]
    # At 200 GiB total, exactly 30% remains after the complete source layout.
    threshold = RESTORE_BYTES + 60 * GIB
    reads[0]["json"]["data"][0].update(total=200 * GIB, avail=threshold)
    reads[0]["json"]["data"][1].update(total=200 * GIB, avail=threshold - 1)
    reads[1]["json"]["data"][0].update(total=200 * GIB, avail=threshold)
    reads[1]["json"]["data"].pop()

    assert inspect(data)["image_storage_ids_off_source_meeting_reported_capacity_count"] == 1


def test_source_node_storage_does_not_count_as_an_isolated_restore_option():
    data = payload()
    data["storage_reads"][1]["json"]["data"] = []
    result = inspect(data)
    assert result["image_storage_ids_off_source_meeting_reported_capacity_count"] == 0
    assert result["other_node_reported_capacity_sufficient"] is False


def test_unsupported_artifact_format_fails_closed_for_capacity_even_with_complete_sizes():
    data = payload()
    data["artifact_receipt"]["candidates"][0]["format"] = "unsupported"
    result = inspect(data)
    assert result["source_layout_parsed"] is False
    assert result["source_disk_sizes_complete"] is True
    assert result["image_storage_ids_off_source_meeting_reported_capacity_count"] == 0


def test_explicit_backup_inclusion_is_reported_only_when_every_disk_is_true():
    data = payload()
    disks = data["artifact_receipt"]["source_disk_layout"]
    for disk in disks:
        disk["included_in_backup"] = True
    assert inspect(data)["backup_inclusion_verified"] is True
    disks[-1]["included_in_backup"] = False
    assert inspect(data)["backup_inclusion_verified"] is False
    disks[-1]["included_in_backup"] = None
    assert inspect(data)["backup_inclusion_verified"] is False


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda data: data.update(nodes=[]), "no online nodes"),
        (lambda data: data.update(source_node="private-node-missing"), "source node is missing"),
        (lambda data: data["nodes"].append(dict(data["nodes"][0])), "duplicate node identities"),
        (lambda data: data["nodes"][0].update(status="unknown"), "malformed node listing"),
        (lambda data: data["storage_reads"].pop(), "incomplete storage-capacity listing"),
        (lambda data: data["storage_reads"][0].update(status=403), "malformed storage-capacity listing"),
        (
            lambda data: data["storage_reads"][0]["json"]["data"].append(
                dict(data["storage_reads"][0]["json"]["data"][0])
            ),
            "duplicate storage identities",
        ),
        (
            lambda data: data["storage_reads"][0]["json"]["data"][0].pop("avail"),
            "malformed image-storage capacity",
        ),
        (
            lambda data: data["storage_reads"][0]["json"]["data"][0].update(total=-1),
            "malformed image-storage capacity",
        ),
        (
            lambda data: data["storage_reads"][0]["json"]["data"][0].update(content=None),
            "malformed storage-capacity listing",
        ),
        (lambda data: data.update(nextid={"status": 200, "json": {"data": "0"}}), "malformed next-VMID"),
    ],
)
def test_malformed_or_incomplete_inputs_fail_closed(mutate, message):
    data = payload()
    mutate(data)
    with pytest.raises(ValueError, match=message):
        inspect(data)


def test_unsupported_or_incomplete_source_facts_do_not_claim_restore_support():
    data = payload()
    data["artifact_receipt"]["candidates"][0]["format"] = "unknown-format"
    data["artifact_receipt"]["source_disk_layout"][0]["size_bytes"] = None

    result = inspect(data)
    assert result["source_layout_parsed"] is False
    assert result["source_disk_sizes_complete"] is False
    assert result["backup_inclusion_verified"] is False
    assert result["image_storage_ids_off_source_meeting_reported_capacity_count"] == 0
    assert result["restore_test_verified"] is False


@pytest.mark.parametrize("value", ["12345", 12345])
def test_nextid_accepts_proxmox_numeric_string_or_integer_without_reporting_it(value):
    data = payload()
    data["nextid"]["json"]["data"] = value
    result = inspect(data)
    assert result["unused_cluster_vmid_available_now"] is True
    assert result["vmid_reserved"] is False
    assert str(value) not in str(result)


def test_cli_refusal_does_not_echo_proxmox_values(monkeypatch, capsys):
    data = payload()
    data["storage_reads"].pop()
    monkeypatch.setattr(sys, "argv", ["inspect_o11y_restore_feasibility.py", "inspect"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(data)))

    assert main() == 2
    captured = capsys.readouterr()
    assert "private-node" not in captured.out
    assert "private-local" not in captured.out
    assert "12345" not in captured.out
    assert captured.err == ""


def test_playbook_feasibility_reads_are_dev_bound_read_only_and_hidden():
    playbook = ROOT / "platform/playbooks/survey-o11y-backup-artifact.yml"
    plays = yaml.safe_load(playbook.read_text())
    play = next(item for item in plays if item.get("name") == "Survey o11y backup artifact details")
    tasks = play["tasks"]
    api_reads = [task for task in tasks if "ansible.builtin.uri" in task]
    assert len(api_reads) == 7
    assert all(task["ansible.builtin.uri"]["method"] == "GET" for task in api_reads)
    assert all(task.get("no_log") is True for task in api_reads)
    assert all(task.get("check_mode") is False for task in api_reads)
    assert all("Authorization" in task["ansible.builtin.uri"]["headers"] for task in api_reads)
    assert any("/api2/json/nodes" in task["ansible.builtin.uri"]["url"] for task in api_reads)
    assert any("/api2/json/cluster/nextid" in task["ansible.builtin.uri"]["url"] for task in api_reads)
    assert all(task["ansible.builtin.uri"]["method"] not in {"POST", "PUT", "DELETE"} for task in api_reads)
    helper = next(task for task in tasks if task["name"] == "Build sanitized restore-target feasibility facts")
    assert helper.get("no_log") is True
    assert "_feasibility_inspector" in str(helper)
    receipt = next(
        task
        for task in tasks
        if task["name"] == "Report aggregate restore feasibility without private identifiers"
    )
    assert "_restore_feasibility_receipt" in str(receipt)


def test_visible_receipt_contains_no_storage_selection_or_vmid_value():
    result = inspect(payload())
    assert result["vmid_reserved"] is False
    assert result["unused_cluster_vmid_available_now"] is True
    assert not any(key in str(result).lower() for key in ("private-node", "vmid=", "volid", "hash"))
