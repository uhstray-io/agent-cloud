"""Every TLS context built in repo Python pins a minimum protocol version.

CodeQL's py/insecure-protocol flags an ssl.SSLContext(...) or ssl.create_default_context(...)
that never restricts the protocol floor, because the former allows TLSv1/1.1 and neither is
guaranteed to on every build. Its query treats an attribute write of `minimum_version` on the
context as the restriction. This ratchet applies the same rule at test time, so a new offender
fails here instead of as a code-scanning alert on a promotion PR: every such call must be
bound to a name, and the same function (or module body) must assign
`<name>.minimum_version = ssl.TLSVersion.TLSv1_2` (or TLSv1_3) to that name.

A context that genuinely cannot be pinned goes in ALLOWLIST with the reason; none exist.
"""

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCAN_ROOTS = ("platform", "scripts")
CONTEXT_FACTORIES = {"SSLContext", "create_default_context"}
PINNED_VERSIONS = {"TLSv1_2", "TLSv1_3"}
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)

# "<repo-relative path>:<function name>" -> reason it cannot pin a floor.
ALLOWLIST: dict[str, str] = {}


def _python_files():
    for root in SCAN_ROOTS:
        yield from sorted((REPO / root).rglob("*.py"))


def _factory_names(tree):
    """Local names bound to the ssl module, and to its context factories via `from ssl import`."""
    ssl_aliases, direct = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            ssl_aliases |= {a.asname or a.name for a in node.names if a.name == "ssl"}
        elif isinstance(node, ast.ImportFrom) and node.module == "ssl":
            direct |= {a.asname or a.name for a in node.names if a.name in CONTEXT_FACTORIES}
    return ssl_aliases, direct


def _is_factory_call(node, ssl_aliases, direct):
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    if isinstance(f, ast.Attribute):
        return f.attr in CONTEXT_FACTORIES and isinstance(f.value, ast.Name) and f.value.id in ssl_aliases
    return isinstance(f, ast.Name) and f.id in direct


def _is_pinned_version(value):
    return (
        isinstance(value, ast.Attribute)
        and value.attr in PINNED_VERSIONS
        and isinstance(value.value, ast.Attribute)
        and value.value.attr == "TLSVersion"
    )


def _scope_nodes(scope):
    """Nodes of one scope; a nested function is a scope of its own, so it is not entered."""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, FUNCTIONS):
            stack.extend(ast.iter_child_nodes(node))


def find_offenders(source, label):
    """Return 'label:line (scope): reason' for each unpinned context in `source`."""
    tree = ast.parse(source)
    ssl_aliases, direct = _factory_names(tree)
    if not (ssl_aliases or direct):
        return []
    scopes = [(tree, "<module>")] + [(n, n.name) for n in ast.walk(tree) if isinstance(n, FUNCTIONS)]
    offenders = []
    for scope, name in scopes:
        nodes = list(_scope_nodes(scope))
        pinned = {
            ast.unparse(t.value)
            for n in nodes
            if isinstance(n, ast.Assign) and _is_pinned_version(n.value)
            for t in n.targets
            if isinstance(t, ast.Attribute) and t.attr == "minimum_version"
        }
        bound = {
            id(n.value): ast.unparse(t)
            for n in nodes
            if isinstance(n, ast.Assign) and _is_factory_call(n.value, ssl_aliases, direct)
            for t in n.targets
        }
        for n in nodes:
            if not _is_factory_call(n, ssl_aliases, direct):
                continue
            if f"{label}:{name}" in ALLOWLIST:
                continue
            target = bound.get(id(n))
            if target is None:
                offenders.append(f"{label}:{n.lineno} ({name}): context not bound to a name")
            elif target not in pinned:
                offenders.append(f"{label}:{n.lineno} ({name}): {target}.minimum_version never set to TLSv1_2/TLSv1_3")
    return offenders


def test_every_tls_context_pins_a_minimum_version():
    offenders = []
    for path in _python_files():
        offenders += find_offenders(path.read_text(), str(path.relative_to(REPO)))
    assert not offenders, "\n".join(offenders)


@pytest.mark.parametrize(
    "source",
    [
        "import ssl\ndef f():\n    c = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)\n",
        "import ssl\ndef f():\n    c = ssl.create_default_context()\n",
        "from ssl import create_default_context\ndef f():\n    c = create_default_context()\n",
        "import ssl\ndef f():\n    return ssl.create_default_context()\n",
        # pinned in a different function does not count
        "import ssl\ndef f():\n    c = ssl.SSLContext()\ndef g():\n    c.minimum_version = ssl.TLSVersion.TLSv1_2\n",
        # pinned on the wrong name
        "import ssl\ndef f():\n    a = ssl.SSLContext()\n    b = 1\n    b.minimum_version = ssl.TLSVersion.TLSv1_2\n",
        # a floor below 1.2 is not a pin
        "import ssl\ndef f():\n    c = ssl.SSLContext()\n    c.minimum_version = ssl.TLSVersion.TLSv1\n",
    ],
)
def test_ratchet_flags_unpinned_contexts(source):
    assert find_offenders(source, "x.py")


@pytest.mark.parametrize(
    "source",
    [
        "import ssl\ndef f():\n    c = ssl.SSLContext()\n    c.minimum_version = ssl.TLSVersion.TLSv1_2\n",
        "import ssl\ndef f():\n    c = ssl.create_default_context()\n    c.minimum_version = ssl.TLSVersion.TLSv1_3\n",
        "import ssl\ndef f(s):\n    s.c = ssl.SSLContext()\n    s.c.minimum_version = ssl.TLSVersion.TLSv1_2\n",
        "import ssl\nc = ssl.SSLContext()\nc.minimum_version = ssl.TLSVersion.TLSv1_2\n",
    ],
)
def test_ratchet_accepts_pinned_contexts(source):
    assert not find_offenders(source, "x.py")
