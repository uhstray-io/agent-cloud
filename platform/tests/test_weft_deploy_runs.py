"""weft's deploy, run for real against a fake container engine.

The structural suite (test_service_weft.bats) reads the playbooks; what a deploy does lives in the
ORDER of its steps, which a grep cannot see (agent-cloud#890):

- B1: the deploy stops the stack before it renders `.env`, and `deploy.sh` reads `.env` for most
  commands, so the first deploy to a fresh host must find nothing to stop rather than fail.
- A host is moved only to the release.json digest a person pinned in site-config, and a failed
  deploy fails the run without rolling anything back by itself: going back is a person's.

So these tests run the real `tasks/weft-deploy.yml`, `tasks/weft-verify-images.yml`,
`tasks/weft-release.yml` and the workspace's `deploy.sh` on this machine. What needs the network
is replaced at its own seam: the release fetch (`tasks/weft-release.yml`, which talks to the
registry) hands over a release.json in the deploy tests, and is run for real against a fake
registry in the fetch tests. `podman` is a script that logs its arguments and answers `inspect`;
its `run` of a `health` one-off fails for one chosen digest, which is how a release is made
unhealthy.
"""

import grp
import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
import yaml
from ansible_python import needs_ansible
from harness_sandbox import run_playbook

REPO = Path(__file__).resolve().parents[2]
PLAYBOOKS = REPO / "platform/playbooks"
WS_DIR = REPO / "platform/services/weft-workspace/deployment"
DOOR_DIR = REPO / "platform/services/weft-door/deployment"
REGISTRY = "ghcr.io/uhstray-io"
SIGNER = "https://github.com/uhstray-io/weft/.github/workflows/images.yml@refs/heads/main"
WORKSPACE_IMAGES = ["weft-workspace", "weft-builder", "weft-proxy", "weft-harness-claude-code", "weft-harness-opencode"]
NEW = "a" * 40
OLD = "b" * 40
DIGEST = "sha256:" + "d" * 64
# A local-dev inventory (every host but the controller named <name>-local, plan 24), so the
# release fetch skips the registry login, which reads OpenBao. The plays target localhost alone.
LOCAL_DEV = "localhost,weft-local,"

pytestmark = [needs_ansible]


def _release(commit: str, digit: str, registry: str = REGISTRY, version: str | None = None) -> dict:
    return {"format": 1, "commit": commit, "version": version, "signed_by": {"identity": SIGNER},
            "images": {name: f"{registry}/{name}@sha256:{digit * 64}" for name in WORKSPACE_IMAGES}}


FAKE_PODMAN = """#!/bin/bash
echo "$*" >> "{log}"
case "$1" in
  inspect)
    case "$*" in
      *ImageName*)
        # What the host runs: the image in {running}, or nothing.
        if [ -s "{running}" ]; then echo "$(cat "{running}") true"; else exit 1; fi ;;
      *Health*) echo healthy ;;
      *RestartCount*) echo 0 ;;
    esac ;;
  run)
    # A health one-off of the image named in {fail} fails: that is the unhealthy release.
    if [ -s "{fail}" ] && [[ "$*" == *"$(cat "{fail}")"* ]] && [[ "$*" == *" health"* ]]; then
      exit 1
    fi
    # A backup one-off prints its set's path, as the binary does.
    if [[ "$*" == *" backup "* ]]; then echo "/srv/weft/ws1/deploys/set-1"; fi ;;
esac
exit 0
"""

# The fetch talks to a registry; the release arrives as a value instead, and the real image check
# still runs on it.
RELEASE_STUB = [
    {"name": "Stub: the fetched release", "ansible.builtin.set_fact": {
        "_weft_release_json": "{{ test_release }}", "_weft_auth_file": "",
        "_weft_release_digest": DIGEST}},
    {"name": "Stub: its images, checked by the real task",
     "ansible.builtin.include_tasks": "weft-verify-images.yml",
     "vars": {"_wvi_release": "{{ _weft_release_json }}"}},
    {"name": "Stub: take the images",
     "ansible.builtin.set_fact": {"_weft_images": "{{ _weft_release_json.images }}",
                                  "_weft_release_name": "{{ _weft_release_json.version or _weft_release_json.commit }}"}},
    {"name": "Stub: the real header check", "ansible.builtin.include_tasks": "weft-board-header.yml"},
]
# What a release that carries weft #511 lists among the keys its loader accepts.
HEADER_KEYS = {"deployment_file_keys": ["board", "board.trusted_proxies", "board.client_address_header"]}


