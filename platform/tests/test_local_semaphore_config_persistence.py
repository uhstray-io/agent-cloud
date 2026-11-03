"""The local Semaphore's encryption key lives on its data volume (bootstrap-local-dev.yml).

config.json holds access_key_encryption, the key for every encrypted environment input in the
Semaphore database. It sat in the container layer, so the recreate tasks (which remove the
container) destroyed it and the next container minted a new one: the seeded inputs stopped
decrypting (2026-10-24). These tests run the REAL carry and recreate tasks with ansible-playbook
against a stub podman whose container layer and data volume are temp directories, so they check
what the tasks do to those files and to the container, not the text they are written in.

The carry task reads the data volume before it may start the container: a stopped container whose
bind-mount source is gone cannot start (2026-11-02), and the start must not be what decides. The
recreate task replaces a container whose bind-mount source is not the one this run mounts, and
keeps the data volume.
"""

import json
import os
import re
import stat
import subprocess
from pathlib import Path
from typing import NamedTuple

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
VOLUME_NAME = "local-semaphore-data"
DEPLOY_SHARE = "/var/lib/agent-cloud-deploy"
IMAGE_ID = "stub-image-id"
STALE_CHECKOUT = "/srv/stale-checkout/agent-cloud-plane-mcp"


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


def test_the_layer_key_is_held_in_memory_and_never_written_to_a_temp_file():
    shell = _task(CARRY)["ansible.builtin.shell"]
    assert "mktemp" not in shell
    assert "trap" not in shell
    assert "key=$(podman cp" in shell
    assert shell.index("set -o pipefail") < shell.index("podman start")


def test_the_volume_probe_precedes_any_start():
    shell = _task(CARRY)["ansible.builtin.shell"]
    assert shell.index("podman run") < shell.index("podman start")


def test_the_probe_is_read_only_and_never_pulls():
    shell = _task(CARRY)["ansible.builtin.shell"]
    probe = shell[shell.index("podman run"):shell.index("rc=$?")]
    assert "--pull=never" in probe
    assert '"$vol:/var/lib/semaphore:ro"' in probe
    assert '"$image"' in probe
    assert "test -f /var/lib/semaphore/config/config.json" in probe


def test_the_status_words_are_unchanged():
    shell = _task(CARRY)["ansible.builtin.shell"]
    assert set(re.findall(r"echo (\w+)", shell)) == {"absent", "present", "carried", "none", "started"}


def test_the_recreate_compares_bind_mount_sources_after_the_destination_check():
    shell = _task(RECREATE)["ansible.builtin.shell"]
    assert ".Source" in shell
    assert "[ \"$REPO_SRC\" != \"{{ _repo_host_dir }}\" ]" in shell
    assert "[ \"$SHARE_SRC\" != \"{{ _deploy_share }}\" ]" in shell
    assert shell.index("grep -q '{{ _deploy_share }}'") < shell.index("\"$REPO_SRC\"")


