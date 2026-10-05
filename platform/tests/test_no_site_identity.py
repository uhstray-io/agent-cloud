"""No committed file names the site's real accounts.

This repository is public; real usernames, token identities and machine paths belong in the
private site-config repository. A promotion review (PR 447) found the operator's account in a
Proxmox token fallback, in gateway client fixtures and in path-derived project ids. Each shape is
listed here so it cannot come back. The byline and contact address are allowed: they are the
author's published identity, not a site account.

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


def _tracked():
    # Git exports GIT_DIR and friends to hooks; clear them so this reads THIS checkout.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    out = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z"], env=env, check=True, capture_output=True)
    return [REPO / p for p in out.stdout.decode().split("\0") if p]


def offenders(paths, base=REPO):
    found = []
    for path in paths:
        try:
            text = path.read_text()
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            found += [f"{path.relative_to(base)}:{n} ({label})" for label, rx in FORBIDDEN.items() if rx.search(line)]
    return found


def test_no_tracked_file_names_a_site_account():
    assert not offenders(_tracked())


def test_each_shape_is_caught(tmp_path):
    sample = tmp_path / "sample.txt"
    sample.write_text(f"'{_NAME}@pve!x'\nclient_{_NAME}: k\nUsers-{_NAME}-Documents\n")
    assert len(offenders([sample], base=tmp_path)) == len(FORBIDDEN)
