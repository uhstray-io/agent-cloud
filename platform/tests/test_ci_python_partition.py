"""Keep CI's Python test shards complete, disjoint, and mandatory."""

import os
import subprocess
import tomllib
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = REPO / ".github/workflows/lint-and-test.yml"
SAFETY_FILES = {
    "platform/tests/test_rollback_inference_route.py",
    "platform/tests/test_executor_internal_overrides.py",
}
PYTEST_FLAGS = ("-v", "-x", "-n", "auto", "--dist", "worksteal", "--durations=20")


def _jobs():
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]


def test_python_shards_keep_root_discovery_and_select_whole_safety_files():
    jobs = _jobs()
    core = jobs["python-core"]
    safety = jobs["python-playbook-safety"]
    core_run = next(step["run"] for step in core["steps"] if step.get("name") == "Run Python core tests")
    safety_run = next(step["run"] for step in safety["steps"] if step.get("name") == "Run Python Playbook Safety tests")

    pytest_config = tomllib.loads((REPO / "pyproject.toml").read_text())["tool"]["pytest"]["ini_options"]
    assert "platform/tests" in pytest_config["testpaths"]
    assert not any(flag.startswith("--ignore") for flag in core_run.split())
    assert {arg.split("=", 1)[1] for arg in core_run.split() if arg.startswith("--deselect=")} == {
        "platform/services/netbox/deployment/tests/", "platform/semaphore/tests/", *SAFETY_FILES,
    }
    assert {arg for arg in safety_run.split() if arg.startswith("platform/tests/")} == SAFETY_FILES
    assert all(flag in core_run and flag in safety_run for flag in PYTEST_FLAGS)
    assert safety["timeout-minutes"] == 30
    dependencies = next(step["run"] for step in safety["steps"] if step.get("name") == "Install test dependencies")
    assert "platform/requirements-test.txt" in dependencies
    assert "platform/requirements-controller.txt" in dependencies
    assert "collections/requirements.yml" in dependencies


def test_unit_tests_aggregate_requires_playbook_safety_success_for_every_result():
    aggregate = _jobs()["test"]
    assert aggregate["if"] == "always()"
    assert "python-playbook-safety" in aggregate["needs"]
    gate = aggregate["steps"][0]
    required = {
        "PYTHON_CORE_RESULT", "PYTHON_PLAYBOOK_SAFETY_RESULT", "PYTHON_NETBOX_RESULT",
        "PYTHON_SEMAPHORE_RESULT", "BATS_RESULT", "REGO_RESULT",
    }
    assert set(gate["env"]) == required
    for name in required:
        assert "${{ needs." in gate["env"][name]
    script = gate["run"]
    for name in required:
        assert f'test "${name}" = success' in script

    for failed_name in required:
        for result_value in ("failure", "cancelled", "skipped", ""):
            values = dict.fromkeys(required, "success")
            values[failed_name] = result_value
            result = subprocess.run(["bash", "-e", "-c", script], env={**os.environ, **values}, check=False)
            assert result.returncode != 0, (failed_name, result_value)
