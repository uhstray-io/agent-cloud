"""probe-agentgateway-client-tls.yml: how the gateway's TLS listener treats a client it must refuse
(inference-gateway-agentgateway tasks 6.1 and 6.4).

The playbook runs for real against a local TLS server that requires a client certificate signed by a
test CA and answers the way the gateway's SAN rule is measured to (agentgateway v1.5.0, 2026-10-02):
no client certificate fails the handshake, a certificate whose SAN is on no allowlist is 403 before
the API key is looked at. The same server also answers the other orderings, so each reading the
report can give is reached by a request the server actually received. Every certificate and key here
is synthetic and generated per run.
"""

import json
import re
import ssl
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import harness_sandbox
import playbook_yaml
import pytest
import yaml
from fake_http import DrainingHandler

REPO = playbook_yaml.REPO
PLAYBOOK = REPO / "platform/playbooks/probe-agentgateway-client-tls.yml"
PROBE = REPO / "platform/playbooks/tasks/agw-probe.yml"
BODY = "SYNTHETIC-RESPONSE-BODY-MUST-NOT-PRINT"
VALID_KEY = "synthetic-valid-key"
DRILL = "drill"  # the leaf under test: a client leaf whose SAN is on no allowlist
DEPLOY_SUBDIR = "platform/services/agentgateway/deployment"


# ── a synthetic CA, and leaves signed by it ───────────────────────────────────

