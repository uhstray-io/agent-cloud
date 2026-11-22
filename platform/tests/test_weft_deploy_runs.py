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
import ipaddress
import json
import os
import shutil
import subprocess
from contextlib import ExitStack
from pathlib import Path

import pytest
import yaml
from ansible_python import needs_ansible
from fake_http import DrainingHandler, serve
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
    {"name": "Stub: the real bind check", "ansible.builtin.include_tasks": "weft-bind.yml"},
]
# What a release that carries weft #511 lists among the keys its loader accepts.
HEADER_KEYS = {"deployment_file_keys": ["board", "board.trusted_proxies", "board.client_address_header"]}


@pytest.fixture
def host(tmp_path):
    """A fresh workspace host: no .env, no weft.toml, nothing running."""
    pb = tmp_path / "pb"
    (pb / "tasks").mkdir(parents=True)
    for name in ("weft-deploy.yml", "weft-verify-images.yml", "verify-cosign.yml", "weft-workspace-files.yml",
                 "weft-whisper-model.yml", "weft-board-header.yml", "weft-bind.yml"):
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
            "weft_release": f"{release['commit']}@{DIGEST}", "weft_workspace_id": "ws1",
            "weft_root": str(srv / "weft"), "weft_state_root": str(srv / "weft-state"),
            "weft_health_minutes": 0, "weft_watch_minutes": 0, "weft_board_bind": "192.0.2.10",
            "_weft_role": "workspace", "_weft_workspace_dir": str(srv / "weft/ws1"),
            "_weft_state_dir": "{{ _weft_workspace_state_dir }}",
            "_weft_deploy_dir": str(tmp_path / "clone/platform/services/weft-workspace/deployment"),
            "_weft_label": "workspace ws1",
            "_weft_podman_socket": "/run/user/0/podman/podman.sock",
            "_weft_deploy_env": {"CONTAINER_ENGINE": "podman", "COMPOSE_CMD": "podman compose", "LOCAL_MODE": ""},
            "_weft_health_argv": ["bash", "deploy.sh", "health"],
            "test_release": release,
            # What weft.toml.j2 reads.
            "weft_domain": "weft.example.test",
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


def test_a_workspace_holds_no_call_port_while_calls_are_off_on_the_rack(host):
    # weft D578: every weft name is proxied by Cloudflare, which carries no UDP, so live calls are
    # off on the rack until a media route is chosen. A [voice] table would start weft-voice and tell
    # browsers to send audio to a port nothing forwards, and a published UDP port is an opening no
    # firewall rule was made for. The deploy above declares no weft_voice_udp and must still pass.
    proc, _ = _deploy(host, _release(NEW, "a"))
    assert proc.returncode == 0, proc.stdout[-4000:]
    toml = (_state(host) / "weft.toml").read_text()
    assert not [line for line in toml.splitlines() if line.strip().startswith("[voice")], (
        "a rack workspace's weft.toml names no [voice] while calls are off (D578):\n" + toml)
    assert "WEFT_VOICE_UDP" not in _env(host), _env(host)
    ports = yaml.safe_load((WS_DIR / "compose.yml").read_text())["services"]["board"]["ports"]
    assert not [port for port in ports if str(port).endswith("/udp")], (
        "the board publishes no UDP port while calls are off on the rack (D578): " + str(ports))


@pytest.mark.parametrize("bind", ["2001:db8::10", "[2001:db8::10]"])
def test_a_workspace_on_an_ipv6_address_is_published_with_it_bracketed(host, bind):
    # weft #585: compose.yml publishes "${WEFT_BOARD_BIND}:${WEFT_BOARD_PORT}:8080", and podman
    # refuses `2001:db8::10:8080:8080` ("invalid port format"); bracketed it is that address.
    proc, log = _deploy(host, _release(NEW, "a"), {"weft_board_bind": bind})
    assert proc.returncode == 0, proc.stdout[-4000:]
    assert "WEFT_BOARD_BIND=[2001:db8::10]\n" in _env(host), _env(host)
    assert len(_ups(log)) == 1, log


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


def _ingest_from(board: list[str]) -> list[str]:
    [line] = [line for line in board if line.startswith("ingest_from = ")]
    return json.loads(line.removeprefix("ingest_from = "))


@pytest.mark.parametrize("site, expected", [
    ({}, ["127.0.0.1/32", "::1/128"]),
    ({"weft_board_ingest_from": ["127.0.0.1/32", "192.0.2.0/24"]}, ["127.0.0.1/32", "192.0.2.0/24", "::1/128"]),
], ids=["no-site-list", "site-lists-loopback"])
def test_the_board_healthcheck_is_always_admitted_from_loopback(host, site, expected):
    # compose.yml's healthcheck fetches /api/workspace from inside the board's container, so the
    # board sees 127.0.0.1, and weft's gate admits nobody its ingest_from does not name. Without
    # loopback every deploy fails wait_for_healthy; listed twice is noise a reader must explain.
    proc, _ = _deploy(host, _release(NEW, "a"), site)
    assert proc.returncode == 0, proc.stdout[-4000:]
    assert _ingest_from(_board_table(host)) == expected, (
        "loopback must be in ingest_from exactly once, beside site-config's list:\n" + "\n".join(_board_table(host)))


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