@pytest.fixture
def host(tmp_path):
    """A fresh workspace host: no .env, no weft.toml, nothing running."""
    pb = tmp_path / "pb"
    (pb / "tasks").mkdir(parents=True)
    for name in ("weft-deploy.yml", "weft-verify-images.yml", "verify-cosign.yml", "weft-workspace-files.yml",
                 "weft-whisper-model.yml", "weft-board-header.yml", "weft-lock.yml"):
        shutil.copy(PLAYBOOKS / "tasks" / name, pb / "tasks" / name)
    (pb / "tasks" / "weft-release.yml").write_text(yaml.safe_dump(RELEASE_STUB))
    # Templates are read from {{ playbook_dir }}/../services/weft-<role>/deployment/templates.
    (tmp_path / "services").symlink_to(REPO / "platform/services")
    clone = tmp_path / "clone/platform"
    shutil.copytree(WS_DIR, clone / "services/weft-workspace/deployment", ignore=shutil.ignore_patterns(".env"))
    (clone / "lib").symlink_to(REPO / "platform/lib")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "podman").write_text(FAKE_PODMAN.format(
        log=tmp_path / "engine.log", fail=tmp_path / "unhealthy", running=tmp_path / "running"))
    (bindir / "podman").chmod(0o755)
    return tmp_path


def _with_model(tmp_path: Path, release: dict) -> dict:
    """The release with the whisper model it pins, served from a file and checked against its sum by
    the real weft-workspace-files.yml, which also renders weft.toml."""
    model = tmp_path / "model.bin"
    model.write_bytes(b"not a real model")
    return {**release, "whisper": {"default": "tiny", "models": {"tiny": {
        "url": model.as_uri(), "file": "model.bin", "sha256": hashlib.sha256(model.read_bytes()).hexdigest()}}}}


def _deploy(tmp_path: Path, release: dict, extra: dict | None = None):
    srv = tmp_path / "srv"
    release = _with_model(tmp_path, release)
    play = [{
        "hosts": "localhost", "connection": "local", "gather_facts": False,
        "vars_files": [str(PLAYBOOKS / "vars/weft.yml")],
        "vars": {
            "weft_release": f"{release['commit']}@{DIGEST}", "weft_workspace_id": "ws1", "weft_voice_udp": 40000,
            "weft_root": str(srv / "weft"), "weft_state_root": str(srv / "weft-state"),
            "weft_health_minutes": 0, "weft_watch_minutes": 0, "weft_board_bind": "127.0.0.1",
            "_weft_role": "workspace", "_weft_workspace_dir": str(srv / "weft/ws1"),
            "_weft_state_dir": "{{ _weft_workspace_state_dir }}",
            "_weft_deploy_dir": str(tmp_path / "clone/platform/services/weft-workspace/deployment"),
            "_weft_label": "workspace ws1",
            "_weft_podman_socket": "/run/user/0/podman/podman.sock",
            "_weft_deploy_env": {"CONTAINER_ENGINE": "podman", "COMPOSE_CMD": "podman compose", "LOCAL_MODE": ""},
            "_weft_health_argv": ["bash", "deploy.sh", "health"],
            "test_release": release,
            # What weft.toml.j2 reads.
            "weft_domain": "weft.example.test", "weft_public_address": "127.0.0.1",
            "weft_default_provider": "p", "weft_daily_tokens": 1000, "weft_tracker_bot_login": "bot",
            "weft_providers": {"p": {"name": "p", "endpoint": "https://p.example.test", "wire": "openai",
                                     "budget": 1, "models": {"standard": "m"}}},
            "_shared": {"public_key": "0" * 64},
        },
        "tasks": [
            {"name": "The directories weft-workspace.yml makes", "ansible.builtin.file": {
                "path": "{{ item }}", "state": "directory", "mode": "0755"},
             "loop": ["{{ _weft_workspace_dir }}", "{{ _weft_state_dir }}"]},
            {"name": "One deploy", "ansible.builtin.include_tasks": "tasks/weft-deploy.yml"},
            # What Deploy weft Staging does after a deploy that passed: record it on GitHub. A
            # failed deploy ends the host's run, so this must never be reached after one.
            {"name": "Stand-in for the staging GitHub record", "ansible.builtin.copy": {
                "content": "recorded\n", "dest": str(tmp_path / "github-record"), "mode": "0644"}},
        ],
    }]
    playbook = tmp_path / "pb/run.yml"
    playbook.write_text(yaml.safe_dump(play, width=10**6))
    env = {"PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}", "ANSIBLE_NOCOLOR": "1"}
    # The state directory is root's on a VM, written with become. Here the run is a user's: the owner
    # is this user and escalation is off, which a launch cannot do (both are internal names, refused
    # as extra vars by refuse-internal-extra-vars.yml, or connection variables).
    me = {"_weft_state_owner": os.environ.get("USER") or str(os.getuid()),
          "_weft_state_group": grp.getgrgid(os.getgid()).gr_name, "ansible_become": False, **(extra or {})}
    proc = run_playbook(playbook, tmp_path, "-e", json.dumps(me), env=env, timeout=180)
    log = tmp_path / "engine.log"
    return proc, (log.read_text() if log.exists() else "")


