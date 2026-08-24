#!/usr/bin/env bats
# Repository-wide credential leak regression tests.
# Validates that no credentials, private IPs, or secrets are committed.
#
# Run: bats platform/tests/test_credential_leaks.bats

REPO_ROOT=""

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
}

# ── Helper: list committed files, excluding vendored/test/doc paths ────

_committed_files() {
  # Returns git-tracked files, excluding:
  #   - netbox-docker/ (vendored upstream)
  #   - .gitkeep files
  #   - binary images
  #   - this test file itself
  #   - markdown documentation (plan/, docs/, README, CLAUDE.md)
  #   - test fixture files (test_*.py, test_*.bats, conftest.py)
  #   - example files (*.example)
  local filter="${1:-}"
  git ls-files -- "$REPO_ROOT" \
    | grep -v '^platform/services/netbox/deployment/netbox-docker/' \
    | grep -v '\.gitkeep$' \
    | grep -v '\.png$' \
    | grep -v '\.svg$' \
    | grep -v '\.ico$' \
    | grep -v 'test_credential_leaks\.bats$' \
    | grep -v '\.md$' \
    | grep -v '\.MD$' \
    | grep -v '^plan/' \
    | grep -v '^docs/' \
    | grep -v 'test_.*\.py$' \
    | grep -v 'test_.*\.bats$' \
    | grep -v 'conftest\.py$' \
    | grep -v '\.example$' \
    | grep -v '\.j2$' \
    ${filter:+| grep "$filter"}
}

# ── RFC1918 IP detection ──────────────────────────────────────────────

@test "credential leak: no hardcoded 192.168.x.x IPs in committed files" {
  local violations
  violations=$(
    _committed_files | while IFS= read -r f; do
      [ -f "$REPO_ROOT/$f" ] || continue
      grep -nE '192\.168\.[0-9]+\.[0-9]+' "$REPO_ROOT/$f" \
        | grep -viE 'target|host:|subnet|scope|example|RFC1918|CIDR|placeholder|template|inventory_hostname|test|\.md:' \
        | sed "s|^|$f:|" || true
    done
  )
  if [ -n "$violations" ]; then
    echo "Found hardcoded 192.168.x.x IPs:"
    echo "$violations"
    false
  fi
}

@test "credential leak: no hardcoded 10.x.x.x IPs in committed files" {
  local violations
  violations=$(
    _committed_files | while IFS= read -r f; do
      [ -f "$REPO_ROOT/$f" ] || continue
      # Match 10.N.N.N but not version strings like 10.0 without third octet
      grep -nE '\b10\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\b' "$REPO_ROOT/$f" \
        | grep -viE 'target|host:|subnet|scope|example|RFC1918|CIDR|placeholder|template|test|\.md:' \
        | sed "s|^|$f:|" || true
    done
  )
  if [ -n "$violations" ]; then
    echo "Found hardcoded 10.x.x.x IPs:"
    echo "$violations"
    false
  fi
}

@test "credential leak: no hardcoded 172.16-31.x.x IPs in committed files" {
  local violations
  violations=$(
    _committed_files | while IFS= read -r f; do
      [ -f "$REPO_ROOT/$f" ] || continue
      grep -nE '\b172\.(1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3}\b' "$REPO_ROOT/$f" \
        | grep -viE 'target|host:|subnet|scope|example|RFC1918|CIDR|placeholder|template|test|\.md:' \
        | sed "s|^|$f:|" || true
    done
  )
  if [ -n "$violations" ]; then
    echo "Found hardcoded 172.16-31.x.x IPs:"
    echo "$violations"
    false
  fi
}

# ── Hardcoded credential detection ───────────────────────────────────

@test "credential leak: no hardcoded passwords in committed files" {
  local violations
  violations=$(
    _committed_files | while IFS= read -r f; do
      [ -f "$REPO_ROOT/$f" ] || continue
      # Match password/passwd assignments with literal values (8+ chars)
      # Exclude template variables ({{ }}), env var refs ($), comments, and docs
      grep -inE '(password|passwd)\s*[:=]\s*["\x27]?[A-Za-z0-9!@#$%^&*]{8,}' "$REPO_ROOT/$f" \
        | grep -v '{{' \
        | grep -v '\$' \
        | grep -v '^\s*#' \
        | grep -v 'example' \
        | grep -v 'REPLACE_ME' \
        | grep -v 'changeme' \
        | grep -v 'placeholder' \
        | sed "s|^|$f:|" || true
    done
  )
  if [ -n "$violations" ]; then
    echo "Found hardcoded passwords:"
    echo "$violations"
    false
  fi
}

