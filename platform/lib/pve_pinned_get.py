#!/usr/bin/env python3
"""Talk to the Proxmox API over TLS pinned to one leaf certificate, and send the admin token only after the pin holds.

Why. The read-only root-CA play used to send the Proxmox admin token over a connection it did not verify
(validate_certs false). This helper is the only thing that now carries the token, and it checks the pin on
the SAME TLS connection that will carry the request: after the handshake, before any request byte is written.
The pin is the SHA-256 of the API's leaf certificate, confirmed by the operator out of band. A mismatch exits
3 and sends nothing, so the token never crosses a connection whose certificate the operator did not confirm.

Verification. TLS chain and hostname checks are off on purpose: the pin replaces them. The pin is compared to
sha256 of the DER certificate that the handshake presented. Python's getpeercert(binary_form=True) returns that
DER whenever the server sent a certificate, independent of verify_mode
(https://docs.python.org/3/library/ssl.html#ssl.SSLSocket.getpeercert), so CERT_NONE does not change what is
compared.

Transport. http.client, not urllib: urllib honours proxy variables from the environment, which would route the
token through a proxy the play did not name. https only; no credentials in the URL; no query or fragment.

Credentials. The token id and secret arrive on stdin, one per line, never in argv (argv is world-readable through
/proc for the life of the process). The Authorization header is built here, so no playbook text carries it.

Usage:
    pve_pinned_get.py fingerprint --url https://<host>[:<port>]
        Handshake only. Prints the leaf certificate's SHA-256 as upper-case colon hex. Sends no HTTP request.
    pve_pinned_get.py get --url https://<host>[:<port>] --path /api2/json/... --sha256 <pin>
        Stdin: token id line, then token secret line. Handshake, compare the pin, then one GET. On 200 the
        response body goes to stdout. The pin may be given with or without colons, in either case.

Exit codes:
    0  success (fingerprint printed, or the 200 body printed)
    2  refused input: not https, credentials or path in the URL, malformed pin or path, malformed stdin
    3  pin mismatch; stderr names the pinned and the observed fingerprint; no request byte was sent
    4  the API answered with a status other than 200; stderr names the status only, never the body
    5  connection, TLS or certificate failure before the request was sent (or during it)

Diagnostics go to stderr and never contain the token, the secret or a response body. The fingerprints are public.

Coverage. The callers do not build a PVEAPIToken string, so test_pve_token_id_required.py's table (which keys on
that header) does not reach them. read-proxmox-root-ca.yml is the one caller today. It asserts the stored token id
before its first get (test_read_proxmox_root_ca.py, test_the_token_id_is_asserted_before_the_first_request), and
read_credentials() refuses an empty id line on its own (exit 2, nothing sent). A new caller must do the same.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import ssl
import sys
from typing import NoReturn
from urllib.parse import urlsplit

TIMEOUT_SECONDS = 30
SHA256_HEX_LEN = 64
HEX_DIGITS = frozenset("0123456789ABCDEF")

EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_PIN_MISMATCH = 3
EXIT_HTTP_STATUS = 4
EXIT_TRANSPORT = 5


def die(msg: str, code: int) -> NoReturn:
    print(f"ERROR: {msg}", file=sys.stderr)
    raise SystemExit(code)


def normalise_pin(text: str) -> str:
    """Upper-case hex digits, colons removed. Anything that is not a SHA-256 digest is refused."""
    flat = text.replace(":", "").strip().upper()
    if len(flat) != SHA256_HEX_LEN or not HEX_DIGITS.issuperset(flat):
        die("the pin is not a SHA-256 digest (64 hex digits, colons optional)", EXIT_REFUSED)
    return flat


def colon_form(flat: str) -> str:
    return ":".join(flat[i:i + 2] for i in range(0, len(flat), 2))


def parse_base_url(url: str) -> tuple[str, int]:
    """Return (host, port) of an https base URL. Refuses everything else without echoing the URL."""
    parts = urlsplit(url)
    if parts.scheme != "https":
        die("the URL must be https: the token and the response must not cross cleartext", EXIT_REFUSED)
    if parts.username is not None or parts.password is not None:
        die("the URL must not carry credentials; the token is read from stdin", EXIT_REFUSED)
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        die("the URL must be a base (https://<host>[:<port>]); the API path is given with --path", EXIT_REFUSED)
    if not parts.hostname:
        die("the URL names no host", EXIT_REFUSED)
    try:
        port = parts.port or 443
    except ValueError:
        die("the URL port is not a number", EXIT_REFUSED)
    return parts.hostname, port


def pinned_context() -> ssl.SSLContext:
    """TLS client context whose only check is the caller's SHA-256 pin, made on the same connection.

    check_hostname must be cleared before verify_mode can be CERT_NONE, or Python raises ValueError.
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # the SHA-256 pin, checked before any request byte, is the verification
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def open_connection(host: str, port: int) -> http.client.HTTPSConnection:
    return http.client.HTTPSConnection(host, port, context=pinned_context(), timeout=TIMEOUT_SECONDS)


