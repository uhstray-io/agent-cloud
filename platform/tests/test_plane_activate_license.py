"""activate_license.py against a stub Plane, and its place in deploy-plane.yml.

The playbook runs plane_session.py and the script joined, with the automation account and the key on
stdin. The stub serves the sign-in redirect, the plan read and the activation route, in the shapes
the v3.3.3 image answers with (plane/payment/views/license_activate.py, product.py).
"""

import json
import pathlib
import subprocess
import sys

import pytest
import yaml
from fake_http import DrainingHandler, serve

ROOT = pathlib.Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "platform/services/plane/deployment"
PLAYBOOK = ROOT / "platform/playbooks/deploy-plane.yml"
SOURCE = (DEPLOY / "plane_session.py").read_text() + "\n" + (DEPLOY / "activate_license.py").read_text()
PASSWORD = "pw-that-must-never-print"
KEY = "key-that-must-never-print"
PLAN = "/api/payments/workspaces/ws/current-plan/"
ACTIVATE = "/api/payments/workspaces/ws/licenses/"


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

    def do_GET(self):
        s = self.state
        s["calls"].append(("GET", self.path))
        if self.path == "/auth/get-csrf-token/":
            return self._send(200, {"csrf_token": "tok"}, cookie="csrftoken=tok; Secure; HttpOnly")
        if self.path == PLAN:
            if s["plan_status"] != 200:
                return self._send(s["plan_status"], {"error": "error fetching product details"})
            return self._send(200, {"product": s["plans"].pop(0) if len(s["plans"]) > 1 else s["plans"][0]})
        return self._send(404, {})

    def do_POST(self):
        s = self.state
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        s["calls"].append(("POST", self.path))
        if self.path == "/auth/sign-in/":
            return self._send(302, location="/", cookie="session-id=s; Secure; HttpOnly")
        if self.path == ACTIVATE:
            s["posted"].append(json.loads(body))
            return self._send(s["activate_status"], s["activate_body"])
        return self._send(404, {})


@pytest.fixture
def plane():
    StubPlane.state = {"calls": [], "posted": [], "plans": ["FREE", "PRO"], "plan_status": 200,
                       "activate_status": 200, "activate_body": {"status": "ok"}}
    with serve(StubPlane) as base:
        yield StubPlane.state, base


def run(base):
    cfg = {"base": base, "host": "plane.test", "origin": "https://plane.test", "email": "a@example.test",
           "password": PASSWORD, "workspace_slug": "ws", "license_key": KEY, "plan": "PRO"}
    return subprocess.run([sys.executable, "-I", "-c", SOURCE], input=json.dumps(cfg) + "\n",
                          capture_output=True, text=True, timeout=60)


def secret_free(out):
    return PASSWORD not in out.stdout + out.stderr and KEY not in out.stdout + out.stderr


def test_a_free_workspace_is_activated_and_read_back(plane):
    state, base = plane
    out = run(base)
    assert out.returncode == 0, out.stdout + out.stderr
    assert json.loads(out.stdout) == {"changed": True, "product": "PRO"}
    assert state["posted"] == [{"license_key": KEY}]
    assert secret_free(out)


def test_a_licensed_workspace_is_never_sent_the_key_again(plane):
    state, base = plane
    state["plans"] = ["PRO"]
    out = run(base)
    assert out.returncode == 0, out.stdout + out.stderr
    assert json.loads(out.stdout) == {"changed": False, "product": "PRO"}
    assert ("POST", ACTIVATE) not in state["calls"]


def test_a_refused_key_reports_the_status_and_never_the_body(plane):
    state, base = plane
    state["plans"] = ["FREE"]
    state["activate_status"] = 400
    state["activate_body"] = {"error": f"invalid {KEY}"}
    out = run(base)
    assert out.returncode == 1, out.stdout + out.stderr
    assert json.loads(out.stdout) == {"error": "licence activation for ws refused (400)"}
    assert secret_free(out)


def test_an_error_status_after_the_key_was_accepted_counts_as_activated_when_the_plan_reads_back(plane):
    # Plane answers 400 when the payment server accepted the key but its own resync failed.
    state, base = plane
    state["activate_status"] = 400
    out = run(base)
    assert out.returncode == 0, out.stdout + out.stderr
    assert json.loads(out.stdout) == {"changed": True, "product": "PRO"}
    assert secret_free(out)


def test_an_activation_that_leaves_the_plan_unchanged_is_refused(plane):
    state, base = plane
    state["plans"] = ["FREE"]
    out = run(base)
    assert out.returncode == 1, out.stdout + out.stderr
    assert json.loads(out.stdout) == {"error": "licence activated but ws reads plan FREE, not PRO"}


def test_an_unreadable_plan_stops_before_any_activation(plane):
    state, base = plane
    state["plan_status"] = 400
    out = run(base)
    assert out.returncode == 1, out.stdout + out.stderr
    assert json.loads(out.stdout) == {"error": "reading the plan of ws failed (400)"}
    assert ("POST", ACTIVATE) not in state["calls"]


def _plays():
    return yaml.safe_load(PLAYBOOK.read_text())


def test_the_key_is_declared_existing_so_the_deploy_never_invents_one():
    phase1 = next(p for p in _plays() if p.get("name", "").startswith("Phase 1"))
    assert {"name": "plane_license_key", "type": "existing"} in phase1["vars"]["_secret_definitions"]


def test_the_licence_play_runs_after_the_service_token_and_before_verify():
    names = [p.get("name", "") for p in _plays()]
    lic = names.index("Phase 3c: Licence")
    assert names[lic - 1] == "Phase 3b: Workspace service token"
    # Before verify; later phases (3d, the GitHub App webhook) may sit between them.
    assert next(i for i, n in enumerate(names) if n.startswith("Phase 4")) > lic


def test_activation_is_skipped_in_check_mode_and_without_a_key_and_the_key_task_is_no_log():
    play = next(p for p in _plays() if p.get("name") == "Phase 3c: Licence")
    block = next(t for t in play["tasks"] if "block" in t)
    assert "not ansible_check_mode" in block["when"]
    assert "_plane_license_key | length > 0" in block["when"]
    script = block["block"][0]
    assert "activate_license.py" in script["ansible.builtin.command"]["argv"][-1]
    assert script["no_log"] is True
