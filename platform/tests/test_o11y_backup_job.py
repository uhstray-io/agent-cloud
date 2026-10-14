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
    NODE_TASK_LOOKBACK_SECONDS,
    TASK_LOG_MAX_LINES,
    _safe_refusal,
    assert_unchanged,
    inspect_candidates,
    main,
    plan_history_pages,
    plan_node_failure_log_page,
    prepare_history_query,
    prepare_node_failure_plan,
    prepare_node_task_query,
    prepare_plan,
    summarize_node_failure_identity,
    summarize_node_failure_log,
    summarize_node_task_history,
    summarize_scheduler_state,
    summarize_selected_storage,
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
        permission_response({"Sys.Audit": 0}),
        permission_response({"Sys.Audit": True}),
        permission_response({"Sys.Audit": False}),
        permission_response({"Sys.Audit": "1"}),
        permission_response({"Sys.Audit": None}),
        permission_response({}),
        permission_response({"Sys.Audit": 1}, path="/nodes/other-node"),
        permission_response(status=403),
        {"status": True, "json": {"data": {"/nodes/node-a": {"Sys.Audit": 1}}}},
        {"status": "200", "json": {"data": {"/nodes/node-a": {"Sys.Audit": 1}}}},
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


def test_history_query_accepts_only_integer_one_for_sys_audit():
    result = prepare_history_query({
        "node": "node-a", "vmid": "200", "until": 2_000,
        "permissions_response": permission_response({"Sys.Audit": 1}),
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


def scheduler_response(data=None, status=200, permissions_verified=True):
    return {
        "permissions_verified": permissions_verified,
        "response": {
            "status": status,
            "json": {"data": {
                "service": "pvescheduler",
                "name": "pvescheduler",
                "desc": "private node description",
                "active-state": "active",
                "state": "running",
                "unit-state": "enabled",
            } if data is None else data},
        },
    }


def test_scheduler_state_reports_only_a_fixed_running_snapshot():
    result = summarize_scheduler_state(scheduler_response())

    assert result == {"status": "running", "reason": None}
    assert set(result) == {"status", "reason"}
    assert "private node description" not in json.dumps(result)


def test_scheduler_state_reports_known_stopped_states():
    for active_state, state in (("inactive", "dead"), ("failed", "failed")):
        stopped = scheduler_response(data={
            "service": "pvescheduler", "name": "pvescheduler", "desc": "private",
            "active-state": active_state, "state": state, "unit-state": "disabled",
        })
        assert summarize_scheduler_state(stopped) == {"status": "not_running", "reason": None}


def test_scheduler_state_permission_denial_and_http_errors_are_unknown():
    denied = summarize_scheduler_state(scheduler_response(permissions_verified=False))
    forbidden = summarize_scheduler_state(scheduler_response(status=403))
    server_error = summarize_scheduler_state(scheduler_response(status=500))

    assert denied == {"status": "unknown", "reason": "permission_unverified"}
    assert forbidden == {"status": "unknown", "reason": "request_failed"}
    assert server_error == {"status": "unknown", "reason": "request_failed"}


def test_scheduler_state_malformed_schema_and_wrong_identity_fail_closed():
    valid = scheduler_response()["response"]["json"]["data"]
    malformed_payload = summarize_scheduler_state([])
    malformed = summarize_scheduler_state({
        "permissions_verified": True,
        "response": {"status": 200, "json": {"data": valid | {"state": 1}}},
    })
    unknown_field = summarize_scheduler_state({
        "permissions_verified": True,
        "response": {"status": 200, "json": {"data": valid | {"extra": "private"}}},
    })
    wrong_service = summarize_scheduler_state(scheduler_response(data=valid | {"service": "other"}))
    wrong_name = summarize_scheduler_state(scheduler_response(data=valid | {"name": "other"}))

    assert malformed == {"status": "unknown", "reason": "response_invalid"}
    assert malformed_payload == {"status": "unknown", "reason": "response_invalid"}
    assert unknown_field == {"status": "unknown", "reason": "response_invalid"}
    assert wrong_service == {"status": "unknown", "reason": "service_identity_invalid"}
    assert wrong_name == {"status": "unknown", "reason": "service_identity_invalid"}
    assert "private" not in json.dumps(malformed | unknown_field | wrong_service | wrong_name)


def test_scheduler_state_unrecognized_values_remain_unknown():
    result = summarize_scheduler_state(scheduler_response(data={
        "service": "pvescheduler", "name": "pvescheduler", "desc": "private",
        "active-state": "activating", "state": "start", "unit-state": "enabled",
    }))

    assert result == {"status": "unknown", "reason": "state_unavailable"}


@pytest.mark.parametrize(
    ("active_state", "state", "unit_state"),
    [
        ("active", "running", "future-unit-state"),
        ("unknown", "dead", "enabled"),
        ("inactive", "future-state", "disabled"),
        ("active", "dead", "enabled"),
        ("inactive", "running", "disabled"),
        ("active", "running", "not-found"),
    ],
)
def test_scheduler_state_invalid_or_contradictory_combinations_remain_unknown(
    active_state, state, unit_state,
):
    result = summarize_scheduler_state(scheduler_response(data={
        "service": "pvescheduler", "name": "pvescheduler", "desc": "private",
        "active-state": active_state, "state": state, "unit-state": unit_state,
    }))

    assert result == {"status": "unknown", "reason": "state_unavailable"}


def node_history_plan(until=2_000):
    return prepare_node_task_query({
        "node": "node-a", "vmid": "200", "until": until,
        "permissions_response": permission_response(),
    })


def node_task_row(index, **overrides):
    row = {
        "starttime": 1_000 + index,
        "endtime": 1_001 + index, "type": "vzdump", "id": "201", "status": "OK",
    }
    row.update(overrides)
    if "endtime" not in overrides:
        row["endtime"] = row["starttime"] + 1
    row.setdefault(
        "upid",
        f"UPID:node-a:00000001:00000001:{row['starttime']:08x}:vzdump:{row['id'] or ''}:root@pam:",
    )
    return row


def test_node_history_query_requires_existing_exact_node_scoped_sys_audit():
    for permissions in (
        permission_response({"Sys.Audit": 0}),
        permission_response({"Sys.Audit": True}),
        permission_response({"Sys.Audit": False}),
        permission_response({"Sys.Audit": "1"}),
        permission_response({}),
        permission_response(path="/nodes/other-node"),
        permission_response(status=403),
        {"status": True, "json": {"data": {"/nodes/node-a": {"Sys.Audit": 1}}}},
        {"status": "200", "json": {"data": {"/nodes/node-a": {"Sys.Audit": 1}}}},
        {"status": 200, "json": {"data": []}},
    ):
        result = prepare_node_task_query({
            "node": "node-a", "vmid": "200", "until": 2_000,
            "permissions_response": permissions,
        })
        assert result["read_allowed"] is False
        assert result["summary"]["status"] == "unknown"
        assert result["summary"]["reason"] == "permission_unverified"


def test_node_history_query_uses_fixed_72_hour_window_without_vmid_filter():
    until = 2_000_000_000
    result = prepare_node_task_query({
        "node": "node-a", "vmid": "200", "until": until,
        "permissions_response": permission_response(),
    })

    assert result["read_allowed"] is True
    assert result["query"] == {
        "source": "archive", "typefilter": "vzdump",
        "since": until - NODE_TASK_LOOKBACK_SECONDS, "until": until,
        "start": 0, "limit": HISTORY_PAGE_SIZE,
    }
    assert NODE_TASK_LOOKBACK_SECONDS == 72 * 60 * 60
    assert "vmid" not in result["query"]


@pytest.mark.parametrize("node", ["../node-a", "node/a", "node.a", "node-a/"])
def test_node_history_query_rejects_nodes_outside_proxmox_node_syntax(node):
    with pytest.raises(ValueError):
        prepare_node_task_query({
            "node": node, "vmid": "200", "until": 2_000,
            "permissions_response": permission_response(),
        })


def test_node_history_query_accepts_only_integer_one_for_sys_audit():
    result = prepare_node_task_query({
        "node": "node-a", "vmid": "200", "until": 2_000,
        "permissions_response": permission_response({"Sys.Audit": 1}),
    })

    assert result["read_allowed"] is True


def test_node_history_empty_and_target_plus_missing_id_rows_are_allowlisted():
    plan = node_history_plan()
    empty = summarize_node_task_history({
        "plan": plan, "first_page": task_page([], 0), "later_pages": [],
    })
    rows = [
        node_task_row(1, starttime=1_800, endtime=1_801, id="200", user="private-user"),
        node_task_row(
            2, starttime=1_900, endtime=1_901, id=None,
            status="ERROR: private-status-detail", user="private-user",
        ),
        node_task_row(3, starttime=1_950, endtime=1_951, id=""),
    ]
    result = summarize_node_task_history({
        "plan": plan, "first_page": task_page(rows, 3), "later_pages": [],
    })

    assert empty["total_recent_node_vzdump_tasks"] == 0
    assert empty["latest_task_outcome"] == "none"
    assert result == {
        "status": "complete", "reason": None,
        "total_recent_node_vzdump_tasks": 3,
        "target_vmid_task_id_count": 1,
        "potential_multi_vm_or_unattributed_task_count": 2,
        "job_attribution": "unavailable",
        "latest_task_outcome": "success", "latest_task_recency": "within_26h",
    }
    encoded = json.dumps(result)
    for private in ("UPID:", '"id"', "node-a", "200", "private-user", "private-status-detail"):
        assert private not in encoded


def test_node_history_unknown_latest_status_returns_fixed_unknown_receipt():
    result = summarize_node_task_history({
        "plan": node_history_plan(),
        "first_page": task_page([node_task_row(1, status="unexpected status")], 1),
        "later_pages": [],
    })

    assert result == {
        "status": "unknown", "reason": "task_status_unavailable",
        "job_attribution": "unavailable",
        "total_recent_node_vzdump_tasks": None,
        "target_vmid_task_id_count": None,
        "potential_multi_vm_or_unattributed_task_count": None,
        "latest_task_outcome": "unknown", "latest_task_recency": "unknown",
    }
    assert "private-UPID" not in json.dumps(result)


def test_node_failure_selection_requires_permission_valid_unique_recent_failure():
    plan = node_history_plan()
    failed = node_task_row(1, starttime=1_900, status="ERROR: secret detail")
    result = prepare_node_failure_plan({
        "plan": plan, "first_page": task_page([failed], 1), "later_pages": [],
    })

    assert result["read_allowed"] is True
    assert result["metadata_path"].startswith("/api2/json/nodes/node-a/tasks/UPID%3A")
    assert "UPID:node-a" not in result["metadata_path"]
    assert "secret detail" not in json.dumps(result)

    for old_plan, row in (
        (plan, node_task_row(2, starttime=1_900, status="OK")),
        (node_history_plan(until=200_000), node_task_row(3, starttime=1_000, status="ERROR: old")),
    ):
        not_applicable = prepare_node_failure_plan({
            "plan": old_plan, "first_page": task_page([row], 1), "later_pages": [],
        })
        assert not_applicable["read_allowed"] is False
        assert "metadata_path" not in not_applicable
        assert not_applicable["diagnostic"]["status"] == "not_applicable"

    ambiguous = prepare_node_failure_plan({
        "plan": plan,
        "first_page": task_page([
            node_task_row(4, starttime=1_900, id="201", status="ERROR: one"),
            node_task_row(5, starttime=1_900, id="202", status="ERROR: two"),
        ], 2),
        "later_pages": [],
    })
    assert ambiguous["read_allowed"] is False
    assert ambiguous["diagnostic"] == {
        "status": "unknown", "reason": "ambiguous_latest_task", "failure_hint": "unknown",
    }

    denied = dict(plan, read_allowed=False)
    refused = prepare_node_failure_plan({"plan": denied, "first_page": task_page([failed], 1), "later_pages": []})
    assert refused["read_allowed"] is False
    assert refused["diagnostic"]["reason"] == "permission_unverified"


def test_unprefixed_proxmox_job_error_plans_only_the_bounded_metadata_get():
    result = prepare_node_failure_plan({
        "plan": node_history_plan(),
        "first_page": task_page([node_task_row(1, starttime=1_900, status="job errors")], 1),
        "later_pages": [],
    })

    assert result["read_allowed"] is True
    assert result["metadata_path"].startswith("/api2/json/nodes/node-a/tasks/UPID%3A")
    assert result["metadata_path"].endswith("/log?start=0&limit=1")
    assert "job%20errors" not in result["metadata_path"]
    assert "job errors" not in json.dumps(result)


def test_node_failure_selection_refuses_malformed_task_identity_and_listing_drift():
    malformed = node_task_row(1, starttime=1_900, status="ERROR: private", upid="private-UPID")
    result = prepare_node_failure_plan({
        "plan": node_history_plan(), "first_page": task_page([malformed], 1), "later_pages": [],
    })
    assert result["read_allowed"] is False
    assert result["diagnostic"]["reason"] == "node_history_unavailable"
    assert "private-UPID" not in json.dumps(result)

    drift = prepare_node_failure_plan({
        "plan": node_history_plan(), "first_page": task_page([node_task_row(1)], 2), "later_pages": [],
    })
    assert drift["read_allowed"] is False
    assert "metadata_path" not in drift


@pytest.mark.parametrize("upid", [
    "UPID:node-a:0000001:00000001:0000076c:vzdump:200:root@pam:",
    "UPID:node-a:00000001:0000000010:0000076c:vzdump:200:root@pam:",
    "UPID:node-a:00000001:00000001:000076c:vzdump:200:root@pam:",
    "UPID:node-a:00000001:00000001:0000076c:vzdump:../200:root@pam:",
    "UPID:node-a:00000001:00000001:0000076c:vzdump:200/../201:root@pam:",
    "UPID:node-a:00000001:00000001:0000076c:vzdump:200?:root@pam:",
    "UPID:node-a:00000001:00000001:0000076c:vzdump:200#root@pam:",
    "UPID:node-a:00000001:00000001:0000076c:vzdump:200%2froot@pam:",
    "UPID:node-b:00000001:00000001:0000076c:vzdump:200:root@pam:",
    "UPID:node-a:00000001:00000001:0000076c:other:200:root@pam:",
    "UPID:node-a:00000001:00000001:0000076c:vzdump:200:unsupported-user:",
    "UPID:node-a:00000001:00000001:0000076c:vzdump:200:root@pam",
    "UPID:node-a:00000001:00000001:0000076c:vzdump:200:root@pam:trailing",
])
def test_node_failure_selection_rejects_malformed_or_path_injected_upids(upid):
    result = prepare_node_failure_plan({
        "plan": node_history_plan(),
        "first_page": task_page([node_task_row(1, starttime=1_900, status="job errors", upid=upid)], 1),
        "later_pages": [],
    })

    assert result["read_allowed"] is False
    assert result["diagnostic"]["reason"] == "node_history_unavailable"
    assert "metadata_path" not in result


@pytest.mark.parametrize("user", ["root@pam", "operator@pam!automation"])
def test_node_failure_selection_accepts_supported_upid_users(user):
    upid = f"UPID:node-a:00000001:00000001:0000076c:vzdump::{user}:"
    result = prepare_node_failure_plan({
        "plan": node_history_plan(),
        "first_page": task_page([node_task_row(1, starttime=1_900, status="job errors", upid=upid)], 1),
        "later_pages": [],
    })

    assert result["read_allowed"] is True


def test_task_log_metadata_read_plans_only_the_last_bounded_page():
    selected = prepare_node_failure_plan({
        "plan": node_history_plan(),
        "first_page": task_page([node_task_row(1, starttime=1_900, status="ERROR: private")], 1),
        "later_pages": [],
    })
    result = plan_node_failure_log_page({
        **selected,
        "metadata_response": {"status": 200, "json": {"total": 150, "data": [{"n": 1, "t": "private first line"}]}},
    })

    assert result["read_allowed"] is True
    assert result["start"] == 50
    assert result["limit"] == TASK_LOG_MAX_LINES
    assert "start=50&limit=100" in result["log_path"]
    assert "private first line" not in json.dumps(result)

    small = plan_node_failure_log_page({
        **selected,
        "metadata_response": {"status": 200, "json": {"total": 3, "data": [{"n": 1, "t": "secret"}]}},
    })
    assert small["start"] == 0
    assert small["limit"] == 3


def test_task_log_accepts_realistic_one_based_proxmox_rows_and_rejects_zero_based_rows():
    metadata = plan_node_failure_log_page({
        "read_allowed": True,
        "metadata_path": "/api2/json/nodes/node-a/tasks/UPID%3Ax/log?start=0&limit=1",
        "metadata_response": {"status": 200, "json": {"total": 3, "data": [{"n": 1, "t": "first"}]}},
    })
    assert metadata["read_allowed"] is True
    assert metadata["start"] == 0
    assert metadata["limit"] == 3

    summary = summarize_node_failure_log({
        "page_plan": metadata,
        "log_response": {
            "status": 200,
            "json": {"total": 3, "data": [
                {"n": 1, "t": "starting backup"},
                {"n": 2, "t": "No space left on device"},
                {"n": 3, "t": "finished with errors"},
            ]},
        },
    })
    assert summary == {"status": "diagnosed", "reason": None, "failure_hint": "no_space"}

    final_page = summarize_node_failure_log({
        "page_plan": {"read_allowed": True, "total": 150, "start": 50, "limit": 100},
        "log_response": {"status": 200, "json": {"total": 150, "data": [
            {"n": index + 51, "t": "No space left on device" if index == 49 else "backup output"}
            for index in range(100)
        ]}},
    })
    assert final_page == {"status": "diagnosed", "reason": None, "failure_hint": "no_space"}

    zero_based_metadata = plan_node_failure_log_page({
        "read_allowed": True,
        "metadata_path": "/api2/json/nodes/node-a/tasks/UPID%3Ax/log?start=0&limit=1",
        "metadata_response": {"status": 200, "json": {"total": 3, "data": [{"n": 0, "t": "first"}]}},
    })
    assert zero_based_metadata["read_allowed"] is False
    assert zero_based_metadata["diagnostic"]["status"] == "unknown"

    zero_based_page = summarize_node_failure_log({
        "page_plan": {"read_allowed": True, "total": 101, "start": 1, "limit": 100},
        "log_response": {"status": 200, "json": {"total": 101, "data": [
            {"n": index + 1, "t": "No space left on device" if index == 0 else "backup output"}
            for index in range(100)
        ]}},
    })
    assert zero_based_page == {"status": "unknown", "reason": "log_invalid", "failure_hint": "unknown"}


@pytest.mark.parametrize("text", ["no content", "unable to open file - private path"])
def test_task_log_synthetic_empty_or_missing_file_rows_are_never_diagnosed(text):
    result = summarize_node_failure_log({
        "page_plan": {"read_allowed": True, "total": 1, "start": 0, "limit": 1},
        "log_response": {"status": 200, "json": {"total": 1, "data": [{"n": 1, "t": text}]}},
    })
    assert result == {"status": "unknown", "reason": "log_invalid", "failure_hint": "unknown"}
    assert text not in json.dumps(result)


@pytest.mark.parametrize(
    "body",
    [
        {"total": True, "data": [{"n": 1, "t": "secret"}]},
        {"total": 10_000_001, "data": [{"n": 1, "t": "secret"}]},
        {"total": 2, "data": []},
        {"total": 2, "data": [{"n": 2, "t": "secret"}]},
        {"total": 2, "data": [{"n": 1, "t": "x" * 4097}]},
        {"total": 2, "data": [{"n": 1, "t": "secret", "private": "value"}]},
        {"total": 1, "data": [{"n": 1, "t": "no content"}]},
        {"total": 1, "data": [{"n": 1, "t": "unable to open file - private path"}]},
    ],
)
def test_task_log_metadata_schema_and_size_fail_closed(body):
    selected = {
        "read_allowed": True,
        "metadata_path": "/api2/json/nodes/node-a/tasks/UPID%3Ax/log?start=0&limit=1",
    }
    result = plan_node_failure_log_page({
        **selected, "metadata_response": {"status": 200, "json": body},
    })
    assert result["read_allowed"] is False
    assert result["diagnostic"] == {
        "status": "unknown", "reason": "metadata_invalid", "failure_hint": "unknown",
    }
    assert "secret" not in json.dumps(result)


@pytest.mark.parametrize(
    ("line", "hint"),
    [
        ("No space left on device at /private/path", "no_space"),
        ("storage unavailable: private-storage", "storage_unavailable"),
        ("Permission denied for user@example", "permission_denied"),
        ("unable to acquire lock for VM 200", "guest_lock"),
        ("connection timed out to https://private", "transport"),
        ("failure detail with secret=private-value", "unknown"),
    ],
)
def test_task_log_failure_hint_is_allowlisted_and_redacts_raw_content(line, hint):
    page_plan = {"read_allowed": True, "total": 1, "start": 0, "limit": 1}
    result = summarize_node_failure_log({
        "page_plan": page_plan,
        "log_response": {"status": 200, "json": {"total": 1, "data": [{"n": 1, "t": line}]}},
    })
    assert result == {"status": "diagnosed", "reason": None, "failure_hint": hint}
    assert line not in json.dumps(result)
    assert "/private/" not in json.dumps(result)
    assert "private-value" not in json.dumps(result)


def test_task_log_cli_output_and_malformed_input_are_fixed_and_redacted(monkeypatch, capsys):
    line = "No space left at /private/storage with token=secret-value"
    monkeypatch.setattr(sys, "argv", ["reconcile_proxmox_backup_job.py", "node-failure-summary"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
        "page_plan": {"read_allowed": True, "total": 1, "start": 0, "limit": 1},
        "log_response": {"status": 200, "json": {"total": 1, "data": [{"n": 1, "t": line}]}},
    })))
    assert main() == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"status": "diagnosed", "reason": None, "failure_hint": "no_space"}
    assert captured.err == ""
    assert line not in captured.out and "/private/storage" not in captured.out and "secret-value" not in captured.out

    monkeypatch.setattr(sys, "argv", ["reconcile_proxmox_backup_job.py", "node-failure-summary"])
    monkeypatch.setattr(sys, "stdin", io.StringIO("malformed private payload"))
    assert main() == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"status": "unknown", "reason": "log_invalid", "failure_hint": "unknown"}
    assert captured.err == ""
    assert "private payload" not in captured.out


