"""Root pytest configuration: the shard hook for CI's test matrix (plan 21 phase 2, D3).

Inert unless PYTEST_SHARD=k/N is set, so local runs and today's CI jobs are unchanged. When it is
set, every test file is assigned to one of N shards by platform/tests/shards.py, and the hook
ignores each file not assigned to shard k at collection time.

Why pytest_ignore_collect and not filtering items after collection: a file that is skipped is
never imported, so each xdist worker imports only its own shard. Filtering afterwards would import
the whole suite once per worker of every shard.

Why the hook never returns False: it is first-result, and False would override collect_ignore and
the __pycache__ handling. None leaves the path to pytest.

A test file the durations file does not know is weighted from its count of `def test_` lines and
bin-packed like any other file. A test file outside the enumeration (outside the testpaths or the
rootdir) is never skipped by shard 1, so such a gap runs more tests, never fewer.

Side effect even when inert: a root conftest.py without an __init__.py puts the repository root on
sys.path under pytest's default import mode, so root-level directories become importable namespace
packages during a test run. No test depends on that today.
"""

from __future__ import annotations

import fnmatch
import functools
import os
import sys
from pathlib import Path

import pytest

DURATIONS = Path("platform") / "tests" / "test_durations.json"


def _shard_lib(root: Path):
    """Import platform/tests/shards.py (stdlib only). Called only when PYTEST_SHARD is set."""
    tests_dir = str(root / "platform" / "tests")
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)
    import shards

    return shards


@functools.cache
def _assignment(root: Path, testpaths: tuple[str, ...], n: int) -> dict[str, int]:
    """The file-to-shard map, computed once per process: every xdist worker computes the same map."""
    lib = _shard_lib(root)
    files = lib.enumerate_files(root, list(testpaths))
    durations = lib.load_durations(root / DURATIONS)
    weights = lib.file_weights(files, durations, root)
    return lib.assign(files, weights, n)


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool | None:
    spec = os.environ.get("PYTEST_SHARD", "").strip()
    if not spec:
        return None
    root = config.rootpath.resolve()
    try:
        k, n = _shard_lib(root).parse_shard(spec)
    except ValueError as exc:
        raise pytest.UsageError(f"PYTEST_SHARD={spec!r}: {exc}") from exc
    if collection_path.is_dir() or not fnmatch.fnmatch(collection_path.name, "test_*.py"):
        return None
    try:
        rel = collection_path.resolve().relative_to(root).as_posix()
    except ValueError:
        rel = None
    assignment = _assignment(root, tuple(config.getini("testpaths")), n)
    owner = assignment.get(rel, 1) if rel is not None else 1
    if owner != k:
        return True
    return None
