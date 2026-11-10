"""platform/lib/pve_pinned_get.py: the Proxmox API read that sends the admin token only after the leaf pin holds.

The helper runs for real against a loopback TLS server built here. The leaf certificate is generated at test
time (cryptography when importable, else the openssl CLI, else the test skips with that reason); no key literal
lives in source. The server records every byte a client sends after the handshake, so "no request byte was sent"
is measured on the wire, not inferred from an exit code.

The helpers _Leaf, _generate_leaf and _TLSRecorder are imported by test_read_proxmox_root_ca.py, which runs the
playbook against the same kind of server.
"""

import ast
import hashlib
import os
import shutil
import socket
import ssl
import subprocess
import sys
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
HELPER = REPO / "platform/lib/pve_pinned_get.py"
TOKEN_ID = "automation@pve!fixture"
SECRET = "SENTINEL-PVE-SECRET-7f3a"
BODY = b'{"data":[{"node":"pve-b","status":"online"}]}'
PATH = "/api2/json/nodes"


class _Leaf:
    """A generated leaf certificate: the PEM files the server loads, and the digests a pin is checked against."""

    def __init__(self, cert_path: Path, key_path: Path):
        self.cert_path = cert_path
        self.key_path = key_path
        self.der = ssl.PEM_cert_to_DER_cert(cert_path.read_text())
        self.flat = hashlib.sha256(self.der).hexdigest().upper()
        self.colon = ":".join(self.flat[i:i + 2] for i in range(0, len(self.flat), 2))


def _generate_leaf(directory: Path) -> _Leaf:
    cert_path = directory / "leaf-cert.pem"
    key_path = directory / "leaf-key.pem"
    try:
        import datetime
        import ipaddress

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID
    except ImportError:
        openssl = shutil.which("openssl")
        if openssl is None:
            pytest.skip("neither the cryptography package nor the openssl CLI is available to build a test certificate")
        subprocess.run(
            [openssl, "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
             "-keyout", str(key_path), "-out", str(cert_path), "-days", "1", "-subj", "/CN=127.0.0.1",
             "-addext", "subjectAltName=IP:127.0.0.1"],
            check=True, capture_output=True,
        )
        return _Leaf(cert_path, key_path)

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                       critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return _Leaf(cert_path, key_path)


class _TLSRecorder:
    """A loopback TLS server. It answers one request per connection and records every byte the client sent.

    A client that only handshakes (fingerprint mode, or a pin mismatch that the helper refuses) leaves
    `received` empty. The listener polls so close() can stop it without a race on accept().
    """

    def __init__(self, leaf: _Leaf, status: int = 200, body: bytes = BODY):
        self._ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        self._ctx.load_cert_chain(str(leaf.cert_path), str(leaf.key_path))
        self._status = status
        self._body = body
        self._listener = socket.create_server(("127.0.0.1", 0))
        self._listener.settimeout(0.2)
        self.port = self._listener.getsockname()[1]
        self.received = b""
        self.handshakes = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                raw, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            try:
                with self._ctx.wrap_socket(raw, server_side=True) as tls:
                    self.handshakes += 1
                    tls.settimeout(5)
                    while True:
                        chunk = tls.recv(65536)
                        if not chunk:
                            break
                        self.received += chunk
                        if b"\r\n\r\n" in self.received:
                            reason = "OK" if self._status == 200 else "ERR"
                            head = (f"HTTP/1.1 {self._status} {reason}\r\n"
                                    "Content-Type: application/json\r\n"
                                    f"Content-Length: {len(self._body)}\r\n"
                                    "Connection: close\r\n\r\n").encode()
                            tls.sendall(head + self._body)
                            break
            except (OSError, ssl.SSLError):
                # The helper closes after a refusal, or a handshake fails: both are the outcome under test.
                pass

    def close(self):
        """Stop serving and wait for the in-flight connection, so the counters are final before any assert."""
        self._stop.set()
        self._thread.join(timeout=15)
        self._listener.close()


@pytest.fixture(scope="module")
def leaf(tmp_path_factory):
    return _generate_leaf(tmp_path_factory.mktemp("pve-pin-leaf"))


