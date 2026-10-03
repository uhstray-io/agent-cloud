"""renew-internal-certs.yml, run for real (openspec production-internal-ca tasks 6.1 and 6.2).

Every inventory host (CA, gateway, Caddy, o11y) uses the local connection. The playbook's own
issuance path runs: keys and requests are made by openssl on the "consumer", a stub container
engine signs them with a test intermediate (cryptography) and records reloads and restarts, and
the placement and the `current` swap are the real ones. Around it, in this process:

- the issuer-password read from OpenBao replaced by a fixed synthetic value (the issuance
  task's own tests own that read; the test suites carry no OpenBao client library): the
  playbook and its task files run from a copy in the test directory, with that one task
  swapped, exactly as test_issue_internal_leaf.py lifts it;
- a TLS "gateway" that requires a client certificate from the test CA, admits only allowlisted
  SANs (403 otherwise), answers a keyless request 401, and serves its server leaf from
  `current/` on every connection (or, "frozen", the leaf it started with);
- a TLS "Caddy" on its HTTPS port answering two routes (the inference route and the gateway
  UI's) each with a chosen status, recording what it was asked;
- a Loki that records every push.

The gateway's server-leaf name is resolved to loopback for the probe path's `uri` call by a
`sitecustomize` on PYTHONPATH (the deploy's interim hosts line does this in production). Runs
through harness_sandbox, so no write leaves the test directory. All values are synthetic.
"""

import datetime
import json
import os
import shutil
import socket
import ssl
import sys
import threading
from contextlib import ExitStack, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import harness_sandbox
import playbook_yaml
import pytest
import seed_harness
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

PLAYBOOK = playbook_yaml.REPO / "platform/playbooks/renew-internal-certs.yml"
TEMPLATES = playbook_yaml.REPO / "platform/semaphore/templates.yml"
ZONE = "dc1.example.internal"
ROUTE = "inference.example.test"
ADMIN = "admin.inference.example.test"
ISSUER_PW = {"server": "synthetic-issuer-server-pw", "client": "synthetic-issuer-client-pw"}
NOW = datetime.datetime.now(datetime.UTC)
DAY = datetime.timedelta(days=1)
HIDDEN_MARKER = "synthetic_hidden_failure_marker"
FRESH = (NOW - DAY, NOW + 29 * DAY)      # 29 of 30 days left: outside the window
DUE = (NOW - 25 * DAY, NOW + 5 * DAY)    # 5 of 30 days left: inside the last third


def _copy_playbook(tmp: Path, password_fails: bool = False) -> Path:
    """The playbook and its task files, with the issuer-password read made a fixed value (or,
    `password_fails`, a hidden failure whose message names HIDDEN_MARKER)."""
    dest = tmp / "pb"
    shutil.copytree(playbook_yaml.REPO / "platform/playbooks/tasks", dest / "tasks")
    shutil.copy(PLAYBOOK, dest / PLAYBOOK.name)
    issue = dest / "tasks/issue-internal-leaf.yml"
    tasks = yaml.safe_load(issue.read_text())
    swapped = 0
    for task in tasks:
        for i, inner in enumerate(task.get("block", [])):
            if inner["name"] == "Read the issuing provisioner's password":
                task["block"][i] = {"name": inner["name"], "no_log": True, "ansible.builtin.set_fact": {
                    "_leaf_issuer_pw": ("{{ " + HIDDEN_MARKER + " }}") if password_fails
                    else "synthetic-issuer-{{ _leaf.profile }}-pw"}}
                swapped += 1
    assert swapped == 1, "the issuer-password task was not found to swap"
    issue.write_text(yaml.safe_dump(tasks, sort_keys=False))
    return dest / PLAYBOOK.name


# ── A test CA ──────────────────────────────────────────────────────────────────

