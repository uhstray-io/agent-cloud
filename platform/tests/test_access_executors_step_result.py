"""The access executors record their workflow step result, a failed run included.

Distribute SSH Keys (ssh-keys), Back Up Service SSH Key (ssh-key-backup), Harden SSH
(access-harden) and Back Up Credentials to site-config (credential-backup) each include
tasks/emit-step-result.yml LAST, outside the block that does the work, so a failure is
rescued, recorded, and only then fails the play. The registry's evidence keys are checked
against these emits by test_workflow_registry.py; this file proves the emit is reachable.
"""

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
PLAYBOOKS = REPO / "platform/playbooks"
_spec = importlib.util.spec_from_file_location(
    "step_results", REPO / "platform/workflows/service-onboarding/lib/step_results.py"
)
step_results = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(step_results)

EXECUTORS = {
    "distribute-ssh-keys.yml": "ssh-keys",
    "backup-service-ssh-key.yml": "ssh-key-backup",
    "harden-ssh.yml": "access-harden",
    "backup-credentials-to-site-config.yml": "credential-backup",
}


def _last_play_tasks(name: str) -> list:
    plays = yaml.safe_load((PLAYBOOKS / name).read_text())
    return plays[-1]["tasks"]


@pytest.mark.parametrize("name,step", EXECUTORS.items())
def test_the_step_result_is_emitted_after_the_rescued_work(name, step):
    tasks = _last_play_tasks(name)
    emits = [i for i, t in enumerate(tasks)
             if str(t.get("ansible.builtin.include_tasks", "")).endswith("emit-step-result.yml")]
    assert emits, f"{name} does not record its step result"
    emit = tasks[emits[-1]]
    assert emit["vars"]["step_result_step"] == step
    # Unconditional, so check mode and a failed run both reach it.
    assert "when" not in emit, f"{name} gates its step result"
    # Every task before it that does work sits in a block with a rescue, or a failure would
    # end the play before the result is recorded.
    blocks = [t for t in tasks[:emits[-1]] if "block" in t]
    assert blocks and all("rescue" in b for b in blocks), name
    # The play still fails after recording a failure.
    assert any("ansible.builtin.fail" in t for t in tasks[emits[-1] + 1:]), name


@pytest.mark.parametrize("name", ["backup-service-ssh-key.yml", "backup-credentials-to-site-config.yml"])
def test_a_backup_dry_run_reaches_the_step_result(name):
    # meta end_play would skip the emit; the clone block is gated by `when` instead.
    text = (PLAYBOOKS / name).read_text()
    assert "meta: end_play" not in text


def _run(name: str, tmp_path: Path, *extra: str) -> subprocess.CompletedProcess:
    inv = tmp_path / "inv.ini"
    inv.write_text("[demo_svc]\nh1 ansible_connection=local service_name=demo ansible_user=nobody\n")
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env.update(ANSIBLE_NOCOLOR="1", BAO_ROLE_ID="r", BAO_SECRET_ID="s")
    # Port 9 on loopback refuses: every OpenBao call fails, which is the failure being recorded.
    return subprocess.run(
        ["ansible-playbook", "-i", str(inv), str(PLAYBOOKS / name), "--check",
         "-e", "target_service=demo_svc", "-e", "service_name=demo",
         "-e", "credential_service=demo", "-e", "openbao_addr=https://127.0.0.1:9", *extra],
        cwd=REPO, env=env, text=True, capture_output=True, check=False,
    )


@pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")
@pytest.mark.parametrize("name,step", EXECUTORS.items())
def test_a_failed_run_records_a_failed_step_and_still_fails(name, step, tmp_path):
    proc = _run(name, tmp_path)
    assert proc.returncode != 0, proc.stdout
    found = step_results.results_in(proc.stdout.splitlines())
    assert len(found) == 1, f"expected one step result\n{proc.stdout}"
    result = found[0]
    assert result["step"] == step
    assert result["status"] == "fail"
    assert result["check_mode"] is True
    assert result["error"]


@pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")
@pytest.mark.parametrize("name", ["harden-ssh.yml", "backup-service-ssh-key.yml",
                                  "backup-credentials-to-site-config.yml"])
def test_a_no_log_failure_is_recorded_by_name_only(name, tmp_path):
    # Their first OpenBao call is no_log; Ansible hands its RAW failure to the rescue, so the
    # recorded error must withhold the message.
    result = step_results.results_in(_run(name, tmp_path).stdout.splitlines())[0]
    assert "details hidden: the task is no_log" in result["error"], result["error"]