STUB = """#!/bin/sh
# Stand-in for podman. The container exists or not (STUB_EXISTS) and is in STUB_STATE. Its named
# data volume is STUB_MOUNT, and /var/lib/semaphore maps onto the temp dir STUB_VOLUME. The container
# layer, /etc/semaphore, maps onto STUB_ETC. Bind-mount sources are STUB_REPO_SRC and STUB_SHARE_SRC.
# Every call that starts, runs, copies, exec-s or removes is logged as one line in STUB_LOG.
log() {
  echo "$*" >> "$STUB_LOG"
}
case "$1" in
  container) [ "$STUB_EXISTS" = yes ] ;;
  image)
    case "$2" in
      exists) exit 0 ;;
      inspect) printf '%s' "$STUB_IMAGE" ;;
      *) echo "unexpected podman image call: $*" >&2; exit 1 ;;
    esac ;;
  inspect)
    case "$*" in
      *Source*)
        case "$*" in
          *agent-cloud-deploy*) printf '%s' "$STUB_SHARE_SRC" ;;
          *) printf '%s' "$STUB_REPO_SRC" ;;
        esac ;;
      *Name*) printf '%s' "$STUB_MOUNT" ;;
      *Destination*) printf '%s' "$STUB_DESTS" ;;
      *SecurityOpt*) printf '%s' '[label=disable]' ;;
      *Config.Env*) printf '%s' '[]' ;;
      *PortBindings*) printf '%s' '{"3000/tcp":[{"HostIp":"127.0.0.1","HostPort":"3000"}]}' ;;
      *Image*) printf '%s' "$STUB_IMAGE" ;;
      *State.Status*) printf '%s' "$STUB_STATE" ;;
      *) echo "unexpected podman inspect: $*" >&2; exit 1 ;;
    esac ;;
  rm) log "$*" ;;
  start)
    log "start"
    if [ "$STUB_START_FAILS" = yes ]; then
      echo "crun: cannot stat the bind-mount source: No such file or directory" >&2
      exit 125
    fi ;;
  run)
    log "$*"
    if [ "$STUB_RUN_FAILS" = yes ]; then echo "Error: engine failure" >&2; exit 125; fi
    script=""; prev=""
    for a in "$@"; do
      if [ "$prev" = "-c" ]; then script=$a; fi
      prev=$a
    done
    script=$(printf '%s' "$script" | sed -e "s|/var/lib/semaphore|$STUB_VOLUME|g" -e "s|/etc/semaphore|$STUB_ETC|g")
    case "$script" in
      *"/dev/stdin"*) if [ "$STUB_STDIN_RUN_FAILS" = yes ]; then echo "Error: write failed" >&2; exit 125; fi ;;
    esac
    exec sh -c "$script" ;;
  cp)
    log "$*"
    if [ -f "$STUB_ETC/config.json" ]; then
      if [ "$3" = "-" ]; then tar -cf - -C "$STUB_ETC" config.json; exit $?; fi
      cp "$STUB_ETC/config.json" "$3"; exit $?
    fi
    echo 'Error: "/etc/semaphore/config.json" could not be found on container' >&2
    exit 125 ;;
  exec)
    log "exec"
    script=$(printf '%s' "$5" | sed -e "s|/var/lib/semaphore|$STUB_VOLUME|g" -e "s|/etc/semaphore|$STUB_ETC|g")
    exec sh -c "$script" ;;
  *) echo "unexpected podman call: $*" >&2; exit 1 ;;
esac
"""

# The debug task prints what the two registered tasks reported. A task that did not run leaves its
# name undefined, so each lookup goes through default() first.
MSG = (
    "CARRY=[{{ ((_sem_key_carry | default({})).stdout_lines | default([])) | join(',') }}] "
    "CARRY_CHANGED=[{{ (_sem_key_carry | default({})).changed | default(false) }}] "
    "RECREATE=[{{ ((_sem_recreate | default({})).stdout_lines | default([])) | join(',') }}]"
)


class Run(NamedTuple):
    out: subprocess.CompletedProcess
    lines: list
    verbs: list
    volume_file: Path
    etc_file: Path
    state_dir: Path


def _run(tmp_path: Path, tasks: list, *, exists: bool = True, state: str = "running",
         volume_config: str | None = None, etc_config: str | None = None, mount: str = VOLUME_NAME,
         start_fails: bool = False, run_fails: bool = False, stdin_run_fails: bool = False,
         repo_src: str | None = None, share_src: str = DEPLOY_SHARE) -> Run:
    """Run the named tasks, then the debug task, against the stub. The checkout the playbook is
    run from is tmp_path/checkout, so a matching bind source is the default."""
    checkout = tmp_path / "checkout"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    podman = bindir / "podman"
    podman.write_text(STUB)
    podman.chmod(0o755)
    volume = tmp_path / "volume"
    etc = tmp_path / "etc"
    state_dir = tmp_path / "state"
    volume.mkdir()
    etc.mkdir()
    if volume_config is not None:
        (volume / "config").mkdir()
        (volume / "config/config.json").write_text(volume_config)
    if etc_config is not None:
        (etc / "config.json").write_text(etc_config)
    play_vars = {**_play()["vars"], "_state_dir": str(state_dir), "_repo_host_dir": str(checkout),
                 "_oidc_ready": False}
    play = tmp_path / "play.yml"
    play.write_text(json.dumps([{
        "hosts": "localhost", "connection": "local", "gather_facts": False, "vars": play_vars,
        "tasks": [_task(name) for name in tasks] + [{"ansible.builtin.debug": {"msg": MSG}}],
    }]))
    log = tmp_path / "engine.log"
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env.update(ANSIBLE_NOCOLOR="1", ANSIBLE_LOCAL_TEMP=str(tmp_path), ANSIBLE_REMOTE_TEMP=str(tmp_path),
               PATH=f"{bindir}:{os.environ['PATH']}", STUB_EXISTS="yes" if exists else "no",
               STUB_STATE=state, STUB_LOG=str(log), STUB_VOLUME=str(volume), STUB_ETC=str(etc),
               STUB_MOUNT=mount, STUB_IMAGE=IMAGE_ID,
               STUB_DESTS=("/run/podman/podman.sock /workspace/agent-cloud "
                           "/var/lib/agent-cloud-deploy /var/lib/semaphore "),
               STUB_REPO_SRC=repo_src or str(checkout), STUB_SHARE_SRC=share_src,
               STUB_START_FAILS="yes" if start_fails else "no", STUB_RUN_FAILS="yes" if run_fails else "no",
               STUB_STDIN_RUN_FAILS="yes" if stdin_run_fails else "no")
    out = subprocess.run(["ansible-playbook", "-i", "localhost,", str(play)], cwd=REPO, env=env, text=True,
                         capture_output=True, check=False, timeout=120, stdin=subprocess.DEVNULL)
    lines = log.read_text().splitlines() if log.exists() else []
    verbs = [line.split()[0] for line in lines if line.strip()]
    return Run(out, lines, verbs, volume / "config/config.json", etc / "config.json", state_dir)