def _door_env(tmp_path: Path, extra: dict, inventory: str = "localhost,") -> tuple:
    """The front door's real header and mail checks, then its real .env template, as weft-release.yml
    and the deploy run them: the checks first, the render only if they passed. `podman` is a script
    that logs what it is asked, in engine.log. `inventory` is where the rack's workspaces are."""
    env_file = tmp_path / "door.env"
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "podman").write_text(f'#!/bin/bash\necho "$*" >> "{tmp_path / "engine.log"}"\n')
    (bindir / "podman").chmod(0o755)
    play = [{
        "hosts": "localhost", "connection": "local", "gather_facts": False,
        "vars_files": [str(PLAYBOOKS / "vars/weft.yml")],
        "vars": {"_weft_release_name": NEW, "_weft_images": {"weft-door": f"{REGISTRY}/weft-door@{DIGEST}"},
                 "weft_door_dir": "/srv/weft-door", "weft_door_bind": "192.0.2.11",
                 "weft_domain": "weft.example.test", **extra},
        "tasks": [
            {"ansible.builtin.include_tasks": str(PLAYBOOKS / "tasks/weft-bind.yml"), "vars": {"_weft_role": "door"}},
            {"ansible.builtin.include_tasks": str(PLAYBOOKS / "tasks/weft-door-header.yml")},
            {"ansible.builtin.include_tasks": str(PLAYBOOKS / "tasks/weft-door-mail.yml"),
             "vars": {"_weft_role": "door"}},
            {"ansible.builtin.template": {"src": str(DOOR_DIR / "templates/door.env.j2"),
                                          "dest": str(env_file), "mode": "0600"}},
        ],
    }]
    playbook = tmp_path / "door.yml"
    playbook.write_text(yaml.safe_dump(play, width=10**6))
    env = {"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}", "ANSIBLE_NOCOLOR": "1"}
    proc = run_playbook(playbook, tmp_path, inventory=inventory, env=env)
    return proc, env_file


def _rack_inventory(tmp_path: Path, workspaces: dict, door: dict | None = None) -> Path:
    """An inventory with the controller, the rack's workspaces ({host: its vars}) and, given `door`,
    a rack front door with those vars. No play reaches any host but localhost."""
    groups = {"weft_workspaces": {"hosts": workspaces}}
    if door is not None:
        groups["weft_door"] = {"hosts": {"weft-door-svc": door}}
    inventory = {"all": {"hosts": {"localhost": {"ansible_connection": "local",
                                                 "ansible_python_interpreter": "{{ ansible_playbook_python }}"}},
                         "children": groups}}
    path = tmp_path / "inventory.yml"
    path.write_text(yaml.safe_dump(inventory))
    return path


def test_the_front_door_is_told_which_header_names_the_client_and_which_proxy_to_believe(tmp_path):
    # weft #484 retired WEFT_FRONT_DOOR_BEHIND_A_PROXY: a door given it alone refuses to start.
    # What replaced it is a header the edge overwrites and the proxies it is believed from, and the
    # door needs both or refuses to start on the one it got.
    proc, env_file = _door_env(tmp_path, {"weft_door_trusted_proxies": ["192.0.2.5/32", " 192.0.2.6/32 "]})
    assert proc.returncode == 0, proc.stdout[-4000:]
    env = env_file.read_text()
    assert "WEFT_DOOR_CLIENT_ADDRESS_HEADER=CF-Connecting-IP\n" in env, (
        "every weft name is proxied by Cloudflare, so the client is in CF-Connecting-IP:\n" + env)
    assert "WEFT_DOOR_TRUSTED_PROXIES=192.0.2.5/32,192.0.2.6/32\n" in env, env
    compose = yaml.safe_load((DOOR_DIR / "compose.yml").read_text())["services"]["door"]["environment"]
    assert "WEFT_FRONT_DOOR_BEHIND_A_PROXY" not in compose, "a door given it alone refuses to start"
    assert compose["WEFT_FRONT_DOOR_CLIENT_ADDRESS_HEADER"].startswith("${WEFT_DOOR_CLIENT_ADDRESS_HEADER:?"), compose
    assert compose["WEFT_FRONT_DOOR_TRUSTED_PROXIES"].startswith("${WEFT_DOOR_TRUSTED_PROXIES:?"), compose


@pytest.mark.parametrize("extra", [{}, {"weft_door_trusted_proxies": []}, {"weft_door_trusted_proxies": [" "]},
                                   {"weft_door_trusted_proxies": "192.0.2.5/32"}],
                         ids=["unset", "empty", "blank", "a-string-not-a-list"])
def test_a_front_door_with_no_proxy_to_believe_is_refused_before_its_env_is_rendered(tmp_path, extra):
    # Behind Caddy with no proxies, every sign-in counts against Caddy's address and ten failures
    # lock everybody out; with the header and no proxies the door refuses to start. Either way the
    # refusal belongs before the stop, so the running door is left as it is.
    proc, env_file = _door_env(tmp_path, extra)
    assert proc.returncode != 0, proc.stdout[-4000:]
    assert "has no weft_door_trusted_proxies" in proc.stdout, proc.stdout[-4000:]
    assert not env_file.exists(), "the .env was rendered after the refusal"


