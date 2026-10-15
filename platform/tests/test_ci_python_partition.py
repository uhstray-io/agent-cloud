"""Keep CI's Python test shards complete, disjoint, and mandatory."""

import os
import shlex
import subprocess
import sys
import tomllib
from itertools import combinations
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = REPO / ".github/workflows/lint-and-test.yml"
SAFETY_FILES = {
    "platform/tests/test_rollback_inference_route.py",
    "platform/tests/test_executor_internal_overrides.py",
}
PYTEST_FLAGS = ("-v", "-x", "-n", "auto", "--dist", "worksteal", "--durations=20")
PYTHON_SHARDS = {
    "python-core": "Run Python core tests",
    "python-playbook-safety": "Run Python Playbook Safety tests",
    "python-agentgateway": "Run Python Agentgateway tests",
    "python-netbox": "Run NetBox Python tests",
    "python-semaphore": "Run Semaphore Python tests",
}


def _jobs():
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]


def _pytest_step(job, step_name):
    step = next(step for step in job["steps"] if step.get("name") == step_name)
    return step["run"], step.get("env", {})


def _node_ids(command, env, *, root=False):
    args = shlex.split(command)
    assert args[0] == "pytest", command
    args = args[1:]
    collected_args = []
    skip_value = False
    for arg in args:
        if skip_value:
            skip_value = False
            continue
        if arg in {"-v", "-x"}:
            continue
        if arg in {"-n", "--dist", "--durations"}:
            skip_value = True
            continue
        if root and arg == "--deselect":
            skip_value = True
            continue
        if arg.startswith("--durations=") or (root and arg.startswith("--deselect=")):
            continue
        collected_args.append(arg)

    result = subprocess.run(
        [sys.executable, "-m", "pytest", *collected_args, "--collect-only", "-q"],
        cwd=REPO,
        env={**os.environ, **env},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"pytest collection failed for {command!r}:\n{result.stdout}\n{result.stderr}"
    )
    node_ids = {line.strip() for line in result.stdout.splitlines() if "::" in line}
    assert node_ids, f"pytest collected no node IDs for {command!r}:\n{result.stdout}"
    return node_ids


def test_python_shards_keep_root_discovery_and_select_whole_safety_files():
    jobs = _jobs()
    core = jobs["python-core"]
    safety = jobs["python-playbook-safety"]
    core_run, _ = _pytest_step(core, PYTHON_SHARDS["python-core"])
    safety_run, _ = _pytest_step(safety, PYTHON_SHARDS["python-playbook-safety"])
    checkout = next(step for step in safety["steps"] if step.get("uses", "").startswith("actions/checkout@"))

    pytest_config = tomllib.loads((REPO / "pyproject.toml").read_text())["tool"]["pytest"]["ini_options"]
    assert checkout.get("with", {}).get("persist-credentials") is False
    assert "platform/tests" in pytest_config["testpaths"]
    assert not any(flag.startswith("--ignore") for flag in core_run.split())
    agw = jobs["python-agentgateway"]
    agw_run, _ = _pytest_step(agw, PYTHON_SHARDS["python-agentgateway"])
    agw_files = {arg for arg in agw_run.split() if arg.startswith("platform/tests/")}
    assert agw_files, "the Python Agentgateway shard names no test files"
    assert all(Path(name).name.startswith("test_agw_") and (REPO / name).is_file() for name in agw_files)
    assert {arg.split("=", 1)[1] for arg in core_run.split() if arg.startswith("--deselect=")} == {
        "platform/services/netbox/deployment/tests/", "platform/semaphore/tests/", *SAFETY_FILES, *agw_files,
    }
    assert all(flag in agw_run for flag in PYTEST_FLAGS)
    assert agw["timeout-minutes"] == 30
    agw_checkout = next(step for step in agw["steps"] if step.get("uses", "").startswith("actions/checkout@"))
    assert agw_checkout.get("with", {}).get("persist-credentials") is False
    agw_dependencies = next(step["run"] for step in agw["steps"] if step.get("name") == "Install test dependencies")
    assert "platform/requirements-test.txt" in agw_dependencies
    assert "platform/requirements-controller.txt" in agw_dependencies
    assert "collections/requirements.yml" in agw_dependencies
    assert {arg for arg in safety_run.split() if arg.startswith("platform/tests/")} == SAFETY_FILES
    assert all(flag in core_run and flag in safety_run for flag in PYTEST_FLAGS)
    assert safety["timeout-minutes"] == 30
    dependencies = next(step["run"] for step in safety["steps"] if step.get("name") == "Install test dependencies")
    assert "platform/requirements-test.txt" in dependencies
    assert "platform/requirements-controller.txt" in dependencies
    assert "collections/requirements.yml" in dependencies


def test_python_shards_collect_exactly_the_root_node_ids():
    jobs = _jobs()
    runs = {
        name: _pytest_step(jobs[name], step_name)
        for name, step_name in PYTHON_SHARDS.items()
    }
    core_run, core_env = runs["python-core"]
    root = _node_ids(core_run, core_env, root=True)
    shards = {
        name: _node_ids(command, env)
        for name, (command, env) in runs.items()
    }

    assert shards["python-playbook-safety"], "the Python Playbook Safety shard is empty"
    assert shards["python-agentgateway"], "the Python Agentgateway shard is empty"
    for (left_name, left), (right_name, right) in combinations(shards.items(), 2):
        overlap = left & right
        assert not overlap, f"{left_name} and {right_name} overlap: {sorted(overlap)[:10]}"
    union = set().union(*shards.values())
    assert union == root, (
        f"shard union differs from root: missing={sorted(root - union)[:10]}, "
        f"extra={sorted(union - root)[:10]}"
    )


def test_unit_tests_aggregate_requires_playbook_safety_success_for_every_result():
    aggregate = _jobs()["test"]
    assert aggregate["if"] == "always()"
    assert "python-playbook-safety" in aggregate["needs"]
    assert "python-agentgateway" in aggregate["needs"]
    gate = aggregate["steps"][0]
    required = {
        "PYTHON_CORE_RESULT", "PYTHON_PLAYBOOK_SAFETY_RESULT", "PYTHON_AGENTGATEWAY_RESULT",
        "PYTHON_NETBOX_RESULT", "PYTHON_SEMAPHORE_RESULT", "BATS_RESULT", "REGO_RESULT",
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
