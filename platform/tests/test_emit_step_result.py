"""The shared step-result task records one parseable result, in check mode too.

Runs a real play through ansible-playbook (ansible-core is installed in CI's test job), so
this proves the contract the collector depends on: the result is printed as JSON under
CUSTOM STATS by the repository ansible.cfg, and --check does not suppress it.
"""

import importlib.util
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import forgeries
import playbook_yaml
import pytest
import yaml

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


# An extra var outranks the include vars every executor passes (ansible-core variable
# precedence: 21 include params, 22 extra vars), so `-e step_result_status=pass` on a launch
# would record a failed step as passed. The task refuses every input it reads when it arrives
# that way; the run then records no result at all, which the collector counts as a failure.
GUARDED = ["step_result_step", "step_result_status", "step_result_evidence", "step_result_error",
           "step_result_service", "step_result_undo", "_step_result_record"]
FORGERIES = {
    "step_result_step": "dns",
    "step_result_status": "pass",
    "step_result_evidence": {"ufw_active": True},
    "step_result_error": "",
    "step_result_service": "authentik",
    "step_result_undo": "none",
    "_step_result_record": {"schema": "agentcloud/step-result/v1", "step": "fw-harden",
                            "status": "pass", "evidence": {}, "check_mode": False},
}
# The caller's honest value for each input: what the plain forgery replaces and what the
# templated forgeries (forgeries.py) show wherever a check could look.
HONEST = {"step_result_step": "fw-harden", "step_result_status": "fail",
          "step_result_evidence": {"ufw_active": False}, "step_result_error": "ufw is inactive",
          "step_result_service": "dns", "step_result_undo": "none", "_step_result_record": None}


def _failing_executor(tmp_path: Path, hosts: str = "localhost", **include_kw) -> Path:
    """An executor whose step failed, recording that through the shared task."""
    include = {"name": "Record the step result", "ansible.builtin.include_tasks": str(TASK),
               "vars": {"step_result_step": "fw-harden", "step_result_status": "fail",
                        "step_result_evidence": {"ufw_active": False},
                        "step_result_error": "ufw is inactive"}}
    include.update(include_kw)
    play = [{"name": "failing executor", "hosts": hosts, "connection": "local",
             "gather_facts": False, "vars": {"service_name": "dns"}, "tasks": [include]}]
    path = tmp_path / "play.yml"
    path.write_text(json.dumps(play))
    return path


def _refused(proc, name: str) -> None:
    assert proc.returncode != 0, proc.stdout
    assert f"Refusing to record a step result: {name} is defined outside the executor" in proc.stdout, proc.stdout
    assert "reserved for emit-step-result's include vars" in proc.stdout, proc.stdout
    assert step_results.results_in(proc.stdout.splitlines()) == []


# Every input, forged plainly and as a context-keyed and a stateful template, in a run and
# under --check (docs/MISTAKES.md 1.15).
@pytest.mark.parametrize("check", [False, True])
@pytest.mark.parametrize("name,forge", [
    pytest.param(name, f.values[0], id=f"{name}-{f.id}")
    for name in GUARDED for f in forgeries.templated_forgeries(name, HONEST[name], FORGERIES[name])
])
def test_an_input_set_as_an_extra_var_is_refused_and_records_nothing(tmp_path, name, forge, check):
    play = _failing_executor(tmp_path)
    _refused(_run(play, "-e", forge(tmp_path), *(["--check"] if check else [])), name)


def test_the_review_template_is_refused(tmp_path):
    # The exact extra var from the PR #458 review: the old probe saw its marker, the record "pass".
    play = _failing_executor(tmp_path)
    _refused(_run(play, "-e", json.dumps(
        {"step_result_status": "{{ '__extra_var_probe__' if _sr_input is defined else 'pass' }}"})),
        "step_result_status")


@pytest.mark.parametrize("override", [{"inventory_hostname": "elsewhere"},
                                      {"hostvars": {"localhost": {}}}])
@pytest.mark.parametrize("forge", forgeries.templated_forgeries("step_result_status", "fail", "pass"))
def test_redirecting_the_lookup_does_not_let_a_forgery_through(tmp_path, override, forge):
    play = _failing_executor(tmp_path)
    proc = _run(play, "-e", json.dumps(override), "-e", forge(tmp_path))
    assert proc.returncode != 0, proc.stdout
    assert step_results.results_in(proc.stdout.splitlines()) == []


def test_every_host_of_a_multi_host_play_is_refused(tmp_path):
    # The refusal is run_once; its failure must still stop every host before the set_stats.
    inv = tmp_path / "inv.ini"
    inv.write_text("[grp]\na ansible_connection=local\nb ansible_connection=local\n")
    play = _failing_executor(tmp_path, hosts="grp")
    proc = _run(play, "-i", str(inv), "-e", "step_result_status=pass")
    assert proc.returncode != 0, proc.stdout
    assert step_results.results_in(proc.stdout.splitlines()) == []


