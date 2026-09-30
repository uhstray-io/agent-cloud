"""Sanitized readback contract for the receiver host storage diagnostic."""

import importlib.util
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

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
        "lvs": json.dumps(
            {"report": [{"lv": [{"lv_path": "/dev/mapper/vg-root", "vg_name": "private-vg",
                                  "lv_size": "700.00"}]}]}
        ),
        "vgs": json.dumps(
            {"report": [{"vg": [{"vg_name": "private-vg", "vg_free": "300.00"}]}]}
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
        "/": {"total_bytes": 1200, "available_bytes": 0, "device_id": 4},
        "/tmp": {"total_bytes": 1000, "available_bytes": 400, "device_id": 7},
        "/var/tmp": {"total_bytes": 2000, "available_bytes": 600, "device_id": 7},
    }.get(path)


def _device_number(path):
    return "253:0" if path == "/dev/mapper/vg-root" else None


def test_reports_runtime_assertion_and_storage_topology_without_network_addresses():
    report = DIAGNOSTIC.diagnose(
        _readbacks(), capacity_reader=_capacity, device_number_reader=_device_number
    )

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
        "filesystem_type": "ext4",
        "filesystem_total_bytes": 1200,
        "filesystem_available_bytes": 0,
        "block_chain": [
            {"type": "lvm", "size_bytes": 700},
            {"type": "part", "size_bytes": 800},
            {"type": "disk", "size_bytes": 1000},
        ],
        "lvm": {
            "status": "observed",
            "logical_volume_size_bytes": 700,
            "volume_group_free_bytes": 300,
        },
    }
    assert report["podman_storage"]["volume_path_filesystem"]["available_bytes"] == 400
    assert report["podman_storage"]["graph_root_filesystem"]["free_percent"] == 30
    assert report["podman_storage"]["volume_path_and_graph_root_share_filesystem"] is True
    assert "192.0.2.30" not in json.dumps(report)
    serialized = json.dumps(report)
    assert "/tmp" not in serialized
    assert "/dev/" not in serialized
    assert "private-vg" not in serialized
    assert "nvme0n1" not in serialized


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

    report = DIAGNOSTIC.diagnose(payload, capacity_reader=_capacity, device_number_reader=_device_number)
    assert report["status"] == "observed"
    assert report["node_exporter"]["pid_mode"] == "private"
    assert report["node_exporter"]["running"] is False
    assert report["node_exporter"]["host_root_mount_read_only"] is False
    assert report["node_exporter"]["published_port_count"] == 1
    assert "0.0.0.0" not in json.dumps(report)


def test_malformed_essential_readbacks_fail_closed_with_fixed_reasons():
    payload = _readbacks()
    payload["exporter_inspect"] = "private inspect error with address 192.0.2.44"
    report = DIAGNOSTIC.diagnose(payload, capacity_reader=_capacity, device_number_reader=_device_number)
    assert report == {"status": "unavailable", "reason": "exporter_inspect_invalid"}
    assert "192.0.2.44" not in json.dumps(report)

    payload = _readbacks()
    topology = json.loads(payload["block_topology"])
    topology["blockdevices"][0]["children"][0]["pkname"] = "/dev/missing"
    payload["block_topology"] = json.dumps(topology)
    report = DIAGNOSTIC.diagnose(payload, capacity_reader=_capacity, device_number_reader=_device_number)
    assert report == {"status": "unavailable", "reason": "block_parent_unresolved"}


def test_device_mapper_alias_uses_kernel_device_number_without_path_guessing():
    payload = _readbacks()
    topology = json.loads(payload["block_topology"])
    root = topology["blockdevices"][0]["children"][0]["children"][0]
    root["name"] = "/dev/dm-0"
    payload["block_topology"] = json.dumps(topology)

    report = DIAGNOSTIC.diagnose(payload, capacity_reader=_capacity)

    assert report["status"] == "observed"
    assert report["guest_root"]["block_chain"][0] == {"type": "lvm", "size_bytes": 700}


def test_lvm_report_skips_unrelated_empty_or_relative_lv_paths():
    payload = _readbacks()
    reports = json.loads(payload["lvs"])
    reports["report"][0]["lv"][:0] = [
        {"lv_path": "", "lv_name": "unavailable-path"},
        {"lv_path": "[pool_tdata]", "lv_name": "thin-data"},
    ]
    payload["lvs"] = json.dumps(reports)

    report = DIAGNOSTIC.diagnose(
        payload, capacity_reader=_capacity, device_number_reader=_device_number
    )

    assert report["status"] == "observed"
    assert report["guest_root"]["lvm"] == {
        "status": "observed",
        "logical_volume_size_bytes": 700,
        "volume_group_free_bytes": 300,
    }


