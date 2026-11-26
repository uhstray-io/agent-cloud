"""Every openssl -extfile signing site in platform/tests is followed by a strict chain check.

A fixture that signs a certificate with `openssl x509 -req -extfile` must check the result with
assert_strict_chain (platform/tests/strict_chain.py), which runs `openssl verify -x509_strict`. Python 3.13+
ssl.create_default_context() sets VERIFY_X509_STRICT and rejects a CA-signed certificate without an Authority
Key Identifier; CI on Python 3.12 does not, so a fixture that omits the extension passes there and fails only on
3.13+. The check itself is the behavioural proof. This file is the per-site convention guard: it counts the
signing sites in each file and requires at least one strict check per site, so a new fixture that signs without
the check fails here. It is a count, not a position: a check written in a comment still counts.
"""

import re
from pathlib import Path

TESTS = Path(__file__).resolve().parent
THIS_FILE = Path(__file__).resolve()
SCANNED_SUFFIXES = (".py", ".bats", ".sh", ".cnf")
SIGNING_SITE = re.compile(r"-extfile")
STRICT_CHECK = re.compile(r"\bassert_strict_chain\(")


def _unchecked_signers(paths):
    """Map each file whose signing sites outnumber its strict checks to (signing sites, strict checks)."""
    unchecked = {}
    for path in paths:
        text = path.read_text(encoding="utf-8")
        sites, checks = len(SIGNING_SITE.findall(text)), len(STRICT_CHECK.findall(text))
        if sites > checks:
            unchecked[path] = (sites, checks)
    return unchecked


def test_every_extfile_signing_site_is_followed_by_a_strict_chain_check():
    files = [p for p in TESTS.rglob("*") if p.is_file() and p.suffix in SCANNED_SUFFIXES and p != THIS_FILE]
    assert files, "found no files under platform/tests to scan; the scan is broken"
    signers = [p for p in files if SIGNING_SITE.search(p.read_text(encoding="utf-8"))]
    assert signers, "found no file that signs with -extfile; the scan is broken or every such fixture was removed"
    unchecked = _unchecked_signers(signers)
    assert not unchecked, (
        "these files sign certificates with openssl -extfile without a strict chain check for every signing site "
        "(signing sites, strict checks): "
        + ", ".join(
            f"{p.relative_to(TESTS).as_posix()} ({sites}, {checks})" for p, (sites, checks) in unchecked.items()
        )
        + ". Call assert_strict_chain(ca, cert) on each signed certificate. Python 3.13+ "
        "ssl.create_default_context() sets VERIFY_X509_STRICT and rejects a CA-signed certificate without an "
        "Authority Key Identifier with 'Missing Authority Key Identifier'; CI on Python 3.12 cannot see it."
    )


def test_the_count_flags_a_signing_site_without_a_check(tmp_path):
    two_sites_one_check = tmp_path / "test_two_sites.py"
    two_sites_one_check.write_text(
        'a = ["-extfile", "a.ext"]\nb = ["-extfile", "b.ext"]\nassert_strict_chain(ca, a)\n', encoding="utf-8"
    )
    balanced = tmp_path / "test_balanced.py"
    balanced.write_text('a = ["-extfile", "a.ext"]\nassert_strict_chain(ca, a)\n', encoding="utf-8")
    assert _unchecked_signers([two_sites_one_check, balanced]) == {two_sites_one_check: (2, 1)}
