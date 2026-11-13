"""Verify Service Persistence records ONE verdict for the whole target group.

D10 review (change service-deployment-workflow, task 7.1): an absent target group exited 0 with
no step result, and the run_once emitter recorded only the first host's verdict. Runs the real
playbook through ansible-playbook with a fake container engine (a script answering `ps` and
`inspect`), so no podman is needed. Its hosts are named -local, so the playbook takes the local-dev branch (no
linger, no boot units); the production tests at the end run on a production inventory.
"""

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path

import forgeries
import harness_sandbox
import pytest
from local_mode_inventories import task_statuses

REPO = Path(__file__).resolve().parents[2]
PLAYBOOK = REPO / "platform/playbooks/verify-service-persistence.yml"
ENSURE = REPO / "platform/playbooks/ensure-service-persistence.yml"
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
    return harness_sandbox.run_playbook(
        PLAYBOOK, tmp_path, "-e", "target_service=demo_svc", *extra, inventory=inventory,
    )


def _result(stdout: str) -> dict:
    found = step_results.results_in(stdout.splitlines())
    assert len(found) == 1, f"expected one step result, got {found!r}\n{stdout[-3000:]}"
    return found[0]


@pytest.mark.parametrize("check", [False, True])
def test_a_failing_second_host_fails_the_recorded_result(tmp_path, check):
    # The first host passes; the old run_once emitter recorded its pass for the group.
    proc = _run(tmp_path, {"alpha-local": "/srv/good", "beta-local": "/srv/empty"}, *(["--check"] if check else []))
    assert proc.returncode != 0
    result = _result(proc.stdout)
    assert result["step"] == "systemd-enablement"
    assert result["status"] == "fail"
    assert result["check_mode"] is check
    assert "beta-local: no containers carry the compose working_dir label" in result["error"]
    assert "alpha-local:" not in result["error"]
    assert result["evidence"]["restart_policies"] == {"alpha-local": {"app-1": "always"}, "beta-local": {}}


def test_every_host_passing_records_a_pass(tmp_path):
    proc = _run(tmp_path, {"alpha-local": "/srv/good-a", "beta-local": "/srv/good-b"})
    assert proc.returncode == 0, proc.stdout[-3000:]
    result = _result(proc.stdout)
    assert result["status"] == "pass" and result["error"] in (None, "")
    assert set(result["evidence"]) == {"linger", "restart_policies", "boot_unit"}
    assert result["evidence"]["linger"] == {"alpha-local": "not-applicable", "beta-local": "not-applicable"}


def test_an_absent_target_group_is_refused_not_a_silent_pass(tmp_path):
    proc = _run(tmp_path, {"alpha-local": "/srv/good"}, "-e", "target_service=no_such_svc")
    assert proc.returncode != 0
    assert "matches no hosts in this inventory" in proc.stdout


# --- production branch: a forged local_mode cannot skip the linger and boot-unit checks (AC-726, plan 24) ----
# sudo for become: drop its flags and run the rest as this user.
SUDO = (
    "#!/bin/sh\n"
    'while [ $# -gt 0 ]; do case "$1" in -H|-S|-n) shift ;; -u) shift 2 ;; --) shift; break ;; *) break ;; esac; done\n'
    'exec "$@"\n'
)


def _production_run(tmp_path: Path, rootful: bool, *extra: str, playbook: Path = PLAYBOOK):
    """One production host on rootless or rootful podman, with test doubles first on PATH: podman answers as
    FAKE_ENGINE, loginctl reports linger on, getent names testuser at a home with no boot unit, sudo runs its
    argument, and systemctl reports every unit disabled. Nothing real is started or read."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    doubles = {
        "podman": FAKE_ENGINE,
        "loginctl": "#!/bin/sh\necho Linger=yes\n",
        "getent": f'#!/bin/sh\necho "testuser:x:1000:1000::{tmp_path / "home"}:/bin/sh"\n',
        "sudo": SUDO,
        "systemctl": "#!/bin/sh\necho disabled\nexit 1\n",
    }
    for name, body in doubles.items():
        (bindir / name).write_text(body)
        (bindir / name).chmod(0o755)
    inventory = tmp_path / "inventory.ini"
    inventory.write_text(
        "[demo_svc]\nalpha ansible_connection=local compose_working_dir=/srv/good\n\n"
        "[demo_svc:vars]\nservice_name=demo\nlocal_mode=false\ncontainer_engine=podman\nlinger_user=testuser\n"
        + ("podman_rootful=true\n" if rootful else "")
    )
    env = {"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}
    return harness_sandbox.run_playbook(
        playbook, tmp_path, "-e", "target_service=demo_svc", *extra, inventory=inventory, env=env,
    )


@pytest.mark.parametrize("forge", forgeries.templated_forgeries("local_mode", honest=False, forged=True)[:2])
def test_verify_checks_linger_and_the_boot_unit_on_production_under_a_forged_local_mode(tmp_path, forge):
    result = _result(_production_run(tmp_path, False, "-e", forge(tmp_path)).stdout)
    assert result["status"] == "fail", result
    assert "podman-restart.service is not enabled for the deploying user" in result["error"], result


@pytest.mark.parametrize("forge", forgeries.templated_forgeries("local_mode", honest=False, forged=True)[:2])
def test_verify_checks_the_system_boot_unit_on_production_under_a_forged_local_mode(tmp_path, forge):
    result = _result(_production_run(tmp_path, True, "-e", forge(tmp_path)).stdout)
    assert result["status"] == "fail", result
    assert "podman-restart.service is not enabled (system unit)" in result["error"], result


@pytest.mark.parametrize("forge", forgeries.templated_forgeries("local_mode", honest=False, forged=True)[:2])
def test_ensure_runs_the_production_system_boot_unit_task_under_a_forged_local_mode(tmp_path, forge):
    # The task's own result depends on the host's systemd, which a test does not control. The claim is that the
    # production branch ran it, so the task is not skipped.
    proc = _production_run(tmp_path, True, "-e", forge(tmp_path), "--check", playbook=ENSURE)
    statuses = task_statuses(proc.stdout)
    assert statuses.get("Rootful podman: the system boot unit") not in (None, "skipping"), proc.stdout[-3000:]
