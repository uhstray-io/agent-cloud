"""The local Semaphore's Mac-side port is an inventory value (bootstrap-local-dev.yml).

It was a literal in two places (the API URL every bootstrap call uses, and the container
publish), so a machine where another app held 127.0.0.1:3000 could not bootstrap at all.
These tests lift the real play's vars, its validation task and its publish line into a
throwaway play and run a real ansible-playbook, so they check what the bootstrap renders and
refuses rather than the text it is written in.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import playbook_yaml
import pytest

REPO = Path(__file__).resolve().parents[2]
BOOTSTRAP = REPO / "platform/playbooks/bootstrap-local-dev.yml"
EXAMPLE = REPO / "platform/inventory/local-dev.yml.example"
VALIDATE = "Assert the local Semaphore host port is a usable unprivileged port"
START = "Start local Semaphore"

needs_ansible = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")


def _play() -> dict:
    return playbook_yaml.load(BOOTSTRAP)[0]


def _task(name: str) -> dict:
    return next(t for t in _play()["tasks"] if t.get("name") == name)


def _run(tmp_path: Path, *extra: str, validate_only: bool = False, inventory: str = "localhost,"):
    """Render the real play's vars and tasks. The publish line is shown, not executed."""
    lines = _task(START)["ansible.builtin.shell"].splitlines()
    publish = next(ln.strip().rstrip("\\").strip() for ln in lines if ln.strip().startswith("-p "))
    tasks = [_task(VALIDATE)]
    if not validate_only:
        tasks += [
            {"ansible.builtin.debug": {"msg": "URL=[{{ _sem_url }}]"}},
            {"ansible.builtin.debug": {"msg": "PUBLISH=[" + publish + "]"}},
        ]
    play = tmp_path / "play.yml"
    play.write_text(json.dumps([{"hosts": "localhost", "connection": "local", "gather_facts": False,
                                 "vars": _play()["vars"], "tasks": tasks}]))
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env.update(ANSIBLE_NOCOLOR="1", ANSIBLE_LOCAL_TEMP=str(tmp_path), ANSIBLE_REMOTE_TEMP=str(tmp_path))
    return subprocess.run(["ansible-playbook", "-i", inventory, str(play), *extra], cwd=REPO, env=env, text=True,
                          capture_output=True, check=False, timeout=120, stdin=subprocess.DEVNULL)


@needs_ansible
def test_default_is_the_prod_typical_port(tmp_path):
    out = _run(tmp_path)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "URL=[http://127.0.0.1:3000]" in out.stdout
    assert "PUBLISH=[-p 127.0.0.1:3000:3000]" in out.stdout


@needs_ansible
@pytest.mark.parametrize("given", ["-e local_semaphore_port=3100", '-e {"local_semaphore_port":3100}'])
def test_an_inventory_value_moves_the_url_and_the_host_side_of_the_publish(tmp_path, given):
    out = _run(tmp_path, *given.split(" ", 1))
    assert out.returncode == 0, out.stdout + out.stderr
    assert "URL=[http://127.0.0.1:3100]" in out.stdout
    # Host side moves; the container side stays 3000, where Caddy and every in-network caller reach it.
    assert "PUBLISH=[-p 127.0.0.1:3100:3000]" in out.stdout


@needs_ansible
@pytest.mark.parametrize("bad", ["abc", "3100.5", "03100", "1023", "0", "65536", "-1", "80"])
def test_a_bad_value_is_refused_before_anything_starts(tmp_path, bad):
    out = _run(tmp_path, "-e", f"local_semaphore_port={bad}", validate_only=True)
    assert out.returncode != 0, out.stdout
    assert "local_semaphore_port must be an integer from 1024 to 65535" in out.stdout


@needs_ansible
@pytest.mark.parametrize("bad", ['" 3100"', "true", '"3100 "'])
def test_a_typed_non_integer_is_refused(tmp_path, bad):
    # Typed (JSON) extra vars: key=value parsing would trim the space and read true as text.
    out = _run(tmp_path, "-e", '{"local_semaphore_port":' + bad + "}", validate_only=True)
    assert out.returncode != 0, out.stdout
    assert "local_semaphore_port must be an integer from 1024 to 65535" in out.stdout


def test_the_validation_is_the_first_stage_zero_task():
    assert _play()["tasks"][0]["name"] == VALIDATE


def _typed_inventory(tmp_path: Path, value: str) -> str:
    """An inventory whose all.vars carries local_semaphore_port as a YAML value, not an
    extra-var string: `0` and `false` are typed here, which `-e name=0` cannot express."""
    inv = tmp_path / "inv.yml"
    inv.write_text(
        "all:\n  hosts:\n    localhost: {ansible_connection: local}\n"
        f"  vars:\n    local_semaphore_port: {value}\n"
    )
    return str(inv)