@test "credential leak: no hardcoded API keys or tokens in committed files" {
  local violations
  violations=$(
    _committed_files | while IFS= read -r f; do
      [ -f "$REPO_ROOT/$f" ] || continue
      # Match token/api_key/secret_key assignments with literal values
      grep -inE '(api_key|apikey|secret_key|secret_id|token)\s*[:=]\s*["\x27]?[A-Za-z0-9_-]{20,}' "$REPO_ROOT/$f" \
        | grep -v '{{' \
        | grep -v '\$' \
        | grep -v '^\s*#' \
        | grep -v 'example' \
        | grep -v 'REPLACE_ME' \
        | grep -v 'placeholder' \
        | grep -v 'dummyKey' \
        | grep -v 'dummy_key' \
        | grep -v 'LOCAL_FAKE' \
        | sed "s|^|$f:|" || true
    done
  )
  if [ -n "$violations" ]; then
    echo "Found hardcoded API keys or tokens:"
    echo "$violations"
    false
  fi
}

# ── Jinja2 template namespace validation ─────────────────────────────

@test "credential leak: Jinja2 templates use approved variable namespaces" {
  local templates
  templates=$(find "$REPO_ROOT/platform/services" -name "*.j2" 2>/dev/null)
  [ -n "$templates" ] || skip "no Jinja2 templates found"

  local violations=""
  for tmpl in $templates; do
    # Extract {{ variable }} patterns, excluding filters (|), lookups, and dict access
    # Approved prefixes: secrets, _, ansible_, inventory_hostname, service_, monorepo_,
    #                    container_, hostvars, groups, item, lookup, netbox_, diode_
    local bare_vars
    bare_vars=$(
      grep -oE '\{\{[\s]*[a-zA-Z][a-zA-Z0-9_.]*' "$tmpl" \
        | sed 's/{{[[:space:]]*//' \
        | grep -vE '^(secrets|_|ansible_|inventory_hostname|service_|monorepo_|container_|hostvars|groups|item|lookup|netbox_|diode_|openbao_|bao_|discovery_|orb_)' \
        || true
    )
    if [ -n "$bare_vars" ]; then
      local basename
      basename=$(basename "$tmpl")
      violations="${violations}${basename}: ${bare_vars}\n"
    fi
  done

  if [ -n "$violations" ]; then
    echo "Jinja2 templates with variables outside approved namespaces:"
    printf '%b' "$violations"
    echo ""
    echo "Approved namespaces: secrets.*, _*, ansible_*, inventory_hostname,"
    echo "  service_*, monorepo_*, container_*, hostvars, groups, item"
    false
  fi
}

# ── Gitignore coverage ───────────────────────────────────────────────

@test "credential leak: .gitignore covers secrets/ directory" {
  grep -qE '^\s*secrets/' "$REPO_ROOT/.gitignore"
}

@test "credential leak: .gitignore covers *.secret files" {
  grep -qE '^\s*\*\.secret' "$REPO_ROOT/.gitignore"
}

@test "credential leak: .gitignore covers *.key files" {
  grep -qE '^\s*\*\.key' "$REPO_ROOT/.gitignore"
}

@test "credential leak: .gitignore covers *.pem files" {
  grep -qE '^\s*\*\.pem' "$REPO_ROOT/.gitignore"
}

@test "credential leak: .gitignore covers runtime .env files" {
  # Must have a pattern that covers config/*.env or similar runtime env files
  grep -qE '(\.env|\*\*/config/\*\.env|\*\.env)' "$REPO_ROOT/.gitignore"
}

# ── Tracked file detection ───────────────────────────────────────────

@test "credential leak: no .env files tracked by git (only .env.example)" {
  local tracked_env
  tracked_env=$(git ls-files -- "$REPO_ROOT" | grep '\.env$' | grep -v '\.example' || true)
  if [ -n "$tracked_env" ]; then
    echo "Found .env files tracked by git (should be .env.example only):"
    echo "$tracked_env"
    false
  fi
}

@test "credential leak: no private key files tracked by git" {
  local tracked_keys
  tracked_keys=$(git ls-files -- "$REPO_ROOT" \
    | grep -E '\.(key|pem|p12|pfx)$' \
    | grep -v '\.example' \
    | grep -v '\.pub$' \
    || true)
  if [ -n "$tracked_keys" ]; then
    echo "Found private key files tracked by git:"
    echo "$tracked_keys"
    false
  fi
}

