"""Verify Service Persistence records ONE verdict for the whole target group.

D10 review (change service-deployment-workflow, task 7.1): an absent target group exited 0 with
no step result, and the run_once emitter recorded only the first host's verdict. Runs the real
playbook through ansible-playbook with a fake container engine (a script answering `ps` and
`inspect`), so no podman is needed; local_mode skips linger and the boot units.
"""

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PLAYBOOK = REPO / "platform/playbooks/verify-service-persistence.yml"
_spec = importlib.util.spec_from_file_location(
    "step_results", REPO / "platform/workflows/service-onboarding/lib/step_results.py"
)
step_results = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(step_results)

pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")

# `ps` lists one container only for a working dir containing "good"; `inspect` reports it on
# restart policy always, running.
FAKE_ENGINE = """#!/bin/sh
if [ "$1" = ps ]; then
  case "$*" in *good*) echo app-1 ;; esac
  exit 0
fi
echo "always running 0 "
"""


def _run(tmp_path, hosts: dict[str, str], *extra) -> subprocess.CompletedProcess:
    engine = tmp_path / "engine"
    engine.write_text(FAKE_ENGINE)
    engine.chmod(0o755)
    lines = ["[demo_svc]"] + [
        f"{h} ansible_connection=local compose_working_dir={wd}" for h, wd in hosts.items()
    ] + ["", "[demo_svc:vars]", "service_name=demo", "local_mode=true", f"container_engine={engine}"]
    inventory = tmp_path / "inventory.ini"
    inventory.write_text("\n".join(lines) + "\n")
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env["ANSIBLE_NOCOLOR"] = "1"
    return subprocess.run(
        ["ansible-playbook", "-i", str(inventory), str(PLAYBOOK), "-e", "target_service=demo_svc", *extra],
        cwd=REPO, env=env, text=True, capture_output=True, stdin=subprocess.DEVNULL, check=False,
    )


def _result(stdout: str) -> dict:
    found = step_results.results_in(stdout.splitlines())
    assert len(found) == 1, f"expected one step result, got {found!r}\n{stdout[-3000:]}"
    return found[0]


@pytest.mark.parametrize("check", [False, True])
def test_a_failing_second_host_fails_the_recorded_result(tmp_path, check):
    # The first host passes; the old run_once emitter recorded its pass for the group.
    proc = _run(tmp_path, {"alpha": "/srv/good", "beta": "/srv/empty"}, *(["--check"] if check else []))
    assert proc.returncode != 0
    result = _result(proc.stdout)
    assert result["step"] == "systemd-enablement"
    assert result["status"] == "fail"
    assert result["check_mode"] is check
    assert "beta: no containers carry the compose working_dir label" in result["error"]
    assert "alpha:" not in result["error"]


def test_every_host_passing_records_a_pass(tmp_path):
    proc = _run(tmp_path, {"alpha": "/srv/good-a", "beta": "/srv/good-b"})
    assert proc.returncode == 0, proc.stdout[-3000:]
    result = _result(proc.stdout)
    assert result["status"] == "pass" and result["error"] in (None, "")
    assert set(result["evidence"]) == {"linger", "restart_policies", "boot_unit"}


def test_an_absent_target_group_is_refused_not_a_silent_pass(tmp_path):
    proc = _run(tmp_path, {"alpha": "/srv/good"}, "-e", "target_service=no_such_svc")
    assert proc.returncode != 0
    assert "matches no hosts in this inventory" in proc.stdout