def _state(tmp_path: Path) -> Path:
    return tmp_path / "srv/weft-state/workspaces/ws1"


def _workspace(tmp_path: Path) -> Path:
    return tmp_path / "srv/weft/ws1"


def _env(tmp_path: Path) -> str:
    return (tmp_path / "clone/platform/services/weft-workspace/deployment/.env").read_text()


def _ups(log: str) -> list[str]:
    return [line for line in log.splitlines()
            if line.startswith("compose ") and " up " in line and line.endswith(" board")]


def _stops(log: str) -> list[str]:
    return [line for line in log.splitlines() if line.startswith("compose ") and " stop " in line]


def test_the_first_deploy_to_a_host_with_no_env_reaches_up(host):
    proc, log = _deploy(host, _release(NEW, "a"))
    assert proc.returncode == 0, (
        "a fresh host must deploy: its stop runs before .env exists, and must find nothing to stop "
        "rather than fail (agent-cloud#890 B1)\n" + proc.stdout[-4000:])
    assert not _stops(log), log
    assert len(_ups(log)) == 1, log
    # weft.toml names the agent images and the commands the orchestrator runs: it is rendered into
    # the state directory, and nothing of it lands where the board can write.
    toml = (_state(host) / "weft.toml").read_text()
    assert toml.splitlines()[0] == f"# release.json: {DIGEST}", toml[:200]
    assert f"{REGISTRY}/weft-builder@sha256:{'a' * 64}" in toml, toml
    assert not list(_workspace(host).rglob("weft.toml")), "weft.toml is under the mounted workspace directory"
    env = _env(host)
    assert f"WEFT_WORKSPACE_CONFIG={_state(host) / 'weft.toml'}" in env, env
    assert f"WEFT_RELEASE={NEW}" in env, env
    assert (_state(host) / "deploy.state").read_text() == f"healthy {DIGEST}\n"
    assert (host / "github-record").exists()


def _board_table(tmp_path: Path) -> list[str]:
    toml = (_state(tmp_path) / "weft.toml").read_text()
    board = toml.split("[board]\n", 1)[1].split("\n[", 1)[0]
    return [line for line in board.splitlines() if line and not line.startswith("#")]


PROXY = {"weft_board_trusted_proxies": ["192.0.2.5/32"]}


def test_trusted_proxies_and_the_client_address_header_are_rendered_together(host):
    # From the weft release that carries weft #511, the board refuses trusted_proxies without
    # client_address_header: one without the other is a workspace that never comes up.
    proc, _ = _deploy(host, {**_release(NEW, "a"), **HEADER_KEYS}, PROXY)
    assert proc.returncode == 0, proc.stdout[-4000:]
    board = _board_table(host)
    assert 'trusted_proxies = ["192.0.2.5/32"]' in board, board
    assert 'client_address_header = "CF-Connecting-IP"' in board, (
        "every weft name is proxied by Cloudflare, so the default header is CF-Connecting-IP:\n" + "\n".join(board))


def test_with_no_trusted_proxies_neither_key_is_rendered(host):
    proc, _ = _deploy(host, {**_release(NEW, "a"), **HEADER_KEYS})
    assert proc.returncode == 0, proc.stdout[-4000:]
    board = _board_table(host)
    assert not [line for line in board if line.startswith(("trusted_proxies", "client_address_header"))], board


