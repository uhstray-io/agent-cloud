#!/usr/bin/env python3
"""RFC 6238 TOTP, stdlib only — SHA-1, 30s step, 6 digits (n8n's defaults).

The base32 seed arrives on STDIN (never argv — argv is world-readable in ps).
Prints the current 6-digit code. TOTP_TIME (env, unix seconds) overrides the
clock so the RFC 6238 Appendix B vectors are testable; production callers
leave it unset.

Used by store-n8n-api-key.yml to log in as an MFA-enabled owner: n8n's
/rest/login accepts an optional mfaCode (verified at n8n@2.25.7,
packages/@n8n/api-types .../login-request.dto.ts).
"""
import base64
import hmac
import os
import struct
import sys
import time


def totp(seed_b32: str, now: int, step: int = 30, digits: int = 6) -> str:
    normalized = seed_b32.strip().replace(" ", "").upper()
    normalized += "=" * (-len(normalized) % 8)
    key = base64.b32decode(normalized)
    counter = struct.pack(">Q", int(now) // step)
    digest = hmac.new(key, counter, "sha1").digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


if __name__ == "__main__":
    seed = sys.stdin.read()
    now = int(os.environ.get("TOTP_TIME", time.time()))
    print(totp(seed, now))
