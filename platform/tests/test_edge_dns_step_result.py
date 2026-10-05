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
TAIL = ("Summarise the tofu run (exit codes and change counts only)",
        "Show the tofu result for action {{ tofu_action }}",
        "Fail on a tofu error (details withheld: the commands carry credentials)",
        "Judge edge-dns on the last plan", "Record the edge-dns step result")


def _tasks():
    (play,) = yaml.safe_load(PLAYBOOK.read_text())
    return play["tasks"]


def _all_tasks(tasks=None):
    for t in _tasks() if tasks is None else tasks:
        yield t
        for key in ("block", "rescue", "always"):
            yield from _all_tasks(t.get(key) or [])


def _run(tmp_path, tf_plan, tf_verify, action, show_plan=None, show_verify=None):
    tail = [t for t in _tasks() if t.get("name") in TAIL]
    assert len(tail) == len(TAIL)
    for t in tail:
        if "ansible.builtin.include_tasks" in t:
            t["ansible.builtin.include_tasks"] = str(EMIT)
    harness = [{"hosts": "localhost", "connection": "local", "gather_facts": False,
                "vars": {"_tf_init": {"rc": 0}, "_tf_plan": tf_plan, "_tf_verify": tf_verify,
                         "_tf_apply": SKIPPED if action == "plan" else {"rc": 0, "changed": True},
                         "_tf_show_plan": show_plan or SKIPPED, "_tf_show_verify": show_verify or SKIPPED,
                         "tofu_action": action},
                "tasks": tail}]
    (tmp_path / "p.yml").write_text(json.dumps(harness))
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env["ANSIBLE_NOCOLOR"] = "1"
    env["ANSIBLE_SHOW_CUSTOM_STATS"] = "1"
    proc = subprocess.run(["ansible-playbook", "-i", "localhost,", str(tmp_path / "p.yml")], cwd=REPO, env=env,
                          text=True, capture_output=True, stdin=subprocess.DEVNULL, check=False)
    found = step_results.results_in(proc.stdout.splitlines())
    assert len(found) == 1, proc.stdout[-3000:]
    _run.stdout = proc.stdout
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
    plans = [t for t in _all_tasks()
             if (t.get("ansible.builtin.command") or {}).get("argv", [None, None])[:2] == ["tofu", "plan"]]
    assert len(plans) == 2
    for t in plans:
        assert "-detailed-exitcode" in t["ansible.builtin.command"]["argv"], t["name"]


def test_every_credential_bearing_tofu_command_is_no_log():
    # PR 436 Codex review: each tofu command runs with the R2 keys and the Cloudflare token in
    # its environment, so it is its own no_log task; failures surface through the visible report.
    tofu = [t for t in _all_tasks() if (t.get("ansible.builtin.command") or {}).get("argv", [None])[0] == "tofu"]
    assert len(tofu) == 6  # init, plan, show, apply, plan after apply, show
    for t in tofu:
        assert t.get("no_log") is True, t["name"]
        assert t.get("failed_when") is False, t["name"]
        assert t["environment"] == "{{ _tf_env }}", t["name"]
    (env,) = [t for t in _all_tasks() if "_tf_env" in (t.get("ansible.builtin.set_fact") or {})]
    assert env.get("no_log") is True


def test_the_visible_report_carries_only_exit_codes_and_counts():
    (report,) = [t for t in _tasks() if "_tf_report" in (t.get("ansible.builtin.set_fact") or {})]
    assert "no_log" not in report
    assert set(report["ansible.builtin.set_fact"]["_tf_report"]) == {
        "init_rc", "plan_rc", "apply_rc", "apply_changed", "verify_rc", "plan_changes", "verify_changes",
        "plan_actions", "verify_actions"}
    for t in _tasks():
        if "ansible.builtin.debug" in t:
            shown = str(t["ansible.builtin.debug"])
            protected = ("_tf_plan", "_tf_apply", "_tf_verify", "_tf_init", "_tf_env", "_tf_show", "_cf")
            assert not [p for p in protected if p in shown], t["name"]


# A `tofu show -json` plan carrying values that must never reach the visible report.
SECRET_ORIGIN = "203.0.113.77"
PLAN_JSON = json.dumps({"format_version": "1.2", "resource_changes": [
    {"address": "cloudflare_record.app", "change": {"actions": ["update"],
     "before": {"content": "198.51.100.9"}, "after": {"content": SECRET_ORIGIN}}},
    {"address": "cloudflare_ruleset.waf", "change": {"actions": ["delete", "create"],
     "before": {"rules": ["x"]}, "after": {"rules": ["y"]}}},
    {"address": "cloudflare_record.unchanged", "change": {"actions": ["no-op"],
     "before": {"content": SECRET_ORIGIN}, "after": {"content": SECRET_ORIGIN}}},
]})


@needs_ansible
def test_the_report_lists_address_and_action_only(tmp_path):
    res = _run(tmp_path, {"rc": 2, "stdout": "Plan: 0 to add, 1 to change, 0 to destroy.\n"}, SKIPPED, "plan",
               show_plan={"rc": 0, "stdout": PLAN_JSON})
    assert res["status"] == "fail"
    out = _run.stdout
    assert '"cloudflare_record.app: update"' in out and '"cloudflare_ruleset.waf: delete/create"' in out
    assert "cloudflare_record.unchanged" not in out, "a no-op resource was listed"
    for leaked in (SECRET_ORIGIN, "198.51.100.9", "content", "before", "after"):
        assert leaked not in out, f"the visible output carries {leaked!r}"


@needs_ansible
def test_the_post_apply_plan_is_listed_the_same_way(tmp_path):
    res = _run(tmp_path, SKIPPED, {"rc": 2, "stdout": "Plan: 0 to add, 1 to change, 0 to destroy.\n"}, "apply",
               show_verify={"rc": 0, "stdout": PLAN_JSON})
    assert res["status"] == "fail"
    assert '"cloudflare_record.app: update"' in _run.stdout and SECRET_ORIGIN not in _run.stdout


def test_saved_plans_are_removed_and_shown_inside_the_no_log_boundary():
    (block,) = [t for t in _tasks() if t.get("name") == "OpenTofu run"]
    shows = [t for t in block["block"] if (t.get("ansible.builtin.command") or {}).get("argv", [None, None])[:2]
             == ["tofu", "show"]]
    assert len(shows) == 2 and all(t.get("no_log") is True for t in shows)
    (cleanup,) = block["always"]
    assert cleanup["ansible.builtin.file"]["state"] == "absent"
    assert set(cleanup["loop"]) == {"{{ _tf_planfile }}", "{{ _tf_verify_planfile }}"}
