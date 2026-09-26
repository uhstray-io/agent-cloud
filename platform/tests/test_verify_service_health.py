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


def _run(tmp_path, status: int, health_path: str = "/health", via_health_url: bool = False,
         host_vars: str = ""):
    server = _serve(status)
    try:
        inventory = tmp_path / "inventory.ini"
        where = (f"health_url=http://127.0.0.1:{server.server_port}/health\n" if via_health_url else
                 f"service_url=http://127.0.0.1:{server.server_port}\nhealth_path={health_path}\n")
        inventory.write_text(
            f"[demo_svc]\ndemo ansible_connection=local {host_vars}\n\n[demo_svc:vars]\n"
            f"service_name=demo\n{where}health_status_codes=[200, 503]\n"
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
    assert "no declared health_url or service_url + health_path" in done.stdout


def test_health_url_alone_is_a_declared_contract(tmp_path):
    # Local-dev declares health_url only: the executor reaches services by container name, and
    # service_url would be written into the service's stored secrets.
    assert _run(tmp_path, 200, via_health_url=True).returncode == 0
    assert _run(tmp_path, 503, via_health_url=True).returncode != 0


def test_health_probe_on_host_runs_the_probe_on_the_target(tmp_path):
    # Production tududi/honcho/n8n publish on loopback or firewall the port to Caddy, so the
    # executor's probe answered -1 for healthy services. The target here has no usable Python:
    # a probe delegated to the executor still passes, one that runs on the target cannot.
    broken = "ansible_python_interpreter=/nonexistent/python"
    assert _run(tmp_path, 200, via_health_url=True, host_vars=broken).returncode == 0
    on_host = _run(tmp_path, 200, via_health_url=True, host_vars=broken + " health_probe_on_host=true")
    assert on_host.returncode != 0
    assert "answered no response" in on_host.stdout, on_host.stdout[-2000:]
    assert _run(tmp_path, 200, via_health_url=True, host_vars="health_probe_on_host=true").returncode == 0


def test_the_local_inventory_declares_the_verified_health_urls():
    # PR 203 Codex review: the generated local inventory declared no health contract, so the
    # local executor could not pass for a healthy service.
    text = (REPO / "platform/playbooks/bootstrap-local-dev.yml").read_text()
    for group in ("step_ca_svc", "authentik_svc", "o11y_svc", "agentgateway_svc"):
        section = text.split(f"[{group}:vars]", 1)[1].split("\n          [", 1)[0]
        assert "health_url=http" in section, group


def test_a_target_group_with_no_hosts_fails(tmp_path):
    # PR 203 Codex review: a play whose hosts: matched nothing exited 0 with no step result.
    inventory = tmp_path / "inventory.ini"
    inventory.write_text("[demo_svc]\ndemo ansible_connection=local\n")
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env["ANSIBLE_NOCOLOR"] = "1"
    done = subprocess.run(
        ["ansible-playbook", "-i", str(inventory), str(PLAYBOOK), "-e", "target_service=typo_svc"],
        cwd=REPO, env=env, text=True, capture_output=True, stdin=subprocess.DEVNULL,
    )
    assert done.returncode != 0
    assert "matches no hosts in this inventory" in done.stdout


def test_one_failing_host_fails_the_recorded_result(tmp_path):
    # PR 203 Codex review: emit-step-result records once per play, and recorded the first host's
    # verdict; a healthy first host hid a failing second one.
    good, bad = _serve(200), _serve(503)
    try:
        inventory = tmp_path / "inventory.ini"
        inventory.write_text(
            "[demo_svc]\n"
            f"demo1 ansible_connection=local health_url=http://127.0.0.1:{good.server_port}/health\n"
            f"demo2 ansible_connection=local health_url=http://127.0.0.1:{bad.server_port}/health\n"
            "\n[demo_svc:vars]\nservice_name=demo\n"
        )
        env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
        env["ANSIBLE_NOCOLOR"] = "1"
        done = subprocess.run(
            ["ansible-playbook", "-i", str(inventory), str(PLAYBOOK), "-e", "target_service=demo_svc"],
            cwd=REPO, env=env, text=True, capture_output=True, stdin=subprocess.DEVNULL,
        )
    finally:
        good.shutdown()
        bad.shutdown()
    assert done.returncode != 0
    recorded = done.stdout.split("CUSTOM STATS", 1)[-1]
    assert '"status": "fail"' in recorded and "answered 503" in recorded, recorded[-1500:]
