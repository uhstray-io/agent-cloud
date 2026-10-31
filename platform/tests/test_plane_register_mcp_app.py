"""register_mcp_app.py against a stub Plane: create, patch, regenerate, and what it never prints.

The playbook runs plane_session.py and the script joined, with the automation account and the app
spec on stdin. These tests run the real script against a stub that serves Plane v3.3.3's shapes:
a plain JSON list of applications (each carrying its stored, hashed client_secret), 201 with the
plaintext client_secret on create, and PATCH on the regenerate-secret route by application id.
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
SOURCE = (DEPLOY / "plane_session.py").read_text() + "\n" + (DEPLOY / "register_mcp_app.py").read_text()
PASSWORD = "pw-that-must-never-print"
SECRET = "plaintext-secret-printed-once"
STORED = "stored-hash-that-is-never-usable"
ME = "automation@example.test"
WS = "ws"
APP = "plane-mcp"
BASE_PATH = f"/api/workspaces/{WS}/applications/"
REDIRECT = "https://mcp.test/callback"
APP_ID = "0b9f2c7e-1d2a-4e5b-8c3d-6f7a8b9c0d1e"


def app_row(owned=True, redirect=REDIRECT, perms=("read", "write")):
    return {"id": APP_ID, "slug": APP, "name": "Plane MCP", "client_id": "cid-123",
            "client_secret": STORED, "redirect_uris": redirect, "resource_permissions": list(perms),
            "is_owned": owned}


class StubPlane(DrainingHandler):
    state = {}

    def log_message(self, *args):
        pass

    def _send(self, status, body=None, location=None, cookie=None):
        self.send_response(status)
        if location:
            self.send_header("Location", location)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        data = json.dumps(body).encode() if body is not None else b""
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        # JSON for the API writes; the sign-in form is urlencoded and is never read here.
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n) if n else b""
        return json.loads(raw) if raw and self.headers.get("Content-Type") == "application/json" else None

    def do_GET(self):
        s = self.state
        if self.path == "/auth/get-csrf-token/":
            return self._send(200, {"csrf_token": "tok"}, cookie="csrftoken=tok; Secure; HttpOnly")
        if self.path == BASE_PATH:
            if s["list_status"] != 200:
                return self._send(s["list_status"], {"detail": "list down"})
            return self._send(200, s["apps"])
        return self._send(404, {})

    def do_POST(self):
        s = self.state
        body = self._body()
        if self.path == "/auth/sign-in/":
            if s["sign_in"] == "ok":
                return self._send(302, location="/", cookie="session-id=s; Secure; HttpOnly")
            return self._send(302, location="/?error_code=INVALID&error_message=Invalid+credentials")
        if self.path == BASE_PATH:
            s["calls"].append(("POST", self.path, body))
            status, reply = s["create"]
            return self._send(status, reply)
        return self._send(404, {})

    def do_PATCH(self):
        s = self.state
        body = self._body()
        if self.path == f"{BASE_PATH}{APP_ID}/regenerate-secret/":
            s["calls"].append(("PATCH", self.path, body))
            status, reply = s["regen"]
            return self._send(status, reply)
        if self.path == f"{BASE_PATH}{APP}/":
            s["calls"].append(("PATCH", self.path, body))
            return self._send(200, {"slug": APP, "client_secret": STORED})
        return self._send(404, {})


@pytest.fixture
def plane():
    StubPlane.state = {
        "calls": [], "sign_in": "ok", "list_status": 200, "apps": [],
        "create": (201, {"client_id": "cid-new", "client_secret": SECRET, "slug": APP}),
        "regen": (200, {"client_secret": SECRET, "id": APP_ID}),
    }
    server = http.server.HTTPServer(("127.0.0.1", 0), StubPlane)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield StubPlane.state, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def run(base, **overrides):
    cfg = {"base": base, "host": "plane.test", "origin": "https://plane.test", "email": ME,
           "password": PASSWORD, "workspace_slug": WS, "app_slug": APP, "app_name": "Plane MCP",
           "company_name": "Uhstray.io", "short_description": "MCP server for Plane",
           "redirect_uri": REDIRECT, "have_secret": False, **overrides}
    return subprocess.run([sys.executable, "-I", "-c", SOURCE], input=json.dumps(cfg) + "\n",
                          capture_output=True, text=True, timeout=60)


def the_line(out):
    assert len(out.stdout.splitlines()) == 1, out.stdout + out.stderr
    return json.loads(out.stdout)


def writes(state):
    return [c for c in state["calls"] if c[0] in ("POST", "PATCH")]


def assert_no_leak(out):
    assert PASSWORD not in out.stdout + out.stderr


def test_a_missing_app_is_created_with_the_exact_body_and_its_secret_printed_once(plane):
    state, base = plane
    state["apps"] = []
    out = run(base)
    assert out.returncode == 0, out.stdout + out.stderr
    assert the_line(out) == {"client_id": "cid-new", "client_secret": SECRET, "created": True,
                             "regenerated": False, "patched": []}
    assert state["calls"] == [("POST", BASE_PATH, {
        "name": "Plane MCP", "slug": APP, "short_description": "MCP server for Plane",
        "company_name": "Uhstray.io", "redirect_uris": REDIRECT,
        "resource_permissions": ["read", "write"],
    })]
    assert_no_leak(out)


def test_an_owned_app_with_the_secret_held_is_left_alone_and_prints_no_secret(plane):
    state, base = plane
    state["apps"] = [app_row()]
    out = run(base, have_secret=True)
    assert out.returncode == 0, out.stdout + out.stderr
    assert the_line(out) == {"client_id": "cid-123", "created": False, "regenerated": False, "patched": []}
    assert writes(state) == []
    assert SECRET not in out.stdout + out.stderr
    assert STORED not in out.stdout + out.stderr
    assert_no_leak(out)


def test_an_owned_app_without_the_secret_gets_a_regenerated_one_by_application_id(plane):
    state, base = plane
    state["apps"] = [app_row()]
    out = run(base)
    assert out.returncode == 0, out.stdout + out.stderr
    assert the_line(out) == {"client_id": "cid-123", "client_secret": SECRET, "created": False,
                             "regenerated": True, "patched": []}
    assert state["calls"] == [("PATCH", f"{BASE_PATH}{APP_ID}/regenerate-secret/", None)]
    assert STORED not in out.stdout + out.stderr
    assert_no_leak(out)


def test_matching_fields_are_not_patched(plane):
    state, base = plane
    state["apps"] = [app_row()]
    out = run(base, have_secret=True)
    assert out.returncode == 0, out.stdout + out.stderr
    assert not any(c[0] == "PATCH" and c[1] == f"{BASE_PATH}{APP}/" for c in state["calls"])
    assert the_line(out)["patched"] == []


def test_differing_redirect_and_permissions_are_patched_together_by_slug(plane):
    state, base = plane
    state["apps"] = [app_row(redirect="https://old.test/callback", perms=("read",))]
    out = run(base, have_secret=True)
    assert out.returncode == 0, out.stdout + out.stderr
    assert state["calls"] == [("PATCH", f"{BASE_PATH}{APP}/", {
        "redirect_uris": REDIRECT, "resource_permissions": ["read", "write"],
    })]
    assert the_line(out)["patched"] == ["redirect_uris", "resource_permissions"]
    assert_no_leak(out)


def test_only_the_differing_field_is_in_the_patch(plane):
    state, base = plane
    state["apps"] = [app_row(redirect="https://old.test/callback")]
    out = run(base, have_secret=True)
    assert out.returncode == 0, out.stdout + out.stderr
    assert state["calls"] == [("PATCH", f"{BASE_PATH}{APP}/", {"redirect_uris": REDIRECT})]
    assert the_line(out)["patched"] == ["redirect_uris"]


def test_a_stored_redirect_list_with_an_extra_uri_is_narrowed_to_the_one_asked_for(plane):
    state, base = plane
    state["apps"] = [app_row(redirect=f"{REDIRECT} https://extra.test/callback")]
    out = run(base, have_secret=True)
    assert out.returncode == 0, out.stdout + out.stderr
    assert state["calls"] == [("PATCH", f"{BASE_PATH}{APP}/", {"redirect_uris": REDIRECT})]


def test_two_redirect_uris_in_any_order_are_not_patched(plane):
    # The deploy passes the HTTP and the SSE callbacks; Plane stores them space-separated.
    state, base = plane
    pair = "https://mcp.test/http/auth/callback https://mcp.test/auth/callback"
    state["apps"] = [app_row(redirect=" ".join(reversed(pair.split())))]
    out = run(base, have_secret=True, redirect_uri=pair)
    assert out.returncode == 0, out.stdout + out.stderr
    assert writes(state) == [] and the_line(out)["patched"] == []


def test_a_slug_owned_by_another_owner_is_refused_before_any_write(plane):
    state, base = plane
    state["apps"] = [app_row(owned=False)]
    out = run(base)
    assert out.returncode == 1, out.stdout + out.stderr
    assert json.loads(out.stdout) == {"error": f"application slug {APP} belongs to another owner"}
    assert state["calls"] == []
    assert_no_leak(out)


def test_a_create_refused_on_the_slug_says_it_exists_but_was_not_listed(plane):
    state, base = plane
    state["apps"] = []
    state["create"] = (400, {"slug": ["Application with this slug already exists."]})
    out = run(base)
    assert out.returncode == 1, out.stdout + out.stderr
    assert json.loads(out.stdout) == {"error": f"application slug {APP} exists but was not listed; re-run"}
    assert SECRET not in out.stdout + out.stderr
    assert_no_leak(out)


def test_a_create_refused_otherwise_prints_status_only_and_no_body(plane):
    state, base = plane
    state["apps"] = []
    state["create"] = (400, {"company_name": ["leak-me-not"]})
    out = run(base)
    assert out.returncode == 1, out.stdout + out.stderr
    assert json.loads(out.stdout) == {"error": f"creating application {APP} refused (400)"}
    assert "leak-me-not" not in out.stdout + out.stderr
    assert_no_leak(out)


def test_a_create_that_answers_success_without_a_secret_is_refused(plane):
    state, base = plane
    state["apps"] = []
    state["create"] = (201, {"client_id": "cid-new"})
    out = run(base)
    assert out.returncode == 1, out.stdout + out.stderr
    assert json.loads(out.stdout) == {"error": f"creating application {APP} refused (201)"}


def test_a_refused_regeneration_prints_status_only_and_no_secret(plane):
    state, base = plane
    state["apps"] = [app_row()]
    state["regen"] = (500, {"client_secret": SECRET, "detail": "boom"})
    out = run(base)
    assert out.returncode == 1, out.stdout + out.stderr
    assert json.loads(out.stdout) == {"error": f"regenerating the secret of {APP} refused (500)"}
    assert SECRET not in out.stdout + out.stderr
    assert_no_leak(out)


def test_a_refused_sign_in_writes_nothing(plane):
    state, base = plane
    state["sign_in"] = "refused"
    state["apps"] = []
    out = run(base)
    assert out.returncode == 1, out.stdout + out.stderr
    assert json.loads(out.stdout) == {"error": "/auth/sign-in/: refused (302, Invalid credentials)"}
    assert state["calls"] == []
    assert_no_leak(out)


def test_a_failed_listing_writes_nothing_and_prints_no_body(plane):
    state, base = plane
    state["list_status"] = 500
    out = run(base)
    assert out.returncode == 1, out.stdout + out.stderr
    assert json.loads(out.stdout) == {"error": f"listing applications of {WS} failed (500)"}
    assert state["calls"] == []
    assert "list down" not in out.stdout + out.stderr
    assert_no_leak(out)


def test_the_joined_source_defines_every_name_it_uses():
    # The script is a fragment that only runs after plane_session.py, so ruff's undefined-name
    # check is off for the file (noqa F821); this checks the joined source the playbook runs.
    import builtins
    import symtable

    script = "register_mcp_app.py"
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
