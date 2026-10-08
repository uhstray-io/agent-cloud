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

import playbook_yaml
import pytest

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
    plays = playbook_yaml.plays(PLAYBOOKS / name)
    return plays[-1]["tasks"]


HOST_PLAYS = {"distribute-ssh-keys.yml", "harden-ssh.yml"}


@pytest.mark.parametrize("name,step", EXECUTORS.items())
def test_the_step_result_is_emitted_after_the_rescued_work(name, step):
    plays = playbook_yaml.plays(PLAYBOOKS / name)
    tasks = plays[-1]["tasks"]
    emits = [i for i, t in enumerate(tasks)
             if str(t.get("ansible.builtin.include_tasks", "")).endswith("emit-step-result.yml")]
    assert emits, f"{name} does not record its step result"
    emit = tasks[emits[-1]]
    assert emit["vars"]["step_result_step"] == step
    # Unconditional, so check mode and a failed run both reach it.
    assert "when" not in emit, f"{name} gates its step result"
    # The play still fails after recording a failure.
    assert any("ansible.builtin.fail" in t for t in tasks[emits[-1] + 1:]), name
    if name in HOST_PLAYS:
        # Recorded on the controller, after the host plays, so an UNREACHABLE host (which no
        # rescue catches) is still named by the result.
        assert plays[-1]["hosts"] == "localhost", name
        work = [p for p in plays[:-1] if p["hosts"] != "localhost"]
    else:
        work = [{"tasks": tasks[:emits[-1]]}]
    for play in work:
        blocks = [t for t in play["tasks"] if "block" in t]
        # Every task that does work sits in a block with a rescue, or a failure would end the
        # play before the result is recorded.
        assert blocks and all("rescue" in b for b in blocks), name


@pytest.mark.parametrize("name", ["backup-service-ssh-key.yml", "backup-credentials-to-site-config.yml"])
def test_a_backup_dry_run_reaches_the_step_result(name):
    # meta end_play would skip the emit; the clone block is gated by `when` instead.
    text = (PLAYBOOKS / name).read_text()
    assert "meta: end_play" not in text


def _run(name: str, tmp_path: Path, *extra: str, hosts: str = "", check: bool = True) -> subprocess.CompletedProcess:
    inv = tmp_path / "inv.ini"
    inv.write_text("[demo_svc]\nh1 ansible_connection=local service_name=demo ansible_user=nobody\n" + hosts)
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env.update(ANSIBLE_NOCOLOR="1", BAO_ROLE_ID="r", BAO_SECRET_ID="s")
    # Port 9 on loopback refuses: every OpenBao call fails, which is the failure being recorded.
    return subprocess.run(
        ["ansible-playbook", "-i", str(inv), str(PLAYBOOKS / name), *(["--check"] if check else []),
         "-e", "target_service=demo_svc", "-e", "service_name=demo",
         "-e", "credential_service=demo", "-e", "openbao_addr=https://127.0.0.1:9", *extra],
        cwd=REPO, env=env, text=True, capture_output=True, check=False,
    )


@pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")
@pytest.mark.parametrize("name,step", EXECUTORS.items())
def test_a_failed_run_records_a_failed_step_and_still_fails(name, step, tmp_path):
    proc = _run(name, tmp_path)
    assert proc.returncode != 0, proc.stdout + proc.stderr
    found = step_results.results_in(proc.stdout.splitlines())
    assert len(found) == 1, f"expected one step result\n{proc.stdout}\nstderr:\n{proc.stderr}"
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


# A host nothing listens for: loopback port 1 refuses at once, so the run does not wait.
UNREACHABLE = ("h2 ansible_host=127.0.0.1 ansible_port=1 ansible_connection=ssh "
               "ansible_user=nobody service_name=demo ansible_ssh_common_args='-o ConnectTimeout=3'\n")


START_AT = {"distribute-ssh-keys.yml": "Ensure .ssh directory exists",
            "harden-ssh.yml": "Harden /etc/ssh/sshd_config"}


@pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")
@pytest.mark.parametrize("name,step", [(n, s) for n, s in EXECUTORS.items() if n in HOST_PLAYS])
def test_an_unreachable_host_is_named_by_the_one_result(name, step, tmp_path):
    # Start at the first task that opens a connection (the OpenBao reads before it run on the
    # controller, fail without a store, and are rescued for every host alike). sshd paths point
    # at scratch files so the local host never reads the real config.
    (tmp_path / "sshd_config").write_text("PasswordAuthentication yes\n")
    proc = _run(name, tmp_path, "--start-at-task", START_AT[name],
                "-e", f"_sshd_config_path={tmp_path / 'sshd_config'}",
                "-e", f"_sshd_config_dir={tmp_path}", "-e", "ansible_become=false", hosts=UNREACHABLE)
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "UNREACHABLE" in proc.stdout, proc.stdout + proc.stderr
    found = step_results.results_in(proc.stdout.splitlines())
    assert len(found) == 1, f"expected one step result\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert found[0]["step"] == step and found[0]["status"] == "fail"
    assert "h2: no verdict" in found[0]["error"], found[0]["error"]


@pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")
@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the read-only directory the failure relies on")
def test_a_failed_harden_does_not_restart_sshd(tmp_path):
    # Gets past OpenBao by starting at the first sshd edit, which CHANGES a scratch sshd_config
    # (notifying the restart), then fails on a read-only override file before the flush. The
    # rescue then lets the play finish; the notified restart must still not run.
    cfg = tmp_path / "sshd_config"
    cfg.write_text("PasswordAuthentication yes\n")
    drop = tmp_path / "sshd_config.d"
    drop.mkdir()
    (drop / "50-cloud.conf").write_text("PasswordAuthentication yes\n")
    drop.chmod(0o555)
    try:
        proc = _run("harden-ssh.yml", tmp_path, "--start-at-task", "Harden /etc/ssh/sshd_config",
                    "-e", f"_sshd_config_path={cfg}", "-e", f"_sshd_config_dir={drop}",
                    "-e", "ansible_become=false", check=False)
    finally:
        drop.chmod(0o755)
    # A real run (under --check the read-only file is never written, so nothing would fail):
    # the scratch config really changed, which is what notified the restart.
    assert "PasswordAuthentication no" in cfg.read_text(), proc.stdout
    result = step_results.results_in(proc.stdout.splitlines())[0]
    assert result["status"] == "fail"
    assert "Harden each sshd_config.d override file" in result["error"], result["error"]
    assert "RUNNING HANDLER [Restart sshd]" not in proc.stdout or "skipping: [h1]" in \
        proc.stdout.split("RUNNING HANDLER [Restart sshd]", 1)[1].split("TASK [", 1)[0], proc.stdout
