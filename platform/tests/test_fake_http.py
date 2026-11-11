"""Guards for the shared fake-server base (fake_http.DrainingHandler)."""

import ast
import http.client
import socket
import ssl
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fake_http import DrainingHandler, serve
from test_pve_pinned_get import _generate_leaf

ROOT = Path(__file__).resolve().parents[2]


# Handlers that cannot import the test-only base, each with why it is safe. Every one must
# read the request body itself before replying (checked below), or it is not safe.
STANDALONE_HANDLERS = {
    # A local-dev service the Caddy deploy routes to, run as its own process from scripts/.
    "scripts/fake-inference-upstream.py": "standalone local-dev upstream; do_POST reads the body first",
}

# Production servers, not fakes: they cannot import a test helper. Each must read the request body itself
# (checked below), and a refusal may drain a body only up to a cap, never buffer it.
PRODUCTION_HANDLERS = {
    "platform/services/launch-gate/deployment/app/launch_gate.py": (
        "the launch gate; reads a POST body in _read_body, drains an over-MAX_BODY body up to DRAIN_CAP before its 413"
    ),
}

REQUEST_BODY_READS = frozenset({"read", "read1", "readinto"})


def _reads_request_body(node):
    """True when the node calls a read method on an ``rfile`` attribute. The call is what counts: a docstring or
    comment that names ``rfile.read`` does not."""
    return any(
        isinstance(sub, ast.Call)
        and isinstance(sub.func, ast.Attribute)
        and sub.func.attr in REQUEST_BODY_READS
        and isinstance(sub.func.value, ast.Attribute)
        and sub.func.value.attr == "rfile"
        for sub in ast.walk(node)
    )


def test_every_fake_server_derives_from_the_draining_base():
    """A handler on BaseHTTPRequestHandler directly can reply with the body unread -> RST."""
    offenders, standalone, production = [], {}, {}
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
                elif rel in PRODUCTION_HANDLERS:
                    production[rel] = node
                else:
                    offenders.append(f"{rel}:{node.lineno} {node.name}")
    assert offenders == [], f"derive these from fake_http.DrainingHandler: {offenders}"
    # Each allowlist entry must be found, and each handler found must be listed: the two sets are equal.
    assert sorted(standalone) == sorted(STANDALONE_HANDLERS), sorted(standalone)
    assert sorted(production) == sorted(PRODUCTION_HANDLERS), sorted(production)
    for rel, node in production.items():
        assert _reads_request_body(node), f"{rel}: {node.name} never reads a request body"
    for rel, node in standalone.items():
        writers = [
            m for m in node.body if isinstance(m, ast.FunctionDef) and m.name in ("do_POST", "do_PUT", "do_PATCH")
        ]
        for method in writers:
            assert _reads_request_body(method), f"{rel}: {method.name} must read the body before replying"


def test_the_body_read_check_needs_a_real_call_not_a_mention():
    # The old check was a substring test on the unparsed class, so this docstring-only class passed it.
    mention = ast.parse(
        'class H(BaseHTTPRequestHandler):\n    """Reads self.rfile.read(n) in the docs only."""\n'
        "    def do_POST(self):\n        pass\n"
    ).body[0]
    assert "rfile.read" in ast.unparse(mention)
    assert not _reads_request_body(mention)
    for call in ("self.rfile.read(5)", "self.rfile.read1(5)", "self.rfile.readinto(buf)"):
        real = ast.parse(f"class H(BaseHTTPRequestHandler):\n    def do_POST(self):\n        {call}\n").body[0]
        assert _reads_request_body(real), call
    other = ast.parse("class H(BaseHTTPRequestHandler):\n    def do_POST(self):\n        self.wfile.read(5)\n").body[0]
    assert not _reads_request_body(other)


class _Refuses(DrainingHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):  # noqa: N802 - the stdlib's name
        # Replies at once, never touching the body: the early-return path that used to reset.
        self.send_response(401)
        self.send_header("Content-Length", "0")
        self.end_headers()


