"""weft's deploy and rollback, run for real against a fake container engine.

The structural suite (test_service_weft.bats) reads the playbooks; two defects in #743 lived in
their ORDER, which a grep cannot see (agent-cloud#890):

- B1: the deploy stopped the stack before it rendered `.env`, and `deploy.sh` reads `.env` for every
  command, so the first deploy to a fresh host failed at its stop.
- B2: the record that decides which image a rollback runs (`deployed.json`) lived in the workspace
  directory, which the internet-facing board mounts read-write, and the rollback rendered its
  digests without checking them.

So these tests run the real `tasks/weft-deploy.yml`, `tasks/weft-rollback.yml`,
`tasks/weft-verify-images.yml` and `deploy.sh` on this machine. What needs the network or a
credential is replaced at its own seam: the release fetch (`tasks/weft-release.yml`, which talks
to the registry) hands over a release.json, and Discord, the seal and GitHub are stubs that record
what they were told. `podman` is a script that logs its arguments and answers `inspect`; its `run`
of a `health` one-off fails for one chosen digest, which is how a release is made unhealthy.
"""

import json
import os
import shutil
import socket
import subprocess
from pathlib import Path

import pytest
import yaml
from ansible_python import needs_ansible

REPO = Path(__file__).resolve().parents[2]
PLAYBOOKS = REPO / "platform/playbooks"
DEPLOY_DIR = REPO / "platform/services/weft/deployment"
REGISTRY = "ghcr.io/uhstray-io"
SIGNER = "https://github.com/uhstray-io/weft/.github/workflows/images.yml@refs/heads/main"
WORKSPACE_IMAGES = ["weft-workspace", "weft-builder", "weft-proxy", "weft-harness-claude-code", "weft-harness-opencode"]
NEW = "a" * 40
OLD = "b" * 40

pytestmark = [needs_ansible, pytest.mark.skipif(shutil.which("ss") is None, reason="needs ss (iproute2)")]


def _release(commit: str, digit: str, registry: str = REGISTRY) -> dict:
    return {"format": 1, "commit": commit, "version": None, "signed_by": {"identity": SIGNER},
            "images": {name: f"{registry}/{name}@sha256:{digit * 64}" for name in WORKSPACE_IMAGES}}


FAKE_PODMAN = """#!/bin/bash
echo "$*" >> "{log}"
case "$1" in
  inspect)
    case "$*" in
      *Health*) echo healthy ;;
      *RestartCount*) echo 0 ;;
    esac ;;
  run)
    # A health one-off of the image named in {fail} fails: that is the unhealthy release.
    if [ -s "{fail}" ] && [[ "$*" == *"$(cat "{fail}")"* ]] && [[ "$*" == *" health"* ]]; then
      exit 1
    fi ;;
esac
exit 0
"""

STUBS = {
    # The fetch and its signature check talk to a registry; the release arrives as a value instead,
    # and the real image check still runs on it.
    "weft-release.yml": [
        {"name": "Stub: the fetched release", "ansible.builtin.set_fact": {
            "_weft_release_json": "{{ test_release }}", "_weft_auth_file": ""}},
        {"name": "Stub: its images, checked by the real task",
         "ansible.builtin.include_tasks": "weft-verify-images.yml",
         "vars": {"_wvi_release": "{{ _weft_release_json }}"}},
        {"name": "Stub: take the images",
         "ansible.builtin.set_fact": {"_weft_images": "{{ _weft_release_json.images }}"}},
    ],
    "weft-seal.yml": [{"name": "Stub: the seal", "ansible.builtin.debug": {"msg": "seal"}}],
    "weft-workspace-files.yml": [{"name": "Stub: the workspace's files", "ansible.builtin.debug": {"msg": "files"}}],
    "weft-github-deployment.yml": [{"name": "Stub: GitHub", "ansible.builtin.debug": {"msg": "github"}}],
    "notify-discord.yml": [{"name": "Stub: Discord", "ansible.builtin.shell": 'printf "%s\\n" "$MSG" >> "$LOG"',
                            "environment": {"MSG": "{{ _discord_message }}", "LOG": "{{ test_discord_log }}"},
                            "changed_when": False}],
}