def _name(cn):
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _ca(tmp: Path) -> dict:
    root_key, int_key = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    def build(subject, issuer, key, signer, path_len):
        return (x509.CertificateBuilder().subject_name(_name(subject)).issuer_name(_name(issuer))
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(NOW - 30 * DAY).not_valid_after(NOW + 365 * DAY)
                .add_extension(x509.BasicConstraints(ca=True, path_length=path_len), critical=True)
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(signer.public_key()), critical=False)
                .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                             content_commitment=False, key_encipherment=False,
                                             data_encipherment=False, key_agreement=False,
                                             encipher_only=False, decipher_only=False), critical=True)
                .sign(signer, hashes.SHA256()))
    root = build("Test Root", "Test Root", root_key, root_key, 1)
    inter = build("Test Intermediate", "Test Root", int_key, root_key, 0)
    d = tmp / "ca"
    d.mkdir()
    pem = lambda c: c.public_bytes(serialization.Encoding.PEM).decode()  # noqa: E731
    (d / "int.crt").write_text(pem(inter))
    (d / "int.key").write_bytes(int_key.private_bytes(serialization.Encoding.PEM,
                                                      serialization.PrivateFormat.PKCS8,
                                                      serialization.NoEncryption()))
    (d / "bundle.crt").write_text(pem(root) + pem(inter))
    return {"dir": d, "inter": inter, "int_key": int_key, "bundle": pem(root) + pem(inter)}


def _sign(ca, key_or_pub, sans, profile, validity):
    eku = ExtendedKeyUsageOID.SERVER_AUTH if profile == "server" else ExtendedKeyUsageOID.CLIENT_AUTH
    pub = key_or_pub.public_key() if hasattr(key_or_pub, "public_key") else key_or_pub
    return (x509.CertificateBuilder().subject_name(_name(sans[0])).issuer_name(ca["inter"].subject)
            .public_key(pub).serial_number(x509.random_serial_number())
            .not_valid_before(validity[0]).not_valid_after(validity[1])
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(s) for s in sans]), critical=False)
            .add_extension(x509.ExtendedKeyUsage([eku]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(pub), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca["int_key"].public_key()),
                           critical=False)
            .sign(ca["int_key"], hashes.SHA256()))


def _place(ca, leaf: dict, validity) -> str:
    """A leaf as issuance leaves it: <dir>/<SERIAL>/{cert,key}.pem, `current` -> SERIAL, and the
    bundle beside the leaf directory. Returns the serial."""
    key = ec.generate_private_key(ec.SECP256R1())
    cert = _sign(ca, key, leaf["sans"], leaf["profile"], validity)
    d = Path(leaf["dir"])
    serial = format(cert.serial_number, "X")
    serial = serial if len(serial) % 2 == 0 else "0" + serial
    (d / serial).mkdir(parents=True)
    (d / serial / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (d / serial / "key.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                                           serialization.PrivateFormat.PKCS8,
                                                           serialization.NoEncryption()))
    os.chmod(d / serial / "key.pem", 0o600)
    (d / "current").symlink_to(serial)
    (d.parent / "step-ca-bundle.crt").write_text(ca["bundle"])
    return serial


def _current_serial(leaf) -> int:
    pem = (Path(leaf["dir"]) / "current" / "cert.pem").read_bytes()
    return x509.load_pem_x509_certificate(pem).serial_number


# ── The stub engine: signing, the intermediate, reloads and restarts ───────────

STUB = """#!{python}
import datetime, json, sys
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.x509.oid import ExtendedKeyUsageOID
ca = {ca!r}
args = sys.argv[1:]
log = open({log!r}, "a")
if args[:3] == ["exec", "-i", "step-ca"]:
    pw, csr = sys.stdin.read().split("\\n", 1)
    profile = args[-1].split("-", 1)[1]
    req = x509.load_pem_x509_csr(csr.encode())
    inter = x509.load_pem_x509_certificate(open(ca + "/int.crt", "rb").read())
    key = serialization.load_pem_private_key(open(ca + "/int.key", "rb").read(), None)
    eku = ExtendedKeyUsageOID.SERVER_AUTH if profile == "server" else ExtendedKeyUsageOID.CLIENT_AUTH
    now = datetime.datetime.now(datetime.timezone.utc)
    san = req.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    cert = (x509.CertificateBuilder().subject_name(req.subject).issuer_name(inter.subject)
            .public_key(req.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=1)).not_valid_after(now + datetime.timedelta(days=30))
            .add_extension(san, critical=False).add_extension(x509.ExtendedKeyUsage([eku]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(req.public_key()), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
    log.write(json.dumps({{"sign": profile, "pw": pw, "sans": [n.value for n in san]}}) + "\\n")
    sys.stdout.write(cert.public_bytes(serialization.Encoding.PEM).decode())
elif args[:3] == ["exec", "step-ca", "cat"]:
    sys.stdout.write(open(ca + "/int.crt").read())
elif args[:1] == ["restart"] or (args[:1] == ["exec"] and "reload" in args):
    log.write(json.dumps({{"reload": args}}) + "\\n")
else:
    log.write(json.dumps({{"unexpected": args}}) + "\\n")
    sys.exit(2)
"""


