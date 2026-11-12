"""Deterministic file-to-shard assignment for the pytest shard hook (plan 21 phase 2, D3).

Pure stdlib, and it never imports pytest: the root conftest.py loads it only when PYTEST_SHARD or
PYTEST_FAST is set, and every xdist worker computes the same assignment from the same inputs.

A file's weight is its recorded time in the durations file (one entry per file, built by
scripts/test-durations.py). A file with no recorded entry is weighted by its count of ``def test_``
lines times the median, over recorded files, of the mean time per test (1.0 when nothing is
recorded), read as text so the file is never imported.

fast_lane (plan 21 phase 4, D6) selects the pre-push subset of the same files. Its test names come from
collected_test_names, which parses the source with the stdlib ast module and never imports it, so the
names it finds are the ones pytest collects rather than every ``def test_`` line.
"""

from __future__ import annotations

import ast
import json
import re
import statistics
from pathlib import Path

DEF_TEST = re.compile(r"^\s*(async\s+)?def test_")
SHARD_SPEC = re.compile(r"([0-9]+)/([0-9]+)")
FAST_CUTOFF = 0.25  # seconds: a file whose recorded tests all ran under this, with none skipped, is in the fast lane


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


def _scope(nodes):
    """The function and class definitions a scope binds: the statements in ``nodes``, descending into compound
    statements (if, try, with, match) but never into a function or class body."""
    for node in nodes:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield node
        else:
            children = [child for child in ast.iter_child_nodes(node)
                        if isinstance(child, (ast.stmt, ast.excepthandler, ast.match_case))]
            yield from _scope(children)


def _collect(nodes, names: set[str]) -> None:
    for node in _scope(nodes):
        if isinstance(node, ast.ClassDef):
            # pytest collects a class named Test*, and any unittest case: a class with a base is a candidate.
            if node.name.startswith("Test") or node.bases:
                _collect(node.body, names)
        elif node.name.startswith("test"):
            names.add(node.name)


def collected_test_names(source: str) -> set[str]:
    """Names pytest would collect from one test file's source, parsed and never imported.

    A module-level function, or a method of a collected class, whose name starts with ``test`` (pytest's
    default python_functions prefix). A collected class is one named ``Test*`` or any class with a base.
    The base case over-counts a helper class with a base that pytest would not collect, which only keeps
    its file in the fast lane. A function nested in a function, and a class with no base that is not
    ``Test*``, is not counted, because pytest never collects it.
    """
    names: set[str] = set()
    _collect(ast.parse(source).body, names)
    return names


def _is_fast(entry: dict) -> bool:
    return float(entry["max"]) < FAST_CUTOFF and not entry["skipped"]


def _has_missing_test(path: Path, entry: dict) -> bool:
    recorded = set(entry.get("tests", []))
    try:
        names = collected_test_names(path.read_text(encoding="utf-8"))
    except (SyntaxError, ValueError):
        return True
    return bool(names - recorded)


def fast_lane(files: list[str], durations: dict, root: Path, changed: set[str]) -> set[str]:
    """The subset of ``files`` the pre-push fast lane runs (plan 21 phase 4, D6).

    A file is kept when any of these holds, tested in this order so a file is parsed only when the cheaper
    rules drop it: it is in ``changed``; it has no durations entry; its recorded tests all ran under
    FAST_CUTOFF with none skipped in CI; or a test it defines is missing from its durations entry. A file
    that does not parse is kept.
    """
    kept: set[str] = set()
    for rel in files:
        entry = durations.get(rel)
        if rel in changed or entry is None or _is_fast(entry) or _has_missing_test(root / rel, entry):
            kept.add(rel)
    return kept
