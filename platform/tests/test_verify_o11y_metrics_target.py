"""A named target stays verifiable when a sibling scrape is unhealthy."""

from pathlib import Path

import yaml
from jinja2 import Environment

ROOT = Path(__file__).resolve().parents[2]


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
    playbook = yaml.safe_load((ROOT / "platform/playbooks/verify-o11y-metrics-target.yml").read_text())
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
