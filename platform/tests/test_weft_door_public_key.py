"""The front door's public key, derived from its signing key, is the one weft's front door uses.

Every workspace's weft.toml names the front door's public key. A wrong one looks like nothing at
all: the front door signs, the workspace checks the signature against the wrong key, and every
sign-in is refused. So the derivation is checked against a published Ed25519 vector (RFC 8032
section 7.1, test 1), the same algorithm weft's `handoff::Issuer` uses for a 32-byte secret.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("cryptography")

HELPER = Path(__file__).resolve().parents[1] / "services/weft/deployment/lib/door_public_key.py"
RFC8032_SEED = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
RFC8032_PUBLIC = "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"


def load():
    spec = importlib.util.spec_from_file_location("door_public_key", HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_public_key_is_the_ed25519_public_half_of_the_signing_key():
    assert load().public_hex(RFC8032_SEED) == RFC8032_PUBLIC, (
        "a workspace configured with this public key would refuse every sign-in the front door signs"
    )


def test_the_key_is_read_from_stdin_and_only_the_public_half_is_printed():
    run = subprocess.run(
        [sys.executable, str(HELPER)], input=RFC8032_SEED + "\n", capture_output=True, text=True, check=True
    )
    assert run.stdout.strip() == RFC8032_PUBLIC
    assert RFC8032_SEED not in run.stdout + run.stderr, "the signing key must never be echoed"


@pytest.mark.parametrize("bad", ["", "zz" * 32, RFC8032_SEED[:-2], RFC8032_SEED.upper()])
def test_a_key_that_is_not_64_lowercase_hex_is_refused_without_echoing_it(bad):
    run = subprocess.run([sys.executable, str(HELPER)], input=bad, capture_output=True, text=True)
    assert run.returncode != 0, "a malformed key must not produce a public key somebody then trusts"
    assert run.stdout == ""
    if bad:
        assert bad not in run.stderr, "a refusal names no part of the input"
