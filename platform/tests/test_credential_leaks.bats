#!/usr/bin/env bats
# Repository-wide credential leak regression tests.
# Validates that no credentials, private IPs, or secrets are committed.
#
# Run: bats platform/tests/test_credential_leaks.bats

REPO_ROOT=""
COMMITTED_FILES_MANIFEST=""

load assert_helpers

setup_file() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  COMMITTED_FILES_MANIFEST="$BATS_FILE_TMPDIR/credential-scan-files"
  _committed_files "$REPO_ROOT" > "$COMMITTED_FILES_MANIFEST"
  export REPO_ROOT COMMITTED_FILES_MANIFEST
}

setup() {
  REPO_ROOT=${REPO_ROOT:-$(git rev-parse --show-toplevel)}
}

# ── Helper: list and scan committed files ─────────────────────────────

_committed_files() {
  # Returns git-tracked files, excluding exactly the historical scan set:
  #   - netbox-docker/ (vendored upstream)
  #   - .gitkeep files
  #   - binary images
  #   - this test file itself
  #   - markdown documentation (plan/, docs/, README, CLAUDE.md)
  #   - test fixture files (test_*.py, test_*.bats, conftest.py)
  #   - example files (*.example)
  # The one awk process replaces fifteen serial grep filters without changing
  # their case-sensitive regular expressions.
  local root="${1:-$REPO_ROOT}"
  git -C "$root" ls-files \
    | awk '
      /^platform\/services\/netbox\/deployment\/netbox-docker\// { next }
      /\.gitkeep$/ { next }
      /\.png$/ { next }
      /\.svg$/ { next }
      /\.ico$/ { next }
      /test_credential_leaks\.bats$/ { next }
      /\.md$/ { next }
      /\.MD$/ { next }
      /^plan\// { next }
      /^docs\// { next }
      /test_.*\.py$/ { next }
      /test_.*\.bats$/ { next }
      /conftest\.py$/ { next }
      /\.example$/ { next }
      /\.j2$/ { next }
      { print }
    '
}

_scan_committed_files() {
  local root="$1"
  shift
  local manifest file
  local -a files=()

  # setup_file creates this immutable name manifest once for the checkout. Every
  # test in this file is read-only there; synthetic coverage passes a separate
  # scratch repository and rebuilds its own manifest instead.
  if [ "$root" = "$REPO_ROOT" ] && [ -n "${COMMITTED_FILES_MANIFEST:-}" ]; then
    manifest="$COMMITTED_FILES_MANIFEST"
  else
    manifest="$BATS_TEST_TMPDIR/credential-scan-files-${BATS_TEST_NUMBER:-fixture}"
    _committed_files "$root" > "$manifest"
  fi

  while IFS= read -r file; do
    [ -f "$root/$file" ] || continue
    files+=("$file")
  done < "$manifest"
  [ "${#files[@]}" -gt 0 ] || return 0

  # One grep keeps the existing grep engine and options, while -H produces the
  # same relative file:line:content prefix that the old per-file sed added.
  (cd "$root" && grep -nH "$@" -- "${files[@]}") || true
}

_private_ip_violations() {
  local root="$1"
  local pattern="$2"
  local exclusions="$3"
  # Apply exclusions only after the file:line: prefix. A filename containing
  # "test" or "example" must not suppress a finding in that file.
  _scan_committed_files "$root" -E "$pattern" \
    | grep -viE "^.*:[0-9]+:.*($exclusions)" \
    || true
}

_password_violations() {
  local root="$1"
  _scan_committed_files "$root" -iE '(password|passwd)\s*[:=]\s*["\x27]?[A-Za-z0-9!@#$%^&*]{8,}' \
    | grep -vE '^.*:[0-9]+:[[:space:]]*#' \
    | grep -vE '^.*:[0-9]+:.*(\{\{|\$|example|REPLACE_ME|changeme|placeholder)' \
    || true
}

_api_key_violations() {
  local root="$1"
  _scan_committed_files "$root" -iE '(api_key|apikey|secret_key|secret_id|token)\s*[:=]\s*["\x27]?[A-Za-z0-9_-]{20,}' \
    | grep -vE '^.*:[0-9]+:[[:space:]]*#' \
    | grep -vE '^.*:[0-9]+:.*(\{\{|\$|example|REPLACE_ME|placeholder|dummyKey|dummy_key|LOCAL_FAKE)' \
    || true
}

