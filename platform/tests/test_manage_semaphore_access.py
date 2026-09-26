"""manage-semaphore-access.yml, run for real against a synthetic Semaphore API.

The playbook's own tasks after the loopback check and the credential lookup are loaded into a
harness play pointed at a local fake, so the reads, the comparison, the writes and the read-back
are exercised as written. All names and the token are synthetic.
"""

import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
PLAYBOOK = ROOT / "platform/playbooks/manage-semaphore-access.yml"
TOKEN = "synthetic-semaphore-token"

pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")


class Fake(BaseHTTPRequestHandler):
    state: dict = {}
    writes: list = []
    ignore_put = False

    def log_message(self, *_args):
        pass

    def reply(self, body, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def body(self):
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")

    def do_GET(self):
        assert self.headers["Authorization"] == f"Bearer {TOKEN}"
        s = type(self).state
        self.reply({"/api/project/1/users": s["members"], "/api/users": s["users"],
                    "/api/project/1/integrations": s["integrations"]}[self.path])

    def do_POST(self):
        body = self.body()
        type(self).writes.append(("POST", self.path, body))
        user = next(u for u in type(self).state["users"] if u["id"] == int(body["user_id"]))
        type(self).state["members"].append({"id": user["id"], "username": user["username"], "role": body["role"]})
        self.reply({}, 204)

    def do_PUT(self):
        body = self.body()
        type(self).writes.append(("PUT", self.path, body))
        uid = int(self.path.rsplit("/", 1)[1])
        for member in type(self).state["members"]:
            if member["id"] == uid and not type(self).ignore_put:
                member["role"] = body["role"]
        self.reply({}, 204)

    def do_DELETE(self):
        type(self).writes.append(("DELETE", self.path, None))
        uid = int(self.path.rsplit("/", 1)[1])
        type(self).state["members"] = [m for m in type(self).state["members"] if m["id"] != uid]
        self.reply({}, 204)


def run(tmp_path, *, declared, integrations=(), check=False, ignore_put=False):
    Fake.writes = []
    Fake.ignore_put = ignore_put
    Fake.state = {
        "users": [{"id": 1, "username": "ops-admin", "admin": True},
                  {"id": 2, "username": "runner-a", "admin": False},
                  {"id": 3, "username": "viewer-b", "admin": False},
                  {"id": 4, "username": "newcomer", "admin": False}],
        "members": [{"id": 2, "username": "runner-a", "role": "task_runner"},
                    {"id": 3, "username": "viewer-b", "role": "guest"}],
        "integrations": list(integrations)}
    server = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        play = yaml.safe_load(PLAYBOOK.read_text())[0]
        names = [t["name"] for t in play["tasks"]]
        # Everything after the loopback check and the credential lookup, unchanged.
        tasks = play["tasks"][names.index("Semaphore runtime access (injected token, or the controller AppRole)") + 1:]
        harness = [{"hosts": "localhost", "connection": "local", "gather_facts": False,
                    "vars": {"_semaphore_url": f"http://127.0.0.1:{server.server_port}", "_pid": 1,
                             "_token": TOKEN, "semaphore_admin_users": ["ops-admin"],
                             **({"semaphore_project_members": declared} if declared is not None else {})},
                    "tasks": tasks}]
        path = tmp_path / "play.yml"
        path.write_text(yaml.safe_dump(harness, sort_keys=False))
        env = {"PATH": os.environ["PATH"], "ANSIBLE_NOCOLOR": "1", "ANSIBLE_LOCAL_TEMP": str(tmp_path),
               "ANSIBLE_FILTER_PLUGINS": str(ROOT / "platform/playbooks/filter_plugins")}
        result = subprocess.run(["ansible-playbook", "-i", "localhost,", str(path), *(["--check"] if check else [])],
                                env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)
    finally:
        server.shutdown()
        server.server_close()
    output = result.stdout + result.stderr
    assert TOKEN not in output
    return result.returncode, output


DECLARED = {"runner-a": "guest", "newcomer": "guest"}  # demote one, add one, drop viewer-b


def test_check_mode_reports_the_drift_and_writes_nothing(tmp_path):
    code, output = run(tmp_path, declared=DECLARED, check=True)
    assert code == 0, output
    assert Fake.writes == []
    assert "To remove: ['viewer-b']" in output and "To add: ['newcomer']" in output


def test_a_real_run_converges_the_team_and_reads_it_back(tmp_path):
    code, output = run(tmp_path, declared=DECLARED)
    assert code == 0, output
    assert sorted(Fake.writes, key=lambda w: w[0]) == [
        ("DELETE", "/api/project/1/users/3", None),
        ("POST", "/api/project/1/users", {"user_id": 4, "role": "guest"}),
        ("PUT", "/api/project/1/users/2", {"role": "guest"})]
    assert {m["username"]: m["role"] for m in Fake.state["members"]} == DECLARED


def test_no_declaration_reports_the_roster_and_writes_nothing(tmp_path):
    code, output = run(tmp_path, declared=None)
    assert code != 0
    assert Fake.writes == []
    assert "'runner-a': 'task_runner'" in output  # the roster an operator declares from
    assert "semaphore_project_members is not declared" in output


def test_a_typo_in_the_declaration_removes_nobody(tmp_path):
    # Replacing viewer-b with a misspelled account must not revoke viewer-b first.
    code, output = run(tmp_path, declared={"runner-a": "task_runner", "viewr-b": "guest"})
    assert code != 0
    assert Fake.writes == []
    assert "Nothing was changed" in output and "declared member viewr-b has no Semaphore account" in output


def test_an_integration_fails_the_run_after_the_team_is_converged(tmp_path):
    code, output = run(tmp_path, declared=DECLARED, integrations=[{"id": 9, "name": "deploy-hook"}])
    assert code != 0
    assert len(Fake.writes) == 3  # removing launch rights is never held back by another problem
    assert "integration deploy-hook can set extra vars" in output


def test_a_write_the_server_did_not_apply_fails_the_read_back(tmp_path):
    code, output = run(tmp_path, declared=DECLARED, ignore_put=True)
    assert code != 0
    assert "The team still differs after convergence" in output and "role ['runner-a']" in output
