"""verify-service-health.yml (workflow step service-validate) passes only on HTTP 200.

Runs the real playbook against a throwaway local HTTP server. The inventory's
health_status_codes admits e.g. OpenBao's sealed 503 for the fleet overview; the step's
criterion is 200 and must not inherit that (PR 203 Codex review). Requires ansible-playbook.
"""

import shutil
import ssl
import subprocess
from contextlib import ExitStack, contextmanager
from pathlib import Path

import harness_sandbox
import pytest
from fake_http import DrainingHandler, serve
from strict_chain import assert_strict_chain

REPO = Path(__file__).resolve().parents[2]
PLAYBOOK = REPO / "platform/playbooks/verify-service-health.yml"

pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")

# The internal-CA shape the probe must verify: a CA, and a serverAuth leaf it signs for the name
# health.test and for 127.0.0.1. Generated per test in tmp_path; no key material is committed.
CA_CNF = """[req]
distinguished_name = dn
prompt = no
[dn]
CN = Test internal CA
[ca]
basicConstraints = critical,CA:TRUE
keyUsage = critical,keyCertSign,cRLSign
# Explicit, not left to the OpenSSL build: without an SKI here, strict chain verification rejects the CA.
subjectKeyIdentifier = hash
"""
LEAF_CNF = """[req]
distinguished_name = dn
prompt = no
[dn]
CN = health.test
[leaf]
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature,keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = DNS:health.test,IP:127.0.0.1
# Python 3.13+ VERIFY_X509_STRICT rejects a CA-signed leaf without an Authority Key Identifier.
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid,issuer
"""
needs_tls = pytest.mark.skipif(shutil.which("openssl") is None or shutil.which("curl") is None,
                               reason="needs openssl and curl")


@contextmanager
def _serve(status: int):
    class Handler(DrainingHandler):
        def do_GET(self):  # noqa: N802 - the stdlib's name
            self.send_response(status)
            self.end_headers()

        def log_message(self, *args):
            pass

    with serve(Handler) as url:
        yield url


def _run(tmp_path, status: int, health_path: str = "/health", via_health_url: bool = False,
         host_vars: str = ""):
    with _serve(status) as base:
        inventory = tmp_path / "inventory.ini"
        where = (f"health_url={base}/health\n" if via_health_url else
                 f"service_url={base}\nhealth_path={health_path}\n")
        inventory.write_text(
            f"[demo_svc]\ndemo ansible_connection=local {host_vars}\n\n[demo_svc:vars]\n"
            f"service_name=demo\n{where}health_status_codes=[200, 503]\n"
        )
        return harness_sandbox.run_playbook(PLAYBOOK, tmp_path, "-e", "target_service=demo_svc",
                                            inventory=inventory)


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
    for group in ("step_ca_svc", "authentik_svc", "o11y_svc", "agentgateway_svc", "opa_svc"):
        section = text.split(f"[{group}:vars]", 1)[1].split("\n          [", 1)[0]
        assert "health_url=http" in section, group


def test_a_target_group_with_no_hosts_fails(tmp_path):
    # PR 203 Codex review: a play whose hosts: matched nothing exited 0 with no step result.
    inventory = tmp_path / "inventory.ini"
    inventory.write_text("[demo_svc]\ndemo ansible_connection=local\n")
    done = harness_sandbox.run_playbook(PLAYBOOK, tmp_path, "-e", "target_service=typo_svc", inventory=inventory)
    assert done.returncode != 0
    assert "matches no hosts in this inventory" in done.stdout


def test_one_failing_host_fails_the_recorded_result(tmp_path):
    # PR 203 Codex review: emit-step-result records once per play, and recorded the first host's
    # verdict; a healthy first host hid a failing second one.
    with ExitStack() as stack:
        good = stack.enter_context(_serve(200))
        bad = stack.enter_context(_serve(503))
        inventory = tmp_path / "inventory.ini"
        inventory.write_text(
            "[demo_svc]\n"
            f"demo1 ansible_connection=local health_url={good}/health\n"
            f"demo2 ansible_connection=local health_url={bad}/health\n"
            "\n[demo_svc:vars]\nservice_name=demo\n"
        )
        done = harness_sandbox.run_playbook(PLAYBOOK, tmp_path, "-e", "target_service=demo_svc",
                                            inventory=inventory)
    assert done.returncode != 0
    recorded = done.stdout.split("CUSTOM STATS", 1)[-1]
    assert '"status": "fail"' in recorded and "answered 503" in recorded, recorded[-1500:]