def identity_payload(line, *, job_id="nightly-o11y", vmid="200", detail_status=200):
    return {
        "job_id": job_id,
        "vmid": vmid,
        "jobs": [{"id": job_id, "storage": "backup-store"}],
        "job_detail_response": {
            "status": detail_status,
            "json": {"data": {"id": job_id, "storage": "backup-store"}},
        },
        "metadata_response": {
            "status": 200,
            "json": {"total": 5, "data": [{"n": 1, "t": line}]},
        },
    }


def test_node_failure_identity_compares_exact_job_vmid_and_storage_without_echoing_values():
    line = (
        "INFO: starting new backup job: vzdump 200 --job-id nightly-o11y "
        "--storage backup-store --mode snapshot"
    )
    result = summarize_node_failure_identity(identity_payload(line))

    assert result == {
        "status": "compared",
        "selected_job_id_match": True,
        "declared_vmid_match": True,
        "selected_storage_match": True,
    }
    assert all(value not in json.dumps(result) for value in ("nightly-o11y", "200", "backup-store", line))


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (
            "INFO: starting new backup job: vzdump 200 --storage backup-store",
            {"status": "job_id_unavailable", "selected_job_id_match": None,
             "declared_vmid_match": True, "selected_storage_match": True},
        ),
        (
            "INFO: starting new backup job: vzdump 201 --job-id another --storage backup-store",
            {"status": "compared", "selected_job_id_match": False,
             "declared_vmid_match": False, "selected_storage_match": True},
        ),
        (
            "INFO: starting new backup job: vzdump 200 201 --job-id nightly-o11y --storage backup-store",
            {"status": "compared", "selected_job_id_match": True,
             "declared_vmid_match": True, "selected_storage_match": True},
        ),
        (
            "INFO: starting new backup job: vzdump 200 --job-id nightly-o11y --storage other-store",
            {"status": "compared", "selected_job_id_match": True,
             "declared_vmid_match": True, "selected_storage_match": False},
        ),
        (
            "INFO: starting new backup job: vzdump 200 --job-id=nightly-o11y --storage=backup-store",
            {"status": "compared", "selected_job_id_match": True,
             "declared_vmid_match": True, "selected_storage_match": True},
        ),
        (
            "INFO: starting new backup job: vzdump 200 --notes-template '--job-id=nightly-o11y' "
            "--storage backup-store",
            {"status": "job_id_unavailable", "selected_job_id_match": None,
             "declared_vmid_match": True, "selected_storage_match": True},
        ),
        (
            "INFO: starting new backup job: vzdump 200 --job-id nightly-o11y "
            "--notes-template '--storage=backup-store'",
            {"status": "compared", "selected_job_id_match": True,
             "declared_vmid_match": True, "selected_storage_match": None},
        ),
        (
            "INFO: starting new backup job: vzdump 200 --job-id --storage backup-store",
            {"status": "unknown", "selected_job_id_match": None,
             "declared_vmid_match": None, "selected_storage_match": None},
        ),
        (
            "INFO: starting new backup job: vzdump 200 --storage --job-id=nightly-o11y",
            {"status": "unknown", "selected_job_id_match": None,
             "declared_vmid_match": None, "selected_storage_match": None},
        ),
        (
            "INFO: starting new backup job: vzdump 200 --job-id",
            {"status": "unknown", "selected_job_id_match": None,
             "declared_vmid_match": None, "selected_storage_match": None},
        ),
        (
            "INFO: starting new backup job: vzdump 200 --storage",
            {"status": "unknown", "selected_job_id_match": None,
             "declared_vmid_match": None, "selected_storage_match": None},
        ),
        (
            "INFO: starting new backup job: vzdump 200 --unknown",
            {"status": "unknown", "selected_job_id_match": None,
             "declared_vmid_match": None, "selected_storage_match": None},
        ),
        (
            "INFO: starting new backup job: vzdump 200 --job-id nightly-o11y 201",
            {"status": "unknown", "selected_job_id_match": None,
             "declared_vmid_match": None, "selected_storage_match": None},
        ),
        (
            "INFO: starting new backup job: vzdump 200 --job-id nightly-o11y --job-id other --storage backup-store",
            {"status": "unknown", "selected_job_id_match": None,
             "declared_vmid_match": None, "selected_storage_match": None},
        ),
        (
            "INFO: starting new backup job: vzdump 200 200 --job-id nightly-o11y --storage backup-store",
            {"status": "unknown", "selected_job_id_match": None,
             "declared_vmid_match": None, "selected_storage_match": None},
        ),
        (
            "INFO: starting new backup job: vzdump 200 --storage backup-store --storage other-store",
            {"status": "unknown", "selected_job_id_match": None,
             "declared_vmid_match": None, "selected_storage_match": None},
        ),
        (
            "INFO: starting new backup job: vzdump 200 --job-id --storage backup-store",
            {"status": "unknown", "selected_job_id_match": None,
             "declared_vmid_match": None, "selected_storage_match": None},
        ),
        (
            "synthetic task log text",
            {"status": "unknown", "selected_job_id_match": None,
             "declared_vmid_match": None, "selected_storage_match": None},
        ),
        (
            "INFO: starting new vzdump worker: vzdump 200 --job-id nightly-o11y --storage backup-store",
            {"status": "unknown", "selected_job_id_match": None,
             "declared_vmid_match": None, "selected_storage_match": None},
        ),
        (
            "INFO: starting new backup job: vzdump 200 --job-id 'unterminated --storage backup-store",
            {"status": "unknown", "selected_job_id_match": None,
             "declared_vmid_match": None, "selected_storage_match": None},
        ),
    ],
)
def test_node_failure_identity_is_fail_closed_for_absent_malformed_duplicate_and_mismatched_fields(line, expected):
    assert summarize_node_failure_identity(identity_payload(line)) == expected


