"""The collector's parsing and aggregation (platform/service-onboarding-workflow)."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "step_results", REPO / "platform/workflows/service-onboarding/lib/step_results.py"
)
step_results = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(step_results)
REGISTRY = yaml.safe_load((REPO / "platform/workflows/service-onboarding/registry.yml").read_text())["steps"]

TEMPLATES = [
    {"id": 1, "name": "Check Secrets"},
    {"id": 2, "name": "Apply Firewall (Dev)"},
    {"id": 3, "name": "Deploy tududi (Local)", "playbook": "platform/playbooks/deploy-tududi.yml"},
    {"id": 4, "name": "Snapshot Access"},
    {"id": 5, "name": "Some Unregistered Template"},
    {"id": 6, "name": "Deploy step-ca", "playbook": "platform/playbooks/deploy-step-ca.yml"},
    {"id": 7, "name": "Deploy postiz", "playbook": "platform/playbooks/deploy-postiz.yml"},
]
GROUPS = {"tududi_svc": "tududi", "step_ca_svc": "step-ca"}  # postiz is not in this inventory
# The closed per-service deploy list the collector reads from OPA's data (not a "Deploy " prefix).
DEPLOYS = frozenset(json.loads((REPO / "platform/services/opa/deployment/policies/agentcloud/data.json").read_text())
                    ["catalog"]["service-agent"]["service_deploy_templates"])


def _run_line(result: dict) -> str:
    return "\tRUN: " + json.dumps({"step_result": result})


def _task(tid, status, template_id, output="", service="tududi", **extra):
    return {"id": tid, "status": status, "end": f"t{tid}", "template_id": template_id,
            "service": service, "output": output, **extra}


def _call(payload: dict) -> dict:
    """Run the parser as the collector does: one JSON object on stdin, JSON on stdout."""
    done = subprocess.run([sys.executable, str(REPO / "platform/workflows/service-onboarding/lib/step_results.py")],
                          input=json.dumps(payload), text=True, capture_output=True, check=True)
    return json.loads(done.stdout)


def _agg(*tasks):
    return step_results.aggregate(REGISTRY, TEMPLATES, list(tasks), deploy_templates=DEPLOYS)


def test_only_workflow_templates_are_selected():
    # A deploy for a service this inventory does not hold (postiz) has no history worth reading,
    # and Deploy step-ca is platform infrastructure, not on the per-service deploy list.
    assert step_results.select(REGISTRY, TEMPLATES, GROUPS, DEPLOYS) == [1, 2, 3, 4]


def test_a_foundation_deploy_is_never_the_service_deploy_step():
    # PR 195 Codex review: local inventory holds netbox_svc, and the "Deploy " prefix mapped
    # Deploy NetBox (Local) to service-deploy, publishing a foundation failure as a service's.
    templates = [{"id": 8, "name": "Deploy NetBox (Local)", "playbook": "platform/playbooks/deploy-netbox.yml"}]
    groups = {**GROUPS, "netbox_svc": "netbox"}
    assert step_results.select(REGISTRY, templates, groups, DEPLOYS) == []
    agg = step_results.aggregate(REGISTRY, templates, [_task(9, "error", 8, "x", service="netbox")],
                                 deploy_templates=DEPLOYS)
    assert agg["services"] == {}


def test_a_recorded_result_is_read_through_ansi_colour():
    out = "CUSTOM STATS: ****\n\x1b[0;32m" + _run_line(
        {"service": "tududi", "step": "secrets-approle", "status": "pass"}) + "\x1b[0m"
    agg = _agg(_task(1, "success", 1, out))
    assert agg["status_by_service"] == {"tududi": {"secrets-approle": "pass"}}
    assert agg["failed_steps"] == {"tududi": []}


def test_a_failure_without_a_result_is_recorded_with_its_tail():
    # Spec scenario "A failure with no result is still recorded".
    out = "\n".join(f"line {i}" for i in range(30))
    rec = _agg(_task(2, "error", 2, out))["services"]["tududi"]["fw-harden"]
    assert rec["status"] == "fail"
    assert rec["error"].splitlines() == [f"line {i}" for i in range(10, 30)]


def test_unmapped_failures_are_ignored():
    assert _agg(_task(6, "error", 5, "boom"))["services"] == {}


def test_per_service_deploy_template_maps_to_service_deploy():
    assert "service-deploy" in _agg(_task(7, "error", 3, "x"))["services"]["tududi"]


def test_pick_keeps_the_newest_finished_task_per_template_and_service():
    rows = [
        {"id": 10, "status": "success", "template_id": 1, "environment": '{"target_service": "tududi_svc"}'},
        {"id": 12, "status": "error", "template_id": 1, "environment": '{"target_service": "tududi_svc"}'},
        {"id": 13, "status": "running", "template_id": 1, "environment": '{"target_service": "tududi_svc"}'},
        {"id": 11, "status": "success", "template_id": 1, "environment": '{"target_service": "step_ca_svc"}'},
    ]
    picked = step_results.pick([rows], GROUPS)
    assert [(t["id"], t["service"]) for t in picked] == [(11, "step-ca"), (12, "tududi")]


def test_aggregate_lets_the_newest_real_result_win():
    older_pass = _task(10, "success", 1, _run_line({"service": "tududi", "step": "secrets-approle", "status": "pass"}))
    newer_fail = _task(12, "error", 1, _run_line({"service": "tududi", "step": "secrets-approle", "status": "fail"}))
    assert _agg(newer_fail, older_pass)["status_by_service"]["tududi"]["secrets-approle"] == "fail"


def test_service_comes_from_inventory_not_string_surgery():
    # step_ca_svc's hosts are service_name step-ca: the name its own step results carry.
    deploy = {"id": 1, "status": "error", "template_id": 6, "tpl_playbook": "platform/playbooks/deploy-step-ca.yml"}
    snap = {"id": 2, "status": "error", "template_id": 4, "environment": '{"target_service": "step_ca_svc"}'}
    phantom = {"id": 3, "status": "error", "template_id": 6, "tpl_playbook": "platform/playbooks/deploy-all.yml"}
    got = {t["id"]: t["service"] for t in step_results.pick([[deploy, snap, phantom]], GROUPS)}
    assert got == {1: "step-ca", 2: "step-ca", 3: None}


def test_a_passing_snapshot_is_an_input_never_the_assessment_passing():
    # PR 195 Codex review: Snapshot Access recorded access-assess `pass` and the collector
    # published it, though no proposal had been validated against the step's criteria.
    out = _run_line({"service": "tududi", "step": "access-assess", "status": "pass"})
    agg = _agg(_task(21, "success", 4, out))
    assert agg["services"] == {} and agg["failed_steps"] == {}
    assert agg["inputs"]["tududi"]["access-assess"]["status"] == "pass"
    # by the template's role, not the status: a snapshot's skip is not the assessment either
    skip = _run_line({"service": "tududi", "step": "access-assess", "status": "skip"})
    skipped = _agg(_task(23, "success", 4, skip))
    assert skipped["services"] == {} and skipped["inputs"]["tududi"]["access-assess"]["status"] == "skip"
    # the same result from a template that is NOT the step's snapshot still counts
    assert _agg(_task(22, "success", 1, _run_line({"service": "tududi", "step": "secrets-approle",
                                                     "status": "pass"})))["services"]


def test_a_failed_snapshot_without_a_result_is_charged_to_its_service():
    row = {"id": 20, "status": "error", "template_id": 4, "end": "t", "environment": '{"target_service": "tududi_svc"}'}
    (picked,) = step_results.pick([[row]], GROUPS)
    agg = _agg(dict(picked, output="no hosts matched"))
    assert agg["failed_steps"] == {"tududi": ["access-assess"]}


def test_report_lists_failed_steps_with_their_context_and_unreviewed_steps():
    # Spec: the report lists criteria not met, error context, undo and the task, and every
    # unreviewed step for every service.
    entry = _agg(_task(9, "error", 2, "denied"))["report"]["tududi"]
    fw = next(s for s in REGISTRY if s["id"] == "fw-harden")
    assert entry["failed"] == [{"step": "fw-harden", "criteria": fw["criteria"], "error": "denied",
                                "task_id": 9, "undo": fw["undo"]}]
    assert entry["unreviewed"] == [s["id"] for s in REGISTRY if not s.get("reviewed")]


def test_loki_streams_label_every_result():
    out = _run_line({"service": "tududi", "step": "secrets-approle", "status": "pass"})
    streams = step_results.loki_streams(_agg(_task(8, "success", 1, out)), 1700000000000000000)
    assert streams == [{
        "stream": {"job": "agent-cloud-conformance", "service": "tududi",
                   "step": "secrets-approle", "status": "pass"},
        "values": [["1700000000000000000", json.dumps(
            {"check_mode": False, "error": None, "task_id": 8}, sort_keys=True)]],
    }]


def test_groups_map_to_their_hosts_service_name():
    groups = {"all": ["a", "b"], "tududi_svc": ["a"], "step_ca_svc": ["b"], "ungrouped": [], "misc": ["c"]}
    assert step_results.group_services(groups, {"a": "tududi", "b": "step-ca"}) == {
        "all": "tududi", "tududi_svc": "tududi", "step_ca_svc": "step-ca"}


def test_main_runs_the_three_modes_as_the_collector_calls_them():
    call = _call
    inv = {"groups": {"tududi_svc": ["h"], "step_ca_svc": ["s"]}, "host_services": {"h": "tududi", "s": "step-ca"}}
    deploys = sorted(DEPLOYS)
    selected = call({"mode": "select", "registry": REGISTRY, "templates": TEMPLATES,
                     "deploy_templates": deploys, **inv})
    assert selected["template_ids"] == [1, 2, 3, 4]
    # the service list the NetBox lookup loops, keyed as the aggregate keys them
    assert selected["inventory_services"] == ["step-ca", "tududi"]
    row = {"id": 5, "status": "success", "template_id": 1, "end": "t",
           "environment": '{"target_service": "tududi_svc"}'}
    picked = call({"mode": "pick", "groups": {"tududi_svc": ["h"]}, "host_services": {"h": "tududi"},
                   "histories": [[row]]})["tasks"]
    out = _run_line({"service": "tududi", "step": "secrets-approle", "status": "pass"})
    agg = call({"mode": "aggregate", "registry": REGISTRY, "templates": TEMPLATES, "now_ns": 1,
                "deploy_templates": deploys,
                "fetched": [{"item": picked[0], "content": out, "status": 200}]})
    assert agg["status_by_service"] == {"tududi": {"secrets-approle": "pass"}}
    assert len(agg["loki_streams"]) == 1


def test_pick_reports_a_template_whose_history_filled_the_window():
    # PR 195 Codex review: runs older than the read window are invisible, so say which
    # templates hit it instead of letting their services look history-less.
    full = [{"id": i, "status": "success", "template_id": 9} for i in range(step_results.HISTORY_WINDOW, 0, -1)]
    short = [{"id": 5000, "status": "success", "template_id": 8}]
    got = _call({"mode": "pick", "groups": {}, "host_services": {}, "histories": [full, short, []]})
    assert got["window_full"] == [9]
    # HISTORY_WINDOW is the size of THIS endpoint's answer; /tasks/last answers 200
    plays = yaml.safe_load((REPO / "platform/playbooks/collect-service-conformance.yml").read_text())
    read = next(t for t in plays[0]["tasks"] if t.get("name") == "Read each workflow template's task history")
    assert read["ansible.builtin.uri"]["url"].endswith("/templates/{{ item }}/tasks")


def test_target_service_may_name_the_service_or_its_group():
    # Snapshots take the group (tududi_svc); provision-vm and the address steps the service.
    rows = [{"id": 1, "status": "error", "template_id": 4, "environment": '{"target_service": "tududi"}'},
            {"id": 2, "status": "error", "template_id": 1, "environment": '{"target_service": "step_ca_svc"}'}]
    assert {t["id"]: t["service"] for t in step_results.pick([rows], GROUPS)} == {1: "tududi", 2: "step-ca"}


# ── check mode never sets conformance (review of PR #195) ─────────────────────

DRY = {"params": {"dry_run": True, "diff": True}}


def test_a_passing_check_after_a_failed_real_run_does_not_clear_the_failure():
    failed_real = _task(20, "error", 2, "\n".join(f"line {i}" for i in range(5)))
    passing_check = _task(21, "success", 2, _run_line(
        {"service": "tududi", "step": "fw-harden", "status": "pass", "check_mode": True}), **DRY)
    agg = _agg(failed_real, passing_check)
    assert agg["status_by_service"]["tududi"]["fw-harden"] == "fail"
    assert agg["failed_steps"]["tududi"] == ["fw-harden"]
    assert agg["validation"]["tududi"]["fw-harden"]["status"] == "pass"
    assert agg["validation"]["tududi"]["fw-harden"]["task_id"] == 21


def test_a_result_contradicting_its_row_is_an_anomaly_kept_out_of_conformance():
    out = _run_line({"service": "tududi", "step": "secrets-approle", "status": "pass", "check_mode": True})
    agg = _agg(_task(22, "success", 1, out))
    assert agg["status_by_service"] == {}
    assert agg["validation"]["tududi"]["secrets-approle"]["check_mode"] is True
    assert agg["anomalies"] == [{"task_id": 22, "service": "tududi", "step": "secrets-approle",
                                 "row_check_mode": False, "result_check_mode": True}]


def test_the_last_real_run_survives_any_number_of_dry_runs():
    # Review of PR #229: one real run, then five (or more) check-mode runs.
    env = '{"target_service": "tududi_svc"}'
    real = {"id": 30, "status": "error", "template_id": 2, "environment": env}
    dry = [dict(real, id=31 + i, status="success", **DRY) for i in range(6)]
    assert [t["id"] for t in step_results.pick([[real, *dry]], GROUPS)] == [30, 36]


def test_a_real_row_failing_without_a_result_is_a_real_failure():
    # The row is the run-mode signal: a real row's failure with no result is a real fail.
    agg = _agg(_task(40, "error", 2, "boom"))
    assert agg["status_by_service"]["tududi"]["fw-harden"] == "fail"
    assert agg["anomalies"] == []


def test_no_template_can_inject_check_mode_outside_the_task_row():
    # The premise that makes the row authoritative: no template passes arguments (e.g.
    # --check) and none allows a per-task argument override.
    for name in ("templates.yml", "templates-local.yml"):
        text = (REPO / "platform/semaphore" / name).read_text()
        assert "--check" not in text, name
        assert "allow_override_args_in_task" not in text, name
        for t in yaml.safe_load(text).get("templates") or []:
            assert "arguments" not in t, (name, t.get("name"))
    setup = (REPO / "platform/semaphore/setup-templates.yml").read_text()
    assert "allow_override_args_in_task" not in setup and "'arguments'" not in setup


def test_an_inventoried_service_with_no_history_still_gets_a_report_row():
    agg = step_results.aggregate(REGISTRY, TEMPLATES, [], ["step-ca", "tududi"])
    assert set(agg["report"]) == {"step-ca", "tududi"}
    assert agg["report"]["step-ca"]["no_history"] is True
    assert agg["report"]["step-ca"]["failed"] == []
    assert agg["report"]["step-ca"]["unreviewed"] == [s["id"] for s in REGISTRY if not s.get("reviewed")]


def test_a_no_history_service_reaches_netbox_and_the_dashboard():
    # Review of PR #229: the collector writes NetBox and Loki from the aggregate, so a
    # tracked service with no run must appear there too, not only in the report.
    agg = step_results.aggregate(REGISTRY, TEMPLATES, [], ["step-ca"])
    assert agg["tracked"] == ["step-ca"]
    assert agg["status_by_service"] == {"step-ca": {}}
    assert agg["failed_steps"] == {"step-ca": []}
    streams = step_results.loki_streams(agg, 1)
    assert streams == [{"stream": {"job": "agent-cloud-conformance", "service": "step-ca", "step": "none",
                                   "status": "no_history"}, "values": [["1", '{"no_history": true}']]}]


def test_a_retained_snapshot_failure_clears_when_the_snapshot_succeeds():
    # PR 258 Codex review: a failed Snapshot Access was recorded as access-assess fail; its
    # successful retry lands in inputs, so the old failure came back from NetBox for good.
    ok = _run_line({"service": "tududi", "step": "access-assess", "status": "pass"})
    agg = step_results.aggregate(REGISTRY, TEMPLATES, [_task(30, "success", 4, ok)], ["tududi"], DEPLOYS,
                                 retained={"tududi": {"access-assess": "fail", "fw-harden": "fail"}})
    assert agg["status_by_service"] == {"tududi": {"fw-harden": "fail"}}
    assert agg["inputs"]["tududi"]["access-assess"]["status"] == "pass"
    # a CHECK-MODE success clears nothing: no real run superseded the failure (PR 258 Codex review)
    dry = _run_line({"service": "tududi", "step": "access-assess", "status": "pass", "check_mode": True})
    agg = step_results.aggregate(REGISTRY, TEMPLATES, [_task(31, "success", 4, dry, **DRY)], ["tududi"], DEPLOYS,
                                 retained={"tududi": {"access-assess": "fail"}})
    assert agg["status_by_service"] == {"tududi": {"access-assess": "fail"}}
    assert agg["validation"]["tududi"]["access-assess"]["status"] == "pass" and agg["inputs"] == {}


def test_a_full_window_marks_only_the_services_it_can_hide():
    # PR 258 Codex review: one full window marked every service incomplete and hid a new,
    # unrelated service from "Services not yet run". A per-service deploy template can hide
    # only its own service's runs; a shared template (Snapshot Access) any service's.
    groups = {"tududi_svc": "tududi", "step_ca_svc": "step-ca"}
    assert step_results.incomplete_services([3], TEMPLATES, REGISTRY, groups, DEPLOYS) == {"tududi"}
    assert step_results.incomplete_services([4], TEMPLATES, REGISTRY, groups, DEPLOYS) == {"tududi", "step-ca"}
    agg = step_results.aggregate(REGISTRY, TEMPLATES, [], ["step-ca", "tududi"], DEPLOYS,
                                 window_full=[3], incomplete={"tududi"})
    assert agg["history_window_full"] == [3] and agg["history_incomplete"] == ["tududi"]
    assert agg["report"]["tududi"]["no_history"] is False and agg["report"]["tududi"]["history_incomplete"] is True
    row = agg["report"]["step-ca"]
    assert row["no_history"] is True and row["history_incomplete"] is False
    markers = {(st["stream"]["service"], st["stream"]["status"]) for st in step_results.loki_streams(agg, 1)
               if st["stream"]["step"] == "none"}
    assert markers == {("tududi", "history_incomplete"), ("step-ca", "no_history")}


def test_a_retained_step_the_registry_no_longer_holds_is_dropped():
    # Grounding review of PR 258: a renamed or removed step's last status came back from NetBox
    # on every run, a stale failure no run could ever clear.
    agg = step_results.aggregate(REGISTRY, TEMPLATES, [], ["tududi"], DEPLOYS,
                                 retained={"tududi": {"fw-harden": "fail", "old-renamed-step": "fail"}})
    assert agg["status_by_service"] == {"tududi": {"fw-harden": "fail"}}
    assert agg["failed_steps"] == {"tududi": ["fw-harden"]}


def test_a_status_older_than_the_window_is_reported_and_streamed_not_only_kept():
    # PR 258 Codex review: NetBox kept a failure whose run fell out of the window, but the
    # report and Loki dropped it. The retained status is merged UNDER this run's results.
    out = _run_line({"service": "tududi", "step": "vm-provision", "status": "fail", "error": "new"})
    retained = {"tududi": {"fw-harden": "fail", "vm-provision": "pass", "fw-assess": "bogus"}}
    agg = step_results.aggregate(REGISTRY, TEMPLATES, [_task(8, "error", 1, out)], ["tududi"], DEPLOYS,
                                 retained=retained)
    # this run wins where it has a result; the older failure survives; an unknown value is dropped
    assert agg["status_by_service"] == {"tududi": {"vm-provision": "fail", "fw-harden": "fail"}}
    assert agg["failed_steps"] == {"tududi": ["fw-harden", "vm-provision"]}
    failed = {f["step"]: f for f in agg["report"]["tududi"]["failed"]}
    assert failed["fw-harden"]["error"] == step_results.RETAINED_ERROR and failed["fw-harden"]["task_id"] is None
    kept = [st for st in step_results.loki_streams(agg, 1) if st["stream"]["step"] == "fw-harden"]
    assert kept[0]["stream"]["status"] == "fail" and json.loads(kept[0]["values"][0][1])["retained"] is True
    assert agg["report"]["tududi"]["no_history"] is False


def test_the_collector_passes_the_window_to_the_aggregate():
    text = (REPO / "platform/playbooks/collect-service-conformance.yml").read_text()
    assert "'window_full': (_pick.stdout | from_json).window_full" in text
    assert "'retained': _retained" in text
    # the aggregate runs after the NetBox read that supplies what it retains
    assert text.index('name: "NetBox: sort the lookups"') < text.index('name: "Aggregate: latest result')


def test_the_collector_writes_every_tracked_service():
    text = (REPO / "platform/playbooks/collect-service-conformance.yml").read_text()
    assert 'loop: "{{ (_select.stdout | from_json).inventory_services }}"' in text
    assert "_agg.loki_streams | length > 0" in text
    assert "_agg.services.keys()" not in text


def test_a_dry_run_failing_without_a_result_is_validation_not_an_anomaly():
    # Review of PR #229: the synthesized failure takes its run mode from the row.
    agg = _agg(_task(41, "error", 2, "boom", **DRY))
    assert agg["status_by_service"] == {}
    assert agg["validation"]["tududi"]["fw-harden"]["status"] == "fail"
    assert agg["anomalies"] == []


def test_the_step_table_excludes_the_no_history_marker_and_a_panel_lists_it():
    dash = json.loads((REPO / "platform/services/o11y/deployment/config/grafana/dashboards/"
                               "service-conformance.json").read_text())
    exprs = {p["title"]: p["targets"][0]["expr"] for p in dash["panels"]}
    assert 'step!="none"' in exprs["Step status by service"]
    assert 'status="no_history"' in exprs["Services not yet run"]
    assert 'status="history_incomplete"' in exprs["History incomplete"]