def test_unavailable_lvm_report_does_not_hide_sanitized_root_filesystem_readback():
    payload = _readbacks()
    payload["lvs"] = "lvm: denied"

    report = DIAGNOSTIC.diagnose(
        payload, capacity_reader=_capacity, device_number_reader=_device_number
    )

    assert report["status"] == "observed"
    assert report["guest_root"]["filesystem_available_bytes"] == 0
    assert report["guest_root"]["lvm"] == {"status": "unavailable", "reason": "lvm_report_invalid"}
    assert "private-vg" not in json.dumps(report)


def test_missing_lvm_reports_have_a_distinct_unavailable_reason():
    for key in ("lvs", "vgs"):
        payload = _readbacks()
        payload[key] = None

        report = DIAGNOSTIC.diagnose(
            payload, capacity_reader=_capacity, device_number_reader=_device_number
        )

        assert report["status"] == "observed"
        assert report["guest_root"]["lvm"] == {
            "status": "unavailable",
            "reason": "lvm_unavailable",
        }


def test_collect_reads_lvm_capacity_with_readonly_metadata_commands():
    commands = []
    with patch.object(DIAGNOSTIC, "_read", side_effect=lambda argv: commands.append(argv) or "{}"):
        DIAGNOSTIC.collect()

    lvm_commands = [argv for argv in commands if argv[0] in {"lvs", "vgs"}]
    assert {argv[0] for argv in lvm_commands} == {"lvs", "vgs"}
    assert all("--readonly" in argv for argv in lvm_commands)


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
    report_task = next(task for task in receiver_tasks if task.get("name") ==
                       "Report sanitized receiver host storage diagnostics")
    assert report_task["ansible.builtin.debug"]["msg"] == "{{ _host_storage_diagnostic.stdout }}"
    assert "from_json" not in str(report_task)
    readback_gate = next(task for task in receiver_tasks if task.get("name") ==
                         "Require complete host storage readbacks")
    conditions = readback_gate["ansible.builtin.assert"]["that"]
    assert "_host_storage_diagnostic.rc == 0" in conditions
    assert "_host_storage_diagnostic.stdout | length > 0" in conditions
    assert "from_json" not in str(readback_gate)
    template = next(
        item for item in yaml.safe_load((ROOT / "platform/semaphore/templates.yml").read_text())["templates"]
        if item["name"] == "Diagnose o11y Host Storage (Dev)"
    )
    assert template["repository"] == "agent-cloud dev"
    assert [item["name"] for item in template["survey_vars"]] == [
        "expected_repository_sha", "expected_receiver_sha"
    ]


def test_both_diagnostics_require_writable_tmpfs_before_using_remote_modules():
    for name in ("diagnose-o11y-host-storage.yml", "diagnose-o11y-grafana-auth.yml"):
        plays = yaml.safe_load((ROOT / "platform/playbooks" / name).read_text())
        receiver = next(play for play in plays if play.get("hosts") == "o11y_svc")
        assert receiver["vars"]["ansible_remote_tmp"] == "/dev/shm/ansible-tmp"
        assert receiver["environment"]["TMPDIR"] == "/dev/shm/ansible-tmp"
        assert receiver["become"] is False
        raw = receiver["tasks"][0]
        assert "ansible.builtin.raw" in raw
        script = raw["ansible.builtin.raw"]
        assert "findmnt" in script
        assert "mkdir" not in script
        assert "if [ -L /dev/shm/ansible-tmp ]" in script
        assert "if [ -e /dev/shm/ansible-tmp ]" in script
        assert script.index("if [ -e /dev/shm/ansible-tmp ]") < script.index(
            "findmnt -n -o FSTYPE --target /dev/shm 2>/dev/null"
        )
        assert "findmnt -n -o FSTYPE --target /dev/shm/ansible-tmp" in script
        assert "current_uid=\"$(id -u 2>/dev/null)\"" in script
        assert "stat -c '%u' /dev/shm/ansible-tmp" in script
        assert "stat -c '%a' /dev/shm/ansible-tmp" in script
        assert '"$path_mode" != 700' in script
        assert 'df -Pk "$tmpfs_path"' in script
        assert "printf 'remote_tmpfs_ready\\n'" in script
        assert raw["changed_when"] is False
        assert raw["check_mode"] is False
        assert subprocess.run(["sh", "-n"], input=script, text=True, capture_output=True).returncode == 0
        assert receiver["tasks"][1]["ansible.builtin.assert"]["that"] == [
            "ansible_remote_tmp == '/dev/shm/ansible-tmp'",
            "_remote_tmpfs_preflight.rc == 0",
            "_remote_tmpfs_preflight.stdout | trim == 'remote_tmpfs_ready'",
        ]