def test_node_failure_identity_rejects_malformed_details_synthetic_rows_and_oversized_lines():
    line = "INFO: starting new backup job: vzdump 200 --job-id nightly-o11y --storage backup-store"
    expected = {
        "status": "unknown", "selected_job_id_match": None,
        "declared_vmid_match": None, "selected_storage_match": None,
    }
    assert summarize_node_failure_identity(identity_payload(line, detail_status=True)) == expected
    assert summarize_node_failure_identity(identity_payload(line, job_id="bad job id")) == expected
    assert summarize_node_failure_identity(identity_payload(line, vmid="0200")) == expected
    oversized = "INFO: starting new backup job: vzdump 200 " + ("x" * 4096)
    assert summarize_node_failure_identity(identity_payload(oversized)) == expected
    payload = identity_payload(line)
    payload["metadata_response"]["json"]["data"][0]["t"] = "unable to open file: private path"
    assert summarize_node_failure_identity(payload) == expected


@pytest.mark.parametrize("jobs", [
    [{"id": "nightly-o11y", "storage": "backup-store"},
     {"id": "nightly-o11y", "storage": "other-store"}],
    [{"id": "nightly-o11y", "storage": "backup-store"},
     {"id": "other-job"}, {"id": "other-job"}],
    [{"id": "nightly-o11y", "storage": "backup-store"}, {"id": "bad id"}],
])
def test_node_failure_identity_refuses_ambiguous_or_malformed_list_identities(jobs):
    line = "INFO: starting new backup job: vzdump 200 --job-id nightly-o11y --storage backup-store"
    payload = identity_payload(line)
    payload["jobs"] = jobs
    assert summarize_node_failure_identity(payload) == {
        "status": "unknown", "selected_job_id_match": None,
        "declared_vmid_match": None, "selected_storage_match": None,
    }