def test_an_immediate_refusal_of_a_large_body_is_not_a_reset():
    with serve(_Refuses) as url:
        for _ in range(5):
            conn = http.client.HTTPConnection("127.0.0.1", urlsplit(url).port, timeout=10)
            conn.request("POST", "/v1/auth/approle/login", body=b"x" * (4 * 1024 * 1024))
            assert conn.getresponse().status == 401
            conn.close()


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
            for sub in [] if isinstance(node.value, ast.Call) else ast.walk(node.value):
                if isinstance(sub, ast.Attribute) and sub.attr in PORT_READS and isinstance(sub.value, ast.Name):
                    # Every assignment counts: a name set in two branches has two origins.
                    derived.setdefault(node.targets[0].id, []).append((node.lineno, sub.value.id))
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in CLOSE_CALLS
            and isinstance(node.func.value, ast.Name)
        ):
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
            from_closed = any(src == name and at < line for at, src in derived.get(load.id, []))
            aimed = load.id == name and (id(load) in port_reads or id(load) in call_args)
            if aimed or from_closed:
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
            "def t(g):\n    s = g()\n    port = s.server_port\n    s.shutdown()\n    s.server_close()\n    run(port)\n"
        ),
        "one name set in two branches, one from a closed socket": (
            "def t(open_):\n    l = socket.socket()\n    if open_:\n        port = l.getsockname()[1]\n"
            "    else:\n        c = socket.socket()\n        port = c.getsockname()[1]\n        c.close()\n"
            "    run(port)\n"
        ),
        "socket closed, port returned": (
            "def t():\n    c = socket.socket()\n    c.bind(('127.0.0.1', 0))\n    port = c.getsockname()[1]\n"
            "    c.close()\n    return port\n"
        ),
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
            "    assert proc.returncode\n"
        ),
        "a server thread that closes its own listener": (
            "def t():\n    l = socket.socket()\n    port = l.getsockname()[1]\n"
            "    def serve():\n        l.close()\n    yield port\n"
        ),
    }
    for label, source in allowed.items():
        assert not _used_after_close(ast.parse(source).body[0]), label


def test_refused_port_refuses_at_once_and_is_never_handed_out():
    import errno
    import socket

    from fake_http import REFUSED_PORT, refused_port

    assert refused_port() == REFUSED_PORT
    handed = set()
    for _ in range(500):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            handed.add(s.getsockname()[1])
    assert REFUSED_PORT not in handed and min(handed) > 1023
    # Tests name `localhost` as well as 127.0.0.1: every address it resolves to must refuse.
    for family, _type, _proto, _name, address in socket.getaddrinfo("localhost", REFUSED_PORT, type=socket.SOCK_STREAM):
        with socket.socket(family) as probe:
            probe.settimeout(2)
            try:
                probe.connect(address)
            except ConnectionRefusedError:
                continue
            except OSError as error:  # no usable IPv6 loopback: nothing can listen there either
                if family == socket.AF_INET6 and error.errno in (
                    errno.EADDRNOTAVAIL,
                    errno.ENETUNREACH,
                    errno.EAFNOSUPPORT,
                ):
                    continue
                raise
            raise AssertionError(f"localhost address {address} accepted a connection on port {REFUSED_PORT}")


# ---- serve(): the one way a test starts a fixture server ------------------------------------------

# Files that still build their own HTTP server (a call to, or a subclass of, HTTPServer or
# ThreadingHTTPServer) instead of fake_http.serve. Exact, and meant only to shrink: a file that
# migrates comes off; adding one is the change review refuses (plan 21 phase 3).
OWN_SERVERS = frozenset({
    "platform/tests/test_agw_client_tls_probe.py",
    "platform/tests/test_agw_isolation_probe.py",
    "platform/tests/test_agw_mcp_verify.py",
    "platform/tests/test_agw_openrouter.py",
    "platform/tests/test_agw_skynet_route.py",
    "platform/tests/test_agw_stream_usage.py",
    "platform/tests/test_instrument_host_o11y.py",
    "platform/tests/test_probe_launch_gate.py",
    "platform/tests/test_probe_skynet_launch.py",
    "platform/tests/test_renew_internal_certs.py",
    "platform/tests/test_verify_o11y_dashboard_data.py",
})


