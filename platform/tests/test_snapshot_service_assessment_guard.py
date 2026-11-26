"""snapshot-service-assessment.yml refuses a target_service that matches no inventory group.

A play whose hosts: matches nothing exits 0 with no snapshot, and the step would record a service-assess
result over nothing. The guard is the same localhost play Verify Service Health carries
(verify-service-health.yml), reading the same target_service as the snapshot play, and it keeps the
verify tag so a verify-only run keeps it. The static test pins the order; the runtime test runs the real
playbook against an inventory that lacks the group. Requires ansible-playbook for the runtime test.
"""

import shutil
from pathlib import Path

import harness_sandbox
import playbook_yaml
import pytest

REPO = Path(__file__).resolve().parents[2]
PLAYBOOK = REPO / "platform/playbooks/snapshot-service-assessment.yml"
GUARD_NAME = "Require target_service to name a populated inventory group"

needs_ansible = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")


def _is_guard(play: dict) -> bool:
    return play.get("hosts") == "localhost" and any(
        isinstance(task, dict) and task.get("name") == GUARD_NAME for task in playbook_yaml.tasks(play.get("tasks"))
    )


def test_the_populated_group_guard_is_a_localhost_play_before_the_snapshot():
    plays = playbook_yaml.loads(PLAYBOOK.read_text())
    guards = [i for i, play in enumerate(plays) if isinstance(play, dict) and _is_guard(play)]
    snapshot = [i for i, play in enumerate(plays)
                if isinstance(play, dict) and play.get("hosts") == "{{ target_service | default('ungrouped') }}"]
    assert len(guards) == 1 and len(snapshot) == 1, (guards, snapshot)
    assert guards[0] < snapshot[0], "the guard must run before the snapshot play"

    guard = plays[guards[0]]
    assert guard.get("tags") == ["verify"], "a verify-only run must keep the guard"
    task = next(t for t in playbook_yaml.tasks(guard["tasks"]) if t.get("name") == GUARD_NAME)
    assert task["ansible.builtin.assert"]["that"] == "(groups.get(target_service | default(''), []) | length) > 0"
    assert plays[snapshot[0]].get("tags") == ["verify"]


@needs_ansible
def test_an_unpopulated_target_is_refused_before_anything_is_snapshotted(tmp_path):
    inventory = tmp_path / "inventory.ini"
    inventory.write_text("[demo_svc]\ndemo ansible_connection=local\n")
    done = harness_sandbox.run_playbook(PLAYBOOK, tmp_path, "-e", "target_service=nope_svc", inventory=inventory)
    # Without the guard, the empty play is skipped ("skipping: no hosts matched") and the run exits 0.
    assert done.returncode != 0, done.stdout[-2000:]
    assert "matches no hosts in this inventory" in done.stdout, done.stdout[-2000:]
