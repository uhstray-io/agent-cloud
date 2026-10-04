"""Apply Cloudflare Tofu records the edge-dns step (change service-deployment-workflow task
7.1, operator decision 2026-10-04: the "Cloudflare plan is zero-diff" criterion left edge-route
for its own step). The judging and recording tasks run for real through ansible-playbook with
the plan results tofu would have registered, so pass/fail and the plan_changes count are
proven without tofu or Cloudflare. Requires ansible-playbook.
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
PLAYBOOK = REPO / "platform/playbooks/apply-cloudflare-tofu.yml"
EMIT = REPO / "platform/playbooks/tasks/emit-step-result.yml"
_spec = importlib.util.spec_from_file_location(
    "step_results", REPO / "platform/workflows/service-onboarding/lib/step_results.py"
)
step_results = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(step_results)

needs_ansible = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")

SKIPPED = {"skipped": True, "changed": False}
CHANGES = "Plan: 1 to add, 2 to change, 0 to destroy.\n"
TAIL = ("Judge edge-dns on the last plan", "Record the edge-dns step result")


def _tasks():
    (play,) = yaml.safe_load(PLAYBOOK.read_text())
    return play["tasks"]


def _run(tmp_path, tf_plan, tf_verify, action):
    tail = [t for t in _tasks() if t.get("name") in TAIL]
    assert len(tail) == 2
    for t in tail:
        if "ansible.builtin.include_tasks" in t:
            t["ansible.builtin.include_tasks"] = str(EMIT)
    harness = [{"hosts": "localhost", "connection": "local", "gather_facts": False,
                "vars": {"_tf_plan": tf_plan, "_tf_verify": tf_verify, "tofu_action": action}, "tasks": tail}]
    (tmp_path / "p.yml").write_text(json.dumps(harness))
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env["ANSIBLE_NOCOLOR"] = "1"
    env["ANSIBLE_SHOW_CUSTOM_STATS"] = "1"
    proc = subprocess.run(["ansible-playbook", "-i", "localhost,", str(tmp_path / "p.yml")], cwd=REPO, env=env,
                          text=True, capture_output=True, stdin=subprocess.DEVNULL, check=False)
    found = step_results.results_in(proc.stdout.splitlines())
    assert len(found) == 1, proc.stdout[-3000:]
    return found[0]


@needs_ansible
def test_plan_only_zero_diff_passes(tmp_path):
    res = _run(tmp_path, {"rc": 0, "stdout": "No changes."}, SKIPPED, "plan")
    assert (res["step"], res["status"], res["evidence"]) == ("edge-dns", "pass", {"plan_changes": 0})


@needs_ansible
def test_plan_only_with_changes_fails_with_the_count(tmp_path):
    res = _run(tmp_path, {"rc": 2, "stdout": CHANGES}, SKIPPED, "plan")
    assert res["status"] == "fail" and res["evidence"] == {"plan_changes": 3}
    assert "not zero-diff: 3 pending" in res["error"]


@needs_ansible
def test_apply_is_judged_on_the_plan_after_it(tmp_path):
    res = _run(tmp_path, SKIPPED, {"rc": 2, "stdout": CHANGES}, "apply")
    assert res["status"] == "fail" and "after apply" in res["error"]
    res = _run(tmp_path, SKIPPED, {"rc": 0, "stdout": "No changes."}, "apply")
    assert res["status"] == "pass"


@needs_ansible
def test_changes_without_a_parsable_summary_still_fail(tmp_path):
    res = _run(tmp_path, {"rc": 2, "stdout": "garbled"}, SKIPPED, "plan")
    assert res["status"] == "fail" and res["evidence"] == {"plan_changes": None}


def test_both_plans_detect_changes_by_exit_code():
    plans = [t for t in _tasks()
             if (t.get("ansible.builtin.command") or {}).get("argv", [None, None])[:2] == ["tofu", "plan"]]
    assert len(plans) == 2
    for t in plans:
        assert "-detailed-exitcode" in t["ansible.builtin.command"]["argv"], t["name"]
        assert "not in [0, 2]" in t["failed_when"], t["name"]
        assert "no_log" not in t, t["name"]
