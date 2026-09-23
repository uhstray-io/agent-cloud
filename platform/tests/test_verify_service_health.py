"""verify-service-health.yml (workflow step service-validate) passes only on HTTP 200.

Runs the real playbook against a throwaway local HTTP server. The inventory's
health_status_codes admits e.g. OpenBao's sealed 503 for the fleet overview; the step's
criterion is 200 and must not inherit that (PR 203 Codex review). Requires ansible-playbook.
"""

import http.server
import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PLAYBOOK = REPO / "platform/playbooks/verify-service-health.yml"

pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")


def _serve(status: int):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - the stdlib's name
            self.send_response(status)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _run(tmp_path, status: int, health_path: str = "/health") -> subprocess.CompletedProcess:
    server = _serve(status)
    try:
        inventory = tmp_path / "inventory.ini"
        inventory.write_text(
            "[demo_svc]\ndemo ansible_connection=local\n\n[demo_svc:vars]\n"
            f"service_name=demo\nservice_url=http://127.0.0.1:{server.server_port}\n"
            f"health_path={health_path}\nhealth_status_codes=[200, 503]\n"
        )
        env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
        env["ANSIBLE_NOCOLOR"] = "1"
        return subprocess.run(
            ["ansible-playbook", "-i", str(inventory), str(PLAYBOOK), "-e", "target_service=demo_svc"],
            cwd=REPO, env=env, text=True, capture_output=True, stdin=subprocess.DEVNULL,
        )
    finally:
        server.shutdown()


def test_200_passes(tmp_path):
    done = _run(tmp_path, 200)
    assert done.returncode == 0, done.stdout[-2000:]


def test_503_fails_even_when_the_inventory_admits_it(tmp_path):
    done = _run(tmp_path, 503)
    assert done.returncode != 0
    assert "answered 503, expected one of 200" in done.stdout


def test_no_declared_health_path_fails(tmp_path):
    done = _run(tmp_path, 200, health_path="")
    assert done.returncode != 0
    assert "no declared service_url + health_path" in done.stdout
