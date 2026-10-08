"""A playbook running from a non-main checkout may not place main on the target
(docs/MISTAKES.md 3.12).

Tasks 2954 and 2956 ran Dev-bound templates launched through the API without
service_branch: Semaphore fills survey defaults only in its web form, so the deploys fell
back to `service_branch | default('main')` and checked main out on the hosts. The guard
task runs here for real, from throwaway checkouts on each kind of branch; the catalog test
holds every Dev-bound template that reads service_branch to a survey default of dev, which
the launcher now sends when --set omits it.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import harness_sandbox
import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
TASKS = REPO / "platform/playbooks/tasks"
GUARD = TASKS / "assert-placement-branch.yml"
TEMPLATES = REPO / "platform/semaphore/templates.yml"
REFUSAL = "cross_branch_placement_refused"


def _git_env(home: Path) -> dict:
    # Git hooks export GIT_DIR and friends; inheriting them would point these commands at
    # the real repository instead of the throwaway one (MISTAKES 3.7).
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(HOME=str(home), GIT_CONFIG_NOSYSTEM="1",
               GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
    return env


def _checkout(tmp: Path, branch: str | None, detached: bool = False) -> Path:
    """A playbook directory inside a checkout of `branch`; None means no checkout at all."""
    root = tmp / "checkout"
    playbooks = root / "platform/playbooks"
    playbooks.mkdir(parents=True)
    if branch is not None:
        env = _git_env(tmp)
        for args in (["init", "-q", "-b", branch], ["commit", "-q", "--allow-empty", "-m", "c"]):
            subprocess.run(["git", *args], cwd=root, env=env, check=True, capture_output=True)
        if detached:
            subprocess.run(["git", "checkout", "-q", "--detach"], cwd=root, env=env, check=True,
                           capture_output=True)
    return playbooks


def _run(tmp: Path, checkout_branch: str | None, placement: str, *args: str,
         detached: bool = False) -> subprocess.CompletedProcess:
    playbooks = _checkout(tmp, checkout_branch, detached)
    (tmp / "inv.yml").write_text(yaml.safe_dump(
        {"all": {"hosts": {"target": {"ansible_connection": "local"}}}}))
    (playbooks / "play.yml").write_text(yaml.safe_dump([{
        "hosts": "all", "gather_facts": False, "vars": {"_branch": placement},
        "tasks": [{"ansible.builtin.include_tasks": str(GUARD)}]}]))
    return harness_sandbox.run(
        ["ansible-playbook", "-i", str(tmp / "inv.yml"), str(playbooks / "play.yml"), *args],
        tmp, cwd=REPO, env=harness_sandbox.env_for(tmp, _git_env(tmp)))


def _refused(r: subprocess.CompletedProcess) -> bool:
    out = r.stdout + r.stderr
    assert r.returncode == 0 or REFUSAL in out, out
    return r.returncode != 0


def test_a_dev_checkout_may_not_place_main(tmp_path):
    r = _run(tmp_path, "dev", "main")
    assert _refused(r)
    assert "service_branch=dev" in r.stdout + r.stderr


def test_the_refusal_holds_in_check_mode(tmp_path):
    # Task 2958 was a check-mode retry: the branch read must not be skipped under --check.
    assert _refused(_run(tmp_path, "dev", "main", "--check"))


@pytest.mark.parametrize(("checkout", "placement"), [
    ("dev", "dev"),
    ("main", "main"),
    ("main", "feat/x"),   # the documented branch deploy from a main-bound template
    ("dev", "feat/x"),
])
def test_agreeing_or_deliberate_feature_placements_pass(tmp_path, checkout, placement):
    assert not _refused(_run(tmp_path, checkout, placement))


def test_the_explicit_override_places_main_from_dev(tmp_path):
    assert not _refused(_run(tmp_path, "dev", "main", "-e", "allow_cross_branch_placement=true"))


def test_an_unreadable_branch_refuses_nothing(tmp_path):
    assert not _refused(_run(tmp_path, "dev", "main", detached=True))


def test_no_checkout_refuses_nothing(tmp_path):
    assert not _refused(_run(tmp_path, None, "main"))


def test_the_branch_is_read_on_the_controller_without_become():
    # The harness target is local too, so only the source shows WHERE the read runs: on the
    # target it would read the host's clone, and the Semaphore container has no sudo.
    read = yaml.safe_load(GUARD.read_text())[0]
    assert read["delegate_to"] == "localhost" and read["become"] is False
    assert read["check_mode"] is False and read["run_once"] is True


@pytest.mark.parametrize(("task_file", "clone_task"), [
    ("place-monorepo.yml", "Clone or update agent-cloud monorepo"),
    ("clone-and-deploy.yml", "Clone or update agent-cloud monorepo"),
])
def test_every_shared_clone_runs_the_guard_first(task_file, clone_task):
    tasks = yaml.safe_load((TASKS / task_file).read_text())
    names = [t.get("name") for t in tasks]
    guard = next(i for i, t in enumerate(tasks)
                 if t.get("ansible.builtin.include_tasks") == GUARD.name)
    assert guard < names.index(clone_task)


def _reads_service_branch(playbook: Path, seen: set[Path] | None = None) -> bool:
    seen = set() if seen is None else seen
    if playbook in seen or not playbook.exists():
        return False
    seen.add(playbook)
    text = playbook.read_text()
    # The placement read (`_branch: service_branch | default('main')`), not a mention: the
    # survey publisher names the field without reading it.
    if re.search(r"service_branch\s*\|\s*default\(", text):
        return True
    return any(_reads_service_branch((playbook.parent / m.strip()).resolve(), seen)
               for m in re.findall(r"import_playbook:\s*\"?([^\"\n]+)", text))


def _published_dev_branch_defaults():
    """(name, service_branch default as published) for every Dev-bound template whose
    playbook reads service_branch. A generated (Dev) variant takes 'dev' from the generator
    in setup-templates.yml when its base declares the field (test_scoped_publication.py)."""
    for t in yaml.safe_load(TEMPLATES.read_text())["templates"]:
        generated = bool(t.get("dev_variant"))
        if not (generated or t.get("repository") == "agent-cloud dev"):
            continue
        if not _reads_service_branch((REPO / t["playbook"]).resolve()):
            continue
        field = next((v for v in t.get("survey_vars", []) if v["name"] == "service_branch"), None)
        default = None if field is None else ("dev" if generated else field.get("default_value"))
        yield t["name"] + (" (Dev)" if generated else ""), default


def test_every_dev_bound_template_that_reads_service_branch_defaults_it_to_dev():
    found = dict(_published_dev_branch_defaults())
    assert "Deploy agentgateway (Dev)" in found and "Deploy step-ca (Dev)" in found
    assert {name: d for name, d in found.items() if d != "dev"} == {}