def _tls_material(directory: Path):
    """A throwaway internal CA and the serverAuth leaf it signs, written into directory."""
    openssl = shutil.which("openssl")
    (directory / "ca.cnf").write_text(CA_CNF)
    (directory / "leaf.cnf").write_text(LEAF_CNF)

    def run(*args):
        subprocess.run([openssl, *args], cwd=directory, check=True, capture_output=True, stdin=subprocess.DEVNULL,
                       timeout=harness_sandbox.DEFAULT_TIMEOUT_SECONDS)

    run("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key", "-out", "ca.pem", "-days", "2",
        "-config", "ca.cnf", "-extensions", "ca")
    run("req", "-new", "-newkey", "rsa:2048", "-nodes", "-keyout", "leaf.key", "-out", "leaf.csr",
        "-config", "leaf.cnf")
    run("x509", "-req", "-in", "leaf.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial", "-days", "2",
        "-out", "leaf.pem", "-extfile", "leaf.cnf", "-extensions", "leaf")
    assert_strict_chain(directory / "ca.pem", directory / "leaf.pem")


@contextmanager
def _serve_tls(status: int, directory: Path):
    class Handler(DrainingHandler):
        def do_GET(self):  # noqa: N802 - the stdlib's name
            self.send_response(status)
            self.end_headers()

        def log_message(self, *args):
            pass

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(directory / "leaf.pem", directory / "leaf.key")
    with serve(Handler, ssl_context=context) as url:
        yield url


def _run_tls(tmp_path, status: int, host_vars: str = "", *, trust: bool = True, pin: bool = True,
             url_host: str = "health.test"):
    """Run the playbook against a local HTTPS server. Returns (completed process, health URL)."""
    if not (tmp_path / "leaf.pem").exists():
        _tls_material(tmp_path)
    with _serve_tls(status, tmp_path) as base:
        url = f"https://{url_host}:{base.rsplit(':', 1)[1]}/health"
        declared = [f"health_url={url}"]
        if trust:
            declared.append(f"health_ca_path={tmp_path / 'ca.pem'}")
        if pin:
            declared.append("health_resolve_address=127.0.0.1")
        inventory = tmp_path / "inventory.ini"
        inventory.write_text(
            f"[demo_svc]\ndemo ansible_connection=local {host_vars}\n\n[demo_svc:vars]\n"
            f"service_name=demo\n" + "\n".join(declared) + "\n"
        )
        done = harness_sandbox.run_playbook(PLAYBOOK, tmp_path, "-e", "target_service=demo_svc",
                                            inventory=inventory)
        return done, url


@needs_tls
def test_https_under_the_internal_ca_at_a_pinned_address_passes(tmp_path):
    done, _ = _run_tls(tmp_path, 200)
    assert done.returncode == 0, done.stdout[-2000:]
    # The evidence carries the integer status from the normalised probe, not a string.
    assert '"health_status": 200' in done.stdout.split("CUSTOM STATS", 1)[-1], done.stdout[-1500:]


@needs_tls
def test_https_without_the_internal_ca_fails_and_names_the_url(tmp_path):
    done, url = _run_tls(tmp_path, 200, trust=False)
    assert done.returncode != 0
    assert f"{url} answered no response" in done.stdout, done.stdout[-2000:]
    # curl's first stderr line rides along: exit 60 is the certificate-verification failure.
    assert "curl: (60)" in done.stdout, done.stdout[-2000:]


@needs_tls
def test_503_over_the_internal_ca_fails_as_503(tmp_path):
    done, url = _run_tls(tmp_path, 503)
    assert done.returncode != 0
    assert f"{url} answered 503, expected one of 200" in done.stdout, done.stdout[-2000:]


@needs_tls
def test_a_ca_alone_verifies_the_urls_own_name(tmp_path):
    # No pinned address: curl --cacert without --resolve, the URL's own IP is the certificate's SAN.
    done, _ = _run_tls(tmp_path, 200, pin=False, url_host="127.0.0.1")
    assert done.returncode == 0, done.stdout[-2000:]


@needs_tls
def test_curl_probe_on_host_runs_on_the_target(tmp_path):
    # Same proof as the uri test: delegated to the executor, a probe passes with a broken target
    # python; run on the target, it cannot.
    broken = "ansible_python_interpreter=/nonexistent/python"
    assert _run_tls(tmp_path, 200, host_vars=broken)[0].returncode == 0
    on_host, _ = _run_tls(tmp_path, 200, host_vars=broken + " health_probe_on_host=true")
    assert on_host.returncode != 0
    assert "answered no response" in on_host.stdout, on_host.stdout[-2000:]
    assert _run_tls(tmp_path, 200, host_vars="health_probe_on_host=true")[0].returncode == 0


def test_a_pinned_address_needs_an_http_url_with_a_host(tmp_path):
    # Declared-contract error: recorded as a failure with the reason, never silently skipped.
    inventory = tmp_path / "inventory.ini"
    inventory.write_text(
        "[demo_svc]\ndemo ansible_connection=local\n\n[demo_svc:vars]\nservice_name=demo\n"
        "health_url=/health\nhealth_resolve_address=127.0.0.1\n"
    )
    done = harness_sandbox.run_playbook(PLAYBOOK, tmp_path, "-e", "target_service=demo_svc", inventory=inventory)
    assert done.returncode != 0
    assert "health_resolve_address needs an http or https health URL with a host, not /health" in done.stdout
    # The contract error is the whole verdict: no probe ran, so there is no "answered" line.
    assert "answered" not in done.stdout
