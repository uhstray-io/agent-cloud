"""Root pytest configuration: the shard hook for CI's test matrix (plan 21 phase 2, D3).

Inert unless PYTEST_SHARD=k/N or PYTEST_FAST=1 is set, so local runs and today's CI jobs are unchanged.
When PYTEST_SHARD is set, every test file is assigned to one of N shards by platform/tests/shards.py,
and the hook ignores each file not assigned to shard k at collection time.

The pre-push fast lane (plan 21 phase 4, D6) is PYTEST_FAST=1. It ignores each test file that
shards.fast_lane does not keep: a file named in the PYTEST_FAST_CHANGED file (repo-relative paths, one
per line) is kept, and so is one with no durations entry, one with a test the durations file does not
record, and one whose recorded tests all ran under FAST_CUTOFF with none skipped. A test file outside the
enumeration is never ignored. The lane and PYTEST_SHARD are one or the other. pytest does not consult this
hook for a file named on the command line, so `pytest some/test_file.py` runs that file whatever the lane
selects; the lane applies to a run that collects a directory, such as the testpaths.

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
    """Import platform/tests/shards.py (stdlib only). Called only when PYTEST_SHARD or PYTEST_FAST is set."""
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


FAST_ENV = "PYTEST_FAST"
FAST_CHANGED_ENV = "PYTEST_FAST_CHANGED"


def _fast_lane_on() -> bool:
    """Whether PYTEST_FAST selects the fast lane. Only "1" or unset/empty is accepted, and not with PYTEST_SHARD."""
    value = os.environ.get(FAST_ENV, "")
    if value not in ("", "1"):
        raise pytest.UsageError(f"{FAST_ENV}={value!r}: expected 1, or unset")
    if value == "1" and os.environ.get("PYTEST_SHARD", "").strip():
        raise pytest.UsageError(f"{FAST_ENV}=1 and PYTEST_SHARD are one or the other: unset one of them")
    return value == "1"


def _changed_paths(path_text: str) -> frozenset[str]:
    """Repo-relative paths from the PYTEST_FAST_CHANGED file, one per line. Unset, empty or missing means none."""
    if not path_text or not Path(path_text).is_file():
        return frozenset()
    lines = Path(path_text).read_text(encoding="utf-8").splitlines()
    return frozenset(line.strip() for line in lines if line.strip())


@functools.cache
def _fast_selection(root: Path, testpaths: tuple[str, ...], changed_file: str) -> tuple[frozenset[str], frozenset[str]]:
    """The enumerated test files and the ones the fast lane keeps, computed once per process."""
    lib = _shard_lib(root)
    files = lib.enumerate_files(root, list(testpaths))
    durations = lib.load_durations(root / DURATIONS)
    kept = lib.fast_lane(files, durations, root, set(_changed_paths(changed_file)))
    return frozenset(files), frozenset(kept)


def _fast_lane_ignore(collection_path: Path, config: pytest.Config) -> bool | None:
    if collection_path.is_dir() or not fnmatch.fnmatch(collection_path.name, "test_*.py"):
        return None
    root = config.rootpath.resolve()
    try:
        rel = collection_path.resolve().relative_to(root).as_posix()
    except ValueError:
        return None
    enumerated, kept = _fast_selection(root, tuple(config.getini("testpaths")), os.environ.get(FAST_CHANGED_ENV, ""))
    if rel in enumerated and rel not in kept:
        return True
    return None


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool | None:
    if _fast_lane_on():
        return _fast_lane_ignore(collection_path, config)
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