@needs_ansible
@pytest.mark.parametrize("value", ["0", "false", '""', "~", "3100.5", "80"])
def test_a_falsy_or_bad_inventory_value_is_refused_not_defaulted(tmp_path, value):
    # default(3000, true) turned 0/false/"" into 3000 before the validation could see them.
    out = _run(tmp_path, validate_only=True, inventory=_typed_inventory(tmp_path, value))
    assert out.returncode != 0, out.stdout
    assert "local_semaphore_port must be an integer from 1024 to 65535" in out.stdout


@needs_ansible
def test_a_typed_inventory_integer_is_accepted(tmp_path):
    out = _run(tmp_path, inventory=_typed_inventory(tmp_path, "3200"))
    assert out.returncode == 0, out.stdout + out.stderr
    assert "URL=[http://127.0.0.1:3200]" in out.stdout


STUB = """#!/bin/sh
# Stand-in for podman: answers what the recreate task asks, logs every `rm`.
case "$1" in
  container) [ "$STUB_EXISTS" = yes ] ;;
  rm) echo "$*" >> "$STUB_LOG" ;;
  inspect)
    case "$*" in
      *PortBindings*) printf '%s' "$STUB_PORTS" ;;
      *Mounts*) printf '%s' '/run/podman/podman.sock /workspace/agent-cloud /var/lib/agent-cloud-deploy ' ;;
      *SecurityOpt*) printf '%s' '[label=disable]' ;;
      *Config.Env*) printf '%s' '["SSL_CERT_FILE=/x"]' ;;
    esac ;;
esac
"""


def _recreate(tmp_path: Path, *, exists: bool, ports: str, want: int = 3000):
    """Run the real recreate task against a stub podman. Everything else the task checks
    (mounts, label=disable, OIDC) is stubbed as satisfied, so only the port decides."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    podman = bindir / "podman"
    podman.write_text(STUB)
    podman.chmod(0o755)
    task = next(t for t in _play()["tasks"] if str(t.get("name", "")).startswith("Recreate Semaphore if"))
    play = tmp_path / "recreate.yml"
    play.write_text(json.dumps([{
        "hosts": "localhost", "connection": "local", "gather_facts": False,
        "vars": {**_play()["vars"], "_oidc_ready": False, "local_semaphore_port": want},
        "tasks": [task, {"ansible.builtin.debug": {"msg": "RESULT=[{{ _sem_recreate.stdout | trim }}]"}}],
    }]))
    log = tmp_path / "rm.log"
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env.update(ANSIBLE_NOCOLOR="1", ANSIBLE_LOCAL_TEMP=str(tmp_path), ANSIBLE_REMOTE_TEMP=str(tmp_path),
               PATH=f"{bindir}:{os.environ['PATH']}", STUB_EXISTS="yes" if exists else "no", STUB_PORTS=ports,
               STUB_LOG=str(log))
    out = subprocess.run(["ansible-playbook", "-i", "localhost,", str(play)], cwd=REPO, env=env, text=True,
                         capture_output=True, check=False, timeout=120, stdin=subprocess.DEVNULL)
    assert out.returncode == 0, out.stdout + out.stderr
    result = out.stdout.split("RESULT=[")[1].split("]")[0]
    return result, (log.read_text() if log.exists() else "")


def _bindings(port: str) -> str:
    return '{"3000/tcp":[{"HostIp":"127.0.0.1","HostPort":"' + port + '"}]}'


@needs_ansible
@pytest.mark.parametrize(
    ("exists", "ports", "want", "result", "removed"),
    [
        (True, _bindings("3000"), 3000, "ok", False),  # published where it should be: leave it
        (True, _bindings("3000"), 3100, "recreated", True),  # the value changed: recreate
        (True, _bindings("30000"), 3000, "recreated", True),  # bound port starts with the wanted one
        (True, _bindings("33000"), 3000, "recreated", True),  # bound port merely contains it
        (True, _bindings("13000"), 3000, "recreated", True),  # ... or ends with it
        (True, _bindings("3000"), 30000, "recreated", True),  # the wanted port is the longer one
        (True, "null", 3000, "recreated", True),  # published nowhere
        (False, "", 3000, "ok", False),  # no container: the start task creates it
    ],
)
def test_a_changed_port_recreates_an_existing_container(tmp_path, exists, ports, want, result, removed):
    """The publish is fixed at creation, so without this a changed value silently does nothing."""
    got, rm_log = _recreate(tmp_path, exists=exists, ports=ports, want=want)
    assert got == result
    assert bool(rm_log) is removed, rm_log


def test_the_example_inventory_declares_the_default():
    text = EXAMPLE.read_text()
    assert "    local_semaphore_port: 3000\n" in text
