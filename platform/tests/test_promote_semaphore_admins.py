"""promote-semaphore-admins.yml, run whole against a synthetic Semaphore API.

Admin is derived, never hand-granted (#604): every platform_admins entry is promoted, plus the
Semaphore-only names in semaphore_admin_users. Both are read from top-level all.vars because the
play runs on implicit localhost, which receives no group's vars. All names and the token are
synthetic.
"""

import json
import os
import shutil
import subprocess
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml
from fake_http import DrainingHandler

ROOT = Path(__file__).resolve().parents[2]
PLAYBOOK = ROOT / "platform/playbooks/promote-semaphore-admins.yml"
TOKEN = "synthetic-semaphore-token"

pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")

ADMINS = [{"key": "ops", "username": "ops-admin", "name": "Ops", "email": "ops@example.invalid"},
          {"key": "second", "username": "second-operator", "name": "Second", "email": "second@example.invalid"},
          {"key": "unseen", "username": "not-signed-in", "name": "Unseen", "email": "unseen@example.invalid"},
          {"key": "svc", "username": "svc-robot", "name": "Robot", "type": "service_account"}]


class Fake(DrainingHandler):
    users: list = []
    writes: list = []
    ignore_put = False
    put_status = 204

    def log_message(self, *_args):
        pass

    def reply(self, body, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def do_GET(self):
        assert self.headers["Authorization"] == f"Bearer {TOKEN}"
        assert self.path == "/api/users"
        self.reply(type(self).users)

    def do_PUT(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        type(self).writes.append((self.path, body))
        uid = int(self.path.rsplit("/", 1)[1])
        for account in type(self).users:
            if account["id"] == uid and not type(self).ignore_put and type(self).put_status == 204:
                account["admin"] = body["admin"]
        self.reply({}, type(self).put_status)


def user(uid, username, admin=False):
    return {"id": uid, "username": username, "name": username, "email": f"{username}@example.invalid",
            "admin": admin, "external": True, "alert": False}


def run(tmp_path, all_vars, *, group_vars=None, check=False, ignore_put=False, put_status=204):
    Fake.writes = []
    Fake.ignore_put = ignore_put
    Fake.put_status = put_status
    Fake.users = [user(1, "local-admin", admin=True), user(2, "ops-admin", admin=True), user(3, "second-operator"),
                  user(4, "svc-robot"), user(5, "bystander")]
    inventory = {"all": {"vars": all_vars, "children": {"agent_cloud": {"vars": group_vars or {}, "hosts": {"h": {}}}}}}
    (tmp_path / "inventory.yml").write_text(yaml.safe_dump(inventory, sort_keys=False))
    server = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        env = {"PATH": os.environ["PATH"], "ANSIBLE_NOCOLOR": "1", "ANSIBLE_LOCAL_TEMP": str(tmp_path),
               "SEMAPHORE_URL": f"http://127.0.0.1:{server.server_port}", "SEMAPHORE_TOKEN": TOKEN}
        result = subprocess.run(["ansible-playbook", "-i", str(tmp_path / "inventory.yml"), str(PLAYBOOK),
                                 *(["--check"] if check else [])],
                                cwd=ROOT, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                                timeout=120)
    finally:
        server.shutdown()
        server.server_close()
    output = result.stdout + result.stderr
    assert TOKEN not in output
    return result.returncode, output


def test_a_platform_admin_who_is_not_yet_a_semaphore_admin_is_promoted(tmp_path):
    code, output = run(tmp_path, {"platform_admins": ADMINS, "semaphore_admin_users": ["local-admin"]})
    assert code == 0, output
    # The service account has a Semaphore account here and is still never promoted.
    assert [(path, body["username"], body["admin"]) for path, body in Fake.writes] == [
        ("/api/users/3", "second-operator", True)]
    assert "Declared admins with no Semaphore account yet" in output and "not-signed-in" in output


def test_check_mode_promotes_nobody(tmp_path):
    code, output = run(tmp_path, {"platform_admins": ADMINS, "semaphore_admin_users": ["local-admin"]}, check=True)
    assert code == 0, output
    assert Fake.writes == []
    assert "Would promote (check mode) 1 user(s)" in output and "['second-operator']" in output


def test_a_promotion_the_server_did_not_apply_fails_the_read_back(tmp_path):
    code, output = run(tmp_path, {"platform_admins": ADMINS, "semaphore_admin_users": ["local-admin"]},
                       ignore_put=True)
    assert code != 0
    assert "still not admin: ['second-operator']" in output


def test_a_refused_promotion_names_the_account_and_status(tmp_path):
    # The PUT task is no_log, so its own failure says only "details hidden" (task 9843, #604).
    code, output = run(tmp_path, {"platform_admins": ADMINS, "semaphore_admin_users": ["local-admin"]},
                       put_status=401)
    assert code != 0
    assert "Semaphore refused these promotions" in output and "['second-operator', 401]" in output


def test_the_admin_list_under_a_group_is_invisible_to_this_play(tmp_path):
    # Why the declarations belong in all.vars: implicit localhost receives no group's vars.
    code, output = run(tmp_path, {"semaphore_admin_users": ["local-admin"]}, group_vars={"platform_admins": ADMINS})
    assert code == 0, output
    assert Fake.writes == []


def test_no_admin_declared_anywhere_fails_before_reading_semaphore(tmp_path):
    code, output = run(tmp_path, {})
    assert code != 0
    assert "No Semaphore admin is declared" in output
    assert Fake.writes == []
