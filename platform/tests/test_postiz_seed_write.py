"""seed-postiz-secrets.yml's write path, run for real against a stateful synthetic OpenBao.

The playbook writes through tasks/bao-merge-keys.yml (since 2026-09-25; it kept its own copy of
the merge-patch idiom before). Proves the behaviour the copy had: a new path is created with CAS
0, an existing path is merge-patched with its siblings kept, and a re-run writes nothing. All
values are synthetic.
"""

import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PLAYBOOK = "platform/playbooks/seed-postiz-secrets.yml"
PROVIDER = "synthetic-provider-value"

pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")


def run_seed(tmp_path, stored):
    """Run the seed once with SEED_X_API_KEY set; `stored` is the path's data (None = absent)."""
    state = {"data": stored, "version": 0 if stored is None else 1}
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def reply(self, body, status=200):
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def body(self):
            return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")

        def do_POST(self):  # noqa: N802
            requests.append(("POST", self.path))
            payload = self.body()
            if self.path == "/v1/auth/approle/login":
                return self.reply({"auth": {"client_token": "synthetic-login-value"}})
            if self.path == "/v1/secret/data/services/postiz":
                if payload.get("options", {}).get("cas") == 0 and state["data"] is not None:
                    return self.reply({"errors": ["check-and-set parameter did not match"]}, 400)
                state["data"], state["version"] = payload["data"], state["version"] + 1
                return self.reply({"data": {"version": state["version"]}})
            return self.reply({}, 403)

        def do_GET(self):  # noqa: N802
            requests.append(("GET", self.path))
            if state["data"] is None:
                return self.reply({}, 404)
            return self.reply({"data": {"data": state["data"], "metadata": {"version": state["version"]}}})

        def do_PATCH(self):  # noqa: N802
            requests.append(("PATCH", self.path))
            if state["data"] is None:
                return self.reply({}, 404)
            state["data"] = {**state["data"], **self.body()["data"]}
            state["version"] += 1
            return self.reply({"data": {"version": state["version"]}})

    class Server(ThreadingHTTPServer):
        request_queue_size = 128
        daemon_threads = True

    server = Server(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        # The address comes from the inventory's all.vars, as in production.
        inventory = tmp_path / "inventory.yml"
        inventory.write_text(json.dumps({"all": {"vars": {"openbao_addr": f"http://127.0.0.1:{server.server_port}"}}}))
        extra = {"bao_role_id": "synthetic-role", "bao_secret_id": "synthetic-role-secret"}
        env = {k: v for k, v in os.environ.items() if not k.startswith("SEED_") and k != "BAO_VALUE"}
        env.update(SEED_X_API_KEY=PROVIDER, ANSIBLE_LOCAL_TEMP=str(tmp_path), ANSIBLE_NOCOLOR="1",
                   # Stock output on purpose: stricter than production's redact_requests.
                   ANSIBLE_STDOUT_CALLBACK="default")
        result = subprocess.run(["ansible-playbook", "-i", str(inventory), "-c", "local", PLAYBOOK,
                                 "-e", json.dumps(extra)],
                                cwd=ROOT, env=env, text=True, capture_output=True, timeout=120,
                                stdin=subprocess.DEVNULL)
    finally:
        server.shutdown()
    output = result.stdout + result.stderr
    for value in (PROVIDER, "synthetic-role-secret", "synthetic-login-value"):
        assert value not in output
    return result.returncode, output, state, requests


def writes(requests):
    return [r for r in requests if r[0] in ("PATCH", "PUT") or (r[0] == "POST" and r[1].startswith("/v1/secret/"))]


def test_a_new_path_is_created_atomically(tmp_path):
    code, output, state, requests = run_seed(tmp_path, None)
    assert code == 0, output
    assert state["data"] == {"postiz_x_api_key": PROVIDER}
    assert writes(requests) == [("POST", "/v1/secret/data/services/postiz")]
    assert "Seeded 1 credential(s)" in output and "(new path)" in output


def test_an_existing_path_keeps_its_generated_siblings(tmp_path):
    generated = {"postiz_jwt_secret": "synthetic-generated", "postiz_db_password": "synthetic-db"}
    code, output, state, requests = run_seed(tmp_path, dict(generated))
    assert code == 0, output
    assert state["data"] == {**generated, "postiz_x_api_key": PROVIDER}
    assert writes(requests) == [("PATCH", "/v1/secret/data/services/postiz")]


def test_a_rerun_with_the_same_value_writes_nothing(tmp_path):
    code, output, state, requests = run_seed(tmp_path, {"postiz_x_api_key": PROVIDER, "other": "kept"})
    assert code == 0, output
    assert writes(requests) == []
    assert state["version"] == 1
    assert "Already stored, nothing written" in output
