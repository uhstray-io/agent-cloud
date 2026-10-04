"""manage-caddy-sites.yml refuses a target group that matches no hosts, and records the refusal
as a failed edge-route step result (D10 review 2026-10-03). Without the guard, a play over no
hosts exits 0 with no result, which reads as a pass.

The absent-group runs execute the whole playbook: the guard fails it before any Caddy task.
The populated, host-name and compound-pattern runs execute only the guard play, staged in
tmp_path, so no Caddy host is needed.
"""

import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
PLAYBOOK = REPO / "platform/playbooks/manage-caddy-sites.yml"
_spec = importlib.util.spec_from_file_location(
    "step_results", REPO / "platform/workflows/service-onboarding/lib/step_results.py"
)
step_results = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(step_results)

pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")


def _run(tmp_path, playbook, target, *extra):
    inv = {"all": {"children": {"caddy_svc": {"hosts": {"caddy": {"ansible_connection": "local"}}}}}}
    (tmp_path / "inv.yml").write_text(json.dumps(inv))
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env["ANSIBLE_NOCOLOR"] = "1"
    proc = subprocess.run(
        ["ansible-playbook", "-i", str(tmp_path / "inv.yml"), str(playbook), "-e", f"target_service={target}", *extra],
        cwd=REPO, env=env, text=True, capture_output=True, stdin=subprocess.DEVNULL, check=False,
    )
    return proc, step_results.results_in(proc.stdout.splitlines())


def _guard_only(tmp_path):
    # Staged in tmp_path, never in the repo tree: other tests scan platform/playbooks/ while
    # this runs under xdist. The include is made absolute so it resolves from here.
    guard = yaml.safe_load(PLAYBOOK.read_text())[0]
    assert guard["name"] == "Refuse a target group that matches no hosts"
    for task in guard["tasks"]:
        if str(task.get("ansible.builtin.include_tasks", "")).endswith("emit-step-result.yml"):
            task["ansible.builtin.include_tasks"] = str(PLAYBOOK.parent / "tasks/emit-step-result.yml")
    path = tmp_path / "guard.yml"
    path.write_text(json.dumps([guard]))
    return path


@pytest.mark.parametrize("check", [False, True])
def test_an_absent_target_group_is_a_recorded_failure(tmp_path, check):
    proc, found = _run(tmp_path, PLAYBOOK, "no_such_svc", *(["--check"] if check else []))
    assert proc.returncode != 0, proc.stdout[-3000:]
    (res,) = found
    assert (res["step"], res["status"], res["check_mode"]) == ("edge-route", "fail", check)
    assert "matches no hosts in this inventory" in res["error"]


@pytest.mark.parametrize("target", ["caddy_svc", "caddy", "caddy_svc:!caddy_svc"])
def test_a_populated_group_a_host_or_a_compound_pattern_passes_the_guard(tmp_path, target):
    # "caddy" is a HOST name: a valid hosts: pattern that is not a key of `groups`.
    # caddy_svc:!caddy_svc is rollback-inference-route.yml's deliberate skip in gateway-config mode.
    proc, found = _run(tmp_path, _guard_only(tmp_path), target)
    assert proc.returncode == 0, proc.stdout[-3000:]
    assert found == []
