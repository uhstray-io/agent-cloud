"""Check a test-built certificate under strict X.509 rules, the way Python 3.13+ does.

Python 3.13+ ssl.create_default_context() sets VERIFY_X509_STRICT, which rejects a CA-signed certificate that has
no Authority Key Identifier. CI runs Python 3.12, where the flag is off, so a fixture that omits the extension
passes CI. Every fixture builder that signs a certificate calls assert_strict_chain on the result, which runs
`openssl verify -x509_strict` on any Python version. Whether a missing extension line shows up depends on the
openssl CLI: 3.0 adds key identifiers to an `-extfile` signing by itself, while 4.1 does not, so the explicit
lines keep the fixtures correct across builds and this check proves the certificate that was really issued.
"""

import subprocess

import harness_sandbox


def assert_strict_chain(ca, cert, untrusted=None):
    """Assert that `cert` verifies under openssl's strict rules against the trust anchor `ca`.

    `untrusted` is an intermediate that `cert` chains through, when the fixture has one.
    """
    command = ["openssl", "verify", "-x509_strict", "-CAfile", str(ca)]
    if untrusted is not None:
        command += ["-untrusted", str(untrusted)]
    result = subprocess.run([*command, str(cert)], capture_output=True, text=True,
                            timeout=harness_sandbox.DEFAULT_TIMEOUT_SECONDS)
    assert result.returncode == 0, (
        f"{cert} does not verify under openssl -x509_strict against {ca}: {(result.stdout + result.stderr).strip()}"
    )
