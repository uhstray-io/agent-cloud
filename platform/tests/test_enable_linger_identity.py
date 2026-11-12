"""enable-linger.yml refuses a forged local_mode before any linger task (AC-696).

The check covers this file's own reads, so it holds whichever playbook reaches the file. It does
not cover a caller's own raw local_mode gate: instrument-host-o11y.yml decides whether to include
this file at all from local_mode (`_ih_rootless`), and ensure-service-persistence.yml's rootful
and Docker branches read it directly (AC-726). The static half (the first task is the include) is
in test_local_mode_identity.py.

A refusal test parametrizes over forgeries.templated_forgeries (docs/MISTAKES.md 1.15). Each run is
--check and includes only enable-linger.yml, so nothing is enabled, copied or written.
"""

import json
from pathlib import Path

import forgeries
import pytest
from ansible_python import needs_ansible
from local_mode_inventories import IDENTITY_REFUSAL, IDENTITY_TASK, LOCAL_INI, PROD_INI, passed, refused, run

REPO = Path(__file__).resolve().parents[2]
LINGER = REPO / "platform/playbooks/tasks/enable-linger.yml"
NAME = IDENTITY_TASK
FIRST_LINGER_TASK = "TASK [Check whether lingering"

pytestmark = needs_ansible


def _linger(tmp_path, inventory, *args):
    # linger_user is set so the linger task names render on any host, and a run that reaches a
    # linger task fails for no reason but the guard's.
    play = (
        "- hosts: localhost\n"
        "  connection: local\n"
        "  gather_facts: false\n"
        "  vars:\n"
        "    linger_user: testuser\n"
        "  tasks:\n"
        "    - name: the linger tasks\n"
        f"      ansible.builtin.include_tasks: {json.dumps(str(LINGER))}\n")
    return run(tmp_path, inventory, play, *args, check=True)


@pytest.mark.parametrize("forge", forgeries.templated_forgeries("local_mode", honest=False, forged=True))
def test_a_forged_local_mode_is_refused_before_any_linger_task(tmp_path, forge):
    proc = _linger(tmp_path, PROD_INI, "-e", forge(tmp_path))
    refused(proc, IDENTITY_REFUSAL)
    assert f"TASK [{NAME}]" in proc.output, proc.output
    assert FIRST_LINGER_TASK not in proc.output, proc.output


def test_a_local_dev_inventory_passes_the_guard_and_skips_the_linger_tasks(tmp_path):
    passed(_linger(tmp_path, LOCAL_INI))