@pytest.fixture
def make_server(leaf):
    servers = []

    def make(status: int = 200, body: bytes = BODY) -> _TLSRecorder:
        server = _TLSRecorder(leaf, status=status, body=body)
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.close()


def _base(server: _TLSRecorder) -> str:
    return f"https://127.0.0.1:{server.port}"


def _run(args: list[str], stdin: str = "") -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    return subprocess.run([sys.executable, str(HELPER), *args], input=stdin.encode(), capture_output=True,
                          env=env, timeout=90)


def _get(server: _TLSRecorder, pin: str, path: str = PATH, stdin: str | None = None) -> subprocess.CompletedProcess:
    creds = f"{TOKEN_ID}\n{SECRET}\n" if stdin is None else stdin
    return _run(["get", "--url", _base(server), "--path", path, "--sha256", pin], creds)


# --- the helper is stdlib-only (the controller installs nothing for it beyond the interpreter) ---------------


def test_the_helper_imports_only_the_standard_library():
    tree = ast.parse(HELPER.read_text())
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module.split(".")[0])
    assert modules <= set(sys.stdlib_module_names), sorted(modules - set(sys.stdlib_module_names))


# --- fingerprint: a handshake, the digest, and no HTTP request ----------------------------------------------


def test_fingerprint_prints_the_leaf_digest_and_sends_no_request(leaf, make_server):
    server = make_server()
    done = _run(["fingerprint", "--url", _base(server)])
    server.close()
    assert done.returncode == 0, done.stderr.decode()
    assert done.stdout.decode().strip() == leaf.colon
    assert server.handshakes == 1
    assert server.received == b"", "fingerprint mode wrote an HTTP request"


# --- get: the pin is checked on the connection that carries the token ---------------------------------------


def test_get_with_a_matching_pin_sends_the_token_header_and_prints_the_body(leaf, make_server):
    server = make_server()
    done = _get(server, leaf.flat.lower())
    server.close()
    assert done.returncode == 0, done.stderr.decode()
    assert done.stdout == BODY
    assert server.handshakes == 1
    assert server.received.startswith(f"GET {PATH} HTTP/1.1\r\n".encode())
    assert f"Authorization: PVEAPIToken={TOKEN_ID}={SECRET}\r\n".encode() in server.received


@pytest.mark.parametrize("form", ["colon", "flat", "lower-colon"])
def test_the_pin_is_accepted_with_or_without_colons_in_either_case(leaf, make_server, form):
    pin = {"colon": leaf.colon, "flat": leaf.flat, "lower-colon": leaf.colon.lower()}[form]
    server = make_server()
    done = _get(server, pin)
    server.close()
    assert done.returncode == 0, (form, done.stderr.decode())
    assert done.stdout == BODY


def test_get_with_a_mismatched_pin_exits_3_and_sends_zero_request_bytes(leaf, make_server):
    other = hashlib.sha256(b"not the leaf certificate").hexdigest()
    server = make_server()
    done = _get(server, other)
    server.close()
    assert done.returncode == 3, (done.returncode, done.stderr.decode())
    # The handshake happened, on this connection, before the refusal: the check ran where the token would go.
    assert server.handshakes == 1
    assert server.received == b"", "a request byte was sent after a pin mismatch"
    err = done.stderr.decode()
    other_colon = ":".join(other.upper()[i:i + 2] for i in range(0, 64, 2))
    assert other_colon in err and leaf.colon in err, "stderr must name both fingerprints"
    assert SECRET not in err and TOKEN_ID not in err
    assert done.stdout == b""


def test_a_non_200_answer_prints_the_status_only_and_no_body(leaf, make_server):
    leak = b'{"errors":"LEAKED-BODY-TEXT"}'
    server = make_server(status=401, body=leak)
    done = _get(server, leaf.flat)
    server.close()
    assert done.returncode == 4, done.stderr.decode()
    assert done.stdout == b""
    err = done.stderr.decode()
    assert "HTTP 401" in err
    assert "LEAKED-BODY-TEXT" not in err and SECRET not in err


