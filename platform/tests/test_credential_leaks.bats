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
