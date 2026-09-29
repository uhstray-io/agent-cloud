"""Sanitized readback contract for the receiver host storage diagnostic."""

import importlib.util
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "platform/playbooks/files/diagnose-o11y-host-storage.py"
SPEC = importlib.util.spec_from_file_location("o11y_host_storage_diagnostic", HELPER)
DIAGNOSTIC = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DIAGNOSTIC)


def _readbacks():
    return {
        "exporter_inspect": json.dumps(
            {
                "pid_mode": "host",
                "state": "running",
                "mounts": [{"Source": "/", "Destination": "/host", "RW": False}],
                "ports": {},
                "networks": {"o11y": {"IPAddress": "192.0.2.30"}},
            }
        ),
        "podman_info": json.dumps(
            {"store": {"volumePath": "/tmp", "graphRoot": "/var/tmp"}}
        ),
        "root_mount": json.dumps(
            {"filesystems": [{"source": "/dev/mapper/vg-root", "fstype": "ext4", "maj:min": "253:0"}]}
        ),
        "block_topology": json.dumps(
            {
                "blockdevices": [
                    {
                        "name": "/dev/nvme0n1",
                        "type": "disk",
                        "size": 1000,
                        "maj:min": "259:0",
                        "children": [
                            {
                                "name": "/dev/nvme0n1p3",
                                "type": "part",
                                "size": 800,
                                "maj:min": "259:3",
                                "pkname": "/dev/nvme0n1",
                                "children": [
                                    {
                                        "name": "/dev/mapper/vg-root",
                                        "type": "lvm",
                                        "size": 700,
                                        "maj:min": "253:0",
                                        "pkname": "/dev/nvme0n1p3",
                                    }
                                ],
                            }
                        ],
                    }
                ]
            }
        ),
    }


def _capacity(path):
    return {
        "/tmp": {"total_bytes": 1000, "available_bytes": 400, "device_id": 7},
        "/var/tmp": {"total_bytes": 2000, "available_bytes": 600, "device_id": 7},
    }.get(path)


def test_reports_runtime_assertion_and_storage_topology_without_network_addresses():
    report = DIAGNOSTIC.diagnose(_readbacks(), capacity_reader=_capacity)

    assert report["status"] == "observed"
    assert report["node_exporter"] == {
        "pid_mode": "host",
        "state": "running",
        "running": True,
        "host_root_mount_read_only": True,
        "network_count": 1,
        "published_port_count": 0,
    }
    assert report["guest_root"] == {
        "source": "/dev/mapper/vg-root",
        "filesystem_type": "ext4",
        "block_chain": [
            {"path": "/dev/mapper/vg-root", "type": "lvm", "size_bytes": 700},
            {"path": "/dev/nvme0n1p3", "type": "part", "size_bytes": 800},
            {"path": "/dev/nvme0n1", "type": "disk", "size_bytes": 1000},
        ],
    }
    assert report["podman_storage"]["volume_path_filesystem"]["available_bytes"] == 400
    assert report["podman_storage"]["graph_root_filesystem"]["free_percent"] == 30
    assert report["podman_storage"]["volume_path_and_graph_root_share_filesystem"] is True
    assert "192.0.2.30" not in json.dumps(report)
    assert "/tmp" not in json.dumps(report)


def test_reports_private_pid_and_writable_mount_without_treating_readback_as_malformed():
    payload = _readbacks()
    inspect = json.loads(payload["exporter_inspect"])
    inspect.update(
        pid_mode="",
        state="exited",
        mounts=[{"Source": "/", "Destination": "/host", "RW": True}],
        ports={"9100/tcp": [{"HostIp": "0.0.0.0", "HostPort": "9100"}]},
    )
    payload["exporter_inspect"] = json.dumps(inspect)

    report = DIAGNOSTIC.diagnose(payload, capacity_reader=_capacity)
    assert report["status"] == "observed"
    assert report["node_exporter"]["pid_mode"] == "private"
    assert report["node_exporter"]["running"] is False
    assert report["node_exporter"]["host_root_mount_read_only"] is False
    assert report["node_exporter"]["published_port_count"] == 1
    assert "0.0.0.0" not in json.dumps(report)


def test_malformed_essential_readbacks_fail_closed_with_fixed_reasons():
    payload = _readbacks()
    payload["exporter_inspect"] = "private inspect error with address 192.0.2.44"
    report = DIAGNOSTIC.diagnose(payload, capacity_reader=_capacity)
    assert report == {"status": "unavailable", "reason": "exporter_inspect_invalid"}
    assert "192.0.2.44" not in json.dumps(report)

    payload = _readbacks()
    topology = json.loads(payload["block_topology"])
    topology["blockdevices"][0]["children"][0]["pkname"] = "/dev/missing"
    payload["block_topology"] = json.dumps(topology)
    report = DIAGNOSTIC.diagnose(payload, capacity_reader=_capacity)
    assert report == {"status": "unavailable", "reason": "block_parent_unresolved"}


def test_device_mapper_alias_uses_kernel_device_number_without_path_guessing():
    payload = _readbacks()
    topology = json.loads(payload["block_topology"])
    root = topology["blockdevices"][0]["children"][0]["children"][0]
    root["name"] = "/dev/dm-0"
    payload["block_topology"] = json.dumps(topology)

    report = DIAGNOSTIC.diagnose(payload, capacity_reader=_capacity)

    assert report["status"] == "observed"
    assert report["guest_root"]["source"] == "/dev/mapper/vg-root"
    assert report["guest_root"]["block_chain"][0]["path"] == "/dev/dm-0"


def test_dev_playbook_and_template_require_both_exact_revisions_and_only_read():
    plays = yaml.safe_load((ROOT / "platform/playbooks/diagnose-o11y-host-storage.yml").read_text())
    assert plays[0]["ansible.builtin.import_playbook"] == "preflight-target-group.yml"
    controller = next(play for play in plays if play.get("hosts") == "localhost")
    controller_tasks = controller["tasks"]
    assert any(
        "expected_repository_sha" in str(task.get("ansible.builtin.assert", {}).get("that", ""))
        for task in controller_tasks
    )
    receiver = next(play for play in plays if play.get("hosts") == "o11y_svc")
    receiver_tasks = receiver["tasks"]
    assert any("expected_receiver_sha" in str(task.get("ansible.builtin.assert", {}).get("that", ""))
               for task in receiver_tasks)
    assert not any(
        any(module in task for module in ("ansible.builtin.file", "ansible.builtin.copy",
                                          "ansible.builtin.template", "ansible.builtin.uri",
                                          "ansible.builtin.include_tasks"))
        for task in controller_tasks + receiver_tasks
    )
    template = next(
        item for item in yaml.safe_load((ROOT / "platform/semaphore/templates.yml").read_text())["templates"]
        if item["name"] == "Diagnose o11y Host Storage (Dev)"
    )
    assert template["repository"] == "agent-cloud dev"
    assert [item["name"] for item in template["survey_vars"]] == [
        "expected_repository_sha", "expected_receiver_sha"
    ]
