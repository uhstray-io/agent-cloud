from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

FILE = Path(__file__).resolve().parents[1] / "playbooks/files/o11y_pbs_receipts.py"
SPEC = importlib.util.spec_from_file_location("o11y_pbs_receipts", FILE)
pbs = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(pbs)

PUBLIC = "a" * 40
PRIVATE = "b" * 40
NOW = datetime(2026, 10, 21, 16, 0, tzinfo=UTC)
BACKUP_PLAYBOOK = "platform/playbooks/backup-o11y-to-pbs.yml"
UPID = "UPID:pve1:00000001:00000002:00000003:vzdump:100:root@pam:"


def manifest() -> dict[str, object]:
    return {
        "backup-type": "vm",
        "backup-id": "120",
        "backup-time": 1792598400,
        "files": [
            {"filename": "drive-scsi0.img.fidx", "crypt-mode": "none", "size": 17, "csum": "1" * 64},
            {"filename": "qemu-server.conf.blob", "crypt-mode": "none", "size": 19, "csum": "2" * 64},
        ],
        "unprotected": {"note": "ignored mutable metadata"},
        "signature": "ignored mutable signature",
    }


def backup_receipt() -> dict[str, object]:
    digest = pbs.artifact_fingerprint(manifest())
    return pbs.seal_receipt({
        "schema_version": 1,
        "contract": "o11y-pbs-backup/v1",
        "status": "complete",
        "reason": "none",
        "public_sha": PUBLIC,
        "private_sha": PRIVATE,
        "inventory_provenance": "exact_bytes_match",
        "inventory_fingerprint": "3" * 64,
        "source_fingerprint": "4" * 64,
        "artifact_fingerprint": digest,
        "observed_start": "2026-10-21T15:00:00Z",
        "observed_end": "2026-10-21T15:30:00Z",
        "role_context": pbs.CONTRACTS["o11y-pbs-backup/v1"]["roles"],
        "evidence": {
            "backup_upid": UPID,
            "snapshot": {"backup_type": "vm", "backup_id": "120", "backup_time": 1792598400},
            "source_vmid": "120",
            "source_node": "pve1",
            "artifact_fingerprint": digest,
            "playbook": BACKUP_PLAYBOOK,
        },
    })


def test_artifact_fingerprint_is_stable_and_excludes_mutable_fields() -> None:
    first = pbs.artifact_fingerprint(manifest())
    altered = manifest()
    altered["signature"] = "changed"
    altered["unprotected"] = {"note": "also changed"}
    altered["files"] = list(reversed(altered["files"]))
    assert pbs.artifact_fingerprint(altered) == first


@pytest.mark.parametrize(
    "change",
    [
        lambda value: value.update({"backup-time": True}),
        lambda value: value.update({"unknown": 1}),
        lambda value: value["files"][0].update({"crypt-mode": "mystery"}),
        lambda value: value["files"][0].update({"size": True}),
        lambda value: value["files"][0].update({"csum": "short"}),
        lambda value: value["files"].append(value["files"][0]),
    ],
)
def test_manifest_rejects_noncanonical_or_ambiguous_fields(change) -> None:
    value = manifest()
    change(value)
    with pytest.raises(pbs.Refusal):
        pbs.artifact_fingerprint(value)


def test_manifest_parser_rejects_duplicate_json_keys() -> None:
    with pytest.raises(pbs.Refusal):
        pbs.parse_manifest('{"backup-type":"vm","backup-type":"vm"}')


def test_source_fingerprint_binds_logical_vm_and_completed_receipt_is_closed() -> None:
    digest = pbs.artifact_fingerprint(manifest())
    logical = pbs.source_fingerprint("120", "pve1")
    assert logical != pbs.source_fingerprint("121", "pve1")
    receipt = pbs.completed_receipt(
        contract="o11y-pbs-backup/v1",
        public_sha=PUBLIC,
        private_sha=PRIVATE,
        inventory_fingerprint="3" * 64,
        source_fingerprint_value=logical,
        artifact=digest,
        observed_start="2026-10-21T15:00:00Z",
        observed_end="2026-10-21T15:30:00Z",
        evidence={
            "backup_upid": UPID,
            "snapshot": {"backup_type": "vm", "backup_id": "120", "backup_time": 1792598400},
            "source_vmid": "120",
            "source_node": "pve1",
            "artifact_fingerprint": digest,
            "playbook": BACKUP_PLAYBOOK,
        },
        now=NOW,
    )
    assert pbs.validate_receipt(
        receipt, contract="o11y-pbs-backup/v1", public_sha=PUBLIC, private_sha=PRIVATE, now=NOW
    ) == receipt


