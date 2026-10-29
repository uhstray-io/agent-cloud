"""configure_instance.py against a stub Plane: what it changes, what it refuses, what it prints.

The script runs inside plane-api with a credential on stdin and its last stdout line is shown in
the Semaphore log, so it must never print a credential, on success or on failure. These tests
run the real script against a stub HTTP server that records every PATCH.
"""

import http.server
import json
import pathlib
import subprocess
import sys
import threading

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "platform/services/plane/deployment/configure_instance.py"
SECRET = "client-secret-value-that-must-never-print"


class StubPlane(http.server.BaseHTTPRequestHandler):
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

    def do_GET(self):
        s = self.state
        if self.path == "/api/instances/":
            self._send(200, {"instance": {"is_setup_done": True}})
        elif self.path == "/auth/get-csrf-token/":
            self._send(200, {"csrf_token": "tok"}, cookie="csrftoken=tok; Secure; HttpOnly")
        elif self.path == "/api/instances/configurations/":
            self._send(200, [{"key": k, "value": v} for k, v in s["config"].items()])
        elif self.path == "/api/instances/workspaces/":
            self._send(200, {"results": [{"slug": "ws"}]})
        else:
            self._send(404, {})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path == "/api/instances/admins/sign-in/":
            self._send(302, location="/god-mode/general/", cookie="admin-session-id=s; Secure; HttpOnly")
        else:
            self._send(404, {})

    def do_PATCH(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        self.state["patches"].append(body)
        if self.state.get("patch_status", 200) == 200:
            self.state["config"].update(body)
        self._send(self.state.get("patch_status", 200), [])


@pytest.fixture
def plane():
    StubPlane.state = {
        "config": {"ENABLE_SIGNUP": "1", "IS_OIDC_ENABLED": "0", "OIDC_CLIENT_SECRET": ""},
        "patches": [],
    }
    server = http.server.HTTPServer(("127.0.0.1", 0), StubPlane)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield StubPlane.state, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def run(base, settings):
    cfg = {"base": base, "host": "plane.test", "origin": "https://plane.test",
           "email": "a@example.test", "password": "pw-never-printed", "first_name": "P",
           "last_name": "A", "company": "c", "workspace_slug": "ws", "workspace_name": "ws",
           "settings": settings}
    return subprocess.run([sys.executable, "-I", str(SCRIPT)], input=json.dumps(cfg) + "\n",
                          capture_output=True, text=True, timeout=30)


def test_changed_settings_are_patched_and_reported_by_name_only(plane):
    state, base = plane
    out = run(base, {"IS_OIDC_ENABLED": "1", "OIDC_CLIENT_SECRET": SECRET})
    assert out.returncode == 0, out.stdout + out.stderr
    assert state["patches"] == [{"ENABLE_SIGNUP": "0", "IS_OIDC_ENABLED": "1", "OIDC_CLIENT_SECRET": SECRET}]
    assert SECRET not in out.stdout and "pw-never-printed" not in out.stdout
    summary = json.loads(out.stdout.splitlines()[-1])
    assert summary["changed"] is True
    assert "OIDC_CLIENT_SECRET" in summary["actions"][0]


def test_a_second_run_with_matching_settings_changes_nothing(plane):
    state, base = plane
    run(base, {"IS_OIDC_ENABLED": "1", "OIDC_CLIENT_SECRET": SECRET})
    state["patches"].clear()
    out = run(base, {"IS_OIDC_ENABLED": "1", "OIDC_CLIENT_SECRET": SECRET})
    assert out.returncode == 0
    assert state["patches"] == []
    assert json.loads(out.stdout.splitlines()[-1]) == {"changed": False, "actions": []}


def test_the_playbook_cannot_reopen_sign_up(plane):
    state, base = plane
    run(base, {"ENABLE_SIGNUP": "1"})
    assert state["config"]["ENABLE_SIGNUP"] == "0"


def test_an_unknown_setting_is_refused_before_any_patch(plane):
    state, base = plane
    out = run(base, {"NO_SUCH_SETTING": "x", "OIDC_CLIENT_SECRET": SECRET})
    assert out.returncode == 1
    assert state["patches"] == []
    assert "NO_SUCH_SETTING" in json.loads(out.stdout.splitlines()[-1])["error"]
    assert SECRET not in out.stdout


def test_a_refused_patch_never_prints_the_secret(plane):
    state, base = plane
    state["patch_status"] = 400
    out = run(base, {"OIDC_CLIENT_SECRET": SECRET})
    assert out.returncode == 1
    assert SECRET not in out.stdout and SECRET not in out.stderr
    assert "error" in json.loads(out.stdout.splitlines()[-1])