@pytest.mark.parametrize("keys", [{"deployment_file_keys": ["board", "board.trusted_proxies"]},
                                  {"deployment_file_keys": None}, {}],
                         ids=["release-before-511", "keys-not-listed", "no-keys-field"])
def test_trusted_proxies_on_a_release_that_cannot_name_the_header_are_refused_before_the_stop(host, keys):
    # A release before weft #511 refuses client_address_header as an unknown key, and without it a
    # release after refuses trusted_proxies: either way the board would not start. null means nobody
    # listed the keys, which is not a yes.
    proc, log = _deploy(host, {**_release(NEW, "a"), **keys}, PROXY)
    assert proc.returncode != 0, proc.stdout[-4000:]
    assert "predates weft's client-address header" in proc.stdout, proc.stdout[-4000:]
    assert not _stops(log) and not _ups(log), "the host was touched before the refusal:\n" + log


def test_a_release_before_the_header_deploys_when_no_proxy_is_trusted(host):
    proc, _ = _deploy(host, _release(NEW, "a"))
    assert proc.returncode == 0, proc.stdout[-4000:]
    assert not [line for line in _board_table(host) if line.startswith("client_address_header")]


def test_a_host_already_on_its_pin_is_left_alone(host):
    # Waves come from per-host pins, so a rollout reaches hosts whose pins did not change; each of
    # them must be left running, not stopped, backed up and restarted.
    release = _release(NEW, "a")
    proc, _ = _deploy(host, release)
    assert proc.returncode == 0, proc.stdout[-4000:]
    (host / "running").write_text(release["images"]["weft-workspace"])
    (host / "engine.log").unlink()

    proc, log = _deploy(host, release)

    assert proc.returncode == 0, proc.stdout[-4000:]
    assert "already runs" in proc.stdout, proc.stdout[-4000:]
    assert not _stops(log) and not _ups(log), "a host on its pin was redeployed:\n" + log


def test_a_host_on_its_pin_is_redeployed_when_asked(host):
    release = _release(NEW, "a")
    _deploy(host, release)
    (host / "running").write_text(release["images"]["weft-workspace"])
    (host / "engine.log").unlink()

    proc, log = _deploy(host, release, {"weft_force_redeploy": True})

    assert proc.returncode == 0, proc.stdout[-4000:]
    assert len(_ups(log)) == 1, log


def test_a_failed_health_check_fails_the_run_and_rolls_nothing_back(host):
    # The release to go back to is the host's previous pin, in site-config's history, and a
    # person chooses it. So the deploy fails, leaves the host as it is, and says how to go back.
    proc, _ = _deploy(host, _release(OLD, "b"))
    assert proc.returncode == 0, proc.stdout[-4000:]
    (host / "running").write_text(_release(OLD, "b")["images"]["weft-workspace"])
    (_workspace(host) / "weft.db").write_bytes(b"catalog")
    (host / "engine.log").unlink()
    (host / "unhealthy").write_text(f"sha256:{'a' * 64}")

    proc, log = _deploy(host, _release(NEW, "a"))

    assert proc.returncode != 0, proc.stdout[-4000:]
    assert len(_ups(log)) == 1, "only the new release may have been started:\n" + log
    assert " restore " not in log, "a deploy restored by itself:\n" + log
    assert "Roll Back weft Host" in proc.stdout and "/srv/weft/ws1/deploys/set-1" in proc.stdout, proc.stdout[-4000:]
    backups = [line for line in log.splitlines() if " backup " in line]
    assert backups and f"--from {OLD} --to {NEW}" in backups[0], log
    assert f"WEFT_RELEASE={NEW}" in _env(host)
    assert (_state(host) / "deploy.state").read_text() == f"deploying {DIGEST}\n", \
        "a deploy that failed its health check must not leave the host marked healthy"