@test "credential leak: no .secret files tracked by git" {
  local tracked_secrets
  tracked_secrets=$(git ls-files -- "$REPO_ROOT" | grep '\.secret$' || true)
  if [ -n "$tracked_secrets" ]; then
    echo "Found .secret files tracked by git:"
    echo "$tracked_secrets"
    false
  fi
}

# ── Secrets must not reach Semaphore-run playbooks as extra vars ──────────────
# Semaphore persists a task's extra-var JSON in its own database and serves it
# back over its API indefinitely (verified against a completed task, whose
# `environment` field came back verbatim). So `-e <secret>=...` leaves a live
# credential in plaintext outside OpenBao. Survey fields were already known to
# persist; extra vars were wrongly believed to be safe, and three templates said
# so. An encrypted environment secret is the channel these playbooks' own OpenBao
# AppRole credentials already arrive on.

@test "secret-writing playbooks prefer an environment secret over an extra var" {
  # ORDER is the assertion. With the extra var as the first operand, a run passing
  # BOTH uses the persisted one even though the secret is configured — the leak,
  # still open. Each playbook must put the env lookup first.
  local pb="$REPO_ROOT/platform/playbooks"

  # store-ssh-password.yml's own ordering is asserted in test_host_access_gate.bats,
  # which lands with that change; this file covers the two seed playbooks.
  grep -qE "_bao_value:.*lookup\('env', *'BAO_VALUE'\) *\| *default\(bao_value" \
    "$pb/seed-openbao-key.yml"
  grep -qE "lookup\('env', *_env_name\) *$" "$pb/seed-postiz-secrets.yml"
  grep -qE '_env_name: "SEED_\{\{ item \| upper \}\}"' "$pb/seed-postiz-secrets.yml"

  # The reverse order must not appear.
  ! grep -qE "_bao_value:.*bao_value *\| *default\(lookup" "$pb/seed-openbao-key.yml"
  ! grep -qE "lookup\('vars', 'seed_' ~ item[^)]*\) *\| *default\(lookup\('env'" \
    "$pb/seed-postiz-secrets.yml"
}

@test "seed-openbao-key uses the resolved value everywhere, not the raw extra var" {
  # The change-detection comparison matters as much as the writes: comparing the
  # stored value against the RAW extra var while writing the resolved one would
  # misjudge whether a write is needed on every environment-supplied run.
  local f="$REPO_ROOT/platform/playbooks/seed-openbao-key.yml"
  [ "$(grep -cE 'data: "\{\{ \{bao_key: _bao_value\} \}\}"' "$f")" -eq 2 ]
  grep -qE '_existing_data\[bao_key\] \| default\(none\) != _bao_value' "$f"
  # The validation gate must test the resolved value too, or a run supplying only
  # the environment would fail the check it just satisfied.
  grep -qE '^\s+- _bao_value \| length > 0' "$f"
  ! grep -qE '^\s+- bao_value is defined' "$f"
}

@test "no Semaphore template still instructs operators to pass a secret as -e" {
  # The operator-facing docs are the actual leak surface: the playbook change is
  # cosmetic while the template tells people to use the persisted form.
  local t="$REPO_ROOT/platform/semaphore/templates.yml"
  ! grep -qE '^\s*#\s*-e bao_value=<secret>' "$t"
  ! grep -qE '^\s*#\s*-e seed_[a-z_]+=\.\.\.' "$t"
  # And the replacement channel is named for each.
  grep -q "BAO_VALUE" "$t"
  grep -q "SEED_X_API_KEY" "$t"
  # NOTE: the Store SSH Password template's own correction (SSH_PASSWORD) is
  # asserted in test_host_access_gate.bats, which lands with that change.
}

# ── The OpenBao transport guard ───────────────────────────────────────────────
# The AppRole secret_id, the client token OpenBao returns, and whatever secret is
# being read or written all cross that connection. http:// is permitted to
# loopback/RFC1918 because the platform's OpenBao sits on an internal VLAN;
# public cleartext is refused.

