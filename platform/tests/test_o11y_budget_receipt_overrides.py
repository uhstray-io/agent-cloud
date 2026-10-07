"""The o11y budget receipt cannot be forged through an extra var.

The per-container memory gate reads `_container_memory.rc` and the receipt prints its stdout.
An extra var outranks a registered result (ansible-core variable precedence, 22 against 12-21),
so `-e '_container_memory={"rc": 0, "stdout": ...}'` would issue a verified receipt for an
unreadable or stopped container (PR #460 review). refuse-internal-extra-vars.yml, the run's
first play, refuses it before any other play starts.
"""

import os
import shutil
import subprocess
from pathlib import Path

import forgeries
import playbook_yaml
import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
PLAYBOOK = REPO / "platform/playbooks/verify-o11y-production-budgets.yml"

needs_ansible = pytest.mark.skipif(shutil.which("ansible-playbook") is None,
                                   reason="ansible-playbook not installed")

# The memory verdict and the readings it is computed from. rc 0 alone passes the gate; the
# forgeries carry no nested JSON because forgeries.py refuses quotes and backslashes.
FORGED = {
    "_container_memory": {"rc": 0, "stdout": "{}"},
    "_container_stats": {"rc": 0, "stdout": "o11y-grafana 1048576"},
    "_lsc": {"rc": 0, "stdout_lines": ["o11y-grafana running"]},
}


def _plays() -> list:
    return yaml.safe_load(PLAYBOOK.read_text())


def test_the_receipt_opens_by_refusing_internal_extra_vars():
    assert _plays()[0] == {"name": "Refuse extra vars that set internal names",
                           "ansible.builtin.import_playbook": playbook_yaml.OVERRIDE_GUARD}


def test_the_memory_gate_reads_the_names_tested_below():
    # The refusals below target what the gate and the receipt actually read.
    tasks = next(p for p in _plays() if p.get("name") == "Read production retention and cardinality settings")["tasks"]
    gate = next(t for t in tasks if t["name"] == "Require a memory reading for every running o11y container")
    assert gate["ansible.builtin.assert"]["that"] == "_container_memory.rc == 0"
    reduce_task = next(t for t in tasks if t["name"] == "Reduce per-container memory to names and MiB")
    assert reduce_task["register"] == "_container_memory"
    stdin = reduce_task["ansible.builtin.command"]["stdin"]
    assert "_lsc.rc" in stdin and "_lsc.stdout_lines" in stdin and "_container_stats.rc" in stdin
    assert "_container_memory.stdout" in tasks[-1]["ansible.builtin.debug"]["msg"]["o11y_container_memory_mib"]


@needs_ansible
@pytest.mark.parametrize("name, forge", [
    pytest.param(name, f.values[0], id=f"{name}-{f.id}")
    for name, forged in FORGED.items()
    for f in forgeries.templated_forgeries(name, None, forged)
])
def test_a_forged_memory_reading_is_refused_before_any_play(name, forge, tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env.update(ANSIBLE_NOCOLOR="1", ANSIBLE_LOCAL_TEMP=str(tmp_path), ANSIBLE_REMOTE_TEMP=str(tmp_path))
    proc = subprocess.run(
        ["ansible-playbook", "-i", "localhost,", str(PLAYBOOK), "--check",
         "-e", "expected_repository_sha=" + "0" * 40, "-e", "expected_receiver_sha=" + "0" * 40,
         "-e", forge(tmp_path)],
        cwd=REPO, env=env, text=True, capture_output=True, check=False, timeout=120)
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert f"Refusing to run: {name} set from outside the playbook" in proc.stdout, proc.stdout + proc.stderr
    assert proc.stdout.count("PLAY [") == 1, proc.stdout
    assert "status: verified" not in proc.stdout and '"status": "verified"' not in proc.stdout
