"""Run a seed playbook for real against a synthetic OpenBao (shared test helper, not a test).

Used by test_postiz_access_only.py and test_postiz_seed_write.py. A test supplies only its
request handling (a FakeBao subclass); the server, the inventory file that declares the
address, the environment scrub and the no-secret-in-output check live here once. All values
are synthetic.
"""

import json
import os
import subprocess
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ROLE = {"bao_role_id": "synthetic-role", "bao_secret_id": "synthetic-role-secret"}
LOGIN = "synthetic-login-value"
NEVER_PRINTED = ("synthetic-role", "synthetic-role-secret", LOGIN, "synthetic-leftover-value")


class FakeBao(BaseHTTPRequestHandler):
    """Subclass per test and define do_GET / do_POST / do_PATCH; call record() in each."""

    requests: list = []

    def log_message(self, *_args):
        pass

    def record(self, method):
        type(self).requests.append((method, self.path))

    def body(self):
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")

    def reply(self, body, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())


@contextmanager
def serve(handler):
    """The synthetic OpenBao's address while it runs."""
    # Default listen backlog is 5; a playbook's request bursts on a loaded machine got
    # "Connection reset by peer" (see test_scoped_publication.py).
    class Server(ThreadingHTTPServer):
        request_queue_size = 128
        daemon_threads = True

    server = Server(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def run_seed(tmp_path, playbook, address, *, extra=None, inputs=None, declare=True, env_addr=None,
             verbose=False, declared=None, inventory_vars=None):
    """Run `playbook` once and return (returncode, output).

    The address is declared in the inventory's all.vars, as in production (the seed refuses
    any other: tasks/assert-bao-addr-declared.yml). `inputs` are the process-environment
    seed inputs; no other SEED_* / BAO_VALUE / OPENBAO_ADDR leaks in from the caller's shell.
    """
    inventory = tmp_path / "inventory.yml"
    group = {"vars": {"openbao_addr": declared or address, **(inventory_vars or {})}} if declare else {"hosts": {}}
    inventory.write_text(json.dumps({"all": group}))
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("SEED_") and k not in ("BAO_VALUE", "OPENBAO_ADDR")}
    env.update(inputs or {})
    env.update({"OPENBAO_ADDR": env_addr} if env_addr else {})
    # Stock output on purpose: stricter than production's redact_requests (MISTAKES 4.6).
    env.update(ANSIBLE_LOCAL_TEMP=str(tmp_path), ANSIBLE_STDOUT_CALLBACK="default", ANSIBLE_NOCOLOR="1")
    result = subprocess.run(
        ["ansible-playbook", *(["-v"] if verbose else []), "-i", str(inventory), "-c", "local", playbook,
         "-e", json.dumps({**ROLE, **(extra or {})})],
        cwd=ROOT, env=env, text=True, capture_output=True, timeout=120, stdin=subprocess.DEVNULL,
    )
    output = result.stdout + result.stderr
    for value in (*NEVER_PRINTED, *(inputs or {}).values()):
        assert value not in output
    return result.returncode, output


def writes(requests):
    """Every request that would change the store."""
    return [r for r in requests if r[0] in ("PATCH", "PUT") or (r[0] == "POST" and r[1].startswith("/v1/secret/"))]
