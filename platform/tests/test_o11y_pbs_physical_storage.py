"""Fail-closed and privacy checks for the read-only PBS-node survey."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "platform/playbooks/files"))

from inspect_o11y_pbs_physical_storage import inspect, main, validate_node  # noqa: E402

GIB = 1024**3


def api(data):
    return {"status": 200, "json": {"data": data}}


def sample():
    return {
        "disks": api([
            {"devpath": "/dev/private-disk-a", "size": 2 * 1024**4, "used": "LVM"},
            {"devpath": "/dev/private-disk-a1", "parent": "/dev/private-disk-a", "size": 1 * GIB,
             "used": "partition"},
            {"devpath": "/dev/private-disk-b", "size": 256 * GIB},
        ]),
        "lvm": api({"leaf": False, "children": [
            {"name": "private-vg", "size": 2 * 1024**4, "free": 500 * GIB, "children": [
                {"name": "private-pv", "size": 2 * 1024**4, "free": 500 * GIB}
            ]}
        ]}),
        "thinpool": api([{
            "lv": "private-pool", "vg": "private-vg", "lv_size": 1000 * GIB,
            "used": 600 * GIB, "metadata_size": 8 * GIB, "metadata_used": 2 * GIB,
        }]),
        "directories": api([{
            "unitfile": "/private/unit", "path": "/private/mount", "device": "/dev/private-device",
            "type": "ext4", "options": "defaults",
        }, {
            "unitfile": "/private/network-unit", "path": "/private/network-mount",
            "device": "private-server:/private/export", "type": "nfs", "options": "defaults",
        }]),
        "storage": api([
            {"storage": "private-lvm", "type": "lvm", "content": "images,backup", "active": 1,
             "shared": 0, "total": 2000 * GIB, "used": 1200 * GIB, "avail": 800 * GIB},
            {"storage": "private-dir", "type": "dir", "content": "backup", "active": 1, "shared": 0},
        ]),
    }


def test_target_node_must_be_private_declared_unique_and_online():
    assert validate_node({"target_node": "private-node", "nodes": [
        {"node": "private-node", "status": "online"},
        {"node": "other", "status": "offline"},
    ]}) == {"node": "private-node"}

    for invalid_nodes in (
        [],
        [{"node": "private-node", "status": "offline"}],
        [{"node": "private-node", "status": "online"}, {"node": "private-node", "status": "online"}],
        [{"node": "private/node", "status": "online"}],
        [{"node": "private-node", "status": "unknown"}],
    ):
        with pytest.raises(ValueError):
            validate_node({"target_node": "private-node", "nodes": invalid_nodes})

    with pytest.raises(ValueError, match="private storage node declaration"):
        validate_node({"target_node": "", "nodes": []})


def test_report_is_allowlisted_and_has_no_private_topology_or_exact_capacity():
    result = inspect(sample())

    assert result == {
        "survey": "read-only-physical-storage-inventory",
        "capacity_basis": "reported_capacity_only",
        "physical_device_count": 2,
        "partition_count": 1,
        "device_usage_class_counts": {
            "lvm": 1, "zfs": 0, "filesystem": 0, "mounted": 0, "partition": 1,
            "other_reported": 0,
        },
        "device_usage_unknown_count": 1,
        "reported_raw_device_capacity_band": "at-least-1-TiB",
        "lvm_volume_group_count": 1,
        "lvm_physical_volume_count": 1,
        "lvm_reported_free_capacity_band": "100-GiB-to-under-1-TiB",
        "lvm_thin_pool_count": 1,
        "lvm_thin_reported_logical_free_capacity_band": "100-GiB-to-under-1-TiB",
        "lvm_thin_reported_logical_headroom_band": "30-to-under-70-percent",
        "managed_directory_count": 2,
        "managed_directory_type_counts": {
            "ext": 1, "xfs": 0, "zfs": 0, "network_fs": 1, "other_reported": 0,
        },
        "directory_locality_verified": False,
        "visible_storage_count": 2,
        "active_storage_count": 2,
        "storage_active_unknown_count": 0,
        "storage_backend_counts": {"lvm": 1, "lvmthin": 0, "directory": 1, "other": 0},
        "storage_shared_true_count": 0,
        "storage_shared_false_count": 2,
        "storage_shared_unknown_count": 0,
        "storage_capacity_known_count": 1,
        "storage_capacity_unreported_count": 1,
        "storage_reported_total_capacity_band_counts": {
            "under-100-GiB": 0, "100-GiB-to-under-1-TiB": 0, "at-least-1-TiB": 1,
        },
        "storage_reported_available_capacity_band_counts": {
            "under-100-GiB": 0, "100-GiB-to-under-1-TiB": 1, "at-least-1-TiB": 0,
        },
        "storage_reported_headroom_band_counts": {
            "under-30-percent": 0, "30-to-under-70-percent": 1,
            "at-least-70-percent": 0, "unknown": 0,
        },
        "device_selected": False,
        "device_safety_verified": False,
        "filesystem_readiness_verified": False,
        "pbs_suitability_verified": False,
        "pbs_readiness_verified": False,
        "write_authorized": False,
    }
    rendered = json.dumps(result)
    for private_value in ("private-disk", "private-vg", "private-pv", "private-pool", "private-dir",
                          "private-lvm", "private-node", "private-server", "/dev/", "/private/",
                          str(2 * 1024**4), str(500 * GIB), str(8 * GIB)):
        assert private_value not in rendered
    assert set(result) == {
        "survey", "capacity_basis", "physical_device_count", "partition_count",
        "device_usage_class_counts", "device_usage_unknown_count", "reported_raw_device_capacity_band",
        "lvm_volume_group_count", "lvm_physical_volume_count", "lvm_reported_free_capacity_band",
        "lvm_thin_pool_count", "lvm_thin_reported_logical_free_capacity_band",
        "lvm_thin_reported_logical_headroom_band", "managed_directory_count",
        "managed_directory_type_counts", "directory_locality_verified", "visible_storage_count",
        "active_storage_count", "storage_active_unknown_count", "storage_backend_counts", "storage_shared_true_count",
        "storage_shared_false_count", "storage_shared_unknown_count", "storage_capacity_known_count",
        "storage_capacity_unreported_count", "storage_reported_total_capacity_band_counts",
        "storage_reported_available_capacity_band_counts", "storage_reported_headroom_band_counts", "device_selected",
        "device_safety_verified", "filesystem_readiness_verified", "pbs_suitability_verified",
        "pbs_readiness_verified", "write_authorized",
    }


@pytest.mark.parametrize(
    ("section", "mutate"),
    [
        ("disks", lambda data: data["disks"]["json"]["data"][0].update(devpath="/bad path")),
        ("disks", lambda data: data["disks"]["json"]["data"][0].update(size="2 TiB")),
        ("disks", lambda data: data["disks"]["json"]["data"][0].update(size=0)),
        ("disks", lambda data: data["disks"]["json"]["data"][0].update(used=2)),
        ("disks", lambda data: data["disks"]["json"]["data"][0].update(used=None)),
        ("disks", lambda data: data["disks"]["json"]["data"][0].update(parent=None)),
        ("lvm", lambda data: data["lvm"]["json"]["data"].update(children="malformed")),
        ("lvm", lambda data: data["lvm"]["json"]["data"]["children"][0].update(free=2**99)),
        ("thinpool", lambda data: data["thinpool"]["json"]["data"][0].pop("metadata_used")),
        ("thinpool", lambda data: data["thinpool"]["json"]["data"][0].update(used=2**99)),
        ("directories", lambda data: data["directories"]["json"]["data"][0].update(type=None)),
        ("storage", lambda data: data["storage"]["json"]["data"][0].update(shared="0")),
        ("storage", lambda data: data["storage"]["json"]["data"][0].update(shared=None)),
        ("storage", lambda data: data["storage"]["json"]["data"][0].update(avail=2**99)),
    ],
)
def test_malformed_endpoint_facts_fail_closed(section, mutate):
    data = sample()
    mutate(data)
    with pytest.raises(ValueError):
        inspect(data)


def test_failed_http_status_and_unreported_disk_usage_are_not_interpreted_as_free():
    data = sample()
    data["disks"]["json"]["data"][0].pop("used")
    data["storage"]["status"] = 403
    with pytest.raises(ValueError):
        inspect(data)

    data = sample()
    result = inspect(data)
    assert result["device_usage_unknown_count"] == 1
    assert result["device_safety_verified"] is False


def test_absent_optional_storage_status_fields_are_counted_as_unknown():
    data = sample()
    storage = data["storage"]["json"]["data"][0]
    storage.pop("active")
    storage.pop("shared")

    result = inspect(data)
    assert result["active_storage_count"] == 1
    assert result["storage_active_unknown_count"] == 1
    assert result["storage_shared_unknown_count"] == 1


def test_zero_capacity_is_unreported_and_not_classified_as_under_100_gib():
    data = sample()
    data["storage"]["json"]["data"].append({
        "storage": "private-empty", "type": "dir", "content": "images", "active": 1,
        "shared": 0, "total": 0, "used": 0, "avail": 0,
    })

    result = inspect(data)

    assert result["visible_storage_count"] == 3
    assert result["storage_capacity_known_count"] == 1
    assert result["storage_capacity_unreported_count"] == 2
    assert result["storage_reported_total_capacity_band_counts"]["under-100-GiB"] == 0
    assert result["storage_reported_available_capacity_band_counts"]["under-100-GiB"] == 0
    assert result["storage_reported_headroom_band_counts"]["unknown"] == 0


@pytest.mark.parametrize(
    ("used", "filesystem_count", "other_count"),
    [("ext4", 1, 0), ("ext3 filesystem", 1, 0), ("extended", 0, 1)],
)
def test_disk_usage_ext_family_requires_filesystem_prefix(used, filesystem_count, other_count):
    data = sample()
    data["disks"]["json"]["data"][0]["used"] = used

    result = inspect(data)

    assert result["device_usage_class_counts"]["filesystem"] == filesystem_count
    assert result["device_usage_class_counts"]["other_reported"] == other_count


def test_cli_refusal_has_only_fixed_safe_text(monkeypatch, capsys):
    data = sample()
    data["storage"]["status"] = 403
    monkeypatch.setattr(sys, "argv", ["inspect_o11y_pbs_physical_storage.py", "inspect"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(data)))

    assert main() == 2
    captured = capsys.readouterr()
    assert "private-" not in captured.out
    assert "/dev/" not in captured.out
    assert captured.err == ""


@pytest.mark.parametrize(
    ("section", "expected_refusal"),
    [
        ("disks", "incomplete disk inventory"),
        ("lvm", "incomplete LVM inventory"),
        ("thinpool", "incomplete thin-pool inventory"),
        ("directories", "incomplete directory inventory"),
        ("storage", "incomplete storage status inventory"),
    ],
)
def test_cli_refusal_identifies_only_the_fixed_invalid_section(monkeypatch, capsys, section, expected_refusal):
    data = sample()
    data[section]["status"] = 403
    monkeypatch.setattr(sys, "argv", ["inspect_o11y_pbs_physical_storage.py", "inspect"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(data)))

    assert main() == 2
    captured = capsys.readouterr()
    assert expected_refusal in captured.out
    assert "private-" not in captured.out
    assert "/dev/" not in captured.out


def test_playbook_is_dev_bound_get_only_and_keeps_raw_reads_private():
    playbook = ROOT / "platform/playbooks/survey-o11y-pbs-physical-storage.yml"
    plays = yaml.safe_load(playbook.read_text())
    play = next(item for item in plays if item.get("name") == "Survey declared PBS-node storage without mutation")
    tasks = play["tasks"]
    api_reads = [task for task in tasks if "ansible.builtin.uri" in task]
    assert len(api_reads) == 6
    assert all(task["ansible.builtin.uri"]["method"] == "GET" for task in api_reads)
    assert all(task.get("no_log") is True and task.get("check_mode") is False for task in api_reads)
    assert all("Authorization" in task["ansible.builtin.uri"]["headers"] for task in api_reads)
    assert any(task["ansible.builtin.uri"]["url"].endswith("/api2/json/nodes") for task in api_reads)
    for endpoint in ("disks/list?include-partitions=1", "disks/lvm", "disks/lvmthin", "disks/directory", "/storage"):
        assert any(endpoint in task["ansible.builtin.uri"]["url"] for task in api_reads)
    validation = next(
        task for task in tasks
        if task.get("name") == "Validate that the private target is uniquely online"
    )
    first_node_read = min(
        index for index, task in enumerate(tasks)
        if "ansible.builtin.uri" in task
        and "/api2/json/nodes/{{ _validated_target_node" in str(task)
    )
    assert tasks.index(validation) < first_node_read
    assert validation.get("no_log") is True
    assert "proxmox_pbs_storage_node" in str(play.get("vars"))
    assert all("{{ _target_node" not in str(task) for task in api_reads)


def test_semaphore_template_requires_exact_sha_and_has_no_node_input():
    templates = yaml.safe_load((ROOT / "platform/semaphore/templates.yml").read_text())["templates"]
    template = next(item for item in templates if item.get("name") == "Survey o11y PBS Physical Storage (Dev)")
    assert template["playbook"] == "platform/playbooks/survey-o11y-pbs-physical-storage.yml"
    assert template["repository"] == "agent-cloud dev"
    vars_by_name = {item["name"]: item for item in template["survey_vars"]}
    assert vars_by_name["expected_repository_sha"]["required"] is True
    assert len(vars_by_name) == 1
