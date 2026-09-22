"""The shared step-result task records one parseable result, in check mode too.

Runs a real play through ansible-playbook (ansible-core is installed in CI's test job), so
this proves the contract the collector depends on: the result is printed as JSON under
CUSTOM STATS by the repository ansible.cfg, and --check does not suppress it.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TASK = REPO / "platform/playbooks/tasks/emit-step-result.yml"

pytestmark = pytest.mark.skipif(
    shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed"
)


def _play(tmp_path: Path, **vars_) -> Path:
    play = [
        {
            "name": "emit fixture",
            "hosts": "localhost",
            "connection": "local",
            "gather_facts": False,
            "vars": vars_,
            "tasks": [{"name": "emit", "ansible.builtin.include_tasks": str(TASK)}],
        }
    ]
    path = tmp_path / "play.yml"
    path.write_text(json.dumps(play))  # JSON is valid YAML
    return path


def _run(play: Path, *args: str) -> subprocess.CompletedProcess:
    # cwd=REPO so the repository ansible.cfg is the one Ansible loads, as on the controller.
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env["ANSIBLE_NOCOLOR"] = "1"
    return subprocess.run(
        ["ansible-playbook", "-i", "localhost,", str(play), *args],
        cwd=REPO, env=env, text=True, capture_output=True, check=False,
    )


def _result(stdout: str) -> dict:
    lines = [line.strip() for line in stdout.splitlines() if line.strip().startswith("RUN:")]
    assert len(lines) == 1, f"expected one CUSTOM STATS line, got {lines!r}\n{stdout}"
    return json.loads(lines[0][len("RUN:"):])["step_result"]


@pytest.mark.parametrize("check", [False, True])
def test_result_is_recorded_and_parseable(tmp_path, check):
    play = _play(
        tmp_path,
        step_result_step="fw-harden",
        step_result_status="pass",
        step_result_evidence={"ufw_active": True, "allows": [22, 443]},
        service_name="agentgateway",
    )
    proc = _run(play, *(["--check"] if check else []))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    result = _result(proc.stdout)
    assert result["schema"] == "agentcloud/step-result/v1"
    assert result["service"] == "agentgateway"
    assert result["step"] == "fw-harden"
    assert result["status"] == "pass"
    assert result["evidence"] == {"ufw_active": True, "allows": [22, 443]}
    assert result["undo"] == "none"
    assert result["check_mode"] is check


def test_fail_without_error_message_is_refused(tmp_path):
    play = _play(tmp_path, step_result_step="fw-harden", step_result_status="fail")
    proc = _run(play)
    assert proc.returncode != 0
    assert "error message when the status is fail" in proc.stdout


def test_unknown_status_is_refused(tmp_path):
    play = _play(tmp_path, step_result_step="fw-harden", step_result_status="green")
    proc = _run(play)
    assert proc.returncode != 0