@pytest.fixture
def host(tmp_path):
    """A fresh workspace host: no .env, no record, nothing running, and a UDP port bound for calls."""
    pb = tmp_path / "pb"
    (pb / "tasks").mkdir(parents=True)
    for name in ("weft-deploy.yml", "weft-rollback.yml", "weft-verify-images.yml", "verify-cosign.yml"):
        shutil.copy(PLAYBOOKS / "tasks" / name, pb / "tasks" / name)
    for name, tasks in STUBS.items():
        (pb / "tasks" / name).write_text(yaml.safe_dump(tasks))
    # Templates are read from {{ playbook_dir }}/../services/weft/deployment/templates.
    (tmp_path / "services").symlink_to(REPO / "platform/services")
    clone = tmp_path / "clone/platform"
    shutil.copytree(DEPLOY_DIR, clone / "services/weft/deployment",
                    ignore=shutil.ignore_patterns(".env", "door.key"))
    (clone / "lib").symlink_to(REPO / "platform/lib")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "podman").write_text(FAKE_PODMAN.format(log=tmp_path / "engine.log", fail=tmp_path / "unhealthy"))
    (bindir / "podman").chmod(0o755)
    # The deploy's health checks that the workspace's calls port is bound; this socket is it.
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind(("127.0.0.1", 0))
    (tmp_path / "udp-port").write_text(str(udp.getsockname()[1]))
    yield tmp_path
    udp.close()


def _deploy(tmp_path: Path, release: dict, udp_port: int):
    srv = tmp_path / "srv"
    play = [{
        "hosts": "localhost", "connection": "local", "gather_facts": False,
        "vars_files": [str(PLAYBOOKS / "vars/weft.yml")],
        "vars": {
            "weft_release": release["commit"], "weft_workspace_id": "ws1", "weft_voice_udp": udp_port,
            "weft_root": str(srv / "weft"), "weft_state_root": str(srv / "weft-state"),
            "weft_health_minutes": 0, "weft_watch_minutes": 0, "weft_board_bind": "127.0.0.1",
            "_weft_role": "workspace", "_weft_workspace_dir": str(srv / "weft/ws1"),
            "_weft_state_dir": "{{ _weft_workspace_state_dir }}",
            "_weft_deploy_dir": str(tmp_path / "clone/platform/services/weft/deployment"),
            "_weft_label": "workspace ws1", "_weft_environment": "staging", "_weft_discord_webhook": "",
            "_weft_podman_socket": "/run/user/0/podman/podman.sock",
            "_weft_deploy_env": {"CONTAINER_ENGINE": "podman", "COMPOSE_CMD": "podman compose", "LOCAL_MODE": ""},
            "_weft_health_argv": ["bash", "deploy.sh", "workspace", "health"],
            "test_release": release, "test_discord_log": str(tmp_path / "discord.log"),
        },
        "tasks": [
            {"name": "The directories weft-workspace.yml makes", "ansible.builtin.file": {
                "path": "{{ item }}", "state": "directory", "mode": "0700"},
             "loop": ["{{ _weft_workspace_dir }}", "{{ _weft_state_dir }}"]},
            {"name": "One deploy", "ansible.builtin.include_tasks": "tasks/weft-deploy.yml"},
        ],
    }]
    playbook = tmp_path / "pb/run.yml"
    playbook.write_text(yaml.safe_dump(play, width=10**6))
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env.update(PATH=f"{tmp_path / 'bin'}{os.pathsep}{env['PATH']}", ANSIBLE_NOCOLOR="1",
               ANSIBLE_LOCAL_TEMP=str(tmp_path), ANSIBLE_REMOTE_TEMP=str(tmp_path))
    proc = subprocess.run(["ansible-playbook", "-i", "localhost,", str(playbook)], cwd=REPO, env=env,
                          text=True, capture_output=True, check=False, timeout=180)
    log = tmp_path / "engine.log"
    discord = tmp_path / "discord.log"
    return proc, (log.read_text() if log.exists() else ""), (discord.read_text() if discord.exists() else "")


def _udp_port(tmp_path: Path) -> int:
    return int((tmp_path / "udp-port").read_text())


def _state(tmp_path: Path) -> Path:
    return tmp_path / "srv/weft-state/workspaces/ws1"


def _workspace(tmp_path: Path) -> Path:
    return tmp_path / "srv/weft/ws1"


def _ups(log: str) -> list[str]:
    return [line for line in log.splitlines()
            if line.startswith("compose ") and " up " in line and line.endswith(" board")]


