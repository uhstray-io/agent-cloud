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

from inspect_o11y_pbs_physical_storage import (  # noqa: E402
    identify_thick_lvm_backing_identity,
    inspect,
    main,
    select_thick_lvm_candidate,
    validate_node,
)

GIB = 1024**3


def api(data):
    return {"status": 200, "json": {"data": data}}


def sample():
    return {
        "target_node": "private-node",
        "declared_storage_id": "private-candidate",
        "storage_config_rows": api([
            {"storage": "private-lvm", "type": "lvm", "content": "images,backup", "vgname": "private-vg"},
            {"storage": "private-dir", "type": "dir"},
        ]),
        "storage_config": api({"type": "lvmthin", "vgname": "private-vg", "thinpool": "private-pool"}),
        "storage_permissions": api({"/storage/private-candidate": {
            "Datastore.Allocate": 1, "Datastore.Audit": 1,
        }}),
        "visible_volumes": api([]),
        "disks": api([
            {"devpath": "/dev/private-disk-a", "size": 2 * 1024**4, "used": "LVM"},
            {"devpath": "/dev/private-disk-a1", "parent": "/dev/private-disk-a", "size": 1 * GIB,
             "used": "partition"},
            {"devpath": "/dev/private-disk-b", "size": 256 * GIB},
        ]),
        "lvm": api({"leaf": False, "children": [
            {"name": "private-vg", "size": 2000 * GIB, "free": 800 * GIB, "children": [
                {"name": "/dev/private-pv", "size": 2000 * GIB, "free": 800 * GIB}
            ]}
        ]}),
        "thinpool": api([{
            "lv": "other-pool", "vg": "other-vg", "lv_size": 1000 * GIB,
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


def candidate_row(*, total=8192 * GIB, used=1024 * GIB, available=None, **overrides):
    if available is None:
        available = total - used if type(total) is int else 0
    return {
        "storage": "private-candidate", "type": "lvmthin", "content": "rootdir,images",
        "active": 1, "shared": 0, "total": total, "used": used, "avail": available,
        **overrides,
    }


def add_candidate(data, row=None, volumes=(), *, metadata_size=8 * GIB, metadata_used=2 * GIB):
    row = row or candidate_row()
    data["storage"]["json"]["data"].append(row)
    data["thinpool"]["json"]["data"].append({
        "lv": "private-pool", "vg": "private-vg", "lv_size": row["total"],
        "used": row["used"], "metadata_size": metadata_size, "metadata_used": metadata_used,
    })
    data["storage_config_rows"]["json"]["data"].append({
        "storage": "private-candidate", "type": "lvmthin", "vgname": "private-vg",
        "thinpool": "private-pool",
    })
    data["visible_volumes"] = api([
        {"volid": f"private-candidate:{name}", "content": kind, "format": "raw", "size": size}
        for name, kind, size in volumes
    ])


def declare_thick_candidate(data):
    data["declared_storage_id"] = "private-lvm"
    data["storage_config"] = api({
        "type": "lvm", "content": "images,backup", "vgname": "private-vg",
    })
    data["storage_permissions"] = api({"/storage/private-lvm": {
        "Datastore.Allocate": 1, "Datastore.Audit": 1,
    }})


def set_thick_capacity(data, total, free):
    status = data["storage"]["json"]["data"][0]
    status.update(total=total, used=total - free, avail=free)
    group = data["lvm"]["json"]["data"]["children"][0]
    group.update(size=total, free=free)
    group["children"][0].update(size=total, free=free)


def selectable_thick_candidate():
    data = sample()
    declare_thick_candidate(data)
    total = 5 * 1024 * GIB
    set_thick_capacity(data, total, total // 2)
    data["lvm"]["json"]["data"]["children"][0]["children"][0]["name"] = "/dev/private-disk-a"
    return data


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

    for invalid in (None, "", "bad/node", ".."):
        data = sample()
        data["target_node"] = invalid
        with pytest.raises(ValueError, match="private storage node declaration"):
            inspect(data)


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
        "declared_storage_row_found": False,
        "declared_storage_row_eligible": False,
        "declared_thinpool_linked": False,
        "visible_volume_permissions_verified": False,
        "visible_volume_inventory_well_formed": False,
        "snapshot_inventory_complete_verified": False,
        "declared_metadata_headroom_30_percent": False,
        "snapshot_unverified_visible_volume_preflight_passes_256_gib_disk": False,
        "snapshot_unverified_visible_volume_preflight_passes_512_gib_disk": False,
        "snapshot_unverified_visible_volume_preflight_passes_1024_gib_disk": False,
        "storage_allocation_authorized": False,
        "declared_thick_lvm_candidate_in_visible_eligible_set": False,
        "declared_thick_lvm_required_config_fields_match": False,
        "declared_thick_lvm_optional_config_fields_match": False,
        "declared_thick_lvm_status_content_matches_config": False,
        "declared_thick_lvm_candidate_exact_match": False,
        "declared_thick_lvm_reported_vg_headroom_passes_256_gib": False,
        "declared_thick_lvm_reported_vg_headroom_passes_512_gib": False,
        "declared_thick_lvm_reported_vg_headroom_passes_1024_gib": False,
        "visible_thick_lvm_image_store_count": 1,
        "thick_lvm_config_vg_mappings_complete": True,
        "thick_lvm_foreign_lvm_config_row_count": 0,
        "thick_lvm_local_config_vg_join_incomplete_count": 0,
        "thick_lvm_unmatched_local_status_row_count": 0,
        "thick_lvm_unmatched_local_config_row_count": 0,
        "thick_lvm_visible_alias_suppressed_candidate_count": 0,
        "thick_lvm_reported_vg_headroom_candidate_count_256_gib": 0,
        "thick_lvm_reported_vg_headroom_candidate_count_512_gib": 0,
        "thick_lvm_reported_vg_headroom_candidate_count_1024_gib": 0,
        "thick_lvm_candidate_vg_pv_inventory_incomplete_count": 0,
        "thick_lvm_candidate_pv_count": 1,
        "thick_lvm_pv_direct_disk_path_join_count": 0,
        "thick_lvm_pv_partition_parent_disk_path_join_count": 0,
        "thick_lvm_pv_path_join_unknown_used_count": 0,
        "thick_lvm_pv_missing_disk_path_join_count": 1,
        "thick_lvm_pv_unverifiable_disk_path_join_count": 0,
        "thick_lvm_backing_media_verified": False,
        "thick_lvm_allocation_authorized": False,
        "device_selected": False,
        "device_safety_verified": False,
        "filesystem_readiness_verified": False,
        "pbs_suitability_verified": False,
        "pbs_readiness_verified": False,
        "write_authorized": False,
    }
    rendered = json.dumps(result)
    for private_value in ("private-disk", "private-vg", "private-pv", "private-pool", "private-dir",
                          "private-candidate",
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
        "storage_reported_available_capacity_band_counts", "storage_reported_headroom_band_counts",
        "declared_storage_row_found", "declared_storage_row_eligible", "declared_thinpool_linked",
        "visible_volume_permissions_verified", "visible_volume_inventory_well_formed",
        "snapshot_inventory_complete_verified",
        "declared_metadata_headroom_30_percent",
        "snapshot_unverified_visible_volume_preflight_passes_256_gib_disk",
        "snapshot_unverified_visible_volume_preflight_passes_512_gib_disk",
        "snapshot_unverified_visible_volume_preflight_passes_1024_gib_disk",
        "storage_allocation_authorized", "declared_thick_lvm_candidate_in_visible_eligible_set",
        "declared_thick_lvm_required_config_fields_match",
        "declared_thick_lvm_optional_config_fields_match",
        "declared_thick_lvm_status_content_matches_config", "declared_thick_lvm_candidate_exact_match",
        "declared_thick_lvm_reported_vg_headroom_passes_256_gib",
        "declared_thick_lvm_reported_vg_headroom_passes_512_gib",
        "declared_thick_lvm_reported_vg_headroom_passes_1024_gib", "device_selected",
        "visible_thick_lvm_image_store_count",
        "thick_lvm_config_vg_mappings_complete",
        "thick_lvm_foreign_lvm_config_row_count",
        "thick_lvm_local_config_vg_join_incomplete_count",
        "thick_lvm_unmatched_local_status_row_count",
        "thick_lvm_unmatched_local_config_row_count",
        "thick_lvm_visible_alias_suppressed_candidate_count",
        "thick_lvm_reported_vg_headroom_candidate_count_256_gib",
        "thick_lvm_reported_vg_headroom_candidate_count_512_gib",
        "thick_lvm_reported_vg_headroom_candidate_count_1024_gib",
        "thick_lvm_candidate_vg_pv_inventory_incomplete_count",
        "thick_lvm_candidate_pv_count",
        "thick_lvm_pv_direct_disk_path_join_count",
        "thick_lvm_pv_partition_parent_disk_path_join_count",
        "thick_lvm_pv_path_join_unknown_used_count",
        "thick_lvm_pv_missing_disk_path_join_count",
        "thick_lvm_pv_unverifiable_disk_path_join_count",
        "thick_lvm_backing_media_verified", "thick_lvm_allocation_authorized",
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


@pytest.mark.parametrize("size_gib", (256, 512, 1024))
def test_thick_lvm_headroom_counts_require_allocation_plus_30_percent(size_gib):
    data = sample()
    total = size_gib * 5 * GIB
    free = size_gib * 5 // 2 * GIB
    status = data["storage"]["json"]["data"][0]
    status.update(total=total, used=total - free, avail=free)
    group = data["lvm"]["json"]["data"]["children"][0]
    group.update(size=total, free=free)
    group["children"][0].update(size=total, free=free)

    result = inspect(data)

    assert result["visible_thick_lvm_image_store_count"] == 1
    assert result[f"thick_lvm_reported_vg_headroom_candidate_count_{size_gib}_gib"] == 1
    assert result["thick_lvm_backing_media_verified"] is False
    assert result["thick_lvm_allocation_authorized"] is False

    status["avail"] -= 1
    status["used"] += 1
    group["free"] -= 1
    group["children"][0]["free"] -= 1
    assert inspect(data)[f"thick_lvm_reported_vg_headroom_candidate_count_{size_gib}_gib"] == 0


def test_declared_thick_lvm_store_matches_visible_candidate_and_reports_fixed_headroom():
    data = sample()
    declare_thick_candidate(data)
    total = 5 * 1024 * GIB
    set_thick_capacity(data, total, total // 2)

    result = inspect(data)

    assert result["declared_thick_lvm_candidate_exact_match"] is True
    assert result["declared_thick_lvm_candidate_in_visible_eligible_set"] is True
    assert result["declared_thick_lvm_required_config_fields_match"] is True
    assert result["declared_thick_lvm_optional_config_fields_match"] is True
    assert result["declared_thick_lvm_status_content_matches_config"] is True
    assert result["declared_thick_lvm_reported_vg_headroom_passes_256_gib"] is True
    assert result["declared_thick_lvm_reported_vg_headroom_passes_512_gib"] is True
    assert result["declared_thick_lvm_reported_vg_headroom_passes_1024_gib"] is True
    assert result["storage_allocation_authorized"] is False
    assert result["thick_lvm_allocation_authorized"] is False
    assert result["thick_lvm_backing_media_verified"] is False
    assert result["pbs_readiness_verified"] is False
    assert result["write_authorized"] is False
    rendered = json.dumps(result)
    assert "private-lvm" not in rendered
    assert "private-vg" not in rendered
    assert "/dev/private-pv" not in rendered


def test_declared_thick_lvm_content_and_nodes_match_as_validated_sets():
    data = sample()
    declare_thick_candidate(data)
    config = data["storage_config_rows"]["json"]["data"][0]
    config.update(content="images,backup", nodes="other-node,private-node")
    data["storage"]["json"]["data"][0]["content"] = "backup,images"
    detail = data["storage_config"]["json"]["data"]
    detail.update(content="backup,images", nodes="private-node,other-node")
    set_thick_capacity(data, 5 * 1024 * GIB, 5 * 1024 * GIB // 2)

    result = inspect(data)

    assert result["declared_thick_lvm_required_config_fields_match"] is True
    assert result["declared_thick_lvm_optional_config_fields_match"] is True
    assert result["declared_thick_lvm_status_content_matches_config"] is True
    assert result["declared_thick_lvm_candidate_exact_match"] is True
    assert result["declared_thick_lvm_reported_vg_headroom_passes_1024_gib"] is True
    assert result["storage_allocation_authorized"] is False
    assert result["thick_lvm_allocation_authorized"] is False
    assert result["thick_lvm_backing_media_verified"] is False
    assert result["pbs_readiness_verified"] is False
    assert result["write_authorized"] is False


@pytest.mark.parametrize(
    "content",
    (
        "images,other",
        "images,backup,images",
        "images,,backup",
        "images, backup",
        "images;backup",
        None,
    ),
)
def test_declared_thick_lvm_invalid_or_changed_detail_content_fails_closed(content):
    data = sample()
    declare_thick_candidate(data)
    data["storage_config"]["json"]["data"]["content"] = content
    set_thick_capacity(data, 5 * 1024 * GIB, 5 * 1024 * GIB // 2)

    result = inspect(data)

    assert result["declared_thick_lvm_candidate_in_visible_eligible_set"] is True
    assert result["declared_thick_lvm_required_config_fields_match"] is False
    assert result["declared_thick_lvm_status_content_matches_config"] is True
    assert result["declared_thick_lvm_candidate_exact_match"] is False
    assert all(
        result[f"declared_thick_lvm_reported_vg_headroom_passes_{size}_gib"] is False
        for size in (256, 512, 1024)
    )


@pytest.mark.parametrize(
    "nodes",
    (
        "other-node",
        "other-node,private-node,private-node",
        "private-node,,other-node",
        "private-node, other-node",
        ".",
    ),
)
def test_declared_thick_lvm_changed_or_malformed_detail_nodes_fail_closed(nodes):
    data = sample()
    declare_thick_candidate(data)
    config = data["storage_config_rows"]["json"]["data"][0]
    config["nodes"] = "other-node,private-node"
    data["storage_config"]["json"]["data"]["nodes"] = nodes
    set_thick_capacity(data, 5 * 1024 * GIB, 5 * 1024 * GIB // 2)

    result = inspect(data)

    assert result["declared_thick_lvm_candidate_in_visible_eligible_set"] is True
    assert result["declared_thick_lvm_required_config_fields_match"] is True
    assert result["declared_thick_lvm_optional_config_fields_match"] is False
    assert result["declared_thick_lvm_candidate_exact_match"] is False
    assert all(
        result[f"declared_thick_lvm_reported_vg_headroom_passes_{size}_gib"] is False
        for size in (256, 512, 1024)
    )


def test_declared_thick_lvm_nodes_presence_mismatch_fails_closed():
    data = sample()
    declare_thick_candidate(data)
    data["storage_config_rows"]["json"]["data"][0]["nodes"] = "private-node"
    set_thick_capacity(data, 5 * 1024 * GIB, 5 * 1024 * GIB // 2)

    result = inspect(data)

    assert result["declared_thick_lvm_optional_config_fields_match"] is False
    assert result["declared_thick_lvm_candidate_exact_match"] is False
    assert all(
        result[f"declared_thick_lvm_reported_vg_headroom_passes_{size}_gib"] is False
        for size in (256, 512, 1024)
    )


def test_unrelated_thick_lvm_headroom_cannot_pass_declared_store_gate():
    data = sample()
    declare_thick_candidate(data)
    set_thick_capacity(data, 2 * 1024 * GIB, 256 * GIB)
    data["storage_config_rows"]["json"]["data"].append({
        "storage": "unrelated-lvm", "type": "lvm", "content": "images,backup",
        "vgname": "unrelated-vg",
    })
    data["storage"]["json"]["data"].append({
        "storage": "unrelated-lvm", "type": "lvm", "content": "images,backup",
        "active": 1, "shared": 0, "total": 5 * 1024 * GIB,
        "used": 2 * 1024 * GIB, "avail": 3 * 1024 * GIB,
    })
    data["lvm"]["json"]["data"]["children"].append({
        "name": "unrelated-vg", "size": 5 * 1024 * GIB, "free": 3 * 1024 * GIB,
        "children": [{"name": "/dev/unrelated-pv", "size": 5 * 1024 * GIB,
                      "free": 3 * 1024 * GIB}],
    })

    result = inspect(data)

    assert result["visible_thick_lvm_image_store_count"] == 2
    assert result["thick_lvm_reported_vg_headroom_candidate_count_256_gib"] == 1
    assert result["declared_thick_lvm_candidate_exact_match"] is True
    assert all(
        result[f"declared_thick_lvm_reported_vg_headroom_passes_{size}_gib"] is False
        for size in (256, 512, 1024)
    )


@pytest.mark.parametrize(
    ("detail_override", "expected_required", "expected_optional"),
    (
        ({"vgname": "other-vg"}, False, True),
        ({"content": "images"}, False, True),
        ({"disable": 0}, True, False),
        ({"nodes": "private-node"}, True, False),
    ),
)
def test_declared_thick_lvm_config_mismatch_is_diagnosed_without_passing_headroom(
    detail_override, expected_required, expected_optional
):
    data = sample()
    declare_thick_candidate(data)
    data["storage_config"]["json"]["data"].update(detail_override)
    total = 5 * 1024 * GIB
    set_thick_capacity(data, total, total // 2)

    result = inspect(data)

    assert result["visible_thick_lvm_image_store_count"] == 1
    assert result["declared_thick_lvm_candidate_in_visible_eligible_set"] is True
    assert result["declared_thick_lvm_required_config_fields_match"] is expected_required
    assert result["declared_thick_lvm_optional_config_fields_match"] is expected_optional
    assert result["declared_thick_lvm_status_content_matches_config"] is True
    assert result["declared_thick_lvm_candidate_exact_match"] is False
    assert all(
        result[f"declared_thick_lvm_reported_vg_headroom_passes_{size}_gib"] is False
        for size in (256, 512, 1024)
    )


def test_declared_thick_lvm_type_mismatch_uses_existing_fail_closed_path():
    data = sample()
    declare_thick_candidate(data)
    data["storage_config"]["json"]["data"]["type"] = "lvmthin"

    with pytest.raises(ValueError, match="incomplete declared storage linkage"):
        inspect(data)


def test_unsupported_declared_storage_detail_type_fails_closed():
    data = sample()
    add_candidate(data)
    data["storage_config"]["json"]["data"]["type"] = "dir"

    with pytest.raises(ValueError, match="incomplete declared storage linkage"):
        inspect(data)


def test_declared_thick_lvm_status_config_mismatch_never_sets_exact_or_headroom():
    data = sample()
    declare_thick_candidate(data)
    data["storage"]["json"]["data"][0]["content"] = "images"
    total = 5 * 1024 * GIB
    set_thick_capacity(data, total, total // 2)

    result = inspect(data)

    assert result["visible_thick_lvm_image_store_count"] == 1
    assert result["declared_thick_lvm_candidate_in_visible_eligible_set"] is True
    assert result["declared_thick_lvm_required_config_fields_match"] is True
    assert result["declared_thick_lvm_optional_config_fields_match"] is True
    assert result["declared_thick_lvm_status_content_matches_config"] is False
    assert result["declared_thick_lvm_candidate_exact_match"] is False
    assert all(
        result[f"declared_thick_lvm_reported_vg_headroom_passes_{size}_gib"] is False
        for size in (256, 512, 1024)
    )
    assert result["thick_lvm_allocation_authorized"] is False


def test_declared_thick_lvm_noneligible_shared_store_is_not_an_exact_candidate():
    data = sample()
    declare_thick_candidate(data)
    data["storage"]["json"]["data"][0]["shared"] = 1
    total = 5 * 1024 * GIB
    set_thick_capacity(data, total, total // 2)

    result = inspect(data)

    assert result["visible_thick_lvm_image_store_count"] == 0
    assert result["declared_thick_lvm_candidate_in_visible_eligible_set"] is False
    assert result["declared_thick_lvm_required_config_fields_match"] is False
    assert result["declared_thick_lvm_optional_config_fields_match"] is False
    assert result["declared_thick_lvm_status_content_matches_config"] is False
    assert result["declared_thick_lvm_candidate_exact_match"] is False
    assert all(
        result[f"declared_thick_lvm_reported_vg_headroom_passes_{size}_gib"] is False
        for size in (256, 512, 1024)
    )


def test_non_candidate_config_rows_may_omit_optional_content():
    data = sample()
    data["lvm"]["json"]["data"]["children"].append({
        "name": "secondary-vg", "size": 100 * GIB, "free": 50 * GIB, "children": [],
    })
    data["storage_config_rows"]["json"]["data"].append({
        "storage": "private-shared-lvm", "type": "lvm", "vgname": "secondary-vg",
    })
    data["storage"]["json"]["data"].append({
        "storage": "private-shared-lvm", "type": "lvm", "content": "images", "active": 1,
        "shared": 1, "total": 2000 * GIB, "used": 1200 * GIB, "avail": 800 * GIB,
    })

    result = inspect(data)

    assert result["visible_thick_lvm_image_store_count"] == 1


@pytest.mark.parametrize(
    ("pv_path", "expected", "used"),
    [
        ("/dev/private-disk-a", "thick_lvm_pv_direct_disk_path_join_count", "LVM"),
        ("/dev/private-disk-a1", "thick_lvm_pv_partition_parent_disk_path_join_count", "LVM"),
        ("/dev/private-missing", "thick_lvm_pv_missing_disk_path_join_count", None),
    ],
)
def test_candidate_pv_reports_only_exact_disk_inventory_lineage(pv_path, expected, used):
    data = sample()
    data["lvm"]["json"]["data"]["children"][0]["children"][0]["name"] = pv_path
    if pv_path in {"/dev/private-disk-a", "/dev/private-disk-a1"}:
        matching_disk = next(row for row in data["disks"]["json"]["data"] if row["devpath"] == pv_path)
        matching_disk["used"] = used

    result = inspect(data)

    assert result[expected] == 1
    assert result["thick_lvm_candidate_pv_count"] == 1
    assert result["thick_lvm_backing_media_verified"] is False
    assert result["device_safety_verified"] is False
    assert result["write_authorized"] is False


def test_partition_path_without_reported_parent_is_unverifiable():
    data = sample()
    data["lvm"]["json"]["data"]["children"][0]["children"][0]["name"] = "/dev/private-disk-a1"
    data["disks"]["json"]["data"][1]["used"] = "LVM"
    data["disks"]["json"]["data"][1]["parent"] = "/dev/private-unlisted-parent"

    result = inspect(data)

    assert result["thick_lvm_pv_partition_parent_disk_path_join_count"] == 0
    assert result["thick_lvm_pv_unverifiable_disk_path_join_count"] == 1
    assert result["thick_lvm_backing_media_verified"] is False


def test_non_lvm_reported_use_class_makes_exact_path_unverifiable():
    data = sample()
    data["lvm"]["json"]["data"]["children"][0]["children"][0]["name"] = "/dev/private-disk-a"
    data["disks"]["json"]["data"][0]["used"] = "ext4"

    result = inspect(data)

    assert result["thick_lvm_pv_direct_disk_path_join_count"] == 0
    assert result["thick_lvm_pv_unverifiable_disk_path_join_count"] == 1


@pytest.mark.parametrize("reported_used", ("lvm", " LVM ", "LVM\n"))
def test_candidate_pv_use_class_must_be_exactly_reported_as_lvm(reported_used):
    data = sample()
    data["lvm"]["json"]["data"]["children"][0]["children"][0]["name"] = "/dev/private-disk-a"
    data["disks"]["json"]["data"][0]["used"] = reported_used

    result = inspect(data)

    assert result["thick_lvm_pv_direct_disk_path_join_count"] == 0
    assert result["thick_lvm_pv_unverifiable_disk_path_join_count"] == 1
    assert result["thick_lvm_pv_path_join_unknown_used_count"] == 0


def test_reported_partitions_class_is_not_treated_as_lvm_pv_use():
    data = sample()
    data["lvm"]["json"]["data"]["children"][0]["children"][0]["name"] = "/dev/private-disk-a1"
    data["disks"]["json"]["data"][1]["used"] = "partitions"

    result = inspect(data)

    assert result["thick_lvm_pv_partition_parent_disk_path_join_count"] == 0
    assert result["thick_lvm_pv_unverifiable_disk_path_join_count"] == 1


def test_missing_used_class_preserves_unknown_while_reporting_path_join():
    data = sample()
    data["lvm"]["json"]["data"]["children"][0]["children"][0]["name"] = "/dev/private-disk-a"
    data["disks"]["json"]["data"][0].pop("used")

    result = inspect(data)

    assert result["device_usage_unknown_count"] == 2
    assert result["thick_lvm_pv_direct_disk_path_join_count"] == 1
    assert result["thick_lvm_pv_path_join_unknown_used_count"] == 1
    assert result["thick_lvm_backing_media_verified"] is False


def test_multiple_candidate_vgs_aggregate_each_unique_pv_lineage():
    data = sample()
    data["lvm"]["json"]["data"]["children"].append({
        "name": "private-vg-b", "size": 1000 * GIB, "free": 500 * GIB,
        "children": [
            {"name": "/dev/private-disk-a", "size": 600 * GIB, "free": 300 * GIB},
            {"name": "/dev/private-disk-a1", "size": 400 * GIB, "free": 200 * GIB},
        ],
    })
    data["storage_config_rows"]["json"]["data"].append({
        "storage": "private-lvm-b", "type": "lvm", "content": "images", "vgname": "private-vg-b",
    })
    data["storage"]["json"]["data"].append({
        "storage": "private-lvm-b", "type": "lvm", "content": "images", "active": 1,
        "shared": 0, "total": 1000 * GIB, "used": 500 * GIB, "avail": 500 * GIB,
    })
    data["disks"]["json"]["data"][1]["used"] = "LVM"

    result = inspect(data)

    assert result["visible_thick_lvm_image_store_count"] == 2
    assert result["thick_lvm_candidate_pv_count"] == 3
    assert result["thick_lvm_pv_direct_disk_path_join_count"] == 1
    assert result["thick_lvm_pv_partition_parent_disk_path_join_count"] == 1
    assert result["thick_lvm_pv_missing_disk_path_join_count"] == 1


def test_missing_candidate_vg_pv_list_is_incomplete_not_zero_pvs():
    data = sample()
    data["lvm"]["json"]["data"]["children"][0]["children"] = []

    result = inspect(data)

    assert result["visible_thick_lvm_image_store_count"] == 1
    assert result["thick_lvm_candidate_vg_pv_inventory_incomplete_count"] == 1
    assert result["thick_lvm_candidate_pv_count"] == 0
    assert result["thick_lvm_pv_missing_disk_path_join_count"] == 0


def test_non_path_pv_in_unrelated_vg_does_not_tighten_general_lvm_inventory():
    data = sample()
    data["lvm"]["json"]["data"]["children"].append({
        "name": "unrelated-vg", "size": 100 * GIB, "free": 50 * GIB,
        "children": [{"name": "pv-internal-name", "size": 100 * GIB, "free": 50 * GIB}],
    })

    result = inspect(data)

    assert result["lvm_volume_group_count"] == 2
    assert result["lvm_physical_volume_count"] == 2
    assert result["thick_lvm_candidate_pv_count"] == 1


def test_non_path_candidate_pv_is_unverifiable_without_failing_unrelated_inventory():
    data = sample()
    data["lvm"]["json"]["data"]["children"][0]["children"][0]["name"] = "private-pv-name"

    result = inspect(data)

    assert result["thick_lvm_candidate_pv_count"] == 1
    assert result["thick_lvm_pv_unverifiable_disk_path_join_count"] == 1


def test_duplicate_disk_paths_fail_closed():
    data = sample()
    data["disks"]["json"]["data"].append(data["disks"]["json"]["data"][0].copy())
    with pytest.raises(ValueError, match="malformed disk inventory"):
        inspect(data)


@pytest.mark.parametrize("kind", ("lvm", "lvmthin"))
def test_unmatched_lvm_status_row_suppresses_thick_lvm_counts(kind):
    data = sample()
    add_candidate(data)
    data["storage"]["json"]["data"].append({
        "storage": "private-unmatched", "type": kind, "content": "images",
        "active": 1, "shared": 0, "total": 2000 * GIB,
        "used": 1200 * GIB, "avail": 800 * GIB,
    })

    result = inspect(data)

    assert result["thick_lvm_config_vg_mappings_complete"] is False
    assert result["thick_lvm_unmatched_local_status_row_count"] == 1
    assert result["thick_lvm_unmatched_local_config_row_count"] == 0
    assert result["visible_thick_lvm_image_store_count"] == 0
    assert all(
        result[f"thick_lvm_reported_vg_headroom_candidate_count_{size}_gib"] == 0
        for size in (256, 512, 1024)
    )


def test_duplicate_candidate_vg_mapping_excludes_all_candidates():
    data = sample()
    data["storage_config_rows"]["json"]["data"].append({
        "storage": "private-lvm-copy", "type": "lvm", "content": "images", "vgname": "private-vg",
    })
    data["storage"]["json"]["data"].append({
        "storage": "private-lvm-copy", "type": "lvm", "content": "images", "active": 1,
        "shared": 0, "total": 2000 * GIB, "used": 1200 * GIB, "avail": 800 * GIB,
    })

    result = inspect(data)

    assert result["thick_lvm_config_vg_mappings_complete"] is True
    assert result["thick_lvm_visible_alias_suppressed_candidate_count"] == 2
    assert result["visible_thick_lvm_image_store_count"] == 0
    assert result["thick_lvm_reported_vg_headroom_candidate_count_256_gib"] == 0


@pytest.mark.parametrize(
    ("kind", "content", "active", "shared"),
    [
        ("lvm", "images", 0, 0),
        ("lvmthin", "images", 1, 1),
        ("lvm", "backup", 1, 0),
    ],
)
def test_any_other_lvm_config_for_the_same_vg_excludes_the_candidate(kind, content, active, shared):
    data = sample()
    data["storage_config_rows"]["json"]["data"].append({
        "storage": "private-alias", "type": kind, "vgname": "private-vg", "content": content,
    })
    data["storage"]["json"]["data"].append({
        "storage": "private-alias", "type": kind, "content": content,
        "active": active, "shared": shared,
    })

    result = inspect(data)

    assert result["visible_thick_lvm_image_store_count"] == 0
    assert result["thick_lvm_visible_alias_suppressed_candidate_count"] == 1
    assert all(result[f"thick_lvm_reported_vg_headroom_candidate_count_{size}_gib"] == 0
               for size in (256, 512, 1024))


def test_foreign_node_config_rows_do_not_join_or_alias_local_storage():
    data = sample()
    data["storage_config_rows"]["json"]["data"].append({
        "storage": "private-foreign", "type": "lvmthin", "nodes": "other-node",
        "vgname": None,
    })
    data["storage"]["json"]["data"].append({
        "storage": "private-foreign", "type": "lvmthin", "enabled": 0,
        "active": 0, "shared": 0, "content": "images",
    })

    result = inspect(data)

    assert result["thick_lvm_foreign_lvm_config_row_count"] == 1
    assert result["thick_lvm_local_config_vg_join_incomplete_count"] == 0
    assert result["thick_lvm_unmatched_local_status_row_count"] == 0
    assert result["thick_lvm_config_vg_mappings_complete"] is True
    assert result["visible_thick_lvm_image_store_count"] == 1
    assert result["thick_lvm_visible_alias_suppressed_candidate_count"] == 0
    assert "private-foreign" not in str(result)
    assert "other-node" not in str(result)


def test_foreign_disabled_status_with_type_mismatch_is_unmatched_local_status():
    data = sample()
    data["storage_config_rows"]["json"]["data"].append({
        "storage": "private-foreign", "type": "lvm", "nodes": "other-node",
        "vgname": "foreign-vg",
    })
    data["storage"]["json"]["data"].append({
        "storage": "private-foreign", "type": "lvmthin", "enabled": 0,
        "active": 0, "shared": 0, "content": "images",
    })

    result = inspect(data)

    assert result["thick_lvm_foreign_lvm_config_row_count"] == 1
    assert result["thick_lvm_unmatched_local_status_row_count"] == 1
    assert result["thick_lvm_unmatched_local_config_row_count"] == 0
    assert result["thick_lvm_config_vg_mappings_complete"] is False
    assert result["visible_thick_lvm_image_store_count"] == 0
    assert "private-foreign" not in str(result)
    assert "foreign-vg" not in str(result)


def test_foreign_scoped_status_not_marked_disabled_is_unmatched_local_status():
    data = sample()
    data["storage_config_rows"]["json"]["data"].append({
        "storage": "private-foreign", "type": "lvmthin", "nodes": "other-node",
        "vgname": "foreign-vg",
    })
    data["storage"]["json"]["data"].append({
        "storage": "private-foreign", "type": "lvmthin", "enabled": 1,
        "active": 0, "shared": 0, "content": "images",
    })

    result = inspect(data)

    assert result["thick_lvm_foreign_lvm_config_row_count"] == 1
    assert result["thick_lvm_config_vg_mappings_complete"] is False
    assert result["thick_lvm_unmatched_local_status_row_count"] == 1
    assert result["visible_thick_lvm_image_store_count"] == 0


def test_foreign_node_same_vg_alias_suppresses_local_candidate():
    data = sample()
    data["storage_config_rows"]["json"]["data"].append({
        "storage": "private-foreign-alias", "type": "lvmthin", "nodes": "other-node",
        "vgname": "private-vg",
    })

    result = inspect(data)

    assert result["thick_lvm_foreign_lvm_config_row_count"] == 1
    assert result["thick_lvm_config_vg_mappings_complete"] is True
    assert result["thick_lvm_visible_alias_suppressed_candidate_count"] == 1
    assert result["visible_thick_lvm_image_store_count"] == 0


def test_malformed_node_scope_on_unrelated_dir_does_not_refuse_survey():
    data = sample()
    data["storage_config_rows"]["json"]["data"][1]["nodes"] = None

    result = inspect(data)

    assert result["visible_thick_lvm_image_store_count"] == 1
    assert result["thick_lvm_config_vg_mappings_complete"] is True


def test_disabled_local_config_without_status_marks_mapping_incomplete():
    data = sample()
    data["storage_config_rows"]["json"]["data"].append({
        "storage": "private-disabled", "type": "lvm", "content": "images",
        "vgname": "private-vg", "disable": 1,
    })

    result = inspect(data)

    assert result["thick_lvm_config_vg_mappings_complete"] is False
    assert result["thick_lvm_unmatched_local_config_row_count"] == 1
    assert result["visible_thick_lvm_image_store_count"] == 0


def test_local_status_config_type_mismatch_marks_mapping_incomplete():
    data = sample()
    data["storage_config_rows"]["json"]["data"][0]["type"] = "lvmthin"

    result = inspect(data)

    assert result["thick_lvm_config_vg_mappings_complete"] is False
    assert result["thick_lvm_unmatched_local_status_row_count"] == 1
    assert result["thick_lvm_unmatched_local_config_row_count"] == 1
    assert result["visible_thick_lvm_image_store_count"] == 0


def test_foreign_scoped_lvm_config_with_node_status_is_unmatched_local_status():
    data = sample()
    data["storage_config_rows"]["json"]["data"][0]["nodes"] = "other-node"

    result = inspect(data)

    assert result["thick_lvm_foreign_lvm_config_row_count"] == 1
    assert result["thick_lvm_config_vg_mappings_complete"] is False
    assert result["thick_lvm_unmatched_local_status_row_count"] == 1
    assert result["thick_lvm_unmatched_local_config_row_count"] == 0
    assert result["visible_thick_lvm_image_store_count"] == 0


def test_local_node_config_alias_is_counted_and_suppresses_candidate():
    data = sample()
    data["storage_config_rows"]["json"]["data"].append({
        "storage": "private-alias", "type": "lvmthin", "nodes": "other-node,private-node",
        "vgname": "private-vg",
    })

    result = inspect(data)

    assert result["thick_lvm_foreign_lvm_config_row_count"] == 0
    assert result["thick_lvm_config_vg_mappings_complete"] is False
    assert result["thick_lvm_unmatched_local_config_row_count"] == 1
    assert result["thick_lvm_visible_alias_suppressed_candidate_count"] == 1
    assert result["visible_thick_lvm_image_store_count"] == 0


def test_local_config_without_node_status_is_reported_and_suppresses_counts():
    data = sample()
    data["storage_config_rows"]["json"]["data"].append({
        "storage": "private-unmatched", "type": "lvmthin", "nodes": "private-node",
        "vgname": "private-vg",
    })

    result = inspect(data)

    assert result["thick_lvm_config_vg_mappings_complete"] is False
    assert result["thick_lvm_unmatched_local_config_row_count"] == 1
    assert result["visible_thick_lvm_image_store_count"] == 0


@pytest.mark.parametrize(
    "nodes", (None, "", "other-node,", "private node", "private-node,private-node", ".")
)
def test_malformed_api_encoded_node_restrictions_fail_closed(nodes):
    data = sample()
    data["storage_config_rows"]["json"]["data"][0]["nodes"] = nodes

    with pytest.raises(ValueError, match="malformed cluster storage node scope"):
        inspect(data)


@pytest.mark.parametrize("mutation", ("invalid_id", "duplicate_id", "invalid_type"))
def test_malformed_or_duplicate_config_rows_fail_closed(mutation):
    data = sample()
    row = {"storage": "private-extra", "type": "dir"}
    if mutation == "invalid_id":
        row["storage"] = "private/extra"
    elif mutation == "invalid_type":
        row["type"] = None
    data["storage_config_rows"]["json"]["data"].append(row)
    if mutation == "duplicate_id":
        data["storage_config_rows"]["json"]["data"].append(row.copy())

    with pytest.raises(ValueError, match="malformed cluster storage config inventory"):
        inspect(data)


def test_missing_candidate_content_fails_closed():
    data = sample()
    data["storage_config_rows"]["json"]["data"][0].pop("content")

    with pytest.raises(ValueError, match="incomplete thick-LVM storage linkage"):
        inspect(data)


@pytest.mark.parametrize("mapping", ("candidate_missing", "candidate_unmapped", "unrelated_missing"))
def test_incomplete_vg_mappings_suppress_all_candidate_counts(mapping):
    data = sample()
    if mapping == "candidate_missing":
        data["storage_config_rows"]["json"]["data"][0].pop("vgname")
    elif mapping == "candidate_unmapped":
        data["storage_config_rows"]["json"]["data"][0]["vgname"] = "unmapped-vg"
    else:
        data["storage_config_rows"]["json"]["data"].append({
            "storage": "private-unmapped-thin", "type": "lvmthin",
        })

    result = inspect(data)

    assert result["thick_lvm_config_vg_mappings_complete"] is False
    assert result["thick_lvm_local_config_vg_join_incomplete_count"] == 1
    assert result["visible_thick_lvm_image_store_count"] == 0


def test_disabled_thick_lvm_config_is_not_counted_even_if_node_status_is_active():
    data = sample()
    data["storage_config_rows"]["json"]["data"][0]["disable"] = 1

    result = inspect(data)

    assert result["visible_thick_lvm_image_store_count"] == 0
    assert all(result[f"thick_lvm_reported_vg_headroom_candidate_count_{size}_gib"] == 0
               for size in (256, 512, 1024))


def test_inconsistent_thick_lvm_status_and_vg_capacity_fails_closed():
    data = sample()
    data["lvm"]["json"]["data"]["children"][0]["free"] -= 1

    with pytest.raises(ValueError, match="inconsistent thick-LVM capacity"):
        inspect(data)


@pytest.mark.parametrize("content", ("images,,backup", "images,images", "images, backup"))
def test_malformed_thick_lvm_candidate_content_fails_closed(content):
    data = sample()
    data["storage_config_rows"]["json"]["data"][0]["content"] = content

    with pytest.raises(ValueError, match="malformed cluster storage config inventory"):
        inspect(data)


def test_declared_active_local_lvmthin_images_store_reports_only_fixed_size_bools():
    data = sample()
    add_candidate(data)

    result = inspect(data)

    assert result["declared_storage_row_found"] is True
    assert result["declared_storage_row_eligible"] is True
    assert result["declared_thinpool_linked"] is True
    assert result["visible_volume_permissions_verified"] is True
    assert result["visible_volume_inventory_well_formed"] is True
    assert result["snapshot_inventory_complete_verified"] is False
    assert result["declared_metadata_headroom_30_percent"] is True
    assert result["snapshot_unverified_visible_volume_preflight_passes_256_gib_disk"] is True
    assert result["snapshot_unverified_visible_volume_preflight_passes_512_gib_disk"] is True
    assert result["snapshot_unverified_visible_volume_preflight_passes_1024_gib_disk"] is True
    assert result["storage_allocation_authorized"] is False
    assert result["declared_thick_lvm_candidate_exact_match"] is False
    assert result["declared_thick_lvm_reported_vg_headroom_passes_256_gib"] is False
    assert result["declared_thick_lvm_reported_vg_headroom_passes_512_gib"] is False
    assert result["declared_thick_lvm_reported_vg_headroom_passes_1024_gib"] is False
    rendered = json.dumps(result)
    assert "private-candidate" not in rendered
    assert str(8192 * GIB) not in rendered
    assert set(result).issuperset({
        "snapshot_unverified_visible_volume_preflight_passes_256_gib_disk",
        "snapshot_unverified_visible_volume_preflight_passes_512_gib_disk",
        "snapshot_unverified_visible_volume_preflight_passes_1024_gib_disk",
    })


@pytest.mark.parametrize("size_gib", (256, 512, 1024))
def test_virtual_disk_capacity_requires_size_plus_30_percent_remaining(size_gib):
    size = size_gib * GIB
    total = size * 5
    threshold_available = size + total * 3 // 10

    data = sample()
    add_candidate(data, candidate_row(
        total=total, used=total - threshold_available, available=threshold_available,
    ))
    assert inspect(data)[f"snapshot_unverified_visible_volume_preflight_passes_{size_gib}_gib_disk"] is True

    data["storage"]["json"]["data"][-1]["used"] += 1
    data["storage"]["json"]["data"][-1]["avail"] -= 1
    data["thinpool"]["json"]["data"][-1]["used"] += 1
    assert inspect(data)[f"snapshot_unverified_visible_volume_preflight_passes_{size_gib}_gib_disk"] is False


@pytest.mark.parametrize(
    "permissions",
    [
        {},
        {"Datastore.Audit": 1},
        {"Datastore.Allocate": 1},
        {"Datastore.Allocate": 1, "Datastore.Audit": "yes"},
    ],
)
def test_partial_image_listing_rights_never_produce_capacity_pass(permissions):
    data = sample()
    add_candidate(data, volumes=(("vm-123-disk-0", "images", 64 * GIB),))
    data["storage_permissions"] = api({"/storage/private-candidate": permissions})

    result = inspect(data)

    assert result["visible_volume_permissions_verified"] is False
    assert result["visible_volume_inventory_well_formed"] is False
    assert all(result[f"snapshot_unverified_visible_volume_preflight_passes_{size}_gib_disk"] is False
               for size in (256, 512, 1024))


def test_non_propagated_rights_still_apply_at_the_exact_storage_path():
    data = sample()
    add_candidate(data)
    data["storage_permissions"] = api({"/storage/private-candidate": {
        "Datastore.Allocate": 0, "Datastore.Audit": 0,
    }})

    result = inspect(data)

    assert result["visible_volume_permissions_verified"] is True
    assert result["snapshot_unverified_visible_volume_preflight_passes_256_gib_disk"] is True


def test_storage_permissions_for_another_path_do_not_prove_complete_visibility():
    data = sample()
    add_candidate(data)
    data["storage_permissions"] = api({"/storage/different": {
        "Datastore.Allocate": 1, "Datastore.Audit": 1,
    }})

    result = inspect(data)

    assert result["visible_volume_permissions_verified"] is False
    assert result["snapshot_unverified_visible_volume_preflight_passes_256_gib_disk"] is False


@pytest.mark.parametrize(
    "row",
    [
        candidate_row(active=0),
        candidate_row(shared=1),
        candidate_row(content="backup"),
        candidate_row(type="dir"),
        candidate_row(total=8192 * GIB, used=7168 * GIB, available=1024 * GIB),
    ],
)
def test_ineligible_or_incomplete_declared_storage_fails_capacity_checks_closed(row):
    data = sample()
    add_candidate(data, row)

    result = inspect(data)

    assert all(result[key] is False for key in (
        "snapshot_unverified_visible_volume_preflight_passes_256_gib_disk",
        "snapshot_unverified_visible_volume_preflight_passes_512_gib_disk",
        "snapshot_unverified_visible_volume_preflight_passes_1024_gib_disk",
    ))


@pytest.mark.parametrize("missing_field", ("active", "shared"))
def test_missing_candidate_eligibility_field_fails_capacity_checks_closed(missing_field):
    data = sample()
    row = candidate_row()
    row.pop(missing_field)
    add_candidate(data, row)

    result = inspect(data)

    assert all(result[key] is False for key in (
        "snapshot_unverified_visible_volume_preflight_passes_256_gib_disk",
        "snapshot_unverified_visible_volume_preflight_passes_512_gib_disk",
        "snapshot_unverified_visible_volume_preflight_passes_1024_gib_disk",
    ))


def test_images_only_store_is_visible_but_ineligible_for_visible_volume_preflight():
    data = sample()
    row = candidate_row(content="images")
    add_candidate(data, row)

    result = inspect(data)

    assert result["declared_storage_row_found"] is True
    assert result["declared_storage_row_eligible"] is False
    assert all(result[key] is False for key in (
        "snapshot_unverified_visible_volume_preflight_passes_256_gib_disk",
        "snapshot_unverified_visible_volume_preflight_passes_512_gib_disk",
        "snapshot_unverified_visible_volume_preflight_passes_1024_gib_disk",
    ))


@pytest.mark.parametrize("missing_field", ("total", "used", "avail"))
def test_missing_candidate_capacity_field_refuses_preflight(missing_field):
    data = sample()
    row = candidate_row()
    row.pop(missing_field)
    data["storage"]["json"]["data"].append(row)
    data["thinpool"]["json"]["data"].append({
        "lv": "private-pool", "vg": "private-vg", "lv_size": 8192 * GIB,
        "used": 1024 * GIB, "metadata_size": 8 * GIB, "metadata_used": 2 * GIB,
    })

    with pytest.raises(ValueError, match="inconsistent declared storage capacity"):
        inspect(data)


def test_differently_named_eligible_storage_does_not_match_private_declaration():
    data = sample()
    add_candidate(data, candidate_row(storage="different-name"))

    result = inspect(data)

    assert result["declared_storage_row_found"] is False
    assert result["declared_storage_row_eligible"] is False
    assert result["snapshot_unverified_visible_volume_preflight_passes_256_gib_disk"] is False
    assert result["snapshot_unverified_visible_volume_preflight_passes_512_gib_disk"] is False
    assert result["snapshot_unverified_visible_volume_preflight_passes_1024_gib_disk"] is False


def test_virtual_overcommit_blocks_preflight_even_when_written_space_is_free():
    data = sample()
    add_candidate(data, volumes=(("vm-123-disk-0", "images", 7500 * GIB),))

    result = inspect(data)

    assert result["visible_volume_inventory_well_formed"] is True
    assert result["snapshot_inventory_complete_verified"] is False
    assert result["snapshot_unverified_visible_volume_preflight_passes_256_gib_disk"] is False
    assert result["snapshot_unverified_visible_volume_preflight_passes_512_gib_disk"] is False
    assert result["snapshot_unverified_visible_volume_preflight_passes_1024_gib_disk"] is False


def test_unfiltered_inventory_sums_both_image_and_rootdir_volume_rows():
    data = sample()
    add_candidate(data, volumes=(
        ("vm-123-disk-0", "images", 64 * GIB),
        ("vm-456-disk-0", "rootdir", 32 * GIB),
    ))

    result = inspect(data)

    assert result["visible_volume_inventory_well_formed"] is True
    assert result["snapshot_inventory_complete_verified"] is False
    assert result["snapshot_unverified_visible_volume_preflight_passes_256_gib_disk"] is True
    assert result["storage_allocation_authorized"] is False


@pytest.mark.parametrize(
    ("volume_name", "content"),
    [
        ("snap_vm-123-disk-0_daily", "images"),
        ("vm-0-disk-0", "images"),
        ("vm-123-disk-0", "backup"),
        ("vm-123-", "images"),
        ("vm-123-disk-0:extra", "images"),
    ],
)
def test_visible_volume_identifiers_and_content_are_strictly_parsed(volume_name, content):
    data = sample()
    add_candidate(data, volumes=((volume_name, content, GIB),))

    with pytest.raises(ValueError, match="malformed visible volume inventory"):
        inspect(data)


def test_low_current_thinpool_metadata_headroom_blocks_preflight():
    data = sample()
    add_candidate(data, metadata_used=6 * GIB)

    result = inspect(data)

    assert result["declared_metadata_headroom_30_percent"] is False
    assert result["snapshot_unverified_visible_volume_preflight_passes_256_gib_disk"] is False


def test_storage_pool_linkage_must_match_and_pool_status_capacity_must_agree():
    data = sample()
    add_candidate(data)
    data["storage_config"]["json"]["data"]["thinpool"] = "different-pool"
    with pytest.raises(ValueError, match="incomplete declared storage linkage"):
        inspect(data)

    data = sample()
    add_candidate(data)
    data["thinpool"]["json"]["data"][-1]["lv_size"] -= GIB
    with pytest.raises(ValueError, match="inconsistent declared storage capacity"):
        inspect(data)

    data = sample()
    add_candidate(data)
    data["thinpool"]["json"]["data"][-1]["used"] += 1
    with pytest.raises(ValueError, match="inconsistent declared storage capacity"):
        inspect(data)


def test_image_listing_must_be_unique_complete_and_storage_scoped():
    data = sample()
    add_candidate(data, volumes=(
        ("vm-123-disk-0", "images", GIB), ("vm-123-disk-0", "images", GIB),
    ))
    with pytest.raises(ValueError, match="malformed visible volume inventory"):
        inspect(data)

    data = sample()
    add_candidate(data)
    data["visible_volumes"]["json"]["data"].append({
        "volid": "different-storage:vm-123-disk-0", "content": "images", "format": "raw", "size": GIB,
    })
    with pytest.raises(ValueError, match="malformed visible volume inventory"):
        inspect(data)


def test_declared_storage_identifier_is_required_safe_and_unique():
    for invalid in (None, "", "../private", "storage/id", "contains spaces"):
        data = sample()
        data["declared_storage_id"] = invalid
        with pytest.raises(ValueError, match="private VM image-storage declaration"):
            inspect(data)

    data = sample()
    data["storage"]["json"]["data"].extend([candidate_row(), candidate_row()])
    with pytest.raises(ValueError, match="malformed storage status inventory"):
        inspect(data)


@pytest.mark.parametrize(
    "overrides",
    [
        {"active": 2},
        {"shared": "0"},
        {"content": None},
        {"type": None},
        {"total": "8192 GiB"},
    ],
)
def test_malformed_declared_storage_row_is_refused(overrides):
    data = sample()
    data["storage"]["json"]["data"].append(candidate_row(**overrides))

    with pytest.raises(ValueError, match="malformed storage status inventory"):
        inspect(data)


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


def test_cli_success_prints_no_candidate_id_or_exact_capacity(monkeypatch, capsys):
    data = selectable_thick_candidate()
    data["disks"]["json"]["data"][0].update(serial="SERIAL-123", wwn="0x5000abcd")
    monkeypatch.setattr(sys, "argv", ["inspect_o11y_pbs_physical_storage.py", "inspect"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(data)))

    assert main() == 0
    captured = capsys.readouterr()
    assert "private-candidate" not in captured.out
    assert "private-lvm" not in captured.out
    assert "private-vg" not in captured.out
    assert "SERIAL-123" not in captured.out
    assert "0x5000abcd" not in captured.out
    assert "/dev/" not in captured.out
    assert str(8192 * GIB) not in captured.out
    assert captured.err == ""


def test_candidate_selector_returns_only_the_unique_storage_id():
    result = select_thick_lvm_candidate(selectable_thick_candidate())

    assert result == {"candidate_storage_id": "private-lvm"}


def test_candidate_selector_cli_emits_only_the_storage_id(monkeypatch, capsys):
    data = selectable_thick_candidate()
    monkeypatch.setattr(
        sys, "argv", ["inspect_o11y_pbs_physical_storage.py", "select-thick-lvm-candidate"]
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(data)))

    assert main() == 0
    captured = capsys.readouterr()
    assert captured.out == '{"candidate_storage_id":"private-lvm"}\n'
    assert "/dev/" not in captured.out
    assert "private-node" not in captured.out
    assert "private-vg" not in captured.out
    assert str(5 * 1024 * GIB) not in captured.out
    assert captured.err == ""


def test_candidate_selector_refuses_two_eligible_stores():
    data = selectable_thick_candidate()
    total = 5 * 1024 * GIB
    data["storage_config_rows"]["json"]["data"].append({
        "storage": "private-lvm-two", "type": "lvm", "content": "images,backup",
        "vgname": "private-vg-two",
    })
    data["storage"]["json"]["data"].append({
        "storage": "private-lvm-two", "type": "lvm", "content": "images,backup",
        "active": 1, "shared": 0, "total": total, "used": total // 2,
        "avail": total // 2,
    })
    data["lvm"]["json"]["data"]["children"].append({
        "name": "private-vg-two", "size": total, "free": total // 2,
        "children": [{"name": "/dev/private-disk-c", "size": total, "free": total // 2}],
    })
    data["disks"]["json"]["data"].append({
        "devpath": "/dev/private-disk-c", "size": total, "used": "LVM",
    })

    with pytest.raises(ValueError, match="No unique thick-LVM storage candidate"):
        select_thick_lvm_candidate(data)


def test_candidate_selector_refuses_insufficient_reported_headroom():
    data = selectable_thick_candidate()
    set_thick_capacity(data, 5 * 1024 * GIB, 2 * 1024 * GIB)

    with pytest.raises(ValueError, match="No unique thick-LVM storage candidate"):
        select_thick_lvm_candidate(data)


def test_candidate_selector_refuses_incomplete_pv_lineage():
    data = selectable_thick_candidate()
    data["lvm"]["json"]["data"]["children"][0]["children"] = []

    with pytest.raises(ValueError, match="No unique thick-LVM storage candidate"):
        select_thick_lvm_candidate(data)


def test_declared_backing_identity_requires_one_direct_lvm_pv_and_reports_optional_wwn():
    data = selectable_thick_candidate()
    disk = data["disks"]["json"]["data"][0]
    disk.update(serial="SERIAL-123", wwn="0x5000abcd", health="PASSED")

    result = identify_thick_lvm_backing_identity(data)

    assert result == {
        "candidate_storage_id": "private-lvm",
        "backing_disk_serial": "SERIAL-123",
        "backing_disk_wwn": "0x5000abcd",
        "backing_disk_health": "passed",
    }
    disk.pop("wwn")
    assert identify_thick_lvm_backing_identity(data)["backing_disk_wwn"] is None
    sanitized = inspect(data)
    assert sanitized["thick_lvm_backing_media_verified"] is False
    assert sanitized["thick_lvm_allocation_authorized"] is False
    assert sanitized["device_safety_verified"] is False
    assert sanitized["write_authorized"] is False
    assert "SERIAL-123" not in json.dumps(sanitized)
    assert "0x5000abcd" not in json.dumps(sanitized)
    assert "backing_disk_health" not in json.dumps(sanitized)


def test_declared_backing_identity_accepts_direct_physical_disk_path():
    data = selectable_thick_candidate()
    disk = data["disks"]["json"]["data"][0]
    disk.update(devpath="/dev/sda", serial="SERIAL-123", model="ST8000NM", vendor="ATA", health="PASSED")
    data["lvm"]["json"]["data"]["children"][0]["children"][0]["name"] = "/dev/sda"

    identity = identify_thick_lvm_backing_identity(data)
    assert identity["backing_disk_serial"] == "SERIAL-123"
    assert identity["backing_disk_health"] == "passed"


def test_declared_backing_identity_classifies_sas_health_status_ok_as_passed():
    data = selectable_thick_candidate()
    data["disks"]["json"]["data"][0].update(serial="SERIAL-123", health="OK")

    assert identify_thick_lvm_backing_identity(data)["backing_disk_health"] == "passed"


@pytest.mark.parametrize(("health", "expected"), (("FAILED", "failed"), ("FAILED!", "failed")))
def test_declared_backing_identity_classifies_reported_smart_failure(health, expected):
    data = selectable_thick_candidate()
    data["disks"]["json"]["data"][0].update(serial="SERIAL-123", health=health)

    assert identify_thick_lvm_backing_identity(data)["backing_disk_health"] == expected


@pytest.mark.parametrize(
    "health",
    (
        None, "UNKNOWN", "SMART Disabled", "unsupported status", "passed",
        " PASSED", "PASSED ", 1, [], {"state": "PASSED"},
    ),
)
def test_declared_backing_identity_keeps_absent_unrecognized_or_invalid_smart_health_unknown(health):
    data = selectable_thick_candidate()
    disk = data["disks"]["json"]["data"][0]
    disk["serial"] = "SERIAL-123"
    if health is None:
        disk.pop("health", None)
    else:
        disk["health"] = health

    assert identify_thick_lvm_backing_identity(data)["backing_disk_health"] == "unknown"


@pytest.mark.parametrize(
    ("devpath", "model", "vendor"),
    (
        ("/dev/vda", None, None),
        ("/dev/private-disk-a", "QEMU HARDDISK", None),
        ("/dev/private-disk-a", "VIRTIO Block Device", None),
        ("/dev/private-disk-a", None, "VMware, Inc."),
        ("/dev/private-disk-a", 123, None),
        ("/dev/private-disk-a", None, ["vendor"]),
    ),
)
def test_declared_backing_identity_refuses_virtual_or_malformed_disk_classification(devpath, model, vendor):
    data = selectable_thick_candidate()
    disk = data["disks"]["json"]["data"][0]
    disk.update(serial="SERIAL-123", model=model, vendor=vendor, type="unknown")
    disk["devpath"] = devpath
    data["lvm"]["json"]["data"]["children"][0]["children"][0]["name"] = devpath

    with pytest.raises(ValueError, match="No unique thick-LVM storage candidate"):
        identify_thick_lvm_backing_identity(data)


@pytest.mark.parametrize("serial", (None, "", "   ", "bad\nserial", 123))
def test_declared_backing_identity_refuses_missing_blank_or_malformed_serial(serial):
    data = selectable_thick_candidate()
    disk = data["disks"]["json"]["data"][0]
    if serial is None:
        disk.pop("serial", None)
    else:
        disk["serial"] = serial

    with pytest.raises(ValueError, match="No unique thick-LVM storage candidate"):
        identify_thick_lvm_backing_identity(data)


def test_declared_backing_identity_refuses_duplicate_serial():
    data = selectable_thick_candidate()
    data["disks"]["json"]["data"][0]["serial"] = "SERIAL-123"
    data["disks"]["json"]["data"].append({
        "devpath": "/dev/private-other", "size": GIB, "serial": "serial-123",
    })

    with pytest.raises(ValueError, match="No unique thick-LVM storage candidate"):
        identify_thick_lvm_backing_identity(data)


def test_declared_backing_identity_refuses_malformed_or_duplicate_wwn():
    data = selectable_thick_candidate()
    data["disks"]["json"]["data"][0].update(serial="SERIAL-123", wwn="bad\nwwn")
    with pytest.raises(ValueError, match="No unique thick-LVM storage candidate"):
        identify_thick_lvm_backing_identity(data)

    data["disks"]["json"]["data"][0]["wwn"] = "0x5000abcd"
    data["disks"]["json"]["data"].append({
        "devpath": "/dev/private-other", "size": GIB, "wwn": "0X5000ABCD",
    })
    with pytest.raises(ValueError, match="No unique thick-LVM storage candidate"):
        identify_thick_lvm_backing_identity(data)


@pytest.mark.parametrize("disk_change", ("partition", "unknown_use"))
def test_declared_backing_identity_refuses_partition_or_unknown_disk_use(disk_change):
    data = selectable_thick_candidate()
    data["disks"]["json"]["data"][0]["serial"] = "SERIAL-123"
    if disk_change == "partition":
        data["lvm"]["json"]["data"]["children"][0]["children"][0]["name"] = "/dev/private-disk-a1"
    else:
        data["disks"]["json"]["data"][0].pop("used")

    with pytest.raises(ValueError, match="No unique thick-LVM storage candidate"):
        identify_thick_lvm_backing_identity(data)


@pytest.mark.parametrize("invalid_proof", ("config_mismatch", "headroom", "multiple_pvs"))
def test_declared_backing_identity_refuses_inexact_or_nonunique_proof(invalid_proof):
    data = selectable_thick_candidate()
    data["disks"]["json"]["data"][0]["serial"] = "SERIAL-123"
    if invalid_proof == "config_mismatch":
        data["storage_config"]["json"]["data"]["content"] = "images"
    elif invalid_proof == "headroom":
        set_thick_capacity(data, 5 * 1024 * GIB, 2 * 1024 * GIB)
    else:
        data["lvm"]["json"]["data"]["children"][0]["children"].append({
            "name": "/dev/private-disk-b", "size": GIB, "free": 0,
        })

    with pytest.raises(ValueError, match="No unique thick-LVM storage candidate"):
        identify_thick_lvm_backing_identity(data)


def test_declared_backing_identity_cli_reports_only_the_validated_private_shape(monkeypatch, capsys):
    data = selectable_thick_candidate()
    data["disks"]["json"]["data"][0].update(serial="SERIAL-123", wwn="0x5000abcd", health="PASSED")
    monkeypatch.setattr(
        sys, "argv", ["inspect_o11y_pbs_physical_storage.py", "identify-thick-lvm-backing-identity"]
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(data)))

    assert main() == 0
    captured = capsys.readouterr()
    assert captured.out == (
        '{"candidate_storage_id":"private-lvm","backing_disk_serial":"SERIAL-123",'
        '"backing_disk_wwn":"0x5000abcd","backing_disk_health":"passed"}\n'
    )
    assert "/dev/" not in captured.out
    assert "private-vg" not in captured.out
    assert captured.err == ""


def test_declared_backing_identity_cli_refusal_is_fixed_and_sanitized(monkeypatch, capsys):
    data = selectable_thick_candidate()
    data["disks"]["json"]["data"][0].update(serial="private-serial", wwn="private-wwn")
    data["disks"]["json"]["data"][0].pop("serial")
    monkeypatch.setattr(
        sys, "argv", ["inspect_o11y_pbs_physical_storage.py", "identify-thick-lvm-backing-identity"]
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(data)))

    assert main() == 2
    captured = capsys.readouterr()
    assert captured.out == (
        '{"refusal":"No unique thick-LVM storage candidate with complete visible mapping '
        'and lineage was identified."}\n'
    )
    assert "private-serial" not in captured.out
    assert "private-wwn" not in captured.out
    assert "/dev/" not in captured.out
    assert captured.err == ""


def test_declared_backing_identity_cli_emits_only_fixed_health_category(monkeypatch, capsys):
    data = selectable_thick_candidate()
    data["disks"]["json"]["data"][0].update(
        serial="private-serial", health="private-health-detail PASSED"
    )
    monkeypatch.setattr(
        sys, "argv", ["inspect_o11y_pbs_physical_storage.py", "identify-thick-lvm-backing-identity"]
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(data)))

    assert main() == 0
    captured = capsys.readouterr()
    assert '"backing_disk_health":"unknown"' in captured.out
    assert "private-health-detail" not in captured.out
    assert captured.err == ""


def test_declared_backing_identity_cli_refuses_virtual_disk_without_disclosing_metadata(monkeypatch, capsys):
    data = selectable_thick_candidate()
    disk = data["disks"]["json"]["data"][0]
    disk.update(
        devpath="/dev/vda", serial="private-serial", wwn="private-wwn", model="QEMU HARDDISK"
    )
    data["lvm"]["json"]["data"]["children"][0]["children"][0]["name"] = "/dev/vda"
    monkeypatch.setattr(
        sys, "argv", ["inspect_o11y_pbs_physical_storage.py", "identify-thick-lvm-backing-identity"]
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(data)))

    assert main() == 2
    captured = capsys.readouterr()
    assert captured.out == (
        '{"refusal":"No unique thick-LVM storage candidate with complete visible mapping '
        'and lineage was identified."}\n'
    )
    for private_value in ("/dev/vda", "private-serial", "private-wwn", "QEMU HARDDISK"):
        assert private_value not in captured.out
    assert captured.err == ""


def test_candidate_selector_cli_refusal_hides_malformed_private_data(monkeypatch, capsys):
    data = selectable_thick_candidate()
    data["storage_config_rows"]["status"] = 403
    monkeypatch.setattr(
        sys, "argv", ["inspect_o11y_pbs_physical_storage.py", "select-thick-lvm-candidate"]
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(data)))

    assert main() == 2
    captured = capsys.readouterr()
    assert captured.out == (
        '{"refusal":"No unique thick-LVM storage candidate with complete visible mapping '
        'and lineage was identified."}\n'
    )
    assert "private-" not in captured.out
    assert "/dev/" not in captured.out
    assert captured.err == ""


def test_cli_refusal_for_bad_node_scope_is_fixed_and_sanitized(monkeypatch, capsys):
    data = sample()
    data["storage_config_rows"]["json"]["data"][0]["nodes"] = "private-node,../secret"
    monkeypatch.setattr(sys, "argv", ["inspect_o11y_pbs_physical_storage.py", "inspect"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(data)))

    assert main() == 2
    captured = capsys.readouterr()
    assert captured.out == json.dumps({
        "refusal": "Proxmox returned a malformed cluster storage node scope."
    }, separators=(",", ":")) + "\n"
    assert "private-node" not in captured.out
    assert "../secret" not in captured.out
    assert captured.err == ""


@pytest.mark.parametrize(
    ("section", "expected_refusal"),
    [
        ("disks", "incomplete disk inventory"),
        ("lvm", "incomplete LVM inventory"),
        ("thinpool", "incomplete thin-pool inventory"),
        ("directories", "incomplete directory inventory"),
        ("storage", "incomplete storage status inventory"),
        ("storage_config_rows", "incomplete cluster storage config inventory"),
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
    assert len(api_reads) == 10
    assert all(task["ansible.builtin.uri"]["method"] == "GET" for task in api_reads)
    assert all(task.get("no_log") is True and task.get("check_mode") is False for task in api_reads)
    assert all("Authorization" in task["ansible.builtin.uri"]["headers"] for task in api_reads)
    assert any(task["ansible.builtin.uri"]["url"].endswith("/api2/json/nodes") for task in api_reads)
    assert any(task["ansible.builtin.uri"]["url"].endswith("/api2/json/storage") for task in api_reads)
    for endpoint in (
        "disks/list?include-partitions=1", "disks/lvm", "disks/lvmthin", "disks/directory",
        "/storage", "/api2/json/storage/", "/access/permissions?path=", "/content",
    ):
        assert any(endpoint in task["ansible.builtin.uri"]["url"] for task in api_reads)
    assert not any("content?content=" in task["ansible.builtin.uri"]["url"] for task in api_reads)
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
    assert "proxmox_pbs_vm_storage_id" in str(play.get("vars"))
    assert "^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$" in str(tasks[0])
    inspection = next(
        task for task in tasks
        if task.get("name") == "Build fixed aggregate physical and virtual-disk capacity receipt"
    )
    assert inspection["ansible.builtin.command"]["stdin"] == "{{ _physical_storage_payload }}"
    assert inspection.get("no_log") is True
    payload = next(
        task for task in tasks
        if task.get("name") == "Assemble protected physical-storage inspection payload"
    )
    assert "declared_storage_id" in str(payload)
    assert "target_node" in str(payload)
    assert "_validated_target_node" in str(payload)
    assert payload.get("no_log") is True
    assert all("{{ _target_node" not in str(task) for task in api_reads)


def test_semaphore_template_requires_exact_sha_and_has_no_node_input():
    templates = yaml.safe_load((ROOT / "platform/semaphore/templates.yml").read_text())["templates"]
    template = next(item for item in templates if item.get("name") == "Survey o11y PBS Physical Storage (Dev)")
    assert template["playbook"] == "platform/playbooks/survey-o11y-pbs-physical-storage.yml"
    assert template["repository"] == "agent-cloud dev"
    vars_by_name = {item["name"]: item for item in template["survey_vars"]}
    assert vars_by_name["expected_repository_sha"]["required"] is True
    assert len(vars_by_name) == 1


def test_candidate_workflow_still_selects_candidate_and_reports_only_storage_id():
    plays = yaml.safe_load((ROOT / "platform/playbooks/survey-o11y-pbs-storage-candidate.yml").read_text())
    assert plays[0]["ansible.builtin.import_playbook"] == "refuse-internal-extra-vars.yml"
    survey_import = plays[1]
    assert survey_import["ansible.builtin.import_playbook"] == "survey-o11y-pbs-physical-storage.yml"
    assert survey_import["vars"]["_extra_var_guard_nested"] is True
    play = plays[2]
    assert play["hosts"] == "localhost"
    assert not any("ansible.builtin.uri" in task for task in play["tasks"])
    selector = next(
        task for task in play["tasks"]
        if task.get("name") == "Select candidate from the protected survey responses"
    )
    command = selector["ansible.builtin.command"]
    assert command["argv"][-1] == "select-thick-lvm-candidate"
    assert command["stdin"] == "{{ _physical_storage_payload }}"
    assert selector["no_log"] is True
    assert selector["changed_when"] is False
    assert selector["check_mode"] is False
    assert selector["failed_when"] is False
    refusal = next(
        task for task in play["tasks"]
        if task.get("name", "").startswith("Require one complete candidate")
    )
    assert refusal["ansible.builtin.assert"]["that"] == "_thick_lvm_candidate_selection.rc == 0"
    assert "{{" not in refusal["ansible.builtin.assert"]["fail_msg"]
    report = next(
        task for task in play["tasks"]
        if task.get("name", "").startswith("Report the candidate storage ID")
    )
    report_message = report["ansible.builtin.debug"]["msg"]
    assert "candidate_storage_id" in report_message
    assert "serial" not in report_message.lower()
    assert "wwn" not in report_message.lower()
    assert "node" not in report_message.lower()


def test_backing_identity_workflow_reuses_get_only_survey_and_protects_output():
    plays = yaml.safe_load((ROOT / "platform/playbooks/identify-o11y-pbs-backing-identity.yml").read_text())
    assert plays[0]["ansible.builtin.import_playbook"] == "refuse-internal-extra-vars.yml"
    survey_import = plays[1]
    assert survey_import["ansible.builtin.import_playbook"] == "survey-o11y-pbs-physical-storage.yml"
    assert survey_import["vars"]["_extra_var_guard_nested"] is True
    play = plays[2]
    assert play["hosts"] == "localhost"
    assert not any("ansible.builtin.uri" in task for task in play["tasks"])
    selector = next(
        task for task in play["tasks"]
        if task.get("name") == "Identify exact declared candidate backing identity"
    )
    command = selector["ansible.builtin.command"]
    assert command["argv"][-1] == "identify-thick-lvm-backing-identity"
    assert command["stdin"] == "{{ _physical_storage_payload }}"
    assert selector["no_log"] is True
    assert selector["changed_when"] is False
    assert selector["check_mode"] is False
    assert selector["failed_when"] is False
    report = next(
        task for task in play["tasks"]
        if task.get("name", "").startswith("Report candidate storage and backing-disk identity")
    )
    assert "backing_disk_serial" in report["ansible.builtin.debug"]["msg"]
    assert "not reported" in report["ansible.builtin.debug"]["msg"]
    shape = next(task for task in play["tasks"] if task.get("name") == "Validate the candidate identity output shape")
    assert shape.get("no_log") is True
    assert "backing_disk_wwn" in str(shape)
    assert "backing_disk_health" in str(shape)


def test_candidate_semaphore_template_is_dev_bound_and_accepts_only_reviewed_sha():
    templates = yaml.safe_load((ROOT / "platform/semaphore/templates.yml").read_text())["templates"]
    template = next(
        item for item in templates
        if item.get("name") == "Identify o11y PBS Storage Candidate (Dev)"
    )
    assert template["playbook"] == "platform/playbooks/survey-o11y-pbs-storage-candidate.yml"
    assert template["repository"] == "agent-cloud dev"
    vars_by_name = {item["name"]: item for item in template["survey_vars"]}
    assert vars_by_name["expected_repository_sha"]["required"] is True
    assert len(vars_by_name) == 1


def test_backing_identity_template_is_separate_dev_bound_and_accepts_only_reviewed_sha():
    templates = yaml.safe_load((ROOT / "platform/semaphore/templates.yml").read_text())["templates"]
    template = next(item for item in templates if item.get("name") == "Identify o11y PBS Backing Disk Identity (Dev)")
    assert template["playbook"] == "platform/playbooks/identify-o11y-pbs-backing-identity.yml"
    assert template["repository"] == "agent-cloud dev"
    vars_by_name = {item["name"]: item for item in template["survey_vars"]}
    assert vars_by_name["expected_repository_sha"]["required"] is True
    assert len(vars_by_name) == 1