PROXIES = {"weft_door_trusted_proxies": ["192.0.2.5/32"]}


@pytest.mark.parametrize("bind, rendered", [("2001:db8::11", "[2001:db8::11]"), ("[2001:db8::11]", "[2001:db8::11]"),
                                            ("192.0.2.11", "192.0.2.11")])
def test_a_front_door_s_bind_is_rendered_as_the_host_before_a_port(tmp_path, bind, rendered):
    # weft #585: compose.yml's port line and deploy.sh's readiness URL both put WEFT_DOOR_BIND
    # before `:port`, so .env carries an IPv6 address bracketed, and never twice.
    proc, env_file = _door_env(tmp_path, {**PROXIES, "weft_door_bind": bind})
    assert proc.returncode == 0, proc.stdout[-4000:]
    assert _env_lines(env_file)["WEFT_DOOR_BIND"] == rendered, env_file.read_text()


def _env_lines(env_file: Path) -> dict:
    return dict(line.split("=", 1) for line in env_file.read_text().splitlines()
                if line and not line.startswith("#"))


def test_staging_s_front_door_mails_a_catcher_on_its_own_loopback_that_sends_nothing(tmp_path):
    # weft #541: the front door refuses to start without mail, and there is no provider yet. Staging's
    # door hands every message to Mailpit, which keeps it. Its links are working sign-up and reset
    # credentials, so they must reach nobody real, and must not cross a network in the clear: the
    # door allows plain SMTP to its own loopback only, which is why the catcher shares its namespace.
    proc, env_file = _door_env(tmp_path, {**PROXIES, "weft_environment": "staging", "weft_door_mailpit": True})
    assert proc.returncode == 0, proc.stdout[-4000:]
    env = _env_lines(env_file)
    pinned = yaml.safe_load((PLAYBOOKS / "vars/weft.yml").read_text())["_weft_mailpit_image"]
    assert env["WEFT_DOOR_MAILPIT_IMAGE"] in pinned, env
    assert "@sha256:" in env["WEFT_DOOR_MAILPIT_IMAGE"], "every image a weft host runs is pinned by digest"
    assert env["WEFT_DOOR_URL"] == "https://weft.example.test", "every link points at the door's own https address"
    assert env["WEFT_DOOR_MAIL_FROM"] == "no-reply@weft.example.test", env
    log = (tmp_path / "engine.log").read_text()
    assert f"pull {env['WEFT_DOOR_MAILPIT_IMAGE']}" in log, (
        "the catcher is pulled with the checks, before anything stops: a pull that fails after the "
        "stop leaves no front door\n" + log)
    overlay = yaml.safe_load((DOOR_DIR / "compose.mailpit.yml").read_text())["services"]
    door, catcher = overlay["door"], overlay["mailpit"]
    assert door["environment"]["WEFT_FRONT_DOOR_SMTP_SECURITY"] == "plain"
    assert door["environment"]["WEFT_FRONT_DOOR_SMTP_HOST"] == "127.0.0.1", (
        "plain SMTP to anything but loopback refuses to start the door")
    assert catcher["network_mode"] == "service:door", "the door's loopback is the catcher's only if they share it"
    assert catcher["environment"]["MP_SMTP_BIND_ADDR"].startswith("127.0.0.1:"), catcher
    assert catcher["image"].startswith("${WEFT_DOOR_MAILPIT_IMAGE:?"), "any default would be a tag"
    assert not [key for key in catcher["environment"] if "RELAY" in key or "FORWARD" in key], (
        "a relay or a forward is how a catcher sends real mail")
    assert not [arg for arg in catcher.get("command", []) if "relay" in arg or "forward" in arg], catcher
    assert all(port.startswith("127.0.0.1:") for port in door["ports"]), (
        "the catcher's UI has no sign-in and shows live links: the VM's loopback only\n" + str(door["ports"]))


@pytest.mark.parametrize("environment", ["", "production"], ids=["unset", "neither-staging-nor-rack"])
def test_a_front_door_outside_staging_and_the_rack_with_the_mail_catcher_is_refused_before_its_env_is_rendered(
        tmp_path, environment):
    # A catcher is a front door whose every sign-up and reset reaches nobody. Only staging, and the
    # rack before its first customer, may run one; anything else is refused with the checks, so the
    # running door is untouched.
    extra = {**PROXIES, "weft_door_mailpit": True}
    if environment:
        extra["weft_environment"] = environment
    proc, env_file = _door_env(tmp_path, extra)
    assert proc.returncode != 0, proc.stdout[-4000:]
    assert "may run it" in proc.stdout and "Nothing has changed" in proc.stdout, proc.stdout[-4000:]
    assert not env_file.exists(), "the .env was rendered after the refusal"
    assert not (tmp_path / "engine.log").exists(), "the catcher was pulled after the refusal"


@pytest.mark.parametrize("workspaces", [{}, {"weft-ws-a": {"weft_workspace_owner": "operator"},
                                             "weft-ws-b": {"weft_workspace_owner": "operator"}}],
                         ids=["no-workspace-yet", "every-workspace-the-operator-s"])
