"""The shared tasks take the local-dev mode from the inventory, so a forged local_mode cannot move them (AC-726).

Each test runs one shared task on a PRODUCTION inventory with a forged `local_mode`, and asserts that the
production branch ran. Four tasks, two forgery forms:
- place-monorepo and enable-linger run the identity check (tasks/assert-local-mode-identity.yml) first, which
  refuses a plain or templated forgery it can see. Their forgery is the render-count form (forgeries.honest_for):
  honest for exactly the renders the check makes on the same host, measured once per module by
  forgeries.counting, then forged, so a raw read after the check takes the forgery.
- install-podman-compose and deploy-orb-agent include no check, so their tests use the templated forms.
A raw read that a test reaches fails here: the test sees the local branch and expects the production one.
Some reads are not reached and only the static ratchet (test_local_mode_reads.py) covers them: place-monorepo's
tasks after its branch guard (the guard refuses here, so nothing after it runs), and enable-linger's "Enable
lingering" and boot-unit tasks, which are check-mode or stat gated and so do not run under --check.

Every run uses one host (localhost): the counter is a file, and two parallel hosts race on it. Each test double
(git, getent, podman, sudo, the container engine) is put first on PATH by harness_sandbox.fake_bin, so the
test never runs the host's own command.
"""

from pathlib import Path

import forgeries
import pytest
from ansible_python import needs_ansible
from harness_sandbox import fake_bin
from local_mode_inventories import PROD_INI, include_play, passed, refused, run, task_statuses

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
# The sudo that become and the one explicit `sudo` in deploy-orb-agent call. Each drops the flags, adds one to
# AGENT_CLOUD_BECOME and runs the rest as this user, so a call's value counts the sudos it ran under: become
# alone is 1, become plus the task's own `sudo` prefix is 2.
SUDO = (
    'while [ $# -gt 0 ]; do case "$1" in -H|-S|-n) shift ;; -u) shift 2 ;; --) shift; break ;; *) break ;; esac; done\n'
    'AGENT_CLOUD_BECOME=$((${AGENT_CLOUD_BECOME:-0}+1)); export AGENT_CLOUD_BECOME\n'
    'exec "$@"\n'
)


def _engine(log: Path) -> str:
    """A container engine that records each subcommand and how many sudos it ran under, and answers inspect."""
    return (f'echo "$1 become=${{AGENT_CLOUD_BECOME:-0}}" >> "{log}"\n'
            'case "$1" in inspect) echo true ;; esac\n'
            'exit 0\n')


@pytest.fixture(scope="module")
def identity_renders(tmp_path_factory) -> int:
    """How many renders of local_mode the identity check makes on a production inventory, on localhost.
    Measured once per module: the count does not depend on the test that uses it."""
    count = tmp_path_factory.mktemp("identity")
    extra, counter = forgeries.counting("local_mode", False, count)
    proc = run(count, PROD_INI, include_play(IDENTITY, hosts="localhost"), "-e", extra)
    assert proc.returncode == 0, proc.output
    return int(counter.read_text())


def test_place_monorepo_takes_the_production_branch_past_the_identity_check(tmp_path, identity_renders):
    play = include_play(PLACE, hosts="localhost", play_vars={
        "_monorepo_dir": str(tmp_path / "dst"),
        "_branch": "main",
        "monorepo_repo": "https://example.invalid/agent-cloud.git",
    })
    # Only the production branch runs the branch guard (its `when` reads the inventory's identity, not
    # local_mode), and it refuses.
    forge = forgeries.honest_for("local_mode", False, True, identity_renders, tmp_path)
    proc = run(tmp_path, PROD_INI, play, "-e", forge, "--check", env=fake_bin(tmp_path, {"git": GIT}))
    refused(proc, "cross_branch_placement_refused")


PROD_LINGER_REACHED = (
    "Check whether lingering is already enabled for testuser",
    "Look up the account that owns the rootless containers",
    "Check the host ships podman's user boot unit",
)


def test_enable_linger_takes_the_production_branch_past_the_identity_check(tmp_path, identity_renders):
    play = include_play(LINGER, hosts="localhost", play_vars={"linger_user": "testuser"})
    forge = forgeries.honest_for("local_mode", False, True, identity_renders, tmp_path)
    # --check: "Enable lingering" and the boot-unit block are check-mode or stat guarded, so --check cannot
    # show them run; the three named here are reached under --check and report ok on the production branch.
    proc = run(tmp_path, PROD_INI, play, "-e", forge, "--check", env=fake_bin(tmp_path, {"getent": GETENT}))
    passed(proc)
    statuses = task_statuses(proc.stdout)
    for name in PROD_LINGER_REACHED:
        assert statuses.get(name) == "ok", (name, statuses, proc.output)


# Plain and context only: install-podman-compose has no identity check before its reads, so the stateful form is
# honest on the first render and cannot show a raw read of local_mode if one were reintroduced. Plain and
# context can.
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
    # The start carries the task's own `sudo` prefix on production, inside become: run counts both (2). The
    # other engine calls run under become alone (1).
    assert set(calls) == {"run become=2", *(f"{sub} become=1" for sub in ("stop", "rm", "inspect", "logs"))}, calls
