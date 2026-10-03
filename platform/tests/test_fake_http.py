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