# ── RFC1918 IP detection ──────────────────────────────────────────────

@test "credential leak: no hardcoded 192.168.x.x IPs in committed files" {
  local violations
  violations=$(_private_ip_violations "$REPO_ROOT" \
    '192\.168\.[0-9]+\.[0-9]+' \
    'target|host:|subnet|scope|example|RFC1918|CIDR|placeholder|template|inventory_hostname|test|\.md:')
  if [ -n "$violations" ]; then
    echo "Found hardcoded 192.168.x.x IPs:"
    echo "$violations"
    false
  fi
}

@test "credential leak: no hardcoded 10.x.x.x IPs in committed files" {
  local violations
  # Match 10.N.N.N but not version strings like 10.0 without third octet.
  violations=$(_private_ip_violations "$REPO_ROOT" \
    '\b10\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\b' \
    'target|host:|subnet|scope|example|RFC1918|CIDR|placeholder|template|test|\.md:')
  if [ -n "$violations" ]; then
    echo "Found hardcoded 10.x.x.x IPs:"
    echo "$violations"
    false
  fi
}

@test "credential leak: no hardcoded 172.16-31.x.x IPs in committed files" {
  local violations
  violations=$(_private_ip_violations "$REPO_ROOT" \
    '\b172\.(1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3}\b' \
    'target|host:|subnet|scope|example|RFC1918|CIDR|placeholder|template|test|\.md:')
  if [ -n "$violations" ]; then
    echo "Found hardcoded 172.16-31.x.x IPs:"
    echo "$violations"
    false
  fi
}

# ── Hardcoded credential detection ───────────────────────────────────

@test "credential leak: no hardcoded passwords in committed files" {
  local violations
  violations=$(_password_violations "$REPO_ROOT")
  if [ -n "$violations" ]; then
    echo "Found hardcoded passwords:"
    echo "$violations"
    false
  fi
}

@test "credential leak: no hardcoded API keys or tokens in committed files" {
  local violations
  violations=$(_api_key_violations "$REPO_ROOT")
  if [ -n "$violations" ]; then
    echo "Found hardcoded API keys or tokens:"
    echo "$violations"
    false
  fi
}

# ── Batched scanner regression fixtures ──────────────────────────────

_fixture_git() {
  local root="$1"
  shift
  env \
    -u GIT_DIR \
    -u GIT_WORK_TREE \
    -u GIT_INDEX_FILE \
    -u GIT_COMMON_DIR \
    -u GIT_OBJECT_DIRECTORY \
    -u GIT_ALTERNATE_OBJECT_DIRECTORIES \
    -u GIT_PREFIX \
    -u GIT_NAMESPACE \
    git -C "$root" "$@"
}

