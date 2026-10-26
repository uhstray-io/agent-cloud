"""Fail-closed identity, isolation, artifact-layout, and capacity checks."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "platform/playbooks/files"))

from inspect_o11y_restore_target import inspect, main  # noqa: E402

GIB = 1024**3


def selection():
    return {
        "volid": "backup-store:backup/vzdump-qemu-102-20261020T000000Z.vma.zst",
        "backend": "non-pbs",
        "format": "vma.zst",
        "creation_time_epoch": 1_792_771_200,
        "size_bytes": 10 * GIB,
    }


def payload():
    selected = {
        "backend_class": "non-pbs",
        "format": "vma.zst",
        "size_bytes": 10 * GIB,
        "creation_time_epoch": 1_792_771_200,
        "proxmox_protected_flag": "unknown",
        "immutability_verified": False,
    }
    return {
        "source": {"vmid": "102", "node": "source-node-private"},
        "target": {
            "vmid": "202",
            "node": "restore-node-private",
            "storage_id": "restore-storage-private",
            "network_mode": "disconnected",
            "headroom_percent": 30,
        },
        "artifact_selector": selection(),
        "artifact_receipt": {
            "target_vm_verified": True,
            "candidate_artifact_count": 1,
            "candidates": [selected],
            "selected_candidate_index": 0,
            "selected_candidate": dict(selected),
        },
        "artifact_config": {
            "status": 200,
            "data": (
                "vmid: 102\n"
                "agent: enabled=1\n"
                "name: private-o11y\n"
                "scsi0: source-storage-private:vm-102-disk-0,size=100G\n"
                "efidisk0: source-storage-private:vm-102-disk-1,size=4M\n"
                "ide2: none,media=cdrom\n"
            ),
        },
        "vms": [{"vmid": 102, "node": "source-node-private", "type": "qemu"}],
        "target_config": {"status": 404},
        "target_nextid": {"status": 200, "data": 202},
        "nodes": [
            {"node": "source-node-private", "status": "online"},
            {"node": "restore-node-private", "status": "online"},
        ],
        "storage_reads": [
            {
                "item": "restore-node-private",
                "status": 200,
                "data": [
                    {
                        "storage": "restore-storage-private",
                        "type": "lvmthin",
                        "active": 1,
                        "shared": 0,
                        "content": "images,rootdir",
                        "total": 200 * GIB,
                        "avail": 180 * GIB,
                    }
                ],
            }
        ],
    }


def test_reports_only_fixed_sanitized_available_now_facts():
    result = inspect(payload())

    assert result == {
        "survey": "read-only-o11y-restore-target-preflight",
        "source_target_identity_distinct": True,
        "target_network_declared_disconnected": True,
        "artifact_selection_verified": True,
        "artifact_restore_layout_complete": True,
        "restore_disk_count": 2,
        "target_vmid_available_now": True,
        "cluster_census_visibility": "unverified",
        "target_vmid_exact_cluster_nextid_match": True,
        "target_vmid_reserved": False,
        "target_node_online_and_off_source": True,
        "target_storage_active_nonshared_image_store": True,
        "proxmox_reported_capacity_includes_declared_headroom": True,
        "capacity_basis": "proxmox-reported",
        "allocation_authorized": False,
        "restore_performed": False,
        "restore_verified": False,
    }
    rendered = str(result)
    assert all(
        private not in rendered
        for private in (
            "102",
            "202",
            "source-node-private",
            "restore-node-private",
            "restore-storage-private",
            "source-storage-private",
            "backup-store",
            "private-o11y",
            str(100 * GIB),
            str(180 * GIB),
        )
    )
    assert "capacity_gate_pass" not in result


def test_artifact_preflight_admits_only_the_canonical_provisioner_console_and_metadata():
    data = payload()
    data["artifact_config"]["data"] += (
        "serial0: socket\n"
        "meta: creation-qemu=8.1.5,ctime=1714504207\n"
    )
    result = inspect(data)
    assert result["artifact_restore_layout_complete"] is True
    assert result["restore_disk_count"] == 2

    for line in (
        "serial0: tcp",
        "serial1: socket",
        "meta: creation-qemu=8.1.5,ctime=1714504207,unreviewed=value",
        "meta: ctime=1714504207,creation-qemu=8.1.5",
        "metadata: creation-qemu=8.1.5,ctime=1714504207",
    ):
        invalid = payload()
        invalid["artifact_config"]["data"] += line + "\n"
        with pytest.raises(ValueError, match="artifact restore layout is incomplete"):
            inspect(invalid)


def test_refuses_target_vmid_occupied_by_exact_config_read():
    data = payload()
    data["target_config"]["status"] = 200
    with pytest.raises(ValueError, match="occupied or conflicts"):
        inspect(data)


def test_refuses_source_and_target_vmid_collision():
    data = payload()
    data["target"]["vmid"] = data["source"]["vmid"]
    with pytest.raises(ValueError, match="declarations conflict"):
        inspect(data)


def test_refuses_target_vmid_census_collision_even_when_exact_read_says_absent():
    data = payload()
    data["vms"].append({"vmid": 202, "node": "other-node-private", "type": "qemu"})
    with pytest.raises(ValueError, match="occupied or conflicts"):
        inspect(data)


@pytest.mark.parametrize(
    ("config_status", "nextid"),
    [
        (404, {"status": 400, "data": None}),
        (404, {"status": 200, "data": 203}),
        (404, {"status": 200, "data": 400}),
        (500, {"status": 400, "data": None}),
        (500, {"status": 200, "data": 203}),
        (403, {"status": 200, "data": 202}),
    ],
)
def test_refuses_missing_or_conflicting_exact_cluster_nextid_evidence(config_status, nextid):
    data = payload()
    data["target_config"]["status"] = config_status
    data["target_nextid"] = nextid
    with pytest.raises(ValueError, match="occupied or conflicts"):
        inspect(data)


def test_accepts_guarded_node_config_500_only_with_exact_nextid_match():
    data = payload()
    data["target_config"]["status"] = 500
    data["target_nextid"] = {"status": 200, "data": "202"}
    assert inspect(data)["target_vmid_available_now"] is True
    assert inspect(data)["target_vmid_reserved"] is False


def test_refuses_disagreement_between_exact_read_and_census():
    data = payload()
    data["target_config"]["status"] = 200
    with pytest.raises(ValueError, match="occupied or conflicts"):
        inspect(data)


def test_refuses_target_declared_on_source_node_or_with_network_enabled():
    data = payload()
    data["target"]["node"] = data["source"]["node"]
    with pytest.raises(ValueError, match="declarations conflict"):
        inspect(data)

    data = payload()
    data["target"]["network_mode"] = "connected"
    with pytest.raises(ValueError, match="disconnected networking"):
        inspect(data)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda data: data["storage_reads"][0]["data"][0].update(storage="other"),
            "unavailable or does not support",
        ),
        (
            lambda data: data["storage_reads"][0]["data"][0].update(active=0),
            "unavailable or does not support",
        ),
        (
            lambda data: data["storage_reads"][0]["data"][0].update(content="backup"),
            "unavailable or does not support",
        ),
        (
            lambda data: data["storage_reads"][0]["data"][0].update(shared=1),
            "unavailable or does not support",
        ),
        (
            lambda data: data["storage_reads"][0]["data"][0].update(avail=50 * GIB),
            "insufficient for the complete restore layout",
        ),
        (
            lambda data: data["storage_reads"][0]["data"][0].update(total="200 GiB"),
            "unavailable or does not support",
        ),
        (
            lambda data: data["nodes"][1].update(status="offline"),
            "unavailable or shares the source node",
        ),
        (
            lambda data: data["nodes"].append(dict(data["nodes"][1])),
            "malformed node evidence",
        ),
        (
            lambda data: data["vms"].append(dict(data["vms"][0])),
            "malformed or conflicting VM census",
        ),
    ],
)
def test_fails_closed_for_storage_capacity_and_authority_conflicts(mutate, message):
    data = payload()
    mutate(data)
    with pytest.raises(ValueError, match=message):
        inspect(data)


@pytest.mark.parametrize(
    "config",
    [
        "scsi0: local:vm-102-disk-0,size=100G\nscsi0: local:other,size=10G\n",
        "scsi0: local:vm-102-disk-0,size=100G,backup=0\n",
        "scsi0: local:vm-102-disk-0,backup=1\n",
        "ide2: none,media=cdrom-invalid\nscsi0: local:disk,size=100G\n",
        "ide2: none,media=cdrom-invalid,size=700M\nscsi0: local:disk,size=100G\n",
        "ide2: none,media=cdrom\nide2: none,media=cdrom\nscsi0: local:disk,size=100G\n",
        "name: o11y\nnet0: e1000=00:11:22:33:44:55,bridge=vmbr0\n",
    ],
)
def test_refuses_incomplete_excluded_or_ambiguous_artifact_disk_layout(config):
    data = payload()
    data["artifact_config"]["data"] = config
    with pytest.raises(ValueError, match="layout is incomplete, excluded, or ambiguous"):
        inspect(data)


def test_refuses_artifact_selector_that_does_not_match_the_verified_census():
    data = payload()
    data["artifact_selector"]["size_bytes"] += 1
    with pytest.raises(ValueError, match="not bound to the verified artifact census"):
        inspect(data)


@pytest.mark.parametrize(
    "config",
    ["x" * (256 * 1024 + 1), "\ud800"],
)
def test_refuses_oversized_or_invalid_unicode_artifact_configuration(config):
    data = payload()
    data["artifact_config"]["data"] = config
    with pytest.raises(ValueError, match="malformed artifact configuration"):
        inspect(data)


def test_cdrom_does_not_count_as_restore_disk_or_require_size():
    data = payload()
    data["artifact_config"]["data"] = (
        "vmid: 102\n"
        "agent: enabled=1\n"
        "ide2: none,media=cdrom\n"
        "scsi0: source-storage-private:vm-102-disk-0,size=100G\n"
    )
    assert inspect(data)["restore_disk_count"] == 1


def test_target_preflight_keeps_block_cloud_init_aux_out_of_disk_capacity_count():
    data = payload()
    data["artifact_config"]["data"] = (
        "vmid: 102\n"
        "agent: 1\n"
        "ciuser: observer\n"
        "ipconfig0: ip=dhcp\n"
        "ide2: source-storage-private:vm-102-cloudinit,media=cdrom\n"
        "scsi0: source-storage-private:vm-102-disk-0,size=100G\n"
    )
    result = inspect(data)
    assert result["restore_disk_count"] == 1
    assert result["artifact_restore_layout_complete"] is True

    data["storage_reads"][0]["data"][0]["type"] = "dir"
    with pytest.raises(ValueError, match="reviewed cloud-init auxiliary shape"):
        inspect(data)


def test_cli_refusal_uses_fixed_sanitized_message(monkeypatch, capsys):
    data = payload()
    data["target_config"]["status"] = 403
    monkeypatch.setattr(sys, "argv", ["inspect_o11y_restore_target.py", "inspect"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(data)))

    assert main() == 2
    output = capsys.readouterr().out
    assert "restore-node-private" not in output
    assert "202" not in output
    assert "refusal" in output


def test_playbook_is_pinned_read_only_and_hides_protected_requests():
    playbook = yaml.safe_load(
        (ROOT / "platform/playbooks/preflight-o11y-restore-target.yml").read_text()
    )
    assert playbook[0]["ansible.builtin.import_playbook"] == "refuse-internal-extra-vars.yml"
    assert any(
        item.get("ansible.builtin.import_playbook") == "require-reviewed-checkout.yml"
        for item in playbook
    )
    uri_tasks = []
    command_tasks = []
    for play in playbook:
        for task in play.get("tasks", []):
            if "ansible.builtin.uri" in task:
                uri_tasks.append(task)
            if "ansible.builtin.command" in task:
                command_tasks.append(task)
    assert uri_tasks
    assert all(task["ansible.builtin.uri"].get("method") == "GET" for task in uri_tasks)
    assert all(task.get("no_log") is True for task in uri_tasks)
    assert all(task.get("check_mode") is False for task in uri_tasks)
    assert all(task.get("no_log") is True for task in command_tasks)
    assert any(
        "/vzdump/extractconfig?volume={{ _artifact_selector.volid | urlencode }}"
        in task["ansible.builtin.uri"]["url"]
        for task in uri_tasks
    )
    target_config_task = next(
        task["ansible.builtin.uri"]
        for play in playbook
        for task in play.get("tasks", [])
        if task.get("register") == "_target_config"
    )
    nextid_task = next(
        task["ansible.builtin.uri"]
        for play in playbook
        for task in play.get("tasks", [])
        if task.get("register") == "_target_nextid"
    )
    assert target_config_task["method"] == "GET"
    assert target_config_task["status_code"] == [200, 404, 500]
    assert nextid_task["method"] == "GET"
    assert nextid_task["status_code"] == [200, 400]
    assert "cluster/nextid?vmid={{ _target_vmid }}" in nextid_task["url"]
    helper_task = next(
        task["ansible.builtin.command"]
        for play in playbook
        for task in play.get("tasks", [])
        if task.get("register") == "_target_inspection"
    )
    assert "target_config" in helper_task["stdin"]
    assert "target_nextid" in helper_task["stdin"]
    rendered = (ROOT / "platform/playbooks/preflight-o11y-restore-target.yml").read_text()
    assert "NetBox" not in rendered
    assert "network_mode" in rendered and "disconnected" in rendered
    assert "o11y_restore_target_vmid" in rendered
    assert "o11y_backup_artifact_selector" in rendered


def test_dev_catalog_and_verify_tag_are_read_only_and_require_only_reviewed_sha():
    templates = yaml.safe_load((ROOT / "platform/semaphore/templates.yml").read_text())["templates"]
    matches = [item for item in templates if item["name"] == "Preflight o11y Restore Target (Dev)"]
    assert len(matches) == 1
    template = matches[0]
    assert template["playbook"] == "platform/playbooks/preflight-o11y-restore-target.yml"
    assert template["repository"] == "agent-cloud dev"
    assert [field["name"] for field in template["survey_vars"]] == ["expected_repository_sha"]
    assert template["survey_vars"][0]["required"] is True

    plays = yaml.safe_load((ROOT / "platform/playbooks/preflight-o11y-restore-target.yml").read_text())
    main = next(play for play in plays if play.get("name") == "Preflight one exact o11y restore target")
    assert main["tags"] == ["verify"]
    transport = next(
        task for task in main["tasks"]
        if task.get("ansible.builtin.include_tasks", {}).get("file") == "tasks/assert-bao-transport.yml"
    )
    assert transport["ansible.builtin.include_tasks"]["apply"]["tags"] == ["verify"]
    assert all(task["ansible.builtin.uri"]["method"] == "GET" for task in main["tasks"]
               if "ansible.builtin.uri" in task)
    assert not any("method: POST" in str(task) or "method: DELETE" in str(task) for task in main["tasks"])