def test_receipt_requires_exact_contract_and_recent_completed_evidence() -> None:
    receipt = backup_receipt()
    assert pbs.validate_receipt(
        receipt, contract="o11y-pbs-backup/v1", public_sha=PUBLIC, private_sha=PRIVATE, now=NOW
    ) == receipt
    stale_body = {key: value for key, value in receipt.items() if key != "receipt_fingerprint"}
    stale_body.update(observed_start="2026-10-20T14:00:00Z", observed_end="2026-10-20T15:00:00Z")
    stale = pbs.seal_receipt(stale_body)
    with pytest.raises(pbs.Refusal, match="receipt_stale"):
        pbs.validate_receipt(
            stale, contract="o11y-pbs-backup/v1", public_sha=PUBLIC, private_sha=PRIVATE, now=NOW
        )
    forged_body = {key: value for key, value in receipt.items() if key != "receipt_fingerprint"}
    forged_body["role_context"] = {
        "primary": "pve_backup",
        "verification": "destination_pbs_pull",
        "destination": "not_applicable",
    }
    forged = pbs.seal_receipt(forged_body)
    with pytest.raises(pbs.Refusal, match="task_binding_mismatch"):
        pbs.validate_receipt(
            forged, contract="o11y-pbs-backup/v1", public_sha=PUBLIC, private_sha=PRIVATE, now=NOW
        )
    tampered = dict(receipt, status="refused")
    with pytest.raises(pbs.Refusal, match="task_binding_mismatch"):
        pbs.validate_receipt(
            tampered, contract="o11y-pbs-backup/v1", public_sha=PUBLIC, private_sha=PRIVATE, now=NOW
        )


