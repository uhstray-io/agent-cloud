"""Synthetic inventories and the one runner for the local_mode identity tests (AC-696).

test_local_mode_identity.py and test_bao_transport_identity.py run a real ansible-playbook on these
inventories. Each names localhost and, for local-dev, one `<name>-local`
host, so the identity filter (filter_plugins/local_dev.py) decides the mode, not the test. Every run
goes through harness_sandbox.run_playbook: bounded, no stdin, writes confined.

Imported as `import local_mode_inventories`, like forgeries: pytest puts this directory on sys.path.
"""

import json
import re
from pathlib import Path

import harness_sandbox

# The message of the shared identity guard (tasks/assert-local-mode-identity.yml).
IDENTITY_REFUSAL = "The mode follows the inventory, not the variable"
# The task name the check is included under (place-monorepo.yml, enable-linger.yml, and Phase 1 of
# deploy-skynet.yml and deploy-launch-gate.yml).
IDENTITY_TASK = "Refuse a local_mode that disagrees with the inventory's identity"

LOCAL_INI = """\
localhost ansible_connection=local
dns-local ansible_connection=local

[all:vars]
local_mode=true
"""
PROD_INI = """\
localhost ansible_connection=local
dns-prod ansible_connection=local
"""
EXAMPLE_YAML = """\
all:
  vars:
    local_mode: true
  hosts:
    dns-local:
      ansible_connection: local
"""


def include_play(task: Path, *, hosts: str = "all", play_vars: dict | None = None,
                 task_vars: dict | None = None) -> str:
    """A play that includes one task file by its absolute path, on a local connection."""
    play = {"hosts": hosts, "connection": "local", "gather_facts": False}
    if play_vars:
        play["vars"] = play_vars
    include = {"name": "the task under test", "ansible.builtin.include_tasks": str(task)}
    if task_vars:
        include["vars"] = task_vars
    play["tasks"] = [include]
    return json.dumps([play])  # JSON is YAML


def run(tmp_path: Path, inventory: str, play: str, *args: str, suffix: str = "ini"):
    """Write the inventory and the play into tmp_path and run them; the output is one line."""
    inv = tmp_path / f"inventory.{suffix}"
    inv.write_text(inventory)
    playbook = tmp_path / "play.yml"
    playbook.write_text(play)
    proc = harness_sandbox.run_playbook(playbook, tmp_path, *args, inventory=inv)
    proc.output = " ".join((proc.stdout + proc.stderr).split())   # one line, whatever the wrapping
    return proc


def passed(proc) -> None:
    assert proc.returncode == 0, proc.output
    assert "failed=0" in proc.output, proc.output
    assert re.search(r"ok=[1-9]", proc.output), proc.output


def refused(proc, text: str) -> None:
    assert proc.returncode != 0, proc.output
    assert text in proc.output, proc.output