def test_a_host_that_failed_its_deploy_is_deployed_again_on_relaunch(host):
    # A failed host runs the pinned image, so the image alone cannot say it is done: a re-launch
    # must deploy it again, and the staging GitHub record must still not be written.
    release = _release(NEW, "a")
    (host / "unhealthy").write_text(f"sha256:{'a' * 64}")
    proc, _ = _deploy(host, release)
    assert proc.returncode != 0, proc.stdout[-4000:]
    (host / "running").write_text(release["images"]["weft-workspace"])
    (host / "engine.log").unlink()

    proc, log = _deploy(host, release)

    assert proc.returncode != 0, proc.stdout[-4000:]
    assert "already runs" not in proc.stdout, "a host whose deploy failed was taken as done:\n" + proc.stdout[-4000:]
    assert len(_ups(log)) == 1, "the re-launch must deploy the failed host again:\n" + log
    assert not (host / "github-record").exists(), "a failed deploy was followed by the staging GitHub record"


def test_a_second_deploy_or_rollback_is_refused_while_one_holds_the_host(host):
    # Semaphore does not serialise templates, so a deploy and a person's rollback of one host can
    # be launched together (agent-cloud#891, N1). The second must be refused by name before it
    # stops anything, not queued and not interleaved.
    _state(host).mkdir(parents=True)
    holder = subprocess.Popen(["flock", str(_state(host) / "deploy.lock"), "sleep", "120"])
    try:
        deadline = time.monotonic() + 10
        while subprocess.run(["flock", "--nonblock", str(_state(host) / "deploy.lock"), "true"],
                             timeout=30).returncode == 0:
            assert time.monotonic() < deadline, "the test's own holder never took the lock"
            time.sleep(0.2)
        proc, log = _deploy(host, _release(NEW, "a"))
    finally:
        holder.kill()
        holder.wait()
    assert proc.returncode != 0, proc.stdout[-4000:]
    assert "Another weft deploy or rollback holds" in proc.stdout, proc.stdout[-4000:]
    assert not _stops(log) and not _ups(log), "the refused run touched the host:\n" + log
    assert not (_state(host) / "deploy.state").exists(), "the refused run marked the host deploying"


def test_a_finished_deploy_releases_the_host(host):
    proc, _ = _deploy(host, _release(NEW, "a"))
    assert proc.returncode == 0, proc.stdout[-4000:]
    # The holder exits when its sleep is killed; give the kernel a moment to see it go.
    deadline = time.monotonic() + 10
    while subprocess.run(["flock", "--nonblock", str(_state(host) / "deploy.lock"), "true"],
                         timeout=30).returncode != 0:
        assert time.monotonic() < deadline, "a deploy that finished still holds its host's lock"
        time.sleep(0.2)


def test_a_host_marked_healthy_at_another_digest_is_deployed(host):
    release = _release(NEW, "a")
    _deploy(host, release)
    (host / "running").write_text(release["images"]["weft-workspace"])
    (_state(host) / "deploy.state").write_text(f"healthy sha256:{'e' * 64}\n")
    (host / "engine.log").unlink()

    proc, log = _deploy(host, release)

    assert proc.returncode == 0, proc.stdout[-4000:]
    assert len(_ups(log)) == 1, log
    assert (_state(host) / "deploy.state").read_text() == f"healthy {DIGEST}\n"


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
    proc = run_playbook(path, tmp_path, env={"ANSIBLE_NOCOLOR": "1"})
    assert proc.returncode == 0, proc.stdout[-3000:]
    line = next(line for line in proc.stdout.splitlines() if "WIRING " in line)
    return json.loads(line.split("WIRING ", 1)[1].rstrip('"').replace('\\"', '"'))


@pytest.mark.parametrize("playbook,play_name", [
    ("weft-workspace.yml", "Workspaces, one at a time, each to its pin"),
    ("weft-door.yml", "Front door: deploy to its pin"),
    ("rollback-weft-host.yml", "Roll back one weft host by hand"),
])
def test_every_play_that_deploys_or_rolls_back_keeps_weft_toml_outside_the_mounted_directory(
        tmp_path, playbook, play_name):
    # weft.toml decides what the orchestrator runs. The data directory is what the role's
    # containers mount (the board's read-write), so the state directory must be neither it nor
    # under it.
    wiring = _wiring(tmp_path, playbook, play_name)
    state, data = wiring["state"].rstrip("/") + "/", wiring["data"].rstrip("/") + "/"
    assert not state.startswith(data), f"{playbook}: the state directory {state} is inside {data}"


