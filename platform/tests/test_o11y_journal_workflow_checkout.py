"""Focused tests for the receiver checkout verifier used by read-only surveys."""

import importlib.util
import os
import shlex
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "platform/playbooks/files/o11y-journal-workflow.py"
SPEC = importlib.util.spec_from_file_location("o11y_journal_workflow", HELPER)
WORKFLOW = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(WORKFLOW)


def _git(checkout, *arguments, **kwargs):
    environment = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    return subprocess.run(
        [str(WORKFLOW.GIT_EXECUTABLE), "-C", str(checkout), *arguments],
        env=environment,
        **kwargs,
    )


@pytest.fixture
def clean_receiver_checkout(tmp_path):
    if not WORKFLOW.GIT_EXECUTABLE.is_file():
        pytest.skip("fixed Git executable is unavailable")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _git(checkout, "init", "-q", check=True)
    (checkout / "tracked.txt").write_text("clean\n", encoding="utf-8")
    _git(checkout, "add", "tracked.txt", check=True)
    _git(checkout, "commit", "-qm", "fixture", check=True)
    revision = _git(
        checkout, "rev-parse", "HEAD", capture_output=True, text=True, check=True,
    ).stdout.strip()
    _git(checkout, "update-ref", "refs/remotes/origin/dev", revision, check=True)
    return checkout, revision


def test_verifies_exact_clean_checkout_and_rejects_bad_sha(clean_receiver_checkout):
    checkout, revision = clean_receiver_checkout
    assert WORKFLOW.verify_checkout(checkout, revision) == {
        "status": "verified",
        "revision": revision,
        "origin_dev_revision": revision,
        "clean": True,
    }
    with pytest.raises(WORKFLOW.Refusal, match="checkout_revision_mismatch"):
        WORKFLOW.verify_checkout(checkout, "0" * 40)
    with pytest.raises(WORKFLOW.Refusal, match="expected_revision_invalid"):
        WORKFLOW.verify_checkout(checkout, "not-a-sha")


@pytest.mark.parametrize("dirty_path", ["tracked.txt", "untracked.txt"])
def test_refuses_tracked_and_untracked_dirt(clean_receiver_checkout, dirty_path):
    checkout, revision = clean_receiver_checkout
    (checkout / dirty_path).write_text("dirty\n", encoding="utf-8")
    with pytest.raises(WORKFLOW.Refusal, match="checkout_dirty_or_unknown"):
        WORKFLOW.verify_checkout(checkout, revision)


def test_refuses_wrong_root_before_running_status(clean_receiver_checkout, monkeypatch):
    checkout, revision = clean_receiver_checkout
    nested = checkout / "nested"
    nested.mkdir()
    real_run = subprocess.run
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return real_run(argv, **kwargs)

    monkeypatch.setattr(WORKFLOW.subprocess, "run", run)
    with pytest.raises(WORKFLOW.Refusal, match="checkout_root_mismatch"):
        WORKFLOW.verify_checkout(nested, revision)
    assert not any("status" in argv for argv in calls)


@pytest.mark.parametrize(
    ("ref", "reason"),
    [(None, "checkout_dev_ref_unavailable"), ("1" * 40, "checkout_dev_ref_mismatch")],
    ids=["missing-dev-ref", "stale-dev-ref"],
)
def test_refuses_missing_or_stale_dev_ref(clean_receiver_checkout, ref, reason, monkeypatch):
    checkout, revision = clean_receiver_checkout
    if ref is None:
        _git(checkout, "update-ref", "-d", "refs/remotes/origin/dev", check=True)
    else:
        real_run = subprocess.run

        def stale_ref(argv, **kwargs):
            if argv[-1] == "refs/remotes/origin/dev^{commit}":
                return subprocess.CompletedProcess(argv, 0, f"{ref}\n", "")
            return real_run(argv, **kwargs)

        monkeypatch.setattr(WORKFLOW.subprocess, "run", stale_ref)
    with pytest.raises(WORKFLOW.Refusal, match=reason):
        WORKFLOW.verify_checkout(checkout, revision)


def test_uses_only_the_fixed_git_executable(clean_receiver_checkout, monkeypatch, tmp_path):
    checkout, revision = clean_receiver_checkout
    monkeypatch.setattr(WORKFLOW, "GIT_EXECUTABLE", tmp_path / "missing-git")
    with pytest.raises(WORKFLOW.Refusal, match="git_unavailable"):
        WORKFLOW.verify_checkout(checkout, revision)


def test_disables_checkout_fsmonitor_before_status(clean_receiver_checkout, tmp_path):
    checkout, revision = clean_receiver_checkout
    marker = tmp_path / "fsmonitor-ran"
    hook = tmp_path / "fsmonitor-hook"
    hook.write_text(
        f"#!/bin/sh\nprintf invoked > {shlex.quote(str(marker))}\nprintf 'token\\n'\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    _git(checkout, "config", "core.fsmonitor", str(hook), check=True)
    _git(checkout, "status", "--porcelain=v1", capture_output=True, text=True, check=False)
    assert marker.exists()
    marker.unlink()

    assert WORKFLOW.verify_checkout(checkout, revision)["status"] == "verified"
    assert not marker.exists()
