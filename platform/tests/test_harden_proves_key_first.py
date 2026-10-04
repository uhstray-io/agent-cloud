"""Harden SSH proves a fresh key-only login BEFORE it withdraws password auth.

AGENTS.md deployment rule 5: never disable an auth method before confirming its replacement
works. The access-harden step's `key_only_proven` evidence therefore comes from a probe that
runs before sudoers or any sshd config is written; when it does not get in, the play refuses
with nothing edited and records a failed step.
"""

import os
import shutil
import subprocess
from pathlib import Path

import harness_sandbox
import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
PLAYBOOK = REPO / "platform/playbooks/harden-ssh.yml"
PROBE = "Probe a fresh key-only login before hardening"
REFUSE = "Refuse to harden without a proven key-only login"
WRITERS = ("ansible.builtin.copy", "ansible.builtin.lineinfile", "ansible.builtin.meta")


def _flat(tasks):
    for task in tasks or []:
        yield task
        for key in ("block", "rescue", "always"):
            yield from _flat(task.get(key))


def _host_tasks() -> list:
    return list(_flat(yaml.safe_load(PLAYBOOK.read_text())[0]["tasks"]))


def test_the_key_only_proof_precedes_every_edit():
    tasks = _host_tasks()
    names = [t.get("name") for t in tasks]
    first_write = next(i for i, t in enumerate(tasks) if any(m in t for m in WRITERS))
    assert names.index(PROBE) < names.index(REFUSE) < first_write, names[first_write]
    probe = tasks[names.index(PROBE)]
    argv = probe["ansible.builtin.command"]["argv"]
    for opt in ("BatchMode=yes", "StrictHostKeyChecking=yes", "IdentitiesOnly=yes",
                "PasswordAuthentication=no", "PreferredAuthentications=publickey"):
        assert opt in argv, opt
    # Read-only connection probe: runs in check mode, never on a credential boundary.
    assert probe["check_mode"] is False and not probe.get("no_log")


def test_key_only_evidence_comes_only_from_the_pre_hardening_probe():
    emit = yaml.safe_load(PLAYBOOK.read_text())[-1]["tasks"][1]
    evidence = emit["vars"]["step_result_evidence"]["key_only_proven"]
    assert "_key_preproof" in evidence and "_key_test" not in evidence


SSH_FAIL = "#!/bin/sh\necho 'nobody@127.0.0.1: Permission denied (publickey).' >&2\nexit 255\n"


@pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")
def test_a_failed_proof_edits_nothing_and_records_the_refusal(tmp_path):
    step_results = __import__("test_access_executors_step_result").step_results
    cfg = tmp_path / "sshd_config"
    cfg.write_text("PasswordAuthentication yes\n")
    (tmp_path / "bin").mkdir()
    stub = tmp_path / "bin" / "ssh"
    stub.write_text(SSH_FAIL)
    stub.chmod(0o755)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(tmp_path / "k")], check=True)
    inv = tmp_path / "inv.ini"
    inv.write_text("[demo_svc]\nh1 ansible_connection=local ansible_host=127.0.0.1 "
                   "service_name=demo ansible_user=nobody\n")
    env = harness_sandbox.env_for(tmp_path)
    env.update(BAO_ROLE_ID="r", BAO_SECRET_ID="s", PATH=f"{tmp_path / 'bin'}:{os.environ.get('PATH', '')}")
    cmd = ["ansible-playbook", "-i", str(inv), str(PLAYBOOK),
           "--start-at-task", "Materialise the pre-hardening proof key (runner-local, 0600)",
           "-e", "target_service=demo_svc", "-e", "ansible_become=false",
           "-e", f"_mgmt_priv_key={(tmp_path / 'k').read_text()}",
           "-e", f'{{"ssh_host_key_files": ["{tmp_path / "k.pub"}"]}}',
           "-e", f"_sshd_config_path={cfg}", "-e", f"_sshd_config_dir={tmp_path}"]
    proc = harness_sandbox.run(cmd, tmp_path, cwd=REPO, env=env)
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, out
    assert cfg.read_text() == "PasswordAuthentication yes\n", out
    assert "TASK [Harden /etc/ssh/sshd_config]" not in proc.stdout, out
    result = step_results.results_in(proc.stdout.splitlines())[0]
    assert result["status"] == "fail" and REFUSE in result["error"], result
    assert "Permission denied" in result["error"], result
    assert result["evidence"]["key_only_proven"] in (False, "False"), result
