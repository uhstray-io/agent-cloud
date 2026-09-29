"""Harden SSH's "password auth is rejected" check can FAIL.

The old probe ran `ssh -o BatchMode=yes -o PubkeyAuthentication=no` and passed on a
non-zero exit. BatchMode disables password prompts (man ssh_config), so that ssh exited
non-zero on every host, and the check was green whether or not the server still took
passwords. It now requires two views to agree: the methods the server advertises to the
runner (`ssh -v`, "Authentications that can continue:") and sshd's effective config
(`sshd -T`). Anything short of proof fails the host.

These lift the real tasks out of harden-ssh.yml and run them on localhost with `ssh` and
`sshd` replaced by stubs on PATH.
"""

import os
import shutil
from pathlib import Path

import harness_sandbox
import pytest
import test_check_mode_contract as check_mode_contract
import yaml

REPO = Path(__file__).resolve().parents[2]
PLAYBOOK = REPO / "platform/playbooks/harden-ssh.yml"
TASKS = [
    "Probe password auth from the runner (what the server offers)",
    "Read sshd's effective authentication settings",
    "Decide whether password auth is really off",
    "Confirm password auth disabled",
    "Lockdown complete",
]
LOCKED = "port 22\npasswordauthentication no\nkbdinteractiveauthentication no\npubkeyauthentication yes\n"

needs_ansible = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")

# SSH_STUB_METHODS: the list the server advertises, or `unreachable`, or `login` (rc 0).
SSH_STUB = r'''#!/bin/sh
case "$SSH_STUB_METHODS" in
  unreachable) echo "ssh: connect to host 192.0.2.10 port 22: Connection refused" >&2; exit 255 ;;
  login) echo SHOULD_NOT_REACH; exit 0 ;;
esac
echo "debug1: Authentications that can continue: $SSH_STUB_METHODS" >&2
echo "debug1: No more authentication methods to try." >&2
echo "tester@192.0.2.10: Permission denied ($SSH_STUB_METHODS)." >&2
exit 255
'''
SSHD_STUB = r'''#!/bin/sh
[ "$SSHD_STUB" = FAIL ] && { echo "sshd: no hostkeys available" >&2; exit 1; }
printf '%s' "$SSHD_STUB"
'''


def _tasks() -> dict:
    found = {}

    def walk(tasks):
        for task in tasks or []:
            found[task.get("name")] = task
            for key in ("block", "rescue", "always"):
                walk(task.get(key))

    for play in yaml.safe_load(PLAYBOOK.read_text()):
        walk(play.get("tasks"))
    return found


def _run(tmp_path: Path, methods: str, sshd: str = LOCKED, check: bool = False):
    by_name = _tasks()
    tasks = [by_name[name] for name in TASKS]
    harness = [{"hosts": "localhost", "connection": "local", "gather_facts": False,
                # ansible_become beats the task keyword, so the sshd read runs unprivileged
                "vars": {"ansible_user": "tester", "ansible_host": "192.0.2.10",
                         "service_name": "svc", "ansible_become": False,
                         # the probe runs inside the key block; stand in for its result
                         "_verify_key": {"known_hosts": str(tmp_path / "known_hosts")},
                         "_verify_pin": {"pinned": True, "addr": "192.0.2.10", "port": 22}},
                "tasks": tasks}]
    (tmp_path / "bin").mkdir()
    for name, body in (("ssh", SSH_STUB), ("sshd", SSHD_STUB)):
        stub = tmp_path / "bin" / name
        stub.write_text(body)
        stub.chmod(0o755)
    (tmp_path / "h.yml").write_text(yaml.safe_dump(harness))
    # Sandboxed like every harness that executes playbook tasks (harness_sandbox.py), and
    # guarded first: the lifted tasks must write no file at all.
    assert not check_mode_contract.violations_in(tasks, None, all_writes=True)
    env = harness_sandbox.env_for(tmp_path)
    env.update(SSH_STUB_METHODS=methods, SSHD_STUB=sshd, PATH=f"{tmp_path / 'bin'}:{os.environ.get('PATH', '')}")
    cmd = ["ansible-playbook", "-i", "localhost,", str(tmp_path / "h.yml")] + (["--check"] if check else [])
    return harness_sandbox.run(cmd, tmp_path, cwd=REPO, env=env)


@needs_ansible
def test_passes_only_when_both_views_say_no_password(tmp_path):
    out = _run(tmp_path, "publickey")
    assert out.returncode == 0, out.stdout[-2500:]
    assert "password auth REJECTED" in out.stdout


# The old check passed every one of these.
@needs_ansible
@pytest.mark.parametrize("methods,sshd,reason", [
    ("publickey,password", LOCKED, "the server still offers password"),
    ("publickey,keyboard-interactive", LOCKED, "the server still offers keyboard-interactive"),
    ("unreachable", LOCKED, "never reached the authentication stage"),
    ("login", LOCKED, "a login succeeded with public-key auth disabled"),
    ("publickey", LOCKED.replace("passwordauthentication no", "passwordauthentication yes"),
     "effective passwordauthentication is yes"),
    ("publickey", LOCKED.replace("kbdinteractiveauthentication no\n", ""),
     "effective kbdinteractiveauthentication is absent"),
    ("publickey", "FAIL", "sshd -T could not be read"),
], ids=["password-offered", "kbd-offered", "unreachable", "login", "effective-yes", "effective-absent",
        "sshd-unreadable"])
def test_fails_whenever_password_auth_is_not_proven_off(tmp_path, methods, sshd, reason):
    out = _run(tmp_path, methods, sshd)
    assert out.returncode != 0, f"false green: {out.stdout[-2500:]}"
    assert reason in out.stdout, out.stdout[-2500:]
    assert "Lockdown complete" not in out.stdout.split("Confirm password auth disabled", 1)[1]


@needs_ansible
def test_older_openssh_setting_name_is_read(tmp_path):
    # Before OpenSSH 8.7 sshd -T reports ChallengeResponseAuthentication for the same setting.
    legacy = LOCKED.replace("kbdinteractiveauthentication", "challengeresponseauthentication")
    assert _run(tmp_path, "publickey", legacy).returncode == 0


@needs_ansible
def test_dry_run_reports_the_current_state_without_failing(tmp_path):
    # Under --check the sshd edits were simulated, so a host that still takes passwords is
    # expected; the report must say so rather than claim REJECTED.
    out = _run(tmp_path, "publickey,password", check=True)
    assert out.returncode == 0, out.stdout[-2500:]
    assert "DRY RUN" in out.stdout and "NOT YET REJECTED" in out.stdout


def test_nothing_on_the_path_ignores_a_failure():
    by_name = _tasks()
    for name in TASKS:
        assert "ignore_errors" not in by_name[name], name
    probe = by_name[TASKS[0]]["ansible.builtin.command"]["argv"]
    assert "PubkeyAuthentication=no" in probe and "-v" in probe
