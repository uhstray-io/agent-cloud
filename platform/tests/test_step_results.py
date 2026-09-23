"""The collector's parsing and aggregation (platform/service-onboarding-workflow)."""

import importlib.util
import json
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


def _run_line(result: dict) -> str:
    return "\tRUN: " + json.dumps({"step_result": result})


def _task(tid, status, template_id, output="", service="tududi", **extra):
    return {"id": tid, "status": status, "end": f"t{tid}", "template_id": template_id,
            "service": service, "output": output, **extra}


def _agg(*tasks):
    return step_results.aggregate(REGISTRY, TEMPLATES, list(tasks))


def test_only_workflow_templates_are_selected():
    # A deploy for a service this inventory does not hold (postiz) has no history worth reading.
    assert step_results.select(REGISTRY, TEMPLATES, GROUPS) == [1, 2, 3, 4, 6]


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


def test_service_comes_from_inventory_not_string_surgery():
    # step_ca_svc's hosts are service_name step-ca: the name its own step results carry.
    deploy = {"id": 1, "status": "error", "template_id": 6, "tpl_playbook": "platform/playbooks/deploy-step-ca.yml"}
    snap = {"id": 2, "status": "error", "template_id": 4, "environment": '{"target_service": "step_ca_svc"}'}
    phantom = {"id": 3, "status": "error", "template_id": 6, "tpl_playbook": "platform/playbooks/deploy-all.yml"}
    got = {t["id"]: t["service"] for t in step_results.pick([[deploy, snap, phantom]], GROUPS)}
    assert got == {1: "step-ca", 2: "step-ca", 3: None}


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
            {"check_mode": None, "error": None, "task_id": 8}, sort_keys=True)]],
    }]


def test_groups_map_to_their_hosts_service_name():
    groups = {"all": ["a", "b"], "tududi_svc": ["a"], "step_ca_svc": ["b"], "ungrouped": [], "misc": ["c"]}
    assert step_results.group_services(groups, {"a": "tududi", "b": "step-ca"}) == {
        "all": "tududi", "tududi_svc": "tududi", "step_ca_svc": "step-ca"}


def test_main_runs_the_three_modes_as_the_collector_calls_them():
    import subprocess
    import sys
    script = REPO / "platform/workflows/service-onboarding/lib/step_results.py"

    def call(payload):
        done = subprocess.run([sys.executable, str(script)], input=json.dumps(payload),
                              text=True, capture_output=True, check=True)
        return json.loads(done.stdout)

    inv = {"groups": {"tududi_svc": ["h"], "step_ca_svc": ["s"]}, "host_services": {"h": "tududi", "s": "step-ca"}}
    selected = call({"mode": "select", "registry": REGISTRY, "templates": TEMPLATES, **inv})
    assert selected["template_ids"] == [1, 2, 3, 4, 6]
    row = {"id": 5, "status": "success", "template_id": 1, "end": "t",
           "environment": '{"target_service": "tududi_svc"}'}
    picked = call({"mode": "pick", "groups": {"tududi_svc": ["h"]}, "host_services": {"h": "tududi"},
                   "histories": [[row]]})["tasks"]
    out = _run_line({"service": "tududi", "step": "secrets-approle", "status": "pass"})
    agg = call({"mode": "aggregate", "registry": REGISTRY, "templates": TEMPLATES, "now_ns": 1,
                "fetched": [{"item": picked[0], "content": out, "status": 200}]})
    assert agg["status_by_service"] == {"tududi": {"secrets-approle": "pass"}}
    assert len(agg["loki_streams"]) == 1


def test_target_service_may_name_the_service_or_its_group():
    # Snapshots take the group (tududi_svc); provision-vm and the address steps the service.
    rows = [{"id": 1, "status": "error", "template_id": 4, "environment": '{"target_service": "tududi"}'},
            {"id": 2, "status": "error", "template_id": 1, "environment": '{"target_service": "step_ca_svc"}'}]
    assert {t["id"]: t["service"] for t in step_results.pick([rows], GROUPS)} == {1: "tududi", 2: "step-ca"}
