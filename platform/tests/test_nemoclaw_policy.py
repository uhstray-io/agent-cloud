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


@pytest.mark.skipif(shutil.which("bao") is None, reason="needs the OpenBao CLI")
def test_openbao_itself_resolves_the_token_path_to_deny(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    addr = f"http://127.0.0.1:{port}"
    server = subprocess.Popen(
        ["bao", "server", "-dev", "-dev-root-token-id=synthetic-root", f"-dev-listen-address=127.0.0.1:{port}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    env = {"BAO_ADDR": addr, "BAO_TOKEN": "synthetic-root", "HOME": str(tmp_path),
           "PATH": os.environ["PATH"]}

    def bao(*args):
        return subprocess.run(["bao", *args], env=env, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=30)
    try:
        for _ in range(100):
            if bao("status").returncode == 0:
                break
            time.sleep(0.1)
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
