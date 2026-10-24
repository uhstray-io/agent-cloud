"""Pure tests for artifact admission and authenticated task evidence."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
import shutil
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parents[1] / "playbooks" / "files"))
sys.path.insert(0, str(Path(__file__).parent))
import harness_sandbox  # noqa: E402
import o11y_isolated_restore as restore  # noqa: E402
import o11y_pbs_receipts as pbs_receipts  # noqa: E402
import playbook_yaml  # noqa: E402
import seed_harness  # noqa: E402

PUBLIC_SHA = "a" * 40
PRIVATE_SHA = "b" * 40
PROJECT_ID = 7
NO_CLOUD_INIT = {
    "present": False,
    "field_fingerprint": hashlib.sha256(b"{}").hexdigest(),
    "scrub_keys": [],
}
BACKUP_UPID = "UPID:pve1:00000001:00000002:00000003:vzdump:120:root@pam:"
VERIFY_UPID = "UPID:pve1:00000004:00000005:00000006:pbs-verify:120:root@pam:"
SYNC_UPID = "UPID:pve-copy:00000007:00000008:00000009:pbs-sync:120:root@pam:"
CONTRACTS = restore.PREREQUISITE_CONTRACTS


def _chain_fixture(*, old_backup: bool = False):
    now = datetime.now(UTC).replace(microsecond=0)
    backup_epoch = int((now - timedelta(hours=25 if old_backup else 1)).timestamp())
    manifest = {
        "backup-type": "vm",
        "backup-id": "120",
        "backup-time": backup_epoch,
        "files": [
            {"filename": "drive-scsi0.img.fidx", "crypt-mode": "none", "size": 100, "csum": "1" * 64},
            {"filename": "qemu-server.conf.blob", "crypt-mode": "none", "size": 200, "csum": "2" * 64},
        ],
    }
    artifact = pbs_receipts.artifact_fingerprint(manifest)
    snapshots = {"backup_type": "vm", "backup_id": "120", "backup_time": backup_epoch}
    copy_store = "copy-store"
    copy_selector = {
        "volid": (
            f"{copy_store}:backup/vm/120/"
            f"{datetime.fromtimestamp(backup_epoch, UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}"
        ),
        "backend": "pbs",
        "format": "pbs-vm",
        "creation_time_epoch": backup_epoch,
        "size_bytes": 300,
    }
    destination_reader = {
        "api_host": "pbs-copy.example",
        "token_id": "o11y-copy-restore@pbs!pve-restore",
        "certificate_fingerprint": "c" * 64,
        "roles": ["Datastore.Audit", "Datastore.Read", "Datastore.Verify"],
        "datastore": "o11y",
        "propagate": False,
    }
    destination_storage = {
        "storage_id": copy_store,
        "server": "pbs-copy.example",
        "port": 8007,
        "datastore": "o11y",
        "username": destination_reader["token_id"],
        "fingerprint": destination_reader["certificate_fingerprint"],
        "content": "backup",
        "nodes": "copy-node",
    }
    evidence = [
        {
            "backup_upid": BACKUP_UPID,
            "snapshot": snapshots,
            "source_vmid": "120",
            "source_node": "pve1",
            "artifact_fingerprint": artifact,
            "playbook": pbs_receipts.CONTRACTS[CONTRACTS[0]]["playbook"],
        },
        {
            "backup_task_id": 100,
            "backup_upid": BACKUP_UPID,
            "verification_upid": VERIFY_UPID,
            "verification_status": "OK",
            "snapshot": snapshots,
            "source_vmid": "120",
            "artifact_fingerprint": artifact,
            "playbook": pbs_receipts.CONTRACTS[CONTRACTS[1]]["playbook"],
        },
        {
            "backup_task_id": 100,
            "verify_task_id": 101,
            "backup_upid": BACKUP_UPID,
            "verification_upid": VERIFY_UPID,
            "sync_upid": SYNC_UPID,
            "verification_status": "OK",
            "snapshot": snapshots,
            "artifact_fingerprint": artifact,
            "remove_vanished": False,
            "prune_enabled": False,
            "playbook": pbs_receipts.CONTRACTS[CONTRACTS[2]]["playbook"],
        },
        {
            "sync_task_id": 102,
            "snapshot": snapshots,
            "artifact_fingerprint": artifact,
            "readback_status": "readable",
            "acl_separation": "verified",
            "pull_direction": "destination_initiated",
            "deletion_isolation": "verified",
            "maintenance_isolation": "verified",
            "normalized_restore_locator": {**copy_selector, "storage_id": copy_store},
            "destination_identity_fingerprint": hashlib.sha256(json.dumps(destination_reader).encode()).hexdigest(),
            "destination_storage_config_fingerprint": hashlib.sha256(
                json.dumps(destination_storage).encode()
            ).hexdigest(),
            "playbook": pbs_receipts.CONTRACTS[CONTRACTS[3]]["playbook"],
        },
    ]
    task_proofs = {}
    launches = {}
    task_ids = {}
    chain_ids = (100, 101, 102, 103)
    for index, (task_id, contract, items) in enumerate(zip(chain_ids, CONTRACTS, evidence, strict=True)):
        started = now - timedelta(minutes=20 - task_id % 10)
        ended = started + timedelta(minutes=3)
        launch = {"expected_repository_sha": PUBLIC_SHA, "site_config_base_sha": PRIVATE_SHA}
        if index >= 1:
            launch["backup_task_id"] = chain_ids[0]
        if index >= 2:
            launch["verify_task_id"] = chain_ids[1]
        if index >= 3:
            launch["sync_task_id"] = chain_ids[2]
        body = pbs_receipts.seal_receipt(
            {
                "schema_version": 1,
                "contract": contract,
                "status": "ready" if contract == CONTRACTS[3] else "complete",
                "reason": "destination_copy_ready_for_isolated_restore" if contract == CONTRACTS[3] else "none",
                "public_sha": PUBLIC_SHA,
                "private_sha": PRIVATE_SHA,
                "inventory_provenance": "exact_bytes_match",
                "inventory_fingerprint": "3" * 64,
                "source_fingerprint": "4" * 64,
                "artifact_fingerprint": artifact,
                "observed_start": started.isoformat().replace("+00:00", "Z"),
                "observed_end": ended.isoformat().replace("+00:00", "Z"),
                "role_context": pbs_receipts.CONTRACTS[contract]["roles"],
                "evidence": items,
            }
        )
        task = {
            "id": task_id,
            "project_id": PROJECT_ID,
            "template_id": 55,
            "commit_hash": PUBLIC_SHA,
            "status": "success",
            "created": (started - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
            "start": started.isoformat().replace("+00:00", "Z"),
            "end": ended.isoformat().replace("+00:00", "Z"),
            "environment": json.dumps(launch),
            "playbook": "",
            "params": {"diff": False, "dry_run": False},
        }
        task_proofs[contract] = {
            "task": {
                "id": task_id,
                "project_id": PROJECT_ID,
                "template_id": task["template_id"],
                "commit_hash": PUBLIC_SHA,
                "status": "success",
                "created": task["created"],
                "start": task["start"],
                "end": task["end"],
                "launch": launch,
            },
            "receipt": body,
        }
        launches[contract] = launch
        task_ids[contract] = task_id
    return {
        "public_sha": PUBLIC_SHA,
        "private_sha": PRIVATE_SHA,
        "expected_project_id": PROJECT_ID,
        "expected_task_ids": task_ids,
        "expected_launches": launches,
        "task_proofs": task_proofs,
        "artifact_fingerprint": artifact,
        "expected_copy_storage_id": copy_store,
    }


def test_exact_artifact_config_requires_source_vmid_included_disks_and_hashes():
    config = {
        "status": 200,
        "data": (
            "vmid: 700\nagent: enabled=1\n"
            "scsi0: pbs:backup/vm/700,backup=1,size=20G\n"
            "net0: virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0\n"
        ),
    }
    result = restore.exact_artifact_config(config, source_vmid=700)
    assert result["disk_count"] == 1
    assert result["nic_keys"] == ["net0"]
    assert result["config_sha256"] == hashlib.sha256(config["data"].encode()).hexdigest()
    assert set(result) == {"config_sha256", "disk_count", "disks", "nic_count", "nic_keys", "config_keys_sha256"}


def test_provisioner_cloud_init_is_separate_from_included_disk_layout():
    config = {
        "status": 200,
        "data": (
            "vmid: 700\nagent: 1\nciuser: observer\nsshkeys: ssh-ed25519%20synthetic\n"
            "ipconfig0: ip=dhcp\nnameserver: 192.0.2.53\n"
            "smbios1: uuid=00000000-0000-4000-8000-000000000700\n"
            "serial0: socket\nmeta: creation-qemu=8.1.5,ctime=1714504207\n"
            "ide2: source-lvm:vm-700-cloudinit,media=cdrom\n"
            "scsi0: pbs:backup/vm/700,backup=1,size=20G\n"
        ),
    }
    layout = restore.exact_artifact_config(config, source_vmid=700)
    cloud_init = restore.exact_artifact_cloud_init(config, source_vmid=700)

    assert layout["disk_count"] == 1
    assert layout["disks"] == [{"device": "scsi0", "size_bytes": 20 * 1024**3}]
    assert cloud_init["present"] is True
    assert cloud_init["scrub_keys"] == ["ciuser", "ipconfig0", "nameserver", "smbios1", "sshkeys"]
    assert cloud_init["field_fingerprint"] == hashlib.sha256(
        json.dumps(
            {
                "ciuser": "observer",
                "ipconfig0": "ip=dhcp",
                "nameserver": "192.0.2.53",
                "smbios1": "uuid=00000000-0000-4000-8000-000000000700",
                "sshkeys": "ssh-ed25519%20synthetic",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


@pytest.mark.parametrize(
    ("line", "reason"),
    [
        ("ide2: local:iso/debian.iso,media=cdrom", "artifact_unsupported"),
        ("ide2: local:vm-701-cloudinit,media=cdrom", "artifact_unsupported"),
        ("ide2: local:vm-700-cloudinit.qcow2,media=cdrom", "artifact_unsupported"),
        ("ide2: local:vm-700-cloudinit,media=cdrom,size=4M", "artifact_unsupported"),
        ("ide2: none,media=cdrom\nciuser: observer", "artifact_unsupported"),
        ("ipconfig1: ip=dhcp", "artifact_unsupported"),
        ("smbios1: uuid=invalid", "artifact_unsupported"),
        ("serial0: tcp", "artifact_unsupported"),
        ("serial1: socket", "artifact_unsupported"),
        ("meta: creation-qemu=8.1.5,ctime=1714504207,unreviewed=value", "artifact_unsupported"),
        ("meta: ctime=1714504207,creation-qemu=8.1.5", "artifact_unsupported"),
        ("metadata: creation-qemu=8.1.5,ctime=1714504207", "artifact_unsupported"),
        ("cicustom: user=local:snippets/user.yaml", "artifact_unsupported"),
    ],
)
def test_unreviewed_cloud_init_and_cdrom_shapes_refuse_before_mutation(line, reason):
    base = "vmid: 700\nagent: enabled=1\nscsi0: pbs:backup/vm/700,backup=1,size=20G\n"
    with pytest.raises(restore.Refusal, match=reason):
        restore.exact_artifact_config({"status": 200, "data": base + line + "\n"}, source_vmid=700)


def test_target_auxiliary_must_be_regenerated_for_target_store_and_vmid():
    config = {
        "status": 200,
        "data": (
            "vmid: 700\nagent: enabled=1\nciuser: observer\n"
            "ide2: source-lvm:vm-700-cloudinit,media=cdrom\n"
            "serial0: socket\nmeta: creation-qemu=8.1.5,ctime=1714504207\n"
            "scsi0: pbs:backup/vm/700,backup=1,size=20G\n"
        ),
    }
    expectation = restore.exact_artifact_cloud_init(config, source_vmid=700)
    expected_fields = {
        "ciuser": "observer",
        "ide2": "target-lvm:vm-910001-cloudinit,media=cdrom",
        "onboot": 0,
        "description": "o11y-restore:abc",
        "scsi0": "target-lvm:vm-910001-disk-0,size=20G",
        "vmid": 910001,
        "serial0": "socket",
        "meta": "creation-qemu=8.1.5,ctime=1714504207",
    }
    admitted = {
        "status": 200,
        "data": {**expected_fields, "digest": "a" * 40},
    }
    assert restore.validate_target_config(
        admitted,
        target_vmid=910001,
        marker=None,
        isolated=False,
        expected_disk_keys=["scsi0"],
        storage_id="target-lvm",
        cloud_init_expectation=expectation,
    )["cloud_init_present"] is True

    for replacement in (
        "source-lvm:vm-700-cloudinit,media=cdrom",
        "target-lvm:vm-910002-cloudinit,media=cdrom",
        "other-lvm:vm-910001-cloudinit,media=cdrom",
    ):
        invalid = copy.deepcopy(admitted)
        invalid["data"]["ide2"] = replacement
        with pytest.raises(restore.Refusal, match="target_config_invalid"):
            restore.validate_target_config(
                invalid,
                target_vmid=910001,
                marker=None,
                isolated=False,
                expected_disk_keys=["scsi0"],
                storage_id="target-lvm",
                cloud_init_expectation=expectation,
            )


def test_isolated_target_keeps_target_auxiliary_but_has_no_source_cloud_init_metadata():
    config = {
        "status": 200,
        "data": {
            "vmid": 910001,
            "digest": "a" * 40,
            "onboot": 0,
            "description": "o11y-restore:abc",
            "scsi0": "target-lvm:vm-910001-disk-0,size=20G",
            "ide2": "target-lvm:vm-910001-cloudinit,media=cdrom",
        },
    }
    expectation = {
        "present": True,
        "field_fingerprint": hashlib.sha256(b"{\"ciuser\":\"observer\"}").hexdigest(),
        "scrub_keys": ["ciuser"],
    }
    assert restore.validate_target_config(
        config,
        target_vmid=910001,
        marker="o11y-restore:abc",
        isolated=True,
        expected_disk_keys=["scsi0"],
        storage_id="target-lvm",
        cloud_init_expectation=expectation,
    )["cloud_init_present"] is True
    config["data"]["sshkeys"] = "must-be-removed"
    with pytest.raises(restore.Refusal, match="target_config_invalid"):
        restore.validate_target_config(
            config,
            target_vmid=910001,
            marker="o11y-restore:abc",
            isolated=True,
            expected_disk_keys=["scsi0"],
            storage_id="target-lvm",
            cloud_init_expectation=expectation,
        )


@pytest.mark.parametrize(
    "line, reason",
    [
        ("hookscript: local:snippets/restore-hook.pl", "artifact_unsupported"),
        ("args: -device host-pci", "artifact_unsupported"),
        ("hostpci0: 0000:03:00.0", "artifact_unsupported"),
        ("usb0: host=1-1", "artifact_unsupported"),
        ("virtiofs0: shared", "artifact_unsupported"),
        ("mp0: /srv/share,mp=/host", "artifact_unsupported"),
        ("smbios1: uuid=abc", "artifact_unsupported"),
        ("cicustom: user=local:snippets/custom.yaml", "artifact_unsupported"),
        ("unused0: local:orphan-disk", "artifact_unsupported"),
        ("ide2: local:iso/debian.iso,media=cdrom", "artifact_unsupported"),
        ("scsi1: pbs:archive,size=20G,backup=0", "artifact_disk_incomplete"),
        ("scsi0: pbs:archive,size=20G,backup=0\nscsi0: pbs:archive,size=20G,backup=1", "artifact_config_invalid"),
    ],
)
def test_unsafe_passthrough_shared_hooks_or_excluded_disks_refuse(line, reason):
    base = "vmid: 700\nagent: enabled=1\nscsi0: pbs:backup/vm/700,backup=1,size=20G\n"
    assert restore.exact_artifact_config({"status": 200, "data": base}, source_vmid=700)["disk_count"] == 1
    config = {"status": 200, "data": base + line + "\n"}
    with pytest.raises(restore.Refusal, match=reason):
        restore.exact_artifact_config(config, source_vmid=700)


def test_artifact_config_requires_exact_source_identity_and_bounded_bytes():
    with pytest.raises(restore.Refusal):
        restore.exact_artifact_config(
            {"status": 200, "data": "vmid: 701\nagent: enabled=1\nscsi0: pbs:backup,size=20G\n"}, source_vmid=700
        )
    with pytest.raises(restore.Refusal):
        restore.exact_artifact_config({"status": 200, "data": "x" * (256 * 1024 + 1)}, source_vmid=700)


def test_target_readback_requires_disconnected_owned_stopped_config():
    good = {
        "status": 200,
        "data": {
            "vmid": 910001,
            "digest": "a" * 40,
            "onboot": 0,
            "description": "o11y-restore:abc",
            "scsi0": "local:disk",
        },
    }
    assert (
        restore.validate_target_config(
            good,
            target_vmid=910001,
            marker="o11y-restore:abc",
            isolated=True,
            expected_disk_keys=["scsi0"],
            storage_id="local",
            cloud_init_expectation=NO_CLOUD_INIT,
        )["nic_count"]
        == 0
    )
    restored = copy.deepcopy(good)
    restored["data"]["description"] = "source snapshot description"
    restored["data"]["onboot"] = 1
    restored["data"]["startup"] = "order=1"
    assert restore.validate_target_config(
        restored,
        target_vmid=910001,
        marker=None,
        isolated=False,
        expected_disk_keys=["scsi0"],
        storage_id="local",
        cloud_init_expectation=NO_CLOUD_INIT,
    )["nic_count"] == 0
    for key, value in (
        ("net0", "virtio=aa,bridge=vmbr0"),
        ("serial0", "tcp"),
        ("serial1", "socket"),
        ("meta", "creation-qemu=8.1.5,ctime=1714504207,unexpected=1"),
        ("hostpci0", "0000:02:00.0"),
        ("onboot", 1),
        ("startup", "order=1"),
        ("description", "other"),
        ("unused0", "local:orphan-disk"),
        ("scsi1", "local:unexpected-disk"),
        ("scsi0", "shared:disk"),
        ("current", 1),
        ("agent-enabled", 1),
        ("digest", "not-a-pve-sha1"),
    ):
        bad = copy.deepcopy(good)
        bad["data"][key] = value
        with pytest.raises(restore.Refusal):
            restore.validate_target_config(
                bad,
                target_vmid=910001,
                marker="o11y-restore:abc",
                isolated=True,
                expected_disk_keys=["scsi0"],
                storage_id="local",
                cloud_init_expectation=NO_CLOUD_INIT,
            )


@pytest.mark.parametrize(
    "status",
    [
        {"exited": True, "exitcode": 1, "out-data": '{"status":"verified"}'},
        {"exited": False, "exitcode": 0, "out-data": '{"status":"verified"}'},
        {"exited": True, "exitcode": 0, "out-truncated": True, "out-data": '{"status":"verified"}'},
        {"exited": True, "exitcode": 0, "out-truncated": 0.0, "out-data": '{"status":"verified"}'},
        {"exited": True, "exitcode": 0, "out-data": "eyJzdGF0dXMiOiJ2ZXJpZmllZCJ9"},
        {"exited": True, "exitcode": 0, "out-data": "[]"},
    ],
)
def test_qga_status_rejects_failed_truncated_or_encoded_output(status):
    with pytest.raises(restore.Refusal, match="guest_exec_output_invalid"):
        restore.parse_guest_exec_status(status)


def test_qga_status_parses_pve_decoded_output_and_fingerprints_exact_bytes():
    output = '{"status":"verified","volume_count":5}'
    parsed = restore.parse_guest_exec_status({"exited": True, "exitcode": 0, "out-data": output})
    assert parsed["receipt"] == {"status": "verified", "volume_count": 5}
    assert parsed["output_sha256"] == hashlib.sha256(output.encode()).hexdigest()


def test_restore_playbook_adapters_match_closed_helpers_and_pve_qga_shape():
    path = Path(__file__).parents[1] / "playbooks" / "restore-o11y-isolated.yml"
    tasks = list(playbook_yaml.tasks(playbook_yaml.load(path)))
    by_name = {task.get("name"): task for task in tasks if task.get("name")}

    vmid = 120
    source_node = "source-node"
    chain_input = _chain_fixture()
    readiness = chain_input["task_proofs"][CONTRACTS[3]]["receipt"]["evidence"]
    destination_storage = readiness["normalized_restore_locator"]["storage_id"]
    destination_reader_fingerprint = "c" * 64
    destination_storage_config = {
        "server": "pbs-copy.example",
        "port": 8007,
        "datastore": "o11y",
        "username": "o11y-copy-restore@pbs!pve-restore",
        "fingerprint": destination_reader_fingerprint,
        "content": "backup",
        "nodes": "copy-node",
    }
    selector = {
        key: readiness["normalized_restore_locator"][key]
        for key in ("volid", "backend", "format", "creation_time_epoch", "size_bytes")
    }
    candidate = {
        "vmid": vmid,
        "volid": selector["volid"],
        "format": selector["format"],
        "size": selector["size_bytes"],
        "ctime": selector["creation_time_epoch"],
    }
    variables = {
        "ansible_connection": "local",
        "ansible_python_interpreter": sys.executable,
        "_restore_chain_input": chain_input,
        "_restore_chain_raw": {
            "rc": 0,
            "stdout": json.dumps(restore.verify_prerequisite_chain(chain_input)),
        },
        "_restore_source_vmid": str(vmid),
        "_restore_source_node": source_node,
        "_restore_vms": {"json": {"data": [{"type": "qemu", "vmid": vmid, "node": source_node}]}},
        "_restore_copy_storage_id": destination_storage,
        "_restore_copy_api_host": "pbs-copy.example",
        "_restore_copy_token_id": "o11y-copy-restore@pbs!pve-restore",
        "_restore_copy_fingerprint": destination_reader_fingerprint,
        "_restore_copy_pve_storage": {"json": {"data": destination_storage_config}},
        "_restore_copy_content": {"status": 200, "json": {"data": [candidate]}},
        "_restore_source_config": {
            "status": 200,
            "json": {"data": {"scsi0": "copy-store:vm-120-disk-0,size=20G"}},
        },
        "_restore_target_preflight": {
            "restore_disk_count": 1,
            "layout": {"disk_count": 99},
            "selected_candidate": {"backend_class": "pbs"},
        },
        "_restore_artifact_admission": {"rc": 0, "stdout": '{"disk_count":1}'},
        "_restore_cloud_init_admission": {
            "rc": 0,
                "stdout": json.dumps(restore.exact_artifact_cloud_init(
                {"status": 200, "data": "vmid: 120\nagent: enabled=1\nscsi0: copy-store:disk,size=20G\n"},
                source_vmid=120,
            )),
        },
        "_restore_guest_exec_status": {
            "json": {
                "data": {
                    "exited": True,
                    "exitcode": 0,
                    "out-data": json.dumps({
                        "schema_version": 1,
                        "status": "verified",
                        "reason": "restored_historical_data_verified",
                        "volume_count": 5,
                        "backend_count": 4,
                        "grafana_integrity": "ok",
                    }, separators=(",", ":")),
                },
            },
        },
        "_restore_pve_host": "http://127.0.0.1",
        "_restore_target_node": "isolated-node",
        "_restore_target_vmid": 910001,
        "_restore_pve_token_id": "synthetic@pve!restore",
        "_restore_pve_token": "synthetic-pve-token",
        "_restore_checker_expectations": {"synthetic": "fixture"},
    }
    expected_checker_source = (
        Path(__file__).parents[1] / "playbooks/files/inspect_o11y_restored_guest.py"
    ).read_text().rstrip()
    expected_checker_sha = hashlib.sha256(expected_checker_source.encode()).hexdigest()
    checker_program = by_name["Send checker source and fixed expectations only through QGA input-data"]
    qga_body = checker_program["ansible.builtin.uri"]["body"]
    expected_command = qga_body["command"][2]

    class SyntheticPve(seed_harness.FakeBao):
        observed = []

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode()
            fields = parse_qs(raw, keep_blank_values=True)
            command = fields.get("command", [])
            body = fields.get("input-data", [])
            accepted = (
                self.path == "/api2/json/nodes/isolated-node/qemu/910001/agent/exec"
                and self.headers.get("Authorization") == "PVEAPIToken=synthetic@pve!restore=synthetic-pve-token"
                and set(fields) == {"command", "input-data"}
                and len(command) == 4
                and command[:3] == ["/usr/bin/python3", "-c", expected_command]
                and len(body) == 1
            )
            if accepted:
                try:
                    input_data = json.loads(body[0])
                    source = base64.b64decode(input_data["source"], validate=True).decode()
                    accepted = (
                        source == expected_checker_source
                        and hashlib.sha256(source.encode()).hexdigest() == command[3] == expected_checker_sha
                        and input_data["expectations"] == variables["_restore_checker_expectations"]
                    )
                except (KeyError, ValueError, UnicodeError, json.JSONDecodeError):
                    accepted = False
            type(self).observed.append(
                {"path": self.path, "auth": self.headers.get("Authorization"), "fields": fields}
            )
            self.reply({"data": {"pid": 1234}} if accepted else {"errors": ["invalid QGA form"]},
                       status=200 if accepted else 400)

    task_names = (
        "Validate all authenticated predecessor links and readiness locator",
        "Require the authenticated task chain and fixed 24-hour freshness",
        "Retain the validated chain and private artifact fingerprint",
        "Bind current Reader identity and PVE storage to authenticated readiness",
        "Require current Reader identity and PVE storage to match readiness fingerprints",
        "Bind one fresh destination candidate to authenticated readiness locator",
        "Require one fresh protected candidate and read online node/storage state",
        "Require complete included disks, safe config, and live isolation preflight",
        "Build and pin the reviewable QGA checker input bytes",
        "Send checker source and fixed expectations only through QGA input-data",
        "Require the actual QGA process ID before polling",
        "Validate decoded QGA status and parse bounded checker receipt",
        "Require valid successful QGA checker output",
        "Retain the bounded content-free guest verification receipt",
        "Require the exact five-volume and historical backend proof",
    )
    selected_tasks = [by_name[name] for name in task_names]

    with tempfile.TemporaryDirectory(prefix="o11y-restore-adapters-") as scratch:
        tmp_path = Path(scratch)
        helper_dir = tmp_path / "files"
        helper_dir.mkdir()
        for helper in (
            "o11y_isolated_restore.py",
            "o11y_restore_cloud_init.py",
            "o11y_restore_coordination.py",
            "o11y_pbs_receipts.py",
            "inspect_o11y_backup_artifact.py",
            "inspect_o11y_restored_guest.py",
        ):
            shutil.copyfile(Path(__file__).parents[1] / "playbooks/files" / helper, helper_dir / helper)
        play = [{"name": "Exercise selected restore adapters", "hosts": "localhost", "gather_facts": False,
                 "connection": "local", "vars": variables, "tasks": selected_tasks}]
        play_path = tmp_path / "adapter.yml"
        SyntheticPve.observed.clear()
        with seed_harness.serve(SyntheticPve) as address:
            variables["_restore_pve_host"] = address
            play_path.write_text(yaml.safe_dump(play, sort_keys=False))
            result = harness_sandbox.run(
                ["ansible-playbook", "-i", "localhost,", str(play_path)],
                tmp_path,
                cwd=playbook_yaml.REPO,
                env=harness_sandbox.env_for(tmp_path),
            )

    assert result.returncode == 0, result.stdout + result.stderr
    assert len(SyntheticPve.observed) == 1
    request = SyntheticPve.observed[0]
    assert request["path"] == "/api2/json/nodes/isolated-node/qemu/910001/agent/exec"
    assert request["auth"] == "PVEAPIToken=synthetic@pve!restore=synthetic-pve-token"
    assert request["fields"]["command"] == ["/usr/bin/python3", "-c", expected_command, expected_checker_sha]
    assert set(request["fields"]) == {"command", "input-data"}
    input_data = json.loads(request["fields"]["input-data"][0])
    checker_source = base64.b64decode(input_data["source"]).decode()
    assert checker_source == expected_checker_source
    assert hashlib.sha256(checker_source.encode()).hexdigest() == expected_checker_sha
    assert input_data["expectations"] == variables["_restore_checker_expectations"]


def test_restore_live_storage_fingerprint_refuses_readiness_drift():
    path = Path(__file__).parents[1] / "playbooks" / "restore-o11y-isolated.yml"
    tasks = {task.get("name"): task for task in playbook_yaml.tasks(playbook_yaml.load(path))}
    selected = [
        tasks["Bind current Reader identity and PVE storage to authenticated readiness"],
        tasks["Require current Reader identity and PVE storage to match readiness fingerprints"],
    ]
    chain = restore.verify_prerequisite_chain(_chain_fixture())
    variables = {
        "ansible_connection": "local",
        "_restore_chain": chain,
        "_restore_copy_api_host": "pbs-copy.example",
        "_restore_copy_storage_id": "copy-store",
        "_restore_copy_token_id": "o11y-copy-restore@pbs!pve-restore",
        "_restore_copy_fingerprint": "c" * 64,
        "_restore_copy_pve_storage": {
            "json": {
                "data": {
                    "server": "pbs-copy.example",
                    "port": 8007,
                    "datastore": "o11y",
                    "username": "o11y-copy-restore@pbs!pve-restore",
                    "fingerprint": "c" * 64,
                    "content": "backup",
                    "nodes": "copy-node",
                }
            }
        },
    }
    with tempfile.TemporaryDirectory(prefix="o11y-ready-fingerprint-") as scratch:
        tmp_path = Path(scratch)

        def run():
            play_path = tmp_path / "fingerprint.yml"
            play_path.write_text(yaml.safe_dump([{
                "name": "Check authenticated destination fingerprints",
                "hosts": "localhost",
                "gather_facts": False,
                "connection": "local",
                "vars": variables,
                "tasks": selected,
            }], sort_keys=False))
            return harness_sandbox.run(
                ["ansible-playbook", "-i", "localhost,", str(play_path)],
                tmp_path,
                cwd=playbook_yaml.REPO,
                env=harness_sandbox.env_for(tmp_path),
            )

        matching = run()
        assert matching.returncode == 0, matching.stdout + matching.stderr
        variables["_restore_chain"] = dict(chain, destination_storage_config_fingerprint="0" * 64)
        drifted = run()
        assert drifted.returncode != 0


def test_reviewed_restore_and_cleanup_payloads_pin_receipt_dependency():
    playbooks = Path(__file__).parents[1] / "playbooks"
    for name in ("restore-o11y-isolated.yml", "cleanup-o11y-isolated-restore.yml"):
        tasks = list(playbook_yaml.tasks(playbook_yaml.load(playbooks / name)))
        reviewed = next(task for task in tasks if task.get("name", "").startswith("Require the exact reviewed"))
        paths = reviewed["vars"]["_reviewed_checkout_payload_paths"]
        assert "platform/playbooks/files/o11y_pbs_receipts.py" in paths
        assert len(paths) <= 16


def test_task_proof_adapter_delegates_only_the_authenticated_closed_fields(monkeypatch):
    proof = {
        "task": {"id": 123},
        "output_rows": [{"task_id": 123, "id": 1}],
        "contract": "o11y-pbs-backup/v1",
        "public_sha": "a" * 40,
        "private_sha": "b" * 40,
        "playbook": "platform/playbooks/backup-o11y-to-pbs.yml",
        "expected_project_id": 7,
        "expected_task_id": 123,
        "expected_launch": {"expected_repository_sha": "a" * 40},
        "expected_artifact_fingerprint": None,
    }
    captured = {}

    def validate(task, output_rows, **kwargs):
        captured.update(task=task, output_rows=output_rows, **kwargs)
        return {"receipt": {"contract": kwargs["contract"]}, "task_metadata": {"id": task["id"]}}

    monkeypatch.setattr(restore, "validate_completed_task", validate)
    assert restore.verify_task_proof(proof) == {
        "receipt": {"contract": "o11y-pbs-backup/v1"},
        "task_metadata": {"id": 123},
    }
    assert captured["expected_project_id"] == 7
    assert captured["expected_task_id"] == 123
    forged = dict(proof, repository={"git_url": "caller supplied"})
    with pytest.raises(restore.Refusal):
        restore.verify_task_proof(forged)


def test_restore_task_proof_selects_restore_contract_and_rejects_receipt_forgery(monkeypatch):
    launch = {
        "expected_repository_sha": PUBLIC_SHA,
        "site_config_base_sha": PRIVATE_SHA,
        "backup_task_id": 100,
        "pbs_verify_task_id": 101,
        "copy_sync_task_id": 102,
        "copy_ready_task_id": 103,
    }
    nested = {"phase": "awaiting_receipt", "active_nonce": None, "pending": None, "restore_nonce": "n" * 64}
    receipt = {
        "type": "restore_receipt/v3",
        "receipt_fingerprint": "c" * 64,
        "bindings": {"public_sha": PUBLIC_SHA, "private_inventory_sha": PRIVATE_SHA},
        "coordination_record": nested,
        "coordination_version": 12,
    }
    task = {
        "id": 104,
        "project_id": PROJECT_ID,
        "environment": launch,
    }
    rows = [{"id": 1, "task_id": 104}]
    observed = {}

    def validate(task_arg, output_rows, **kwargs):
        observed.update(task=task_arg, output_rows=output_rows, **kwargs)
        return {"receipt": copy.deepcopy(receipt), "task_metadata": {"id": 104}}

    def validate_nested(evidence, prior_record, expected_version):
        observed["coordination"] = (evidence, prior_record, expected_version)
        return receipt["receipt_fingerprint"]

    monkeypatch.setattr(restore, "validate_completed_output", validate)
    monkeypatch.setattr(restore.restore_coordination, "_restore_receipt", validate_nested)
    proof = {
        "task": task,
        "output_rows": rows,
        "public_sha": PUBLIC_SHA,
        "expected_project_id": PROJECT_ID,
        "expected_task_id": 104,
        "expected_template_id": 55,
        "expected_launch": launch,
        "selector": "type",
        "expected_selector": "restore_receipt/v3",
    }

    result = restore.verify_restore_task_proof(proof)
    assert result["receipt"] == receipt
    assert observed["expected_playbook"] == "platform/playbooks/restore-o11y-isolated.yml"
    assert observed["allowed_launch_keys"] == restore.RESTORE_TASK_LAUNCH_KEYS
    assert observed["coordination"][1]["phase"] == "restore_active"
    assert observed["coordination"][2] == 12

    bad_receipt = copy.deepcopy(receipt)
    bad_receipt["bindings"]["private_inventory_sha"] = "d" * 40
    monkeypatch.setattr(
        restore,
        "validate_completed_output",
        lambda *args, **kwargs: {"receipt": bad_receipt, "task_metadata": {"id": 104}},
    )
    with pytest.raises(restore.Refusal, match="restore_receipt_invalid"):
        restore.verify_restore_task_proof(proof)

    extra_launch = dict(launch, target_vmid=910001)
    forged = dict(proof, expected_launch=extra_launch)
    with pytest.raises(restore.Refusal, match="restore_receipt_invalid"):
        restore.verify_restore_task_proof(forged)

    caller_receipt = dict(proof, expected_receipt_fingerprint="c" * 64)
    with pytest.raises(restore.Refusal, match="invalid_input"):
        restore.verify_restore_task_proof(caller_receipt)


def test_prerequisite_chain_authenticates_task_order_links_and_artifact():
    result = restore.verify_prerequisite_chain(_chain_fixture())
    assert result["status"] == "verified"
    assert result["task_ids"] == [100, 101, 102, 103]
    assert result["task_metadata"][CONTRACTS[3]]["project_id"] == PROJECT_ID
    assert result["normalized_restore_locator"]["storage_id"] == "copy-store"
    assert re.fullmatch(r"[0-9a-f]{64}", result["destination_identity_fingerprint"])


def test_prerequisite_chain_rejects_link_forgery_duplicate_tasks_and_stale_artifact():
    broken_link = _chain_fixture()
    broken_link["task_proofs"][CONTRACTS[2]]["receipt"]["evidence"]["verify_task_id"] = 999
    with pytest.raises(restore.Refusal):
        restore.verify_prerequisite_chain(broken_link)

    repeated_task = _chain_fixture()
    repeated_task["expected_task_ids"][CONTRACTS[3]] = 102
    with pytest.raises(restore.Refusal):
        restore.verify_prerequisite_chain(repeated_task)

    stale = _chain_fixture(old_backup=True)
    with pytest.raises(restore.Refusal, match="artifact_stale"):
        restore.verify_prerequisite_chain(stale)


def test_prerequisite_chain_uses_only_the_authenticated_ready_locator_and_storage():
    mismatch = _chain_fixture()
    ready_receipt = mismatch["task_proofs"][CONTRACTS[3]]["receipt"]
    ready_receipt.pop("receipt_fingerprint")
    ready_receipt["evidence"]["normalized_restore_locator"]["creation_time_epoch"] += 1
    ready_receipt.update(pbs_receipts.seal_receipt(ready_receipt))
    with pytest.raises(restore.Refusal, match="invalid_input"):
        restore.verify_prerequisite_chain(mismatch)
    caller_selector = _chain_fixture()
    caller_selector["private_copy_selector"] = dict(
        caller_selector["task_proofs"][CONTRACTS[3]]["receipt"]["evidence"]["normalized_restore_locator"]
    )
    with pytest.raises(restore.Refusal, match="invalid_input"):
        restore.verify_prerequisite_chain(caller_selector)
    undeclared_store = _chain_fixture()
    undeclared_store["expected_copy_storage_id"] = "other-store"
    with pytest.raises(restore.Refusal, match="task_chain_invalid"):
        restore.verify_prerequisite_chain(undeclared_store)


def test_cleanup_keeps_historical_and_current_private_inventory_pins_independent():
    pins = {
        "restore_launch_private_sha": "a" * 40,
        "restore_receipt_private_sha": "a" * 40,
        "current_private_sha": "b" * 40,
        "current_provenance_private_sha": "b" * 40,
        "current_inventory_provenance": "exact_bytes_match",
        "current_inventory_fingerprint": "c" * 64,
    }
    result = restore.verify_cleanup_inventory_pins(pins)
    assert result["status"] == "verified"
    assert result["historical_private_sha"] == "a" * 40
    assert result["current_private_sha"] == "b" * 40
    assert result["pins_are_independent"] is True

    wrong_restore_pin = dict(pins, restore_receipt_private_sha="d" * 40)
    with pytest.raises(restore.Refusal, match="restore_receipt_invalid"):
        restore.verify_cleanup_inventory_pins(wrong_restore_pin)
    wrong_current_pin = dict(pins, current_provenance_private_sha="e" * 40)
    with pytest.raises(restore.Refusal, match="restore_receipt_invalid"):
        restore.verify_cleanup_inventory_pins(wrong_current_pin)


@pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")
def test_cleanup_provenance_include_inherits_current_pin_not_historical_restore_pin(tmp_path):
    playbooks = Path(__file__).resolve().parents[1] / "playbooks"
    cleanup = playbook_yaml.load(playbooks / "cleanup-o11y-isolated-restore.yml")
    cleanup_tasks = list(playbook_yaml.tasks(cleanup))
    include = copy.deepcopy(
        next(
            task
            for task in cleanup_tasks
            if task.get("name") == "Reprove exact current private inventory bytes against the restore binding"
        )
    )
    assert "site_config_base_sha" not in include.get("vars", {})

    provenance_tasks = list(
        playbook_yaml.tasks(playbook_yaml.load(playbooks / "tasks/o11y-pbs-inventory-provenance.yml"))
    )
    validate_selectors = copy.deepcopy(
        next(task for task in provenance_tasks if task.get("name") == "Validate fixed PBS provenance selectors")
    )
    current_sha = "b" * 40
    historical_sha = "a" * 40
    assert current_sha != historical_sha

    stub = tmp_path / "provenance-stub.yml"
    stub.write_text(
        yaml.safe_dump(
            [
                validate_selectors,
                {
                    "name": "Confirm the included pin is the current cleanup pin",
                    "ansible.builtin.assert": {
                        "that": [
                            "site_config_base_sha == expected_current_pin",
                            "site_config_base_sha != historical_restore_pin",
                        ]
                    },
                },
            ]
        )
    )
    include["ansible.builtin.include_tasks"] = str(stub)
    inventory = tmp_path / "inventory.ini"
    inventory.write_text("localhost ansible_connection=local\n")
    harness = tmp_path / "cleanup-provenance.yml"
    harness.write_text(
        yaml.safe_dump(
            [
                {
                    "hosts": "localhost",
                    "gather_facts": False,
                    "vars": {
                        "expected_repository_sha": "c" * 40,
                        "site_config_base_sha": current_sha,
                        "expected_current_pin": current_sha,
                        "historical_restore_pin": historical_sha,
                        "estate_coverage_semaphore_project_id": 7,
                        "estate_coverage_semaphore_repository_id": 11,
                    },
                    "tasks": [include],
                }
            ]
        )
    )

    result = harness_sandbox.run(
        ["ansible-playbook", "-i", str(inventory), str(harness)],
        tmp_path,
        cwd=Path(__file__).resolve().parents[2],
        env=harness_sandbox.env_for(tmp_path),
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_restore_and_cleanup_api_writes_follow_intent_handle_and_effect_order():
    playbooks = Path(__file__).parents[1] / "playbooks"
    restore_tasks = list(playbook_yaml.tasks(playbook_yaml.load(playbooks / "restore-o11y-isolated.yml")))
    cleanup_tasks = list(playbook_yaml.tasks(playbook_yaml.load(playbooks / "cleanup-o11y-isolated-restore.yml")))

    def position(tasks, name):
        return next(i for i, task in enumerate(tasks) if task.get("name") == name)

    restore_sequences = (
        (
            "Persist the restore intent through CAS",
            "Submit exactly one stopped restore from the protected copy",
            "Record the actual qmrestore UPID with CAS readback",
            "Commit stopped restore effect before isolation may begin",
            "POST",
        ),
        (
            "Persist isolation intent before changing target configuration",
            "Remove every target NIC, disable boot, and write only the ownership marker",
            "Read back the full disconnected target configuration",
            "Commit verified isolation effect through CAS readback",
            "PUT",
        ),
        (
            "Persist start intent through CAS before starting the target",
            "Start only the isolated restore target",
            "Record the actual qmstart UPID through CAS",
            "Commit verified start effect before QGA execution",
            "POST",
        ),
        (
            "Persist guest execution intent before QGA",
            "Send checker source and fixed expectations only through QGA input-data",
            "Record QGA PID in the action journal",
            "Commit successful guest proof to the action journal",
            "POST",
        ),
        (
            "Persist stop intent before stopping the temporary target",
            "Stop only the exact isolated restore target",
            "Record the actual qmstop UPID before polling",
            "CAS complete stopped-target effect",
            "POST",
        ),
    )
    fake_proxmox_calls = []
    for sequence in restore_sequences:
        *names, expected_method = sequence
        positions = [position(restore_tasks, name) for name in names]
        assert positions == sorted(positions)
        mutation = restore_tasks[positions[1]]
        assert "ansible.builtin.uri" in mutation
        request = mutation["ansible.builtin.uri"]
        assert request["method"] == expected_method
        assert "retries" not in mutation
        fake_proxmox_calls.append((expected_method, names[1]))

    assert len(fake_proxmox_calls) == 5

    cleanup_sequence = (
        "Transfer ownership and persist delete intent with exact journal CAS",
        "Delete only the authenticated disconnected target",
        "Record the actual qmdestroy UPID through exact CAS readback",
        "Commit verified deletion effect through the action journal CAS",
        "CAS the terminal cleaned record after exact readback",
    )
    cleanup_positions = [position(cleanup_tasks, name) for name in cleanup_sequence]
    assert cleanup_positions == sorted(cleanup_positions)
    delete = cleanup_tasks[cleanup_positions[1]]
    assert delete["ansible.builtin.uri"]["method"] == "DELETE"
    assert "retries" not in delete
    fake_proxmox_calls.append(("DELETE", cleanup_sequence[1]))
    assert fake_proxmox_calls[-1] == ("DELETE", "Delete only the authenticated disconnected target")
    assert "expected_receipt_sha" not in (playbooks / "cleanup-o11y-isolated-restore.yml").read_text()