def test_the_containers_mount_nothing_but_the_data_directory_the_socket_the_key_and_weft_toml():
    # The state directory is safe only because no service mounts anything that could contain it,
    # and the one file of it a service needs is read-only.
    allowed = {"${WEFT_WORKSPACE_DIR}", "${WEFT_PODMAN_SOCKET}", "${WEFT_DOOR_DIR}", "./door.key",
               "${WEFT_WORKSPACE_CONFIG}"}
    for compose in (WS_DIR / "compose.yml", DOOR_DIR / "compose.yml"):
        services = yaml.safe_load(compose.read_text())["services"]
        sources = {v.split(":", 1)[0] for svc in services.values() for v in svc.get("volumes", [])}
        assert sources <= allowed, f"{compose} mounts {sorted(sources - allowed)}"
        for name, svc in services.items():
            for v in svc.get("volumes", []):
                if v.startswith("${WEFT_WORKSPACE_CONFIG}"):
                    assert v.endswith(":ro"), f"{compose}: {name} mounts weft.toml writable"
            if (svc.get("environment") or {}).get("WEFT_CONFIG"):
                assert svc["environment"]["WEFT_CONFIG"] == "${WEFT_WORKSPACE_CONFIG}", name
    deploy_sh = (WS_DIR / "deploy.sh").read_text()
    assert '-v "${WEFT_WORKSPACE_CONFIG}:${WEFT_WORKSPACE_CONFIG}:ro"' in deploy_sh
    assert "WEFT_WORKSPACE_CONFIG" in deploy_sh.split("REQUIRED=(WEFT_WORKSPACE_IMAGE", 1)[1].split(")", 1)[0]


STATE_NAMES = ("_weft_state_dir", "_weft_door_state_dir", "_weft_workspace_config")
WRITERS = ("ansible.builtin.copy", "ansible.builtin.template", "ansible.builtin.file")


def _tasks(items):
    for t in items or []:
        if not isinstance(t, dict):
            continue
        yield t
        for key in ("block", "rescue", "always", "tasks"):
            yield from _tasks(t.get(key))


def test_every_write_to_the_state_directory_escalates_and_leaves_it_root_s():
    # The orchestrator holds the deploy user's podman socket, so a file the deploy user can write is
    # one the orchestrator can rewrite: weft.toml would then name its own agent images. Every write
    # into the state directory runs as root and sets root's ownership.
    files = [*sorted(PLAYBOOKS.glob("tasks/weft-*.yml")), PLAYBOOKS / "weft-workspace.yml",
             PLAYBOOKS / "weft-door.yml", PLAYBOOKS / "rollback-weft-host.yml"]
    writes, bad = 0, []
    for f in files:
        for task in _tasks(yaml.safe_load(f.read_text())):
            module = next((m for m in WRITERS if m in task), None)
            if module is None:
                continue
            body = task[module]
            if not any(name in json.dumps(body) for name in STATE_NAMES):
                continue
            writes += 1
            if task.get("become") is not True:
                bad.append(f"{f.name}: {task.get('name')} does not escalate")
            if body.get("owner") != "{{ _weft_state_owner }}":
                bad.append(f"{f.name}: {task.get('name')} does not set the state owner")
            if str(body.get("mode")) not in ("0644", "0755"):
                bad.append(f"{f.name}: {task.get('name')} mode {body.get('mode')}")
    assert writes >= 2, writes
    assert not bad, bad
    weft_vars = yaml.safe_load((PLAYBOOKS / "vars/weft.yml").read_text())
    assert weft_vars["_weft_state_owner"] == "root" and weft_vars["_weft_state_group"] == "root"


def _release_guard(tmp_path: Path, state_dir: str, release: str = f"{NEW}@{DIGEST}", environment: str = "rack"):
    """tasks/weft-release.yml alone, in check mode, which fetches nothing."""
    play = [{
        "hosts": "localhost", "connection": "local", "gather_facts": False,
        "vars_files": [str(PLAYBOOKS / "vars/weft.yml")],
        "vars": {"weft_release": release, "weft_environment": environment,
                 "_weft_role": "workspace", "_weft_workspace_dir": str(tmp_path / "srv/weft/ws1"),
                 "_weft_state_dir": state_dir},
        "tasks": [{"ansible.builtin.include_tasks": str(PLAYBOOKS / "tasks/weft-release.yml")}],
    }]
    playbook = tmp_path / "guard.yml"
    playbook.write_text(yaml.safe_dump(play))
    return run_playbook(playbook, tmp_path, "--check", inventory=LOCAL_DEV, env={"ANSIBLE_NOCOLOR": "1"})