def observe_leaf(conn: http.client.HTTPSConnection) -> bytes:
    """Complete the TLS handshake and return the presented leaf certificate's DER. Writes no HTTP bytes."""
    try:
        conn.connect()
    except (OSError, ValueError):
        die("TLS handshake failed before any request was sent", EXIT_TRANSPORT)
    # getpeercert(binary_form=True): DER of the certificate the server presented, None if it presented none.
    # https://docs.python.org/3/library/ssl.html#ssl.SSLSocket.getpeercert
    der = conn.sock.getpeercert(binary_form=True) if conn.sock is not None else None
    if not der:
        die("the server presented no certificate; nothing was sent", EXIT_TRANSPORT)
    return der


def digest_of(der: bytes) -> str:
    return hashlib.sha256(der).hexdigest().upper()


def read_credentials() -> tuple[str, str]:
    lines = sys.stdin.read().splitlines()
    if len(lines) != 2 or not lines[0] or not lines[1]:
        die("stdin must carry the token id and the token secret, one per line; nothing was sent", EXIT_REFUSED)
    return lines[0], lines[1]


def cmd_fingerprint(args: argparse.Namespace) -> int:
    host, port = parse_base_url(args.url)
    conn = open_connection(host, port)
    try:
        der = observe_leaf(conn)
    finally:
        conn.close()
    print(colon_form(digest_of(der)))
    return EXIT_OK


def cmd_get(args: argparse.Namespace) -> int:
    host, port = parse_base_url(args.url)
    pin = normalise_pin(args.sha256)
    if not args.path.startswith("/") or any(ch.isspace() for ch in args.path):
        die("--path must be an absolute API path with no whitespace; nothing was sent", EXIT_REFUSED)
    token_id, secret = read_credentials()
    conn = open_connection(host, port)
    try:
        observed = digest_of(observe_leaf(conn))
        if observed != pin:
            die(
                "the API certificate does not match the pin. "
                f"pinned {colon_form(pin)}, observed {colon_form(observed)}. No request was sent.",
                EXIT_PIN_MISMATCH,
            )
        conn.request(
            "GET",
            args.path,
            headers={
                "Accept": "application/json",
                "Authorization": f"PVEAPIToken={token_id}={secret}",
            },
        )
        resp = conn.getresponse()
        body = resp.read()
        status = resp.status
    except (OSError, ValueError, http.client.HTTPException) as exc:
        die(f"the request failed: {type(exc).__name__}", EXIT_TRANSPORT)
    finally:
        conn.close()
    if status != 200:
        die(f"HTTP {status}", EXIT_HTTP_STATUS)
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Proxmox API read over a leaf-certificate pin.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    fp = sub.add_parser("fingerprint", help="print the leaf SHA-256; no HTTP request")
    fp.add_argument("--url", required=True, help="https://<host>[:<port>]")

    gp = sub.add_parser("get", help="pin-check the connection, then one GET with the token from stdin")
    gp.add_argument("--url", required=True, help="https://<host>[:<port>]")
    gp.add_argument("--path", required=True, help="absolute API path, e.g. /api2/json/nodes")
    gp.add_argument("--sha256", required=True, help="pinned leaf SHA-256 (colons and case optional)")

    args = parser.parse_args(argv)
    if args.cmd == "fingerprint":
        return cmd_fingerprint(args)
    return cmd_get(args)


if __name__ == "__main__":
    raise SystemExit(main())