@test "every play that resolves an OpenBao URL includes the transport guard" {
  # Counted, not merely present: a playbook with three plays and one include
  # leaves two plays unguarded, and a file-wide grep cannot tell the difference.
  local pb="$REPO_ROOT/platform/playbooks"
  local shared="$pb/tasks/assert-bao-transport.yml"
  [ -f "$shared" ]

  # Exactly ONE definition of the rule may exist. Six independently-worded copies
  # is how the userinfo bypass survived in two playbooks after being fixed in the
  # other three.
  # `run` so a no-match grep (exit 1, which is the PASSING case here) does not
  # abort the test before the assertion is evaluated.
  run bash -c "grep -l 'Refusing to send secret material' '$pb'/*.yml 2>/dev/null || true"
  [ -z "$output" ]
  grep -q 'Refusing to send secret material' "$shared"

  # The dots must be written `\\.` in the YAML. Jinja processes escapes in its
  # string literals, so `\\.` arrives as `\.` — an escaped dot. A single `\.`
  # happens to behave the same today only because Python passes an unrecognised
  # escape through unchanged, which is deprecated. Pin the explicit form.
  grep -qE '127\(\\\\\.\[0-9\]' "$shared"
  ! grep -qE '127\(\\\.\[0-9\]' "$shared"

  # Every playbook that reaches OpenBao, not just the ones the guard started in.
  # The two seed playbooks kept their own inline copies for one commit and were
  # therefore still bypassable in exactly the way the shared task now prevents.
  local f n_url n_inc
  for f in distribute-ssh-keys.yml store-ssh-password.yml \
           seed-postiz-secrets.yml seed-openbao-key.yml; do
    n_url=$(grep -cE '^    _bao_url:' "$pb/$f")
    n_inc=$(grep -cE 'include_tasks: tasks/assert-bao-transport\.yml' "$pb/$f")
    [ "$n_url" -gt 0 ]
    [ "$n_inc" -eq "$n_url" ]
  done
}

@test "the transport pattern accepts internal endpoints and refuses public ones" {
  # Tests the regex itself rather than running a playbook: the Unit Tests CI job
  # installs pytest and bats, not ansible, so an execution test would fail there.
  # The pattern is what actually decides, so it is what gets tested.
  #
  # The pattern must be decoded the way JINJA decodes it. In the YAML the dots are
  # written `\\.`, and Jinja processes escapes in its string literals, so what the
  # regex engine finally receives is `\.`. Compiling the raw file text instead would
  # treat it as "a literal backslash then any character" and the whole table would
  # come out wrong — passing or failing for entirely the wrong reason.
  run python3 -c "
import re, codecs, sys
src = open('$REPO_ROOT/platform/playbooks/tasks/assert-bao-transport.yml').read()
m = re.search(r\"_assert_bao_url is match\\('(.*?)'\\)\", src, re.S)
assert m, 'pattern not found in the shared task'
pat = codecs.decode(m.group(1), 'unicode_escape')

# Generic RFC1918 examples, deliberately NOT the platform's real endpoint —
# that address is site data and lives in site-config, not here.
accept = [
    'https://bao.example.com:8200',        # example: TLS anywhere is fine
    'http://localhost:8200',               # example: loopback by name
    'http://127.0.0.1:8200',               # example: loopback by address
    'http://10.1.2.3:8200',                # example: RFC1918 10/8
    'http://10.1.2.3',                     # example: no port at all
    'http://192.168.0.1:8200/v1',          # example: RFC1918 192.168/16 with path
    'http://172.16.0.1:8200',              # example: RFC1918 lower bound
    'http://172.31.255.254:8200',          # example: RFC1918 upper bound
]
refuse = [
    'http://bao.example.com:8200',         # example: public host, cleartext
    'http://10.evil.example/',             # example: prefix-match bypass
    'http://192.168.0.1.evil.example/',    # example: private-looking public host
    'http://172.32.0.1:8200',              # example: just OUTSIDE RFC1918
    'http://172.15.0.1:8200',              # example: just BELOW RFC1918
    'http://1270.0.0.1:8200',              # example: not loopback
    # userinfo bypasses: everything before the @ is credentials, not the host, so
    # the request actually goes to the PUBLIC host after it. These carry
    # trufflehog:ignore because a <user>:<pass>@<host> shape is exactly what a
    # URI-credential detector looks for — the strings hold no secret, they are the
    # attack they exist to prove is refused.
    'http://127.0.0.1:80@bao.evil.example/v1/auth/approle/login',  # trufflehog:ignore
    'http://127.0.0.1@bao.evil.example/v1',                        # trufflehog:ignore
    'http://10.1.2.3:8200@bao.evil.example/v1',                    # trufflehog:ignore
    'http://192.168.0.1:8200@bao.evil.example/',                   # trufflehog:ignore
]
bad = []
for u in accept:
    if not re.match(pat, u): bad.append('should ACCEPT: ' + u)
for u in refuse:
    if re.match(pat, u): bad.append('should REFUSE: ' + u)
if bad:
    print('\\n'.join(bad)); sys.exit(1)
print('all %d cases correct' % (len(accept) + len(refuse)))
"
  [ "$status" -eq 0 ]
  [[ "$output" == *"all 18 cases correct"* ]]
}