@needs_ansible
@pytest.mark.parametrize(
    ("kwargs", "result", "changed", "verbs"),
    [
        ({"exists": False}, "absent", False, []),  # fresh install: the start task generates the key on the volume
        # running, key on the volume: the probe answers, nothing is executed
        ({"volume_config": VOLUME_KEY, "etc_config": KEY}, "present", False, ["run"]),
        ({"etc_config": KEY}, "carried", True, ["run", "exec"]),  # container layer only: copy it onto the volume
        ({"state": "exited", "etc_config": KEY}, "started,carried", True, ["run", "start", "exec"]),
        # the volume has the key: the container is never started
        ({"state": "exited", "volume_config": VOLUME_KEY}, "present", False, ["run"]),
        # the volume has the key and a start would fail: still present, still no start
        ({"state": "exited", "volume_config": VOLUME_KEY, "start_fails": True}, "present", False, ["run"]),
        # no key on the volume, start fails, the layer has the key: copy it through stdin
        ({"state": "exited", "etc_config": KEY, "start_fails": True}, "carried", True, ["run", "start", "cp", "run"]),
    ],
)
def test_the_carry_copies_only_when_the_volume_has_no_key(tmp_path, kwargs, result, changed, verbs):
    run = _run(tmp_path, [CARRY], **kwargs)
    assert run.out.returncode == 0, run.out.stdout + run.out.stderr
    assert f"CARRY=[{result}] CARRY_CHANGED=[{changed}]" in run.out.stdout
    assert run.verbs == verbs
    assert KEY not in run.out.stdout + run.out.stderr
    assert VOLUME_KEY not in run.out.stdout + run.out.stderr
    if kwargs.get("volume_config") is not None:
        assert run.volume_file.read_text() == kwargs["volume_config"]
    elif result.endswith("carried"):
        assert run.volume_file.read_text() == KEY
        assert stat.S_IMODE(run.volume_file.stat().st_mode) == 0o600
        assert run.etc_file.read_text() == KEY
    else:
        assert not run.volume_file.exists()
    assert not run.state_dir.exists(), "nothing is written to the workstation disk"


@needs_ansible
def test_the_probe_runs_from_the_containers_own_image_read_only_and_never_pulls(tmp_path):
    run = _run(tmp_path, [CARRY], state="exited", volume_config=VOLUME_KEY)
    assert run.out.returncode == 0, run.out.stdout + run.out.stderr
    assert run.verbs == ["run"]
    probe = run.lines[0]
    assert "--pull=never" in probe
    assert f"-v {VOLUME_NAME}:/var/lib/semaphore:ro" in probe
    assert f"{IMAGE_ID} -c test -f /var/lib/semaphore/config/config.json" in probe


