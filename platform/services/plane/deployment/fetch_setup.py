#!/usr/bin/env python3
"""Fetch Plane's Commercial setup files for one release and verify them, writing nothing.

Plane's EULA (plane.so/legals/eula, section 2.1) forbids redistributing its files, so nothing
Plane wrote is committed here: the deploy fetches the release's docker-compose.yml and
variables.env from the public setup endpoint on every run and checks each file against the
sha256 pinned in deploy-plane.yml. The zip's own digest is not pinned: the endpoint stamps entry
dates at request time, so two downloads of one release differ (design.md, spike 0.1).

Only the two named members are read, by exact name. Both are verified in memory and printed
as one JSON object; nothing is written to disk, so a dry run fetches and verifies for real
without a write to guard. A changed or tampered release prints nothing and exits non-zero.

Usage: fetch_setup.py <version> <compose-sha256> <variables-sha256>
"""

import hashlib
import io
import json
import sys
import urllib.parse
import urllib.request
import zipfile

SETUP_URL = "https://prime.plane.so/api/v2/setup/?version={}"
MEMBERS = ("docker-compose.yml", "variables.env")


def verified_members(blob, expected):
    """Return {name: bytes} for MEMBERS, or raise ValueError naming the first mismatch."""
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        out = {}
        for name in MEMBERS:
            data = z.read(name)  # KeyError when the release lacks the member
            digest = hashlib.sha256(data).hexdigest()
            if digest != expected[name]:
                raise ValueError(f"{name}: sha256 {digest} does not match the pinned {expected[name]}")
            out[name] = data
        return out


def main(argv):
    if len(argv) != 4:
        sys.exit(__doc__.strip().splitlines()[-1])
    version, compose_sha, vars_sha = argv[1:]
    url = SETUP_URL.format(urllib.parse.quote(version, safe=""))
    with urllib.request.urlopen(url, timeout=60) as resp:  # noqa: S310 - fixed https host
        blob = resp.read()
    try:
        files = verified_members(blob, {MEMBERS[0]: compose_sha, MEMBERS[1]: vars_sha})
    except (KeyError, ValueError, zipfile.BadZipFile) as exc:
        sys.exit(f"Refusing Plane {version} setup files: {exc}")
    print(json.dumps({name: data.decode("utf-8") for name, data in files.items()}))


if __name__ == "__main__":
    main(sys.argv)
