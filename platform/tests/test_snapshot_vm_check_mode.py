"""snapshot-vm.yml under --check and for real, against a synthetic Proxmox over TLS.

A production dry run failed at "Record the snapshot task UPID": the create request is a write
that cannot simulate, so it is skipped under --check, and the next task read its missing result.
Under --check the play now reads the VM, writes nothing, and reports the snapshot it would take
(plan/architecture/08-ansible-automation-standards.md, the three check-mode classes). The real
run is exercised too, so the fix cannot have broken create, wait and validate. Requires
ansible-playbook.
"""

import datetime
import json
import os
import shutil
import ssl
import subprocess
import threading
from http.server import ThreadingHTTPServer

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fake_http import DrainingHandler
from playbook_yaml import REPO

PLAYBOOK = REPO / "platform/playbooks/snapshot-vm.yml"
VM = "/api2/json/nodes/n1/qemu/100"
UPID = "UPID:n1:0001:snapshot"
SNAP = "preup-test"

pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")


def _self_signed(tmp_path):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "pve.test")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(hours=1)).sign(key, hashes.SHA256()))
    cert_f, key_f = tmp_path / "pve.crt", tmp_path / "pve.key"
    cert_f.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_f.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()))
    return cert_f, key_f


class FakeProxmoxTLS:
    def __init__(self, tmp_path):
        self.seen, self.created = [], False
        fake = self

        class Handler(DrainingHandler):
            def log_message(self, *_args):
                pass

            def _reply(self, data, status=200):
                body = json.dumps({"data": data}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                fake.seen.append(("GET", self.path))
                if self.path == VM + "/status/current":
                    return self._reply({"name": "vm", "status": "running"})
                if self.path == f"/api2/json/nodes/n1/tasks/{UPID}/status":
                    return self._reply({"status": "stopped", "exitstatus": "OK"})
                if self.path == VM + "/snapshot":
                    return self._reply([{"name": SNAP}] if fake.created else [])
                return self._reply(None, 404)

            def do_POST(self):  # noqa: N802
                fake.seen.append(("POST", self.path))
                if self.path == VM + "/snapshot":
                    fake.created = True
                    return self._reply(UPID)
                return self._reply(None, 404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(*_self_signed(tmp_path))
        self.server.socket = ctx.wrap_socket(self.server.socket, server_side=True)
        self.url = f"https://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _snapshot(tmp_path, check):
    fake = FakeProxmoxTLS(tmp_path)
    extra = {"pve_api_host": fake.url, "pve_node": "n1", "pve_vmid": 100, "snapshot_name": SNAP,
             "pve_token_id": "automation@pve!fixture"}
    env = {k: v for k, v in os.environ.items() if k not in ("ANSIBLE_CONFIG", "PVE_TOKEN_SECRET")}
    env.update(ANSIBLE_NOCOLOR="1", PVE_TOKEN_SECRET="synthetic-pve-secret")
    try:
        done = subprocess.run(["ansible-playbook", "-i", "localhost,", str(PLAYBOOK), "-e", json.dumps(extra),
                               *(["--check"] if check else [])],
                              cwd=REPO, env=env, text=True, capture_output=True, stdin=subprocess.DEVNULL)
    finally:
        fake.close()
    assert "synthetic-pve-secret" not in done.stdout + done.stderr
    return done, fake


def test_a_dry_run_reads_the_vm_writes_nothing_and_reports_the_snapshot(tmp_path):
    done, fake = _snapshot(tmp_path, check=True)
    assert done.returncode == 0, done.stdout[-2500:]
    assert fake.seen == [("GET", VM + "/status/current")]
    assert f"Dry run: would snapshot VM 100 on node n1 as '{SNAP}'" in done.stdout


def test_a_real_run_creates_waits_and_validates(tmp_path):
    done, fake = _snapshot(tmp_path, check=False)
    assert done.returncode == 0, done.stdout[-2500:]
    assert ("POST", VM + "/snapshot") in fake.seen and fake.seen[-1] == ("GET", VM + "/snapshot")
    assert "Dry run: would snapshot" not in done.stdout
    assert f"Snapshot '{SNAP}' created + validated" in done.stdout