def test_completed_task_must_bind_task_output_project_and_reviewed_commit() -> None:
    receipt = backup_receipt()
    task = {
        "id": 10,
        "project_id": 1,
        "template_id": 55,
        "commit_hash": PUBLIC,
        "status": "success",
        "created": "2026-10-21T14:59:00Z",
        "start": "2026-10-21T15:00:00Z",
        "end": "2026-10-21T15:30:00Z",
        "environment": json.dumps({
            "expected_repository_sha": PUBLIC,
            "site_config_base_sha": PRIVATE,
        }),
        "playbook": "",
        "params": {"diff": False, "dry_run": False},
    }
    serialized = json.dumps(receipt, indent=2)
    lines = serialized.splitlines()
    split = len(lines) // 2
    rows = [
        {"id": 1, "task_id": 10, "stage_id": None, "time": "2026-10-21T15:20:00Z", "output": "\n".join(lines[:split])},
        {"id": 2, "task_id": 10, "stage_id": None, "time": "2026-10-21T15:20:01Z", "output": "\n".join(lines[split:])},
    ]
    result = pbs.validate_completed_task(
        task,
        rows,
        contract="o11y-pbs-backup/v1",
        public_sha=PUBLIC,
        private_sha=PRIVATE,
        playbook=BACKUP_PLAYBOOK,
        expected_project_id=1,
        expected_task_id=10,
        expected_launch={"expected_repository_sha": PUBLIC, "site_config_base_sha": PRIVATE},
        now=NOW,
    )
    assert result["receipt"] == receipt
    assert result["task_metadata"]["template_id"] == 55
    for wrapped_output in (
        json.dumps({"o11y_pbs_receipt": receipt}, indent=2),
        json.dumps({"msg": receipt}, indent=2),
    ):
        assert pbs.validate_completed_task(
            task,
            [{**rows[0], "output": wrapped_output}],
            contract="o11y-pbs-backup/v1",
            public_sha=PUBLIC,
            private_sha=PRIVATE,
            playbook=BACKUP_PLAYBOOK,
            expected_project_id=1,
            expected_task_id=10,
            expected_launch={"expected_repository_sha": PUBLIC, "site_config_base_sha": PRIVATE},
            now=NOW,
        )["receipt"] == receipt
    with pytest.raises(pbs.Refusal, match="receipt_missing"):
        pbs.validate_completed_task(
            task,
            [{**rows[0], "output": json.dumps({"unrecognized": {"receipt": receipt}})}],
            contract="o11y-pbs-backup/v1",
            public_sha=PUBLIC,
            private_sha=PRIVATE,
            playbook=BACKUP_PLAYBOOK,
            expected_project_id=1,
            expected_task_id=10,
            expected_launch={"expected_repository_sha": PUBLIC, "site_config_base_sha": PRIVATE},
            now=NOW,
        )
    with pytest.raises(pbs.Refusal, match="output_invalid"):
        pbs.validate_completed_task(
            task,
            [{"id": 1, "task_id": 11, "stage_id": None, "time": "2026-10-21T15:20:00Z", "output": serialized}],
            contract="o11y-pbs-backup/v1",
            public_sha=PUBLIC,
            private_sha=PRIVATE,
            playbook=BACKUP_PLAYBOOK,
            expected_project_id=1,
            expected_task_id=10,
            expected_launch={"expected_repository_sha": PUBLIC, "site_config_base_sha": PRIVATE},
            now=NOW,
        )

    wrong_private_launch = {"expected_repository_sha": PUBLIC, "site_config_base_sha": "5" * 40}
    wrong_private_task = {**task, "environment": json.dumps(wrong_private_launch)}
    with pytest.raises(pbs.Refusal, match="task_binding_mismatch"):
        pbs.validate_completed_task(
            wrong_private_task,
            rows,
            contract="o11y-pbs-backup/v1",
            public_sha=PUBLIC,
            private_sha=PRIVATE,
            playbook=BACKUP_PLAYBOOK,
            expected_project_id=1,
            expected_task_id=10,
            expected_launch=wrong_private_launch,
            now=NOW,
        )


def test_verification_receipt_must_bind_its_authenticated_backup_task_selector() -> None:
    digest = pbs.artifact_fingerprint(manifest())
    evidence = {
        "backup_task_id": 10,
        "backup_upid": UPID,
        "verification_upid": UPID,
        "verification_status": "OK",
        "snapshot": {"backup_type": "vm", "backup_id": "120", "backup_time": 1792598400},
        "source_vmid": "120",
        "artifact_fingerprint": digest,
        "playbook": "platform/playbooks/verify-o11y-pbs-artifact.yml",
    }
    receipt = pbs.completed_receipt(
        contract="o11y-pbs-verify/v1",
        public_sha=PUBLIC,
        private_sha=PRIVATE,
        inventory_fingerprint="3" * 64,
        source_fingerprint_value="4" * 64,
        artifact=digest,
        observed_start="2026-10-21T15:00:00Z",
        observed_end="2026-10-21T15:30:00Z",
        evidence=evidence,
        now=NOW,
    )
    task = {
        "id": 11,
        "project_id": 1,
        "template_id": 55,
        "commit_hash": PUBLIC,
        "status": "success",
        "created": "2026-10-21T14:59:00Z",
        "start": "2026-10-21T15:00:00Z",
        "end": "2026-10-21T15:30:00Z",
        "environment": {
            "expected_repository_sha": PUBLIC,
            "site_config_base_sha": PRIVATE,
            "backup_task_id": 10,
        },
        "playbook": "",
        "params": {"diff": False, "dry_run": False},
    }
    output = json.dumps(receipt, indent=2)
    rows = [{"id": 8, "task_id": 11, "stage_id": None,
             "time": "2026-10-21T15:20:00Z", "output": output}]
    arguments = {
        "contract": "o11y-pbs-verify/v1",
        "public_sha": PUBLIC,
        "private_sha": PRIVATE,
        "playbook": "platform/playbooks/verify-o11y-pbs-artifact.yml",
        "expected_project_id": 1,
        "expected_launch": task["environment"],
        "expected_task_id": 11,
        "now": NOW,
    }
    assert pbs.validate_completed_task(task, rows, **arguments)["receipt"] == receipt

    forged_evidence = dict(evidence, backup_task_id=9)
    forged_receipt = pbs.completed_receipt(
        contract="o11y-pbs-verify/v1",
        public_sha=PUBLIC,
        private_sha=PRIVATE,
        inventory_fingerprint="3" * 64,
        source_fingerprint_value="4" * 64,
        artifact=digest,
        observed_start="2026-10-21T15:00:00Z",
        observed_end="2026-10-21T15:30:00Z",
        evidence=forged_evidence,
        now=NOW,
    )
    forged_rows = [{**rows[0], "output": json.dumps(forged_receipt, indent=2)}]
    with pytest.raises(pbs.Refusal, match="task_binding_mismatch"):
        pbs.validate_completed_task(task, forged_rows, **arguments)


