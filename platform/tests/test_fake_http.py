"""Guards for the shared fake-server base (fake_http.DrainingHandler)."""

import ast
import http.client
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from fake_http import DrainingHandler

ROOT = Path(__file__).resolve().parents[2]


# Handlers that cannot import the test-only base, each with why it is safe. Every one must
# read the request body itself before replying (checked below), or it is not safe.
STANDALONE_HANDLERS = {
    # A local-dev service the Caddy deploy routes to, run as its own process from scripts/.
    "scripts/fake-inference-upstream.py": "standalone local-dev upstream; do_POST reads the body first",
}


def test_every_fake_server_derives_from_the_draining_base():
    """A handler on BaseHTTPRequestHandler directly can reply with the body unread -> RST."""
    offenders, standalone = [], {}
    for path in sorted([*(ROOT / "platform").rglob("*.py"), *(ROOT / "scripts").rglob("*.py")]):
        if path.name == "fake_http.py":
            continue
        rel = str(path.relative_to(ROOT))
        for node in ast.walk(ast.parse(path.read_text(), str(path))):
            if isinstance(node, ast.ClassDef) and any(
                "BaseHTTPRequestHandler" in ast.unparse(base) for base in node.bases
            ):
                if rel in STANDALONE_HANDLERS:
                    standalone[rel] = node
                else:
                    offenders.append(f"{rel}:{node.lineno} {node.name}")
    assert offenders == [], f"derive these from fake_http.DrainingHandler: {offenders}"
    # The allowlist only shrinks, and each entry must still read the body before replying.
    assert sorted(standalone) == sorted(STANDALONE_HANDLERS), sorted(standalone)
    for rel, node in standalone.items():
        writers = [
            m for m in node.body if isinstance(m, ast.FunctionDef) and m.name in ("do_POST", "do_PUT", "do_PATCH")
        ]
        for method in writers:
            assert "rfile.read" in ast.unparse(method), f"{rel}: {method.name} must read the body before replying"


class _Refuses(DrainingHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):  # noqa: N802 - the stdlib's name
        # Replies at once, never touching the body: the early-return path that used to reset.
        self.send_response(401)
        self.send_header("Content-Length", "0")
        self.end_headers()


def test_an_immediate_refusal_of_a_large_body_is_not_a_reset():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Refuses)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        for _ in range(5):
            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
            conn.request("POST", "/v1/auth/approle/login", body=b"x" * (4 * 1024 * 1024))
            assert conn.getresponse().status == 401
            conn.close()
    finally:
        server.shutdown()
        server.server_close()


# ── refused_port(): "nothing listens here" without a port another test can be handed ──

CLOSE_CALLS = {"stop", "close", "shutdown", "server_close"}
PORT_READS = {"getsockname", "server_port", "server_address", "url"}


def _own_nodes(func):
    """The function's own nodes: a nested function (a server thread) runs on its own schedule."""
    stack = list(ast.iter_child_nodes(func))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            stack.extend(ast.iter_child_nodes(node))


def _used_after_close(func):
    """Ports a function aims at after it stopped or closed the server or socket they came from.

    Counted after the close: a port value read from the object before it (getsockname,
    server_port, server_address, url), any of those reads on the object itself, and the object
    passed into a call (a helper that reads its URL). Reading what a stopped fake recorded is not
    aiming at it, and only objects the function made itself count (so os.close(fd) does not)."""
    made, closes, receivers, derived, port_reads, call_args, loads = set(), [], set(), {}, set(), set(), []
    for node in _own_nodes(func):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            made.add(node.targets[0].id)
            # A port, or a string built around one; a call's result (a process run) is not a port.
            for sub in ([] if isinstance(node.value, ast.Call) else ast.walk(node.value)):
                if isinstance(sub, ast.Attribute) and sub.attr in PORT_READS and isinstance(sub.value, ast.Name):
                    derived[node.targets[0].id] = (node.lineno, sub.value.id)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in CLOSE_CALLS and isinstance(node.func.value, ast.Name)):
            closes.append((node.lineno, node.func.value.id))
            receivers.add(id(node.func.value))
        if isinstance(node, ast.Attribute) and node.attr in PORT_READS and isinstance(node.value, ast.Name):
            port_reads.add(id(node.value))
        if isinstance(node, ast.Call):
            call_args.update(id(a) for a in [*node.args, *(k.value for k in node.keywords)] if isinstance(a, ast.Name))
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            loads.append(node)
    found = []
    for line, name in closes:
        if name not in made:
            continue
        for load in loads:
            if load.lineno <= line or id(load) in receivers:
                continue
            origin = derived.get(load.id)
            aimed = load.id == name and (id(load) in port_reads or id(load) in call_args)
            if aimed or (origin and origin[1] == name and origin[0] < line):
                found.append(f"{func.name}:{load.lineno} aims at {load.id} after {name} was closed at {line}")
    return found


def test_a_test_never_aims_at_a_port_it_has_just_freed():
    """A freed port can be handed to a parallel test's server; use fake_http.refused_port()."""
    offenders = []
    for path in sorted((ROOT / "platform" / "tests").glob("*.py")):
        if path.name in {"fake_http.py", "test_fake_http.py"}:
            continue
        for node in ast.walk(ast.parse(path.read_text(), str(path))):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                offenders += [f"{path.name}:{hit}" for hit in _used_after_close(node)]
    assert not offenders, "use fake_http.refused_port() instead of a freed port:\n" + "\n".join(offenders)


def test_the_guard_flags_each_freed_port_shape():
    shapes = {
        "server stopped, then passed on": "def t(g):\n    s = g()\n    s.stop()\n    run(s)\n",
        "port read, then server closed": (
            "def t(g):\n    s = g()\n    port = s.server_port\n    s.shutdown()\n    s.server_close()\n"
            "    run(port)\n"),
        "socket closed, port returned": (
            "def t():\n    c = socket.socket()\n    c.bind(('127.0.0.1', 0))\n    port = c.getsockname()[1]\n"
            "    c.close()\n    return port\n"),
    }
    for label, source in shapes.items():
        func = ast.parse(source).body[0]
        assert _used_after_close(func), label
    allowed = {
        "fixture teardown": "def t(made):\n    for s in made:\n        s.stop()\n",
        "reading what a stopped fake recorded": "def t(g):\n    f = g()\n    f.stop()\n    assert f.requests == []\n",
        "a module function, not a socket": "def t():\n    fd = os.open('x', 0)\n    os.close(fd)\n    os.stat('x')\n",
        "a result read after the server stopped": (
            "def t(g):\n    s = g()\n    proc = run(f'http://127.0.0.1:{s.server_port}')\n    s.stop()\n"
            "    assert proc.returncode\n"),
        "a server thread that closes its own listener": (
            "def t():\n    l = socket.socket()\n    port = l.getsockname()[1]\n"
            "    def serve():\n        l.close()\n    yield port\n"),
    }
    for label, source in allowed.items():
        assert not _used_after_close(ast.parse(source).body[0]), label


def test_refused_port_refuses_at_once_and_is_never_handed_out():
    import socket

    from fake_http import REFUSED_PORT, refused_port

    assert refused_port() == REFUSED_PORT
    handed = set()
    for _ in range(500):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            handed.add(s.getsockname()[1])
    assert REFUSED_PORT not in handed and min(handed) > 1023
