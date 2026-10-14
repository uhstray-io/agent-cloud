"""Safety and convergence checks for the Dev-bound o11y backup-job reconciler."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest
import yaml
from jinja2 import Environment, StrictUndefined

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "platform/playbooks/files"))

from reconcile_proxmox_backup_job import (  # noqa: E402
    HISTORY_LOOKBACK_SECONDS,
    HISTORY_MAX_ROWS,
    HISTORY_PAGE_SIZE,
    HISTORY_RECENCY_SECONDS,
    _safe_refusal,
    assert_unchanged,
    inspect_candidates,
    main,
    plan_history_pages,
    prepare_history_query,
    prepare_plan,
    summarize_task_history,
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


def permission_response(rights=None, status=200, path="/nodes/node-a"):
    return {"status": status, "json": {"data": {path: {"Sys.Audit": 1} if rights is None else rights}}}


def task_row(index, status="OK", **overrides):
    row = {
        "upid": f"UPID:node-a:task-{index}",
        "starttime": 1_000 + index,
        "endtime": 1_001 + index,
        "type": "vzdump",
        "id": "200",
        "status": status,
    }
    row.update(overrides)
    return row


def task_page(rows, total):
    return {"status": 200, "json": {"data": rows, "total": total}}


def history_plan(until=2_000):
    return prepare_history_query({
        "node": "node-a",
        "vmid": "200",
        "until": until,
        "permissions_response": permission_response(),
    })


def test_history_query_requires_positive_node_scoped_sys_audit():
    for permissions in (
        permission_response({"Sys.Audit": True}),
        permission_response({"Sys.Audit": False}),
        permission_response({"Sys.Audit": "1"}),
        permission_response({"Sys.Audit": None}),
        permission_response({}),
        permission_response({"Sys.Audit": 1}, path="/nodes/other-node"),
        permission_response(status=403),
        {"status": 200, "json": {"data": []}},
    ):
        result = prepare_history_query({
            "node": "node-a", "vmid": "200", "until": 2_000,
            "permissions_response": permissions,
        })
        assert result == {
            "read_allowed": False,
            "summary": {
                "outcome": "unknown", "reason": "permission_unverified",
                "task_count": None, "job_attribution": "unavailable",
                "latest_success_within_26h": None,
            },
        }
        assert "Sys.Audit" not in json.dumps(result)
        assert "/nodes/other-node" not in json.dumps(result)


@pytest.mark.parametrize("propagation", [0, 1])
def test_history_query_accepts_valid_sys_audit_propagation_flags(propagation):
    result = prepare_history_query({
        "node": "node-a", "vmid": "200", "until": 2_000,
        "permissions_response": permission_response({"Sys.Audit": propagation}),
    })

    assert result["read_allowed"] is True
    assert result["query"]["source"] == "archive"


def test_history_query_uses_archived_vzdump_vm_window_and_30_day_lookback():
    result = history_plan()

    assert result["read_allowed"] is True
    assert result["query"] == {
        "source": "archive", "typefilter": "vzdump", "vmid": 200,
        "since": max(0, 2_000 - HISTORY_LOOKBACK_SECONDS), "until": 2_000,
        "start": 0, "limit": HISTORY_PAGE_SIZE,
    }
    assert result["summary"] is None


def test_history_empty_result_requires_permission_and_reports_no_job_attribution():
    plan = history_plan()
    result = summarize_task_history({
        "plan": plan, "first_page": task_page([], 0), "later_pages": [],
    })

    assert result == {
        "outcome": "no_archived_task_in_lookback", "reason": None,
        "task_count": 0, "job_attribution": "unavailable",
        "latest_success_within_26h": False,
    }


def test_empty_task_page_without_total_is_a_valid_empty_result():
    plan = history_plan()
    first = {"status": 200, "json": {"data": []}}

    assert plan_history_pages({"read_allowed": True, "first_page": first}) == {
        "page_starts": [], "summary": None,
    }
    assert summarize_task_history({"plan": plan, "first_page": first, "later_pages": []}) == {
        "outcome": "no_archived_task_in_lookback", "reason": None,
        "task_count": 0, "job_attribution": "unavailable",
        "latest_success_within_26h": False,
    }


def test_nonempty_task_page_without_total_is_invalid():
    plan = history_plan()
    first = {"status": 200, "json": {"data": [task_row(1)]}}

    result = summarize_task_history({"plan": plan, "first_page": first, "later_pages": []})
    assert result["outcome"] == "unknown"
    assert result["reason"] == "response_invalid"


def test_history_exact_upper_bound_is_paginated_and_accepted():
    plan = history_plan()
    rows = [task_row(index) for index in range(HISTORY_MAX_ROWS)]
    first = task_page(rows[:HISTORY_PAGE_SIZE], HISTORY_MAX_ROWS)
    page_plan = plan_history_pages({"read_allowed": True, "first_page": first})
    assert page_plan == {
        "page_starts": [50, 100, 150, 200], "summary": None,
    }
    later = [
        task_page(rows[offset:offset + HISTORY_PAGE_SIZE], HISTORY_MAX_ROWS)
        for offset in range(HISTORY_PAGE_SIZE, HISTORY_MAX_ROWS, HISTORY_PAGE_SIZE)
    ]

    result = summarize_task_history({"plan": plan, "first_page": first, "later_pages": later})
    assert result == {
        "outcome": "success", "reason": None, "task_count": HISTORY_MAX_ROWS,
        "job_attribution": "unavailable",
        "latest_success_within_26h": True,
    }


def test_history_over_bound_and_incomplete_pages_are_unknown():
    plan = history_plan()
    over_bound = task_page([task_row(index) for index in range(HISTORY_PAGE_SIZE)], HISTORY_MAX_ROWS + 1)
    assert plan_history_pages({"read_allowed": True, "first_page": over_bound}) == {
        "page_starts": [],
        "summary": {
            "outcome": "unknown", "reason": "pagination_limit_exceeded",
            "task_count": None, "job_attribution": "unavailable",
            "latest_success_within_26h": None,
        },
    }
    assert summarize_task_history({"plan": plan, "first_page": over_bound})["outcome"] == "unknown"

    first = task_page([task_row(index) for index in range(HISTORY_PAGE_SIZE)], 51)
    gap = summarize_task_history({"plan": plan, "first_page": first, "later_pages": []})
    malformed = summarize_task_history({
        "plan": plan, "first_page": {"status": 200, "json": {"data": [], "total": "0"}},
    })
    request_failed = summarize_task_history({
        "plan": plan, "first_page": {"status": 403, "json": {}},
    })
    mismatch = summarize_task_history({
        "plan": plan,
        "first_page": first,
        "later_pages": [task_page([task_row(50)], 52)],
    })
    assert [gap["reason"], malformed["reason"], mismatch["reason"]] == [
        "pagination_incomplete", "response_invalid", "pagination_incomplete",
    ]
    assert request_failed == {
        "outcome": "unknown", "reason": "request_failed", "task_count": None,
        "job_attribution": "unavailable",
        "latest_success_within_26h": None,
    }


@pytest.mark.parametrize(
    ("status", "expected", "recency"),
    [
        ("OK", "success", True),
        ("WARNINGS: 2", "incomplete", False),
        ("ERROR: private backup path failed", "failed", False),
        (None, "unknown", None),
        ("unexpected status", "unknown", None),
    ],
)
def test_history_classifies_latest_status_without_exposing_it(status, expected, recency):
    plan = history_plan()
    result = summarize_task_history({
        "plan": plan,
        "first_page": task_page([task_row(1, status=status)], 1),
        "later_pages": [],
    })
    encoded = json.dumps(result)
    assert result["outcome"] == expected
    assert result["job_attribution"] == "unavailable"
    assert result["latest_success_within_26h"] is recency
    assert "private backup path failed" not in encoded
    assert "UPID:" not in encoded


def test_history_schema_gaps_and_wrong_identity_fail_closed_without_echo():
    plan = history_plan()
    malformed = summarize_task_history({
        "plan": plan,
        "first_page": task_page([task_row(1, comment="private detail", endtime="bad")], 1),
        "later_pages": [],
    })
    wrong_id = summarize_task_history({
        "plan": plan,
        "first_page": task_page([task_row(1, id="201", status="ERROR: private")], 1),
        "later_pages": [],
    })
    assert malformed["outcome"] == "unknown"
    assert malformed["reason"] == "task_schema_invalid"
    assert wrong_id["outcome"] == "unknown"
    assert wrong_id["reason"] == "task_identity_invalid"
    assert "private" not in json.dumps(malformed | wrong_id)


def test_history_recency_is_true_at_the_inclusive_26_hour_boundary_and_false_after():
    exact_plan = history_plan(until=HISTORY_RECENCY_SECONDS + 1)
    exact = summarize_task_history({
        "plan": exact_plan,
        "first_page": task_page([task_row(1, starttime=1)], 1),
        "later_pages": [],
    })
    old_plan = history_plan(until=HISTORY_RECENCY_SECONDS + 2)
    old = summarize_task_history({
        "plan": old_plan,
        "first_page": task_page([task_row(1, starttime=1)], 1),
        "later_pages": [],
    })

    assert exact["latest_success_within_26h"] is True
    assert old["outcome"] == "success"
    assert old["latest_success_within_26h"] is False


@pytest.mark.parametrize("cutoff", [True, False, "2000", 2000.0, None, 0, -1])
def test_history_rejects_malformed_cutoff_types_as_unknown(cutoff):
    plan = history_plan()
    plan["query"]["until"] = cutoff
    result = summarize_task_history({
        "plan": plan,
        "first_page": task_page([task_row(1)], 1),
        "later_pages": [],
    })

    assert result == {
        "outcome": "unknown", "reason": "response_invalid", "task_count": None,
        "job_attribution": "unavailable", "latest_success_within_26h": None,
    }


def test_conflicting_latest_statuses_make_recency_unknown_and_output_stays_allowlisted():
    plan = history_plan()
    rows = [
        task_row(1, status="OK", starttime=1_900, endtime=1_901),
        task_row(2, status="WARNINGS: 1", starttime=1_900, endtime=1_901),
    ]
    result = summarize_task_history({
        "plan": plan, "first_page": task_page(rows, 2), "later_pages": [],
    })

    assert result == {
        "outcome": "unknown", "reason": "task_status_unavailable", "task_count": 2,
        "job_attribution": "unavailable", "latest_success_within_26h": None,
    }
    assert set(result) == {
        "outcome", "reason", "task_count", "job_attribution", "latest_success_within_26h",
    }
    assert "UPID:" not in json.dumps(result)
    assert "1900" not in json.dumps(result)


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
    history_tasks = [
        task for task in tasks
        if task.get("name", "").startswith((
            "Read effective permissions on the declared Proxmox node",
            "Read the first archived vzdump task page",
            "Read later archived vzdump task pages",
        ))
    ]
    assert len(history_tasks) == 3
    assert all(task.get("no_log") is True for task in history_tasks)
    assert all(task.get("check_mode") is False for task in history_tasks)
    assert history_tasks[0]["when"] == "_inspect_only"
    assert "_inspect_only" in history_tasks[1]["when"]
    assert "_inspect_only" in history_tasks[2]["when"]
    for task_name in (
        "Plan bounded archived backup-task history inspection",
        "Keep the bounded backup-task history query private to this run",
        "Plan remaining bounded task-history pages",
        "Keep the task-history pagination plan private to this run",
        "Summarize archived task history without exposing task identity or details",
        "Keep the sanitized task-history summary private to this run",
    ):
        assert by_name[task_name][1]["when"] == "_inspect_only"
    assert "source=archive" in history_tasks[1]["ansible.builtin.uri"]["url"]
    assert "typefilter=vzdump" in history_tasks[1]["ansible.builtin.uri"]["url"]
    assert "~ item ~ '&limit=50'" in history_tasks[2]["ansible.builtin.uri"]["url"]
    query_vars = {
        "_pve_host": "https://pve.example",
        "_node": "node-a",
        "_vmid": "200",
        "_backup_history_query": {"query": {"since": 1_700_000_000, "until": 1_702_592_000}},
    }
    first_url = Environment(undefined=StrictUndefined).from_string(
        history_tasks[1]["ansible.builtin.uri"]["url"]
    ).render(query_vars)
    later_url = Environment(undefined=StrictUndefined).from_string(
        history_tasks[2]["ansible.builtin.uri"]["url"]
    ).render(query_vars | {"item": 50})
    assert first_url == (
        "https://pve.example/api2/json/nodes/node-a/tasks?source=archive&typefilter=vzdump&vmid=200"
        "&since=1700000000&until=1702592000&start=0&limit=50"
    )
    assert later_url.endswith("&since=1700000000&until=1702592000&start=50&limit=50")
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
        if len(argv) > 2 and argv[2] in {
            "inspect", "plan", "recheck", "verify", "history-query", "history-pages", "history-summary",
        }:
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