def _builds_own_server(tree) -> bool:
    # Any use of the class counts, not only a direct call or a class base: an alias
    # (`server_type = ThreadingHTTPServer`) or a type() subclass builds a server too.
    # Import statements are alias nodes, not Name/Attribute, so an unused import is not flagged.
    names = {"HTTPServer", "ThreadingHTTPServer"}
    return any((isinstance(n, ast.Name) and n.id in names) or (isinstance(n, ast.Attribute) and n.attr in names)
               for n in ast.walk(tree))


def test_no_new_test_builds_its_own_http_server():
    import tomllib
    testpaths = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["pytest"]["ini_options"]["testpaths"]
    found = set()
    for t in testpaths:
        for p in (ROOT / t).rglob("*.py"):
            if "__pycache__" in p.parts or p.name == "fake_http.py":
                continue
            text = p.read_text(encoding="utf-8")
            # Exact, not a heuristic: any use the parse would find spells one of the two names.
            if "HTTPServer" in text and _builds_own_server(ast.parse(text)):
                found.add(p.relative_to(ROOT).as_posix())
    assert not found - OWN_SERVERS, f"start it with fake_http.serve(handler): {sorted(found - OWN_SERVERS)}"
    assert not OWN_SERVERS - found, f"migrated: take these off OWN_SERVERS: {sorted(OWN_SERVERS - found)}"


class _Echo(DrainingHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _post(url, body, *, context=None):
    parts = urlsplit(url)
    if context is None:
        connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=5)
    else:
        connection = http.client.HTTPSConnection(parts.hostname, parts.port, timeout=5, context=context)
    try:
        connection.request("POST", "/", body=body)
        return connection.getresponse().read()
    finally:
        connection.close()


def test_serve_answers_on_an_ephemeral_loopback_port_and_stops_after():
    with serve(_Echo) as url:
        assert url.startswith("http://127.0.0.1:")
        assert _post(url, b"ping") == b"ping"
    with pytest.raises(OSError):
        http.client.HTTPConnection("127.0.0.1", urlsplit(url).port, timeout=2).request("GET", "/")


def test_serve_on_a_wildcard_host_is_reached_on_loopback():
    with serve(_Echo, host="0.0.0.0") as url:
        assert url.startswith("http://127.0.0.1:")
        assert _post(url, b"wildcard") == b"wildcard"


def test_serve_on_ipv6_loopback():
    try:
        with socket.socket(socket.AF_INET6) as probe:
            probe.bind(("::1", 0))
    except OSError:
        pytest.skip("this host cannot bind IPv6 loopback")
    with serve(_Echo, host="::1") as url:
        assert url.startswith("http://[::1]:")
        assert _post(url, b"over-ipv6") == b"over-ipv6"


def _tls_contexts(tmp_path):
    leaf = _generate_leaf(tmp_path)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.minimum_version = ssl.TLSVersion.TLSv1_2
    server_context.load_cert_chain(leaf.cert_path, leaf.key_path)
    client_context = ssl.create_default_context(cafile=str(leaf.cert_path))
    client_context.minimum_version = ssl.TLSVersion.TLSv1_2
    return server_context, client_context


def test_serve_with_an_ssl_context_answers_over_tls(tmp_path):
    server_context, client_context = _tls_contexts(tmp_path)
    with serve(_Echo, ssl_context=server_context) as url:
        assert url.startswith("https://127.0.0.1:")
        assert _post(url, b"over-tls", context=client_context) == b"over-tls"


def test_a_tls_client_that_never_handshakes_holds_up_only_itself(tmp_path):
    # A handshake on the accept thread (the listening socket wrapped) would block here until the
    # silent client gives up: the real request then times out, and the test fails instead of hanging.
    server_context, client_context = _tls_contexts(tmp_path)
    with serve(_Echo, ssl_context=server_context) as url:
        silent = socket.create_connection(("127.0.0.1", urlsplit(url).port), timeout=5)
        try:
            assert _post(url, b"past-the-stall", context=client_context) == b"past-the-stall"
        finally:
            silent.close()
