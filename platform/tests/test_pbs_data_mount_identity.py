"""The PBS installer must prove that its data path is the VMID-pinned UUID mount."""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

HELPER = Path(__file__).parents[1] / "playbooks/files/assert_pbs_data_mount.py"
SPEC = importlib.util.spec_from_file_location("pbs_mount_identity", HELPER)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def make_runner(*, duplicate=False, partition=False, wrong_fstab=False, no_noatime=False):
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        if argv[0] == "lsblk":
            nodes = [{
                "path": "/dev/sdb", "type": "disk", "serial": "ac-pbs-data-123",
                "uuid": "a1b2-c3d4", "fstype": "ext4", "mountpoints": ["/srv/pbs-data"],
            }]
            if partition:
                nodes[0] = {**nodes[0], "type": "part"}
            if duplicate:
                nodes.append({**nodes[0], "path": "/dev/sdc"})
            stdout = {"blockdevices": nodes}
        elif "--fstab" in argv:
            stdout = {"filesystems": [{
                "source": "UUID=wrong" if wrong_fstab else "UUID=a1b2-c3d4",
                "target": "/srv/pbs-data", "fstype": "ext4",
                "options": "defaults" if no_noatime else "defaults,noatime",
            }]}
        else:
            stdout = {"filesystems": [{
                "source": "/dev/sdb", "target": "/srv/pbs-data",
                "fstype": "ext4", "uuid": "a1b2-c3d4", "options": "rw,noatime",
            }]}
        return subprocess.CompletedProcess(argv, 0, json.dumps(stdout), "")

    return runner, calls


def test_proves_the_serial_pinned_whole_disk_and_uuid_fstab_mount():
    runner, calls = make_runner()
    assert MODULE.inspect_mount("123", runner) == {
        "device": "/dev/sdb", "uuid": "a1b2-c3d4", "target": "/srv/pbs-data", "fstype": "ext4",
    }
    assert [call[0] for call in calls] == ["lsblk", "findmnt", "findmnt"]
    assert MODULE.inspect_mount("123", runner)["uuid"] == "a1b2-c3d4"


@pytest.mark.parametrize("kwargs,message", [
    ({"duplicate": True}, "exactly one device"),
    ({"partition": True}, "whole disk"),
    ({"wrong_fstab": True}, "fstab entry"),
    ({"no_noatime": True}, "noatime option"),
])
def test_refuses_ambiguous_or_unmounted_storage(kwargs, message):
    runner, _ = make_runner(**kwargs)
    with pytest.raises(ValueError, match=message):
        MODULE.inspect_mount("123", runner)


def test_rejects_invalid_vmid_before_device_queries():
    runner, calls = make_runner()
    with pytest.raises(ValueError, match="VMID"):
        MODULE.inspect_mount("../123", runner)
    assert calls == []