# ── In-process servers ─────────────────────────────────────────────────────────

@contextmanager
def _thread(server):
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


class _TLSServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, context_for, handler):
        self.context_for = context_for
        super().__init__(("127.0.0.1", 0), handler)

    def get_request(self):
        sock, addr = super().get_request()
        sock.settimeout(10)
        try:
            return self.context_for().wrap_socket(sock, server_side=True), addr
        except (ssl.SSLError, OSError):
            sock.close()
            raise


def _gateway(ca, server_leaf, allowed, scratch: Path, frozen=False):
    d = Path(server_leaf["dir"])
    held = {}
    def load():
        cur = d / "current"
        held["files"] = ((cur / "cert.pem").read_bytes(), (cur / "key.pem").read_bytes())
    if frozen:
        load()  # the leaf it started with, whatever is swapped in later
    def context():
        if not frozen:
            load()
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cert_f, key_f = scratch / ".gw-cert.pem", scratch / ".gw-key.pem"
        cert_f.write_bytes(held["files"][0])
        key_f.write_bytes(held["files"][1])
        ctx.load_cert_chain(cert_f, key_f)
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.load_verify_locations(cadata=ca["bundle"])
        return ctx

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a):
            pass

        def do_GET(self):  # noqa: N802
            sans = [v for k, v in self.connection.getpeercert().get("subjectAltName", ()) if k == "DNS"]
            status = 401 if set(sans) & set(allowed) else 403
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

    return _TLSServer(context, Handler)


def _caddy(ca, tmp: Path, status: dict, asked: list):
    key = ec.generate_private_key(ec.SECP256R1())
    cert = _sign(ca, key, [ROUTE, ADMIN], "server", (NOW - DAY, NOW + 30 * DAY))
    cert_f, key_f = tmp / ".caddy-cert.pem", tmp / ".caddy-key.pem"
    cert_f.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_f.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()))
    def context():
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert_f, key_f)
        return ctx

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a):
            pass

        def do_GET(self):  # noqa: N802
            host = self.headers.get("Host")
            asked.append((host, self.path))
            ok = host in status and self.headers.get("Authorization", "").startswith("Bearer ")
            self.send_response(status[host] if ok else 400)
            self.send_header("Content-Length", "0")
            self.end_headers()

    return _TLSServer(context, Handler)


def _loki(pushes: list):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a):
            pass

        def do_POST(self):  # noqa: N802
            pushes.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(204)
            self.end_headers()

    return ThreadingHTTPServer(("127.0.0.1", 0), Handler)


# Shadowing the interpreter's own sitecustomize would drop what it does (Homebrew's adds its
# site-packages), so the next one on sys.path is run first.
SITECUSTOMIZE = f"""import os, socket, sys
_here = os.path.dirname(os.path.abspath(__file__))
for _p in sys.path:
    _f = os.path.join(_p or ".", "sitecustomize.py")
    if os.path.abspath(_p or ".") != _here and os.path.isfile(_f):
        exec(compile(open(_f).read(), _f, "exec"), {{"__file__": _f, "__name__": "sitecustomize"}})
        break
_gai = socket.getaddrinfo
def getaddrinfo(host, *a, **k):
    if isinstance(host, str) and host.endswith(".{ZONE}"):
        host = "127.0.0.1"
    return _gai(host, *a, **k)
socket.getaddrinfo = getaddrinfo
"""


# ── The world: declared leaves, placed certificates, inventory, a run ──────────

