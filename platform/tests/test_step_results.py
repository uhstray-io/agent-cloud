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


def _run_line(result: dict) -> str:
    return "\tRUN: " + json.dumps({"step_result": result})


def _task(tid, status, template, output, service="tududi"):
    return {"id": tid, "status": status, "end": f"t{tid}", "template_name": template,
            "service": service, "output": output}


def test_a_recorded_result_is_read():
    out = ["CUSTOM STATS: ****", _run_line({"service": "tududi", "step": "secrets-approle",
                                            "status": "pass", "evidence": {"empty": []}})]
    agg = step_results.aggregate(REGISTRY, [_task(1, "success", "Check Secrets", out)])
    assert agg["services"]["tududi"]["secrets-approle"]["status"] == "pass"
    assert agg["failed_steps"] == {"tududi": []}


def test_a_failure_without_a_result_is_recorded_with_its_tail():
    # Spec scenario "A failure with no result is still recorded".
    out = [f"line {i}" for i in range(30)]
    agg = step_results.aggregate(REGISTRY, [_task(2, "error", "Apply Firewall (Dev)", out)])
    rec = agg["services"]["tududi"]["fw-harden"]
    assert rec["status"] == "fail"
    assert rec["error"].splitlines() == [f"line {i}" for i in range(10, 30)]
    assert agg["failed_steps"]["tududi"] == ["fw-harden"]


def test_the_latest_task_wins():
    old = _task(3, "success", "Check Secrets", [_run_line(
        {"service": "tududi", "step": "secrets-approle", "status": "fail", "error": "empty keys: x"})])
    new = _task(4, "success", "Check Secrets", [_run_line(
        {"service": "tududi", "step": "secrets-approle", "status": "pass"})])
    agg = step_results.aggregate(REGISTRY, [new, old])
    assert agg["services"]["tududi"]["secrets-approle"]["task_id"] == 4
    assert agg["failed_steps"]["tududi"] == []


def test_running_tasks_and_unmapped_failures_are_ignored():
    agg = step_results.aggregate(REGISTRY, [
        _task(5, "running", "Check Secrets", []),
        _task(6, "error", "Some Unregistered Template", ["boom"]),
    ])
    assert agg["services"] == {}


def test_per_service_deploy_template_maps_to_service_deploy():
    agg = step_results.aggregate(REGISTRY, [_task(7, "error", "Deploy tududi (Local)", ["x"])])
    assert "service-deploy" in agg["services"]["tududi"]


def test_loki_streams_label_every_result():
    out = [_run_line({"service": "tududi", "step": "secrets-approle", "status": "pass"})]
    agg = step_results.aggregate(REGISTRY, [_task(8, "success", "Check Secrets", out)])
    streams = step_results.loki_streams(agg, 1700000000000000000)
    assert streams == [{
        "stream": {"job": "agent-cloud-conformance", "service": "tududi",
                   "step": "secrets-approle", "status": "pass"},
        "values": [["1700000000000000000", json.dumps(
            {"check_mode": None, "error": None, "task_id": 8}, sort_keys=True)]],
    }]
