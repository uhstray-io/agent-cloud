"""Find the test code that starts a child process itself (plan 21 phase 3).

A spawn is a `subprocess` call (run, Popen, call, check_call, check_output), however subprocess was
imported, read with the AST and never imported. Its program is the first element of the command:

- "ansible-playbook": the command names ansible-playbook. That holds for a string or f-string that
  starts with it or ends with "/ansible-playbook", for `shutil.which("ansible-playbook")`, and for a
  name or list that is assigned one of those in the same function or at module level.
- "dynamic": the program cannot be named statically (a function parameter, an attribute, a call).
- anything else is the program's literal name ("git", "openssl") and is not tracked here.

The shared runner, harness_sandbox.run and run_playbook, is the one sanctioned spawner.
"""

import ast
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SPAWNERS = {"run", "Popen", "call", "check_call", "check_output"}
PLAYBOOK = "ansible-playbook"
DYNAMIC = "dynamic"
SHARED_RUNNER = ("platform/tests/harness_sandbox.py", {"run", "run_playbook"})


def _testpaths() -> list[str]:
    return tomllib.loads((REPO / "pyproject.toml").read_text())["tool"]["pytest"]["ini_options"]["testpaths"]


def _is_playbook_text(text: str) -> bool:
    first = text.strip().split(" ", 1)[0] if text.strip() else ""
    return first == PLAYBOOK or first.endswith("/" + PLAYBOOK)


def _assignments(scope) -> dict[str, list]:
    """Name -> every value assigned to it directly in `scope` (a module or a function body)."""
    found: dict[str, list] = {}
    body = scope.body
    stack = list(body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue  # nested scopes keep their own names
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    found.setdefault(target.id, []).append(node.value)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and isinstance(node.target, ast.Name) and node.value:
            found.setdefault(node.target.id, []).append(node.value)
        stack.extend(ast.iter_child_nodes(node))
    return found


def _program(node, chain: list, depth: int = 0):
    """PLAYBOOK, DYNAMIC, or the literal program name of a command expression. `chain` holds the
    assignments of the enclosing scopes, innermost first and the module last."""
    if depth > 8:
        return DYNAMIC
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return PLAYBOOK if _is_playbook_text(node.value) else node.value.strip().split(" ", 1)[0]
    if isinstance(node, ast.JoinedStr):
        head = node.values[0] if node.values else None
        if isinstance(head, ast.Constant) and isinstance(head.value, str) and head.value.strip():
            return PLAYBOOK if _is_playbook_text(head.value) else head.value.strip().split(" ", 1)[0]
        return _program(head.value, chain, depth + 1) if isinstance(head, ast.FormattedValue) else DYNAMIC
    if isinstance(node, (ast.List, ast.Tuple)):
        if not node.elts:
            return DYNAMIC
        first = node.elts[0]
        return _program(first.value if isinstance(first, ast.Starred) else first, chain, depth + 1)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _program(node.left, chain, depth + 1)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):  # pathlib: bindir / "ansible-playbook"
        right = node.right
        if isinstance(right, ast.Constant) and isinstance(right.value, str) and right.value == PLAYBOOK:
            return PLAYBOOK
        return DYNAMIC
    if isinstance(node, ast.Call) and getattr(node.func, "id", None) in ("str", "list") and node.args:
        return _program(node.args[0], chain, depth + 1)
    if isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
        # shutil.which("ansible-playbook"), or a repository helper such as ansible_bin("ansible-playbook")
        return PLAYBOOK if node.args[0].value == PLAYBOOK else DYNAMIC
    if isinstance(node, ast.Name):
        for index, scope in enumerate(chain):
            if node.id in scope:
                kinds = {_program(value, chain[index:], depth + 1) for value in scope[node.id]}
                if PLAYBOOK in kinds:
                    return PLAYBOOK
                return kinds.pop() if len(kinds) == 1 else DYNAMIC
        return DYNAMIC
    return DYNAMIC


def _own_nodes(function):
    """Every node of a module's or function's body, stopping at nested functions (which are
    yielded, not entered); a class body is entered, so its methods are found."""
    stack = list(function.body)
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            stack.extend(ast.iter_child_nodes(node))
        elif isinstance(node, ast.ClassDef):
            stack.extend(node.body)


def _subprocess_names(tree) -> tuple[set[str], set[str]]:
    """Names that mean the subprocess module, and names imported from it that spawn."""
    modules, functions = {"subprocess"}, set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess":
                    modules.add(alias.asname or "subprocess")
        elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            for alias in node.names:
                if alias.name in SPAWNERS:
                    functions.add(alias.asname or alias.name)
    return modules, functions


def _bounded(call: ast.Call) -> bool:
    for keyword in call.keywords:
        if keyword.arg == "timeout":
            return not (isinstance(keyword.value, ast.Constant) and keyword.value.value is None)
    return False  # Popen takes no timeout; **kwargs cannot be read, so it counts as unbounded


def _command(call: ast.Call):
    if call.args:
        return call.args[0]
    return next((k.value for k in call.keywords if k.arg == "args"), None)


def spawns(tree, skip_functions=()):
    """(program kind, bounded, line) for each subprocess call. Decorators are not code that runs
    the spawn, so only function bodies and module-level statements are read."""
    modules, functions = _subprocess_names(tree)
    scopes = []

    def collect(scope, chain):
        # Each function is collected once, from its own parent, with its enclosing scopes' names.
        scopes.append((scope, chain))
        for node in _own_nodes(scope):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name not in skip_functions:
                collect(node, [_assignments(node), *chain])

    collect(tree, [_assignments(tree)])
    for scope, chain in scopes:
        stack = list(scope.body)
        while stack:
            node = stack.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue  # each function is its own scope above
            stack.extend(ast.iter_child_nodes(node))
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            direct = isinstance(func, ast.Name) and func.id in functions
            via_module = (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
                          and func.value.id in modules and func.attr in SPAWNERS)
            if direct or via_module:
                command = _command(node)
                kind = _program(command, chain) if command is not None else DYNAMIC
                yield kind, _bounded(node), node.lineno


def scan() -> tuple[set[str], set[str], set[str]]:
    """Repo-relative paths of the files that start ansible-playbook directly; of those with an
    ansible-playbook spawn that has no timeout; and of those with an unbounded spawn whose program
    cannot be named statically (it may be ansible-playbook, and it can hang a worker either way)."""
    direct, unbounded, unbounded_dynamic = set(), set(), set()
    for testpath in _testpaths():
        for path in sorted((REPO / testpath).rglob("*.py")):
            rel = path.relative_to(REPO).as_posix()
            if "__pycache__" in path.parts:
                continue
            skip = SHARED_RUNNER[1] if rel == SHARED_RUNNER[0] else ()
            for kind, bounded, _ in spawns(ast.parse(path.read_text(encoding="utf-8")), skip):
                if kind == PLAYBOOK:
                    direct.add(rel)
                    if not bounded:
                        unbounded.add(rel)
                elif kind == DYNAMIC and not bounded:
                    unbounded_dynamic.add(rel)
    return direct, unbounded, unbounded_dynamic
