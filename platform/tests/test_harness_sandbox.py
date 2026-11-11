"""Tests for bounded, output-safe subprocess execution in harness_sandbox."""

import subprocess
import sys
import time
import traceback

import harness_sandbox
import pytest


def test_run_timeout_suppresses_captured_child_output(tmp_path, monkeypatch):
    # Exercise the wrapper as it runs on GitHub-hosted Linux, where there is no kernel sandbox.
    monkeypatch.setattr(harness_sandbox, "SANDBOX", None)
    secret_marker = b"tempo-fixture-secret-must-not-escape"
    command = ["fixture-command"]

    def timeout_with_secret_output(cmd, **kwargs):
        raise subprocess.TimeoutExpired(
            cmd,
            kwargs["timeout"],
            output=secret_marker,
            stderr=secret_marker,
        )

    monkeypatch.setattr(harness_sandbox.subprocess, "run", timeout_with_secret_output)
    with pytest.raises(RuntimeError, match="test command timed out after 0.1s") as error:
        harness_sandbox.run(
            command,
            tmp_path,
            cwd=tmp_path,
            env=harness_sandbox.env_for(tmp_path),
            timeout=0.1,
        )

    assert "captured output suppressed" in str(error.value)
    assert secret_marker.decode() not in "".join(traceback.format_exception(error.value))
    assert error.value.__suppress_context__ is True
    assert error.value.__cause__ is None


def test_run_enforces_subprocess_timeout(tmp_path, monkeypatch):
    # Exercise the actual child-process timeout independently of the deterministic no-leak test.
    monkeypatch.setattr(harness_sandbox, "SANDBOX", None)
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="test command timed out after 0.1s"):
        harness_sandbox.run(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            tmp_path,
            cwd=tmp_path,
            env=harness_sandbox.env_for(tmp_path),
            timeout=0.1,
        )
    assert time.monotonic() - started < 2


def test_run_gives_the_child_no_stdin(tmp_path, monkeypatch):
    # A prompt in a playbook must fail at once, not hold the worker until the timeout.
    monkeypatch.setattr(harness_sandbox, "SANDBOX", None)
    seen = {}
    monkeypatch.setattr(harness_sandbox.subprocess, "run", lambda cmd, **kwargs: seen.update(kwargs))
    harness_sandbox.run(["true"], tmp_path, cwd=tmp_path, env={})
    assert seen["stdin"] is subprocess.DEVNULL
    assert seen["timeout"] == harness_sandbox.DEFAULT_TIMEOUT_SECONDS


def test_run_playbook_builds_the_command_environment_and_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(harness_sandbox, "SANDBOX", None)
    monkeypatch.setenv("ANSIBLE_CONFIG", "/elsewhere/ansible.cfg")
    seen = {}
    monkeypatch.setattr(harness_sandbox.subprocess, "run", lambda cmd, **kwargs: seen.update(kwargs, cmd=cmd))
    harness_sandbox.run_playbook(tmp_path / "p.yml", tmp_path, "--check", "-e", "x=1",
                                 inventory="inv.yml", env={"EXTRA": "1", "ANSIBLE_CONFIG": "/x.cfg"}, timeout=7)
    assert seen["cmd"] == ["ansible-playbook", "-i", "inv.yml", str(tmp_path / "p.yml"), "--check", "-e", "x=1"]
    assert seen["cwd"] == harness_sandbox.REPO
    assert (harness_sandbox.REPO / "ansible.cfg").is_file()
    assert "ANSIBLE_CONFIG" not in seen["env"]  # the repository's ansible.cfg applies via cwd
    assert seen["env"]["EXTRA"] == "1" and seen["env"]["ANSIBLE_NOCOLOR"] == "1"
    assert seen["env"]["ANSIBLE_LOCAL_TEMP"].startswith(str(tmp_path))
    assert seen["timeout"] == 7


def test_run_playbook_runs_a_real_playbook(tmp_path):
    play = tmp_path / "play.yml"
    play.write_text('- hosts: localhost\n  gather_facts: false\n  connection: local\n'
                    '  tasks:\n    - debug: {msg: "ran-from-the-shared-runner"}\n')
    proc = harness_sandbox.run_playbook(play, tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ran-from-the-shared-runner" in proc.stdout