def _leaves(tmp: Path) -> dict:
    return {
        "agw-server": {"name": "agw-server", "host": "gw", "dir": str(tmp / "gw/certs/agw-server"),
                       "profile": "server", "sans": [f"vm01.gateway.{ZONE}", f"gateway.{ZONE}"], "reload": "restart"},
        "agw-verifier": {"name": "agw-verifier", "host": "gw", "dir": str(tmp / "gw/certs/agw-verifier"),
                         "profile": "client", "sans": [f"verifier.gateway.{ZONE}"], "reload": "none"},
        "caddy": {"name": "caddy", "host": "caddy", "dir": str(tmp / "caddy/certs/caddy"),
                  "profile": "client", "sans": [f"caddy.{ZONE}"], "reload": "caddy reload --force"},
    }


class World:
    def __init__(self, tmp: Path, password_fails: bool = False):
        self.tmp = tmp
        self.ca = _ca(tmp)
        self.leaves = _leaves(tmp)
        self.log = tmp / "engine.log"
        self.engine = tmp / "engine"
        self.engine.write_text(STUB.format(python=sys.executable, ca=str(self.ca["dir"]), log=str(self.log)))
        self.engine.chmod(0o755)
        (tmp / "site").mkdir()
        (tmp / "site/sitecustomize.py").write_text(SITECUSTOMIZE)
        self.pushes: list = []
        self.caddy_status = {ROUTE: 401, ADMIN: 302}
        self.caddy_asked: list = []
        self.allowed = [f"verifier.gateway.{ZONE}", f"caddy.{ZONE}"]
        self.playbook = _copy_playbook(tmp, password_fails)

    def place(self, **validity):
        """Place each declared leaf with the validity named for it (default FRESH)."""
        for name, leaf in self.leaves.items():
            _place(self.ca, leaf, validity.get(name.replace("-", "_"), FRESH))

    def calls(self) -> list:
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def run(self, *, check=False, extra=None, frozen=False, host_over=None, leaves=None):
        host = {"ansible_connection": "local", "ansible_python_interpreter": sys.executable,
                "container_engine": str(self.engine)}
        with ExitStack() as stack:
            gw = stack.enter_context(_thread(_gateway(self.ca, self.leaves["agw-server"], self.allowed,
                                                      self.tmp, frozen)))
            caddy = stack.enter_context(_thread(_caddy(self.ca, self.tmp, self.caddy_status, self.caddy_asked)))
            loki = stack.enter_context(_thread(_loki(self.pushes)))
            hosts = {
                "step_ca_svc": {"ca": dict(host)},
                "agentgateway_svc": {"gw": {**host, "agw_listener_tls": True, "agw_bind": "127.0.0.1",
                                            "agw_port": str(gw.server_port), "agw_ui_enabled": False}},
                "caddy_svc": {"caddy": {**host, "inference_route_address": ROUTE,
                                        "caddy_https_port": str(caddy.server_port)}},
                "o11y_svc": {"o11y": {**host, "o11y_loki_bind": "127.0.0.1",
                                      "o11y_loki_port": str(loki.server_port)}},
            }
            for group, over in (host_over or {}).items():
                for name, value in over.items():
                    if value is None:
                        hosts[group].pop(name, None)
                    else:
                        hosts[group][name] = {**hosts[group].get(name, host), **value}
            inv = {"all": {"vars": {"openbao_addr": "https://openbao.example.test", "dns_site": "dc1",
                                    "dns_zone": "example.internal",
                                    "internal_leaves": list(self.leaves.values()) if leaves is None else leaves},
                           "children": {g: {"hosts": h} for g, h in hosts.items()}}}
            (self.tmp / "inv.yml").write_text(yaml.safe_dump(inv))
            args = {**seed_harness.ROLE, "renew_proof_retries": 2, "renew_proof_delay": 1, **(extra or {})}
            cmd = ["ansible-playbook", "-i", str(self.tmp / "inv.yml"), str(self.playbook), "-e", json.dumps(args),
                   *(["--check"] if check else [])]
            env = harness_sandbox.env_for(self.tmp)
            env.update(ANSIBLE_STDOUT_CALLBACK="default", PYTHONPATH=str(self.tmp / "site"))
            r = harness_sandbox.run(cmd, self.tmp, cwd=playbook_yaml.REPO, env=env, timeout=300)
        out = r.stdout + r.stderr
        for value in (*ISSUER_PW.values(), *seed_harness.NEVER_PRINTED, "PRIVATE KEY"):
            assert value not in out, f"{value[:14]}... reached the output"
        for leaf in self.leaves.values():
            for key in Path(leaf["dir"]).glob("*/key.pem"):
                body = key.read_text().splitlines()[1]
                assert body not in out, f"{leaf['name']}'s key reached the output"
        return r.returncode, out

    def serials(self) -> dict:
        return {n: _current_serial(leaf) for n, leaf in self.leaves.items()}

    def reloads(self) -> list:
        return [c["reload"] for c in self.calls() if "reload" in c]

    def signed(self) -> list:
        return [c for c in self.calls() if "sign" in c]

    def streams(self) -> list:
        return [s for _path, body in self.pushes for s in body["streams"]]


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


