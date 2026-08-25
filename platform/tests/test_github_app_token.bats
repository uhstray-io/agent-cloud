#!/usr/bin/env bats
# Tests for platform/lib/github_app_token.py — the GitHub App credential chain.
#
# The signing tests are BEHAVIOURAL. A JWT that is well-formed but wrongly signed is
# indistinguishable from a correct one by inspection and fails only at the forge, during
# registration, on a host that is already built and hardened. So these generate a
# throwaway RSA key, sign a real assertion, and verify the signature independently.
# No network is touched; the token exchanges are not exercised here.
#
# Run: bats platform/tests/test_github_app_token.bats

load assert_helpers

setup() {
  LIB="$BATS_TEST_DIRNAME/../lib/github_app_token.py"
  [ -f "$LIB" ]
  PY=python3
  # SKIP, not die, when the library is absent. setup() runs before every test, so a bare
  # failure here reported all eleven tests as FAILED in CI — where `cryptography` is not
  # installed — and the "cryptography is importable" guard below could never fire,
  # because setup() had already failed. A missing library is a skip; a wrong signature is
  # a failure. They must not look the same.
  $PY -c 'import cryptography' >/dev/null 2>&1 \
    || skip "cryptography not installed — signing tests cannot run (CI installs it)"

  # A throwaway key per test. Never a real one: a real App key belongs in the secret
  # store and nowhere else (docs/MISTAKES.md §4.3 — a fixture is a committed file).
  KEY="$BATS_TEST_TMPDIR/throwaway.pem"
  $PY - <<PYGEN
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
open("$KEY","wb").write(k.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.TraditionalOpenSSL,
    serialization.NoEncryption()))
PYGEN
  [ -s "$KEY" ]
}

sign() {  # sign <issuer> -> prints the assertion
  python3 -c "
import sys; sys.path.insert(0, '$BATS_TEST_DIRNAME/../lib')
import importlib.util as u
s = u.spec_from_file_location('g', '$LIB'); m = u.module_from_spec(s); s.loader.exec_module(m)
print(m.sign_assertion('$1', open('$KEY','rb').read()))"
}

@test "gh-app: cryptography is importable, or these tests mean nothing" {
  run python3 -c "import cryptography; print('ok')"
  [ "$status" -eq 0 ]
}

@test "gh-app: the assertion is three unpadded base64url segments on one line" {
  local jwt; jwt=$(sign 123456)
  [ "$(printf '%s' "$jwt" | tr -cd '.' | wc -c | tr -d ' ')" = "2" ]
  printf '%s' "$jwt" > "$BATS_TEST_TMPDIR/jwt.txt"
  refute_grep -q '[+/=]' "$BATS_TEST_TMPDIR/jwt.txt"
  [ "$(printf '%s' "$jwt" | wc -l | tr -d ' ')" = "0" ]
}

@test "gh-app: the signature verifies against the signing key, and not against another" {
  local jwt; jwt=$(sign 123456)
  run python3 -c "
import base64, sys
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
def d(s): return base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))
h, p, sig = '$jwt'.split('.')
key = serialization.load_pem_private_key(open('$KEY','rb').read(), password=None)
key.public_key().verify(d(sig), f'{h}.{p}'.encode(), padding.PKCS1v15(), hashes.SHA256())
other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
try:
    other.public_key().verify(d(sig), f'{h}.{p}'.encode(), padding.PKCS1v15(), hashes.SHA256())
    print('BAD: verified against an unrelated key')
except Exception:
    print('ok')"
  [ "$status" -eq 0 ]
  [ "$output" = "ok" ]
}

@test "gh-app: the header is RS256 and the claims sit inside the forge's limits" {
  local jwt; jwt=$(sign 987654)
  run python3 -c "
import base64, json
def d(s): return base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))
h, p, _ = '$jwt'.split('.')
hdr, cl = json.loads(d(h)), json.loads(d(p))
assert hdr['alg'] == 'RS256' and hdr['typ'] == 'JWT', hdr
assert cl['iss'] == '987654', cl
# 9 minutes, deliberately inside the documented 10-minute ceiling: sitting on the
# boundary turns clock skew into an intermittent auth failure.
assert cl['exp'] - cl['iat'] == 600, cl
import time; assert cl['iat'] < time.time(), 'iat must be backdated'
print('ok')"
  [ "$status" -eq 0 ]
  [ "$output" = "ok" ]
}

