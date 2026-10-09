"""Every run of agentgateway's deploy.sh takes the one environment in vars/agw-deploy-env.yml.

The rollback once spelled the environment out itself and left COMPOSE_OVERLAYS off, so it
recreated the gateway without compose.tls.yml: no ./certs mount, and the gateway exited with
"failed to watch configured file paths: /certs/agw-server/current/cert.pem" (Semaphore tasks
3844 and 3904; docs/MISTAKES.md 3.14). The structural guard here reads the YAML, not the text:
a task in a play that targets the gateway host group, whose shell or command runs deploy.sh,
must set `environment` to exactly "{{ _agw_deploy_env }}" and its play must load the vars
file. The behavioural half is in test_rollback_inference_route.py (the recorded environment of
both rollback recreates).

The structural guard cannot see a caller in an included task file, in a play whose `hosts:` is a
variable, or one that uses `ansible.builtin.script`. The text backstop covers them: every `*.yml`
under platform/playbooks (tasks/ and vars/ included) that runs a deploy.sh and is recognisably the
gateway's is counted, and the (file, count) set must equal KNOWN, so a new caller anywhere fails
until it is added to KNOWN, where the structural check then covers it.
"""

import copy
import re
from pathlib import Path

import playbook_yaml
import pytest
from jinja2 import Environment

REPO = playbook_yaml.REPO
VARS_FILE = REPO / "platform/playbooks/vars/agw-deploy-env.yml"
VARS_FILE_REF = "vars/agw-deploy-env.yml"
ENV_REF = "{{ _agw_deploy_env }}"
GROUP = "agentgateway_svc"
# The playbooks known to run the gateway's deploy.sh; a refactor that moves a call out of every
# one of them must update this on purpose, not leave the guard scanning nothing.
KNOWN = {
    "platform/playbooks/deploy-agentgateway.yml": 2,  # --pull-only and --no-pull
    "platform/playbooks/rollback-inference-route.yml": 2,  # the put-back and the restore
    "platform/playbooks/verify-agentgateway-runtime.yml": 1,  # --verify-only
}


PLAYBOOKS = REPO / "platform/playbooks"
# A line that RUNS a deploy.sh (not one that names it in a task title or a comment).
RUNS = re.compile(r"(?:\b(?:bash|sh|source|exec)[ ,\]]+|^\s*-?\s*|script:\s*)(?:\./)?(?:\S*/)?deploy\.sh\b")
# What makes a file the gateway's: its name, a play that targets the gateway host group, or a
# reference to the gateway's deployment directory. Other services' playbooks run an identical
# `bash deploy.sh`; the text alone cannot tell them apart, these markers can.
GATEWAY_NAME = re.compile(r"agentgateway|agw")
GATEWAY_HOSTS = re.compile(r"^\s*-?\s*hosts:.*" + GROUP, re.M)
GATEWAY_DIR = re.compile(r"^[^#\n]*agentgateway/deployment", re.M)


def _text_callers(root: Path, base: Path) -> dict[str, int]:
    """{path relative to base: deploy.sh runs} for the gateway's files under root."""
    found: dict[str, int] = {}
    for path in sorted(root.rglob("*.yml")):
        text = path.read_text()
        runs = sum(1 for line in text.splitlines() if not line.lstrip().startswith("#") and RUNS.search(line))
        if not runs:
            continue
        if GATEWAY_NAME.search(path.name) or GATEWAY_HOSTS.search(text) or GATEWAY_DIR.search(text):
            found[str(path.relative_to(base))] = runs
    return found


def _runs_deploy_sh(task) -> bool:
    for module in ("ansible.builtin.shell", "ansible.builtin.command", "shell", "command"):
        value = task.get(module)
        if value is None:
            continue
        if isinstance(value, dict):
            value = [value.get("cmd", ""), *(value.get("argv") or [])]
        if "deploy.sh" in " ".join(str(v) for v in playbook_yaml.strings(value)):
            return True
    return False


def _gateway_deploy_tasks():
    """(relative path, play, task) for every deploy.sh run in a play that targets the gateway."""
    found = []
    for path in playbook_yaml.files():
        doc = playbook_yaml.load(path)
        if not isinstance(doc, list):
            continue
        for play in doc:
            if not isinstance(play, dict) or GROUP not in str(play.get("hosts", "")):
                continue
            for task in playbook_yaml.tasks(play.get("tasks")):
                if _runs_deploy_sh(task):
                    found.append((str(path.relative_to(REPO)), play, task))
    return found