def test_the_rack_s_front_door_keeps_the_mail_catcher_while_no_workspace_is_a_customer_s(tmp_path, workspaces):
    # Jacob, 2026-11-17 (weft #332): the rack's front door serves only the operator's own people until
    # the first customer, so it may mail the catcher, published exactly as staging's is.
    inventory = _rack_inventory(tmp_path, workspaces)
    proc, env_file = _door_env(tmp_path, {**PROXIES, "weft_environment": "rack", "weft_door_mailpit": True}, inventory)
    assert proc.returncode == 0, proc.stdout[-4000:]
    env = _env_lines(env_file)
    assert "@sha256:" in env["WEFT_DOOR_MAILPIT_IMAGE"], env
    assert f"pull {env['WEFT_DOOR_MAILPIT_IMAGE']}" in (tmp_path / "engine.log").read_text()


@pytest.mark.parametrize("owner", [None, "customer", "Operator", ""], ids=["unmarked", "customer", "miscased", "empty"])
def test_the_rack_s_front_door_is_refused_the_mail_catcher_once_a_workspace_is_a_customer_s(tmp_path, owner):
    # A customer's sign-ups and resets would reach nobody, and their links would sit in a catcher only
    # the operator reads. Fail closed: a workspace nobody marked as the operator's own is a customer's.
    customer = {} if owner is None else {"weft_workspace_owner": owner}
    inventory = _rack_inventory(tmp_path, {"weft-ws-a": {"weft_workspace_owner": "operator"}, "weft-ws-b": customer})
    proc, env_file = _door_env(tmp_path, {**PROXIES, "weft_environment": "rack", "weft_door_mailpit": True}, inventory)
    assert proc.returncode != 0, proc.stdout[-4000:]
    assert "workspace weft-ws-b is not marked" in proc.stdout, proc.stdout[-4000:]
    assert "workspace weft-ws-a is not marked" not in proc.stdout, "the operator's own workspace was refused"
    assert not env_file.exists(), "the .env was rendered after the refusal"
    assert not (tmp_path / "engine.log").exists(), "the catcher was pulled after the refusal"


def test_staging_s_front_door_keeps_the_mail_catcher_whoever_the_rack_s_workspaces_belong_to(tmp_path):
    inventory = _rack_inventory(tmp_path, {"weft-ws-b": {}})
    proc, _ = _door_env(tmp_path, {**PROXIES, "weft_environment": "staging", "weft_door_mailpit": True}, inventory)
    assert proc.returncode == 0, proc.stdout[-4000:]


def _workspace_mail_check(tmp_path: Path, workspace: dict, door: dict | None, in_rack_group: bool = False):
    """The real tasks/weft-door-mail.yml as tasks/weft-release.yml includes it for a workspace, with
    localhost as the workspace (in weft_workspaces when `in_rack_group`) and `door` as the rack's
    front door, then a stand-in for the stop."""
    play = [{
        "hosts": "localhost", "connection": "local", "gather_facts": False,
        "vars_files": [str(PLAYBOOKS / "vars/weft.yml")],
        "vars": {"_weft_role": "workspace", **workspace},
        "tasks": [
            {"ansible.builtin.include_tasks": str(PLAYBOOKS / "tasks/weft-door-mail.yml")},
            {"name": "Stand-in for the stop", "ansible.builtin.copy": {
                "content": "stopped\n", "dest": str(tmp_path / "stopped"), "mode": "0644"}},
        ],
    }]
    playbook = tmp_path / "mail.yml"
    playbook.write_text(yaml.safe_dump(play, width=10**6))
    inventory = _rack_inventory(tmp_path, {}, door)
    if in_rack_group:
        declared = yaml.safe_load(inventory.read_text())
        declared["all"]["children"]["weft_workspaces"]["hosts"] = {"localhost": None}
        inventory.write_text(yaml.safe_dump(declared))
    return run_playbook(playbook, tmp_path, inventory=inventory, env={"ANSIBLE_NOCOLOR": "1"})


@pytest.mark.parametrize("owner, environment, in_rack_group", [
    (None, "rack", False), ("customer", "rack", False),
    # A rack workspace misdeclared as staging: Stand Up and Deploy weft Workspace accept it, so the
    # staging skip must not open for a host in weft_workspaces.
    (None, "staging", True),
], ids=["unmarked", "customer", "rack-host-declared-staging"])
def test_a_customer_s_workspace_is_refused_while_the_rack_s_front_door_has_the_mail_catcher(
        tmp_path, owner, environment, in_rack_group):
    # Standing a customer's workspace up does not redeploy the front door, so the door's own check
    # would not see it: the workspace's deploy refuses instead, before anything stops.
    workspace = {"weft_environment": environment}
    if owner is not None:
        workspace["weft_workspace_owner"] = owner
    proc = _workspace_mail_check(tmp_path, workspace, {"weft_door_mailpit": True}, in_rack_group)
    assert proc.returncode != 0, proc.stdout[-4000:]
    assert "counts as a customer's workspace" in proc.stdout and "weft-door-svc" in proc.stdout, proc.stdout[-4000:]
    assert not (tmp_path / "stopped").exists(), "the stop was reached after the refusal"


