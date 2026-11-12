"""The shared tasks take the local-dev mode from the inventory, so a forged local_mode cannot move them (AC-726).

Each test runs one shared task on a PRODUCTION inventory with a forged `local_mode`, and asserts that
the production branch ran. The identity check (tasks/assert-local-mode-identity.yml) refuses a forged
value it can see, so a plain or templated forgery shows nothing past it: place-monorepo and enable-linger
run that check first. Their forgery is the render-count form (forgeries.honest_for): honest for exactly
the renders the check makes on the same host, measured first by forgeries.counting, then forged, so a
raw read after the check takes the forgery. install-podman-compose and deploy-orb-agent have no check, so
their tests use the templated forms. A raw read of local_mode in any of the five tasks fails here: the
test for it sees the local branch and expects the production one.

Every run uses one host (localhost): the counter is a file, and two parallel hosts race on it. Each
test double (git, getent, podman, sudo, the container engine) is put first on PATH by fake_bin, so the
test never runs the host's own command.
"""

from pathlib import Path

import forgeries
import pytest
from ansible_python import needs_ansible
from local_mode_inventories import PROD_INI, fake_bin, include_play, passed, refused, run, task_statuses

REPO = Path(__file__).resolve().parents[2]
TASKS = REPO / "platform/playbooks/tasks"
IDENTITY = TASKS / "assert-local-mode-identity.yml"
PLACE = TASKS / "place-monorepo.yml"
LINGER = TASKS / "enable-linger.yml"
PODMAN_COMPOSE = TASKS / "install-podman-compose.yml"
ORB = TASKS / "deploy-orb-agent.yml"

pytestmark = needs_ansible

# A checkout on a feature branch: place-monorepo's branch guard refuses main from it (line 42).
GIT = 'case "$*" in *"rev-parse --abbrev-ref HEAD"*) echo feature-x ;; *) exit 1 ;; esac\n'
GETENT = 'echo "testuser:x:1000:1000::/var/empty:/bin/sh"\n'
# The sudo that become and the one explicit `sudo` in deploy-orb-agent call: it drops the flags, marks the
# run, and runs the rest as this user, so a call's AGENT_CLOUD_BECOME says whether it ran under become.
SUDO = (
    'while [ $# -gt 0 ]; do case "$1" in -H|-S|-n) shift ;; -u) shift 2 ;; --) shift; break ;; *) break ;; esac; done\n'
    'AGENT_CLOUD_BECOME=1; export AGENT_CLOUD_BECOME\n'
    'exec "$@"\n'
)


def _engine(log: Path) -> str:
    """A container engine that records each subcommand and whether it ran under sudo, and answers inspect."""
    return (f'echo "$1 become=${{AGENT_CLOUD_BECOME:-0}}" >> "{log}"\n'
            'case "$1" in inspect) echo true ;; esac\n'
            'exit 0\n')


def _identity_renders(tmp_path: Path) -> int:
    """How many renders of local_mode the identity check makes on a production inventory, on localhost."""
    count = tmp_path / "count"
    count.mkdir()
    extra, counter = forgeries.counting("local_mode", False, count)
    proc = run(count, PROD_INI, include_play(IDENTITY, hosts="localhost"), "-e", extra)
    assert proc.returncode == 0, proc.output
    return int(counter.read_text())


def test_place_monorepo_takes_the_production_branch_past_the_identity_check(tmp_path):
    renders = _identity_renders(tmp_path)
    play = include_play(PLACE, hosts="localhost", play_vars={
        "_monorepo_dir": str(tmp_path / "dst"),
        "_branch": "main",
        "monorepo_repo": "https://example.invalid/agent-cloud.git",
    })
    # Only the production branch runs the branch guard (its `when` is not local_mode), and it refuses.
    forge = forgeries.honest_for("local_mode", False, True, renders, tmp_path)
    proc = run(tmp_path, PROD_INI, play, "-e", forge, "--check", env=fake_bin(tmp_path, {"git": GIT}))
    refused(proc, "cross_branch_placement_refused")


PROD_LINGER_REACHED = (
    "Check whether lingering is already enabled for testuser",
    "Look up the account that owns the rootless containers",
    "Check the host ships podman's user boot unit",
)


def test_enable_linger_takes_the_production_branch_past_the_identity_check(tmp_path):
    renders = _identity_renders(tmp_path)
    play = include_play(LINGER, hosts="localhost", play_vars={"linger_user": "testuser"})
    forge = forgeries.honest_for("local_mode", False, True, renders, tmp_path)
    # --check: "Enable lingering" and the boot-unit block are check-mode or stat guarded, so --check cannot
    # show them run; the three named here are reached under --check and report ok on the production branch.
    proc = run(tmp_path, PROD_INI, play, "-e", forge, "--check", env=fake_bin(tmp_path, {"getent": GETENT}))
    passed(proc)
    statuses = task_statuses(proc.stdout)
    for name in PROD_LINGER_REACHED:
        assert statuses.get(name) == "ok", (name, statuses, proc.output)


# Plain and context only: install-podman-compose reads the mode once and has no check before it, so its
# first render is the only one. The stateful form is honest on that render and cannot show the read.
@pytest.mark.parametrize("forge", forgeries.templated_forgeries("local_mode", honest=False, forged=True)[:2])
def test_install_podman_compose_takes_the_production_branch_under_a_forged_local_mode(tmp_path, forge):
    # Linux with a podman-compose version that has no pinned wheel: the production branch refuses it, the
    # local branch skips the block. ansible_system is a play var so the minimal-facts task does not run.
    play = include_play(PODMAN_COMPOSE, hosts="localhost", play_vars={
        "ansible_system": "Linux",
        "podman_compose_version": "0.0.0",
    })
    proc = run(tmp_path, PROD_INI, play, "-e", forge(tmp_path), "--check",
               env=fake_bin(tmp_path, {"podman": "exit 0\n"}))
    refused(proc, "has no pinned wheel sha256")


@pytest.mark.parametrize("forge", forgeries.templated_forgeries("local_mode", honest=False, forged=True))
def test_deploy_orb_agent_runs_every_engine_call_under_sudo_on_production(tmp_path, forge):
    mono = tmp_path / "mono"
    (mono / "deploy" / "discovery").mkdir(parents=True)
    (mono / "deploy" / "discovery" / "agent.yaml").write_text("{}\n")
    log = tmp_path / "engine.log"
    play = include_play(ORB, hosts="localhost", play_vars={
        "_monorepo_dir": str(mono),
        "monorepo_deploy_path": "deploy",
        "container_engine": "fake-engine",
    })
    env = fake_bin(tmp_path, {"sudo": SUDO, "fake-engine": _engine(log)})
    proc = run(tmp_path, PROD_INI, play, "-e", forge(tmp_path), env=env)
    passed(proc)
    calls = log.read_text().splitlines()
    assert set(calls) == {f"{sub} become=1" for sub in ("stop", "rm", "run", "inspect", "logs")}, calls
