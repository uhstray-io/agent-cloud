"""HTTPS listener tests for the launch gate: TLS, certificate reload, the body cap, HTTP framing and main.

Each test starts its own GateServer on loopback port 0 with a stub gate, so nothing is shared and the file
runs under pytest-xdist. Leaves are self-signed and written in the production layout (``<leaf>/<serial>/``
with ``<leaf>/current`` a symlink), and renewal is the same symlink swap the renewal job makes. The request
flow is tested in test_launch_gate_flow.py; here the gate behind the server is a stub that records calls.
"""

import datetime
import http.client
import importlib.util
import ipaddress
import json
import os
import re
import socket
import ssl
import struct
import sys
import threading
import time
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "platform/services/launch-gate/deployment/app/launch_gate.py"


def _load_gate():
    spec = importlib.util.spec_from_file_location("launch_gate_tls", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before exec so dataclasses can resolve the module by name.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


lg = _load_gate()

SKYNET_TOKEN = "skyn-" + "j" * 40
SEMA_TOKEN = "sema-" + "k" * 40
TEMPLATE = "Deploy agentgateway (Dev)"
# A unique sentinel: a leak of an exception message would show up in stderr, never by coincidence.
SECRET_VALUE = "sv-w3-" + "z" * 20


def _leaf_pair(serial, subject):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(serial)
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return key, cert


def _key_pem(key):
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


class StubGate:
    """Records what the server hands the gate and answers 200. Its ``_log`` collects the server's events."""

    def __init__(self):
        self.calls = []
        self.events = []
        self._log = self.events.append

    def handle(self, method, path, headers, body):
        self.calls.append((method, path, dict(headers), body))
        return 200, {"ok": True}


class Listener:
    """A GateServer on loopback, serving a leaf from ``<root>/current`` in a thread."""

    def __init__(self, tmp_path):
        self.root = tmp_path / "certs" / "launch-gate-server"
        self.root.mkdir(parents=True)
        self.gate = StubGate()
        self.leaf_pems = []
        self.point_current(self.install(1))
        self.server = lg.GateServer(
            ("127.0.0.1", 0), self.gate, str(self.current / "cert.pem"), str(self.current / "key.pem")
        )
        self.port = self.server.server_address[1]
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def current(self):
        return self.root / "current"

    def install(self, serial):
        """Write a leaf into ``<root>/<serial>/`` and trust its certificate from now on."""
        key, cert = _leaf_pair(serial, f"launch-gate-test-{serial}")
        directory = self.root / str(serial)
        directory.mkdir()
        (directory / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        (directory / "key.pem").write_bytes(_key_pem(key))
        self.leaf_pems.append(cert.public_bytes(serialization.Encoding.PEM))
        return directory

    def point_current(self, target):
        """Swap ``current`` the way renewal does: a new link beside it, then one rename over the old."""
        tmp = self.root / ".current.tmp"
        if os.path.lexists(tmp):
            os.unlink(tmp)
        os.symlink(os.path.relpath(target, self.root), tmp)
        os.replace(tmp, self.current)

    def client_context(self):
        bundle = self.root.parent / "trust.pem"
        bundle.write_bytes(b"".join(self.leaf_pems))
        context = ssl.create_default_context(cafile=str(bundle))
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        return context

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def listener(tmp_path):
    built = Listener(tmp_path)
    yield built
    built.close()


def _https(port, context, method, path, *, body=None, headers=None):
    conn = http.client.HTTPSConnection("127.0.0.1", port, context=context, timeout=5)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def _served_serial(port, context):
    """The serial of the certificate the server presents on a new connection, after one request."""
    conn = http.client.HTTPSConnection("127.0.0.1", port, context=context, timeout=5)
    try:
        conn.connect()
        der = conn.sock.getpeercert(binary_form=True)
        conn.request("GET", "/healthz")
        conn.getresponse().read()
    finally:
        conn.close()
    return x509.load_der_x509_certificate(der).serial_number


def _send_and_read(port, context, head):
    """Send the given request bytes over TLS in one write, and read the reply by its Content-Length."""
    tcp = socket.create_connection(("127.0.0.1", port), timeout=5)
    sock = context.wrap_socket(tcp, server_hostname="localhost")
    response = http.client.HTTPResponse(sock)
    try:
        sock.sendall(head)
        response.begin()
        return response.status, response.read()
    finally:
        response.close()
        sock.close()


def _with_authorization(port, context, values):
    conn = http.client.HTTPSConnection("127.0.0.1", port, context=context, timeout=5)
    try:
        conn.putrequest("GET", "/healthz")
        for value in values:
            conn.putheader("Authorization", value)
        conn.endheaders()
        conn.getresponse().read()
    finally:
        conn.close()


# ---------- transport: TLS only ----------


def test_https_request_reaches_the_gate(listener):
    status, body = _https(listener.port, listener.client_context(), "GET", "/healthz")
    assert (status, json.loads(body)) == (200, {"ok": True})
    assert [(method, path) for method, path, _, _ in listener.gate.calls] == [("GET", "/healthz")]


def test_plain_http_gets_no_200_and_reaches_nothing(listener, capsys):
    conn = http.client.HTTPConnection("127.0.0.1", listener.port, timeout=5)
    try:
        conn.request("GET", "/healthz")
        status = conn.getresponse().status
    except (http.client.HTTPException, OSError):
        status = None
    finally:
        conn.close()
    assert status != 200
    assert listener.gate.calls == []
    # A failed handshake is normal traffic: it writes nothing, not even a traceback.
    assert capsys.readouterr().err == ""


def test_a_silent_connection_does_not_hold_the_accept_loop(listener):
    silent = socket.create_connection(("127.0.0.1", listener.port))
    try:
        started = time.monotonic()
        status, _ = _https(listener.port, listener.client_context(), "GET", "/healthz")
        elapsed = time.monotonic() - started
    finally:
        # Closing the silent socket ends its handshake, so the server can shut down even if this test failed.
        silent.close()
    assert status == 200
    assert elapsed < 5


def test_a_reset_connection_writes_one_fixed_line_and_no_traceback(listener, capsys):
    tcp = socket.create_connection(("127.0.0.1", listener.port), timeout=5)
    tls = listener.client_context().wrap_socket(tcp, server_hostname="localhost")
    tls.sendall(b"GET /hea")
    # Let the server finish its handshake and block reading the partial request line before the reset.
    time.sleep(0.2)
    tls.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    tls.close()
    err = ""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and "connection error" not in err:
        err += capsys.readouterr().err
        time.sleep(0.05)
    # One fixed line naming the exception class, whichever class the kernel reports for the reset.
    assert re.fullmatch(r"launch-gate: connection error \(\w+\)\n", err)


# ---------- certificate reload ----------


def test_renewed_certificate_is_served_without_a_restart(listener):
    before = _served_serial(listener.port, listener.client_context())
    listener.point_current(listener.install(2))
    after = _served_serial(listener.port, listener.client_context())
    assert (before, after) == (1, 2)
    assert listener._thread.is_alive()
    assert listener.gate.events == []


def test_failed_reload_keeps_the_last_good_certificate_and_retries(listener):
    bad = listener.root / "bad"
    bad.mkdir()
    (bad / "cert.pem").write_bytes(b"not a certificate\n")
    (bad / "key.pem").write_bytes(b"not a key\n")
    listener.point_current(bad)
    assert _served_serial(listener.port, listener.client_context()) == 1
    assert listener.gate.events == [{"event": "tls_reload_failed", "error": "SSLError"}]
    # The failure is not remembered: once a good leaf is in place, the next connection picks it up.
    listener.point_current(listener.install(3))
    assert _served_serial(listener.port, listener.client_context()) == 3
    assert len(listener.gate.events) == 1


# ---------- HTTP framing and the body cap ----------


def test_post_body_reaches_the_gate_byte_for_byte(listener):
    payload = b'{"template": "x"}'
    status, _ = _https(
        listener.port,
        listener.client_context(),
        "POST",
        "/v1/launch",
        body=payload,
        headers={"Content-Type": "application/json"},
    )
    assert status == 200
    assert listener.gate.calls[0][3] == payload


def test_body_at_the_cap_is_read_and_is_not_refused(listener):
    status, _ = _https(listener.port, listener.client_context(), "POST", "/v1/launch", body=b"x" * lg.MAX_BODY)
    assert status == 200
    assert len(listener.gate.calls[0][3]) == lg.MAX_BODY


def test_oversize_body_is_drained_and_refused_before_the_gate(listener):
    # The 413 waits for the declared body: with all but its last byte sent, the server is still reading and
    # has replied nothing. Sending the last byte then brings the 413, so no reset can stand in for it.
    body = b"x" * (lg.MAX_BODY + 1)
    head = f"POST /v1/launch HTTP/1.1\r\nHost: localhost\r\nContent-Length: {len(body)}\r\n\r\n".encode()
    tcp = socket.create_connection(("127.0.0.1", listener.port), timeout=5)
    sock = listener.client_context().wrap_socket(tcp, server_hostname="localhost")
    try:
        sock.sendall(head + body[:-1])
        sock.settimeout(0.5)
        with pytest.raises(TimeoutError):
            sock.recv(4096)
        sock.settimeout(5)
        sock.sendall(body[-1:])
        response = http.client.HTTPResponse(sock)
        response.begin()
        assert (response.status, json.loads(response.read())) == (413, {"error": "too_large"})
        response.close()
    finally:
        sock.close()
    assert listener.gate.calls == []


def test_oversize_body_never_sent_is_refused_once_the_drain_gives_up(listener, monkeypatch):
    # No body arrives at all: the drain stops at its first read timeout, and the 413 still goes out.
    monkeypatch.setattr(lg, "CONNECTION_TIMEOUT", 0.5)
    head = f"POST /v1/launch HTTP/1.1\r\nHost: localhost\r\nContent-Length: {lg.MAX_BODY + 1}\r\n\r\n".encode()
    status, body = _send_and_read(listener.port, listener.client_context(), head)
    assert (status, json.loads(body)) == (413, {"error": "too_large"})
    assert listener.gate.calls == []


def test_oversize_body_is_read_only_up_to_the_drain_cap(listener):
    # The declared body is one byte past the cap and the client sends exactly the cap. A drain that read past it
    # would wait for that byte, and the reply would miss the client's timeout.
    head = f"POST /v1/launch HTTP/1.1\r\nHost: localhost\r\nContent-Length: {lg.DRAIN_CAP + 1}\r\n\r\n".encode()
    status, body = _send_and_read(listener.port, listener.client_context(), head + b"x" * lg.DRAIN_CAP)
    assert (status, json.loads(body)) == (413, {"error": "too_large"})
    assert listener.gate.calls == []


@pytest.mark.parametrize(
    "lengths",
    [[], ["abc"], ["-1"], ["+5"], ["1_0"], ["5", "5"]],
    ids=["missing", "non-digit", "negative", "plus-sign", "underscore", "repeated"],
)
def test_post_without_a_plain_content_length_is_400(listener, lengths):
    head = "POST /v1/launch HTTP/1.1\r\nHost: localhost\r\n"
    head += "".join(f"Content-Length: {value}\r\n" for value in lengths) + "\r\n"
    status, body = _send_and_read(listener.port, listener.client_context(), head.encode())
    assert (status, json.loads(body)) == (400, {"error": "bad_request", "detail": "Content-Length required"})
    assert listener.gate.calls == []


def test_query_string_is_not_part_of_the_path(listener):
    status, _ = _https(listener.port, listener.client_context(), "GET", "/healthz?probe=1")
    assert status == 200
    assert listener.gate.calls[0][:2] == ("GET", "/healthz")


def test_two_authorization_headers_are_not_passed_to_the_gate(listener):
    _with_authorization(listener.port, listener.client_context(), ["Bearer " + "a" * 40, "Bearer " + "b" * 40])
    seen = {name.lower() for name in listener.gate.calls[0][2]}
    assert "authorization" not in seen


def test_one_authorization_header_is_passed_through(listener):
    token = "Bearer " + "a" * 40
    _with_authorization(listener.port, listener.client_context(), [token])
    assert listener.gate.calls[0][2]["Authorization"] == token


# ---------- connection cap and request deadline ----------


def test_a_connection_past_the_cap_is_dropped_and_capacity_returns(listener):
    held = []
    try:
        for _ in range(lg.MAX_CONNECTIONS):
            held.append(socket.create_connection(("127.0.0.1", listener.port)))
            time.sleep(0.02)
        # Let the accept loop take a slot for every held connection before the probe queues behind them.
        time.sleep(0.2)
        started = time.monotonic()
        try:
            status, _ = _https(listener.port, listener.client_context(), "GET", "/healthz")
        except (OSError, http.client.HTTPException):
            status = None
        elapsed = time.monotonic() - started
        # Refused at once: the server closes the probe during its handshake instead of holding a thread for it.
        assert status is None and elapsed < 3
        assert listener.gate.calls == []
    finally:
        # Closing the held sockets ends their handshakes, so their slots come back.
        for sock in held:
            sock.close()
    deadline = time.monotonic() + 5
    while True:
        try:
            status, _ = _https(listener.port, listener.client_context(), "GET", "/healthz")
            break
        except (OSError, http.client.HTTPException):
            if time.monotonic() > deadline:
                raise
            time.sleep(0.1)
    assert status == 200


def test_a_body_trickled_past_the_request_deadline_is_refused_and_never_completed(listener, monkeypatch):
    monkeypatch.setattr(lg, "REQUEST_DEADLINE", 1.0)
    # Declares 1000 bytes and sends one a tenth of a second: finishing would take about 100 seconds.
    head = b"POST /v1/launch HTTP/1.1\r\nHost: localhost\r\nContent-Length: 1000\r\n\r\n"
    tcp = socket.create_connection(("127.0.0.1", listener.port), timeout=5)
    sock = listener.client_context().wrap_socket(tcp, server_hostname="localhost")
    reply = b""
    ended = False
    try:
        sock.sendall(head)
        sock.settimeout(0.1)
        started = time.monotonic()
        while not ended and time.monotonic() - started < 4:
            try:
                sock.sendall(b"x")
                chunk = sock.recv(4096)
            except TimeoutError:
                time.sleep(0.1)
                continue
            except OSError:
                ended = True
                break
            if not chunk:
                ended = True
            reply += chunk
            time.sleep(0.1)
    finally:
        sock.close()
    # The server answers 408 or closes the connection; it never reads the body to its end.
    assert ended
    assert not reply or re.match(rb"HTTP/1\.[01] 408 ", reply)
    assert listener.gate.calls == []


def test_headers_trickled_past_the_connection_deadline_are_cut(listener, monkeypatch):
    # Each header byte arrives well inside CONNECTION_TIMEOUT, so only the whole-connection watchdog can end
    # this one; without it the connection outlives the four-second loop below. The cut must also never reach
    # the gate: http.server reads end-of-stream as the end of the headers.
    monkeypatch.setattr(lg, "CONNECTION_DEADLINE", 1.0)
    tcp = socket.create_connection(("127.0.0.1", listener.port), timeout=5)
    sock = listener.client_context().wrap_socket(tcp, server_hostname="localhost")
    ended = False
    try:
        sock.settimeout(0.1)
        started = time.monotonic()
        while not ended and time.monotonic() - started < 4:
            try:
                sock.sendall(b"X-Slow: y\r\n" if time.monotonic() - started > 0.05 else b"GET /healthz HTTP/1.1\r\n")
                if not sock.recv(4096):
                    ended = True
            except TimeoutError:
                pass
            except OSError:
                ended = True
            time.sleep(0.1)
    finally:
        sock.close()
    assert ended
    assert listener.gate.calls == []


def test_a_launch_slower_than_the_connection_deadline_still_answers(listener, monkeypatch):
    # The watchdog bounds the client's part only. A gate whose Semaphore and OPA calls outlast it may already
    # have created a task, and the reply carrying the task id must still arrive.
    monkeypatch.setattr(lg, "CONNECTION_DEADLINE", 0.5)
    answer = listener.gate.handle

    def slow(method, path, headers, body):
        time.sleep(1.5)
        return answer(method, path, headers, body)

    monkeypatch.setattr(listener.gate, "handle", slow)
    status, _ = _https(listener.port, listener.client_context(), "GET", "/healthz")
    assert status == 200


def test_a_connection_the_watchdog_cut_never_reaches_the_gate(listener, monkeypatch):
    # Platform-independent form of the trickle case above: whether a cut read returns end-of-stream (Linux,
    # which http.server then takes as the end of the headers) or raises (macOS) varies, so the guard itself
    # is driven here. A complete request on a connection marked cut must not be dispatched.
    monkeypatch.setattr(listener.server, "disarm", lambda: True)
    with pytest.raises((OSError, http.client.HTTPException)):
        _https(listener.port, listener.client_context(), "GET", "/healthz")
    assert listener.gate.calls == []


def test_the_watchdog_marks_the_connection_before_it_shuts_it(monkeypatch):
    # The order is the property: a handler woken by the shutdown must already see the mark.
    left, right = socket.socketpair()
    watch = lg._Watch()
    seen = []
    real = socket.socket.shutdown

    def shutdown(sock, how):
        seen.append(watch.cut)
        real(sock, how)

    monkeypatch.setattr(lg.socket.socket, "shutdown", shutdown)
    try:
        lg._cut(left, watch)
        assert seen == [True]
        assert right.recv(1) == b""
    finally:
        left.close()
        right.close()


def test_a_cut_that_starts_after_the_disarm_does_nothing(listener, monkeypatch):
    # Timer.cancel() cannot stop a callback already running, so the cut checks the
    # disarm under the same lock. Once the server has disarmed, the connection is neither marked nor shut.
    # Driven through GateServer.disarm() on this thread, as the handler thread would call it.
    left, right = socket.socketpair()
    watch = lg._Watch()
    listener.server._watch.timer = None
    listener.server._watch.state = watch
    calls = []
    monkeypatch.setattr(lg.socket.socket, "shutdown", lambda sock, how: calls.append(how))
    try:
        assert listener.server.disarm() is False
        lg._cut(left, watch)
        assert calls == []
        assert not watch.cut
    finally:
        left.close()
        right.close()


def test_a_read_that_runs_out_the_request_deadline_is_answered_408(listener, monkeypatch):
    monkeypatch.setattr(lg, "REQUEST_DEADLINE", 1.0)
    # The headers and the first body byte arrive, then silence: the server's read of the rest is bounded by the
    # deadline itself, so it times out at the deadline and must answer 408 rather than drop the connection.
    head = b"POST /v1/launch HTTP/1.1\r\nHost: localhost\r\nContent-Length: 1000\r\n\r\nx"
    tcp = socket.create_connection(("127.0.0.1", listener.port), timeout=5)
    sock = listener.client_context().wrap_socket(tcp, server_hostname="localhost")
    reply = b""
    try:
        sock.sendall(head)
        time.sleep(2.0)
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            reply += chunk
    finally:
        sock.close()
    assert re.match(rb"HTTP/1\.[01] 408 ", reply), reply[:60]
    assert listener.gate.calls == []


def test_a_body_stalled_within_the_deadline_closes_without_a_reply(listener, monkeypatch):
    # The per-read bound is the smaller one here, so a stall is an idle client, not an expired deadline: the
    # connection ends with no reply, the same as any idle client.
    monkeypatch.setattr(lg, "CONNECTION_TIMEOUT", 0.5)
    monkeypatch.setattr(lg, "REQUEST_DEADLINE", 5.0)
    head = b"POST /v1/launch HTTP/1.1\r\nHost: localhost\r\nContent-Length: 1000\r\n\r\nx"
    tcp = socket.create_connection(("127.0.0.1", listener.port), timeout=5)
    sock = listener.client_context().wrap_socket(tcp, server_hostname="localhost")
    reply = b""
    try:
        sock.sendall(head)
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            reply += chunk
    finally:
        sock.close()
    assert reply == b""
    assert listener.gate.calls == []


# ---------- the gate's log write can fail without losing the response ----------


CANNED_REQUEST = {
    "template": TEMPLATE,
    "agent": "infra-agent",
    "workflow_id": "onboard-x",
    "step": "ssh-keys",
    "survey": {},
}


class CannedSemaphore:
    def template(self, name):
        return {"id": 12, "name": name, "repository_id": 5, "survey_vars": []}

    def branch(self, repository_id):
        return "dev"

    def running(self, template_id):
        return []

    def create(self, body):
        return 77


class CannedOPA:
    def decide(self, decision_input):
        return "ok by policy"


def _write_config(tmp_path, *, port=8443, tls_cert="/unused/cert.pem", tls_key="/unused/key.pem"):
    doc = {
        "semaphore_url": "https://semaphore.internal.test",
        "semaphore_project": 2,
        "opa_url": "https://opa.internal.test:8181",
        "allowlist": [TEMPLATE],
        "callers": {"skynet": ["infra-agent"]},
        "listen_host": "127.0.0.1",
        "listen_port": port,
        "tls_cert": str(tls_cert),
        "tls_key": str(tls_key),
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _set_tokens(monkeypatch):
    monkeypatch.setenv("LAUNCH_GATE_SEMAPHORE_TOKEN", SEMA_TOKEN)
    monkeypatch.setenv("LAUNCH_GATE_CALLER_TOKEN_SKYNET", SKYNET_TOKEN)


def test_a_failing_log_write_still_returns_the_task_id_and_says_so(tmp_path, monkeypatch, capsys):
    _set_tokens(monkeypatch)
    cfg = lg.load_config(str(_write_config(tmp_path)), dict(os.environ))
    recorded = []

    def failing_log(event):
        recorded.append(event)
        raise OSError(f"no space left writing {SECRET_VALUE}")

    gate = lg.Gate(cfg, CannedSemaphore(), CannedOPA(), log=failing_log)
    status, payload = gate.handle(
        "POST", "/v1/launch", {"Authorization": f"Bearer {SKYNET_TOKEN}"}, json.dumps(CANNED_REQUEST).encode()
    )
    assert (status, payload["task_id"]) == (201, 77)
    assert len(recorded) == 1 and recorded[0]["outcome"] == 201
    # The fixed line only: the exception text carries a value that must not reach stderr.
    assert capsys.readouterr().err == "launch-gate: log write failed\n"


# ---------- entry point ----------


def _must_not_serve(self):
    pytest.fail("serve_forever was reached")


class _EphemeralGateServer(lg.GateServer):
    """GateServer that binds port 0 whatever the config names. A probe socket closed before main() binds
    could hand its port to another process, so the test lets the kernel choose and reads the port back."""

    def __init__(self, address, gate, cert_path, key_path):
        super().__init__((address[0], 0), gate, cert_path, key_path)


def test_main_refuses_a_missing_config_file(capsys):
    assert lg.main(["/nonexistent/launch-gate/config.json"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("launch-gate: ") and "/nonexistent/launch-gate/config.json" in err


def test_main_reads_the_config_path_from_the_environment(monkeypatch, capsys):
    monkeypatch.setenv("LAUNCH_GATE_CONFIG", "/nonexistent/from-environment.json")
    assert lg.main([]) == 2
    assert "/nonexistent/from-environment.json" in capsys.readouterr().err


def test_main_refuses_an_unreadable_certificate_before_listening(tmp_path, monkeypatch, capsys):
    _set_tokens(monkeypatch)
    # The certificate fails before the socket is bound, so the configured port is never taken.
    path = _write_config(tmp_path, tls_cert=tmp_path / "missing" / "cert.pem", tls_key=tmp_path / "missing" / "key.pem")
    monkeypatch.setattr(lg.GateServer, "serve_forever", _must_not_serve)
    assert lg.main([str(path)]) == 2
    assert capsys.readouterr().err == "launch-gate: TLS certificate or key unreadable (FileNotFoundError)\n"


def test_main_starts_the_listener_and_reports_it(tmp_path, monkeypatch, capsys):
    _set_tokens(monkeypatch)
    key, cert = _leaf_pair(9, "launch-gate-main")
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(_key_pem(key))
    path = _write_config(tmp_path, port=8443, tls_cert=cert_path, tls_key=key_path)
    bound = []

    class _Recording(_EphemeralGateServer):
        def __init__(self, *args):
            super().__init__(*args)
            bound.append(self.server_address[1])

    monkeypatch.setattr(lg, "GateServer", _Recording)
    monkeypatch.setattr(_Recording, "serve_forever", lambda self: None)
    assert lg.main([str(path)]) == 0
    assert bound and bound[0] != 0
    # The event names the configured port; the listener itself is the ephemeral one bound above.
    assert json.loads(capsys.readouterr().out) == {"event": "listening", "host": "127.0.0.1", "port": 8443}
