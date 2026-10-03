"""The shared step-result task records one parseable result, in check mode too.

Runs a real play through ansible-playbook (ansible-core is installed in CI's test job), so
this proves the contract the collector depends on: the result is printed as JSON under
CUSTOM STATS by the repository ansible.cfg, and --check does not suppress it.
"""

import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TASK = REPO / "platform/playbooks/tasks/emit-step-result.yml"
_spec = importlib.util.spec_from_file_location(
    "step_results", REPO / "platform/workflows/service-onboarding/lib/step_results.py"
)
step_results = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(step_results)

pytestmark = pytest.mark.skipif(
    shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed"
)


def _play(tmp_path: Path, **vars_) -> Path:
    play = [
        {
            "name": "emit fixture",
            "hosts": "localhost",
            "connection": "local",
            "gather_facts": False,
            "vars": vars_,
            "tasks": [{"name": "emit", "ansible.builtin.include_tasks": str(TASK)}],
        }
    ]
    path = tmp_path / "play.yml"
    path.write_text(json.dumps(play))  # JSON is valid YAML
    return path


def _run(play: Path, *args: str) -> subprocess.CompletedProcess:
    # cwd=REPO so the repository ansible.cfg is the one Ansible loads, as on the controller.
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env["ANSIBLE_NOCOLOR"] = "1"
    return subprocess.run(
        ["ansible-playbook", "-i", "localhost,", str(play), *args],
        cwd=REPO, env=env, text=True, capture_output=True, check=False,
    )


def _stats(stdout: str) -> dict:
    """The run's custom stats, as the RUN line under CUSTOM STATS prints them."""
    line = next(x for x in stdout.splitlines() if step_results.RUN.match(x))
    return json.loads(step_results.RUN.match(line).group(1))


def _result(stdout: str) -> dict:
    # The collector's own parser, so this proves the emit -> collector contract end to end.
    found = step_results.results_in(stdout.splitlines())
    assert len(found) == 1, f"expected one step result, got {found!r}\n{stdout}"
    return found[0]


@pytest.mark.parametrize("check", [False, True])
def test_result_is_recorded_and_parseable(tmp_path, check):
    play = _play(
        tmp_path,
        step_result_step="fw-harden",
        step_result_status="pass",
        step_result_evidence={"ufw_active": True, "allows": [22, 443]},
        service_name="agentgateway",
    )
    proc = _run(play, *(["--check"] if check else []))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    result = _result(proc.stdout)
    # The compatibility key older readers know carries the same record.
    assert _stats(proc.stdout)["step_result"] == result
    assert result["schema"] == "agentcloud/step-result/v1"
    assert result["service"] == "agentgateway"
    assert result["step"] == "fw-harden"
    assert result["status"] == "pass"
    assert result["evidence"] == {"ufw_active": True, "allows": [22, 443]}
    assert result["undo"] == "none"
    assert result["check_mode"] is check


def test_fail_without_error_message_is_refused(tmp_path):
    play = _play(tmp_path, step_result_step="fw-harden", step_result_status="fail")
    proc = _run(play)
    assert proc.returncode != 0
    assert "error message when the status is fail" in proc.stdout


def test_unknown_status_is_refused(tmp_path):
    play = _play(tmp_path, step_result_step="fw-harden", step_result_status="green")
    proc = _run(play)
    assert proc.returncode != 0


def test_two_includes_in_one_run_record_both_results(tmp_path):
    play = [{"name": "emit twice", "hosts": "localhost", "connection": "local", "gather_facts": False,
             "vars": {"service_name": "dns"},
             "tasks": [{"name": "first", "ansible.builtin.include_tasks": str(TASK),
                        "vars": {"step_result_step": "provision-vm", "step_result_status": "pass"}},
                       {"name": "second", "ansible.builtin.include_tasks": str(TASK),
                        "vars": {"step_result_step": "cloud-init", "step_result_status": "fail",
                                 "step_result_error": "cloud-init: FAILED"}}]}]
    path = tmp_path / "play.yml"
    path.write_text(json.dumps(play))
    proc = _run(path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    found = step_results.results_in(proc.stdout.splitlines())
    assert [(r["step"], r["status"]) for r in found] == [("provision-vm", "pass"), ("cloud-init", "fail")]
    # The single key every older reader knows still carries a complete result: the last one.
    line = next(x for x in proc.stdout.splitlines() if step_results.RUN.match(x))
    assert json.loads(step_results.RUN.match(line).group(1))["step_result"]["step"] == "cloud-init"


# The parser against literal output lines: history written before the list existed, and after.
def _line(stats: dict) -> str:
    return "\tRUN: " + json.dumps(stats)


def test_parser_reads_output_recorded_before_the_list_existed():
    old = {"step": "fw-harden", "status": "pass"}
    assert step_results.results_in(["CUSTOM STATS: ****", _line({"step_result": old})]) == [old]


def test_parser_reads_every_result_in_the_list_and_ignores_the_duplicate_single_key():
    a, b = {"step": "provision-vm", "status": "pass"}, {"step": "cloud-init", "status": "skip"}
    assert step_results.results_in([_line({"step_result": b, "step_results": [a, b]})]) == [a, b]


def test_a_non_list_step_results_earlier_in_the_run_falls_back_to_the_single_key(tmp_path):
    # ansible-core update_custom_stats returns None, silently, when an aggregated value's type
    # differs from the one already held, so the list append is dropped. Chosen behaviour: no
    # guard in the task; the parser ignores a non-list `step_results` and recovers the result
    # from `step_result`, which the non-aggregating set_stats always writes.
    play = [{"name": "foreign stat then emit", "hosts": "localhost", "connection": "local",
             "gather_facts": False, "vars": {"service_name": "dns"},
             "tasks": [{"name": "a foreign non-list step_results",
                        "ansible.builtin.set_stats": {"data": {"step_results": {"not": "a list"}},
                                                      "per_host": False, "aggregate": True}},
                       {"name": "emit", "ansible.builtin.include_tasks": str(TASK),
                        "vars": {"step_result_step": "cloud-init", "step_result_status": "pass"}}]}]
    path = tmp_path / "play.yml"
    path.write_text(json.dumps(play))
    proc = _run(path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _stats(proc.stdout)["step_results"] == {"not": "a list"}  # the append was dropped
    assert [(r["step"], r["status"]) for r in step_results.results_in(proc.stdout.splitlines())] \
        == [("cloud-init", "pass")]