def test_hostvars_holds_an_extra_var_but_not_the_include_vars_a_caller_passes(tmp_path):
    # The mechanism the refusal relies on.
    probe = ("{{ ['step_result_status', 'step_result_step'] | select('in', hostvars[inventory_hostname])"
             " | list | to_json }}")
    play = [{"hosts": "localhost", "connection": "local", "gather_facts": False,
             "tasks": [{"ansible.builtin.debug": {"msg": "SEEN " + probe},
                        "vars": {"step_result_step": "fw-harden", "step_result_status": "fail"}}]}]
    path = tmp_path / "play.yml"
    path.write_text(json.dumps(play))
    assert '"msg": "SEEN []"' in _run(path).stdout
    assert '"msg": "SEEN [\\"step_result_status\\"]"' in _run(path, "-e", "step_result_status=pass").stdout


def test_a_tag_filtered_caller_is_refused_too(tmp_path):
    play = _failing_executor(
        tmp_path,
        **{"ansible.builtin.include_tasks": {"file": str(TASK), "apply": {"tags": ["verify"]}},
           "tags": ["verify"]})
    _refused(_run(play, "--tags", "verify", "-e", "step_result_status=pass"), "step_result_status")


def test_extra_vars_the_task_does_not_own_are_still_accepted(tmp_path):
    # workflow_id is set by the orchestrator as an extra var by design; service_name is the
    # caller's, read only as the service default.
    play = _failing_executor(tmp_path)
    proc = _run(play, "-e", "workflow_id=wf-1", "-e", "service_name=authentik")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    result = _result(proc.stdout)
    assert (result["workflow_id"], result["service"], result["status"]) == ("wf-1", "authentik", "fail")


def _guarded_names() -> list[str]:
    refusal = next(t for t in yaml.safe_load(TASK.read_text())
                   if t.get("name") == "Step result: refuse inputs set as extra vars")
    return re.findall(r"'([A-Za-z_]+)'", refusal["ansible.builtin.assert"]["that"][0].split("|")[0])


def test_every_input_the_task_reads_is_guarded():
    # A new input added to the task without the refusal would be forgeable again.
    guarded = _guarded_names()
    body = TASK.read_text().split("\n- name:", 1)[1]  # the tasks, not the header comment
    # (?<![&*]): not the YAML anchor the two set_stats tasks share; (?<!'): not the list itself
    read = set(re.findall(r"(?<![&*'])\b(step_result_[a-z_]+|_step_result_record)\b", body))
    assert read <= set(guarded), f"read but not guarded: {read - set(guarded)}"
    assert set(guarded) == set(GUARDED)


def _emit_includes(tasks):
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        for key in ("block", "rescue", "always"):
            yield from _emit_includes(task.get(key))
        include = task.get("ansible.builtin.include_tasks") or ""
        include = include.get("file", "") if isinstance(include, dict) else include
        if include.endswith("emit-step-result.yml"):
            yield task


def test_every_caller_passes_only_guarded_inputs():
    # Ratchet over the callers: each records through the guarded task, and passes no input
    # name the refusal does not cover.
    callers = 0
    for path in sorted((REPO / "platform/playbooks").glob("*.yml")):
        doc = yaml.safe_load(path.read_text())
        for play in doc if isinstance(doc, list) else []:
            if not isinstance(play, dict):
                continue
            for key in ("pre_tasks", "tasks", "post_tasks", "handlers"):
                for task in _emit_includes(play.get(key)):
                    callers += 1
                    unguarded = set(task.get("vars", {})) - set(GUARDED)
                    assert not unguarded, f"{path.name}: unguarded emit input(s) {unguarded}"
    assert callers >= 17, callers


# The names are RESERVED: the refusal reads hostvars, which also holds inventory vars, facts
# and registered vars, so an honest definition of one there would be refused like a forgery.
def _keys(node):
    if isinstance(node, dict):
        for k, v in node.items():
            yield k
            yield from _keys(v)
    elif isinstance(node, list):
        for v in node:
            yield from _keys(v)


def test_no_playbook_or_task_sets_or_registers_a_reserved_name():
    offenders = []
    for path in playbook_yaml.files():
        for task in playbook_yaml.tasks(playbook_yaml.load(path)):
            names = set(task.get("ansible.builtin.set_fact") or task.get("set_fact") or {})
            names |= {task.get("register")}
            offenders += [f"{path.relative_to(REPO)}: {n}" for n in names & set(GUARDED)]
    assert offenders == []


def test_no_example_inventory_defines_a_reserved_name():
    found = []
    for path in sorted((REPO / "platform/inventory").glob("*")):
        if path.suffix in (".yml", ".yaml", ".example"):
            found += [f"{path.name}: {k}" for k in _keys(yaml.safe_load(path.read_text())) if k in GUARDED]
    assert found == []


def test_an_inventory_var_of_a_reserved_name_is_refused_with_the_rule(tmp_path):
    # A site-config collision is refused too, and the message says why.
    inv = tmp_path / "inv.yml"
    inv.write_text(yaml.safe_dump({"all": {"hosts": {"localhost": {"ansible_connection": "local",
                                                                   "step_result_undo": "none"}}}}))
    play = _failing_executor(tmp_path)
    proc = _run(play, "-i", str(inv))
    _refused(proc, "step_result_undo")
    assert "Rename the colliding variable" in proc.stdout