@pytest.mark.parametrize("inside", ["srv/weft/ws1", "srv/weft/ws1/state", "srv/weft/ws1/../ws1/x"])
def test_a_state_directory_inside_the_mounted_workspace_directory_is_refused(tmp_path, inside):
    proc = _release_guard(tmp_path, str(tmp_path / inside), release=f"v1.2.3@{DIGEST}")
    assert proc.returncode != 0, proc.stdout[-3000:]
    assert "no container may write it" in proc.stdout, proc.stdout[-3000:]


GOOD_STATE = "srv/weft-state/workspaces/ws1"


@pytest.mark.parametrize("pin,environment", [
    (f"v1.2.3@{DIGEST}", "rack"),
    (f"v1.2.3@{DIGEST}", "staging"),
    (f"{NEW}@{DIGEST}", "staging"),
    (NEW, "staging"),
])
def test_a_pin_to_a_signed_digest_is_accepted(tmp_path, pin, environment):
    proc = _release_guard(tmp_path, str(tmp_path / GOOD_STATE), release=pin, environment=environment)
    assert proc.returncode == 0, proc.stdout[-3000:]


@pytest.mark.parametrize("pin,environment", [
    ("v1.2.3", "rack"),               # a tag: a name that can move after the pin
    ("main", "staging"),              # a branch
    (NEW[:12], "staging"),            # a short commit
    (NEW, "rack"),                    # the rack takes a digest, never a bare commit
    (f"{NEW}@{DIGEST}", "rack"),      # the rack runs releases, never a commit
    (f"v1.2.3@sha256:{'D' * 64}", "rack"),
])
def test_a_pin_that_is_not_a_signed_digest_is_refused_before_anything_runs(tmp_path, pin, environment):
    proc = _release_guard(tmp_path, str(tmp_path / GOOD_STATE), release=pin, environment=environment)
    assert proc.returncode != 0, proc.stdout[-3000:]
    assert "is a name that can move" in proc.stdout, proc.stdout[-3000:]


# ---------------------------------------------------------------------------------------------
# The verifiers (agent-cloud#743 second review, H1). cosign and oras run from root's image store,
# and what oras fetches is hashed on the controller against the digest it was asked for, so a
# tampered oras, or one in a store the orchestrator can write, cannot hand the deploy a different
# release.json than the one whose signature was verified.

FAKE_ORAS_PODMAN = """#!/bin/bash
echo "$*" >> "{log}"
case "$*" in
  *" resolve "*) cat "{resolved}" ;;
  *" manifest fetch "*) cat "{manifest}" ;;
  *" blob fetch "*) cat "{blob}" ;;
esac
exit 0
"""


def _registry(tmp_path: Path, *, manifest_tamper: bool = False, blob_tamper: bool = False,
              release: dict | None = None) -> tuple[Path, str]:
    """A fake registry behind a fake podman: the honest release.json, its manifest, and the digest
    resolve returns. Tampering changes what is served after the digests were taken. Returns the
    bin directory and the manifest's digest, which is what a pin names."""
    blob = json.dumps(release or _release(NEW, "a")).encode()
    manifest = json.dumps({"schemaVersion": 2, "layers": [{
        "mediaType": "application/json", "digest": "sha256:" + hashlib.sha256(blob).hexdigest(),
        "size": len(blob), "annotations": {"org.opencontainers.image.title": "release.json"}}]}).encode()
    digest = "sha256:" + hashlib.sha256(manifest).hexdigest()
    (tmp_path / "resolved").write_text(digest + "\n")
    # A newline after the JSON is the change the command module would strip; it must still be caught.
    (tmp_path / "manifest").write_bytes(manifest + (b"\n" if manifest_tamper else b""))
    (tmp_path / "blob").write_bytes(blob.replace(b"weft-builder", b"weft-builderX") if blob_tamper else blob)
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "podman").write_text(FAKE_ORAS_PODMAN.format(
        log=tmp_path / "engine.log", resolved=tmp_path / "resolved",
        manifest=tmp_path / "manifest", blob=tmp_path / "blob"))
    (bindir / "podman").chmod(0o755)
    return bindir, digest


