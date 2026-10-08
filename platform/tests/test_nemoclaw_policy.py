"""nemoclaw-read.hcl never grants the Semaphore API token.

Whoever holds that token can launch any template with any extra var, and a templated extra
var runs code on the Semaphore runner (plan 01, "Launch permission is runner access"). The
structural test runs everywhere; the second asks a real OpenBao dev server for the token's
capabilities and is skipped where no `bao` binary is installed (CI does not install one).
"""

import os
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

POLICY = Path(__file__).resolve().parents[2] / "platform/services/openbao/deployment/config/policies/nemoclaw-read.hcl"
DENIED = ("secret/data/services/semaphore", "secret/metadata/services/semaphore")


def blocks(text):
    """{path: [capabilities]} for every `path "..." { capabilities = [...] }` block."""
    found = re.findall(r'path\s+"([^"]+)"\s*\{\s*capabilities\s*=\s*\[([^\]]*)\]', text)
    return {path: re.findall(r'"([^"]+)"', caps) for path, caps in found}


def test_the_semaphore_token_paths_are_denied_outright():
    rules = blocks(POLICY.read_text())
    for path in DENIED:
        assert rules.get(path) == ["deny"], path
    # The broad grant is still there, which is why the exact deny is needed at all.
    assert "read" in rules["secret/data/services/*"]


def _candidate_port():
    """A port free a moment ago. Only a candidate: it is released before bao binds it, so
    another process (a parallel test worker) can take it in between."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _bao_runner(env):
    def bao(*args, timeout=30):
        return subprocess.run(["bao", *args], env=env, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=timeout)
    return bao


def _answers(bao):
    # Short timeout: whoever took the port may accept and never reply, and a 30 s hang
    # here would hide that our own server has already exited.
    try:
        return bao("status", timeout=2).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def start_dev_server(tmp_path, attempts=5):
    """Start `bao server -dev` and return (server, bao) once it answers.

    The dev server cannot bind port 0 usefully: it then reports its API address as `:0`,
    and it also binds a cluster listener on port+1. So a port is chosen, the server is
    started on it, and if the server exits before answering (its port or port+1 was
    taken), a fresh port is tried.
    """
    for _ in range(attempts):
        port = _candidate_port()
        env = {"BAO_ADDR": f"http://127.0.0.1:{port}", "BAO_TOKEN": "synthetic-root",
               "HOME": str(tmp_path), "PATH": os.environ["PATH"]}
        server = subprocess.Popen(
            ["bao", "server", "-dev", "-dev-root-token-id=synthetic-root",
             f"-dev-listen-address=127.0.0.1:{port}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        bao = _bao_runner(env)
        for _ in range(100):
            if server.poll() is not None:
                break  # exited: lost the port race, try another
            if _answers(bao):
                return server, bao
            time.sleep(0.1)
        if server.poll() is None:
            server.terminate()
            server.wait(timeout=10)
            pytest.fail("bao dev server started but never answered")
    pytest.fail(f"bao dev server failed to bind in {attempts} attempts")


@pytest.mark.skipif(shutil.which("bao") is None, reason="needs the OpenBao CLI")
def test_openbao_itself_resolves_the_token_path_to_deny(tmp_path):
    server, bao = start_dev_server(tmp_path)
    try:
        assert bao("policy", "write", "nemoclaw-read", str(POLICY)).returncode == 0
        token = bao("token", "create", "-policy=nemoclaw-read", "-field=token").stdout.strip()
        assert token

        def caps(path):
            return bao("token", "capabilities", token, path).stdout.strip()
        for path in DENIED:
            assert caps(path) == "deny", path
        # A sibling service secret is still readable: the deny is exact, not a wider cut.
        assert caps("secret/data/services/netbox") == "read"
    finally:
        server.terminate()
        server.wait(timeout=10)