# ── Threshold and the per-host decision ────────────────────────────────────────

def test_fresh_leaves_are_left_alone(world):
    world.place()
    before = world.serials()
    rc, out = world.run()
    assert rc == 0, out
    assert world.serials() == before
    assert world.signed() == [] and world.reloads() == []
    assert out.count("left alone") >= 3


def test_one_leaf_inside_its_window_renews_every_leaf_on_its_host_and_reloads_once(world):
    world.place(agw_verifier=DUE)
    before = world.serials()
    rc, out = world.run()
    assert rc == 0, out
    after = world.serials()
    assert after["agw-server"] != before["agw-server"] and after["agw-verifier"] != before["agw-verifier"]
    assert after["caddy"] == before["caddy"], "a host with no leaf in its window was renewed"
    assert sorted(c["sign"] for c in world.signed()) == ["client", "server"]
    # Each request was signed under its own profile's issuer password.
    assert all(c["pw"] == ISSUER_PW[c["sign"]] for c in world.signed())
    assert world.reloads() == [[ "restart", "agentgateway"]]


def test_a_missing_certificate_puts_its_host_in_the_window(world):
    world.place()
    for leaf in ("agw-server", "agw-verifier"):
        os.unlink(Path(world.leaves[leaf]["dir"]) / "current")
    rc, out = world.run(extra={"renew_proof_retries": 4})
    # The fresh gateway in this harness serves `current`, which is gone until issuance: it
    # proves the new leaf only after the swap, which is what the run waits for.
    assert rc == 0, out
    assert len(world.signed()) == 2


def test_the_caddy_host_reloads_once_for_two_leaves(world):
    second = {**world.leaves["caddy"], "name": "caddy-two", "dir": str(world.tmp / "caddy/certs/caddy-two"),
              "sans": [f"two.caddy.{ZONE}"]}
    world.leaves["caddy-two"] = second
    world.place(caddy=DUE)
    rc, out = world.run()
    assert rc == 0, out
    assert len(world.signed()) == 2
    reloads = world.reloads()
    assert len(reloads) == 1 and reloads[0][-3:] == ["--config", "/etc/caddy/Caddyfile", "--force"], reloads


def test_the_drill_threshold_renews_every_leaf(world):
    world.place()
    before = world.serials()
    rc, out = world.run(extra={"renew_threshold": "1"})
    assert rc == 0, out
    after = world.serials()
    assert all(after[n] != before[n] for n in before)
    assert sorted(r[0] for r in world.reloads()) == ["exec", "restart"]


def test_a_leaf_in_use_is_proven_with_its_new_serial(world):
    world.place(agw_server=DUE)
    rc, out = world.run()
    assert rc == 0, out
    [run] = [s for s in world.streams() if s["stream"]["kind"] == "run"]
    body = json.loads(run["values"][0][1])
    assert sorted(body["renewed"]) == ["agw-server", "agw-verifier"]


# ── Proof failures fail the run, and still report ──────────────────────────────

def test_a_gateway_still_serving_the_old_leaf_fails_the_run(world):
    world.place(agw_server=DUE)
    rc, out = world.run(frozen=True, extra={"renew_proof_retries": 1})
    assert rc != 0
    assert "Prove the gateway serves the new server leaf" in out
    runs = [s["stream"] for s in world.streams() if s["stream"]["kind"] == "run"]
    assert runs == [{"job": "renew-internal-certs", "kind": "run", "status": "failure"}]


def test_a_per_call_leaf_the_gateway_refuses_fails_the_run(world):
    world.allowed = [f"caddy.{ZONE}"]
    world.place(agw_verifier=DUE)
    rc, out = world.run()
    assert rc != 0
    assert "Refuse a per-call client leaf the gateway did not admit" in out and "403" in out