def test_completed_output_reconstructs_private_restore_receipt_and_closed_launch() -> None:
    receipt_body = {
        key: ("restore_receipt/v3" if key == "type" else "verified")
        for key in pbs.RESTORE_RECEIPT_KEYS - {"receipt_fingerprint", "coordination_record"}
    }
    receipt_body["bindings"] = {"public_sha": PUBLIC, "private_inventory_sha": PRIVATE}
    receipt_body["coordination_record"] = {"receipt_fingerprint": None, "version": 1}
    receipt = pbs.seal_restore_receipt(receipt_body)
    task = {
        "id": 14,
        "project_id": 1,
        "template_id": 56,
        "commit_hash": PUBLIC,
        "status": "success",
        "created": "2026-10-21T14:59:00Z",
        "start": "2026-10-21T15:00:00Z",
        "end": "2026-10-21T15:30:00Z",
        "environment": {
            "expected_repository_sha": PUBLIC,
            "site_config_base_sha": PRIVATE,
            "backup_task_id": 10,
            "pbs_verify_task_id": 11,
            "copy_sync_task_id": 12,
            "copy_ready_task_id": 13,
        },
        "playbook": "",
        "params": {"diff": False, "dry_run": False},
    }
    lines = json.dumps(receipt, indent=2).splitlines()
    midpoint = len(lines) // 2
    rows = [
        {
            "id": 3,
            "task_id": 14,
            "stage_id": None,
            "time": "2026-10-21T15:20:00Z",
            "output": "\n".join(lines[:midpoint]),
        },
        {
            "id": 4,
            "task_id": 14,
            "stage_id": None,
            "time": "2026-10-21T15:20:01Z",
            "output": "\n".join(lines[midpoint:]),
        },
    ]
    result = pbs.validate_completed_output(
        task,
        rows,
        public_sha=PUBLIC,
        expected_project_id=1,
        expected_playbook="platform/playbooks/restore-o11y-isolated.yml",
        expected_launch=task["environment"],
        selector="type",
        expected_selector="restore_receipt/v3",
        expected_receipt_keys=pbs.RESTORE_RECEIPT_KEYS,
        allowed_launch_keys=set(task["environment"]),
        now=NOW,
    )
    assert result["receipt"] == receipt
    forged = dict(receipt)
    forged["coordination_record"] = {"receipt_fingerprint": "0" * 64, "version": 1}
    forged_launch_task = dict(task)
    forged_launch_task["id"] = 15
    forged_rows = [
        {"id": 5, "task_id": 15, "stage_id": None, "time": "2026-10-21T15:20:00Z", "output": json.dumps(forged)}
    ]
    with pytest.raises(pbs.Refusal, match="receipt_missing"):
        pbs.validate_completed_output(
            forged_launch_task,
            forged_rows,
            public_sha=PUBLIC,
            expected_project_id=1,
            expected_playbook="platform/playbooks/restore-o11y-isolated.yml",
            expected_launch=task["environment"],
            selector="type",
            expected_selector="restore_receipt/v3",
            expected_receipt_keys=pbs.RESTORE_RECEIPT_KEYS,
            allowed_launch_keys=set(task["environment"]),
            now=NOW,
        )

    for bad_launch in (
        {key: value for key, value in task["environment"].items() if key != "site_config_base_sha"},
        {**task["environment"], "site_config_base_sha": 123},
        {**task["environment"], "site_config_base_sha": "not-a-git-sha"},
        {**task["environment"], "site_config_base_sha": "5" * 40},
        {**task["environment"], "copy_ready_task_id": 0},
    ):
        bad_task = {**task, "environment": bad_launch, "id": task["id"] + 1}
        bad_rows = [{**rows[0], "task_id": bad_task["id"], "output": json.dumps(receipt)}]
        with pytest.raises(pbs.Refusal):
            pbs.validate_completed_output(
                bad_task,
                bad_rows,
                public_sha=PUBLIC,
                expected_project_id=1,
                expected_playbook="platform/playbooks/restore-o11y-isolated.yml",
                expected_launch=bad_launch,
                selector="type",
                expected_selector="restore_receipt/v3",
                expected_receipt_keys=pbs.RESTORE_RECEIPT_KEYS,
                allowed_launch_keys=set(bad_launch),
                now=NOW,
            )

    with pytest.raises(pbs.Refusal, match="invalid_input"):
        pbs.validate_completed_output(
            task,
            rows,
            public_sha=PUBLIC,
            expected_project_id=1,
            expected_playbook="platform/playbooks/restore-o11y-isolated.yml",
            expected_launch=task["environment"],
            selector="type",
            expected_selector="restore_receipt/v3",
            expected_receipt_keys=pbs.RESTORE_RECEIPT_KEYS,
            allowed_launch_keys=set(task["environment"]) | {"restore_locator"},
            now=NOW,
        )

    with pytest.raises(pbs.Refusal, match="task_binding_mismatch"):
        mismatched_task = {**task, "environment": {**task["environment"], "site_config_base_sha": "5" * 40}}
        pbs.validate_completed_output(
            mismatched_task,
            rows,
            public_sha=PUBLIC,
            expected_project_id=1,
            expected_playbook="platform/playbooks/restore-o11y-isolated.yml",
            expected_launch=mismatched_task["environment"],
            selector="type",
            expected_selector="restore_receipt/v3",
            expected_receipt_keys=pbs.RESTORE_RECEIPT_KEYS,
            allowed_launch_keys=set(mismatched_task["environment"]),
            now=NOW,
        )