@pytest.mark.parametrize("workspace, door", [
    ({"weft_environment": "rack", "weft_workspace_owner": "operator"}, {"weft_door_mailpit": True}),
    ({"weft_environment": "rack"}, {}),
    ({"weft_environment": "rack"}, None),
    ({"weft_environment": "staging"}, {"weft_door_mailpit": True}),
], ids=["the-operator-s-own", "door-without-the-catcher", "no-rack-door", "staging"])
def test_a_workspace_deploys_when_the_mail_catcher_reaches_no_customer(tmp_path, workspace, door):
    proc = _workspace_mail_check(tmp_path, workspace, door)
    assert proc.returncode == 0, proc.stdout[-4000:]
    assert (tmp_path / "stopped").exists()


def test_a_front_door_without_the_mail_catcher_renders_none_of_it(tmp_path):
    proc, env_file = _door_env(tmp_path, {**PROXIES, "weft_environment": "rack"})
    assert proc.returncode == 0, proc.stdout[-4000:]
    assert not [key for key in _env_lines(env_file) if "MAIL" in key or key == "WEFT_DOOR_URL"], (
        "deploy.sh loads the catcher's overlay from .env alone\n" + env_file.read_text())
    assert not (tmp_path / "engine.log").exists(), "nothing of the catcher is pulled"


def _bind_check(tmp_path: Path, role: str, bind: dict, inventory: str = "localhost,"):
    """The real tasks/weft-bind.yml, as tasks/weft-release.yml includes it for every deploy and
    rollback, then a file standing in for the stop that comes after it."""
    play = [{
        "hosts": "localhost", "connection": "local", "gather_facts": False,
        "vars_files": [str(PLAYBOOKS / "vars/weft.yml")],
        "vars": {"_weft_role": role, **bind},
        "tasks": [
            {"ansible.builtin.include_tasks": str(PLAYBOOKS / "tasks/weft-bind.yml")},
            {"name": "Stand-in for the stop", "ansible.builtin.copy": {
                "content": "stopped\n", "dest": str(tmp_path / "stopped"), "mode": "0644"}},
        ],
    }]
    playbook = tmp_path / "bind.yml"
    playbook.write_text(yaml.safe_dump(play, width=10**6))
    return run_playbook(playbook, tmp_path, inventory=inventory, env={"ANSIBLE_NOCOLOR": "1"})


REFUSED_BINDS = ["0.0.0.0", "::", "[::]", "0:0:0:0:0:0:0:0", "127.0.0.1", "127.1.2.3", "::1", "[::1]",
                 "::ffff:127.0.0.1", "localhost", "LOCALHOST", " ",
                 # weft #582: spellings the text match let through.
                 "::0", "0::0", "0::1", "::ffff:7f00:1",
                 # weft #585: a name, which podman refused at `compose up`, after the stop.
                 "weft-ws1.example.test"]


@pytest.mark.parametrize("role, key", [("workspace", "weft_board_bind"), ("door", "weft_door_bind")])
@pytest.mark.parametrize("bind", REFUSED_BINDS)
def test_a_wildcard_loopback_or_named_bind_is_refused_before_anything_stops(tmp_path, role, key, bind):
    # weft #580: the board and the door run on pasta, which keeps a connection's source address.
    # Published on a wildcard or loopback, a process on the VM connecting to 127.0.0.1 arrives as
    # 127.0.0.1 (measured on podman 4.9.3), and the board's ingest_from always admits loopback for
    # its healthcheck: every local process would be admitted as that healthcheck.
    proc = _bind_check(tmp_path, role, {key: bind})
    assert proc.returncode != 0, f"{key}={bind!r} was accepted\n" + proc.stdout[-4000:]
    assert f"{key} is" in proc.stdout and "Nothing has changed" in proc.stdout, proc.stdout[-4000:]
    assert not (tmp_path / "stopped").exists(), "the stop was reached after the refusal"


@pytest.mark.parametrize("role", ["workspace", "door"])
def test_an_unset_bind_is_the_host_s_address_and_is_held_to_the_same_rule(tmp_path, role):
    # workspace.env.j2 and door.env.j2 publish on ansible_host when no bind is set.
    proc = _bind_check(tmp_path, role, {"ansible_host": "127.0.0.1"})
    assert proc.returncode != 0, proc.stdout[-4000:]
    assert not (tmp_path / "stopped").exists()


@pytest.mark.parametrize("role, key", [("workspace", "weft_board_bind"), ("door", "weft_door_bind")])
def test_an_unset_bind_on_a_host_addressed_by_name_is_refused_naming_ansible_host(tmp_path, role, key):
    # weft #585: with no bind, the env templates publish on ansible_host, which inventory may give as
    # a name. The refusal names where the value came from, so the person knows which line to change.
    proc = _bind_check(tmp_path, role, {"ansible_host": "weft-ws1.example.test"})
    assert proc.returncode != 0, proc.stdout[-4000:]
    assert f"ansible_host ({key} is unset) is 'weft-ws1.example.test'" in proc.stdout, proc.stdout[-4000:]
    assert "not an IP address" in proc.stdout and "Nothing has changed" in proc.stdout, proc.stdout[-4000:]
    assert not (tmp_path / "stopped").exists(), "the stop was reached after the refusal"


