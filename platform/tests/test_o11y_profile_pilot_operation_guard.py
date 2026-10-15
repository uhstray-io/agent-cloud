from pathlib import Path

import yaml
from jinja2 import Environment

ROOT = Path(__file__).resolve().parents[2]
GUARD = ROOT / "platform/playbooks/tasks/assert-o11y-profile-pilot-operation.yml"
DEPLOY = ROOT / "platform/playbooks/deploy-o11y.yml"
CLEAN_DEPLOY = ROOT / "platform/playbooks/clean-deploy-o11y.yml"
GUARD_TASK = "tasks/assert-o11y-profile-pilot-operation.yml"


def test_shared_guard_refuses_enabled_or_requested_profile_pilot():
    tasks = yaml.safe_load(GUARD.read_text())
    assertion = tasks[0]["ansible.builtin.assert"]["that"]
    enabled_gate, operation_gate = assertion
    evaluate = Environment().compile_expression(enabled_gate)
    operation_check = Environment().compile_expression(operation_gate)

    assert evaluate()
    assert evaluate(o11y_alloy_profile_pilot_enabled=False)
    assert not evaluate(o11y_alloy_profile_pilot_enabled=True)
    assert not evaluate(o11y_alloy_profile_pilot_enabled="false")
    assert operation_check()
    assert not operation_check(o11y_profile_pilot_operation="activate")


def test_normal_and_clean_deploy_run_guard_before_mutation():
    deploy = yaml.safe_load(DEPLOY.read_text())
    deploy_play = next(play for play in deploy if play.get("name", "").startswith("Phase 1:"))
    clean = yaml.safe_load(CLEAN_DEPLOY.read_text())
    clean_play = next(play for play in clean if play.get("name") == "Clean o11y deployment")

    assert deploy_play["tasks"][0]["ansible.builtin.include_tasks"] == GUARD_TASK
    assert clean_play["tasks"][0]["ansible.builtin.include_tasks"] == GUARD_TASK
    assert not any("clean-service.yml" in str(task) for task in clean_play["tasks"][:2])


def test_shared_guard_has_no_file_container_or_volume_mutation():
    task_text = GUARD.read_text()
    task = yaml.safe_load(task_text)[0]

    assert set(task) == {"name", "ansible.builtin.assert"}
    assert "command:" not in task_text
    assert "file:" not in task_text
    assert "podman" not in task_text
    assert "volume" not in task_text.lower()