def test_node_failure_identity_refuses_selected_detail_configuration_drift():
    line = "INFO: starting new backup job: vzdump 200 --job-id nightly-o11y --storage backup-store"
    payload = identity_payload(line)
    payload["jobs"][0]["enabled"] = 0
    assert summarize_node_failure_identity(payload) == {
        "status": "unknown", "selected_job_id_match": None,
        "declared_vmid_match": None, "selected_storage_match": None,
    }


def selected_storage_payload(*, job_id="nightly-o11y", detail_id="nightly-o11y", storage_id="backup-store",
                             rows=None, detail_status=200, storage_status=200):
    if rows is None:
        rows = [{"storage": storage_id, "active": 1, "content": "backup,iso", "total": 1000, "avail": 300}]
    return {
        "job_id": job_id,
        "jobs": [{"id": job_id, "storage": storage_id}],
        "job_detail_response": {
            "status": detail_status,
            "json": {"data": {"id": detail_id, "storage": storage_id}},
        },
        "storage_response": {"status": storage_status, "json": {"data": rows}},
    }


@pytest.mark.parametrize(
    ("available", "expected"),
    [(0, "zero"), (99, "below_10_percent"), (100, "10_to_30_percent"),
     (299, "10_to_30_percent"), (300, "at_least_30_percent")],
)
def test_selected_storage_reports_only_coarse_free_bands(available, expected):
    payload = selected_storage_payload(rows=[{
        "storage": "backup-store", "active": 1, "content": "backup,iso",
        "total": 1000, "avail": available,
    }])
    assert summarize_selected_storage(payload) == {
        "destination_state": "active_backup_capable", "free_band": expected,
    }


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (selected_storage_payload(job_id=""), {"destination_state": "unknown", "free_band": "unknown"}),
        (selected_storage_payload(detail_id="other-job"), {"destination_state": "unknown", "free_band": "unknown"}),
        (selected_storage_payload(detail_status=403), {"destination_state": "unknown", "free_band": "unknown"}),
        (selected_storage_payload(storage_status=True), {"destination_state": "unknown", "free_band": "unknown"}),
        (selected_storage_payload(rows={"storage": "backup-store"}),
         {"destination_state": "unknown", "free_band": "unknown"}),
        (selected_storage_payload(rows=[{"storage": "backup-store", "active": True,
                                        "content": "backup", "total": 1000, "avail": 500}]),
         {"destination_state": "unknown", "free_band": "unknown"}),
        (selected_storage_payload(rows=[{"storage": "backup-store", "active": 1,
                                        "content": "images", "total": 1000, "avail": 500}]),
         {"destination_state": "not_active_backup_capable", "free_band": "unknown"}),
        (selected_storage_payload(rows=[{"storage": "backup-store", "active": 0,
                                        "content": "backup", "total": 1000, "avail": 500}]),
         {"destination_state": "not_active_backup_capable", "free_band": "unknown"}),
        (selected_storage_payload(rows=[{"storage": "backup-store", "active": 1,
                                        "content": "backup", "total": True, "avail": 500}]),
         {"destination_state": "unknown", "free_band": "unknown"}),
        (selected_storage_payload(rows=[{"storage": "backup-store", "active": 1,
                                        "content": "backup", "total": 1000, "avail": False}]),
         {"destination_state": "unknown", "free_band": "unknown"}),
        (selected_storage_payload(rows=[{"storage": "backup-store", "active": 1,
                                        "content": "backup", "total": 1000, "avail": 1001}]),
         {"destination_state": "unknown", "free_band": "unknown"}),
        (selected_storage_payload(rows=[
            {"storage": "backup-store", "active": 1, "content": "backup", "total": 1000, "avail": 500},
            {"storage": "backup-store", "active": 1, "content": "backup", "total": 1000, "avail": 500},
        ]), {"destination_state": "unknown", "free_band": "unknown"}),
        (selected_storage_payload(rows=[{"storage": "backup-store", "active": 1,
                                        "content": "backup", "total": 1000}]),
         {"destination_state": "unknown", "free_band": "unknown"}),
    ],
)
def test_selected_storage_missing_malformed_duplicate_or_partial_data_has_exact_result(payload, expected):
    assert summarize_selected_storage(payload) == expected


