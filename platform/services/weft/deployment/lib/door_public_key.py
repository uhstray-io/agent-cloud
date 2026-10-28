#!/usr/bin/env python3
"""Print the front door's public key, given its signing key on stdin.

    python3 door_public_key.py < <file holding the 64-hex signing key>

The front door signs every workspace's sign-in with an Ed25519 key whose secret is 32 bytes of hex
(weft's `handoff::Issuer::from_secret_hex`). Each workspace's weft.toml names the matching public
key, so deploy-weft-door.yml derives it here, on the controller, and stores it beside the signing
key in OpenBao for the workspaces' deploys to read. The public half is not secret; the input is.

The key arrives on stdin and never as an argument, so no process listing shows it. Nothing but the
public key is printed, and a refusal names no part of the input.

Runs on the Semaphore controller only: `cryptography` is a declared controller dependency
(platform/requirements-controller.txt), never installed on a service VM.
"""

import re
import sys

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


def public_hex(secret_hex: str) -> str:
    secret_hex = secret_hex.strip()
    if not re.fullmatch(r"[0-9a-f]{64}", secret_hex):
        raise ValueError("the signing key is not 32 bytes of lowercase hex")
    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(secret_hex))
    return key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()


def main() -> int:
    try:
        print(public_hex(sys.stdin.read()))
    except ValueError as why:
        print(f"door_public_key: {why}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
