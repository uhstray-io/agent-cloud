"""Deterministic file-to-shard assignment for the pytest shard hook (plan 21 phase 2, D3).

Pure stdlib, and it never imports pytest: the root conftest.py loads it only when PYTEST_SHARD is
set, and every xdist worker computes the same assignment from the same inputs.

A file's weight is its recorded time in the durations file (one entry per file, built by
scripts/test-durations.py). A file with no recorded entry is weighted by its count of ``def test_``
lines times the median, over recorded files, of the mean time per test (1.0 when nothing is
recorded), read as text so the file is never imported.
"""

from __future__ import annotations

import json
import re
import statistics
from pathlib import Path

DEF_TEST = re.compile(r"^\s*(async\s+)?def test_")
SHARD_SPEC = re.compile(r"([0-9]+)/([0-9]+)")


def enumerate_files(root: Path, testpaths: list[str]) -> list[str]:
    """Sorted repo-relative posix paths of ``test_*.py`` files under each testpath, ``__pycache__`` excluded."""
    found: set[str] = set()
    for testpath in testpaths:
        for path in (root / testpath).rglob("test_*.py"):
            rel = path.relative_to(root)
            if "__pycache__" in rel.parts or not path.is_file():
                continue
            found.add(rel.as_posix())
    return sorted(found)


def load_durations(path: Path) -> dict:
    """The durations file: ``{path: {"time", "max", "nodes", "skipped", "tests"}}``. A missing file is an empty map."""
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def count_def_tests(path: Path) -> int:
    """Count of ``def test_`` / ``async def test_`` lines, found by text so the module is never imported."""
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if DEF_TEST.match(line))


def file_weights(files: list[str], durations: dict, root: Path) -> dict[str, float]:
    """Recorded time per file; a file with no recorded entry gets its def-test count times the median
    per-test mean of the recorded files."""
    means = [float(entry["time"]) / entry["nodes"] for entry in durations.values() if entry["nodes"]]
    median = statistics.median(means) if means else 1.0
    return {rel: float(durations[rel]["time"]) if rel in durations else count_def_tests(root / rel) * median
            for rel in files}


def assign(files: list[str], weights: dict[str, float], n: int) -> dict[str, int]:
    """Shard number (1..n) per file: heaviest first, each to the least-loaded shard, ties by lowest shard and path."""
    if n < 1:
        raise ValueError(f"shard count must be at least 1, got {n}")
    loads = [0.0] * n
    assignment: dict[str, int] = {}
    for rel in sorted(files, key=lambda f: (-weights[f], f)):
        shard = min(range(n), key=lambda i: (loads[i], i))
        assignment[rel] = shard + 1
        loads[shard] += weights[rel]
    return assignment


def parse_shard(value: str) -> tuple[int, int]:
    """Parse ``k/N`` with 1 <= k <= N. Anything else is a ValueError."""
    match = SHARD_SPEC.fullmatch(value)
    if match is None:
        raise ValueError(f"expected k/N, got {value!r}")
    k, n = int(match.group(1)), int(match.group(2))
    if n < 1 or not 1 <= k <= n:
        raise ValueError(f"shard {k}/{n} is out of range: need 1 <= k <= N")
    return k, n
