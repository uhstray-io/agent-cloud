"""Run the seed playbook's read-only mode against a synthetic OpenBao endpoint."""

import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("provider", ["", "synthetic-provider-value"])
def test_access_only_never_writes_even_with_provider_inputs(tmp_path, provider):
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
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.path == "/v1/auth/approle/login":
                return self.reply({"auth": {"client_token": "synthetic-login-value"}})
            return self.reply({}, 403)

        def do_GET(self):  # noqa: N802
            requests.append(("GET", self.path))
            return self.reply({}, 404)

        def do_PATCH(self):  # noqa: N802
            requests.append(("PATCH", self.path))
            return self.reply({}, 403)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        extra = {
            "openbao_addr": f"http://127.0.0.1:{server.server_port}",
            "bao_role_id": "synthetic-role",
            "bao_secret_id": "synthetic-role-secret",
            "postiz_verify_access_only": True,
        }
        env = {key: value for key, value in os.environ.items() if not key.startswith("SEED_")}
        env.update(SEED_X_API_KEY=provider, ANSIBLE_LOCAL_TEMP=str(tmp_path),
                   ANSIBLE_STDOUT_CALLBACK="default", ANSIBLE_NOCOLOR="1")
        result = subprocess.run(
            ["ansible-playbook", "-v", "-i", "localhost,", "-c", "local",
             "platform/playbooks/seed-postiz-secrets.yml", "-e", json.dumps(extra)],
            cwd=ROOT, env=env, text=True, capture_output=True, timeout=60,
        )
        output = result.stdout + result.stderr
        assert result.returncode == 0, output
        assert "Read-only Postiz access verified" in output
        assert requests == [("POST", "/v1/auth/approle/login"),
                            ("GET", "/v1/secret/data/services/postiz")]
        for value in [provider, "synthetic-role", "synthetic-role-secret", "synthetic-login-value"]:
            if value:
                assert value not in output
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
