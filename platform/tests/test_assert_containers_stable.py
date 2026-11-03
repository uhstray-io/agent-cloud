"""assert-containers-stable.yml: the container gate in Deploy Plane's verify (MISTAKES 2.34).

Three layers. The decision (the set_fact and the assert that reads it) runs against synthetic
first and second readings, one case per rule, so the rules are pinned without an engine. The whole
task then runs against a fake engine, a shell script that answers ps and inspect from files. That
checks the composition: the listing, both inspects with and without --check, the settle window, and
the exact inspect format the engine receives. Deploy Plane's Phase 4 wiring is checked structurally.

The ansible-driven tests need ansible-playbook and skip without it; the wiring test always runs.
"""

import copy
import json
import os
import shutil
import subprocess
from pathlib import Path

import playbook_yaml
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
TASK = ROOT / "platform/playbooks/tasks/assert-containers-stable.yml"
PLANE = ROOT / "platform/playbooks/deploy-plane.yml"
DECIDE = "Containers: find stopped, failed or restarting ones"
GATE = "Containers: every container is running and none is crash-looping"
WIRED = "tasks/assert-containers-stable.yml"
PROBE = "API answers through the proxy, set up and invitation-only"

needs_ansible = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")

# Stands in for the container engine. `ps` prints the names file; each `inspect` appends its argv to
# argv.log and answers with the next pass file (pass1, then pass2).
FAKE_ENGINE = """#!/bin/sh
d="$FAKE_DIR"
case "$1" in
  ps) cat "$d/names" ;;
  inspect)
    n=$(cat "$d/inspects" 2>/dev/null || echo 0)
    n=$((n + 1))
    echo "$n" > "$d/inspects"
    printf '%s\\n' "$*" >> "$d/argv.log"
    cat "$d/pass$n" ;;
  *) echo "unexpected engine call: $*" >&2; exit 2 ;;
esac
"""

FORMAT = ("{{.Name}}|{{.State.Status}}|{{.State.ExitCode}}|{{.RestartCount}}"
          "|{{index .Config.Labels \"agent-cloud.one-shot\"}}")

RUNNING = ["plane-api|running|0|0|", "plane_runner_1|running|0|0|", "plane-migrator|exited|0|0|true"]


def named(name):
    """The task with this name, from the task file (block children included), as a fresh copy."""
    found = [t for t in playbook_yaml.tasks(playbook_yaml.load(TASK)) if t.get("name") == name]
    assert len(found) == 1, f"{name!r} is not a single task in {TASK.name}"
    return copy.deepcopy(found[0])


def run_play(tmp_path, play, *, check=False, env=None):
    play_file = tmp_path / "play.yml"
    play_file.write_text(yaml.safe_dump(play, sort_keys=False))
    cmd = ["ansible-playbook", "-i", "localhost,", *(["--check"] if check else []), str(play_file)]
    return subprocess.run(
        cmd, capture_output=True, text=True, cwd=ROOT, stdin=subprocess.DEVNULL, timeout=240,
        env={"ANSIBLE_NOCOLOR": "1", "PATH": os.environ["PATH"], "ANSIBLE_LOCAL_TEMP": str(tmp_path),
             **(env or {})})


def decide(tmp_path, first, second, window=30):
    """Run the decision and the gate on synthetic readings; return what the gate concluded."""
    gate = named(GATE)
    gate.update(register="_acs_gate", ignore_errors=True)
    result = tmp_path / "result.json"
    play = [{
        "hosts": "localhost", "connection": "local", "gather_facts": False,
        "vars": {"_acs_first": {"stdout_lines": first}, "_acs_second": {"stdout_lines": second},
                 "_acs_settle_seconds": window},
        "tasks": [named(DECIDE), gate, {"ansible.builtin.copy": {"dest": str(result), "content":
                 "{{ {'problems': _acs_problems | trim, 'ok': _acs_gate is not failed} | to_json }}"}}],
    }]
    out = run_play(tmp_path, play)
    assert out.returncode == 0, out.stdout + out.stderr
    return json.loads(result.read_text())


@needs_ansible
def test_running_containers_with_steady_restart_counts_pass(tmp_path):
    assert decide(tmp_path, RUNNING, RUNNING) == {"problems": "", "ok": True}


@needs_ansible
def test_a_restart_count_that_grew_across_the_window_fails_naming_the_container(tmp_path):
    first = ["plane_runner_1|running|0|450|"]
    second = ["plane_runner_1|running|0|453|"]
    assert decide(tmp_path, first, second) == {"problems": "plane_runner_1: restarted 3 times in 30s", "ok": False}


@needs_ansible
def test_a_long_lived_container_that_exited_fails(tmp_path):
    same = ["plane-web|exited|127|0|"]
    assert decide(tmp_path, same, same) == {"problems": "plane-web: exited (exit 127)", "ok": False}


@needs_ansible
def test_a_restarting_container_fails_on_its_status(tmp_path):
    first = ["plane_runner_1|restarting|127|450|"]
    second = ["plane_runner_1|restarting|127|453|"]
    assert decide(tmp_path, first, second) == {"problems": "plane_runner_1: restarting (exit 127)", "ok": False}


@needs_ansible
def test_a_declared_one_shot_that_exited_zero_passes(tmp_path):
    same = ["plane-migrator|exited|0|0|true"]
    assert decide(tmp_path, same, same) == {"problems": "", "ok": True}