_make_credential_scan_fixture() {
  local root="$1"
  local api_value password_value private_10 private_172 private_192
  api_value=$(printf '%024d' 0)
  password_value=$(printf '%012d' 0)
  private_10=$(printf '10.%d.%d.%d' 20 30 40)
  private_172=$(printf '172.%d.%d.%d' 20 30 40)
  private_192=$(printf '192.%d.%d.%d' 168 30 40)

  mkdir -p \
    "$root/src" \
    "$root/plan" \
    "$root/docs" \
    "$root/platform/services/netbox/deployment/netbox-docker"
  _fixture_git "$root" init -q

  printf 'listen=%s\n' "$private_192" > "$root/src/private address.conf"
  printf 'listen=%s\n' "$private_10" > "$root/src/example test network.conf"
  printf 'listen=%s\n' "$private_172" > "$root/src/network 172.conf"
  printf 'api_key=%s\npassword=%s\n' "$api_value" "$password_value" > "$root/src/credential value.conf"
  printf 'api_key=%s\npassword=%s\n' "$api_value" "$password_value" \
    > "$root/src/# example:test credential.conf"
  printf '# api_key=%s\n   # api_key=%s\n# password=%s\n   # password=%s\n' \
    "$api_value" "$api_value" "$password_value" "$password_value" \
    > "$root/src/commented credentials.conf"
  printf 'workspace=/%s/%s/project\n' Users fixture-user > "$root/src/workstation path.conf"
  printf '%s\n' 'listen={{ service_host }}' 'api_key=$RUNTIME_KEY' 'workspace=/srv/app' > "$root/src/clean.conf"

  local word
  : > "$root/src/private exclusions.conf"
  for word in target host: subnet scope example RFC1918 CIDR placeholder template test .md:; do
    printf '%s=%s\n' "$word" "$private_10" >> "$root/src/private exclusions.conf"
  done
  printf 'inventory_hostname=%s\n' "$private_192" >> "$root/src/private exclusions.conf"

  {
    printf 'api_key=%s {{ ignored }}\n' "$api_value"
    printf 'api_key=%s $RUNTIME\n' "$api_value"
    for word in example REPLACE_ME placeholder dummyKey dummy_key LOCAL_FAKE; do
      printf 'api_key=%s%s\n' "$word" "$api_value"
    done
    printf 'password=%s {{ ignored }}\n' "$password_value"
    printf 'password=%s $RUNTIME\n' "$password_value"
    for word in example REPLACE_ME changeme placeholder; do
      printf 'password=%s%s\n' "$word" "$password_value"
    done
  } > "$root/src/secret exclusions.conf"

  local excluded
  local -a excluded_files=(
    'platform/services/netbox/deployment/netbox-docker/vendor.conf'
    'src/.gitkeep'
    'src/fixture.png'
    'src/fixture.svg'
    'src/fixture.ico'
    'src/test_credential_leaks.bats'
    'src/fixture.md'
    'src/fixture.MD'
    'plan/fixture.conf'
    'docs/fixture.conf'
    'src/test_fixture.py'
    'src/test_fixture.bats'
    'src/conftest.py'
    'src/fixture.example'
    'src/fixture.j2'
  )
  for excluded in "${excluded_files[@]}"; do
    mkdir -p "$root/$(dirname "$excluded")"
    printf 'api_key=%s\n' "$api_value" > "$root/$excluded"
  done

  _fixture_git "$root" add -- .
}

_legacy_scan_committed_files() {
  local root="$1"
  shift
  local file
  _committed_files "$root" | while IFS= read -r file; do
    [ -f "$root/$file" ] || continue
    grep -n "$@" "$root/$file" | sed "s|^|$file:|" || true
  done
}

@test "credential scan batch preserves grep findings, prefixes, and status" {
  local fixture="$BATS_TEST_TMPDIR/findings-repo"
  _make_credential_scan_fixture "$fixture"

  local batched legacy prefixes
  batched=$(_scan_committed_files "$fixture" -E '192\.168\.[0-9]+\.[0-9]+')
  local batched_status=$?
  legacy=$(_legacy_scan_committed_files "$fixture" -E '192\.168\.[0-9]+\.[0-9]+')
  local legacy_status=$?
  [ "$batched_status" -eq "$legacy_status" ]
  [ "$batched" = "$legacy" ]
  prefixes=$(printf '%s\n' "$batched" | sed -E 's/^(.+:[0-9]+):.*$/\1/')
  assert_grep -qxF 'src/private address.conf:1' <<< "$prefixes"

  batched=$(_scan_committed_files "$fixture" -iE '(api_key|apikey|secret_key|secret_id|token)\s*[:=]\s*["\x27]?[A-Za-z0-9_-]{20,}')
  legacy=$(_legacy_scan_committed_files "$fixture" -iE '(api_key|apikey|secret_key|secret_id|token)\s*[:=]\s*["\x27]?[A-Za-z0-9_-]{20,}')
  [ "$batched" = "$legacy" ]

  batched=$(_scan_committed_files "$fixture" -E '/(Users|home)/[A-Za-z0-9._-]+/')
  legacy=$(_legacy_scan_committed_files "$fixture" -E '/(Users|home)/[A-Za-z0-9._-]+/')
  [ "$batched" = "$legacy" ]
  prefixes=$(printf '%s\n' "$batched" | sed -E 's/^(.+:[0-9]+):.*$/\1/')
  [ "$prefixes" = 'src/workstation path.conf:1' ]

  run _scan_committed_files "$fixture" -E 'THIS_PATTERN_HAS_NO_FIXTURE_MATCH'
  [ "$status" -eq 0 ]
  [ -z "$output" ]
}