def test_the_known_gateway_deploy_sh_callers_are_all_found():
    counts: dict[str, int] = {}
    for rel, _play, _task in _gateway_deploy_tasks():
        counts[rel] = counts.get(rel, 0) + 1
    for rel, expected in KNOWN.items():
        assert counts.get(rel) == expected, f"{rel}: expected {expected} deploy.sh task(s), found {counts.get(rel)}"


def test_no_gateway_deploy_sh_caller_exists_outside_the_known_set():
    """The backstop: a new caller in any file under platform/playbooks, structural guard blind
    spots included, changes this set and fails until it is added to KNOWN."""
    assert _text_callers(PLAYBOOKS, REPO) == KNOWN


def test_the_text_backstop_finds_callers_in_included_files_variable_hosts_and_scripts(tmp_path):
    """The backstop itself, on synthetic playbooks: each way a caller can hide is counted, other
    services' deploy.sh runs and mere mentions are not."""
    root = tmp_path
    (root / "tasks").mkdir()
    cases = {
        "tasks/agw-fake-restart.yml": "- shell: |\n    cd /x\n    bash deploy.sh --no-pull\n",  # name
        "uses-hosts.yml": "- hosts: agentgateway_svc\n  tasks:\n    - command:\n        argv: [bash, deploy.sh]\n",
        "variable-hosts.yml": (
            '- hosts: "{{ t }}"\n  tasks:\n    - shell: cd services/agentgateway/deployment && bash deploy.sh\n'
        ),
        "tasks/runs-script.yml": "- hosts: agentgateway_svc\n  tasks:\n    - ansible.builtin.script: ./deploy.sh --x\n",
        "deploy-other.yml": "- hosts: other_svc\n  tasks:\n    - shell: bash deploy.sh\n",
        "comment-only.yml": (
            "- hosts: agentgateway_svc\n  tasks:\n    # bash deploy.sh\n    - name: Show deploy.sh output\n"
        ),
    }
    for name, text in cases.items():
        (root / name).write_text(text)
    assert _text_callers(root, root) == {
        "tasks/agw-fake-restart.yml": 1,
        "uses-hosts.yml": 1,
        "variable-hosts.yml": 1,
        "tasks/runs-script.yml": 1,
    }


def _violations(rel, play, task) -> list[str]:
    found = []
    if task.get("environment") != ENV_REF:
        found.append(f"{rel}: '{task.get('name')}' sets environment={task.get('environment')!r}")
    if VARS_FILE_REF not in (play.get("vars_files") or []):
        found.append(f"{rel}: play '{play.get('name')}' does not load {VARS_FILE_REF}")
    return found


def test_every_gateway_deploy_sh_run_uses_the_one_shared_environment():
    offenders = [v for rel, play, task in _gateway_deploy_tasks() for v in _violations(rel, play, task)]
    assert not offenders, "\n".join(offenders)


def test_the_guard_flags_an_environment_spelled_out_in_the_task_and_a_play_without_the_vars_file():
    """The guard itself: copies of a real caller, one with the environment inlined and one in a
    play that does not load the vars file, are offenders; the real one is not."""
    rel, play, task = next(
        t for t in _gateway_deploy_tasks() if t[0] == "platform/playbooks/rollback-inference-route.yml"
    )
    assert _violations(rel, play, task) == []
    inlined = copy.deepcopy(task)
    inlined["environment"] = {"CONTAINER_ENGINE": "{{ _engine }}", "LOCAL_MODE": ""}
    assert len(_violations(rel, play, inlined)) == 1
    unloaded = {k: v for k, v in play.items() if k != "vars_files"}
    assert len(_violations(rel, unloaded, task)) == 1


@pytest.mark.parametrize(("tls", "local", "overlay"), [
    (False, False, ""),
    (True, False, "compose.tls.yml"),
    (False, True, ""),
    (True, True, ""),
])
def test_the_shared_environment_selects_the_tls_overlay_only_outside_local_dev(tls, local, overlay):
    env_vars = playbook_yaml.load(VARS_FILE)["_agw_deploy_env"]
    assert set(env_vars) == {"CONTAINER_ENGINE", "LOCAL_MODE", "COMPOSE_CMD", "COMPOSE_OVERLAYS"}
    jinja = Environment()
    jinja.filters["bool"] = bool
    rendered = jinja.from_string(env_vars["COMPOSE_OVERLAYS"]).render(agw_listener_tls=tls, local_mode=local)
    assert rendered.strip() == overlay
