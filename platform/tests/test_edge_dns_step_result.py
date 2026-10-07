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

import playbook_yaml
import pytest

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
    (play,) = playbook_yaml.plays(PLAYBOOK)
    return play["tasks"]


def _all_tasks(tasks=None):
    for t in _tasks() if tasks is None else tasks:
        yield t
        for key in ("block", "rescue", "always"):
            yield from _all_tasks(t.get(key) or [])


def _run(tmp_path, tf_plan, tf_verify, action, show_plan=None, show_verify=None, fails=False):
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
    _run.stdout = proc.stdout
    if fails:
        return proc
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


DRY_RUN = "Dry run: OpenTofu init, plan and show in a throwaway data directory"


def _dry_run_task():
    (task,) = [t for t in _all_tasks() if t.get("name") == DRY_RUN]
    return task


def test_both_plans_detect_changes_by_exit_code():
    plans = [t for t in _all_tasks()
             if (t.get("ansible.builtin.command") or {}).get("argv", [None, None])[:2] == ["tofu", "plan"]]
    assert len(plans) == 2
    for t in plans:
        assert "-detailed-exitcode" in t["ansible.builtin.command"]["argv"], t["name"]
    assert "tofu plan -input=false -no-color -detailed-exitcode" in _dry_run_task()["ansible.builtin.shell"]["cmd"]


def test_every_credential_bearing_tofu_command_is_no_log():
    # PR 436 Codex review: each tofu command runs with the R2 keys and the Cloudflare token in
    # its environment, so it is its own no_log task; failures surface through the visible report.
    tofu = [t for t in _all_tasks() if (t.get("ansible.builtin.command") or {}).get("argv", [None])[0] == "tofu"]
    assert len(tofu) == 6  # init, plan, show, apply, plan after apply, show
    tofu.append(_dry_run_task())  # the dry run's init, plan and show, in one throwaway root
    for t in tofu:
        assert t.get("no_log") is True, t["name"]
        assert t.get("failed_when") is False, t["name"]
        assert t["environment"] == "{{ _tf_env }}", t["name"]
    (env,) = [t for t in _all_tasks() if "_tf_env" in (t.get("ansible.builtin.set_fact") or {})]
    assert env.get("no_log") is True
    (unpack,) = [t for t in _all_tasks() if "_tf_dry_result" in (t.get("ansible.builtin.set_fact") or {})]
    assert unpack.get("no_log") is True, "the dry run's plan text and JSON carry the origin address"


def test_the_visible_report_carries_only_exit_codes_and_counts():
    (report,) = [t for t in _tasks() if "_tf_report" in (t.get("ansible.builtin.set_fact") or {})]
    assert "no_log" not in report
    assert set(report["ansible.builtin.set_fact"]["_tf_report"]) == {
        "init_rc", "plan_rc", "apply_rc", "apply_changed", "verify_rc", "show_rc", "verify_show_rc",
        "plan_changes", "verify_changes", "plan_actions", "verify_actions"}
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


# A fake tofu that writes what the real one would: init fills the data directory
# (TF_DATA_DIR, else .terraform/ in the working directory) and rewrites the lock file unless
# -lockfile=readonly; plan saves its -out file. It logs each call with the data directory.
FAKE_TOFU = """#!/bin/sh
d="${TF_DATA_DIR:-.terraform}"
echo "$1|$d|$*" >> "$FAKE_TOFU_LOG"
case "$1" in
  init)
    mkdir -p "$d/providers" && touch "$d/terraform.tfstate"
    case " $* " in *" -lockfile=readonly "*) ;; *) echo "# rewritten" >> .terraform.lock.hcl ;; esac
    exit "${FAKE_INIT_RC:-0}" ;;
  plan)
    for a in "$@"; do case "$a" in -out=*) out="${a#-out=}" ;; esac; done
    mkdir -p "$(dirname "$out")" && echo saved > "$out"
    printf '%s\\n' "$FAKE_PLAN_STDOUT"
    exit "${FAKE_PLAN_RC:-0}" ;;
  show)
    printf '%s\\n' "$FAKE_SHOW_JSON"
    exit "${FAKE_SHOW_RC:-0}" ;;
esac
"""
ROOT_FILES = {".terraform.lock.hcl": "# committed lock\n", "backend.hcl": 'bucket = "b"\n',
              "versions.tf": "terraform {}\n"}


def _tofu_run(tmp_path, check, plan_rc=0, plan_stdout="No changes.", init_rc=0, show_rc=0):
    """Run the playbook's tofu block and its report through ansible-playbook with a fake tofu."""
    bin_dir, tf_dir, log = tmp_path / "bin", tmp_path / "tf", tmp_path / "tofu.log"
    bin_dir.mkdir()
    tf_dir.mkdir()
    for name, text in ROOT_FILES.items():
        (tf_dir / name).write_text(text)
    (bin_dir / "tofu").write_text(FAKE_TOFU)
    (bin_dir / "tofu").chmod(0o755)
    (play,) = playbook_yaml.plays(PLAYBOOK)
    names = ("Build the tofu environment", "OpenTofu run", *TAIL)
    tasks = [t for t in play["tasks"] if t.get("name") in names]
    assert len(tasks) == len(names)
    for t in tasks:
        if "ansible.builtin.include_tasks" in t:
            t["ansible.builtin.include_tasks"] = str(EMIT)
    harness = [{"hosts": "localhost", "connection": "local", "gather_facts": False,
                "vars": {**play["vars"], "_tf_dir": str(tf_dir), "tofu_action": "plan", "_zone_id": "z",
                         "_caddy_ip": SECRET_ORIGIN,
                         "_cf": {"r2_access_key_id": "a", "r2_secret_access_key": "b", "api_token": "c"}},
                "tasks": tasks}]
    (tmp_path / "p.yml").write_text(json.dumps(harness))
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env.update({"PATH": f"{bin_dir}:{env['PATH']}", "ANSIBLE_NOCOLOR": "1", "ANSIBLE_SHOW_CUSTOM_STATS": "1",
                "FAKE_TOFU_LOG": str(log), "FAKE_INIT_RC": str(init_rc), "FAKE_PLAN_RC": str(plan_rc),
                "FAKE_PLAN_STDOUT": plan_stdout, "FAKE_SHOW_JSON": PLAN_JSON,
                "FAKE_SHOW_RC": str(show_rc)})
    argv = ["ansible-playbook", "-i", "localhost,", *(["--check"] if check else []), str(tmp_path / "p.yml")]
    proc = subprocess.run(argv, cwd=REPO, env=env, text=True, capture_output=True, stdin=subprocess.DEVNULL,
                          check=False)
    calls = [line.split("|") for line in log.read_text().splitlines()] if log.exists() else []
    root = {str(p.relative_to(tf_dir)): p.read_text() for p in tf_dir.rglob("*") if p.is_file()}
    return proc, calls, root, tf_dir