def test_local_mode_publishes_on_loopback(tmp_path):
    # A local-dev inventory puts every host at 127.0.0.1, with the edge on the same machine.
    proc = _bind_check(tmp_path, "workspace", {"weft_board_bind": "127.0.0.1"}, inventory=LOCAL_DEV)
    assert proc.returncode == 0, proc.stdout[-4000:]
    assert (tmp_path / "stopped").exists()


@pytest.mark.parametrize("role, bind", [
    ("workspace", {"weft_board_bind": "192.0.2.10"}), ("door", {"weft_door_bind": "2001:db8::10"}),
    ("workspace", {"ansible_host": "192.0.2.10"}),
    # Each role reads its own key: the other role's loopback is not this one's bind.
    ("door", {"weft_board_bind": "127.0.0.1", "weft_door_bind": "192.0.2.11"}),
], ids=["board-address", "door-ipv6", "host-address", "own-key"])
def test_the_vm_s_own_address_is_accepted_as_a_bind(tmp_path, role, bind):
    proc = _bind_check(tmp_path, role, bind)
    assert proc.returncode == 0, proc.stdout[-4000:]
    assert (tmp_path / "stopped").exists()


def _default_gateway() -> str | None:
    """This machine's IPv4 default gateway, which the real task gathers as the VM's (/proc/net/route)."""
    try:
        lines = Path("/proc/net/route").read_text().splitlines()[1:]
    except OSError:
        return None
    for fields in (line.split() for line in lines):
        if len(fields) > 3 and fields[1] == "00000000" and int(fields[3], 16) & 2:  # RTF_GATEWAY
            return str(ipaddress.IPv4Address(int(fields[2], 16).to_bytes(4, "little")))
    return None


@pytest.mark.parametrize("role, peers", [
    ("workspace", {"weft_board_trusted_proxies": ["192.0.2.0/24"]}),
    ("door", {"weft_door_trusted_proxies": ["192.0.2.0/24"]}),
    ("workspace", {"weft_board_ingest_from": ["169.254.1.2/32"]}),
    ("door", {"weft_door_trusted_proxies": ["169.254.1.2/32"]}),
], ids=["board-range", "door-range", "ingest-pasta-guest", "door-pasta-guest"])
def test_believed_peers_that_hold_the_vm_s_own_processes_are_refused_before_anything_stops(tmp_path, role, peers):
    # weft #582: under pasta a process or container on the VM arrives as the default gateway
    # (podman 4.9.3) or 169.254.1.2 (5.3 on), and a trusted proxy may name any client it likes. A
    # range trusts whatever else is in it, so trusted_proxies is the Caddy host alone, as a /32.
    bind = {"weft_board_bind": "192.0.2.10"} if role == "workspace" else {"weft_door_bind": "192.0.2.11"}
    proc = _bind_check(tmp_path, role, {**bind, **peers})
    assert proc.returncode != 0, f"{peers} was accepted\n" + proc.stdout[-4000:]
    assert "Nothing has changed" in proc.stdout, proc.stdout[-4000:]
    assert not (tmp_path / "stopped").exists(), "the stop was reached after the refusal"


@pytest.mark.parametrize("key", ["weft_board_trusted_proxies", "weft_board_ingest_from"])
def test_the_gateway_the_task_gathers_is_refused_as_a_believed_peer(tmp_path, key):
    # The gateway is read from the host the play runs on, not passed in: every weft play runs
    # gather_facts: false, so a task that only read the fact would never see one.
    gateway = _default_gateway()
    if gateway is None:
        pytest.skip("this machine has no IPv4 default route to stand in for the VM's")
    proc = _bind_check(tmp_path, "workspace", {"weft_board_bind": "192.0.2.10", key: [f"{gateway}/32"]})
    assert proc.returncode != 0, f"{key} holding the gateway {gateway} was accepted\n" + proc.stdout[-4000:]
    assert f"holds {gateway}" in proc.stdout, proc.stdout[-4000:]
    assert not (tmp_path / "stopped").exists(), "the stop was reached after the refusal"


@pytest.mark.parametrize("role, peers", [
    ("workspace", {"weft_board_trusted_proxies": ["198.51.100.5/32"], "weft_board_ingest_from": ["203.0.113.0/24"]}),
    ("door", {"weft_door_trusted_proxies": ["198.51.100.5/32"]}),
], ids=["board", "door"])
def test_the_caddy_host_alone_and_ingest_sources_clear_of_the_vm_are_accepted(tmp_path, role, peers):
    bind = {"weft_board_bind": "192.0.2.10"} if role == "workspace" else {"weft_door_bind": "192.0.2.11"}
    proc = _bind_check(tmp_path, role, {**bind, **peers})
    assert proc.returncode == 0, proc.stdout[-4000:]
    assert (tmp_path / "stopped").exists()


def test_local_mode_does_not_judge_believed_peers(tmp_path):
    # Local mode is the operator's own machine; its edge and every collector are on it.
    proc = _bind_check(tmp_path, "workspace", {"weft_board_bind": "127.0.0.1",
                                               "weft_board_ingest_from": ["192.0.2.0/24"]}, inventory=LOCAL_DEV)
    assert proc.returncode == 0, proc.stdout[-4000:]
    assert (tmp_path / "stopped").exists()