def test_a_route_that_does_not_reach_the_upstream_fails_the_run(world):
    world.caddy_status[ROUTE] = 502
    world.place(caddy=DUE)
    rc, out = world.run(extra={"renew_proof_retries": 1})
    assert rc != 0
    assert "Prove one request through a Caddy route that presents the leaf" in out


def test_the_caddy_proof_asks_the_inference_route_for_401_by_default(world):
    world.place(caddy=DUE)
    rc, out = world.run()
    assert rc == 0, out
    assert set(world.caddy_asked) == {(ROUTE, "/v1/models")}
    assert f"{ROUTE} answered 401" in out


def test_the_caddy_proof_can_target_the_route_that_carries_the_leaf(world):
    # Before the gateway route switch only the UI route presents Caddy's leaf: the gateway's
    # OIDC redirect (302) is the status only a request across the mutual-TLS hop gets.
    world.caddy_status[ROUTE] = 502  # the inference route must not be what passes the proof
    world.place(caddy=DUE)
    proof = {"renew_caddy_proof_host": ADMIN, "renew_caddy_proof_status": 302, "renew_caddy_proof_path": "/ui"}
    rc, out = world.run(host_over={"caddy_svc": {"caddy": proof}})
    assert rc == 0, out
    assert set(world.caddy_asked) == {(ADMIN, "/ui")}
    assert f"{ADMIN} answered 302" in out


def test_a_caddy_proof_route_answering_another_status_fails_the_run(world):
    world.place(caddy=DUE)
    rc, out = world.run(host_over={"caddy_svc": {"caddy": {"renew_caddy_proof_host": ADMIN}}},
                        extra={"renew_proof_retries": 1})
    assert rc != 0, "302 from the UI route passed a proof that expects 401"
    assert "Prove one request through a Caddy route that presents the leaf" in out


def test_with_listener_tls_off_the_gateway_leaves_are_renewed_but_not_restarted(world):
    world.place(agw_server=DUE)
    rc, out = world.run(host_over={"agentgateway_svc": {"gw": {"agw_listener_tls": False}}})
    assert rc == 0, out
    assert len(world.signed()) == 2 and world.reloads() == []
    assert "not in use: gateway listener TLS off" in out


def test_a_hidden_task_failure_is_reported_by_name_only(tmp_path):
    world = World(tmp_path, password_fails=True)
    world.place(caddy=DUE)
    rc, out = world.run()
    assert rc != 0
    assert "caddy: 'Read the issuing provisioner's password' failed" in out
    # Ansible's own display of the hidden task's templating error is not this playbook's to
    # control; what the playbook carries onward (the failure, the stats, the Loki line) is.
    carried = [line for line in out.splitlines() if "did not finish" in line or '"renewal"' in line
               or "failed:" in line and "caddy:" in line]
    assert carried and not any(HIDDEN_MARKER in line for line in carried), carried
    [run] = [s for s in world.streams() if s["stream"]["kind"] == "run"]
    assert run["stream"]["status"] == "failure" and HIDDEN_MARKER not in run["values"][0][1]


# ── Refusals: nothing reaches the CA ───────────────────────────────────────────

def _refused(world, out, rc, text):
    assert rc != 0 and text in out, out
    assert world.signed() == [], "the CA was reached by a refused run"
    runs = [s["stream"] for s in world.streams() if s["stream"]["kind"] == "run"]
    assert runs == [{"job": "renew-internal-certs", "kind": "run", "status": "failure"}], runs


@pytest.mark.parametrize("threshold", ["0", "0.0", "1.5", "-0.2", "a third"])
def test_a_threshold_outside_zero_to_one_is_refused(world, threshold):
    world.place(caddy=DUE)
    rc, out = world.run(extra={"renew_threshold": threshold})
    _refused(world, out, rc, "renew_threshold is the fraction")


