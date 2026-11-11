"""Find the test code that starts ansible-playbook itself (plan 21 phase 3).

A spawn here is a `subprocess` call (run, Popen, call, check_call, check_output) inside a function
whose code names "ansible-playbook". Read with the AST, never imported. The shared runner,
harness_sandbox.run_playbook, is the one sanctioned spawner.
"""

import ast
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SPAWNERS = {"run", "Popen", "call", "check_call", "check_output"}
SHARED_RUNNER = "platform/tests/harness_sandbox.py"


def _testpaths() -> list[str]:
    return tomllib.loads((REPO / "pyproject.toml").read_text())["tool"]["pytest"]["ini_options"]["testpaths"]


def _spawns(tree):
    """(direct spawn call, has a timeout) for each subprocess call in an ansible-playbook function."""
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not any(isinstance(n, ast.Constant) and n.value == "ansible-playbook" for n in ast.walk(fn)):
            continue
        for call in ast.walk(fn):
            func = call.func if isinstance(call, ast.Call) else None
            if (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
                    and func.value.id == "subprocess" and func.attr in SPAWNERS):
                yield call, any(k.arg == "timeout" for k in call.keywords)


def scan() -> tuple[set[str], set[str]]:
    """Repo-relative paths of the files that spawn ansible-playbook directly, and of those among
    them with at least one such spawn that has no timeout."""
    direct, unbounded = set(), set()
    for testpath in _testpaths():
        for path in sorted((REPO / testpath).rglob("*.py")):
            rel = path.relative_to(REPO).as_posix()
            if "__pycache__" in path.parts or rel == SHARED_RUNNER:
                continue
            for _, bounded in _spawns(ast.parse(path.read_text(encoding="utf-8"))):
                direct.add(rel)
                if not bounded:
                    unbounded.add(rel)
    return direct, unbounded
