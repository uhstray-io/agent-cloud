"""Bounded shared checkout consistency verifier contracts."""

import base64
import importlib.util
import os
import shlex
import stat
import subprocess
import time
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "platform/playbooks/files/reviewed-checkout.py"
SPEC = importlib.util.spec_from_file_location("reviewed_checkout", HELPER)
CHECKOUT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECKOUT)


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
        [CHECKOUT.GIT, "-C", str(checkout), *arguments], env=environment, **kwargs,
    )


@pytest.fixture
def clean_checkout(tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _git(checkout, "init", "-q", check=True)
    (checkout / "tracked.txt").write_bytes(b"tracked\n")
    (checkout / "helper.py").write_bytes(b"helper = 'exact bytes'  \n")
    os.symlink("tracked.txt", checkout / "link")
    _git(checkout, "add", "tracked.txt", "helper.py", "link", check=True)
    _git(checkout, "commit", "-qm", "fixture", check=True)
    revision = _git(checkout, "rev-parse", "HEAD", capture_output=True, text=True, check=True).stdout.strip()
    _git(checkout, "update-ref", "refs/remotes/origin/dev", revision, check=True)
    return checkout, revision


def _verify(checkout, revision, paths=None, require_dev=False):
    return CHECKOUT.verify_checkout(checkout, revision, paths or [], require_dev)


def test_verifies_clean_checkout_and_returns_exact_retained_payload(clean_checkout):
    checkout, revision = clean_checkout
    before = (checkout / ".git/index").read_bytes()
    result = _verify(checkout, revision, ["helper.py"], require_dev=True)
    assert result["status"] == "checkout_consistent"
    assert result["revision"] == revision
    assert result["index_matches_revision"] is True
    assert result["tracked_bytes_match_revision"] is True
    assert result["local_dev_ref_matches_revision"] is True
    assert base64.b64decode(result["payloads"]["helper.py"]) == b"helper = 'exact bytes'  \n"
    assert (checkout / ".git/index").read_bytes() == before


@pytest.mark.parametrize(
    ("dirty_path", "reason"),
    [("tracked.txt", "checkout_worktree_bytes"), ("untracked.txt", "checkout_dirty_or_unknown")],
)
def test_refuses_tracked_or_untracked_changes(clean_checkout, dirty_path, reason):
    checkout, revision = clean_checkout
    (checkout / dirty_path).write_bytes(b"changed\n")
    with pytest.raises(CHECKOUT.Refusal, match=reason):
        _verify(checkout, revision)


def test_refuses_staged_index_drift(clean_checkout):
    checkout, revision = clean_checkout
    (checkout / "tracked.txt").write_bytes(b"staged\n")
    _git(checkout, "add", "tracked.txt", check=True)
    with pytest.raises(CHECKOUT.Refusal, match="checkout_index_mismatch"):
        _verify(checkout, revision)


def test_refuses_wrong_revision_and_nested_repository_root(clean_checkout):
    checkout, revision = clean_checkout
    with pytest.raises(CHECKOUT.Refusal, match="checkout_revision_mismatch"):
        _verify(checkout, "0" * 40)
    nested = checkout / "nested"
    nested.mkdir()
    with pytest.raises(CHECKOUT.Refusal, match="checkout_root_mismatch"):
        _verify(nested, revision)


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_rejects_index_shortcut_flags_without_rewriting_index(clean_checkout, flag):
    checkout, revision = clean_checkout
    _git(checkout, "update-index", flag, "tracked.txt", check=True)
    index = checkout / ".git/index"
    before = (index.read_bytes(), index.stat().st_mtime_ns, index.stat().st_size)
    with pytest.raises(CHECKOUT.Refusal, match="checkout_index_flags"):
        _verify(checkout, revision)
    assert (index.read_bytes(), index.stat().st_mtime_ns, index.stat().st_size) == before


def test_detects_same_size_bytes_with_restored_timestamp(clean_checkout):
    checkout, revision = clean_checkout
    path = checkout / "tracked.txt"
    old_stat = path.stat()
    path.write_bytes(b"changed\n")
    os.utime(path, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
    with pytest.raises(CHECKOUT.Refusal, match="checkout_worktree_bytes"):
        _verify(checkout, revision)


def test_refuses_tampered_requested_helper(clean_checkout):
    checkout, revision = clean_checkout
    (checkout / "helper.py").write_bytes(b"helper = 'tampered'  \n")
    with pytest.raises(CHECKOUT.Refusal, match="checkout_worktree_bytes"):
        _verify(checkout, revision, ["helper.py"])


def test_fsmonitor_marker_never_runs_and_index_is_unchanged(clean_checkout, tmp_path):
    checkout, revision = clean_checkout
    marker = tmp_path / "fsmonitor-ran"
    hook = tmp_path / "fsmonitor-hook"
    hook.write_text(
        f"#!/bin/sh\nprintf invoked > {shlex.quote(str(marker))}\nprintf 'token\\n'\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    _git(checkout, "config", "core.fsmonitor", str(hook), check=True)
    assert not marker.exists()
    index = checkout / ".git/index"
    before = (index.read_bytes(), index.stat().st_mtime_ns)
    assert _verify(checkout, revision)["status"] == "checkout_consistent"
    assert not marker.exists()
    assert (index.read_bytes(), index.stat().st_mtime_ns) == before


def test_untracked_scan_does_not_run_configured_clean_filter(clean_checkout, tmp_path):
    checkout, revision = clean_checkout
    marker = tmp_path / "clean-filter-ran"
    filter_command = f"/bin/sh -c 'touch {shlex.quote(str(marker))}; cat'"
    (checkout / ".gitattributes").write_text("tracked.txt filter=marker\n", encoding="utf-8")
    _git(checkout, "add", ".gitattributes", check=True)
    _git(checkout, "commit", "-qm", "configure clean filter", check=True)
    revision = _git(
        checkout, "rev-parse", "HEAD", capture_output=True, text=True, check=True,
    ).stdout.strip()
    _git(checkout, "update-ref", "refs/remotes/origin/dev", revision, check=True)
    _git(checkout, "config", "filter.marker.clean", filter_command, check=True)

    # Git only runs the clean filter on a tracked file whose stat data no longer
    # matches the index entry (the "racy git" rule): a stat-clean file is trusted
    # without being rehashed. Whether the fixture's stat data matched depended on
    # whether the index was written in the same second as the file, so under load
    # a plain status skipped the filter and the precondition below failed
    # intermittently. Force a stat mismatch (mtime only; the bytes are unchanged)
    # so git must rehash through the filter regardless of timing.
    tracked = checkout / "tracked.txt"
    os.utime(tracked, ns=(1_000_000_000_000_000_000, 1_000_000_000_000_000_000))
    index = checkout / ".git/index"
    index_before = index.read_bytes()

    assert not marker.exists()
    CHECKOUT._run_git(
        checkout,
        ("status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=none"),
        deadline=time.monotonic() + 2,
    )
    assert marker.exists()
    marker.unlink()
    # The status above takes no optional locks, so it did not refresh the index:
    # the stat mismatch is still present and would still trigger the filter if
    # _verify ran a plain status. Assert that rather than assume it.
    assert index.read_bytes() == index_before
    assert _verify(checkout, revision)["status"] == "checkout_consistent"
    assert not marker.exists()


def test_split_index_is_refused_without_modifying_either_index_file(clean_checkout):
    checkout, revision = clean_checkout
    _git(checkout, "update-index", "--split-index", check=True)
    index_paths = [checkout / ".git/index", *checkout.glob(".git/sharedindex.*")]
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_size)
        for path in index_paths
    }
    with pytest.raises(CHECKOUT.Refusal, match="checkout_split_index_unsupported"):
        _verify(checkout, revision)
    after_paths = [checkout / ".git/index", *checkout.glob(".git/sharedindex.*")]
    after = {
        path: (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_size)
        for path in after_paths
    }
    assert after == before


def test_untracked_cache_reads_do_not_rewrite_index(clean_checkout):
    checkout, revision = clean_checkout
    _git(checkout, "update-index", "--untracked-cache", check=True)
    index = checkout / ".git/index"
    before = (index.read_bytes(), index.stat().st_mtime_ns, index.stat().st_size)
    assert _verify(checkout, revision)["status"] == "checkout_consistent"
    assert (index.read_bytes(), index.stat().st_mtime_ns, index.stat().st_size) == before


def test_symlink_target_is_compared_as_link_bytes(clean_checkout):
    checkout, revision = clean_checkout
    assert _verify(checkout, revision)["status"] == "checkout_consistent"
    (checkout / "link").unlink()
    os.symlink("different-target", checkout / "link")
    with pytest.raises(CHECKOUT.Refusal, match="checkout_worktree_bytes"):
        _verify(checkout, revision)


def test_git_invocations_disable_writes_replacements_and_fetches():
    argv = CHECKOUT._git_argv(Path("/tmp/checkout"), "status")
    assert "--no-optional-locks" in argv
    assert "--no-replace-objects" in argv
    assert "core.fsmonitor=false" in argv
    environment = CHECKOUT._environment()
    assert environment["GIT_OPTIONAL_LOCKS"] == "0"
    assert environment["GIT_NO_LAZY_FETCH"] == "1"
    assert environment["GIT_NO_REPLACE_OBJECTS"] == "1"


def test_bounded_runner_discards_stderr_and_refuses_stdout_overflow(tmp_path, monkeypatch):
    fake_git = tmp_path / "fake-git"
    fake_git.write_text("#!/bin/sh\nhead -c 65536 /dev/zero\nhead -c 65536 /dev/zero >&2\n", encoding="utf-8")
    fake_git.chmod(0o755)
    monkeypatch.setattr(CHECKOUT, "GIT", str(fake_git))
    with pytest.raises(CHECKOUT.Refusal, match="checkout_output_limit"):
        CHECKOUT._run_git(Path("/tmp"), ("status",), output_limit=1000, deadline=time.monotonic() + 2)


def test_bounded_runner_kills_child_holding_stdout_pipe(tmp_path, monkeypatch):
    fake_git = tmp_path / "fake-git"
    fake_git.write_text("#!/bin/sh\nsleep 30 &\nwait\n", encoding="utf-8")
    fake_git.chmod(0o755)
    monkeypatch.setattr(CHECKOUT, "GIT", str(fake_git))
    monkeypatch.setattr(CHECKOUT, "COMMAND_TIMEOUT", 0.1)
    started = time.monotonic()
    with pytest.raises(CHECKOUT.Refusal, match="checkout_timeout"):
        CHECKOUT._run_git(Path("/tmp"), ("status",), deadline=time.monotonic() + 1)
    assert time.monotonic() - started < 2


def test_bounded_runner_kills_child_after_parent_exits(tmp_path, monkeypatch):
    fake_git = tmp_path / "fake-git"
    marker = tmp_path / "child-survived"
    child_pid = tmp_path / "child.pid"
    fake_git.write_text(
        f"#!/bin/sh\n(sleep 3; touch {shlex.quote(str(marker))}) &\n"
        f"echo $! > {shlex.quote(str(child_pid))}\nexit 0\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o755)
    monkeypatch.setattr(CHECKOUT, "GIT", str(fake_git))
    # The timeout must comfortably exceed the time a loaded machine needs to
    # start the fake git and let it write the pid file (a 0.5 s timeout let the
    # runner kill it first, so the pid file never appeared). The child's sleep
    # stays longer than the timeout so a survivor would still touch the marker.
    monkeypatch.setattr(CHECKOUT, "COMMAND_TIMEOUT", 1.5)
    started = time.monotonic()
    with pytest.raises(CHECKOUT.Refusal, match="checkout_timeout"):
        CHECKOUT._run_git(Path("/tmp"), ("status",), deadline=time.monotonic() + 3)
    assert time.monotonic() - started < 5
    time.sleep(3.5)
    assert child_pid.exists()
    assert not marker.exists()


def test_terminate_tolerates_eperm_from_killpg(tmp_path, monkeypatch):
    # macOS raises EPERM from killpg for a group holding only zombies.
    process = subprocess.Popen(["/bin/sh", "-c", "exit 0"], start_new_session=True)

    def refuse(_pid, _signal):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(CHECKOUT.os, "killpg", refuse)
    CHECKOUT._terminate(process)
    assert process.returncode is not None


def test_malformed_git_metadata_fails_closed():
    with pytest.raises(CHECKOUT.Refusal, match="checkout_metadata_invalid"):
        CHECKOUT._nul_records(b"unterminated")


def test_malformed_cli_request_emits_only_fixed_json(capsys):
    assert CHECKOUT.main(["--checkout", "/unused", "--expected-sha", "0" * 40, "--payload-paths", "{"]) == 1
    response = capsys.readouterr()
    assert response.err == ""
    assert response.out == '{"status":"refused","reason":"checkout_operation_failed"}\n'


def test_missing_cli_arguments_emit_no_argparse_diagnostics(capsys):
    assert CHECKOUT.main([]) == 1
    response = capsys.readouterr()
    assert response.err == ""
    assert response.out == '{"status":"refused","reason":"arguments_invalid"}\n'


def test_dev_ref_is_optional_for_generic_guard(clean_checkout):
    checkout, revision = clean_checkout
    assert "local_dev_ref_matches_revision" not in _verify(checkout, revision)
    _git(checkout, "update-ref", "-d", "refs/remotes/origin/dev", check=True)
    with pytest.raises(CHECKOUT.Refusal, match="checkout_dev_ref_unavailable"):
        _verify(checkout, revision, require_dev=True)


def test_rejects_unsupported_worktree_types_and_modes(clean_checkout):
    checkout, revision = clean_checkout
    path = checkout / "tracked.txt"
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    with pytest.raises(CHECKOUT.Refusal, match="checkout_worktree_mode"):
        _verify(checkout, revision)


def test_payload_limit_accepts_sixteen_and_refuses_seventeen(clean_checkout):
    checkout, _revision = clean_checkout
    paths = []
    for index in range(CHECKOUT.MAX_PAYLOAD_PATHS):
        relative = f"payload/item-{index:02d}.txt"
        path = checkout / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"payload {index}\n", encoding="utf-8")
        paths.append(relative)
    _git(checkout, "add", "payload", check=True)
    _git(checkout, "commit", "-qm", "add bounded payload fixtures", check=True)
    revision = _git(checkout, "rev-parse", "HEAD", capture_output=True, text=True, check=True).stdout.strip()
    _git(checkout, "update-ref", "refs/remotes/origin/dev", revision, check=True)

    result = _verify(checkout, revision, paths)
    assert set(result["payloads"]) == set(paths)
    with pytest.raises(CHECKOUT.Refusal, match="payload_paths_invalid"):
        _verify(checkout, revision, paths + ["payload/over-limit.txt"])


def test_reviewed_checkout_playbook_uses_same_sixteen_path_cap():
    plays = yaml.safe_load((ROOT / "platform/playbooks/require-reviewed-checkout.yml").read_text())
    assertions = plays[0]["tasks"][0]["ansible.builtin.assert"]["that"]
    assert "_reviewed_checkout_requested_payloads | length <= 16" in assertions