@pytest.mark.parametrize("change,text", [
    (lambda lv: lv["agw-server"].update(host="caddy"), "a server leaf can be proven only as the gateway"),
    (lambda lv: lv["agw-server"].update(reload="caddy reload --force"), "a server leaf can be proven only"),
    (lambda lv: lv["agw-verifier"].update(reload="restart"), "no proof path for profile client"),
    (lambda lv: lv["caddy"].update(host="gw"), "a caddy-reloaded leaf must be on the one caddy_svc host"),
    (lambda lv: lv["agw-verifier"].update(host="caddy"), "a per-call client leaf is proven through the gateway probe"),
    (lambda lv: lv.pop("agw-verifier"), "the gateway server leaf is proven by connecting with the verifier"),
    (lambda lv: lv["caddy"].update(host="nowhere"), "its host nowhere is not in the inventory"),
    (lambda lv: lv["caddy"].update(name="agw-verifier"), "a leaf must be named, and declared once"),
], ids=["server-off-gateway", "server-caddy-reload", "client-restart", "caddy-leaf-off-caddy",
        "per-call-off-gateway", "no-verifier", "unknown-host", "duplicate-name"])
def test_a_leaf_with_no_proof_path_is_refused_before_any_issuance(world, change, text):
    world.place(agw_server=DUE, agw_verifier=DUE, caddy=DUE)
    leaves = {k: dict(v) for k, v in world.leaves.items()}
    change(leaves)
    rc, out = world.run(leaves=list(leaves.values()))
    _refused(world, out, rc, text)


def test_two_per_call_leaves_on_one_host_are_refused(world):
    world.place(agw_verifier=DUE)
    extra = {**world.leaves["agw-verifier"], "name": "bench", "sans": [f"bench.gateway.{ZONE}"]}
    rc, out = world.run(leaves=[*world.leaves.values(), extra])
    _refused(world, out, rc, "more than one per-call client leaf")


def test_a_caddy_without_the_route_address_is_refused(world):
    world.place(caddy=DUE)
    rc, out = world.run(host_over={"caddy_svc": {"caddy": {"inference_route_address": ""}}})
    _refused(world, out, rc, "which must declare inference_route_address (or renew_caddy_proof_host)")


@pytest.mark.parametrize("over", [{"renew_caddy_proof_status": "ok"}, {"renew_caddy_proof_status": 999},
                                  {"renew_caddy_proof_path": "v1/models"}, {"renew_caddy_proof_path": "/a b"}],
                         ids=["status-word", "status-999", "relative-path", "path-space"])
def test_a_malformed_caddy_proof_is_refused(world, over):
    world.place(caddy=DUE)
    rc, out = world.run(host_over={"caddy_svc": {"caddy": over}})
    _refused(world, out, rc, "renew_caddy_proof_status an HTTP status")


# ── Check mode ─────────────────────────────────────────────────────────────────

def test_check_mode_reads_and_decides_but_writes_nothing(world):
    world.place(agw_server=DUE, agw_verifier=DUE, caddy=DUE)
    before = world.serials()
    tree = sorted(p.relative_to(world.tmp) for p in world.tmp.rglob("*") if "certs" in p.parts)
    rc, out = world.run(check=True)
    assert rc == 0, out
    assert world.serials() == before
    assert sorted(p.relative_to(world.tmp) for p in world.tmp.rglob("*") if "certs" in p.parts) == tree
    assert world.signed() == [] and world.reloads() == [] and world.pushes == []
    assert "host renews (check mode: nothing is issued)" in out


# ── The Loki contract the o11y alert rules select on ───────────────────────────

