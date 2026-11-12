"""Synthetic inventories and the one runner for the local_mode identity tests (AC-696).

test_local_mode_identity.py, test_bao_transport_identity.py and test_local_mode_shared_tasks.py run a real
ansible-playbook on these inventories. Each names localhost and, for local-dev, one `<name>-local`
host, so the identity filter (filter_plugins/local_dev.py) decides the mode, not the test. Every run
goes through harness_sandbox.run_playbook: bounded, no stdin, writes confined. A run that needs a
command the task calls (git, getent, sudo) puts a test double first on PATH (fake_bin).

Imported as `import local_mode_inventories`, like forgeries: pytest puts this directory on sys.path.
"""

import json
import os
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

_TASK_HEADER = re.compile(r"^TASK \[(.*)\] \**\s*$")
_RESULT = re.compile(r"^(ok|changed|skipping|fatal|failed)(?=: \[)")


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


def run(tmp_path: Path, inventory: str, play: str, *args: str, suffix: str = "ini", env: dict | None = None):
    """Write the inventory and the play into tmp_path and run them; the output is one line.

    `env` is laid over the harness environment (fake_bin's PATH, for one)."""
    inv = tmp_path / f"inventory.{suffix}"
    inv.write_text(inventory)
    playbook = tmp_path / "play.yml"
    playbook.write_text(play)
    proc = harness_sandbox.run_playbook(playbook, tmp_path, *args, inventory=inv, env=env)
    proc.output = " ".join((proc.stdout + proc.stderr).split())   # one line, whatever the wrapping
    return proc


def fake_bin(tmp_path: Path, scripts: dict[str, str]) -> dict:
    """The environment for a run whose PATH starts with test doubles: one shell script per name in
    tmp_path/fakebin, so a command a task calls (git, getent, sudo, a container engine) is the double
    and never the host's own. Returns the `env` argument of run()."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    for name, body in scripts.items():
        script = bindir / name
        script.write_text("#!/bin/sh\n" + body)
        script.chmod(0o755)
    return {"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}


def task_statuses(stdout: str) -> dict[str, str]:
    """The first result under each task's TASK header, by task name, from the default output format:
    ok, changed, skipping, fatal or failed. A name that appears twice keeps its first result."""
    statuses: dict[str, str] = {}
    name = None
    for line in stdout.splitlines():
        header = _TASK_HEADER.match(line)
        if header:
            name = header.group(1)
            continue
        result = _RESULT.match(line)
        if name is not None and result and name not in statuses:
            statuses[name] = result.group(1)
    return statuses


def passed(proc) -> None:
    assert proc.returncode == 0, proc.output
    assert "failed=0" in proc.output, proc.output
    assert re.search(r"ok=[1-9]", proc.output), proc.output


def refused(proc, text: str) -> None:
    assert proc.returncode != 0, proc.output
    assert text in proc.output, proc.output
