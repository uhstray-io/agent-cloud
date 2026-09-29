"""Safety and convergence checks for the Dev-bound o11y backup-job reconciler."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "platform/playbooks/files"))

from reconcile_proxmox_backup_job import (  # noqa: E402
    _safe_refusal,
    assert_unchanged,
    inspect_candidates,
    main,
    prepare_plan,
    verify_readback,
)


def job(**overrides):
    value = {
        "id": "nightly-o11y",
        "enabled": True,
        "vmid": "100,101",
        "schedule": "02:00",
        "storage": "backup-store",
        "mode": "snapshot",
        "compress": "zstd",
    }
    value.update(overrides)
    return value


def plan_for(detail=None, jobs=None, vmid="200"):
    detail = job() if detail is None else detail
    listed = dict(detail) | {"next-run": 1_800_000_000}
    return prepare_plan({
        "job_id": "nightly-o11y",
        "vmid": vmid,
        "jobs": [listed] if jobs is None else jobs,
        "detail": detail,
    })


def test_plan_appends_only_the_declared_vmid_and_marks_a_real_change():
    plan = plan_for()

    assert plan["changed"] is True
    assert plan["before_vmids"] == [100, 101]
    assert plan["desired_vmids"] == [100, 101, 200]
    assert plan["desired_vmid"] == "100,101,200"
    assert plan["before"]["storage"] == "backup-store"
    assert plan["before"]["schedule"] == "02:00"
    assert "next-run" not in plan["before"]


def test_existing_membership_is_a_noop():
    plan = plan_for(vmid="101")

    assert plan["changed"] is False
    assert plan["desired_vmids"] == [100, 101]
    assert plan["desired_vmid"] == "100,101"


@pytest.mark.parametrize(
    "selection",
    [
        {"all": True},
        {"pool": "production"},
        {"exclude": "102"},
    ],
)
def test_unsupported_selectors_fail_closed(selection):
    with pytest.raises(ValueError, match="unsupported"):
        plan_for(detail=job(**selection))


@pytest.mark.parametrize("members", ["", "100,,101", "100,100", "99", "100, 101", 100])
def test_malformed_or_duplicate_membership_fails_closed(members):
    with pytest.raises(ValueError, match="membership|VMID"):
        plan_for(detail=job(vmid=members))


def test_missing_ambiguous_disabled_or_mismatched_job_fails_closed():
    listed = job() | {"next-run": 1_800_000_000}
    with pytest.raises(ValueError, match="missing or ambiguous"):
        plan_for(jobs=[])
    with pytest.raises(ValueError, match="duplicate backup-job IDs"):
        plan_for(jobs=[listed, dict(listed)])

    with pytest.raises(ValueError, match="disabled"):
        plan_for(detail=job(enabled=False))

    with pytest.raises(ValueError, match="do not match"):
        plan_for(jobs=[job(vmid="100") | {"next-run": 1_800_000_000}])


def test_default_enabled_state_follows_the_proxmox_api_contract():
    config = job()
    config.pop("enabled")
    plan = plan_for(detail=config)
    assert plan["changed"] is True


def test_inspection_lists_sanitized_candidates_without_raw_membership_or_other_fields():
    result = inspect_candidates({
        "vmid": "200",
        "jobs": [
            job() | {"comment": "private operator note"},
            job(id="all-vms", vmid=None, all=True, enabled=1, schedule="03:00"),
            job(id="disabled", enabled=False),
            job(id="pooled", vmid="", pool="production"),
        ],
    })

    assert result["candidate_count"] == 4
    assert result["candidates"] == [
        {
            "id": "nightly-o11y",
            "type": "vzdump",
            "enabled": "enabled",
            "selector": "explicit-vmid",
            "member_count": 2,
            "includes_declared_o11y_vmid": False,
            "schedule": "02:00",
        },
        {
            "id": "all-vms",
            "type": "vzdump",
            "enabled": "enabled",
            "selector": "all",
            "member_count": None,
            "includes_declared_o11y_vmid": None,
            "schedule": "03:00",
        },
        {
            "id": "disabled",
            "type": "vzdump",
            "enabled": "disabled",
            "selector": "explicit-vmid",
            "member_count": 2,
            "includes_declared_o11y_vmid": False,
            "schedule": "02:00",
        },
        {
            "id": "pooled",
            "type": "vzdump",
            "enabled": "enabled",
            "selector": "pool",
            "member_count": None,
            "includes_declared_o11y_vmid": None,
            "schedule": "02:00",
        },
    ]
    assert "100,101" not in str(result)
    assert "private operator note" not in str(result)


def test_inspection_refuses_duplicate_or_malformed_backup_job_ids():
    with pytest.raises(ValueError, match="duplicate backup-job IDs"):
        inspect_candidates({"vmid": "200", "jobs": [job(), job()]})
    with pytest.raises(ValueError, match="malformed ID"):
        inspect_candidates({"vmid": "200", "jobs": [job(id="unsafe id")]})


@pytest.mark.parametrize(
    "updated",
    [
        job(vmid="100,101,200"),
        job(schedule="03:00"),
        job(storage="other-store"),
    ],
)
def test_prewrite_refresh_refuses_membership_or_option_drift(updated):
    plan = plan_for()

    with pytest.raises(ValueError, match="changed after the initial inspection"):
        assert_unchanged(plan, updated)


def test_readback_requires_exact_membership_and_preserves_every_option():
    plan = plan_for()
    final = job(vmid=plan["desired_vmid"])
    result = verify_readback(plan, final)

    assert result == {"verified": True, "member_count": 3, "target_member": True}
    with pytest.raises(ValueError, match="option change"):
        verify_readback(plan, job(vmid=plan["desired_vmid"], storage="other-store"))
    with pytest.raises(ValueError, match="VMID readback"):
        verify_readback(plan, job(vmid="100,101"))


def test_inactive_selector_fields_normalize_for_prewrite_and_readback():
    initial = job(all=0, pool="", exclude=None)
    plan = plan_for(detail=initial)
    omitted = job(vmid=plan["desired_vmid"])

    assert "all" not in plan["before"]
    assert "pool" not in plan["before"]
    assert "exclude" not in plan["before"]
    assert_unchanged(plan, job())
    assert verify_readback(plan, omitted)["verified"] is True


def test_helper_refusal_messages_are_allow_listed_and_never_echo_private_values():
    assert _safe_refusal(ValueError("The selected backup job uses an unsupported pool or exclude selector.")) == (
        "The selected backup job uses an unsupported pool or exclude selector."
    )
    assert _safe_refusal(ValueError("private-job has secret-option=hidden")) == (
        "Backup-job reconciliation refused because Proxmox returned invalid data."
    )


def test_cli_refusal_reports_only_allow_listed_text(monkeypatch, capsys):
    private_detail = job(pool="private-pool-name", comment="private operator note")
    monkeypatch.setattr(sys, "argv", ["reconcile_proxmox_backup_job.py", "plan"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
        "job_id": "nightly-o11y",
        "vmid": "200",
        "jobs": [private_detail],
        "detail": private_detail,
    })))

    assert main() == 2
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "refusal": "The selected backup job uses an unsupported pool or exclude selector."
    }
    assert captured.err == ""
    assert "private-pool-name" not in captured.out
    assert "private operator note" not in captured.out


def test_playbook_requires_reviewed_dev_and_guards_every_mutation():
    plays = yaml.safe_load((ROOT / "platform/playbooks/reconcile-o11y-backup-job.yml").read_text())
    play = next(item for item in plays if item.get("name") == "Reconcile the declared o11y backup job")
    tasks = play["tasks"]
    by_name = {task["name"]: (index, task) for index, task in enumerate(tasks)}

    assert "expected_repository_sha | default('') is match('^[0-9a-f]{40}$')" in by_name[
        "Require the reviewed Dev revision and exact private o11y declarations"
    ][1]["ansible.builtin.assert"]["that"]
    assert "_controller_revision.stdout == expected_repository_sha" in by_name[
        "Require the exact reviewed controller revision"
    ][1]["ansible.builtin.assert"]["that"]
    assert "not (local_mode | default(false) | bool)" in by_name[
        "Require the reviewed Dev revision and exact private o11y declarations"
    ][1]["ansible.builtin.assert"]["that"]
    assert "inspect_only | default('true') | string | lower in ['true', 'false']" in by_name[
        "Require the reviewed Dev revision and exact private o11y declarations"
    ][1]["ansible.builtin.assert"]["that"]
    assert play["vars"]["_inspect_only"] == "{{ inspect_only | default('true') | bool }}"
    assert "hostvars[_o11y_host].o11y_backup_job_id" in play["vars"]["_backup_job_id"]
    assert "_backup_job_id is match('^[A-Za-z0-9._-]{1,64}$')" in by_name[
        "Require the private selected job ID for apply mode"
    ][1]["ansible.builtin.assert"]["that"]
    assert by_name["Require the private selected job ID for apply mode"][1]["when"] == "not _inspect_only"

    names = list(by_name)
    assert names.index("Build a sanitized backup-job candidate report") < names.index(
        "Report backup-job candidates for inventory selection"
    ) < names.index("End after read-only backup-job inspection") < names.index(
        "Require one exact private selected backup job"
    ) < names.index("Read the selected backup-job detail")
    assert by_name["End after read-only backup-job inspection"][1]["when"] == "_inspect_only"
    assert names.index("Refresh the selected backup job immediately before a write") < names.index(
        "Refuse a backup job changed after inspection"
    ) < names.index("Add the o11y VM to the explicit VMID list")
    put = by_name["Add the o11y VM to the explicit VMID list"][1]
    assert put["when"] == ["_backup_plan.changed | bool", "not ansible_check_mode"]
    assert put["ansible.builtin.uri"]["method"] == "PUT"
    assert put["ansible.builtin.uri"]["body_format"] == "form-urlencoded"
    assert set(put["ansible.builtin.uri"]["body"]) == {"vmid"}
    assert put.get("no_log") is True

    api = [task for task in tasks if "ansible.builtin.uri" in task]
    assert all(task.get("no_log") is True for task in api)
    assert all(task.get("check_mode") is False for task in api if task["ansible.builtin.uri"]["method"] == "GET")
    assert not any(task.get("check_mode") is False for task in api if task["ansible.builtin.uri"]["method"] == "PUT")

    helper_assertions = {
        "Require safe backup-job inspection data": "_backup_inspection_result",
        "Require a safe explicit-VMID backup-job plan": "_backup_plan_result",
        "Refuse a backup job changed after inspection": "_backup_recheck_result",
        "Require exact backup-job readback": "_backup_verify_result",
    }
    for name, result_name in helper_assertions.items():
        task = by_name[name][1]
        assert "refusal" in task["ansible.builtin.assert"]["fail_msg"]
        assert f"{result_name}.stdout | from_json" in task["ansible.builtin.assert"]["fail_msg"]
        assert "stderr" not in task["ansible.builtin.assert"]["fail_msg"]
    for task in tasks:
        argv = task.get("ansible.builtin.command", {}).get("argv", [])
        if len(argv) > 2 and argv[2] in {"inspect", "plan", "recheck", "verify"}:
            assert task.get("no_log") is True


def test_semaphore_template_is_dev_bound_and_uses_required_reviewed_sha():
    templates = yaml.safe_load((ROOT / "platform/semaphore/templates.yml").read_text())["templates"]
    template = next(item for item in templates if item["name"] == "Reconcile o11y Backup Job (Dev)")

    assert template["repository"] == "agent-cloud dev"
    assert template["playbook"] == "platform/playbooks/reconcile-o11y-backup-job.yml"
    assert template["survey_vars"] == [
        {
            "name": "expected_repository_sha",
            "title": "Reviewed commit SHA",
            "description": "Exact reviewed Dev revision for backup-job reconciliation",
            "type": "string",
            "required": True,
        },
        {
            "name": "inspect_only",
            "title": "Inspect backup jobs only",
            "description": (
                "true lists sanitized candidates and stops before any write; "
                "false uses the private inventory job ID"
            ),
            "type": "enum",
            "required": True,
            "default_value": "true",
            "values": [
                {"name": "Inspect only", "value": "true"},
                {"name": "Apply declared job membership", "value": "false"},
            ],
        },
    ]
