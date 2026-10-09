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


def _run(tmp_path: Path, *extra: str, validate_only: bool = False):
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
    return subprocess.run(["ansible-playbook", "-i", "localhost,", str(play), *extra], cwd=REPO, env=env, text=True,
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


def test_a_changed_port_recreates_an_existing_container():
    """The publish is fixed at creation, so without this a changed value silently does nothing."""
    task = next(t for t in _play()["tasks"] if str(t.get("name", "")).startswith("Recreate Semaphore if"))
    body = task["ansible.builtin.shell"]
    assert """grep -q '"HostPort":"{{ _sem_port }}"'""" in body


def test_the_example_inventory_declares_the_default():
    text = EXAMPLE.read_text()
    assert "    local_semaphore_port: 3000\n" in text
