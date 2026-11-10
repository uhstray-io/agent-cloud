"""Keep CI's Python test shards complete, disjoint, and mandatory (plan 21 phase 2).

CI runs the whole root pytest collection as one matrix job of N shards. The root conftest.py skips,
at collection time, every test file that platform/tests/shards.py does not assign to the shard named
by PYTEST_SHARD=k/N. These tests prove the assignment covers exactly the root collection, that the
hook really applies it, and that the workflow and the Unit Tests aggregate agree on the matrix.
"""

import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = REPO / ".github/workflows/lint-and-test.yml"
PYTEST_FLAGS = ("-v", "-x", "-n", "auto", "--dist", "worksteal", "--durations=20")
RUN_STEP = re.compile(r"Run Python tests \(shard \$\{\{ matrix\.shard \}\} of (\d+)\)")

sys.path.insert(0, str(Path(__file__).resolve().parent))
import shards  # noqa: E402


def _jobs():
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]


def _run_step(job):
    return next(step for step in job["steps"] if RUN_STEP.fullmatch(step.get("name", "")))


def _shard_count():
    return int(RUN_STEP.fullmatch(_run_step(_jobs()["python"])["name"]).group(1))


def _testpaths():
    return tomllib.loads((REPO / "pyproject.toml").read_text())["tool"]["pytest"]["ini_options"]["testpaths"]


def _collect(env, *args):
    """Node IDs pytest collects from the repository root with the shard job's environment."""
    environment = {key: value for key, value in os.environ.items() if key != "PYTEST_SHARD"}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", *args],
        cwd=REPO, env={**environment, **env}, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, f"pytest collection failed:\n{result.stdout}\n{result.stderr}"
    return {line.strip() for line in result.stdout.splitlines() if "::" in line}


def _assignment(n):
    files = shards.enumerate_files(REPO, _testpaths())
    weights = shards.file_weights(files, shards.load_durations(REPO / "platform/tests/test_durations.json"), REPO)
    return files, shards.assign(files, weights, n)


@pytest.fixture(scope="module")
def root_node_ids():
    env = _run_step(_jobs()["python"])["env"]
    ids = _collect({key: value for key, value in env.items() if key != "PYTEST_SHARD"})
    assert ids, "the root collection is empty"
    return ids


def test_the_python_matrix_runs_the_root_collection_in_n_shards():
    job = _jobs()["python"]
    n = _shard_count()
    assert job["name"] == f"Python (${{{{ matrix.shard }}}}/{n})"
    assert job["strategy"]["matrix"]["shard"] == list(range(1, n + 1))
    assert job["strategy"]["fail-fast"] is False
    assert job["timeout-minutes"] == 30
    checkout = next(step for step in job["steps"] if step.get("uses", "").startswith("actions/checkout@"))
    assert checkout.get("with", {}).get("persist-credentials") is False
    dependencies = next(step["run"] for step in job["steps"] if step.get("name") == "Install test dependencies")
    assert "uv sync --locked" in dependencies
    assert "collections/requirements.yml" in dependencies

    step = _run_step(job)
    assert step["env"]["PYTEST_SHARD"] == f"${{{{ matrix.shard }}}}/{n}"
    assert step["env"]["PYTHONPATH"] == (
        "platform/services/netbox/deployment/workers/proxmox_discovery:"
        "platform/services/netbox/deployment/workers/pfsense_sync")
    args = step["run"].split()
    assert args[0] == "pytest"
    assert all(flag in args for flag in PYTEST_FLAGS)
    assert "junit_family=xunit1" in args
    # Root discovery only: a path, --deselect or --ignore would make the shards miss tests.
    assert not [arg for arg in args[1:] if not arg.startswith("-") and arg not in {
        "auto", "worksteal", "junit_family=xunit1"}]
    assert not [arg for arg in args if arg.startswith(("--deselect", "--ignore", "-k", "-m"))]
    assert "platform/tests" in _testpaths()


def test_the_shards_partition_exactly_the_root_collection(root_node_ids):
    # The hook enumerates files itself; every file the root collects must be one it enumerated, or
    # that file would fall to shard 1 by default instead of by assignment.
    n = _shard_count()
    files, assignment = _assignment(n)
    collected_files = {node.split("::", 1)[0] for node in root_node_ids}
    assert collected_files <= set(files), sorted(collected_files - set(files))[:10]
    by_shard = {k: {node for node in root_node_ids if assignment[node.split("::", 1)[0]] == k}
                for k in range(1, n + 1)}
    assert all(by_shard.values()), {k: len(v) for k, v in by_shard.items()}
    assert sum(len(v) for v in by_shard.values()) == len(root_node_ids)
    assert set().union(*by_shard.values()) == root_node_ids


def test_the_hook_collects_only_its_own_shard():
    # A directory, not file paths: paths named on the command line bypass pytest_ignore_collect.
    n = _shard_count()
    directory = "platform/semaphore/tests"
    _, assignment = _assignment(n)
    mine = {f for f in assignment if f.startswith(directory + "/")}
    k = next(k for k in range(1, n + 1) if 0 < sum(assignment[f] == k for f in mine) < len(mine))
    collected = {node.split("::", 1)[0] for node in _collect({"PYTEST_SHARD": f"{k}/{n}"}, directory)}
    assert collected == {f for f in mine if assignment[f] == k}


def test_no_job_runs_the_pi_provider_that_moved_to_the_harness_repository():
    # The provider lives in uhstray-io/harness (harnesses/pi/extensions/uhstray-provider), and its
    # tests run there. A job pointed at its old directory here would test nothing that exists.
    assert "node-pi-provider" not in _jobs()
    assert "pi-provider" not in WORKFLOW.read_text()
    assert not (REPO / "platform/services/agentgateway/context/clients/pi-provider").exists()


def test_unit_tests_aggregate_requires_every_test_group_to_succeed():
    aggregate = _jobs()["test"]
    assert aggregate["if"] == "always()"
    assert aggregate["needs"] == ["python", "bats", "rego", "rust-console"]
    gate = aggregate["steps"][0]
    required = {"PYTHON_RESULT", "BATS_RESULT", "REGO_RESULT", "RUST_CONSOLE_RESULT"}
    assert set(gate["env"]) == required
    assert gate["env"]["PYTHON_RESULT"] == "${{ needs.python.result }}"
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
