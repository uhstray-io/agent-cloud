"""Check a test-built certificate under strict X.509 rules, the way Python 3.13+ does.

Python 3.13+ ssl.create_default_context() sets VERIFY_X509_STRICT, which rejects a CA-signed certificate that has
no Authority Key Identifier. CI runs Python 3.12, where the flag is off, so a fixture that omits the extension
passes CI. Every fixture builder that signs a certificate calls assert_strict_chain on the result, so the
omission fails on every interpreter, using the same openssl verification that reports the error.
"""

import subprocess


def assert_strict_chain(ca, cert, untrusted=None):
    """Assert that `cert` verifies under openssl's strict rules against the trust anchor `ca`.

    `untrusted` is an intermediate that `cert` chains through, when the fixture has one.
    """
    command = ["openssl", "verify", "-x509_strict", "-CAfile", str(ca)]
    if untrusted is not None:
        command += ["-untrusted", str(untrusted)]
    result = subprocess.run([*command, str(cert)], capture_output=True, text=True)
    assert result.returncode == 0, (
        f"{cert} does not verify under openssl -x509_strict against {ca}: {(result.stdout + result.stderr).strip()}"
    )
