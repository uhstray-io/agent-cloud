#!/usr/bin/env bats
# Tests for platform/lib/github-app-token.sh — the GitHub App credential chain.
#
# The signing tests are BEHAVIOURAL, not structural: a JWT that is well-formed but
# incorrectly signed is indistinguishable from a correct one by inspection, and would
# fail only at the forge, at registration time, on a live host. So these generate a
# throwaway RSA key, sign a real assertion, and verify the signature with openssl.
# No network is touched — the token exchanges are not exercised here.
#
# Run: bats platform/tests/test_github_app_token.bats

setup() {
  LIB="$BATS_TEST_DIRNAME/../lib/github-app-token.sh"
  [ -f "$LIB" ]
  # A throwaway key per test. Never a real one: a real App key belongs in the secret
  # store and nowhere else (docs/MISTAKES.md §4.3 — fixtures are committed files).
  KEY="$BATS_TEST_TMPDIR/throwaway.pem"
  PUB="$BATS_TEST_TMPDIR/throwaway.pub"
  openssl genrsa -out "$KEY" 2048 2>/dev/null
  openssl rsa -in "$KEY" -pubout -out "$PUB" 2>/dev/null
  # shellcheck disable=SC1090
  source "$LIB"
}

# base64url -> raw, restoring the padding the encoder strips.
b64url_decode() {
  local s="${1//-/+}"; s="${s//_//}"
  case $(( ${#s} % 4 )) in 2) s="${s}==";; 3) s="${s}=";; esac
  printf '%s' "$s" | openssl base64 -d -A
}

@test "gh-app: the source guard makes repeated sourcing a no-op" {
  # shellcheck disable=SC1090
  source "$LIB"
  source "$LIB"
  [ "$_GITHUB_APP_TOKEN_SH_LOADED" = "1" ]
}

@test "gh-app: the assertion is three unpadded base64url segments" {
  local jwt
  jwt=$(_gh_app_jwt 123456 "$KEY")
  # Exactly two separators.
  [ "$(printf '%s' "$jwt" | tr -cd '.' | wc -c | tr -d ' ')" = "2" ]
  # base64url alphabet only: '+' and '/' must have been translated, '=' stripped.
  ! printf '%s' "$jwt" | grep -q '[+/=]'
  # And it must not have been wrapped — a multi-line token is a corrupt token.
  [ "$(printf '%s' "$jwt" | wc -l | tr -d ' ')" = "0" ]
}

@test "gh-app: the signature verifies against the key that signed it" {
  local jwt header payload sig_b64
  jwt=$(_gh_app_jwt 123456 "$KEY")
  header="${jwt%%.*}"
  payload=$(printf '%s' "$jwt" | cut -d. -f2)
  sig_b64=$(printf '%s' "$jwt" | cut -d. -f3)

  printf '%s.%s' "$header" "$payload" > "$BATS_TEST_TMPDIR/signing_input"
  b64url_decode "$sig_b64" > "$BATS_TEST_TMPDIR/sig.bin"

  run openssl dgst -sha256 -verify "$PUB" \
        -signature "$BATS_TEST_TMPDIR/sig.bin" "$BATS_TEST_TMPDIR/signing_input"
  [ "$status" -eq 0 ]
  [[ "$output" == *"Verified OK"* ]]
}

@test "gh-app: a signature does NOT verify against a different key" {
  # Guards the test above from passing vacuously — openssl must actually be checking.
  local jwt other
  jwt=$(_gh_app_jwt 123456 "$KEY")
  other="$BATS_TEST_TMPDIR/other.pub"
  openssl genrsa -out "$BATS_TEST_TMPDIR/other.pem" 2048 2>/dev/null
  openssl rsa -in "$BATS_TEST_TMPDIR/other.pem" -pubout -out "$other" 2>/dev/null

  printf '%s.%s' "${jwt%%.*}" "$(printf '%s' "$jwt" | cut -d. -f2)" > "$BATS_TEST_TMPDIR/si"
  b64url_decode "$(printf '%s' "$jwt" | cut -d. -f3)" > "$BATS_TEST_TMPDIR/sig.bin"

  run openssl dgst -sha256 -verify "$other" \
        -signature "$BATS_TEST_TMPDIR/sig.bin" "$BATS_TEST_TMPDIR/si"
  [ "$status" -ne 0 ]
}

@test "gh-app: the header declares RS256 and the claims are within the forge's limits" {
  local jwt iat exp iss
  jwt=$(_gh_app_jwt 987654 "$KEY")
  [ "$(b64url_decode "${jwt%%.*}" | jq -r .alg)" = "RS256" ]
  [ "$(b64url_decode "${jwt%%.*}" | jq -r .typ)" = "JWT" ]

  local claims
  claims=$(b64url_decode "$(printf '%s' "$jwt" | cut -d. -f2)")
  iat=$(printf '%s' "$claims" | jq -r .iat)
  exp=$(printf '%s' "$claims" | jq -r .exp)
  iss=$(printf '%s' "$claims" | jq -r .iss)

  [ "$iss" = "987654" ]
  # 9 minutes, deliberately inside the documented 10-minute ceiling: sitting on the
  # boundary turns clock skew into an intermittent auth failure.
  [ "$((exp - iat))" -eq 600 ]
  [ "$((exp - iat))" -lt 660 ]
  # iat is backdated, so the forge never sees an assertion issued in its own future.
  [ "$iat" -lt "$(date +%s)" ]
}

@test "gh-app: the key can arrive on stdin and leaves nothing behind" {
  local jwt before after
  before=$(find "${TMPDIR:-/tmp}" -maxdepth 1 -name 'tmp*' 2>/dev/null | wc -l | tr -d ' ')
  jwt=$(_gh_app_jwt 123456 - < "$KEY")
  [ -n "$jwt" ]
  [ "$(printf '%s' "$jwt" | tr -cd '.' | wc -c | tr -d ' ')" = "2" ]
  after=$(find "${TMPDIR:-/tmp}" -maxdepth 1 -name 'tmp*' 2>/dev/null | wc -l | tr -d ' ')
  # The staged key must be removed on the way out, not left at 0600 for later.
  [ "$after" -le "$before" ]
}

@test "gh-app: missing inputs fail loudly instead of producing a token" {
  run _gh_app_jwt "" "$KEY"
  [ "$status" -ne 0 ]
  [[ "$output" == *"app id is required"* ]]

  run _gh_app_jwt 123456 ""
  [ "$status" -ne 0 ]
  [[ "$output" == *"private key path is required"* ]]

  run _gh_app_jwt 123456 "$BATS_TEST_TMPDIR/absent.pem"
  [ "$status" -ne 0 ]
  [[ "$output" == *"not readable"* ]]

  run gh_app_installation_token 123456 "" "$KEY"
  [ "$status" -ne 0 ]
  [[ "$output" == *"installation id is required"* ]]

  run gh_runner_registration_token "" "some-token"
  [ "$status" -ne 0 ]
  [[ "$output" == *"organisation is required"* ]]

  run gh_runner_registration_token "uhstray-io" ""
  [ "$status" -ne 0 ]
  [[ "$output" == *"installation token is required"* ]]
}

@test "gh-app: a signing failure is surfaced, not masked into a valid-looking token" {
  # The failure this guards: piping openssl's output into the encoder would report the
  # ENCODER's exit status, yielding a well-formed token with a garbage signature that
  # fails only at the forge (docs/MISTAKES.md §1.3).
  printf 'not a private key\n' > "$BATS_TEST_TMPDIR/bad.pem"
  run _gh_app_jwt 123456 "$BATS_TEST_TMPDIR/bad.pem"
  [ "$status" -ne 0 ]
  [[ "$output" == *"signing failed"* ]]
}

@test "gh-app: no credential is ever placed in argv" {
  # argv is world-readable through /proc on a multi-user host, so a token passed as a
  # flag leaks to any local user for the life of the process. Tokens must travel in
  # headers and the key must be read from a file or stdin.
  grep -q 'Authorization: Bearer' "$LIB"
  ! grep -qE '(--token|-u [^ ]*:|access_token=)' "$LIB"
  # The key reaches openssl as a PATH (-sign "$keyfile"), never as key material.
  grep -qF 'openssl dgst -sha256 -sign "$keyfile" -binary' "$LIB"
}

@test "gh-app: a remove-token path exists so a runner can be withdrawn" {
  # Withdrawal must not require destroying the host — that is the difference between
  # a reversible stop and a rebuild.
  grep -qF 'gh_runner_remove_token()' "$LIB"
  grep -qF 'remove-token' "$LIB"
}
