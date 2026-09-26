"""Both persistence playbooks find a legacy-layout service through compose_working_dir, and
Ensure refuses a rootless container a boot unit would not start.

Runs the real playbooks against real podman containers on this machine (local_mode, so no
linger or boot unit is touched). Production openbao, semaphore and caddy run from directories
outside the monorepo; podman's boot unit starts only `always` containers. The rootful path
(a per-container systemd unit) needs root and systemd, so its decision logic is tested in
test_workflow_credential_boundaries.py instead. Requires ansible-playbook and a working podman.
"""

import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PLAYBOOKS = REPO / "platform/playbooks"


def _podman_works():
    if shutil.which("podman") is None or shutil.which("ansible-playbook") is None:
        return False
    return subprocess.run(["podman", "info"], capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(not _podman_works(), reason="needs ansible-playbook and podman")

IMAGE = "docker.io/library/alpine:latest"


def _podman(*args):
    return subprocess.run(["podman", *args], capture_output=True, text=True, check=True).stdout.strip()


def _playbook(tmp_path, name, workdir, *extra):
    inventory = tmp_path / "inventory.ini"
    inventory.write_text(
        "[demo_svc]\ndemo ansible_connection=local\n\n[demo_svc:vars]\n"
        # No monorepo_deploy_path: a legacy service may declare only its working dir (PR 284 review).
        f"service_name=demo\nlocal_mode=true\ncontainer_engine=podman\ncompose_working_dir={workdir}\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env["ANSIBLE_NOCOLOR"] = "1"
    return subprocess.run(
        ["ansible-playbook", "-i", str(inventory), str(PLAYBOOKS / name), "-e", "target_service=demo_svc", *extra],
        cwd=REPO, env=env, text=True, capture_output=True, stdin=subprocess.DEVNULL,
    )


@pytest.fixture
def legacy_service():
    tag = uuid.uuid4().hex[:8]
    workdir = f"/legacy/demo-{tag}"
    label = f"com.docker.compose.project.working_dir={workdir}"
    app, init = f"demo-app-{tag}", f"demo-init-{tag}"
    try:
        try:
            _podman("run", "-d", "--name", app, "--label", label, "--restart", "unless-stopped", IMAGE, "sleep", "600")
            _podman("run", "--name", init, "--label", label, "--label", "agent-cloud.one-shot=true",
                    "--restart", "no", IMAGE, "true")
        except subprocess.CalledProcessError as err:
            pytest.skip(f"cannot start a test container: {err.stderr}")
        yield workdir, app, init
    finally:
        # Also after a skip: the app may have started before the one-shot failed (PR 284 review).
        subprocess.run(["podman", "rm", "-f", app, init], capture_output=True)


def _state(name):
    return _podman("inspect", "--format", "{{.HostConfig.RestartPolicy.Name}} {{.State.Pid}}", name).split()


def test_verify_finds_the_legacy_service_and_ensure_refuses_what_would_not_boot(tmp_path, legacy_service):
    workdir, app, init = legacy_service
    verified = _playbook(tmp_path, "verify-service-persistence.yml", workdir)
    assert verified.returncode != 0
    # Found through the override, and failed for the policy, not for an empty selection.
    assert f"restart policy not always: {app}" in verified.stdout, verified.stdout[-2000:]
    assert "no containers carry" not in verified.stdout

    for extra in ((), ("--check",)):
        ensured = _playbook(tmp_path, "ensure-service-persistence.yml", workdir, *extra)
        assert ensured.returncode != 0, extra
        assert f"boot unit will not start them: {app}." in ensured.stdout, ensured.stdout[-2000:]
        assert init not in ensured.stdout.split("will not start them:", 1)[1].split("\n", 1)[0]
    assert _state(app)[0] == "unless-stopped" and _state(init)[0] == "no"


def test_without_the_override_nothing_is_found(tmp_path, legacy_service):
    # The monorepo path does not match the legacy working_dir label: the override is what finds it.
    elsewhere = "/home/nobody/agent-cloud/platform/services/demo/deployment"
    done = _playbook(tmp_path, "verify-service-persistence.yml", elsewhere)
    assert done.returncode != 0
    assert "no containers carry the compose working_dir label" in done.stdout
