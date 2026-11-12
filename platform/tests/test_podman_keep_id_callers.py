"""The four listener-TLS deploys use the one shared podman keep-id check, after placement, with their own gate (AC-702).

Each caller must include tasks/assert-podman-keep-id.yml exactly once, directly after its placement include
(tasks/place-monorepo.yml, which installs podman), with the gate the deploy's keep-id overlay needs and the
name the refusal gives. No caller may keep a hand copy of the version read.
"""

import harness_sandbox
import pytest
import yaml

PLAYBOOKS = harness_sandbox.REPO / "platform/playbooks"
PLACE = "tasks/place-monorepo.yml"
SHARED = "tasks/assert-podman-keep-id.yml"
NAME = "Refuse a podman older than 4.3 for userns keep-id"

# playbook -> (the when the include carries, the _apk_for the refusal names)
CALLERS = {
    "deploy-skynet.yml": (
        ["not _skynet_local", "_skynet_tls"],
        "Production skynet's HTTPS listener (compose.prod-tls.yml)",
    ),
    "deploy-opa.yml": ("_opa_tls", "opa_tls (compose.tls.yml)"),
    "deploy-agentgateway.yml": ("_tls and not (local_mode | default(false) | bool)", "Listener TLS (compose.tls.yml)"),
    "deploy-launch-gate.yml": ("not _launch_gate_local_mode", "Production launch gate (compose.prod.yml)"),
}


def _task_lists(playbook):
    """Every task list in the file (pre_tasks, tasks, post_tasks of every play), so an order is read within one list."""
    plays = yaml.safe_load((PLAYBOOKS / playbook).read_text(encoding="utf-8"))
    lists = []
    for play in plays:
        if isinstance(play, dict):
            lists.extend(play.get(key, []) for key in ("pre_tasks", "tasks", "post_tasks"))
    return lists


@pytest.mark.parametrize("playbook", sorted(CALLERS))
def test_the_shared_check_is_included_once_directly_after_placement(playbook):
    when, apk_for = CALLERS[playbook]
    hits = [
        (tasks, index)
        for tasks in _task_lists(playbook)
        for index, task in enumerate(tasks)
        if task.get("ansible.builtin.include_tasks") == SHARED
    ]
    assert len(hits) == 1, f"{playbook}: expected one include of {SHARED}, found {len(hits)}"
    tasks, index = hits[0]
    assert index > 0 and tasks[index - 1].get("ansible.builtin.include_tasks") == PLACE, (
        f"{playbook}: the keep-id include must directly follow the placement include"
    )
    task = tasks[index]
    assert task["name"] == NAME, task
    assert task["when"] == when, task
    assert task["vars"] == {"_apk_for": apk_for}, task


@pytest.mark.parametrize("playbook", sorted(CALLERS))
def test_no_caller_keeps_a_hand_copy_of_the_version_read(playbook):
    for tasks in _task_lists(playbook):
        for task in tasks:
            assert not str(task.get("register", "")).endswith("podman_version"), (
                f"{playbook}: a hand copy of the version read remains: {task.get('name')}"
            )
            command = task.get("ansible.builtin.command")
            argv = command.get("argv", []) if isinstance(command, dict) else []
            assert "version" not in argv, f"{playbook}: a hand-written version read remains: {task.get('name')}"
