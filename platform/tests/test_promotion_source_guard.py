"""The promotion guard refuses a dev -> main PR whose head lacks a commit on main
(docs/MISTAKES.md 10.23).

sync-main-to-dev cannot push a main-only change to a workflow file into dev, so a promotion
from such a dev would revert it on main. The guard's own step script is run here against
throwaway repositories, so the test exercises the shipped shell rather than a copy of it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = REPO / ".github/workflows/enforce-promotion-source.yml"
RULESET = REPO / ".github/rulesets/protect-main.json"
SEMVER = REPO / "scripts/semver.py"
JOB = "promotion-source"
ANCESTRY_STEP = "Require dev to contain every commit on main"
SOURCE_STEP = "Require the PR to originate from dev"
BUDGET_STEP = "Require the release to fit the release budget"


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


# The release budget step (policy 10 R2, R3, R12). It runs the real scripts/semver.py, committed on
# main, so the count the guard reports is the count the release run reports.

BUDGET_OVER_FILES = tuple(f"many/{i:03}.txt" for i in range(121))  # one more than R3's 120


def _pr_commit(env: dict, cwd: Path, subject: str, files: tuple[str, ...]) -> str:
    """A squash-style pull request commit: one parent, subject `title (#N)`, touching files."""
    for name in files:
        path = cwd / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(subject)
    _git(env, cwd, "add", *files)
    _git(env, cwd, "commit", "-q", "-m", subject)
    return _git(env, cwd, "rev-parse", "HEAD")


def _tag(env: dict, work: Path, tag: str) -> None:
    """An annotated release tag on main's tip, pushed to origin."""
    _git(env, work, "tag", "-a", tag, "-m", tag, _git(env, work, "rev-parse", "main"))
    _git(env, work, "push", "-q", "origin", tag)


def _setup_budget(tmp_path: Path) -> tuple[dict, Path, Path]:
    """origin with main = A, tagged v0.1.0, and the real semver.py committed before dev branches.

    The work clone is left on dev at A. Each test pushes dev itself, so it decides when the
    commits and any v0.15.0 tag reach origin.
    """
    env = _git_env(tmp_path)
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    _git(env, tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    _git(env, tmp_path, "clone", "-q", str(origin), str(work))
    _git(env, work, "checkout", "-q", "-b", "main")
    (work / "scripts").mkdir()
    shutil.copy(SEMVER, work / "scripts/semver.py")
    _git(env, work, "add", "scripts/semver.py")
    _git(env, work, "commit", "-q", "-m", "A")
    _git(env, work, "push", "-q", "origin", "main")
    _tag(env, work, "v0.1.0")
    _git(env, work, "checkout", "-q", "-b", "dev")
    return env, origin, work


def _ci_clone(env: dict, origin: Path, tmp_path: Path, name: str, head_sha: str) -> Path:
    """A CI clone of origin, detached at the PR head, as Actions checks it out."""
    ci = tmp_path / name
    _git(env, tmp_path, "clone", "-q", str(origin), str(ci))
    _git(env, ci, "checkout", "-q", "--detach", head_sha)
    return ci


def _run_budget(env: dict, ci: Path, head_sha: str):
    return subprocess.run(
        ["bash", "-e", "-c", _step(BUDGET_STEP)["run"]],
        cwd=ci,
        env={**env, "HEAD_SHA": head_sha},
        capture_output=True,
        text=True,
    )


def test_budget_step_runs_last_and_reads_the_head_from_the_event():
    steps = _job()["steps"]
    assert steps[-1]["name"] == BUDGET_STEP
    assert steps.index(_step(ANCESTRY_STEP)) < steps.index(_step(BUDGET_STEP))
    assert _step(BUDGET_STEP)["env"] == {"HEAD_SHA": "${{ github.event.pull_request.head.sha }}"}
    # Values arrive through env only; an inline expression in the script is an injection path.
    assert "${{" not in _step(BUDGET_STEP)["run"]


def test_budget_passes_over_the_limit_before_v0_15_0(tmp_path):
    env, origin, work = _setup_budget(tmp_path)
    head = _pr_commit(env, work, "fix: many (#1)", BUDGET_OVER_FILES)
    _git(env, work, "push", "-q", "origin", "dev")
    result = _run_budget(env, _ci_clone(env, origin, tmp_path, "ci", head), head)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "::notice title=Release budget transition::" in result.stdout
    assert "1 pull requests and 121 files" in result.stdout


def test_budget_fails_over_the_file_limit_once_v0_15_0_is_tagged(tmp_path):
    env, origin, work = _setup_budget(tmp_path)
    _tag(env, work, "v0.15.0")
    head = _pr_commit(env, work, "fix: many (#1)", BUDGET_OVER_FILES)
    _git(env, work, "push", "-q", "origin", "dev")
    result = _run_budget(env, _ci_clone(env, origin, tmp_path, "ci", head), head)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "::error title=Release over budget::" in result.stdout
    assert "121 files (limit 120)" in result.stdout
    assert "since v0.15.0" in result.stdout


def test_budget_fails_over_the_pull_request_limit(tmp_path):
    env, origin, work = _setup_budget(tmp_path)
    _tag(env, work, "v0.15.0")
    for n in range(1, 102):
        # The same file each time: 101 pull requests and one file, so only the PR count is over.
        head = _pr_commit(env, work, f"fix: change {n} (#{n})", ("same.txt",))
    _git(env, work, "push", "-q", "origin", "dev")
    result = _run_budget(env, _ci_clone(env, origin, tmp_path, "ci", head), head)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "::error title=Release over budget::101 pull requests (limit 100)" in result.stdout


def test_budget_passes_within_the_limits_after_v0_15_0(tmp_path):
    env, origin, work = _setup_budget(tmp_path)
    _tag(env, work, "v0.15.0")
    _pr_commit(env, work, "fix: one (#1)", ("one.txt",))
    _pr_commit(env, work, "feat: two (#2)", ("two.txt",))
    head = _pr_commit(env, work, "docs: three (#3)", ("three.txt",))
    _git(env, work, "push", "-q", "origin", "dev")
    result = _run_budget(env, _ci_clone(env, origin, tmp_path, "ci", head), head)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK: 3/100 pull requests and 3/120 files since v0.15.0." in result.stdout


def test_budget_step_fetches_the_tags_it_decides_on(tmp_path):
    env, origin, work = _setup_budget(tmp_path)
    head = _pr_commit(env, work, "fix: many (#1)", BUDGET_OVER_FILES)
    _git(env, work, "push", "-q", "origin", "dev")
    ci = _ci_clone(env, origin, tmp_path, "ci", head)
    # v0.15.0 reaches origin only AFTER the CI clone. A step that skipped its own tag fetch would
    # see no v0.15.0, call the release a transition and pass over the limit.
    _tag(env, work, "v0.15.0")
    absent = subprocess.run(
        ["git", "-C", str(ci), "rev-parse", "-q", "--verify", "refs/tags/v0.15.0"],
        env=env,
        capture_output=True,
    )
    assert absent.returncode != 0
    result = _run_budget(env, ci, head)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "::error title=Release over budget::" in result.stdout
    assert "since v0.15.0" in result.stdout
