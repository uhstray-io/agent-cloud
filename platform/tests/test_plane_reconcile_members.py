"""reconcile_members.py against a stub Plane: what it plans, what it changes, what it never touches.

The script runs inside plane-api with the automation password on stdin and its last stdout line is
shown in the Semaphore log. These tests run the real script against a stub that serves Plane
v3.3.3's shapes: a cursor-paginated member roster with `role_slug` and `is_active`, a plain
invitation list, and `role_slug` on PATCH.
"""

import http.server
import json
import pathlib
import subprocess
import sys
import threading
import urllib.parse

import pytest
from fake_http import DrainingHandler

DEPLOY = pathlib.Path(__file__).resolve().parents[2] / "platform/services/plane/deployment"
# The playbook runs plane_session.py and the script joined, exactly as here.
SOURCE = (DEPLOY / "plane_session.py").read_text() + "\n" + (DEPLOY / "reconcile_members.py").read_text()
PASSWORD = "pw-that-must-never-print"
ME = "automation@example.test"


def member(mid, email, role, active=True):
    return {"id": mid, "member": {"email": email}, "role_slug": role, "is_active": active}


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
        n = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        s = self.state
        url = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(url.query)
        if url.path == "/auth/get-csrf-token/":
            self._send(200, {"csrf_token": "tok"}, cookie="csrftoken=tok; Secure; HttpOnly")
        elif url.path == "/api/workspaces/ws/members/":
            s["member_queries"].append(query)
            # Two pages of one row each, so the cursor has to be followed.
            page = 1 if query.get("cursor") == ["1:1:0"] else 0
            rows = s["members"][:1] if page == 0 else s["members"][1:]
            self._send(200, {"results": rows, "next_cursor": "1:1:0", "next_page_results": page == 0})
        elif url.path == "/api/workspaces/ws/invitations/":
            self._send(200, s["invites"])
        else:
            self._send(404, {})

    def do_POST(self):
        body = self._body()
        if self.path == "/auth/sign-in/":
            self._send(302, location="/", cookie="session-id=s; Secure; HttpOnly")
        elif self.path == "/api/workspaces/ws/invitations/":
            self.state["calls"].append(("POST", self.path, json.loads(body)))
            self._send(self.state.get("invite_status", 200), self.state.get("invite_body", {"message": "ok"}))
        else:
            self._send(404, {})

    def do_PATCH(self):
        self.state["calls"].append(("PATCH", self.path, json.loads(self._body())))
        self._send(200, {})

    def do_DELETE(self):
        self.state["calls"].append(("DELETE", self.path, None))
        self._send(204)


@pytest.fixture
def plane():
    StubPlane.state = {"members": [], "invites": [], "calls": [], "member_queries": []}
    server = http.server.HTTPServer(("127.0.0.1", 0), StubPlane)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield StubPlane.state, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def run(base, desired, apply):
    cfg = {"base": base, "host": "plane.test", "origin": "https://plane.test", "email": ME,
           "password": PASSWORD, "workspace_slug": "ws", "desired": desired, "apply": apply}
    return subprocess.run([sys.executable, "-I", "-c", SOURCE], input=json.dumps(cfg) + "\n",
                          capture_output=True, text=True, timeout=30)


def summary(out):
    assert out.returncode == 0, out.stdout + out.stderr
    assert PASSWORD not in out.stdout + out.stderr
    return json.loads(out.stdout.splitlines()[-1])


def test_without_apply_it_plans_and_changes_nothing(plane):
    state, base = plane
    state["members"] = [member("m1", ME, "owner"), member("m2", "gone@example.test", "member")]
    out = summary(run(base, {"new@example.test": "admin"}, apply=False))
    assert state["calls"] == []
    assert out == {"changed": False, "applied": False, "actions": [],
                   "plan": {"invite": ["new@example.test (admin)"], "set_role": [],
                            "remove": ["gone@example.test"]}}


def test_apply_invites_sets_roles_and_removes(plane):
    state, base = plane
    state["members"] = [member("m1", "Dev@Example.test", "member"), member("m2", "left@example.test", "member")]
    state["invites"] = [{"id": "i1", "email": "pending@example.test", "role_slug": "member", "accepted": False},
                        {"id": "i2", "email": "stale@example.test", "role_slug": "member", "accepted": False}]
    out = summary(run(base, {"dev@example.test": "admin", "pending@example.test": "admin",
                             "new@example.test": "member"}, apply=True))
    assert out["changed"] is True
    assert state["calls"] == [
        ("POST", "/api/workspaces/ws/invitations/", {"emails": [{"email": "new@example.test", "role_slug": "member"}]}),
        ("PATCH", "/api/workspaces/ws/members/m1/", {"role_slug": "admin"}),
        ("PATCH", "/api/workspaces/ws/invitations/i1/", {"role_slug": "admin"}),
        ("DELETE", "/api/workspaces/ws/members/m2/", None),
        ("DELETE", "/api/workspaces/ws/invitations/i2/", None),
    ]


def test_the_roster_is_people_only_and_every_page_is_read(plane):
    state, base = plane
    state["members"] = [member("m1", "a@example.test", "member"), member("m2", "b@example.test", "member")]
    out = summary(run(base, {"a@example.test": "member"}, apply=False))
    # b is on the second page: reading only the first would miss the removal.
    assert out["plan"]["remove"] == ["b@example.test"]
    assert all(q.get("membership_type") == ["people"] for q in state["member_queries"])
    assert len(state["member_queries"]) == 2


def test_the_automation_account_owners_and_inactive_rows_are_never_touched(plane):
    state, base = plane
    state["members"] = [member("m1", ME, "owner"), member("m2", "owner2@example.test", "owner"),
                        member("m3", "was@example.test", "member", active=False)]
    out = summary(run(base, {"owner2@example.test": "member", "was@example.test": "member"}, apply=True))
    # The deactivated row counts as absent, so the person comes back by invitation.
    assert state["calls"] == [
        ("POST", "/api/workspaces/ws/invitations/", {"emails": [{"email": "was@example.test", "role_slug": "member"}]}),
    ]
    assert out["plan"]["remove"] == [] and out["plan"]["set_role"] == []


def test_a_converged_workspace_changes_nothing(plane):
    state, base = plane
    state["members"] = [member("m1", ME, "owner"), member("m2", "dev@example.test", "member")]
    out = summary(run(base, {"dev@example.test": "member"}, apply=True))
    assert state["calls"] == []
    assert out["changed"] is False


def test_a_refused_invite_fails_with_plane_error_and_no_password(plane):
    state, base = plane
    state["invite_status"] = 403
    state["invite_body"] = {"error": "Admin role is not available on this plan"}
    out = run(base, {"new@example.test": "admin"}, apply=True)
    assert out.returncode == 1
    assert "Admin role is not available on this plan" in out.stdout
    assert PASSWORD not in out.stdout + out.stderr
