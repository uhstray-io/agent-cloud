"""Synthetic identity, blank-disk, refusal, and rerun checks for PBS guest storage."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "platform/playbooks/files"))

import prepare_pbs_guest_disk as guest_disk  # noqa: E402
from prepare_pbs_guest_disk import (  # noqa: E402
    Refusal,
    disk_state,
    ensure_datastore_path,
    main,
    select_disk,
    validate_datastore_path,
)

SERIAL = "ac-pbs-data-1234"
MOUNT = "/srv/pbs-data"
SIZE = 549755813888
EXPECTED_SELECTOR_DELEGATE = (
    "{{ ('localhost' if ansible_check_mode else inventory_hostname) "
    "| default(inventory_hostname) }}"
)


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


def test_datastore_creation_is_confined_and_exact_rerun_preserves_contents(tmp_path):
    mountpoint = tmp_path / "pbs-data"
    mountpoint.mkdir()
    datastore = mountpoint / "datastore"
    targets = [str(mountpoint)]

    assert ensure_datastore_path(str(mountpoint), str(datastore), targets) is True
    payload = datastore / "backup-index"
    payload.write_text("existing backup metadata")

    assert ensure_datastore_path(str(mountpoint), str(datastore), targets) is False
    assert payload.read_text() == "existing backup metadata"
    assert datastore.stat().st_dev == mountpoint.stat().st_dev


def test_datastore_symlink_refuses_without_changing_outside_target(tmp_path):
    mountpoint = tmp_path / "pbs-data"
    mountpoint.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    payload = outside / "protected"
    payload.write_text("leave untouched")
    payload.chmod(0o640)
    datastore = mountpoint / "datastore"
    datastore.symlink_to(outside, target_is_directory=True)

    with pytest.raises(Refusal, match="symlink"):
        ensure_datastore_path(str(mountpoint), str(datastore), [str(mountpoint)])

    assert payload.read_text() == "leave untouched"
    assert payload.stat().st_mode & 0o777 == 0o640


def test_nested_mount_below_datastore_refuses_before_any_directory_write(tmp_path):
    mountpoint = tmp_path / "pbs-data"
    mountpoint.mkdir()
    datastore = mountpoint / "datastore"
    nested = datastore / "child"
    targets = [str(mountpoint), str(nested)]

    with pytest.raises(Refusal, match="nested mount"):
        ensure_datastore_path(str(mountpoint), str(datastore), targets)

    assert not datastore.exists()


def test_existing_populated_mountpoint_children_and_symlinks_are_detectable(tmp_path):
    mountpoint = tmp_path / "mountpoint"
    mountpoint.mkdir()
    child = mountpoint / "existing-dir"
    child.mkdir()
    (child / "payload").write_text("keep")
    symlink = mountpoint / "existing-link"
    symlink.symlink_to(child, target_is_directory=True)

    found = {entry.name for entry in os.scandir(mountpoint)}
    assert found == {"existing-dir", "existing-link"}
    assert (child / "payload").read_text() == "keep"


def test_datastore_path_rejects_nonchild_and_nested_filesystem_paths(tmp_path):
    mountpoint = tmp_path / "pbs-data"
    mountpoint.mkdir()
    with pytest.raises(Refusal, match="outside"):
        validate_datastore_path(str(mountpoint), str(tmp_path / "pbs-data-escape"), [str(mountpoint)])

    datastore = mountpoint / "datastore"
    datastore.mkdir()
    with pytest.raises(Refusal, match="nested mount"):
        validate_datastore_path(
            str(mountpoint), str(datastore), [str(mountpoint), str(datastore / "child")]
        )


def test_datastore_cli_readback_reports_only_validated_state(monkeypatch, capsys, tmp_path):
    mountpoint = tmp_path / "pbs-data"
    mountpoint.mkdir()
    mount_tree = {"filesystems": [{"target": str(mountpoint), "children": []}]}
    monkeypatch.setattr(guest_disk.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stdout=json.dumps(mount_tree), stderr=""
    ))
    monkeypatch.setattr(sys, "argv", [
        "prepare_pbs_guest_disk.py", "--validate-datastore-path", "--mountpoint", str(mountpoint),
        "--datastore-path", str(mountpoint / "datastore"),
    ])

    assert main() == 0
    assert json.loads(capsys.readouterr().out) == {"datastore_path": "validated", "created": False}


def test_datastore_cli_refuses_nested_mount_without_disclosing_paths(monkeypatch, capsys, tmp_path):
    mountpoint = tmp_path / "pbs-data"
    mountpoint.mkdir()
    datastore = mountpoint / "datastore"
    mount_tree = {
        "filesystems": [{
            "target": str(mountpoint),
            "children": [{"target": str(datastore), "children": []}],
        }]
    }
    monkeypatch.setattr(guest_disk.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stdout=json.dumps(mount_tree), stderr=""
    ))
    monkeypatch.setattr(sys, "argv", [
        "prepare_pbs_guest_disk.py", "--ensure-datastore-path", "--mountpoint", str(mountpoint),
        "--datastore-path", str(datastore),
    ])

    assert main() == 2
    output = capsys.readouterr()
    assert "nested mount" in output.err
    assert str(tmp_path) not in output.err + output.out
    assert not datastore.exists()


def test_ensure_cli_rechecks_mount_tree_after_creating_datastore(monkeypatch, capsys, tmp_path):
    mountpoint = tmp_path / "pbs-data"
    mountpoint.mkdir()
    datastore = mountpoint / "datastore"
    mount_trees = iter([
        {"filesystems": [{"target": str(mountpoint), "children": []}]},
        {"filesystems": [{
            "target": str(mountpoint),
            "children": [{"target": str(datastore), "children": []}],
        }]},
    ])
    monkeypatch.setattr(guest_disk.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stdout=json.dumps(next(mount_trees)), stderr=""
    ))
    monkeypatch.setattr(sys, "argv", [
        "prepare_pbs_guest_disk.py", "--ensure-datastore-path", "--mountpoint", str(mountpoint),
        "--datastore-path", str(datastore),
    ])

    assert main() == 2
    output = capsys.readouterr()
    assert "nested mount" in output.err
    assert datastore.is_dir()
    assert list(datastore.iterdir()) == []


@pytest.mark.parametrize(("check_mode", "guest_helper_state"), [
    (False, "current"),
    (True, "absent"),
    (True, "outdated"),
])
def test_selector_task_sequence_preserves_result_in_normal_and_check_mode(
    check_mode, guest_helper_state, tmp_path
):
    ansible_playbook = shutil.which("ansible-playbook")
    if ansible_playbook is None:
        pytest.skip("needs ansible-playbook")
    playbook_path = ROOT / "platform/playbooks/prepare-pbs-guest-data-disk.yml"
    playbook = next(
        play for play in yaml.safe_load(playbook_path.read_text())
        if play.get("hosts") == "pbs_svc" and play.get("name") == "Prepare only the declared PBS guest's data disk"
    )
    tasks = playbook["tasks"]
    selector = next(
        task for task in tasks if task.get("name") == "Select the unique disk carrying the declared Proxmox serial"
    )
    assert_task = next(
        task for task in tasks if task.get("name") == "Require successful unique guest-disk identity validation"
    )
    set_fact_task = next(
        task for task in tasks if task.get("name") == "Save selected disk state without displaying raw device inventory"
    )
    assert selector["register"] == "_disk_selection"
    assert selector["delegate_to"] == EXPECTED_SELECTOR_DELEGATE
    assert selector["become"] == "{{ not ansible_check_mode }}"

    installed_helper = tmp_path / "guest-installed-helper.py"
    previous_bytes = None
    source_helper = ROOT / "platform/playbooks/files/prepare_pbs_guest_disk.py"
    if guest_helper_state == "current":
        installed_helper.write_bytes(source_helper.read_bytes())
    elif guest_helper_state == "outdated":
        previous_bytes = b"stale helper must not be executed"
        installed_helper.write_bytes(previous_bytes)
    assert (guest_helper_state == "absent") is (not installed_helper.exists())

    vars = {
        "_controller_helper": str(source_helper),
        "_helper": str(installed_helper),
        "_expected_serial": SERIAL,
        "_expected_bytes": SIZE,
        "_mountpoint": MOUNT,
        "_pbs_lsblk_inventory": {"stdout": json.dumps(document(disk()))},
    }
    # Preserve the real selector task's command, delegation, and register behavior while
    # omitting only its production privilege escalation in this synthetic local harness.
    harness_selector = {**selector}
    harness_selector.pop("become")
    test_play = [{
        "hosts": "localhost",
        "connection": "local",
        "gather_facts": False,
        "vars": vars,
        "tasks": [harness_selector, assert_task, set_fact_task, {
            "name": "Verify the selected disk result",
            "ansible.builtin.assert": {
                "that": "_pbs_disk == {'device': '/dev/vdb', 'state': 'blank'}",
                "quiet": True,
            },
        }],
    }]
    test_play_path = tmp_path / "selector-sequence.yml"
    test_play_path.write_text(yaml.safe_dump(test_play))
    command = [ansible_playbook, "-i", "localhost,", "-c", "local", str(test_play_path)]
    if check_mode:
        command.insert(1, "--check")
    env = {
        **os.environ,
        "ANSIBLE_LOCAL_TEMP": str(tmp_path / "ansible-tmp"),
        "ANSIBLE_NOCOLOR": "1",
        "ANSIBLE_STDOUT_CALLBACK": "default",
    }
    completed = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True,
                               check=False, timeout=60)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    if previous_bytes is not None:
        assert installed_helper.read_bytes() == previous_bytes
    assert "Verify the selected disk result" in completed.stdout


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
        "platform/playbooks/tasks/read-pbs-guest-disk-inventory.yml",
    ]
    pve_play = next(
        play for play in plays
        if play.get("name") == "Re-read and validate the Proxmox PBS VM disk identity"
    )
    assert pve_play["hosts"] == "localhost"
    pve_tasks = pve_play["tasks"]
    pve_reads = [task["ansible.builtin.uri"] for task in pve_tasks if "ansible.builtin.uri" in task]
    assert len(pve_reads) == 1
    assert pve_reads[0]["method"] == "GET"
    assert "/nodes/{{ _node | urlencode }}/qemu/{{ _vmid }}/config" in pve_reads[0]["url"]
    assert all(task.get("no_log") is True for task in pve_tasks if "ansible.builtin.uri" in task)
    assert "tasks/validate-pbs-data-disk-vm-config.yml" in str(pve_tasks)

    guest_play = next(play for play in plays if play.get("name") == "Prepare only the declared PBS guest's data disk")
    assert guest_play["hosts"] == "pbs_svc"
    tasks = guest_play["tasks"]
    names = [task["name"] for task in tasks]
    transfer = next(task for task in tasks if task["name"].startswith("Transfer the reviewed guest guard"))
    assert transfer["ansible.builtin.copy"]["src"] == "{{ _controller_helper }}"
    assert transfer["ansible.builtin.copy"]["dest"] == "{{ _helper }}"
    assert transfer["ansible.builtin.copy"]["owner"] == "root"
    assert transfer["ansible.builtin.copy"]["mode"] == "0700"
    source_digest = next(task for task in tasks if task["name"].startswith("Read the reviewed helper checksum"))
    installed_digest = next(task for task in tasks if task["name"].startswith("Require the installed helper"))
    assert source_digest["delegate_to"] == "localhost"
    assert source_digest["ansible.builtin.stat"]["checksum_algorithm"] == "sha256"
    assert "_guest_helper_stat.stat.checksum == _controller_helper_stat.stat.checksum" in installed_digest[
        "ansible.builtin.assert"]["that"]
    assert guest_play["vars"]["_helper"] == "/usr/local/lib/agent-cloud-prepare-pbs-guest-disk.py"
    lsblk_include = next(
        task for task in tasks if task["name"].startswith("Capture guest block-device inventory")
    )
    lsblk_task = yaml.safe_load(
        (ROOT / "platform/playbooks/tasks/read-pbs-guest-disk-inventory.yml").read_text()
    )[0]
    select = next(task for task in tasks if task["name"].startswith("Select the unique disk"))
    assert lsblk_include["ansible.builtin.include_tasks"] == "tasks/read-pbs-guest-disk-inventory.yml"
    assert lsblk_task["ansible.builtin.command"]["argv"] == [
        "lsblk", "--json", "--bytes", "--output",
        "NAME,TYPE,SERIAL,SIZE,FSTYPE,LABEL,UUID,MOUNTPOINTS,PARTUUID,PKNAME",
    ]
    assert lsblk_task["no_log"] is True
    assert select["ansible.builtin.command"]["stdin"] == "{{ _pbs_lsblk_inventory.stdout }}"
    assert select["no_log"] is True
    assert "{{ _controller_helper if ansible_check_mode else _helper }}" in str(
        select["ansible.builtin.command"]["argv"]
    )
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
    contents = next(task for task in tasks if task["name"].startswith("Inspect existing mountpoint contents"))
    assert contents["ansible.builtin.find"]["file_type"] == "any"
    assert contents["ansible.builtin.find"]["recurse"] is False
    datastore = next(task for task in tasks if task["name"].startswith("Create or verify the datastore"))
    assert "--ensure-datastore-path" in datastore["ansible.builtin.command"]["argv"]
    assert "ansible.builtin.file" not in datastore
    assert not any("ansible.builtin.script" in task for task in tasks)
    assert select["delegate_to"] == EXPECTED_SELECTOR_DELEGATE
    assert "_helper" in str(select["ansible.builtin.command"]["argv"])


def test_verify_tag_has_read_only_guest_path_and_required_prerequisites():
    plays = yaml.safe_load((ROOT / "platform/playbooks/prepare-pbs-guest-data-disk.yml").read_text())
    preflight = next(play for play in plays if play.get("name") == "Require the PBS guest declaration for verification")
    verify = next(play for play in plays if play.get("name") == "Verify the mounted PBS data disk without changing it")
    assert preflight["tags"] == ["verify"]
    assert verify["hosts"] == "pbs_svc" and verify["tags"] == ["verify"]
    assert not any(
        module in str(verify)
        for module in (
            "ansible.posix.mount", "ansible.builtin.file", "mkfs", "--ensure-datastore-path",
            "ansible.builtin.copy",
        )
    )
    assert any("serial-pinned disk" in task["name"] for task in verify["tasks"])
    assert any("UUID mount and fstab" in task["name"] for task in verify["tasks"])
    inventory = next(task for task in verify["tasks"] if task["name"].startswith("Capture guest block-device"))
    assert inventory["ansible.builtin.include_tasks"]["file"] == "tasks/read-pbs-guest-disk-inventory.yml"
    assert inventory["ansible.builtin.include_tasks"]["apply"]["tags"] == ["verify"]
    selector = next(task for task in verify["tasks"] if task["name"].startswith("Require the serial-pinned disk"))
    assert selector["ansible.builtin.command"]["stdin"] == "{{ _pbs_lsblk_inventory.stdout }}"


def test_verify_yaml_capture_argv_hands_canonical_inventory_to_real_helper(tmp_path):
    plays = yaml.safe_load((ROOT / "platform/playbooks/prepare-pbs-guest-data-disk.yml").read_text())
    verify = next(play for play in plays if play.get("name") == "Verify the mounted PBS data disk without changing it")
    inventory_include = next(task for task in verify["tasks"] if task["name"].startswith("Capture guest block-device"))
    inventory_task = yaml.safe_load(
        (ROOT / "platform/playbooks" / inventory_include["ansible.builtin.include_tasks"]["file"]).read_text()
    )[0]
    capture_argv = inventory_task["ansible.builtin.command"]["argv"]
    assert capture_argv == [
        "lsblk", "--json", "--bytes", "--output",
        "NAME,TYPE,SERIAL,SIZE,FSTYPE,LABEL,UUID,MOUNTPOINTS,PARTUUID,PKNAME",
    ]

    selector = next(task for task in verify["tasks"] if "--expected-serial" in str(task))
    context = {
        "_helper": str(ROOT / "platform/playbooks/files/prepare_pbs_guest_disk.py"),
        "_expected_serial": SERIAL,
        "_expected_bytes": SIZE,
        "_mountpoint": MOUNT,
    }
    from jinja2 import Environment

    render = Environment().from_string
    helper_argv = [render(str(arg)).render(context) for arg in selector["ansible.builtin.command"]["argv"]]
    assert helper_argv[0] == "python3"
    assert helper_argv[1] == context["_helper"]

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_lsblk = bin_dir / "lsblk"
    fake_lsblk.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "columns = sys.argv[sys.argv.index('--output') + 1].split(',')\n"
        "values = {'NAME': 'vdb', 'TYPE': 'disk', 'SERIAL': 'ac-pbs-data-1234',\n"
        "          'SIZE': 549755813888, 'FSTYPE': 'ext4', 'LABEL': 'pbs-data',\n"
        "          'UUID': '7fe9b535-121c-46d7-93be-65563cd5fcac',\n"
        "          'MOUNTPOINTS': ['/srv/pbs-data'], 'PARTUUID': None, 'PKNAME': None}\n"
        "print(json.dumps({'blockdevices': [{k.lower(): values[k] for k in columns}]}))\n"
    )
    fake_lsblk.chmod(0o755)
    captured = subprocess.run(
        capture_argv, capture_output=True, text=True,
        env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"},
        check=False,
    )
    assert captured.returncode == 0, captured.stderr
    selected = subprocess.run(
        helper_argv, input=captured.stdout, capture_output=True, text=True, check=False,
    )
    assert selected.returncode == 0, selected.stderr
    assert json.loads(selected.stdout) == {"device": "/dev/vdb", "state": "existing"}


def test_attachment_config_carries_stable_proxmox_serial_and_requires_it_on_reruns():
    attachment = yaml.safe_load((ROOT / "platform/playbooks/attach-pbs-data-disk.yml").read_text())
    attach_play = next(
        play for play in attachment if play.get("name") == "Attach the declared PBS guest data disk"
    )
    vars_text = str(attach_play["vars"])
    assert "ac-pbs-data-{{ _vmid }}" in vars_text
    assert "serial={{ _expected_disk_serial }}" in vars_text
    assert "serial={{ _expected_disk_serial }}" in vars_text
    assert attach_play["vars"]["_disk_request"] == "{{ _storage }}:512,serial={{ _expected_disk_serial }}"

    pattern = attach_play["vars"]["_expected_disk_pattern"]
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
