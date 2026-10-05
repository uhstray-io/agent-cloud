"""The promotion guard refuses a dev -> main PR whose head lacks a commit on main
(docs/MISTAKES.md 10.23).

sync-main-to-dev cannot push a main-only change to a workflow file into dev, so a promotion
from such a dev would revert it on main. The guard's own step script is run here against
throwaway repositories, so the test exercises the shipped shell rather than a copy of it.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = REPO / ".github/workflows/enforce-promotion-source.yml"
RULESET = REPO / ".github/rulesets/protect-main.json"
JOB = "promotion-source"
ANCESTRY_STEP = "Require dev to contain every commit on main"
SOURCE_STEP = "Require the PR to originate from dev"


def _job() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())["jobs"][JOB]


def _step(name: str) -> dict:
    return next(s for s in _job()["steps"] if s.get("name") == name)


def _git_env(home: Path) -> dict:
    # Git hooks export GIT_DIR and friends; inheriting them would point these commands at
    # the real repository instead of the throwaway ones.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        HOME=str(home),
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.invalid",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.invalid",
    )
    return env


def _git(env: dict, cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


def _commit(env: dict, cwd: Path, name: str) -> str:
    (cwd / name).write_text(name)
    _git(env, cwd, "add", name)
    _git(env, cwd, "commit", "-q", "-m", name)
    return _git(env, cwd, "rev-parse", "HEAD")


def _setup(tmp_path: Path) -> tuple[dict, Path, Path]:
    """origin (bare) with main = A and dev = A-B; a work clone to push from."""
    env = _git_env(tmp_path)
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    _git(env, tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    _git(env, tmp_path, "clone", "-q", str(origin), str(work))
    _git(env, work, "checkout", "-q", "-b", "main")
    _commit(env, work, "A")
    _git(env, work, "push", "-q", "origin", "main")
    _git(env, work, "checkout", "-q", "-b", "dev")
    _commit(env, work, "B")
    _git(env, work, "push", "-q", "origin", "dev")
    return env, origin, work


def _run_guard(env: dict, origin: Path, tmp_path: Path, head_sha: str):
    """Run the step as Actions would, in a fresh clone checked out at the PR head."""
    ci = tmp_path / f"ci-{head_sha[:12]}"
    _git(env, tmp_path, "clone", "-q", str(origin), str(ci))
    _git(env, ci, "checkout", "-q", "--detach", head_sha)
    return _run_script(env, ci, head_sha)


def _run_script(env: dict, ci: Path, head_sha: str):
    return subprocess.run(
        ["bash", "-e", "-c", _step(ANCESTRY_STEP)["run"]],
        cwd=ci,
        env={**env, "HEAD_SHA": head_sha},
        capture_output=True,
        text=True,
    )


def test_job_name_is_the_required_check_context():
    contexts = {
        c["context"]
        for rule in json.loads(RULESET.read_text())["rules"]
        if rule["type"] == "required_status_checks"
        for c in rule["parameters"]["required_status_checks"]
    }
    assert _job()["name"] in contexts


def test_workflow_token_is_read_only():
    assert yaml.safe_load(WORKFLOW.read_text())["permissions"] == {"contents": "read"}


def test_checkout_fetches_full_history_at_the_pr_head():
    steps = _job()["steps"]
    checkout = next(s for s in steps if str(s.get("uses", "")).startswith("actions/checkout@"))
    assert checkout["with"]["fetch-depth"] == 0
    assert checkout["with"]["ref"] == "${{ github.event.pull_request.head.sha }}"
    assert steps.index(checkout) < steps.index(_step(ANCESTRY_STEP))
    assert _step(ANCESTRY_STEP)["env"]["HEAD_SHA"] == "${{ github.event.pull_request.head.sha }}"


def test_passes_when_dev_contains_main(tmp_path):
    env, origin, work = _setup(tmp_path)
    result = _run_guard(env, origin, tmp_path, _git(env, work, "rev-parse", "dev"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK: dev contains every commit on main." in result.stdout


def test_refuses_when_main_has_a_commit_dev_lacks(tmp_path):
    env, origin, work = _setup(tmp_path)
    dev = _git(env, work, "rev-parse", "dev")
    ci = tmp_path / "ci"
    _git(env, tmp_path, "clone", "-q", str(origin), str(ci))
    _git(env, ci, "checkout", "-q", "--detach", dev)
    # main moves AFTER the CI clone, so a guard that skipped its own fetch would compare
    # against a stale origin/main and pass.
    _git(env, work, "checkout", "-q", "main")
    _commit(env, work, "C-main-only")
    _git(env, work, "push", "-q", "origin", "main")

    result = _run_script(env, ci, dev)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "::error title=main has commits dev lacks::" in result.stdout
    assert "revert 1 commit(s)" in result.stdout
    assert "C-main-only" in result.stdout


def test_passes_once_main_is_merged_into_dev(tmp_path):
    env, origin, work = _setup(tmp_path)
    _git(env, work, "checkout", "-q", "main")
    _commit(env, work, "C-main-only")
    _git(env, work, "push", "-q", "origin", "main")
    _git(env, work, "checkout", "-q", "dev")
    _git(env, work, "merge", "-q", "--no-ff", "--no-edit", "main")
    _git(env, work, "push", "-q", "origin", "dev")

    result = _run_guard(env, origin, tmp_path, _git(env, work, "rev-parse", "dev"))
    assert result.returncode == 0, result.stdout + result.stderr


def test_an_unresolvable_head_is_an_error_not_a_missing_commit(tmp_path):
    env, origin, work = _setup(tmp_path)
    dev = _git(env, work, "rev-parse", "dev")
    ci = tmp_path / "ci"
    _git(env, tmp_path, "clone", "-q", str(origin), str(ci))
    _git(env, ci, "checkout", "-q", "--detach", dev)

    result = _run_script(env, ci, "0" * 40)
    assert result.returncode not in (0, 1), result.stdout + result.stderr
    assert "::error title=Ancestry check failed::" in result.stdout
    assert "main has commits dev lacks" not in result.stdout


def _run_source(head_ref: str, head_repo: str, base_repo: str = "uhstray-io/agent-cloud"):
    return subprocess.run(
        ["bash", "-e", "-c", _step(SOURCE_STEP)["run"]],
        env={"PATH": os.environ["PATH"], "HEAD_REF": head_ref, "HEAD_REPO": head_repo, "BASE_REPO": base_repo},
        capture_output=True,
        text=True,
    )


def test_source_step_reads_the_head_repository_from_the_event():
    env = _step(SOURCE_STEP)["env"]
    assert env["HEAD_REF"] == "${{ github.head_ref }}"
    assert env["HEAD_REPO"] == "${{ github.event.pull_request.head.repo.full_name }}"
    assert env["BASE_REPO"] == "${{ github.repository }}"
    # Values arrive through env only; an inline expression in the script is an injection path.
    assert "${{" not in _step(SOURCE_STEP)["run"]


def test_source_passes_for_this_repositorys_dev():
    result = _run_source("dev", "uhstray-io/agent-cloud")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK: promotion source is 'dev' -> 'main'." in result.stdout


def test_source_refuses_a_forks_branch_named_dev():
    result = _run_source("dev", "someone/agent-cloud")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "not a fork" in result.stdout
    assert "someone/agent-cloud" in result.stdout


def test_source_refuses_a_branch_other_than_dev():
    result = _run_source("feat/x", "uhstray-io/agent-cloud")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "must come from 'dev' (got 'feat/x')" in result.stdout
