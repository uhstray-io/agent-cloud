"""Synthetic identity, blank-disk, refusal, and rerun checks for PBS guest storage."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "platform/playbooks/files"))

from prepare_pbs_guest_disk import Refusal, disk_state, main, select_disk  # noqa: E402

SERIAL = "ac-pbs-data-1234"
MOUNT = "/srv/pbs-data"
SIZE = 549755813888


def disk(**changes):
    result = {
        "name": "vdb", "type": "disk", "serial": SERIAL, "size": SIZE,
        "fstype": None, "label": None, "uuid": None, "mountpoints": [None], "partuuid": None,
        "pkname": None, "children": None,
    }
    result.update(changes)
    return result


def document(*devices):
    return {"blockdevices": list(devices)}


def test_only_unique_disk_with_declared_virtual_serial_is_selected():
    selected = select_disk(document(disk(name="vda", serial="system-disk"), disk()), SERIAL, SIZE)
    assert selected["name"] == "vdb"
    with pytest.raises(Refusal, match="missing or ambiguous"):
        select_disk(document(disk(serial="other-disk")), SERIAL, SIZE)
    with pytest.raises(Refusal, match="missing or ambiguous"):
        select_disk(document(disk(), disk(name="vdc")), SERIAL, SIZE)


@pytest.mark.parametrize("changes", [
    {"size": SIZE - 1},
    {"type": "part", "pkname": "vda"},
    {"partuuid": "already-partitioned"},
    {"children": [{"name": "vdb1", "type": "part"}]},
    {"fstype": "xfs"},
    {"fstype": "ext4", "mountpoints": [None]},
    {"fstype": None, "mountpoints": ["/somewhere"]},
])
def test_mismatched_or_nonblank_disk_refuses_before_formatting(changes):
    candidate = disk(**changes)
    with pytest.raises(Refusal):
        state = disk_state(select_disk(document(candidate), SERIAL, SIZE), MOUNT)
        assert state == "blank"


def test_exact_existing_ext4_mount_is_idempotent_and_preserves_data():
    candidate = disk(fstype="ext4", label="pbs-data", uuid="7fe9b535-121c-46d7-93be-65563cd5fcac",
                     mountpoints=[MOUNT])
    selected = select_disk(document(candidate), SERIAL, SIZE)
    assert disk_state(selected, MOUNT) == "existing"
    assert selected["name"] == "vdb"


def test_rerun_resumes_only_our_unmounted_labeled_filesystem_after_interruption():
    candidate = disk(fstype="ext4", label="pbs-data", uuid="7fe9b535-121c-46d7-93be-65563cd5fcac")
    assert disk_state(select_disk(document(candidate), SERIAL, SIZE), MOUNT) == "existing_unmounted"
    candidate["mountpoints"] = ["/unexpected"]
    with pytest.raises(Refusal, match="unexpected path"):
        disk_state(select_disk(document(candidate), SERIAL, SIZE), MOUNT)


def test_duplicate_filesystem_uuid_refuses_uuid_mount_ambiguity():
    uuid = "7fe9b535-121c-46d7-93be-65563cd5fcac"
    with pytest.raises(Refusal, match="UUID is not unique"):
        select_disk(document(disk(fstype="ext4", label="pbs-data", uuid=uuid),
                             disk(name="vdc", serial="other-disk", uuid=uuid)), SERIAL, SIZE)


def test_cli_emits_only_selected_device_and_state_or_sanitized_refusal(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["prepare_pbs_guest_disk.py", "--expected-serial", SERIAL,
                                      "--expected-size", str(SIZE), "--mountpoint", MOUNT])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(document(disk()))))
    assert main() == 0
    assert json.loads(capsys.readouterr().out) == {"device": "/dev/vdb", "state": "blank"}

    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(document(disk(serial="unrelated-private-id")))))
    assert main() == 2
    output = capsys.readouterr()
    assert "unrelated-private-id" not in output.out + output.err
    assert "refused" in output.err


def test_workflow_checks_unique_identity_and_blank_signatures_before_any_mkfs():
    playbook_path = ROOT / "platform/playbooks/prepare-pbs-guest-data-disk.yml"
    plays = yaml.safe_load(playbook_path.read_text())
    assert plays[0]["ansible.builtin.import_playbook"] == "refuse-internal-extra-vars.yml"
    assert plays[1]["ansible.builtin.import_playbook"] == "require-reviewed-checkout.yml"
    assert plays[1]["vars"]["_reviewed_checkout_payload_paths"] == [
        "platform/playbooks/prepare-pbs-guest-data-disk.yml",
        "platform/playbooks/files/prepare_pbs_guest_disk.py",
        "platform/playbooks/attach-pbs-data-disk.yml",
        "platform/playbooks/tasks/validate-pbs-data-disk-vm-config.yml",
    ]
    pve_play = plays[3]
    assert pve_play["hosts"] == "localhost"
    pve_tasks = pve_play["tasks"]
    pve_reads = [task["ansible.builtin.uri"] for task in pve_tasks if "ansible.builtin.uri" in task]
    assert len(pve_reads) == 1
    assert pve_reads[0]["method"] == "GET"
    assert "/nodes/{{ _node | urlencode }}/qemu/{{ _vmid }}/config" in pve_reads[0]["url"]
    assert all(task.get("no_log") is True for task in pve_tasks if "ansible.builtin.uri" in task)
    assert "tasks/validate-pbs-data-disk-vm-config.yml" in str(pve_tasks)

    assert plays[4]["hosts"] == "pbs_svc"
    tasks = plays[4]["tasks"]
    names = [task["name"] for task in tasks]
    assert names.index("Select the unique disk carrying the declared Proxmox serial") < names.index(
        "Refuse signatures on the new disk before formatting"
    ) < names.index("Create ext4 on the proven blank guest virtual disk")
    assert names.index("Require an empty pre-existing mountpoint") < names.index(
        "Create ext4 on the proven blank guest virtual disk"
    )
    mkfs = next(task for task in tasks if task["name"].startswith("Create ext4"))
    assert mkfs["when"] == ["not ansible_check_mode", "_pbs_disk.state == 'blank'"]
    assert "{{ _pbs_disk.device }}" in str(mkfs["ansible.builtin.command"])
    mount = next(task for task in tasks if task["name"].startswith("Mount the guest filesystem"))
    assert mount["ansible.posix.mount"]["src"] == "UUID={{ _filesystem_uuid.stdout | trim }}"
    assert mount["ansible.posix.mount"]["path"] == "{{ _mountpoint }}"
    findmnt = next(task for task in tasks if task["name"].startswith("Refuse an existing unrelated mount"))
    assert findmnt["ansible.builtin.command"]["argv"] == [
        "findmnt", "--noheadings", "--mountpoint", "{{ _mountpoint }}", "--output", "SOURCE,FSTYPE"
    ]
    stat = next(task for task in tasks if task["name"].startswith("Inspect the existing mountpoint"))
    assert stat["ansible.builtin.stat"]["follow"] is False


def test_attachment_config_carries_stable_proxmox_serial_and_requires_it_on_reruns():
    attachment = yaml.safe_load((ROOT / "platform/playbooks/attach-pbs-data-disk.yml").read_text())
    vars_text = str(attachment[2]["vars"])
    assert "ac-pbs-data-{{ _vmid }}" in vars_text
    assert "serial={{ _expected_disk_serial }}" in vars_text
    assert "serial={{ _expected_disk_serial }}" in vars_text
    assert attachment[2]["vars"]["_disk_request"] == "{{ _storage }}:512,serial={{ _expected_disk_serial }}"

    pattern = attachment[2]["vars"]["_expected_disk_pattern"]
    import re
    assert re.search(pattern.replace("{{ _storage }}", "pbs-data").replace("{{ _vmid }}", "1234").replace(
        "{{ _expected_disk_serial }}", SERIAL
    ), "pbs-data:vm-1234-disk-1,serial=ac-pbs-data-1234,size=512G")
    assert re.search(pattern.replace("{{ _storage }}", "pbs-data").replace("{{ _vmid }}", "1234").replace(
        "{{ _expected_disk_serial }}", SERIAL
    ), "pbs-data:vm-1234-disk-1,size=512G,serial=ac-pbs-data-1234")
    assert not re.search(pattern.replace("{{ _storage }}", "pbs-data").replace("{{ _vmid }}", "1234").replace(
        "{{ _expected_disk_serial }}", SERIAL
    ), "pbs-data:vm-1234-disk-1,size=512G,serial=wrong")