def test_selected_storage_does_not_use_capacity_from_another_destination_or_echo_identifiers():
    payload = selected_storage_payload(storage_id="private-store", rows=[
        {"storage": "other-store", "active": 1, "content": "backup", "total": 1000, "avail": 900},
    ])
    result = summarize_selected_storage(payload)
    assert result == {"destination_state": "unknown", "free_band": "unknown"}
    assert "private-store" not in json.dumps(result) and "other-store" not in json.dumps(result)


@pytest.mark.parametrize("jobs", [
    [{"id": "nightly-o11y", "storage": "backup-store"},
     {"id": "nightly-o11y", "storage": "other-store"}],
    [{"id": "nightly-o11y", "storage": "backup-store"},
     {"id": "other-job"}, {"id": "other-job"}],
    [{"id": "nightly-o11y", "storage": "backup-store"}, {"id": "bad id"}],
])
def test_selected_storage_refuses_ambiguous_or_malformed_list_identities(jobs):
    payload = selected_storage_payload()
    payload["jobs"] = jobs
    assert summarize_selected_storage(payload) == {
        "destination_state": "unknown", "free_band": "unknown",
    }


def test_selected_storage_refuses_selected_detail_configuration_drift():
    payload = selected_storage_payload()
    payload["jobs"][0]["enabled"] = 0
    assert summarize_selected_storage(payload) == {
        "destination_state": "unknown", "free_band": "unknown",
    }