def test_the_first_deploy_to_a_host_with_no_env_and_no_record_reaches_up(host):
    proc, log, discord = _deploy(host, _release(NEW, "a"), _udp_port(host))
    assert proc.returncode == 0, (
        "a fresh host must deploy: its stop runs before .env exists, and must find nothing to stop "
        "rather than fail (agent-cloud#890 B1)\n" + proc.stdout[-4000:])
    assert not [line for line in log.splitlines() if line.startswith("compose ") and " stop " in line], log
    assert len(_ups(log)) == 1, log
    record = json.loads((_state(host) / "deployed.json").read_text())
    assert record["release"]["commit"] == NEW and record["refused"] == [], record
    assert "is on" in discord, discord


def test_a_deploy_writes_nothing_of_its_record_into_the_mounted_workspace_directory(host):
    proc, _, _ = _deploy(host, _release(NEW, "a"), _udp_port(host))
    assert proc.returncode == 0, proc.stdout[-4000:]
    # The board mounts the workspace directory read-write. Whatever is in it, a compromised board
    # could have written; none of it may decide which image runs (agent-cloud#890 B2).
    for name in ("deployed.json", "release.replaced.json", "release-incoming", "deploying"):
        assert not list(_workspace(host).rglob(name)), f"{name} is under the mounted workspace directory"
    assert (_state(host) / "deployed.json").is_file()


def test_a_rollback_refuses_a_previous_image_from_outside_weft_s_registry(host):
    # A record naming an attacker's image as "the previous release": what a board that could write
    # the record would have written before B2.
    forged = _release(OLD, "b")
    forged["images"]["weft-workspace"] = f"docker.io/attacker/weft-workspace@sha256:{'c' * 64}"
    _state(host).mkdir(parents=True)
    (_state(host) / "deployed.json").write_text(json.dumps({"release": forged, "deployed_at": None, "refused": []}))
    (host / "unhealthy").write_text(f"sha256:{'a' * 64}")

    proc, log, discord = _deploy(host, _release(NEW, "a"), _udp_port(host))

    assert proc.returncode != 0, proc.stdout[-4000:]
    env = (host / "clone/platform/services/weft/deployment/.env").read_text()
    assert "attacker" not in env, "the rollback rendered an unverified image into .env:\n" + env
    assert len(_ups(log)) == 1, "only the new release may have been started:\n" + log
    assert "is down" in discord, "a person must be asked when the rollback cannot go back:\n" + discord
    record = json.loads((_state(host) / "deployed.json").read_text())
    assert NEW in record["refused"], record


def test_a_rollback_verifies_the_previous_release_then_runs_it_again(host):
    previous = _release(OLD, "b")
    _state(host).mkdir(parents=True)
    (_state(host) / "deployed.json").write_text(json.dumps({"release": previous, "deployed_at": None, "refused": []}))
    (host / "unhealthy").write_text(f"sha256:{'a' * 64}")

    proc, log, discord = _deploy(host, _release(NEW, "a"), _udp_port(host))

    # An automatic rollback fails the run on purpose, so a rollout stops at this host.
    assert proc.returncode != 0, proc.stdout[-4000:]
    lines = log.splitlines()
    ups = [i for i, line in enumerate(lines) if line in _ups(log)]
    assert len(ups) == 2, log
    image = previous["images"]["weft-workspace"]
    verified = [i for i, line in enumerate(lines) if " verify " in line and image in line]
    assert verified and verified[0] < ups[1], "the previous release must be verified before it starts:\n" + log
    env = (host / "clone/platform/services/weft/deployment/.env").read_text()
    assert previous["images"]["weft-workspace"] in env, env
    record = json.loads((_state(host) / "deployed.json").read_text())
    assert record["release"]["commit"] == OLD and NEW in record["refused"], record
    assert "rolled back to" in discord, discord


