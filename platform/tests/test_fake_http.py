"""Guards for the shared fake-server base (fake_http.DrainingHandler)."""

import ast
import http.client
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from fake_http import DrainingHandler

ROOT = Path(__file__).resolve().parents[2]


def test_every_fake_server_derives_from_the_draining_base():
    """A handler on BaseHTTPRequestHandler directly can reply with the body unread -> RST."""
    offenders = []
    for path in sorted((ROOT / "platform").rglob("*.py")):
        if path.name == "fake_http.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(), str(path))):
            if isinstance(node, ast.ClassDef) and any(
                "BaseHTTPRequestHandler" in ast.unparse(base) for base in node.bases
            ):
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno} {node.name}")
    assert offenders == [], f"derive these from fake_http.DrainingHandler: {offenders}"


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