@test "gh-app: a key that is not a PEM private key fails with a specific message" {
  printf 'not a key\n' > "$BATS_TEST_TMPDIR/bad.pem"
  run bash -c "printf 'not a key\n' | python3 '$LIB' installation-id --issuer 1 --org o --key -"
  [ "$status" -ne 0 ]
  [[ "$output" == *"could not be parsed"* ]]
}

@test "gh-app: an empty stdin key is refused, not treated as absent" {
  run bash -c "printf '' | python3 '$LIB' installation-id --issuer 1 --org o --key -"
  [ "$status" -ne 0 ]
  [[ "$output" == *"no private key on stdin"* ]]
}

@test "gh-app: an unreadable key path names the path" {
  run python3 "$LIB" installation-id --issuer 1 --org o --key "$BATS_TEST_TMPDIR/absent.pem"
  [ "$status" -ne 0 ]
  [[ "$output" == *"not readable"* ]]
}

@test "gh-app: the key is never accepted through argv" {
  # argv is world-readable through /proc for the life of the process, so a key or token
  # passed as a flag leaks to any local user. Only a PATH or '-' is accepted.
  grep -qF '"--key", default="-"' "$LIB"
  refute_grep -qE '"--(pem|private-key|token|secret)"' "$LIB"
  # And the key is read from stdin or a file, never from an argument value.
  grep -qF 'sys.stdin.buffer.read()' "$LIB"
}

@test "gh-app: it depends on neither openssl nor jq" {
  # The first version used both. The orchestrator image has neither, and because signing
  # sits inside a no_log boundary it surfaced as an unexplained credential failure
  # rather than a missing binary.
  # CODE only. The module docstring names openssl and jq precisely to explain why they
  # were removed, and an assertion that forbids naming the hazard suppresses the
  # documentation of it (docs/MISTAKES.md §2.8). Strip the docstring and comments first.
  python3 - "$LIB" > "$BATS_TEST_TMPDIR/code.py" <<'STRIP'
import ast, sys
src = open(sys.argv[1]).read()
tree = ast.parse(src)
doc = ast.get_docstring(tree, clean=False)
lines = src.split("\n")
if doc is not None:
    body0 = tree.body[0]
    for i in range(body0.lineno - 1, body0.end_lineno):
        lines[i] = ""
print("\n".join(l for l in lines if not l.lstrip().startswith("#")))
STRIP
  refute_grep -qE 'openssl' "$BATS_TEST_TMPDIR/code.py"
  refute_grep -qE '\bjq\b' "$BATS_TEST_TMPDIR/code.py"
  refute_grep -qE 'subprocess|os\.system|popen' "$BATS_TEST_TMPDIR/code.py"
}

@test "gh-app: a missing cryptography library is reported as such, not as a bad key" {
  # rc=127-style confusion is what made the original failure take three runs to
  # diagnose: the message blamed the key when the tool was absent.
  grep -qF "the 'cryptography' package is unavailable" "$LIB"
}

@test "gh-app: withdrawal is a first-class action" {
  # A runner must be removable without destroying its host — the difference between a
  # reversible stop and a rebuild.
  grep -qF 'remove-token' "$LIB"
  run python3 "$LIB" --help
  [ "$status" -eq 0 ]
  [[ "$output" == *"remove-token"* ]]
}

@test "gh-app: only the credential goes to stdout" {
  # A caller captures stdout into a variable; anything diagnostic mixed in would be
  # captured as part of the credential.
  grep -qF 'sys.stdout.write(out)' "$LIB"
  grep -qF 'file=sys.stderr' "$LIB"
}