def test_cleanup_receipt_mode_uses_its_own_exact_launch_shape() -> None:
    receipt_body = {
        key: ("restore_receipt/v3" if key == "type" else "verified")
        for key in pbs.RESTORE_RECEIPT_KEYS - {"receipt_fingerprint", "coordination_record"}
    }
    receipt_body["coordination_record"] = {"receipt_fingerprint": None, "version": 1}
    receipt = pbs.seal_restore_receipt(receipt_body)
    launch = {
        "expected_repository_sha": PUBLIC,
        "site_config_base_sha": PRIVATE,
        "restore_task_id": 14,
        "expected_restore_repository_sha": "8" * 40,
    }
    task = {
        "id": 15,
        "project_id": 1,
        "template_id": 57,
        "commit_hash": PUBLIC,
        "status": "success",
        "created": "2026-10-21T14:59:00Z",
        "start": "2026-10-21T15:00:00Z",
        "end": "2026-10-21T15:30:00Z",
        "environment": launch,
        "playbook": "",
        "params": {"diff": False, "dry_run": False},
    }
    rows = [{
        "id": 5,
        "task_id": 15,
        "stage_id": None,
        "time": "2026-10-21T15:20:00Z",
        "output": json.dumps(receipt),
    }]
    result = pbs.validate_completed_output(
        task,
        rows,
        public_sha=PUBLIC,
        expected_project_id=1,
        expected_playbook="platform/playbooks/cleanup-o11y-isolated-restore.yml",
        expected_launch=launch,
        selector="type",
        expected_selector="restore_receipt/v3",
        expected_receipt_keys=pbs.RESTORE_RECEIPT_KEYS,
        allowed_launch_keys=set(launch),
        now=NOW,
    )
    assert result["receipt"] == receipt

    wrong_mode_keys = set(launch) | {"target_vmid"}
    with pytest.raises(pbs.Refusal, match="invalid_input"):
        pbs.validate_completed_output(
            task,
            rows,
            public_sha=PUBLIC,
            expected_project_id=1,
            expected_playbook="platform/playbooks/cleanup-o11y-isolated-restore.yml",
            expected_launch=launch,
            selector="type",
            expected_selector="restore_receipt/v3",
            expected_receipt_keys=pbs.RESTORE_RECEIPT_KEYS,
            allowed_launch_keys=wrong_mode_keys,
            now=NOW,
        )