@needs_ansible
def test_a_layer_key_reaches_the_volume_through_stdin_and_nothing_is_left_on_disk(tmp_path):
    run = _run(tmp_path, [CARRY], state="exited", etc_config=KEY, start_fails=True)
    assert run.out.returncode == 0, run.out.stdout + run.out.stderr
    copy = run.lines[-1]
    assert copy.startswith("run --rm -i --pull=never --entrypoint sh")
    assert "cp /dev/stdin" in copy and "mv" in copy
    assert run.volume_file.read_text() == KEY
    assert not (run.volume_file.parent / "config.json.tmp").exists()
    assert not run.state_dir.exists()


@needs_ansible
def test_a_failed_stdin_write_fails_the_play_and_never_removes_the_container(tmp_path):
    """The write of the layer key is the only step that changes the volume. If its failure did not fail
    the play, the carry would report carried and the recreate would remove the container."""
    run = _run(tmp_path, [CARRY, RECREATE], state="exited", etc_config=KEY, start_fails=True,
               repo_src=STALE_CHECKOUT, stdin_run_fails=True)
    assert run.out.returncode != 0, run.out.stdout + run.out.stderr
    assert run.verbs == ["run", "start", "cp", "run"]
    assert not any(line.startswith("rm") for line in run.lines)
    assert not run.volume_file.exists()
    assert not run.state_dir.exists()


@needs_ansible
def test_a_probe_error_fails_the_task_and_never_starts_the_container(tmp_path):
    run = _run(tmp_path, [CARRY], state="exited", etc_config=KEY, run_fails=True)
    assert run.out.returncode != 0
    assert run.verbs == ["run"]
    assert not run.volume_file.exists()


@needs_ansible
def test_a_container_that_cannot_start_without_a_named_volume_fails_the_task(tmp_path):
    run = _run(tmp_path, [CARRY], state="exited", etc_config=KEY, start_fails=True, mount="")
    assert run.out.returncode != 0
    assert run.verbs == ["start"]


@needs_ansible
def test_no_key_anywhere_reports_none_and_fails_the_task(tmp_path):
    run = _run(tmp_path, [CARRY], state="exited", start_fails=True)
    assert run.out.returncode != 0
    assert run.verbs == ["run", "start", "cp"]
    assert not run.volume_file.exists()
    assert not run.state_dir.exists()


@needs_ansible
def test_a_running_container_with_no_key_anywhere_fails_the_task(tmp_path):
    run = _run(tmp_path, [CARRY])
    assert run.out.returncode != 0, run.out.stdout
    assert run.verbs == ["run", "exec"]
    assert not run.volume_file.exists()


@needs_ansible
def test_the_recreate_keeps_a_container_whose_sources_match(tmp_path):
    run = _run(tmp_path, [RECREATE])
    assert run.out.returncode == 0, run.out.stdout + run.out.stderr
    assert "RECREATE=[ok]" in run.out.stdout
    assert run.lines == []


@needs_ansible
def test_the_recreate_replaces_a_container_whose_checkout_source_is_gone(tmp_path):
    run = _run(tmp_path, [RECREATE], repo_src=STALE_CHECKOUT)
    assert run.out.returncode == 0, run.out.stdout + run.out.stderr
    assert "RECREATE=[recreated]" in run.out.stdout
    assert run.lines == ["rm -f local-semaphore"], "rm without -v: the data volume is kept"


@needs_ansible
def test_the_recreate_replaces_a_container_whose_deploy_share_source_differs(tmp_path):
    run = _run(tmp_path, [RECREATE], share_src="/srv/elsewhere")
    assert run.out.returncode == 0, run.out.stdout + run.out.stderr
    assert "RECREATE=[recreated]" in run.out.stdout
    assert run.lines == ["rm -f local-semaphore"]


@needs_ansible
def test_a_stale_checkout_with_the_key_on_the_volume_is_carried_then_recreated_with_the_volume_kept(tmp_path):
    run = _run(tmp_path, [CARRY, RECREATE], state="exited", volume_config=VOLUME_KEY,
               start_fails=True, repo_src=STALE_CHECKOUT)
    assert run.out.returncode == 0, run.out.stdout + run.out.stderr
    assert "CARRY=[present]" in run.out.stdout
    assert "RECREATE=[recreated]" in run.out.stdout
    assert run.verbs == ["run", "rm"], "the carry probes and returns before any start; then one rm"
    assert run.volume_file.read_text() == VOLUME_KEY
