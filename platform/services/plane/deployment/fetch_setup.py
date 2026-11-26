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

After the compose file's digest matches, its short image names are qualified with their
registry (`makeplane/x` -> `docker.io/makeplane/x`, `postgres:15` -> `docker.io/library/postgres:15`).
Plane's file uses Docker Hub short names; a host whose registries.conf defines no search
registry (Ubuntu 24.04's podman, the production Plane VM) refuses them, and the platform qualifies
images rather than relying on search registries. The digest is checked on Plane's bytes as
published; only the printed copy is qualified. Any `image:` line left without a registry after
that (a form the rewrite does not handle, such as an anchor or a trailing comment) refuses the
fetch, so the deploy stops here rather than at the pull. platform/tests/test_plane_fetch_setup.py
guards this; the platform's test_compose_image_qualified.py sees only committed compose files.

Usage: fetch_setup.py <version> <compose-sha256> <variables-sha256>
"""

import hashlib
import io
import json
import re
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


IMAGE_LINE = re.compile(r"^([ \t]*image:[ \t]*)(\S+)[ \t]*$", re.M)


def registry_named(ref):
    first, slash, _ = ref.partition("/")
    return bool(slash) and ("." in first or ":" in first or first == "localhost")


def qualify(ref):
    """A Docker Hub short name with its registry; a name that already has one is left alone.

    A quoted value keeps its quotes. A value whose first path component is a variable, or that is
    a YAML anchor or alias, is left as it is (and then refused by unqualified(), not guessed at).
    """
    quote = ref[0] if ref[:1] in ("'", '"') and ref[-1:] == ref[:1] and len(ref) > 1 else ""
    bare = ref[1:-1] if quote else ref
    if bare[:1] in ("&", "*") or "${" in bare.partition("/")[0] or registry_named(bare):
        return ref
    return quote + (f"docker.io/{bare}" if "/" in bare else f"docker.io/library/{bare}") + quote


def qualify_images(compose):
    return IMAGE_LINE.sub(lambda m: m.group(1) + qualify(m.group(2)), compose)


# Any `image:` key, in any form the rewrite above may not handle (list item, comment, CRLF, anchor).
ANY_IMAGE = re.compile(r"^[ \t]*(?:-[ \t]+)?image:[ \t]*([^\r\n]*?)[ \t]*(?:#[^\r\n]*)?\r?$", re.M)


def unqualified(compose):
    """The image values still without a registry: each one would fail a pull on such a host."""
    bad = []
    for value in ANY_IMAGE.findall(compose):
        bare = value.strip("'\"")
        if not (registry_named(bare) or bare.partition("/")[0].startswith("${")):
            bad.append(value)
    return bad


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
    out = {name: data.decode("utf-8") for name, data in files.items()}
    out[MEMBERS[0]] = qualify_images(out[MEMBERS[0]])
    left = unqualified(out[MEMBERS[0]])
    if left:
        sys.exit(f"Refusing Plane {version} setup files: image references without a registry: {left}")
    print(json.dumps(out))


if __name__ == "__main__":
    main(sys.argv)
