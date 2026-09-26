"""ensure-service-persistence.yml converges restart policy in place; both playbooks find a
legacy-layout service through compose_working_dir.

Runs the real playbooks against real podman containers on this machine (local_mode, so no
linger or boot unit is touched). Production openbao, semaphore and caddy run from directories
outside the monorepo, with `unless-stopped` or no policy; a boot unit starts only `always`.
Requires ansible-playbook and a working podman.
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


def _playbook(tmp_path, name, workdir):
    inventory = tmp_path / "inventory.ini"
    inventory.write_text(
        "[demo_svc]\ndemo ansible_connection=local\n\n[demo_svc:vars]\n"
        f"service_name=demo\nlocal_mode=true\ncontainer_engine=podman\ncompose_working_dir={workdir}\n"
        "monorepo_deploy_path=platform/services/demo/deployment\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env["ANSIBLE_NOCOLOR"] = "1"
    return subprocess.run(
        ["ansible-playbook", "-i", str(inventory), str(PLAYBOOKS / name), "-e", "target_service=demo_svc"],
        cwd=REPO, env=env, text=True, capture_output=True, stdin=subprocess.DEVNULL,
    )


@pytest.fixture
def legacy_service():
    tag = uuid.uuid4().hex[:8]
    workdir = f"/legacy/demo-{tag}"
    label = f"com.docker.compose.project.working_dir={workdir}"
    app, init = f"demo-app-{tag}", f"demo-init-{tag}"
    try:
        _podman("run", "-d", "--name", app, "--label", label, "--restart", "unless-stopped", IMAGE, "sleep", "600")
        _podman("run", "--name", init, "--label", label, "--label", "agent-cloud.one-shot=true",
                "--restart", "no", IMAGE, "true")
    except subprocess.CalledProcessError as err:
        pytest.skip(f"cannot start a test container: {err.stderr}")
    yield workdir, app, init
    subprocess.run(["podman", "rm", "-f", app, init], capture_output=True)


def _state(name):
    return _podman("inspect", "--format", "{{.HostConfig.RestartPolicy.Name}} {{.State.Pid}}", name).split()


def test_ensure_sets_always_in_place_and_verify_then_passes(tmp_path, legacy_service):
    workdir, app, init = legacy_service
    assert _playbook(tmp_path, "verify-service-persistence.yml", workdir).returncode != 0

    pid_before = _state(app)[1]
    ensured = _playbook(tmp_path, "ensure-service-persistence.yml", workdir)
    assert ensured.returncode == 0, ensured.stdout[-2000:]

    policy, pid_after = _state(app)
    assert policy == "always"
    assert pid_after == pid_before, "the container was restarted"
    assert _state(init)[0] == "no", "a one-shot container's policy was changed"

    verified = _playbook(tmp_path, "verify-service-persistence.yml", workdir)
    assert verified.returncode == 0, verified.stdout[-2000:]
    assert app in verified.stdout and init in verified.stdout


def test_without_the_override_nothing_is_found(tmp_path, legacy_service):
    # The monorepo path does not match the legacy working_dir label: the override is what finds it.
    elsewhere = "/home/nobody/agent-cloud/platform/services/demo/deployment"
    done = _playbook(tmp_path, "verify-service-persistence.yml", elsewhere)
    assert done.returncode != 0
    assert "no containers carry the compose working_dir label" in done.stdout