def test_the_lines_carry_exactly_the_alert_rules_labels(world):
    world.place(caddy=DUE)
    rc, out = world.run()
    assert rc == 0, out
    streams = world.streams()
    # Pushed through push-loki-lines.yml: the push API, and the run line in the LAST push.
    assert {path for path, _ in world.pushes} == {"/loki/api/v1/push"}
    assert [s["stream"]["kind"] for s in world.pushes[-1][1]["streams"]] == ["run"]
    certs = [s for s in streams if s["stream"]["kind"] == "cert"]
    labels = sorted((s["stream"]["role"], s["stream"]["host"], s["stream"]["leaf"]) for s in certs)
    assert labels == [("intermediate", "ca", "intermediate"), ("leaf", "caddy", "caddy"),
                      ("leaf", "gw", "agw-server"), ("leaf", "gw", "agw-verifier")]
    serials = world.serials()
    for s in certs:
        assert set(s["stream"]) == {"job", "kind", "role", "host", "leaf"}
        assert s["stream"]["job"] == "renew-internal-certs"
        [[ts, line]] = s["values"]
        body = json.loads(line)
        assert set(body) == {"not_after", "remaining_seconds", "serial"}
        assert isinstance(body["not_after"], int) and isinstance(body["remaining_seconds"], int)
        assert abs(body["not_after"] - body["remaining_seconds"] - int(ts) // 10**9) <= 1
        if s["stream"]["role"] == "leaf":
            assert int(body["serial"], 16) == serials[s["stream"]["leaf"]]
    caddy_line = json.loads(next(s for s in certs if s["stream"]["leaf"] == "caddy")["values"][0][1])
    assert caddy_line["remaining_seconds"] > 29 * 86400, "the renewed leaf's expiry was not the one pushed"
    [run] = [s for s in streams if s["stream"]["kind"] == "run"]
    assert run["stream"] == {"job": "renew-internal-certs", "kind": "run", "status": "success"}


def test_a_failed_host_pushes_only_the_certificates_it_read(world):
    world.place()
    Path(world.leaves["caddy"]["dir"], "current").unlink()
    (Path(world.leaves["caddy"]["dir"]) / "current").symlink_to("missing")
    world.caddy_status[ROUTE] = 502
    rc, out = world.run(extra={"renew_proof_retries": 1})
    assert rc != 0
    certs = {s["stream"]["leaf"] for s in world.streams() if s["stream"]["kind"] == "cert"}
    # The caddy leaf was issued (its host had no current certificate) and read again, so it is
    # reported with its new expiry even though the route proof failed.
    assert certs == {"agw-server", "agw-verifier", "caddy", "intermediate"}
    assert [s["stream"]["status"] for s in world.streams() if s["stream"]["kind"] == "run"] == ["failure"]


# ── Task 6.2: the schedule is code ─────────────────────────────────────────────

def test_the_template_runs_daily_from_dev_right_after_issue_internal_leaf():
    names = [t["name"] for t in yaml.safe_load(TEMPLATES.read_text())["templates"]]
    tpl = next(t for t in yaml.safe_load(TEMPLATES.read_text())["templates"]
               if t["name"] == "Renew Internal Certs (Dev)")
    assert names.index("Renew Internal Certs (Dev)") == names.index("Issue Internal Leaf (Dev)") + 1
    assert tpl["playbook"] == "platform/playbooks/renew-internal-certs.yml"
    assert tpl["repository"] == "agent-cloud dev" and "dev_variant" not in tpl
    minute, hour, dom, month, dow = tpl["schedule"]["cron"].split()
    assert minute.isdigit() and hour.isdigit() and (dom, month, dow) == ("*", "*", "*")


def test_the_proof_scripts_never_print_a_key():
    play = playbook_yaml.load(PLAYBOOK)[1]
    for name in ("_renew_reader", "_renew_server_prover", "_renew_route_prover"):
        script = play["vars"][name]
        assert "key.pem" not in script and "PRIVATE" not in script, name
    # No task in the renewal itself is hidden: secrets live only inside the issuance task.
    for task in playbook_yaml.tasks(playbook_yaml.load(PLAYBOOK)):
        assert "no_log" not in task, task.get("name")


def test_the_serial_python_reports_is_the_serial_openssl_prints(tmp_path):
    # The server proof compares Python's getpeercert() serial with openssl's `-serial`, as
    # integers; a leading-zero difference between the two must not read as a mismatch.
    ca = _ca(tmp_path)
    leaf = {"dir": str(tmp_path / "l"), "sans": [f"gateway.{ZONE}"], "profile": "server"}
    serial = _place(ca, leaf, FRESH)
    srv = _thread(_gateway(ca, leaf, [], tmp_path))
    with srv as server:
        ctx = ssl.create_default_context(cadata=ca["bundle"])
        client = {"dir": str(tmp_path / "c"), "sans": [f"c.{ZONE}"], "profile": "client"}
        _place(ca, client, FRESH)
        ctx.load_cert_chain(Path(client["dir"]) / "current/cert.pem", Path(client["dir"]) / "current/key.pem")
        with (socket.create_connection(("127.0.0.1", server.server_port)) as raw,
              ctx.wrap_socket(raw, server_hostname=f"gateway.{ZONE}") as tls):
            assert int(tls.getpeercert()["serialNumber"], 16) == int(serial, 16)