def test_copy_readiness_binds_exact_restore_locator_and_destination_fingerprints() -> None:
    pbs_manifest = manifest()
    pbs_manifest["backup-time"] = 1792594800
    digest = pbs.artifact_fingerprint(pbs_manifest)
    evidence = {
        "sync_task_id": 12,
        "snapshot": {"backup_type": "vm", "backup_id": "120", "backup_time": 1792594800},
        "artifact_fingerprint": digest,
        "readback_status": "readable",
        "acl_separation": "verified",
        "pull_direction": "destination_initiated",
        "deletion_isolation": "verified",
        "maintenance_isolation": "verified",
        "playbook": "platform/playbooks/verify-o11y-protected-pbs-copy.yml",
        "normalized_restore_locator": {
            "storage_id": "o11y-protected",
            "volid": "o11y-protected:backup/vm/120/2026-10-21T15:00:00Z",
            "backend": "pbs",
            "format": "pbs-vm",
            "creation_time_epoch": 1792594800,
            "size_bytes": 1024,
        },
        "destination_identity_fingerprint": "5" * 64,
        "destination_storage_config_fingerprint": "6" * 64,
    }
    receipt = pbs.seal_receipt({
        "schema_version": 1,
        "contract": "o11y-pbs-copy-ready/v1",
        "status": "ready",
        "reason": "destination_copy_ready_for_isolated_restore",
        "public_sha": PUBLIC,
        "private_sha": PRIVATE,
        "inventory_provenance": "exact_bytes_match",
        "inventory_fingerprint": "3" * 64,
        "source_fingerprint": "4" * 64,
        "artifact_fingerprint": digest,
        "observed_start": "2026-10-21T15:00:00Z",
        "observed_end": "2026-10-21T15:30:00Z",
        "role_context": pbs.CONTRACTS["o11y-pbs-copy-ready/v1"]["roles"],
        "evidence": evidence,
    })
    assert pbs.validate_receipt(
        receipt, contract="o11y-pbs-copy-ready/v1", public_sha=PUBLIC, private_sha=PRIVATE, now=NOW
    ) == receipt
    task = {
        "id": 12,
        "project_id": 1,
        "template_id": 57,
        "commit_hash": PUBLIC,
        "status": "success",
        "created": "2026-10-21T14:59:00Z",
        "start": "2026-10-21T15:00:00Z",
        "end": "2026-10-21T15:30:00Z",
        "environment": {
            "expected_repository_sha": PUBLIC,
            "site_config_base_sha": PRIVATE,
            "backup_task_id": 10,
            "verify_task_id": 11,
            "sync_task_id": 12,
        },
        "playbook": "",
        "params": {"diff": False, "dry_run": False},
    }
    validated = pbs.validate_completed_task(
        task,
        [{"id": 8, "task_id": 12, "stage_id": None,
          "time": "2026-10-21T15:20:00Z", "output": json.dumps({"msg": receipt})}],
        contract="o11y-pbs-copy-ready/v1",
        public_sha=PUBLIC,
        private_sha=PRIVATE,
        playbook="platform/playbooks/verify-o11y-protected-pbs-copy.yml",
        expected_project_id=1,
        expected_task_id=12,
        expected_launch=task["environment"],
        now=NOW,
    )
    assert validated["receipt"]["status"] == "ready"
    bad_body = {key: value for key, value in receipt.items() if key != "receipt_fingerprint"}
    bad_body["evidence"] = dict(evidence, normalized_restore_locator={"volid": "invented"})
    with pytest.raises(pbs.Refusal, match="readback_incomplete"):
        pbs.validate_receipt(
            pbs.seal_receipt(bad_body),
            contract="o11y-pbs-copy-ready/v1",
            public_sha=PUBLIC,
            private_sha=PRIVATE,
            now=NOW,
        )