def _fetch(tmp_path: Path, bindir: Path, release: str, environment: str = "staging"):
    """tasks/weft-release.yml for real, against the fake registry."""
    play = [{
        "hosts": "localhost", "connection": "local", "gather_facts": False,
        "vars_files": [str(PLAYBOOKS / "vars/weft.yml")],
        "vars": {"weft_release": release, "weft_environment": environment,
                 "_weft_role": "workspace", "_weft_workspace_dir": str(tmp_path / "srv/weft/ws1"),
                 "_weft_state_dir": str(tmp_path / GOOD_STATE)},
        "tasks": [{"ansible.builtin.include_tasks": str(PLAYBOOKS / "tasks/weft-release.yml")},
                  {"ansible.builtin.debug": {"msg": "COMMIT {{ _weft_release_json.commit }}"}}],
    }]
    playbook = tmp_path / "fetch.yml"
    playbook.write_text(yaml.safe_dump(play, width=10**6))
    env = {"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}", "ANSIBLE_NOCOLOR": "1"}
    return run_playbook(playbook, tmp_path, "-e", json.dumps({"ansible_become": False}),
                        inventory=LOCAL_DEV, env=env)


def test_a_pinned_release_json_is_fetched_by_its_digest_and_never_resolved(tmp_path):
    bindir, digest = _registry(tmp_path, release=_release(NEW, "a", version="v1.2.3"))
    proc = _fetch(tmp_path, bindir, f"v1.2.3@{digest}", environment="rack")
    assert proc.returncode == 0, proc.stdout[-4000:]
    assert f"COMMIT {NEW}" in proc.stdout
    log = (tmp_path / "engine.log").read_text()
    assert " resolve " not in log, "a pinned release looked a tag up:\n" + log
    assert f"weft-release@{digest}" in log, log


def test_a_staging_commit_is_resolved_then_verified(tmp_path):
    bindir, _ = _registry(tmp_path)
    proc = _fetch(tmp_path, bindir, NEW)
    assert proc.returncode == 0, proc.stdout[-4000:]
    assert f"COMMIT {NEW}" in proc.stdout


def test_a_pin_whose_release_json_names_another_version_is_refused(tmp_path):
    # The digest is signed, so it is weft's; the version must still be the one the person wrote.
    bindir, digest = _registry(tmp_path, release=_release(NEW, "a", version="v1.2.4"))
    proc = _fetch(tmp_path, bindir, f"v1.2.3@{digest}", environment="rack")
    assert proc.returncode != 0, proc.stdout[-4000:]
    assert "does not describe v1.2.3" in proc.stdout, proc.stdout[-4000:]
    assert " pull " not in (tmp_path / "engine.log").read_text()


@pytest.mark.parametrize("tamper", ["manifest", "blob"])
def test_oras_output_that_does_not_match_its_digest_is_refused(tmp_path, tamper):
    bindir, digest = _registry(tmp_path, manifest_tamper=tamper == "manifest", blob_tamper=tamper == "blob")
    proc = _fetch(tmp_path, bindir, f"{NEW}@{digest}")
    assert proc.returncode != 0, proc.stdout[-4000:]
    assert "does not hash to" in proc.stdout, proc.stdout[-4000:]
    assert "COMMIT" not in proc.stdout
    log = (tmp_path / "engine.log").read_text()
    assert " pull " not in log, "an image was pulled on the strength of an unverified release.json:\n" + log


def test_every_verifier_runs_as_root_from_root_s_image_store():
    # A rootless store is writable by anything holding that user's podman socket, and podman does
    # not re-verify a stored digest's layers: a verifier from it could lie.
    cosign = yaml.safe_load((PLAYBOOKS / "tasks/verify-cosign.yml").read_text())
    (verify,) = [t for t in cosign if t.get("name") == "Verify each signature against the pinned identity"]
    assert verify.get("become") is True
    release = yaml.safe_load((PLAYBOOKS / "tasks/weft-release.yml").read_text())
    oras = [t for t in release if "_weft_oras_image" in json.dumps(t)]
    assert len(oras) == 3, [t.get("name") for t in oras]
    assert all(t.get("become") is True for t in oras), [t.get("name") for t in oras if t.get("become") is not True]
