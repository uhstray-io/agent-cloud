"""service_token.py against a stub Plane: it prints the workspace token and nothing else, and only on success.

The playbook runs plane_session.py and the script joined, with the automation account on stdin. These
tests run the real script against a stub that serves the sign-in redirect and Plane's get-or-create
route: 201 on the first mint, 200 on a repeat.
"""

import http.server
import json
import pathlib
import subprocess
import sys
import threading

import pytest
from fake_http import DrainingHandler

DEPLOY = pathlib.Path(__file__).resolve().parents[2] / "platform/services/plane/deployment"
# The playbook runs plane_session.py and the script joined, exactly as here.
SOURCE = (DEPLOY / "plane_session.py").read_text() + "\n" + (DEPLOY / "service_token.py").read_text()
PASSWORD = "pw-that-must-never-print"
TOKEN = "token-that-is-printed-once"
ME = "automation@example.test"
TOKEN_PATH = "/api/workspaces/ws/service-api-tokens/"


class StubPlane(DrainingHandler):
    state = {}

    def log_message(self, *args):
        pass

    def _send(self, status, body=None, location=None, cookie=None):
        self.state["paths"].append(self.path)
        self.send_response(status)
        if location:
            self.send_header("Location", location)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        data = json.dumps(body).encode() if body is not None else b""
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/auth/get-csrf-token/":
            return self._send(200, {"csrf_token": "tok"}, cookie="csrftoken=tok; Secure; HttpOnly")
        return self._send(404, {})

    def do_POST(self):
        s = self.state
        if self.path == "/auth/sign-in/":
            if s["sign_in"] == "ok":
                return self._send(302, location="/", cookie="session-id=s; Secure; HttpOnly")
            return self._send(302, location="/?error_code=INVALID&error_message=Invalid+credentials")
        if self.path == TOKEN_PATH:
            return self._send(s["token_status"], s["token_body"])
        return self._send(404, {})


@pytest.fixture
def plane():
    StubPlane.state = {"paths": [], "sign_in": "ok", "token_status": 201, "token_body": {"token": TOKEN}}
    server = http.server.HTTPServer(("127.0.0.1", 0), StubPlane)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield StubPlane.state, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def run(base):
    cfg = {"base": base, "host": "plane.test", "origin": "https://plane.test", "email": ME,
           "password": PASSWORD, "workspace_slug": "ws"}
    return subprocess.run([sys.executable, "-I", "-c", SOURCE], input=json.dumps(cfg) + "\n",
                          capture_output=True, text=True, timeout=60)


def test_a_first_mint_prints_the_token_once_with_created_true(plane):
    state, base = plane
    out = run(base)
    assert out.returncode == 0, out.stdout + out.stderr
    assert len(out.stdout.splitlines()) == 1
    assert json.loads(out.stdout) == {"token": TOKEN, "created": True}
    assert PASSWORD not in out.stdout + out.stderr


def test_a_repeat_returns_the_same_token_with_created_false(plane):
    state, base = plane
    state["token_status"] = 200
    out = run(base)
    assert out.returncode == 0, out.stdout + out.stderr
    assert json.loads(out.stdout) == {"token": TOKEN, "created": False}
    assert PASSWORD not in out.stdout + out.stderr


def test_a_refused_mint_prints_no_body_and_no_token(plane):
    state, base = plane
    state["token_status"] = 403
    state["token_body"] = {"detail": "leak-me-not"}
    out = run(base)
    assert out.returncode == 1, out.stdout + out.stderr
    assert json.loads(out.stdout) == {"error": "service token for ws refused (403)"}
    assert "leak-me-not" not in out.stdout + out.stderr
    assert PASSWORD not in out.stdout + out.stderr


def test_a_success_status_without_a_token_is_refused(plane):
    state, base = plane
    state["token_body"] = {"detail": "no token here"}
    out = run(base)
    assert out.returncode == 1, out.stdout + out.stderr
    assert json.loads(out.stdout) == {"error": "service token for ws refused (201)"}


def test_a_refused_sign_in_stops_before_the_token_request(plane):
    state, base = plane
    state["sign_in"] = "refused"
    out = run(base)
    assert out.returncode == 1, out.stdout + out.stderr
    assert json.loads(out.stdout) == {"error": "/auth/sign-in/: refused (302, Invalid credentials)"}
    assert TOKEN_PATH not in state["paths"]
    assert PASSWORD not in out.stdout + out.stderr


def test_the_joined_source_defines_every_name_it_uses():
    # The script is a fragment that only runs after plane_session.py, so ruff's undefined-name
    # check is off for the file (noqa F821); this checks the joined source the playbook runs.
    import builtins
    import symtable

    script = "service_token.py"
    source = (DEPLOY / "plane_session.py").read_text() + "\n" + (DEPLOY / script).read_text()
    top = symtable.symtable(source, script, "exec")
    defined = {s.get_name() for s in top.get_symbols() if s.is_assigned() or s.is_imported()}
    defined |= {c.get_name() for c in top.get_children()} | set(dir(builtins))

    def used(table):
        for s in table.get_symbols():
            if s.is_referenced() and (s.is_global() or table is top):
                yield s.get_name()
        for child in table.get_children():
            yield from used(child)

    assert sorted(set(used(top)) - defined) == []