def _openssl(*args, cwd):
    r = subprocess.run(["openssl", *args], cwd=cwd, capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr
    return r


def _ca(directory: Path, name: str) -> None:
    _openssl("req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-days", "2",
             "-keyout", f"{name}.key", "-out", f"{name}.crt", "-subj", f"/CN={name}",
             "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign",
             cwd=directory)


def _leaf(directory: Path, ca: str, name: str, san: str, usage: str, dest: Path) -> None:
    """A leaf at <dest>/current/{cert,key}.pem, signed by `ca`."""
    _openssl("req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
             "-keyout", f"{name}.key", "-out", f"{name}.csr", "-subj", f"/CN={name}", cwd=directory)
    (directory / f"{name}.ext").write_text(f"subjectAltName={san}\nextendedKeyUsage={usage}\n")
    _openssl("x509", "-req", "-in", f"{name}.csr", "-CA", f"{ca}.crt", "-CAkey", f"{ca}.key", "-CAcreateserial",
             "-days", "2", "-out", f"{name}.crt", "-extfile", f"{name}.ext", cwd=directory)
    (dest / "current").mkdir(parents=True)
    (dest / "current/cert.pem").write_text((directory / f"{name}.crt").read_text())
    (dest / "current/key.pem").write_text((directory / f"{name}.key").read_text())
    (dest / "current/key.pem").chmod(0o600)


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    d = tmp_path_factory.mktemp("pki")
    _ca(d, "ca")
    _ca(d, "other-ca")
    leaves = d / "leaves"
    _leaf(d, "ca", "server", "DNS:localhost", "serverAuth", leaves / "server")
    _leaf(d, "ca", "caddy", "DNS:caddy.example.internal", "clientAuth", leaves / "caddy")
    _leaf(d, "ca", DRILL, "DNS:drill.example.internal", "clientAuth", leaves / DRILL)
    return d


# ── the listener ──────────────────────────────────────────────────────────────

class Listener(ThreadingHTTPServer):
    """TLS with a required client certificate. `mode` picks the order of the two checks:
    san-first (the measured gateway), key-first, or admit (no SAN rule, no key check)."""

    daemon_threads = True
    request_queue_size = 64

    def __init__(self, pki: Path, mode: str, ca: str = "ca"):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.mode, self.allowed = mode, {"caddy.example.internal"}
        self.seen: list[dict] = []   # one entry per request that got through the handshake
        self.refused = 0             # handshakes the server ended
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        self.ctx.load_cert_chain(pki / "leaves/server/current/cert.pem", pki / "leaves/server/current/key.pem")
        self.ctx.verify_mode = ssl.CERT_REQUIRED
        self.ctx.load_verify_locations(pki / f"{ca}.crt")

    def get_request(self):
        sock, addr = self.socket.accept()
        return self.ctx.wrap_socket(sock, server_side=True, do_handshake_on_connect=False), addr

    def handle_error(self, request, client_address):
        pass


class _Handler(DrainingHandler):
    def setup(self):
        try:
            self.request.do_handshake()
        except (ssl.SSLError, OSError):
            self.server.refused += 1
            raise
        super().setup()

    def log_message(self, *_args):
        pass

    def do_GET(self):  # noqa: N802
        cert = self.request.getpeercert() or {}
        sans = [v for k, v in cert.get("subjectAltName", ()) if k == "DNS"]
        auth = self.headers.get("Authorization")
        self.server.seen.append({"path": self.path, "auth": auth, "sans": sans})
        allowed = bool(set(sans) & self.server.allowed)
        mode = self.server.mode
        key_ok = auth == f"Bearer {VALID_KEY}"
        if mode == "admit":
            status = 200
        elif mode == "san-first":
            status = 403 if not allowed else (200 if key_ok else 401)
        else:  # key-first
            status = 401 if not key_ok else (200 if allowed else 403)
        self.send_response(status)
        self.send_header("Content-Length", str(len(BODY)))
        self.end_headers()
        self.wfile.write(BODY.encode())


@pytest.fixture
def listener(pki):
    started = []

    def start(mode="san-first", ca="ca"):
        server = Listener(pki, mode, ca)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        started.append(server)
        return server

    yield start
    for s in started:
        s.shutdown()
        s.server_close()


# ── running the playbook ──────────────────────────────────────────────────────

def _leaves(pki: Path, **extra) -> list[dict]:
    leaves = [
        {"name": "caddy", "host": "gw", "profile": "client", "dir": str(pki / "leaves/caddy"),
         "sans": ["caddy.example.internal"]},
        {"name": DRILL, "host": "gw", "profile": "client", "dir": str(pki / "leaves" / DRILL),
         "sans": ["drill.example.internal"]},
        {"name": "agw-server", "host": "gw", "profile": "server", "dir": str(pki / "leaves/server"),
         "sans": ["localhost"]},
    ]
    return [{**leaf, **extra.get(leaf["name"], {})} for leaf in leaves]


def _bundle(tmp: Path, pki: Path, ca: str = "ca") -> Path:
    mono = tmp / "mono"
    certs = mono / DEPLOY_SUBDIR / "certs"
    certs.mkdir(parents=True, exist_ok=True)
    (certs / "step-ca-bundle.crt").write_text((pki / f"{ca}.crt").read_text())
    return mono


def _run(tmp: Path, pki: Path, base: str | None, *, host=None, extra=None, check=False, bundle_ca="ca"):
    mono = _bundle(tmp, pki, bundle_ca)
    host_vars = {"ansible_connection": "local", "ansible_python_interpreter": sys.executable,
                 "agw_listener_tls": True, "internal_leaves": _leaves(pki), "local_monorepo_dir": str(mono),
                 **({"agw_verify_base_url": base} if base else {}), **(host or {})}
    inv = {"all": {"children": {"agentgateway_svc": {"hosts": {"gw": host_vars}}}}}
    (tmp / "inv.yml").write_text(yaml.safe_dump(inv))
    extra_vars = {"probe_leaf": DRILL, **(extra or {})}
    cmd = ["ansible-playbook", "-v", "-i", str(tmp / "inv.yml"), str(PLAYBOOK), "-e", json.dumps(extra_vars),
           *(["--check"] if check else [])]
    env = {**harness_sandbox.env_for(tmp), "ANSIBLE_STDOUT_CALLBACK": "default"}
    r = harness_sandbox.run(cmd, tmp, cwd=REPO, env=env, timeout=300)
    return r.returncode, r.stdout + r.stderr


def _report(out: str) -> dict:
    """The three classes and the reading, from the debug report."""
    lines = {int(m.group(1)): m.group(2) for m in re.finditer(r'"(\d)\. [^:"]*(?:\([^)]*\))?: ([a-z0-9-]+)', out)}
    reading = re.search(r'"reading: ([^"]*)"', out)
    return {"classes": lines, "reading": reading.group(1) if reading else None}


def _no_leak(out: str):
    assert BODY not in out
    assert "Bearer" not in out
    assert "drill-invalid-key" not in out
    assert VALID_KEY not in out


# ── the three probes ──────────────────────────────────────────────────────────

def test_a_leaf_on_no_allowlist_is_refused_with_403_before_the_key_is_looked_at(tmp_path, pki, listener):
    gw = listener("san-first")
    rc, out = _run(tmp_path, pki, f"https://localhost:{gw.server_port}")
    assert rc == 0, out
    rep = _report(out)
    assert rep["classes"][1] in ("tls-handshake-refused", "connection-closed", "http-400"), out
    assert rep["classes"][2] == "http-403" and rep["classes"][3] == "http-403", out
    assert "the SAN rule ran before the API-key check" in rep["reading"] and "HTTP 403" in rep["reading"], out
    # what the server received: no request without a certificate, then the leaf with no Authorization,
    # then the leaf with the literal invalid key, and nothing else
    assert [(s["sans"], s["auth"]) for s in gw.seen] == [
        (["drill.example.internal"], None),
        (["drill.example.internal"], "Bearer drill-invalid-key"),
    ]
    assert gw.refused >= 1
    _no_leak(out)


def test_the_key_check_reached_first_is_reported_as_that(tmp_path, pki, listener):
    gw = listener("key-first")
    rc, out = _run(tmp_path, pki, f"https://localhost:{gw.server_port}")
    assert rc == 0, out
    rep = _report(out)
    assert rep["classes"][2] == "http-401" and rep["classes"][3] == "http-401", out
    assert "the API-key check was reached first" in rep["reading"], out
    _no_leak(out)


def test_a_gateway_that_admits_a_client_it_must_refuse_fails_the_run(tmp_path, pki, listener):
    # No SAN rule and no key check: the leaf is served. The 2xx is the finding, not a pass.
    gw = listener("admit")
    rc, out = _run(tmp_path, pki, f"https://localhost:{gw.server_port}")
    assert rc != 0 and "The gateway ADMITTED probe(s) p2, p3" in out, out
    assert _report(out)["classes"][2] == "http-200"
    _no_leak(out)


def test_a_server_certificate_the_probe_cannot_verify_is_inconclusive_not_a_refusal(tmp_path, pki, listener):
    # The bundle on the VM holds another CA, so every probe fails on the server's certificate before
    # the gateway's policy is reached: three transport failures that would otherwise read as refusals.
    gw = listener("san-first")
    rc, out = _run(tmp_path, pki, f"https://localhost:{gw.server_port}", bundle_ca="other-ca")
    assert rc != 0 and "inconclusive" in out, out
    assert set(_report(out)["classes"].values()) == {"server-certificate-not-trusted"}, out
    assert gw.seen == []


def test_a_gateway_that_is_not_listening_is_inconclusive(tmp_path, pki, listener):
    gw = listener("san-first")
    port = gw.server_port
    gw.shutdown()
    gw.server_close()
    rc, out = _run(tmp_path, pki, f"https://localhost:{port}")
    assert rc != 0 and "inconclusive" in out, out
    assert set(_report(out)["classes"].values()) == {"connection-refused"}, out


def test_a_dry_run_sends_nothing_and_still_checks_the_leaf(tmp_path, pki, listener):
    gw = listener("san-first")
    rc, out = _run(tmp_path, pki, f"https://localhost:{gw.server_port}", check=True)
    assert rc == 0 and "Nothing was sent" in out, out
    assert gw.seen == [] and gw.refused == 0
    # the refusals are read-only, so they hold under --check too
    rc, out = _run(tmp_path, pki, f"https://localhost:{gw.server_port}", check=True, extra={"probe_leaf": "nobody"})
    assert rc != 0 and "must be declared once in internal_leaves" in out, out


# ── what the probe will not run against ───────────────────────────────────────

@pytest.mark.parametrize("host, extra, message", [
    pytest.param({"internal_leaves": []}, None, "must be declared once in internal_leaves", id="undeclared"),
    pytest.param(None, {"probe_leaf": "agw-server"}, "client-profile leaf", id="server-profile"),
    pytest.param({"agw_client_cert_allowlist": ["caddy", DRILL]}, None, "is admitted by the SAN rule",
                 id="allowlisted"),
    pytest.param(None, {"probe_leaf": "caddy"}, "is admitted by the SAN rule", id="allowlisted-by-default"),
    pytest.param({"agw_listener_tls": False}, None, "without TLS there is no client certificate", id="tls-off"),
    pytest.param(None, {"probe_leaf": ""}, "Pass -e probe_leaf", id="no-leaf"),
    pytest.param(None, {"probe_leaf": "a b"}, "Pass -e probe_leaf", id="unsafe-name"),
])
def test_the_probe_refuses_before_any_request(tmp_path, pki, listener, host, extra, message):
    gw = listener("admit")
    kw = {"host": host, "extra": extra}
    rc, out = _run(tmp_path, pki, f"https://localhost:{gw.server_port}", **kw)
    assert rc != 0 and message in out, out
    assert gw.seen == [] and gw.refused == 0


def test_a_leaf_sharing_a_san_with_an_allowlisted_leaf_is_refused(tmp_path, pki, listener):
    gw = listener("admit")
    leaves = _leaves(pki, **{DRILL: {"sans": ["drill.example.internal", "caddy.example.internal"]}})
    rc, out = _run(tmp_path, pki, f"https://localhost:{gw.server_port}", host={"internal_leaves": leaves})
    assert rc != 0 and "is admitted by the SAN rule" in out, out
    assert gw.seen == []


def test_a_leaf_declared_for_another_host_is_refused(tmp_path, pki, listener):
    gw = listener("admit")
    leaves = _leaves(pki, **{DRILL: {"host": "elsewhere"}})
    rc, out = _run(tmp_path, pki, f"https://localhost:{gw.server_port}", host={"internal_leaves": leaves})
    assert rc != 0 and "must be declared once in internal_leaves" in out, out
    assert gw.seen == []


def test_a_leaf_that_is_not_issued_yet_is_refused(tmp_path, pki, listener):
    gw = listener("admit")
    leaves = _leaves(pki, **{DRILL: {"dir": str(tmp_path / "never-issued")}})
    rc, out = _run(tmp_path, pki, f"https://localhost:{gw.server_port}", host={"internal_leaves": leaves})
    assert rc != 0 and "Issue the leaf first" in out and "never-issued/current/cert.pem" in out, out
    assert gw.seen == []


# ── the shared probe path ─────────────────────────────────────────────────────

def _probe_include(tmp: Path, pki: Path, port: int, no_cert: bool | None):
    """tasks/agw-probe.yml on its own, the way a caller includes it."""
    mono = _bundle(tmp, pki)
    vars_ = {"_agwp_base": f"https://localhost:{port}", "_agwp_path": "/v1/models", "_agwp_status": [200, 401, 403],
             "_agwp_leaf_dir": str(pki / "leaves" / DRILL),
             "_agwp_ca": str(mono / DEPLOY_SUBDIR / "certs/step-ca-bundle.crt"),
             **({} if no_cert is None else {"_agwp_no_client_cert": no_cert})}
    (tmp / "inv.yml").write_text(yaml.safe_dump({"all": {"hosts": {"gw": {
        "ansible_connection": "local", "ansible_python_interpreter": sys.executable, "agw_listener_tls": True}}}}))
    (tmp / "play.yml").write_text(yaml.safe_dump([{"hosts": "all", "gather_facts": False, "tasks": [
        {"ansible.builtin.include_tasks": str(PROBE), "vars": vars_},
        {"ansible.builtin.debug": {"msg": "status={{ _agwp_out.status }}"}}]}]))
    r = harness_sandbox.run(["ansible-playbook", "-i", str(tmp / "inv.yml"), str(tmp / "play.yml")], tmp, cwd=REPO,
                            env={**harness_sandbox.env_for(tmp), "ANSIBLE_STDOUT_CALLBACK": "default"}, timeout=120)
    return r.returncode, r.stdout + r.stderr


@pytest.mark.parametrize("no_cert, presented", [
    pytest.param(None, True, id="default-presents-the-leaf"),
    pytest.param(False, True, id="false-presents-the-leaf"),
    pytest.param(True, False, id="true-presents-nothing"),
])
def test_the_shared_probe_presents_the_leaf_unless_asked_not_to(tmp_path, pki, listener, no_cert, presented):
    gw = listener("san-first")
    rc, out = _probe_include(tmp_path, pki, gw.server_port, no_cert)
    assert rc == 0, out
    if presented:
        assert [s["sans"] for s in gw.seen] == [["drill.example.internal"]] and "status=403" in out, out
    else:
        # the server leaf was still verified (the handshake began), and the client had no certificate
        assert gw.seen == [] and gw.refused == 1 and "status=403" not in out, out


# ── wiring ────────────────────────────────────────────────────────────────────

def _templates():
    return yaml.safe_load((REPO / "platform/semaphore/templates.yml").read_text())["templates"]


def test_the_semaphore_template_is_dev_bound_with_one_required_survey_var():
    t = next(t for t in _templates() if t["name"] == "Probe agentgateway Client TLS (Dev)")
    assert t["playbook"] == "platform/playbooks/probe-agentgateway-client-tls.yml"
    assert t["repository"] == "agent-cloud dev"
    (var,) = t["survey_vars"]
    assert var["name"] == "probe_leaf" and var["required"] is True and "default_value" not in var


def test_the_playbook_is_launchable_and_guarded_first():
    doc = playbook_yaml.load(PLAYBOOK)
    assert doc[0].get("ansible.builtin.import_playbook") == "refuse-internal-extra-vars.yml"
    assert doc[1].get("ansible.builtin.import_playbook") == "preflight-target-group.yml"
    assert doc[2]["hosts"] == "agentgateway_svc"


def test_agents_md_and_the_service_doc_list_the_workflow():
    agents = (REPO / "AGENTS.md").read_text()
    assert "| Probe agentgateway Client TLS (Dev) | `probe-agentgateway-client-tls.yml` |" in agents
    doc = (REPO / "platform/services/agentgateway/context/architecture.md").read_text()
    assert "probe-agentgateway-client-tls.yml" in doc