def _wiring(tmp_path: Path, playbook: str, play_name: str) -> dict:
    """The state and data directories a real play hands its deploy, evaluated by Ansible from the
    play's own vars and vars/weft.yml, with the inventory's defaults."""
    (play,) = [p for p in yaml.safe_load((PLAYBOOKS / playbook).read_text()) if p.get("name") == play_name]
    probe = [{
        "hosts": "localhost", "connection": "local", "gather_facts": False,
        "vars_files": [str(PLAYBOOKS / "vars/weft.yml")],
        "vars": {**play["vars"], "weft_workspace_id": "ws1"},
        "tasks": [{"ansible.builtin.debug": {
            "msg": "WIRING {{ {'state': _weft_state_dir, 'data': _weft_data_dir} | to_json }}"}}],
    }]
    path = tmp_path / f"wiring-{playbook}"
    path.write_text(yaml.safe_dump(probe, width=10**6))
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env.update(ANSIBLE_NOCOLOR="1", ANSIBLE_LOCAL_TEMP=str(tmp_path), ANSIBLE_REMOTE_TEMP=str(tmp_path))
    proc = subprocess.run(["ansible-playbook", "-i", "localhost,", str(path)], cwd=REPO, env=env,
                          text=True, capture_output=True, check=False, timeout=120)
    assert proc.returncode == 0, proc.stdout[-3000:]
    line = next(line for line in proc.stdout.splitlines() if "WIRING " in line)
    return json.loads(line.split("WIRING ", 1)[1].rstrip('"').replace('\\"', '"'))


@pytest.mark.parametrize("playbook,play_name", [
    ("weft-workspace.yml", "Workspaces, one at a time: deploy {{ weft_release | default('(unset)') }}"),
    ("weft-door.yml", "Front door: deploy {{ weft_release | default('(unset)') }}"),
    ("rollback-weft-workspace.yml", "Roll back one workspace by hand"),
])
def test_every_play_that_deploys_or_rolls_back_keeps_its_record_outside_the_mounted_directory(
        tmp_path, playbook, play_name):
    # The record decides which image runs. The data directory is what the role's containers mount
    # (the board's read-write), so the record must be neither it nor under it (agent-cloud#890 B2).
    wiring = _wiring(tmp_path, playbook, play_name)
    state, data = wiring["state"].rstrip("/") + "/", wiring["data"].rstrip("/") + "/"
    assert not state.startswith(data), f"{playbook}: the deploy record {state} is inside {data}"


def test_the_containers_mount_nothing_but_the_data_directory_the_socket_and_the_door_key():
    # The other half of the B2 property: the state directory is safe only because no service
    # mounts anything that could contain it.
    allowed = {"${WEFT_WORKSPACE_DIR}", "${WEFT_PODMAN_SOCKET}", "${WEFT_DOOR_DIR}", "./door.key"}
    for compose in ("compose.workspace.yml", "compose.door.yml"):
        services = yaml.safe_load((DEPLOY_DIR / compose).read_text())["services"]
        sources = {v.split(":", 1)[0] for svc in services.values() for v in svc.get("volumes", [])}
        assert sources <= allowed, f"{compose} mounts {sorted(sources - allowed)}"


def _release_guard(tmp_path: Path, state_dir: str):
    """tasks/weft-release.yml alone, in check mode, which fetches nothing."""
    play = [{
        "hosts": "localhost", "connection": "local", "gather_facts": False,
        "vars_files": [str(PLAYBOOKS / "vars/weft.yml")],
        "vars": {"weft_release": NEW, "local_mode": True, "_weft_role": "workspace",
                 "_weft_workspace_dir": str(tmp_path / "srv/weft/ws1"), "_weft_state_dir": state_dir},
        "tasks": [{"ansible.builtin.include_tasks": str(PLAYBOOKS / "tasks/weft-release.yml")}],
    }]
    playbook = tmp_path / "guard.yml"
    playbook.write_text(yaml.safe_dump(play))
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env.update(ANSIBLE_NOCOLOR="1", ANSIBLE_LOCAL_TEMP=str(tmp_path), ANSIBLE_REMOTE_TEMP=str(tmp_path))
    return subprocess.run(["ansible-playbook", "-i", "localhost,", "--check", str(playbook)], cwd=REPO, env=env,
                          text=True, capture_output=True, check=False, timeout=120)


@pytest.mark.parametrize("inside", ["srv/weft/ws1", "srv/weft/ws1/state", "srv/weft/ws1/../ws1/x"])
def test_a_state_directory_inside_the_mounted_workspace_directory_is_refused(tmp_path, inside):
    proc = _release_guard(tmp_path, str(tmp_path / inside))
    assert proc.returncode != 0, proc.stdout[-3000:]
    assert "no container may write it" in proc.stdout, proc.stdout[-3000:]


def test_a_state_directory_beside_the_mounted_workspace_directory_is_accepted(tmp_path):
    proc = _release_guard(tmp_path, str(tmp_path / "srv/weft-state/workspaces/ws1"))
    assert proc.returncode == 0, proc.stdout[-3000:]