@test "credential scan batch preserves file and line exclusions" {
  local fixture="$BATS_TEST_TMPDIR/exclusions-repo"
  _make_credential_scan_fixture "$fixture"

  local files excluded output_prefixes
  files=$(_committed_files "$fixture")
  local -a excluded_files=(
    'platform/services/netbox/deployment/netbox-docker/vendor.conf'
    'src/.gitkeep'
    'src/fixture.png'
    'src/fixture.svg'
    'src/fixture.ico'
    'src/test_credential_leaks.bats'
    'src/fixture.md'
    'src/fixture.MD'
    'plan/fixture.conf'
    'docs/fixture.conf'
    'src/test_fixture.py'
    'src/test_fixture.bats'
    'src/conftest.py'
    'src/fixture.example'
    'src/fixture.j2'
  )
  for excluded in "${excluded_files[@]}"; do
    refute_grep -qxF "$excluded" <<< "$files"
  done

  output_prefixes=$(_private_ip_violations "$fixture" \
    '\b10\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\b' \
    'target|host:|subnet|scope|example|RFC1918|CIDR|placeholder|template|test|\.md:' \
    | sed -E 's/^(.+:[0-9]+):.*$/\1/')
  [ "$output_prefixes" = 'src/example test network.conf:1' ]

  output_prefixes=$(_private_ip_violations "$fixture" \
    '192\.168\.[0-9]+\.[0-9]+' \
    'target|host:|subnet|scope|example|RFC1918|CIDR|placeholder|template|inventory_hostname|test|\.md:' \
    | sed -E 's/^(.+:[0-9]+):.*$/\1/')
  [ "$output_prefixes" = 'src/private address.conf:1' ]

  output_prefixes=$(_private_ip_violations "$fixture" \
    '\b172\.(1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3}\b' \
    'target|host:|subnet|scope|example|RFC1918|CIDR|placeholder|template|test|\.md:' \
    | sed -E 's/^(.+:[0-9]+):.*$/\1/')
  [ "$output_prefixes" = 'src/network 172.conf:1' ]

  output_prefixes=$(_api_key_violations "$fixture" | sed -E 's/^(.+:[0-9]+):.*$/\1/')
  [ "$output_prefixes" = $'src/# example:test credential.conf:1\nsrc/credential value.conf:1' ]
  output_prefixes=$(_password_violations "$fixture" | sed -E 's/^(.+:[0-9]+):.*$/\1/')
  [ "$output_prefixes" = $'src/# example:test credential.conf:2\nsrc/credential value.conf:2' ]
}