def test_a_failed_handshake_exits_5_and_no_token_is_sent(leaf):
    # A plain-TCP peer answers the ClientHello with nothing and closes: the handshake fails before any request.
    listener = socket.create_server(("127.0.0.1", 0))
    seen = bytearray()

    def peer():
        raw, _ = listener.accept()
        with raw:
            raw.settimeout(5)
            try:
                while True:
                    chunk = raw.recv(65536)
                    if not chunk:
                        break
                    seen.extend(chunk)
            except OSError:
                pass

    thread = threading.Thread(target=peer, daemon=True)
    thread.start()
    port = listener.getsockname()[1]
    done = _run(["get", "--url", f"https://127.0.0.1:{port}", "--path", PATH, "--sha256", leaf.flat],
                f"{TOKEN_ID}\n{SECRET}\n")
    thread.join(timeout=15)
    listener.close()
    assert done.returncode == 5, (done.returncode, done.stderr.decode())
    assert SECRET.encode() not in bytes(seen) and b"Authorization" not in bytes(seen)
    assert SECRET not in done.stderr.decode()


# --- refusals: each one happens before any connection -------------------------------------------------------


@pytest.mark.parametrize("url_tail", [
    "http://127.0.0.1:{port}",
    "https://user:" + SECRET + "@127.0.0.1:{port}",
    "https://127.0.0.1:{port}/api2",
    "https://127.0.0.1:{port}?x=1",
    "https://127.0.0.1:{port}#frag",
])
def test_a_url_that_is_not_a_bare_https_base_is_refused_before_connecting(leaf, make_server, url_tail):
    server = make_server()
    url = url_tail.format(port=server.port)
    done = _run(["get", "--url", url, "--path", PATH, "--sha256", leaf.flat], f"{TOKEN_ID}\n{SECRET}\n")
    server.close()
    assert done.returncode == 2, (url_tail, done.returncode, done.stderr.decode())
    assert SECRET not in done.stderr.decode()
    assert server.handshakes == 0 and server.received == b""


def test_fingerprint_refuses_http_too(make_server):
    server = make_server()
    done = _run(["fingerprint", "--url", f"http://127.0.0.1:{server.port}"])
    server.close()
    assert done.returncode == 2
    assert server.handshakes == 0


@pytest.mark.parametrize("pin", ["", "ABC", "Z" * 64, "AA" * 31])
def test_a_pin_that_is_not_a_sha256_digest_is_refused_before_connecting(leaf, make_server, pin):
    server = make_server()
    done = _get(server, pin)
    server.close()
    assert done.returncode == 2, (pin, done.stderr.decode())
    assert server.handshakes == 0 and server.received == b""


@pytest.mark.parametrize("path", ["api2/json/nodes", "/api2 json/nodes", ""])
def test_a_relative_or_spaced_api_path_is_refused_before_connecting(leaf, make_server, path):
    server = make_server()
    done = _get(server, leaf.flat, path=path)
    server.close()
    assert done.returncode == 2, (path, done.stderr.decode())
    assert server.handshakes == 0


@pytest.mark.parametrize("stdin", [f"{TOKEN_ID}\n", "", f"{TOKEN_ID}\n{SECRET}\nextra\n", f"\n{SECRET}\n"])
def test_malformed_credentials_on_stdin_are_refused_before_connecting(leaf, make_server, stdin):
    server = make_server()
    done = _get(server, leaf.flat, stdin=stdin)
    server.close()
    assert done.returncode == 2, (stdin, done.stderr.decode())
    assert SECRET not in done.stderr.decode()
    assert server.handshakes == 0 and server.received == b""


# --- the pin against the operator's own cross-check command --------------------------------------------------


def test_the_pin_matches_the_openssl_cross_check_the_playbook_documents(leaf, make_server):
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("the openssl CLI is not available to run the documented cross-check")
    server = make_server()
    # The playbook's documented pipeline: openssl s_client -connect <host>:<port> </dev/null | openssl x509 ...
    probe = subprocess.run([openssl, "s_client", "-connect", f"127.0.0.1:{server.port}"],
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
    digest = subprocess.run([openssl, "x509", "-noout", "-fingerprint", "-sha256"],
                            input=probe.stdout, capture_output=True, timeout=60)
    server.close()
    assert digest.returncode == 0, digest.stderr.decode()
    value = digest.stdout.decode().strip().split("=", 1)[-1].strip().upper()
    assert value == leaf.colon, (value, leaf.colon)
