"""Synthetic refusal and disclosure checks for the read-only PBS VM census."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "platform/playbooks/files"))

from inspect_pbs_vm_census import inspect, main, template_requests  # noqa: E402


def api(data):
    return {"status": 200, "json": {"data": data}}


def census():
    return {
        "nodes": api([{"node": "private-node", "status": "online"}]),
        "vms": api([
            {"type": "qemu", "node": "private-node", "vmid": 9000, "template": 1},
            {"type": "lxc", "node": "private-node", "vmid": 101},
        ]),
        "storages": api([
            {"storage": "private-store", "type": "lvmthin", "content": "images,rootdir"},
            {"storage": "backup-store", "type": "pbs", "content": "backup"},
        ]),
        "nextid": "102",
        "template_configs": api([{
            "item": {"node": "private-node", "vmid": "9000"}, "status": 200,
            "json": {"data": {"name": "Debian-cloud", "ostype": "l26", "ide2": "private-store:cloudinit"}},
        }]),
        "api_token": "synthetic-secret",
    }


def test_template_requests_are_validated_and_contain_only_protected_api_targets():
    data = census()
    assert template_requests({"nodes": data["nodes"], "vms": data["vms"]}) == [
        {"node": "private-node", "vmid": "9000"},
    ]


def test_report_is_fixed_and_does_not_claim_debian_provenance_from_name_or_ostype():
    result = inspect(census())
    assert result == {
        "survey": "pbs-vm-census",
        "read_only": True,
        "inventory_visibility": "unverified",
        "node_count": 1,
        "online_node_count": 1,
        "offline_node_count": 0,
        "vm_count": 2,
        "qemu_vm_count": 1,
        "lxc_container_count": 1,
        "next_vm_identity_absent_from_visible_rows": True,
        "template_count": 1,
        "cloud_init_template_count": 1,
        "debian_name_hint_count": 1,
        "linux_guest_type_hint_count": 1,
        "proven_debian_image_provenance_count": 0,
        "storage_count": 2,
        "storage_backend_counts": {
            "cephfs": 0, "cifs": 0, "dir": 0, "lvm": 0, "lvmthin": 1,
            "nfs": 0, "pbs": 1, "rbd": 0, "zfspool": 0, "other": 0,
        },
        "image_capable_storage_count": 1,
        "backup_capable_storage_count": 1,
    }
    serialized = json.dumps(result)
    for private_value in ("private-node", "9000", "Debian-cloud", "private-store", "backup-store", "synthetic-secret"):
        assert private_value not in serialized


@pytest.mark.parametrize("mutate", [
    lambda data: data["nodes"]["json"]["data"].append({"node": "private-node", "status": "online"}),
    lambda data: data["vms"]["json"]["data"].append({"type": "lxc", "node": "private-node", "vmid": 9000}),
    lambda data: data["storages"]["json"]["data"].append(
        {"storage": "private-store", "type": "lvm", "content": "images"}
    ),
    lambda data: data["template_configs"]["json"]["data"].append(data["template_configs"]["json"]["data"][0]),
    lambda data: data.update(nextid="not-an-id"),
    lambda data: data["vms"]["json"]["data"].append({"type": [], "node": "private-node", "vmid": 100}),
])
def test_malformed_duplicate_or_ambiguous_evidence_is_refused(mutate):
    data = census()
    mutate(data)
    with pytest.raises(ValueError):
        inspect(data)


def test_missing_or_offline_template_configuration_target_is_refused():
    data = census()
    data["nodes"]["json"]["data"][0]["status"] = "offline"
    with pytest.raises(ValueError):
        template_requests({"nodes": data["nodes"], "vms": data["vms"]})

    data = census()
    data["template_configs"]["json"]["data"].clear()
    with pytest.raises(ValueError):
        inspect(data)


def test_lxc_template_is_counted_but_only_qemu_templates_request_config_reads():
    data = census()
    data["vms"]["json"]["data"].append(
        {"type": "lxc", "node": "private-node", "vmid": 103, "template": 1}
    )

    result = inspect(data)
    assert result["vm_count"] == 3
    assert result["lxc_container_count"] == 2
    assert result["template_count"] == 1
    assert template_requests({"nodes": data["nodes"], "vms": data["vms"]}) == [
        {"node": "private-node", "vmid": "9000"},
    ]


def test_empty_permission_filtered_lists_are_reported_as_visibility_unverified():
    data = census()
    data["vms"] = api([])
    data["storages"] = api([])
    data["template_configs"] = api([])

    result = inspect(data)
    assert result["inventory_visibility"] == "unverified"
    assert result["vm_count"] == 0
    assert result["storage_count"] == 0
    assert "inventory_complete" not in result


def test_cli_refusal_does_not_echo_credentials_or_response_values(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["inspect_pbs_vm_census.py", "inspect"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"api_token": "synthetic-secret", "nodes": "bad"})))
    assert main() == 2
    result = capsys.readouterr()
    assert "synthetic-secret" not in result.out + result.err
    assert "private-node" not in result.out + result.err
    assert "refused" in result.err


def test_semaphore_entry_is_dev_bound_and_the_playbook_is_get_only():
    playbook = yaml.safe_load((ROOT / "platform/playbooks/survey-pbs-vm-census.yml").read_text())
    templates = yaml.safe_load((ROOT / "platform/semaphore/templates.yml").read_text())["templates"]
    declaration = [row for row in templates if row.get("name") == "Survey PBS VM Census (Dev)"]
    assert len(declaration) == 1
    assert declaration[0]["playbook"] == "platform/playbooks/survey-pbs-vm-census.yml"
    assert declaration[0]["repository"] == "agent-cloud dev"
    assert declaration[0]["survey_vars"] == [{
        "name": "expected_repository_sha", "title": "Reviewed commit SHA",
        "description": "Exact reviewed Dev revision for the read-only Proxmox VM and storage census",
        "type": "string", "required": True,
    }]
    assert playbook[0]["ansible.builtin.import_playbook"] == "refuse-internal-extra-vars.yml"
    census_play = playbook[1]
    assert "expected_repository_sha" in str(census_play["tasks"][0]["ansible.builtin.assert"]["that"])
    requests = [task["ansible.builtin.uri"] for task in census_play["tasks"] if "ansible.builtin.uri" in task]
    assert requests and all(request["method"] == "GET" for request in requests)
    assert all(task.get("no_log") is True for task in census_play["tasks"] if "ansible.builtin.uri" in task)
    assert "ansible.builtin.set_stats" in str(census_play["tasks"][-1])
