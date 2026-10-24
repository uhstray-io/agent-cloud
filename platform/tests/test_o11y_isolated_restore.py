"""Pure tests for artifact admission and authenticated task evidence."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "playbooks" / "files"))
sys.path.insert(0, str(Path(__file__).parent))
import o11y_isolated_restore as restore  # noqa: E402
import o11y_pbs_receipts as pbs_receipts  # noqa: E402
import playbook_yaml  # noqa: E402

PUBLIC_SHA = "a" * 40
PRIVATE_SHA = "b" * 40
PROJECT_ID = 7
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
            "destination_identity_fingerprint": "a" * 64,
            "destination_storage_config_fingerprint": "b" * 64,
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
        "private_copy_selector": copy_selector,
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


@pytest.mark.parametrize(
    "line",
    [
        "hookscript: local:snippets/restore-hook.pl",
        "args: -device host-pci",
        "hostpci0: 0000:03:00.0",
        "usb0: host=1-1",
        "virtiofs0: shared",
        "mp0: /srv/share,mp=/host",
        "smbios1: uuid=abc",
        "cicustom: user=local:snippets/custom.yaml",
        "unused0: local:orphan-disk",
        "ide2: local:iso/debian.iso,media=cdrom",
        "scsi0: pbs:archive,size=20G,backup=0",
        "scsi0: pbs:archive,size=20G,backup=0\nscsi0: pbs:archive,size=20G,backup=1",
    ],
)
def test_unsafe_passthrough_shared_hooks_or_excluded_disks_refuse(line):
    config = {"status": 200, "data": "vmid: 700\nscsi0: pbs:backup/vm/700,backup=1,size=20G\n" + line + "\n"}
    with pytest.raises(restore.Refusal):
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
    )["nic_count"] == 0
    for key, value in (
        ("net0", "virtio=aa,bridge=vmbr0"),
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
            )


@pytest.mark.parametrize(
    "status",
    [
        {"exited": True, "exitcode": 1, "out-data": '{"status":"verified"}'},
        {"exited": False, "exitcode": 0, "out-data": '{"status":"verified"}'},
        {"exited": True, "exitcode": 0, "out-truncated": True, "out-data": '{"status":"verified"}'},
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

    chain = by_name["Validate all authenticated predecessor links and the closed private selector"]
    assert "'mode': 'prerequisite_chain', 'payload': _restore_chain_input" in chain["ansible.builtin.command"]["stdin"]

    artifact = by_name["Bind one fresh destination candidate to the private selector"]
    artifact_input = artifact["ansible.builtin.command"]["stdin"]
    assert "'vmid': _restore_source_vmid | string" in artifact_input
    assert "'node': _restore_source_node" in artifact_input

    disk_assertion = by_name["Require complete included disks, safe config, and live isolation preflight"]
    disk_clauses = disk_assertion["ansible.builtin.assert"]["that"]
    assert any("_restore_target_preflight.restore_disk_count" in clause for clause in disk_clauses)

    submit = by_name["Send checker source and fixed expectations only through QGA input-data"]
    submit = submit["ansible.builtin.uri"]
    assert submit["body"]["command"][0] == "/usr/bin/python3"
    assert "arg" not in submit["body"]
    assert "capture-output" not in submit["body"]
    assert "input-data" in submit["body"]

    parse = by_name["Validate decoded QGA status and parse bounded checker receipt"]["ansible.builtin.command"]
    assert "'mode': 'qga_exec_status'" in parse["stdin"]
    receipt = by_name["Retain the bounded content-free guest verification receipt"]["ansible.builtin.set_fact"]
    assert "b64decode" not in receipt["_restore_guest_receipt"]


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


def test_prerequisite_chain_requires_the_private_copy_selector_and_storage():
    mismatch = _chain_fixture()
    mismatch["private_copy_selector"] = dict(mismatch["private_copy_selector"], size_bytes=301)
    with pytest.raises(restore.Refusal, match="task_chain_invalid"):
        restore.verify_prerequisite_chain(mismatch)
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