@pytest.mark.parametrize(("operation", "expected"), [
    ("node-failure-plan", {
        "read_allowed": False,
        "diagnostic": {"status": "unknown", "reason": "node_history_unavailable", "failure_hint": "unknown"},
    }),
    ("node-failure-pages", {
        "read_allowed": False,
        "diagnostic": {"status": "unknown", "reason": "metadata_invalid", "failure_hint": "unknown"},
    }),
    ("node-failure-summary", {
        "status": "unknown", "reason": "log_invalid", "failure_hint": "unknown",
    }),
    ("node-failure-identity", {
        "status": "unknown", "selected_job_id_match": None,
        "declared_vmid_match": None, "selected_storage_match": None,
    }),
    ("storage-summary", {"destination_state": "unknown", "free_band": "unknown"}),
])
def test_node_failure_cli_rejects_non_mapping_json_with_fixed_output(
    operation, expected, monkeypatch, capsys,
):
    monkeypatch.setattr(sys, "argv", ["reconcile_proxmox_backup_job.py", operation])
    monkeypatch.setattr(sys, "stdin", io.StringIO('["private input"]'))

    assert main() == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == expected
    assert captured.err == ""
    assert "private input" not in captured.out


def test_task_log_rejects_pagination_drift_numbering_and_unbounded_responses():
    page_plan = {"read_allowed": True, "total": 101, "start": 1, "limit": 100}
    for total, rows in (
        (102, [{"n": 1, "t": "secret"}] * 100),
        (101, [{"n": index + 3, "t": "secret"} for index in range(100)]),
        (101, [{"n": index + 1, "t": "secret"} for index in range(100)]),
        (101, [{"n": index + 2, "t": "x" * 4097} for index in range(100)]),
    ):
        result = summarize_node_failure_log({
            "page_plan": page_plan,
            "log_response": {"status": 200, "json": {"total": total, "data": rows}},
        })
        assert result == {"status": "unknown", "reason": "log_invalid", "failure_hint": "unknown"}
        assert "secret" not in json.dumps(result)


