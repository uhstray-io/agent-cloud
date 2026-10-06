"""No committed file names the site's real accounts.

This repository is public; real usernames, token identities and machine paths belong in the
private site-config repository. A promotion review (PR 447) found the operator's account in a
Proxmox token fallback, in gateway client fixtures and in path-derived project ids. Each shape is
listed here so it cannot come back. The byline and contact address are allowed: they are the
author's published identity, not a site account.

It reads the committed tree (HEAD), not the working tree: a local tool rewriting a tracked
file must not fail it, and an edit is judged once it is committed, which is what a push sends.

The patterns are assembled from fragments so this file does not match itself.
"""

import os
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
_NAME = "st" + "ray"
FORBIDDEN = {
    "Proxmox token identity": re.compile(rf"\b{_NAME}@(pve|pam)\b"),
    "gateway client key field": re.compile(rf"\bclient_{_NAME}\b"),
    "path-derived machine id": re.compile(rf"\bUsers-{_NAME}-"),
}


def _git(*args, stdin=None):
    # Git exports GIT_DIR and friends to hooks; clear them so this reads THIS checkout.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(["git", "-C", str(REPO), *args], env=env, check=True, capture_output=True,
                          input=stdin).stdout


def committed(rev="HEAD"):
    """(path, bytes) for every blob in `rev`. The committed tree is what a push publishes, so
    working-tree churn (an indexer rewriting a tracked artifact, an unstaged edit) neither
    trips this nor hides from it once committed."""
    entries = [e.split("\t", 1) for e in _git("ls-tree", "-r", "-z", rev).decode().split("\0") if e]
    blobs = [(meta.split()[2], path) for meta, path in entries if meta.split()[1] == "blob"]
    stream = _git("cat-file", "--batch", stdin="".join(f"{oid}\n" for oid, _ in blobs).encode())
    out, pos = [], 0
    for _oid, path in blobs:
        header_end = stream.index(b"\n", pos)
        size = int(stream[pos:header_end].split()[2])
        out.append((path, stream[header_end + 1:header_end + 1 + size]))
        pos = header_end + 1 + size + 1
    return out


def offenders(files):
    found = []
    for path, data in files:
        try:
            text = data.decode()
        except UnicodeDecodeError:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            found += [f"{path}:{n} ({label})" for label, rx in FORBIDDEN.items() if rx.search(line)]
    return found


def test_no_committed_file_names_a_site_account():
    files = committed()
    assert len(files) > 100  # the tree was read, not an empty listing
    assert not offenders(files)


def test_each_shape_is_caught():
    sample = f"'{_NAME}@pve!x'\nclient_{_NAME}: k\nUsers-{_NAME}-Documents\n".encode()
    assert len(offenders([("sample.txt", sample)])) == len(FORBIDDEN)
