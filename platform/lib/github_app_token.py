#!/usr/bin/env python3
"""Mint short-lived GitHub credentials from an organisation App.

Why Python and not the shell: the first version of this used ``openssl`` and ``jq``,
on the reasoning that both are everywhere. The orchestrator's container image has
neither, so every run failed with ``openssl: command not found`` — and because the
signing step is inside a ``no_log`` boundary, it surfaced as an unexplained credential
failure rather than a missing binary. Standard library plus ``cryptography`` (already
present wherever Ansible's ``community.hashi_vault`` runs) removes both dependencies.

The credential chain, each step shorter-lived than the last::

    App private key (secret store, never on a runner host)
      -> RS256 assertion   (<=10 min, signed here)
        -> installation token (~1 hour)
          -> registration / remove token (1 hour, single use)

The private key is read from stdin or a file, never from argv, because argv is
world-readable through /proc for the life of the process. Tokens are written to stdout
alone so a caller can capture one without filtering log noise; everything diagnostic
goes to stderr, and no diagnostic ever contains key or token material.

Usage:
    github_app_token.py installation-id     --issuer <id> --org <org> [--key <path>]
    github_app_token.py installation-token  --issuer <id> --org <org> [--key <path>]
    github_app_token.py registration-token   --issuer <id> --org <org> [--key <path>]
    github_app_token.py remove-token         --issuer <id> --org <org> [--key <path>]

--key defaults to '-' (stdin). --issuer is the App's client id (upstream's
recommendation) or its app id; the App's OAuth client secret plays no part here.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

API = os.environ.get("GITHUB_API", "https://api.github.com")


def die(msg: str, code: int = 1) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    raise SystemExit(code)


def _b64url(raw: bytes) -> str:
    """base64url without padding, as a JWT requires."""
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def read_key(path: str) -> bytes:
    if path == "-":
        data = sys.stdin.buffer.read()
        if not data:
            die("no private key on stdin")
        return data
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError as exc:
        die(f"private key not readable: {path} ({exc.strerror})")
    return b""  # unreachable; keeps type checkers quiet


def sign_assertion(issuer: str, key_pem: bytes) -> str:
    if not issuer:
        die("issuer is required (the App's client id, or its app id)", 2)
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
    except ImportError:
        die(
            "the 'cryptography' package is unavailable to this interpreter, so the App "
            "assertion cannot be signed. Install it for the interpreter Ansible uses on "
            "this host."
        )

    try:
        key = serialization.load_pem_private_key(key_pem, password=None)
    except Exception as exc:  # noqa: BLE001 - any parse failure is the same story here
        die(f"the private key could not be parsed as a PEM private key: {exc}")

    if not isinstance(key, rsa.RSAPrivateKey):
        die("the App assertion must be signed with an RSA key; this key is not RSA")

    now = int(time.time())
    # iat is backdated 60s to absorb clock skew against the forge, which rejects an
    # assertion issued in its own future. exp is 9 minutes: the documented ceiling is
    # 10, and sitting on the boundary turns skew into an intermittent auth failure.
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64url(
        json.dumps({"iat": now - 60, "exp": now + 540, "iss": issuer}, separators=(",", ":")).encode()
    )
    signing_input = f"{header}.{payload}".encode()
    sig = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{payload}.{_b64url(sig)}"


def api(path: str, bearer: str, method: str = "GET") -> object:
    req = urllib.request.Request(
        f"{API}{path}",
        method=method,
        headers={
            "Authorization": f"Bearer {bearer}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "agent-cloud-runner-automation",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        # The response body can carry the forge's own explanation, which is far more
        # useful than the status alone. It never contains our credentials.
        detail = ""
        try:
            detail = json.loads(exc.read() or b"{}").get("message", "")
        except Exception:  # noqa: BLE001
            pass
        die(f"{method} {path} failed: HTTP {exc.code}{' — ' + detail if detail else ''}")
    except urllib.error.URLError as exc:
        die(f"{method} {path} failed: {exc.reason}")
    return None


def installation_id(issuer: str, key_pem: bytes, org: str) -> str:
    jwt = sign_assertion(issuer, key_pem)
    installs = api("/app/installations", jwt) or []
    if not installs:
        die(
            "this App has no installations. It exists and its key authenticates, but it "
            f"has not been INSTALLED on any account — install it on '{org}' before a "
            "runner can be registered."
        )
    for inst in installs:
        if (inst.get("account") or {}).get("login") == org:
            return str(inst["id"])
    where = ", ".join(str((i.get("account") or {}).get("login")) for i in installs)
    die(f"this App is installed, but not on '{org}'. Installed on: {where}")
    return ""


def installation_token(issuer: str, key_pem: bytes, org: str) -> str:
    jwt = sign_assertion(issuer, key_pem)
    iid = installation_id(issuer, key_pem, org)
    data = api(f"/app/installations/{iid}/access_tokens", jwt, method="POST") or {}
    token = data.get("token")
    if not token:
        die("no token in the installation response")
    return token


def runner_token(issuer: str, key_pem: bytes, org: str, kind: str) -> str:
    itok = installation_token(issuer, key_pem, org)
    data = api(f"/orgs/{org}/actions/runners/{kind}", itok, method="POST") or {}
    token = data.get("token")
    if not token:
        die(
            f"no token in the {kind} response — does the App grant Organization "
            "'Self-hosted runners: read & write'?"
        )
    return token


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "action",
        choices=["installation-id", "installation-token", "registration-token", "remove-token"],
    )
    ap.add_argument("--issuer", required=True, help="App client id (recommended) or app id")
    ap.add_argument("--org", required=True)
    ap.add_argument("--key", default="-", help="private key path, or '-' for stdin (default)")
    args = ap.parse_args()

    key_pem = read_key(args.key)
    if args.action == "installation-id":
        out = installation_id(args.issuer, key_pem, args.org)
    elif args.action == "installation-token":
        out = installation_token(args.issuer, key_pem, args.org)
    elif args.action == "registration-token":
        out = runner_token(args.issuer, key_pem, args.org, "registration-token")
    else:
        out = runner_token(args.issuer, key_pem, args.org, "remove-token")
    sys.stdout.write(out)


if __name__ == "__main__":
    main()
