"""Every run of agentgateway's deploy.sh takes the one environment in vars/agw-deploy-env.yml.

The rollback once spelled the environment out itself and left COMPOSE_OVERLAYS off, so it
recreated the gateway without compose.tls.yml: no ./certs mount, and the gateway exited with
"failed to watch configured file paths: /certs/agw-server/current/cert.pem" (Semaphore tasks
3844 and 3904; docs/MISTAKES.md 3.14). The structural guard here reads the YAML, not the text:
a task in a play that targets the gateway host group, whose shell or command runs deploy.sh,
must set `environment` to exactly "{{ _agw_deploy_env }}" and its play must load the vars
file. The behavioural half is in test_rollback_inference_route.py (the recorded environment of
both rollback recreates).
"""

import copy

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
