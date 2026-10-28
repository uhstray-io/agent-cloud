"""A named target stays verifiable when a sibling scrape is unhealthy."""

import json
import shutil
from pathlib import Path

import harness_sandbox
import playbook_yaml
import pytest
import yaml
from jinja2 import Environment

ROOT = Path(__file__).resolve().parents[2]
METRICS_TASK = ROOT / "platform/playbooks/tasks/verify-o11y-metrics.yml"


def test_exact_instance_filters_sibling_before_health_assertion():
    tasks = yaml.safe_load((ROOT / "platform/playbooks/tasks/verify-o11y-metrics.yml").read_text())
    values = tasks[1]["vars"]
    env = Environment()

    def evaluate(name, **context):
        expression = values[name].strip()[2:-2].strip()
        return env.compile_expression(expression)(**context)

    healthy = {"metric": {"instance": "node-1:9100"}, "value": [1, "1"]}
    down = {"metric": {"instance": "node-2:9100"}, "value": [1, "0"]}
    data = {"data": {"result": [healthy, down]}}
    selected = evaluate("_service_scrapes", _scrapes_json=data, expected_instance="node-1:9100")
    assert selected == [healthy]
    assert evaluate("_unhealthy_scrapes", _service_scrapes=selected) == []
    all_targets = evaluate("_service_scrapes", _scrapes_json=data, expected_instance="")
    assert all_targets == [healthy, down]
    assert evaluate("_unhealthy_scrapes", _service_scrapes=all_targets) == [down]


def test_dev_verifier_keeps_revision_and_survey_guards():
    playbook = playbook_yaml.plays(ROOT / "platform/playbooks/verify-o11y-metrics-target.yml")
    assert playbook[0]["any_errors_fatal"] is True
    tasks = playbook[0]["tasks"]
    assert any(task["name"] == "Read controller checkout changes" for task in tasks)
    guards = tasks[-1]["ansible.builtin.assert"]["that"]
    assert "_controller_changes.stdout | length == 0" in guards
    assert "_controller_revision.stdout == expected_repository_sha" in guards
    assert any("expected_instance" in guard for guard in guards)
    assert any("expected_metric" in guard for guard in guards)
    assert "expected_metric is not match('^(up|scrape_.*)$')" in guards
    assert playbook[1]["tasks"][1]["check_mode"] is False
    names = {field["name"] for template in yaml.safe_load(
        (ROOT / "platform/semaphore/templates.yml").read_text())["templates"]
        if template["name"] == "Verify o11y Metrics Target (Dev)"
        for field in template["survey_vars"]}
    assert names == {"expected_service", "expected_instance", "expected_metric", "expected_repository_sha"}


def _run_shared_verifier(tmp_path, rows, expected_instance=""):
    tmp_path.mkdir()
    fake_prometheus = tmp_path / "fake-prometheus"
    fake_prometheus.write_text(
        """#!/usr/bin/env python3
import json
import os
import re
import sys
from urllib.parse import parse_qs, urlsplit

query = parse_qs(urlsplit(sys.argv[-1]).query).get("query", [""])[0]
if 'job!="o11y-availability"' not in query:
    sys.exit(2)
service = re.search(r'service="([^"]+)"', query)
if service is None:
    sys.exit(3)
rows = json.loads(open(os.environ["FAKE_PROMETHEUS_ROWS"], encoding="utf-8").read())
result = [row for row in rows if row["metric"].get("service") == service.group(1)
          and row["metric"].get("job") != "o11y-availability"]
print(json.dumps({"status": "success", "data": {"result": result}}))
"""
    )
    fake_prometheus.chmod(0o755)
    rows_path = tmp_path / "prometheus-rows.json"
    rows_path.write_text(json.dumps(rows))
    playbook = tmp_path / "verify.yml"
    playbook.write_text(yaml.safe_dump([{
        "hosts": "localhost",
        "connection": "local",
        "gather_facts": False,
        "vars": {
            "_engine": str(fake_prometheus),
            "expected_service": "checkout",
            "expected_instance": expected_instance,
        },
        "tasks": [{"ansible.builtin.include_tasks": str(METRICS_TASK)}],
    }]))
    env = harness_sandbox.env_for(tmp_path)
    env["FAKE_PROMETHEUS_ROWS"] = str(rows_path)
    return harness_sandbox.run(
        ["ansible-playbook", "-i", "localhost,", "-c", "local", str(playbook)],
        tmp_path,
        cwd=ROOT,
        env=env,
    )


def _series(instance, job, value):
    return {"metric": {"service": "checkout", "instance": instance, "job": job}, "value": [1, str(value)]}


@pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")
def test_shared_verifier_accepts_real_scrapes_and_rejects_probe_only_or_probe_rescued_data(tmp_path):
    real = _series("checkout:8080", "app", 1)
    probe = _series("probe:9115", "o11y-availability", 1)
    down = _series("checkout-sibling:8080", "app", 0)

    real_and_probe = _run_shared_verifier(tmp_path / "real-and-probe", [real, probe], "checkout:8080")
    assert real_and_probe.returncode == 0, real_and_probe.stdout[-2000:]

    probe_only = _run_shared_verifier(tmp_path / "probe-only", [probe])
    assert probe_only.returncode != 0, probe_only.stdout[-2000:]

    unhealthy_real_with_probe = _run_shared_verifier(tmp_path / "mixed-unhealthy", [down, probe])
    assert unhealthy_real_with_probe.returncode != 0, unhealthy_real_with_probe.stdout[-2000:]


def test_service_overview_generic_metrics_queries_exclude_probe_job():
    dashboard_path = ROOT / "platform/services/o11y/deployment/config/grafana/dashboards/service-overview.json"
    dashboard = json.loads(dashboard_path.read_text())
    expressions = [
        target["expr"]
        for panel in dashboard["panels"]
        for target in panel.get("targets", [])
        if "up{" in target.get("expr", "")
    ]
    assert expressions == [
        'min by (service) (up{service=~"$service",job!="o11y-availability"})',
        'count by (service) (up{service=~"$service",job!="o11y-availability"})',
        'avg by (service) (up{service=~"$service",job!="o11y-availability"}) * 100',
        'up{service=~"$service",job!="o11y-availability"} == 0',
    ]
    service = next(item for item in dashboard["templating"]["list"] if item["name"] == "service")
    assert service["query"] == 'label_values(up{job!="o11y-availability"}, service)'