@needs_ansible
def test_a_dry_run_writes_nothing_into_the_tofu_root(tmp_path):
    # D10 (task 7.1): the dry run used to run `tofu init` in the tofu root, leaving .terraform/
    # (and any lock-file update) in the runner's checkout.
    proc, calls, root, tf_dir = _tofu_run(tmp_path, check=True)
    assert proc.returncode == 0, proc.stdout[-3000:]
    assert [c[0] for c in calls] == ["init", "plan", "show"]
    assert root == ROOT_FILES, "the dry run wrote into the tofu root"
    assert not (tf_dir / ".terraform").exists()
    (data_dir,) = {c[1] for c in calls}
    assert Path(data_dir).is_absolute() and not Path(data_dir).is_relative_to(tf_dir)
    assert not Path(data_dir).parent.exists(), "the throwaway root was not removed"
    assert "-lockfile=readonly" in calls[0][2].split()
    (res,) = step_results.results_in(proc.stdout.splitlines())
    assert (res["step"], res["status"], res["evidence"]) == ("edge-dns", "pass", {"plan_changes": 0})


@needs_ansible
def test_a_dry_run_with_changes_reports_counts_and_actions_only(tmp_path):
    proc, calls, root, _ = _tofu_run(tmp_path, check=True, plan_rc=2,
                                     plan_stdout="Plan: 0 to add, 1 to change, 0 to destroy.")
    (res,) = step_results.results_in(proc.stdout.splitlines())
    assert res["status"] == "fail" and res["evidence"] == {"plan_changes": 1}
    assert '"cloudflare_record.app: update"' in proc.stdout
    assert SECRET_ORIGIN not in proc.stdout and "198.51.100.9" not in proc.stdout
    assert root == ROOT_FILES


@needs_ansible
def test_a_dry_run_init_failure_fails_the_run_and_still_writes_nothing(tmp_path):
    proc, calls, root, _ = _tofu_run(tmp_path, check=True, init_rc=1)
    assert proc.returncode != 0 and "'init_rc': 1," in proc.stdout, proc.stdout[-3000:]
    assert [c[0] for c in calls] == ["init"]
    assert root == ROOT_FILES
    assert not Path(calls[0][1]).parent.exists()


@needs_ansible
def test_a_real_plan_is_unchanged(tmp_path):
    # Not a dry run: init, plan and show run as separate tasks in the tofu root's own .terraform/.
    proc, calls, root, tf_dir = _tofu_run(tmp_path, check=False)
    assert proc.returncode == 0, proc.stdout[-3000:]
    assert [(c[0], c[1]) for c in calls] == [("init", ".terraform"), ("plan", ".terraform"), ("show", ".terraform")]
    assert "-lockfile=readonly" not in calls[0][2].split()
    assert (tf_dir / ".terraform").is_dir()
    assert not (tf_dir / ".terraform/edge-dns.tfplan").exists(), "the saved plan was not removed"
    (res,) = step_results.results_in(proc.stdout.splitlines())
    assert res["status"] == "pass"


# PR 454 Codex review: a failed `tofu show` left plan_actions empty while the run still passed,
# so the resource and action evidence could silently go missing.
FAILED_SHOW = {"rc": 1, "stdout": ""}


@needs_ansible
def test_a_failed_show_of_the_plan_fails_the_run(tmp_path):
    proc = _run(tmp_path, {"rc": 0, "stdout": "No changes."}, SKIPPED, "plan", show_plan=FAILED_SHOW, fails=True)
    assert proc.returncode != 0 and "'show_rc': 1," in proc.stdout, proc.stdout[-3000:]
    assert step_results.results_in(proc.stdout.splitlines()) == []


@needs_ansible
def test_a_failed_show_of_the_post_apply_plan_fails_the_run(tmp_path):
    proc = _run(tmp_path, SKIPPED, {"rc": 0, "stdout": "No changes."}, "apply", show_verify=FAILED_SHOW, fails=True)
    assert proc.returncode != 0 and "'verify_show_rc': 1," in proc.stdout, proc.stdout[-3000:]


@needs_ansible
@pytest.mark.parametrize("check", [True, False], ids=["dry-run", "real-plan"])
def test_a_failed_tofu_show_fails_the_run_in_either_mode(tmp_path, check):
    proc, calls, _, _ = _tofu_run(tmp_path, check=check, show_rc=1)
    assert [c[0] for c in calls] == ["init", "plan", "show"]
    assert proc.returncode != 0 and "'show_rc': 1," in proc.stdout, proc.stdout[-3000:]