@pytest.mark.parametrize(
    "rows",
    [
        [node_task_row(1, type="other")],
        [node_task_row(1, starttime=2_001)],
        [node_task_row(1, status=None)],
        [node_task_row(1), node_task_row(1)],
        [node_task_row(1, id="private-id")],
    ],
)
def test_node_history_malformed_rows_become_sanitized_unknown(rows):
    result = summarize_node_task_history({
        "plan": node_history_plan(), "first_page": task_page(rows, len(rows)), "later_pages": [],
    })

    assert result["status"] == "unknown"
    assert result["total_recent_node_vzdump_tasks"] is None
    assert "private" not in json.dumps(result)
    assert "UPID" not in json.dumps(result)


def test_node_history_unbounded_incomplete_and_failed_responses_are_unknown():
    plan = node_history_plan()
    over_bound = task_page([node_task_row(i) for i in range(HISTORY_PAGE_SIZE)], HISTORY_MAX_ROWS + 1)
    incomplete = summarize_node_task_history({
        "plan": plan, "first_page": task_page([node_task_row(i) for i in range(HISTORY_PAGE_SIZE)], 51),
        "later_pages": [],
    })
    bounded = summarize_node_task_history({
        "plan": plan, "first_page": over_bound, "later_pages": [],
    })
    failed = summarize_node_task_history({
        "plan": plan, "first_page": {"status": 403, "json": {}}, "later_pages": [],
    })

    assert incomplete["reason"] == "pagination_incomplete"
    assert bounded["reason"] == "pagination_limit_exceeded"
    assert failed["reason"] == "request_failed"


