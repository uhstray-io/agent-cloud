"""The local Semaphore's encryption key lives on its data volume (bootstrap-local-dev.yml).

config.json holds access_key_encryption, the key for every encrypted environment input in the
Semaphore database. It sat in the container layer, so the recreate tasks (which remove the
container) destroyed it and the next container minted a new one: the seeded inputs stopped
decrypting (2026-10-24). These tests run the REAL carry task with ansible-playbook against a stub
podman whose container layer and data volume are temp directories, so they check what the task
does to those files, not the text it is written in.
"""

import json
import os
import stat
import subprocess
from pathlib import Path

import playbook_yaml
import pytest
from ansible_python import needs_ansible

REPO = Path(__file__).resolve().parents[2]
BOOTSTRAP = REPO / "platform/playbooks/bootstrap-local-dev.yml"
START = "Start local Semaphore"
CARRY = "Carry an existing Semaphore encryption key onto the data volume"
RECREATE = "Recreate Semaphore if a required mount, label=disable, the host port or the image differs"
KEY = "synthetic-container-key"
VOLUME_KEY = "synthetic-volume-key"


def _play() -> dict:
    return playbook_yaml.load(BOOTSTRAP)[0]


def _task(name: str) -> dict:
    return next(t for t in _play()["tasks"] if t.get("name") == name)


def test_the_start_task_points_semaphore_at_the_data_volume():
    lines = [line.strip() for line in _task(START)["ansible.builtin.shell"].splitlines()]
    assert "-e SEMAPHORE_CONFIG_PATH=/var/lib/semaphore/config \\" in lines
    assert "-v local-semaphore-data:/var/lib/semaphore \\" in lines


def test_the_key_is_carried_before_anything_can_remove_the_container():
    tasks = _play()["tasks"]
    names = [t.get("name") for t in tasks]
    carry = names.index(CARRY)
    assert carry < names.index(RECREATE)
    assert carry < names.index(START)
    removers = [i for i, t in enumerate(tasks) if "podman rm" in str(t) and "_sem_name" in str(t)]
    assert removers, "the recreate task is expected to remove the Semaphore container"
    assert all(i > carry for i in removers)


def test_the_carry_script_never_prints_the_key():
    shell = _task(CARRY)["ansible.builtin.shell"]
    assert "cat " not in shell
    assert "echo \"$(" not in shell


STUB = """#!/bin/sh
# Stand-in for podman: the container exists or not, is in STUB_STATE, and its /etc/semaphore and
# the data volume are temp directories. Every start and exec is logged; exec runs the script with
# both paths mapped onto those directories.
case "$1" in
  container) [ "$STUB_EXISTS" = yes ] ;;
  inspect) printf '%s' "$STUB_STATE" ;;
  start) echo start >> "$STUB_LOG" ;;
  exec)
    echo exec >> "$STUB_LOG"
    script=$(printf '%s' "$5" | sed -e "s|/var/lib/semaphore|$STUB_VOLUME|g" -e "s|/etc/semaphore|$STUB_ETC|g")
    exec sh -c "$script" ;;
  *) echo "unexpected podman call: $*" >&2; exit 1 ;;
esac
"""


def _carry(tmp_path: Path, *, exists: bool = True, state: str = "running",
           volume_config: str | None = None, etc_config: str | None = None):
    """Run the real carry task against the stub. Returns the result, the stub's calls, and the
    two config paths the task reads and writes."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    podman = bindir / "podman"
    podman.write_text(STUB)
    podman.chmod(0o755)
    volume = tmp_path / "volume"
    etc = tmp_path / "etc"
    volume.mkdir()
    etc.mkdir()
    if volume_config is not None:
        (volume / "config").mkdir()
        (volume / "config/config.json").write_text(volume_config)
    if etc_config is not None:
        (etc / "config.json").write_text(etc_config)
    task = _task(CARRY)
    play = tmp_path / "carry.yml"
    play.write_text(json.dumps([{
        "hosts": "localhost", "connection": "local", "gather_facts": False,
        "vars": _play()["vars"],
        "tasks": [task, {"ansible.builtin.debug": {
            "msg": "RESULT=[{{ _sem_key_carry.stdout_lines | join(',') }}] CHANGED=[{{ _sem_key_carry.changed }}]"}}],
    }]))
    log = tmp_path / "engine.log"
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env.update(ANSIBLE_NOCOLOR="1", ANSIBLE_LOCAL_TEMP=str(tmp_path), ANSIBLE_REMOTE_TEMP=str(tmp_path),
               PATH=f"{bindir}:{os.environ['PATH']}", STUB_EXISTS="yes" if exists else "no",
               STUB_STATE=state, STUB_LOG=str(log), STUB_VOLUME=str(volume), STUB_ETC=str(etc))
    out = subprocess.run(["ansible-playbook", "-i", "localhost,", str(play)], cwd=REPO, env=env, text=True,
                         capture_output=True, check=False, timeout=120, stdin=subprocess.DEVNULL)
    calls = log.read_text().split() if log.exists() else []
    return out, calls, volume / "config/config.json", etc / "config.json"


@needs_ansible
@pytest.mark.parametrize(
    ("kwargs", "result", "changed", "calls"),
    [
        ({"exists": False}, "absent", False, []),  # fresh install: the start task generates the key on the volume
        ({"volume_config": VOLUME_KEY, "etc_config": KEY}, "present", False, ["exec"]),  # already carried: keep it
        ({"etc_config": KEY}, "carried", True, ["exec"]),  # container layer only: copy it onto the volume
        ({"state": "exited", "etc_config": KEY}, "started,carried", True, ["start", "exec"]),  # start first
        ({"state": "exited", "volume_config": VOLUME_KEY}, "started,present", True, ["start", "exec"]),
    ],
)
def test_the_carry_copies_only_when_the_volume_has_no_key(tmp_path, kwargs, result, changed, calls):
    out, got_calls, volume_file, etc_file = _carry(tmp_path, **kwargs)
    assert out.returncode == 0, out.stdout + out.stderr
    assert f"RESULT=[{result}] CHANGED=[{changed}]" in out.stdout
    assert got_calls == calls
    if kwargs.get("volume_config") is not None:
        assert volume_file.read_text() == kwargs["volume_config"]
    elif result.endswith("carried"):
        assert volume_file.read_text() == KEY
        assert stat.S_IMODE(volume_file.stat().st_mode) == 0o600
        assert etc_file.read_text() == KEY
    else:
        assert not volume_file.exists()


@needs_ansible
def test_a_running_container_with_no_key_anywhere_fails_the_task(tmp_path):
    out, _, volume_file, _ = _carry(tmp_path)
    assert out.returncode != 0, out.stdout
    assert not volume_file.exists()
