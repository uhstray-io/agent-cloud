"""The console's launch-gate probe, run against a live launch gate (docs/MISTAKES.md 10.17).

deploy-console.yml's Phase 3 probe POSTs to the gate with no token and must read the gate's 401. This file takes that
argv from the playbook's own Phase 3 vars, renders it with ansible's templar (the helper test_console_phase3_creds.py
uses), and runs it with curl against the real launch gate: launch_gate.py's Gate behind its GateServer, over TLS, on
loopback. The gate, its certificate and its config are test_launch_gate_tls.py's machinery.

The control runs the same argv with its body removed. The gate refuses that request in its own framing (400, before
it authenticates), so a 401 can only come from the body the playbook sends.
"""

import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest
import test_launch_gate_tls as gate_tls
from cryptography.hazmat.primitives import serialization
from test_console_phase3_creds import _render, _verify_vars

lg = gate_tls.lg
NEEDS_ANSIBLE = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")
NEEDS_CURL = pytest.mark.skipif(shutil.which("curl") is None, reason="needs curl on PATH to run the probe argv")
PROBE_FIELDS = ("_console_gate_probe_url", "_console_gate_probe_resolve", "_console_gate_probe_argv")
PROBE_BODY = ["--header", "Content-Type: application/json", "--data", "{}"]


@pytest.fixture
def live_gate(tmp_path, monkeypatch):
    """The real launch gate on loopback over TLS, with one caller token. Yields the port, the trust bundle the probe
    reads (the leaf itself: it is self-signed), and the gate's event log."""
    gate_tls._set_tokens(monkeypatch)
    key, cert = gate_tls._leaf_pair(1, "launch-gate-probe")
    leaf = tmp_path / "launch-gate"
    leaf.mkdir()
    cert_path, key_path = leaf / "cert.pem", leaf / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(gate_tls._key_pem(key))
    config_path = gate_tls._write_config(tmp_path, tls_cert=cert_path, tls_key=key_path)
    config = lg.load_config(str(config_path), dict(os.environ))
    events = []
    gate = lg.Gate(config, gate_tls.CannedSemaphore(), gate_tls.CannedOPA(), log=events.append)
    server = lg.GateServer(("127.0.0.1", 0), gate, str(cert_path), str(key_path))
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield {
            "port": server.server_address[1],
            "bundle": cert.public_bytes(serialization.Encoding.PEM),
            "events": events,
        }
    finally:
        server.shutdown()
        server.server_close()


def _probe_argv(tmp_path: Path, live: dict) -> list[str]:
    """The probe argv as deploy-console.yml renders it for a declared, non-local gate at the live server."""
    deploy = tmp_path / "deploy"
    (deploy / "certs").mkdir(parents=True)
    (deploy / "certs" / "step-ca-bundle.crt").write_bytes(live["bundle"])
    verify = _verify_vars()
    variables = {name: verify[name] for name in PROBE_FIELDS}
    variables.update(
        {
            "_deploy_dir": str(deploy),
            "console_launch_gate_url": f"https://localhost:{live['port']}",
            "console_launch_gate_ip": "127.0.0.1",
            "local_mode": False,
        }
    )
    template = tmp_path / "argv.j2"
    template.write_text("{{ _console_gate_probe_argv | to_json }}")
    return json.loads(_render(tmp_path, template, variables))


def _body_at(argv: list[str]) -> int:
    """Where the body flags start. They must be the exact four items the playbook adds, so a removed body fails here."""
    at = argv.index("--header")
    assert argv[at : at + 4] == PROBE_BODY, argv
    return at


def _curl(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)


@NEEDS_ANSIBLE
@NEEDS_CURL
def test_the_probe_argv_reads_the_gates_401_for_a_request_with_no_token(tmp_path, live_gate):
    argv = _probe_argv(tmp_path, live_gate)
    _body_at(argv)
    proc = _curl(argv)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "401", proc.stdout + proc.stderr
    # The 401 is the gate's own authentication refusal, logged as one launch event, not its framing.
    assert [(event["outcome"], event["error"]) for event in live_gate["events"]] == [(401, "unauthorized")]


@NEEDS_ANSIBLE
@NEEDS_CURL
def test_the_same_argv_without_its_body_is_refused_before_the_gate_authenticates(tmp_path, live_gate):
    argv = _probe_argv(tmp_path, live_gate)
    at = _body_at(argv)
    without_body = argv[:at] + argv[at + 4 :]
    proc = _curl(without_body)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "400", proc.stdout + proc.stderr
    assert live_gate["events"] == []