def test_node_history_summarizes_complete_multi_page_response():
    plan = node_history_plan()
    rows = [node_task_row(index) for index in range(HISTORY_PAGE_SIZE + 1)]
    result = summarize_node_task_history({
        "plan": plan,
        "first_page": task_page(rows[:HISTORY_PAGE_SIZE], len(rows)),
        "later_pages": [task_page(rows[HISTORY_PAGE_SIZE:], len(rows))],
    })

    assert result["status"] == "complete"
    assert result["total_recent_node_vzdump_tasks"] == HISTORY_PAGE_SIZE + 1
    assert result["target_vmid_task_id_count"] == 0
    assert result["potential_multi_vm_or_unattributed_task_count"] == 0
    assert result["job_attribution"] == "unavailable"
    assert "private-UPID" not in json.dumps(result)


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
        "Require safe backup-job inspection data"
    ) < names.index("Keep the sanitized backup-job candidate report private to this run") < names.index(
        "Read the privately selected backup-job detail for inspection"
    ) < names.index("Read storage available to the declared node for destination inspection") < names.index(
        "Report backup-job candidates for inventory selection"
    ) < names.index("End after read-only backup-job inspection") < names.index(
        "Require one exact private selected backup job"
    ) < names.index("Read the selected backup-job detail")
    assert by_name["End after read-only backup-job inspection"][1]["when"] == "_inspect_only"
    selected_detail = by_name["Read the privately selected backup-job detail for inspection"][1]
    assert selected_detail["ansible.builtin.uri"]["method"] == "GET"
    assert "/api2/json/cluster/backup/{{ _backup_job_id }}" in selected_detail["ansible.builtin.uri"]["url"]
    assert selected_detail["when"] == [
        "_inspect_only",
        "_backup_job_id is match('^[A-Za-z0-9._-]{1,64}$')",
        "(_backup_inspection.candidates | selectattr('id', 'equalto', _backup_job_id) | list | length) == 1",
    ]
    assert selected_detail["no_log"] is True and selected_detail["check_mode"] is False
    storage_get = by_name["Read storage available to the declared node for destination inspection"][1]
    assert storage_get["ansible.builtin.uri"]["method"] == "GET"
    assert "/api2/json/nodes/{{ _node }}/storage" in storage_get["ansible.builtin.uri"]["url"]
    assert storage_get["when"] == selected_detail["when"]
    assert storage_get["no_log"] is True and storage_get["check_mode"] is False
    storage_summary = by_name["Summarize the selected job destination without exposing its identity"][1]
    assert storage_summary["ansible.builtin.command"]["argv"][2] == "storage-summary"
    assert storage_summary["no_log"] is True
    assert "'jobs': _backup_jobs.json.data" in storage_summary["ansible.builtin.command"]["stdin"]
    assert by_name["Keep the sanitized selected-destination state"][1]["no_log"] is True
    history_get = by_name["Read the first archived vzdump task page"][1]
    assert history_get["ansible.builtin.uri"]["method"] == "GET"
    assert history_get["when"] == "_inspect_only and _backup_history_query.read_allowed | bool"
    assert history_get["no_log"] is True and history_get["check_mode"] is False
    scheduler_get = by_name["Read pvescheduler state after verified node-scoped permission"][1]
    assert scheduler_get["ansible.builtin.uri"]["method"] == "GET"
    assert "/api2/json/nodes/{{ _node }}/services/pvescheduler/state" in scheduler_get[
        "ansible.builtin.uri"
    ]["url"]
    assert scheduler_get["when"] == [
        "_inspect_only", "_backup_history_query.read_allowed | default(false) | bool",
    ]
    assert scheduler_get["no_log"] is True
    assert scheduler_get["check_mode"] is False
    scheduler_parse = by_name["Summarize pvescheduler state without exposing the API response"][1]
    assert scheduler_parse["ansible.builtin.command"]["argv"][2] == "scheduler-state"
    assert scheduler_parse["no_log"] is True
    assert scheduler_parse["when"] == "_inspect_only"
    assert by_name["Keep the sanitized scheduler state private to this run"][1]["no_log"] is True
    node_query = by_name["Plan bounded node-wide archived vzdump history inspection"][1]
    assert node_query["ansible.builtin.command"]["argv"][2] == "node-history-query"
    assert node_query["no_log"] is True
    assert node_query["when"] == "_inspect_only"
    node_get = by_name["Read the first node-wide archived vzdump task page"][1]
    node_url = node_get["ansible.builtin.uri"]["url"]
    assert node_get["ansible.builtin.uri"]["method"] == "GET"
    assert "source=archive" in node_url and "typefilter=vzdump" in node_url
    assert "since=" in node_url and "until=" in node_url
    assert "start=0&limit=50" in node_url and "vmid" not in node_url
    assert node_get["when"] == "_inspect_only and _backup_node_history_query.read_allowed | bool"
    assert node_get["no_log"] is True and node_get["check_mode"] is False
    node_pages = by_name["Plan remaining bounded node-history pages"][1]
    assert node_pages["ansible.builtin.command"]["argv"][2] == "history-pages"
    node_later = by_name["Read later node-wide archived vzdump task pages"][1]
    assert node_later["loop"] == "{{ _backup_node_history_pages.page_starts }}"
    assert "start=' ~ item ~ '&limit=50'" in node_later["ansible.builtin.uri"]["url"]
    assert "vmid" not in node_later["ansible.builtin.uri"]["url"]
    assert node_later["no_log"] is True and node_later["check_mode"] is False
    node_summary = by_name[
        "Summarize node-wide archived task history without task identity or details"
    ][1]
    assert node_summary["ansible.builtin.command"]["argv"][2] == "node-history-summary"
    assert node_summary["no_log"] is True
    assert "no_log" not in by_name[
        "Keep the sanitized node-history summary private to this run"
    ][1]
    failure_select = by_name["Select one recent failed node task for a bounded diagnostic"][1]
    assert failure_select["ansible.builtin.command"]["argv"][2] == "node-failure-plan"
    assert failure_select["no_log"] is True and failure_select["when"] == "_inspect_only"
    metadata_get = by_name["Read one task-log metadata line for a recent unique failure"][1]
    assert metadata_get["ansible.builtin.uri"]["method"] == "GET"
    assert "metadata_path" in metadata_get["ansible.builtin.uri"]["url"]
    assert metadata_get["no_log"] is True and metadata_get["check_mode"] is False
    assert "read_allowed" in metadata_get["when"]
    identity = by_name["Compare task metadata with private selected backup identity"][1]
    assert identity["ansible.builtin.command"]["argv"][2] == "node-failure-identity"
    assert identity["no_log"] is True and identity["when"] == "_inspect_only"
    assert "'jobs': _backup_jobs.json.data" in identity["ansible.builtin.command"]["stdin"]
    assert by_name["Keep sanitized task identity comparisons"][1]["no_log"] is True
    log_plan = by_name["Plan the bounded final task-log page"][1]
    assert log_plan["ansible.builtin.command"]["argv"][2] == "node-failure-pages"
    assert log_plan["no_log"] is True
    log_get = by_name["Read the bounded final task-log lines"][1]
    assert log_get["ansible.builtin.uri"]["method"] == "GET"
    assert log_get["no_log"] is True and log_get["check_mode"] is False
    assert "read_allowed" in log_get["when"]
    log_summary = by_name["Classify task log into a fixed failure hint"][1]
    assert log_summary["ansible.builtin.command"]["argv"][2] == "node-failure-summary"
    assert log_summary["no_log"] is True
    assert "no_log" not in by_name["Keep sanitized node-task failure diagnostic"][1]
    assert names.index("Read effective permissions on the declared Proxmox node") < names.index(
        "Plan bounded node-wide archived vzdump history inspection"
    ) < names.index("Read the first node-wide archived vzdump task page") < names.index(
        "Report backup-job candidates for inventory selection"
    )
    assert "proxmox_node_task_history" in by_name[
        "Report backup-job candidates for inventory selection"
    ][1]["ansible.builtin.debug"]["msg"]
    assert "proxmox_node_task_failure_diagnostic" in by_name[
        "Report backup-job candidates for inventory selection"
    ][1]["ansible.builtin.debug"]["msg"]
    report = by_name["Report backup-job candidates for inventory selection"][1]["ansible.builtin.debug"]["msg"]
    assert "proxmox_node_task_identity" in report and "selected_backup_destination" in report
    assert "verified_o11y_vmid" not in by_name[
        "Report backup-job candidates for inventory selection"
    ][1]["ansible.builtin.debug"]["msg"]
    assert names.index("Read effective permissions on the declared Proxmox node") < names.index(
        "Read pvescheduler state after verified node-scoped permission"
    ) < names.index("Report backup-job candidates for inventory selection")
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
            "scheduler-state",
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