PASTA_ENGINE = """#!/bin/bash
echo "$*" >> "{log}"
printf '%s' "$(cat "{answer}")"
exit {rc}
"""


def _pasta_check(tmp_path: Path, answer: str, rc: int = 0):
    """The real tasks/assert-podman-pasta.yml, as weft-workspace.yml and weft-door.yml include it,
    against a `podman` that prints `answer` for `info` and exits `rc`. A file stands in for the stop
    that tasks/weft-deploy.yml makes after it."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    (tmp_path / "answer").write_text(answer)
    (bindir / "podman").write_text(PASTA_ENGINE.format(log=tmp_path / "engine.log", answer=tmp_path / "answer", rc=rc))
    (bindir / "podman").chmod(0o755)
    play = [{
        "hosts": "localhost", "connection": "local", "gather_facts": False,
        "tasks": [
            {"ansible.builtin.include_tasks": str(PLAYBOOKS / "tasks/assert-podman-pasta.yml"),
             "vars": {"_app_for": "weft's board"}},
            {"name": "Stand-in for the stop", "ansible.builtin.copy": {
                "content": "stopped\n", "dest": str(tmp_path / "stopped"), "mode": "0644"}},
        ],
    }]
    playbook = tmp_path / "pasta.yml"
    playbook.write_text(yaml.safe_dump(play, width=10**6))
    env = {"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}", "ANSIBLE_NOCOLOR": "1"}
    return run_playbook(playbook, tmp_path, env=env)


@pytest.mark.parametrize("answer, rc", [("", 0), ("\n", 0), ("", 125)], ids=["no-pasta", "blank", "podman-failed"])
def test_a_host_whose_podman_has_no_pasta_is_refused_before_anything_stops(tmp_path, answer, rc):
    # weft #580: the board and the front door run on pasta so that they see the Caddy host's
    # address; from a rootless bridge every request came from their own bridge address. podman
    # only recommends passt, and without it the new container fails at creation, after the stop
    # has taken the running one away. `podman info` names no pasta when podman could not run one.
    proc = _pasta_check(tmp_path, answer, rc)
    assert proc.returncode != 0, proc.stdout[-4000:]
    assert "weft's board runs on podman's pasta network mode" in proc.stdout, proc.stdout[-4000:]
    assert "Run Install Podman" in proc.stdout, "the refusal names the fix:\n" + proc.stdout[-4000:]
    assert not (tmp_path / "stopped").exists(), "the stop was reached after the refusal"


def test_a_host_whose_podman_names_its_pasta_goes_on_to_the_deploy(tmp_path):
    proc = _pasta_check(tmp_path, "/usr/bin/pasta\n")
    assert proc.returncode == 0, proc.stdout[-4000:]
    assert (tmp_path / "stopped").exists()
    assert (tmp_path / "engine.log").read_text() == "info --format {{.Host.Pasta.Executable}}\n", (
        "the read must be podman's own answer, which is what a container's creation will use")


DOOR_ENGINE = """#!/bin/bash
echo "$*" >> "{log}"
case "$1 $2" in
  "container exists") [ -e "{left}" ] ;;