@needs_ansible
def test_a_declared_one_shot_that_exited_nonzero_fails(tmp_path):
    same = ["plane-migrator|exited|1|0|true"]
    assert decide(tmp_path, same, same) == {"problems": "plane-migrator: one-shot exited 1", "ok": False}


@needs_ansible
def test_a_container_missing_from_the_first_pass_fails(tmp_path):
    first = ["plane-api|running|0|0|"]
    second = ["plane-api|running|0|0|", "plane-web|running|0|0|"]
    assert decide(tmp_path, first, second) == {"problems": "plane-web: not read in the first pass", "ok": False}


@needs_ansible
def test_every_problem_is_reported_in_read_order(tmp_path):
    first = ["plane-web|running|0|0|", "plane_runner_1|running|0|450|"]
    second = ["plane-web|exited|1|0|", "plane_runner_1|running|0|453|", "plane-migrator|exited|2|0|true"]
    assert decide(tmp_path, first, second) == {
        "problems": "plane-web: exited (exit 1); plane_runner_1: restarted 3 times in 30s; "
                    "plane-migrator: one-shot exited 2",
        "ok": False,
    }


def engine_run(tmp_path, *, names, pass1, pass2, check=False):
    """Run the whole task against the fake engine; return the process and the engine's directory."""
    fake = tmp_path / "engine"
    fake.mkdir()
    engine = fake / "fake-engine"
    engine.write_text(FAKE_ENGINE)
    engine.chmod(0o755)
    # An empty list writes an empty file, so the engine prints nothing, as a real one does.
    for name, lines in (("names", names), ("pass1", pass1), ("pass2", pass2)):
        (fake / name).write_text("".join(line + "\n" for line in lines))
    play = [{
        "hosts": "localhost", "connection": "local", "gather_facts": False,
        "vars": {"container_engine": str(engine), "monorepo_deploy_path": "services/plane/deployment",
                 "local_monorepo_dir": str(tmp_path), "_acs_settle_seconds": 1},
        "tasks": [{"ansible.builtin.include_tasks": str(TASK)}],
    }]
    out = run_play(tmp_path, play, check=check, env={"FAKE_DIR": str(fake)})
    return out, fake


PLANE_NAMES = ["plane-api", "plane_runner_1", "plane-migrator"]


@needs_ansible
@pytest.mark.parametrize("check", [False, True])
def test_a_stable_plane_passes_and_both_passes_read_even_under_check(tmp_path, check):
    out, fake = engine_run(tmp_path, names=PLANE_NAMES, pass1=RUNNING, pass2=RUNNING, check=check)
    assert out.returncode == 0, out.stdout + out.stderr
    assert (fake / "inspects").read_text().strip() == "2"
    assert "3 containers stable over 1s" in out.stdout


@needs_ansible
@pytest.mark.parametrize("check", [False, True])
def test_a_crash_loop_fails_the_run_and_names_the_container(tmp_path, check):
    pass1 = ["plane-api|running|0|0|", "plane_runner_1|running|0|450|"]
    pass2 = ["plane-api|running|0|0|", "plane_runner_1|running|0|453|"]
    out, _ = engine_run(tmp_path, names=PLANE_NAMES[:2], pass1=pass1, pass2=pass2, check=check)
    assert out.returncode != 0
    assert "plane_runner_1: restarted 3 times in 1s" in out.stdout + out.stderr


@needs_ansible
def test_an_empty_listing_fails_before_any_inspect(tmp_path):
    out, fake = engine_run(tmp_path, names=[], pass1=[], pass2=[])
    assert out.returncode != 0
    assert "listing failed or was empty" in out.stdout + out.stderr
    assert not (fake / "inspects").exists()


@needs_ansible
def test_the_engine_is_asked_for_state_counts_and_the_one_shot_label_only(tmp_path):
    out, fake = engine_run(tmp_path, names=PLANE_NAMES, pass1=RUNNING, pass2=RUNNING)
    assert out.returncode == 0, out.stdout + out.stderr
    calls = (fake / "argv.log").read_text().splitlines()
    assert len(calls) == 2
    for line in calls:
        assert line == "inspect --format " + FORMAT + " plane-api plane_runner_1 plane-migrator"


def test_the_inspects_are_reads_that_run_under_check_mode():
    reads = [t for t in playbook_yaml.tasks(playbook_yaml.load(TASK)) if "ansible.builtin.command" in t]
    assert len(reads) == 2
    for task in reads:
        assert task.get("check_mode") is False, task.get("name")
        assert task.get("changed_when") is False, task.get("name")


def test_deploy_plane_runs_the_gate_after_the_api_probe_and_before_report():
    play = next(p for p in playbook_yaml.plays(PLANE) if p.get("name") == "Phase 4: Verify plane")
    steps = list(playbook_yaml.tasks(play["tasks"]))

    def at(pred):
        hits = [i for i, t in enumerate(steps) if pred(t)]
        assert len(hits) == 1, f"expected one matching step in Phase 4, found {len(hits)}"
        return hits[0]

    probe = at(lambda t: t.get("name") == PROBE)
    gate = at(lambda t: t.get("ansible.builtin.include_tasks") == WIRED)
    report = at(lambda t: t.get("name") == "Report")
    assert probe < gate < report
    assert "when" not in steps[gate], "the container gate must not be skippable"
