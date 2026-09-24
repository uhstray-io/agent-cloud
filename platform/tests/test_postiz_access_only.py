"""Run both seed playbooks' read-only access checks against a synthetic OpenBao.

The check must never write, must not pass on a GET 404 alone (OpenBao answers 404 for a
missing path AND for one the token cannot see), and must require the capability the real
seed uses: `create` to POST a new path, `patch` to PATCH an existing one (reviews of
PR #205). tasks/assert-bao-seed-access.yml is the shared implementation.
"""

import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

PLAYBOOKS = {
    "postiz": ("platform/playbooks/seed-postiz-secrets.yml", "services/postiz",
               {"postiz_verify_access_only": True}, "Read-only Postiz access verified"),
    "openbao-key": ("platform/playbooks/seed-openbao-key.yml", "services/agentgateway",
                    {"bao_verify_access_only": True, "bao_path": "services/agentgateway",
                     "bao_key": "vllm_api_key"}, "Read-only access to secret/services/agentgateway verified"),
}


def writes(requests):
    return [r for r in requests if r[0] in ("PATCH", "PUT") or (r[0] == "POST" and r[1].startswith("/v1/secret/"))]


def run_access_check(tmp_path, which, capabilities, provider="", exists=False):
    playbook, path, extra_vars, _ = PLAYBOOKS[which]
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def reply(self, body, status=200):
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def do_POST(self):  # noqa: N802
            requests.append(("POST", self.path))
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.path == "/v1/auth/approle/login":
                return self.reply({"auth": {"client_token": "synthetic-login-value"}})
            if self.path == "/v1/sys/capabilities-self":
                assert json.loads(body) == {"paths": [f"secret/data/{path}"]}
                return self.reply({"capabilities": capabilities, f"secret/data/{path}": capabilities})
            return self.reply({}, 403)

        def do_GET(self):  # noqa: N802
            requests.append(("GET", self.path))
            if exists:
                return self.reply({"data": {"data": {"unrelated": "x"}, "metadata": {"version": 1}}})
            return self.reply({}, 404)  # a missing path, or one this token cannot see

        def do_PATCH(self):  # noqa: N802
            requests.append(("PATCH", self.path))
            return self.reply({}, 403)

    # Default listen backlog is 5; a playbook's request bursts on a loaded machine got
    # "Connection reset by peer" (see test_scoped_publication.py).
    class Server(ThreadingHTTPServer):
        request_queue_size = 128
        daemon_threads = True

    server = Server(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        extra = {"openbao_addr": f"http://127.0.0.1:{server.server_port}",
                 "bao_role_id": "synthetic-role", "bao_secret_id": "synthetic-role-secret", **extra_vars}
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("SEED_") and key != "BAO_VALUE"}
        env.update(SEED_X_API_KEY=provider, BAO_VALUE=provider, ANSIBLE_LOCAL_TEMP=str(tmp_path),
                   ANSIBLE_STDOUT_CALLBACK="default", ANSIBLE_NOCOLOR="1")
        result = subprocess.run(
            ["ansible-playbook", "-v", "-i", "localhost,", "-c", "local", playbook, "-e", json.dumps(extra)],
            cwd=ROOT, env=env, text=True, capture_output=True, timeout=90,
        )
        output = result.stdout + result.stderr
        for value in [provider, "synthetic-role", "synthetic-role-secret", "synthetic-login-value"]:
            if value:
                assert value not in output
        return result.returncode, output, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("which", sorted(PLAYBOOKS))
@pytest.mark.parametrize("exists,capabilities", [(False, ["read", "create"]), (True, ["read", "patch"]),
                                                 (False, ["root"]), (True, ["root"])])
def test_access_check_passes_with_the_capability_the_seed_uses(tmp_path, which, exists, capabilities):
    code, output, requests = run_access_check(tmp_path, which, capabilities, exists=exists)
    assert code == 0, output
    assert PLAYBOOKS[which][3] in output
    assert ("POST", "/v1/sys/capabilities-self") in requests
    assert writes(requests) == []


@pytest.mark.parametrize("which", sorted(PLAYBOOKS))
def test_access_check_never_writes_even_with_a_staged_value(tmp_path, which):
    code, output, requests = run_access_check(tmp_path, which, ["read", "create"], "synthetic-provider-value")
    assert code == 0, output
    assert writes(requests) == []


@pytest.mark.parametrize("which", sorted(PLAYBOOKS))
@pytest.mark.parametrize("exists,capabilities", [
    (False, ["deny"]), (False, ["read"]),
    (False, ["read", "update"]), (False, ["read", "patch"]),   # new path is POSTed: needs create
    (True, ["read", "create"]), (True, ["read", "update"]),    # existing path is PATCHed: needs patch
    (False, ["create"]),                                        # the seed reads first
])
def test_access_check_refuses_a_token_the_real_seed_would_be_denied(tmp_path, which, exists, capabilities):
    code, output, requests = run_access_check(tmp_path, which, capabilities, exists=exists)
    assert code != 0, output
    assert PLAYBOOKS[which][3] not in output
    assert "seeding needs read plus" in output
    assert writes(requests) == []