esac
"""


EDGE_INVENTORY = """\
localhost ansible_connection=local
[weft_door]
door1 ansible_host={door}
[weft_workspaces]
ws1 ansible_host=192.0.2.10 weft_workspace_name=acme {board}
"""


def _edge_fragment(tmp_path: Path, door: str, board: str) -> str:
    """caddy-site.j2 rendered as weft-edge.yml renders it, from an inventory of one door and one workspace."""
    inventory = tmp_path / "inventory.ini"
    inventory.write_text(EDGE_INVENTORY.format(door=door, board=board))
    out = tmp_path / "site.caddy"
    play = [{
        "hosts": "localhost", "connection": "local", "gather_facts": False,
        "vars": {"weft_domain": "weft.example.test", "_weft_door_hosts": "{{ groups['weft_door'] }}",
                 "_weft_workspace_hosts": "{{ groups['weft_workspaces'] }}"},
        "tasks": [{"ansible.builtin.template": {"src": str(DOOR_DIR / "templates/caddy-site.j2"),
                                                "dest": str(out), "mode": "0644"}}],
    }]
    playbook = tmp_path / "edge.yml"
    playbook.write_text(yaml.safe_dump(play, width=10**6))
    proc = run_playbook(playbook, tmp_path, inventory=inventory, env={"ANSIBLE_NOCOLOR": "1"})
    assert proc.returncode == 0, proc.stdout[-4000:]
    return out.read_text()


@pytest.mark.parametrize("door, board", [
    ("2001:db8::11", "weft_board_bind=2001:db8::10"),
    ("192.0.2.99 weft_door_bind=[2001:db8::11]", "weft_board_bind=[2001:db8::10]"),
], ids=["bare", "already-bracketed"])
def test_the_edge_reaches_a_door_and_a_workspace_published_on_ipv6(tmp_path, door, board):
    # weft #585: `reverse_proxy 2001:db8::10:8080` is not a host and a port Caddy can split. Each
    # upstream is the host's bind (or ansible_host), bracketed once, whichever way inventory wrote it.
    proxied = sorted(line.strip() for line in _edge_fragment(tmp_path, door, board).splitlines()
                     if line.strip().startswith("reverse_proxy "))
    assert proxied == ["reverse_proxy [2001:db8::10]:8080",
                       "reverse_proxy [2001:db8::11]:8081", "reverse_proxy [2001:db8::11]:8081"], proxied


def test_the_edge_reaches_an_ipv4_host_as_written(tmp_path):
    proxied = sorted(line.strip() for line in _edge_fragment(tmp_path, "192.0.2.11", "").splitlines()
                     if line.strip().startswith("reverse_proxy "))
    assert proxied == ["reverse_proxy 192.0.2.10:8080",
                       "reverse_proxy 192.0.2.11:8081", "reverse_proxy 192.0.2.11:8081"], proxied


def _door_deploy_sh(tmp_path: Path, port: int, catcher: bool, left_behind: bool, command: str,
                    bind: str = "127.0.0.1"):
    """The front door's real deploy.sh, against a podman that logs its arguments and says whether a
    catcher container is left on the host."""
    clone = tmp_path / "clone/platform"
    door_dir = clone / "services/weft-door/deployment"
    shutil.copytree(DOOR_DIR, door_dir, ignore=shutil.ignore_patterns(".env", "door.key"))
    (clone / "lib").symlink_to(REPO / "platform/lib")
    env = (f"WEFT_DOOR_IMAGE={REGISTRY}/weft-door@{DIGEST}\nWEFT_DOOR_DIR=/srv/weft-door\n"
           f"WEFT_DOOR_BIND={bind}\nWEFT_DOOR_PORT={port}\n"
           "WEFT_DOOR_WORKSPACE_URL=https://{name}.weft.example.test\n"
           "WEFT_DOOR_CLIENT_ADDRESS_HEADER=CF-Connecting-IP\nWEFT_DOOR_TRUSTED_PROXIES=192.0.2.5/32\n")
    if catcher:
        env += f"WEFT_DOOR_MAILPIT_IMAGE=docker.io/axllent/mailpit@{DIGEST}\n"
    (door_dir / ".env").write_text(env)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    left = tmp_path / "catcher-left"
    if left_behind:
        left.touch()
    (bindir / "podman").write_text(DOOR_ENGINE.format(log=tmp_path / "engine.log", left=left))
    (bindir / "podman").chmod(0o755)
    proc = subprocess.run(["bash", "deploy.sh", command], cwd=door_dir, capture_output=True, text=True, timeout=60,
                          env={**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
                               "CONTAINER_ENGINE": "podman", "COMPOSE_CMD": "podman compose", "LOCAL_MODE": ""})
    log = tmp_path / "engine.log"
    return proc, (log.read_text().splitlines() if log.exists() else [])


class _Page(DrainingHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.mark.parametrize("command", ["up", "stop"])
def test_the_mail_catcher_stops_and_starts_with_the_door_whose_namespace_it_shares(tmp_path, command):
    # podman refuses to remove a container another shares the network namespace of, so recreating the
    # door alone fails while the catcher exists, and podman-compose still exits 0 and starts the OLD
    # door again: a deploy that reports a release it is not running.
    with serve(_Page) as url:
        proc, log = _door_deploy_sh(tmp_path, int(url.rsplit(":", 1)[1]), True, False, command)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    composed = [line for line in log if line.startswith("compose ")]
    assert composed and all("-f compose.mailpit.yml" in line for line in composed), log
    assert composed[-1].endswith(" door mailpit"), composed


def test_a_front_door_on_an_ipv6_address_passes_its_readiness_check(tmp_path):
    # weft #585: deploy.sh waits on http://${WEFT_DOOR_BIND}:${WEFT_DOOR_PORT}/. With the bind as
    # door.env.j2 writes it, bracketed, that is a URL; bare, `up` would wait out its timeout and fail.
    with ExitStack() as stack:
        try:
            url = stack.enter_context(serve(_Page, host="::1"))
        except OSError as e:
            pytest.skip(f"this machine cannot listen on IPv6 loopback: {e}")
        port = int(url.rsplit(":", 1)[1])
        proc, _ = _door_deploy_sh(tmp_path, port, False, False, "up", bind="[::1]")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"front door at http://[::1]:{port}/" in proc.stdout + proc.stderr, proc.stdout + proc.stderr


def test_a_mail_catcher_the_env_no_longer_names_is_removed_before_the_door_is_recreated(tmp_path):
    # Stopped, it still holds the door's namespace: recreating the door would fail, and the old door
    # would start again on the old image.
    with serve(_Page) as url:
        proc, log = _door_deploy_sh(tmp_path, int(url.rsplit(":", 1)[1]), False, True, "up")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    removed = log.index("rm -f weft-door-mailpit")
    up = next(i for i, line in enumerate(log) if line.startswith("compose ") and " up " in line)
    assert removed < up, log
    assert log[up].endswith(" door") and "compose.mailpit.yml" not in log[up], log


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