@test "credential scan excludes comments after the file and line prefix" {
  local fixture="$BATS_TEST_TMPDIR/comment-prefix-repo"
  _make_credential_scan_fixture "$fixture"

  run _api_key_violations "$fixture"
  [ "$status" -eq 0 ]
  refute_grep -qF 'src/commented credentials.conf:' <<< "$output"
  assert_grep -qF 'src/# example:test credential.conf:1:' <<< "$output"

  run _password_violations "$fixture"
  [ "$status" -eq 0 ]
  refute_grep -qF 'src/commented credentials.conf:' <<< "$output"
  assert_grep -qF 'src/# example:test credential.conf:2:' <<< "$output"
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
  grep -qE '_env_name: "\{\{ _seed_env\[item\] \}\}"' "$pb/seed-postiz-secrets.yml"
  grep -qF "map('regex_replace', '^', 'SEED_')" "$pb/seed-postiz-secrets.yml"

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
  # The write path is now tasks/bao-merge-keys.yml; the property this test owns
  # is that the RESOLVED value (_bao_value — env-first) is what reaches it, and
  # that change detection happens against that same dict inside the shared task.
  grep -qE '_bm_data: "\{\{ \{bao_key: _bao_value\} \}\}"' "$f"
  refute_grep -qE '_bm_data: "\{\{ \{bao_key: bao_value\} \}\}"' "$f"
  # (_bm_set IS the caller's _bm_data; the shared task also accepts a _bm_remove list.)
  local bm="$REPO_ROOT/platform/playbooks/tasks/bao-merge-keys.yml"
  grep -qF '_bm_set: "{{ _bm_data | default({}) }}"' "$bm"
  grep -qE 'combine\(_bm_set\)\) != _bm_current' "$bm"
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

@test "NetBox token bootstrap guards AppRole transport before login" {
  assert_guard_precedes_first_uri "$REPO_ROOT/platform/playbooks/provision-netbox-automation-token.yml"
}

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

  # ESCAPING PIN, updated for the var form. The pattern now lives in a YAML
  # single-quoted VAR (_pat_strict), which Jinja passes to match() as a value —
  # no string-literal escape processing — so a single `\.` IS the escaped dot
  # and `\\.` would put a literal backslash into the regex. (The old inline
  # `is match('...')` form was the opposite; runtime-proven 2026-08-30 across
  # eight accept/refuse cases when this moved.)
  grep -qE "_pat_strict: '.*127\(\\\.\[0-9\]" "$shared"
  ! grep -qE "_pat_strict: '.*127\(\\\\\." "$shared"

  # Every playbook that reaches OpenBao, not just the ones the guard started in.
  # The two seed playbooks kept their own inline copies for one commit and were
  # therefore still bypassable in exactly the way the shared task now prevents.
  # Population selected by the CONDITION (the play resolves a store URL), never
  # by a hand-kept list of names. The previous form of this test listed FOUR files
  # under a comment claiming "every playbook that reaches OpenBao" — deriving the
  # population found 40 of 52 unguarded (docs/MISTAKES.md 2.18).
  #
  # A RATCHET rather than a blanket assertion, because guarding 38 live-service
  # deploys is its own change: every unguarded play must be named in the ratchet
  # file, and every play named there must still be unguarded — so a gap cannot
  # appear silently, and a fix cannot leave its exception behind.
  local ratchet="$REPO_ROOT/platform/tests/known_unguarded_bao_plays.txt"
  [ -f "$ratchet" ]
  local f base n_url n_inc n_seen=0 unguarded="" bad=""
  for f in "$pb"/*.yml; do
    n_url=$(grep -cE '^    _bao_url:' "$f" || true)
    [ "$n_url" -gt 0 ] || continue
    n_seen=$((n_seen + 1))
    base=$(basename "$f")
    # >= not ==: a play may guard MORE endpoints than it resolves store URLs for
    # (netbox-allocate-ip.yml guards the NetBox API too, via _assert_url_label).
    n_inc=$(grep -cE 'include_tasks: tasks/assert-bao-transport\.yml' "$f" || true)
    if [ "$n_inc" -lt "$n_url" ]; then
      unguarded="${unguarded}${base}"$'\n'
      grep -qxF "$base" "$ratchet" || bad="${bad}NEW unguarded play not in the ratchet: ${base}"$'\n'
    fi
  done
  # The other direction: a listed play that is now guarded must leave the list.
  local listed
  while read -r listed; do
    case "$listed" in ''|'#'*) continue ;; esac
    [ -f "$pb/$listed" ] || { bad="${bad}ratchet names a play that no longer exists: ${listed}"$'\n'; continue; }
    printf '%s' "$unguarded" | grep -qxF "$listed" \
      || bad="${bad}${listed} is now guarded — remove it from the ratchet"$'\n'
  done < "$ratchet"
  if [ -n "$bad" ]; then
    printf '%s' "$bad" >&2
    return 1
  fi
  # Not vacuous: the guard's original four plays, plus this change's, are in the set.
  [ "$n_seen" -ge 8 ]
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
m = re.search(r\"_pat_strict: '(.*?)'\", src)
ms = re.search(r\"_pat_single_label: '(.*?)'\", src)
assert m and ms, 'patterns not found in the shared task'
pat = m.group(1)
pat_single = ms.group(1)
# The single-label branch must be gated on local_mode in the assert expression —
# unconditional single-label acceptance is what the resolver search-suffix
# refusal below exists to prevent.
assert re.search(r'local_mode \| default\(false\).*is match\(_pat_single_label\)', src, re.S), 'single-label branch not gated on local_mode'

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
    'http://local-openbao@bao.evil.example/',                      # trufflehog:ignore
    'http://local-openbao.evil.example:8200/',  # dotted = public FQDN space, refused
    'http://local-openbao:8200',           # single-label: allowed ONLY under local_mode, strict refuses
]
bad = []
for u in accept:
    if not re.match(pat, u): bad.append('should ACCEPT: ' + u)
for u in refuse:
    if re.match(pat, u): bad.append('should REFUSE: ' + u)
# The local_mode branch's own pattern: single-label only, same trailing anchor.
sl_accept = ['http://local-openbao:8200', 'http://local-openbao:8200/v1',
             'http://host.containers.internal:8000']   # podman's engine-host name, exact
sl_refuse = [
    'http://host.containers.internal.evil.example/',   # the exact name is not a prefix
    'http://host.containers.internal@evil.example/',   # trufflehog:ignore — userinfo
    'http://evil.containers.internal:8000',            # only the one host-gateway name
    'http://local-openbao@bao.evil.example/',   # trufflehog:ignore — userinfo
    'http://local-openbao.evil.example:8200/',  # dotted = public FQDN space
    'https://anything',                          # wrong scheme for this branch
]
for u in sl_accept:
    if not re.match(pat_single, u): bad.append('single-label should ACCEPT: ' + u)
for u in sl_refuse:
    if re.match(pat_single, u): bad.append('single-label should REFUSE: ' + u)
if bad:
    print('\\n'.join(bad)); sys.exit(1)
print('all %d cases correct' % (len(accept) + len(refuse) + len(sl_accept) + len(sl_refuse)))
"
  [ "$status" -eq 0 ]
  [[ "$output" == *"all 30 cases correct"* ]]
}

@test "repo: no generated Python bytecode is tracked" {
  # A __pycache__ under platform/lib/ was committed because the ignore rule was scoped to
  # one service. Bytecode is interpreter-specific, unreviewable as source, and churns on
  # every run.
  local root tracked
  root=$(git rev-parse --show-toplevel)
  tracked=$(cd "$root" && git ls-files | grep -E '\.py[cod]$|(^|/)__pycache__/' || true)
  [ -z "$tracked" ] || { echo "tracked bytecode: $tracked"; false; }
  # And the ignore rule must be general, not per-service.
  grep -qE '^__pycache__/$' "$root/.gitignore"
  grep -qE '^\*\.py\[cod\]$' "$root/.gitignore"
}

@test "bao-merge-keys: creation is CAS-guarded, and losing the race falls back to a merge" {
  # Without options.cas: 0 the create path can overwrite a concurrent creation,
  # dropping its sibling keys — the same clobbering race merge-patch closes on
  # the update path. Measured: cas:0 answers 400 on an existing path, 200 fresh.
  local t="$REPO_ROOT/platform/playbooks/tasks/bao-merge-keys.yml"
  local blk
  blk=$(task_block "$t" "Create the new path")
  [ -n "$blk" ]
  assert_grep -qE '^        cas: 0' <<<"$blk"
  assert_grep -qE 'status_code: \[200, 400\]' <<<"$blk"
  # And the 400 (lost race) path merges instead of failing or clobbering.
  blk=$(task_block "$t" "Losing the create race")
  [ -n "$blk" ]
  assert_grep -q 'method: PATCH' <<<"$blk"
  assert_grep -q 'merge-patch' <<<"$blk"
}

@test "seed-openbao-key: the read-only access check ends the play before any write" {
  # Run once after Provision Seed Environment, before the first real seed: it must
  # prove the seed's own capabilities and stop, even when a value is staged.
  local f="$REPO_ROOT/platform/playbooks/seed-openbao-key.yml"
  local t="$REPO_ROOT/platform/playbooks/tasks/assert-bao-seed-access.yml"
  assert_precedes "$f" 'include_tasks: tasks/assert-bao-seed-access.yml' 'include_tasks: tasks/bao-merge-keys.yml'
  assert_precedes "$f" 'ansible.builtin.meta: end_play' 'include_tasks: tasks/bao-merge-keys.yml'
  local blk
  blk=$(task_block "$f" 'End read-only verification before every secret-store write')
  assert_grep -qF 'when: bao_verify_access_only | default(false) | bool' <<<"$blk"
  # The value check is skipped only in access-check mode.
  blk=$(task_block "$f" 'Validate the secret value is present')
  assert_grep -qF 'when: not (bao_verify_access_only | default(false) | bool)' <<<"$blk"
  # Capabilities decide, never a GET status alone; every token-bearing call is no_log.
  assert_grep -qF '/v1/sys/capabilities-self' "$t"
  # The patch-vs-create rule lives in filter_plugins/seed_access.py (test_seed_access_rule.py).
  assert_grep -qF '_missing: "{{ _caps | seed_access_missing(_exists | bool) }}"' "$t"
  [ "$(grep -c 'no_log: true' "$t")" -eq 2 ]
}
