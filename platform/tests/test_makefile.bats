#!/usr/bin/env bats
# Structural tests for the root Makefile (new in this PR).
# Verifies all documented targets exist, the pattern rule works, and
# the help output is non-empty. Static analysis only — no live make invocations.
#
# Run: bats platform/tests/test_makefile.bats

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  MF="$REPO_ROOT/Makefile"
}

# ── Presence ──────────────────────────────────────────────────────────────────

@test "Makefile: file exists at repo root" {
  [ -f "$MF" ]
}

@test "Makefile: uses LOCAL_DEV variable pointing to scripts/local-dev.sh" {
  grep -q 'LOCAL_DEV.*scripts/local-dev.sh' "$MF"
}

# ── Declared targets (data-driven: one test, the expected set as data) ────────

@test "Makefile: all documented concrete targets are declared" {
  # Data-driven instead of ~20 near-identical greps. A missing target names
  # itself in the failure. Keep this list in sync with the Makefile's targets.
  local targets=(help local-preflight local-init local-bootstrap local-up local-all
                 local-creds local-validate local-smoke local-netbox local-netbox-discover
                 local-dns local-dns-resolver local-https local-https-down
                 local-tls-trust local-tls-untrust local-clean promote)
  for t in "${targets[@]}"; do
    grep -qE "^${t}:" "$MF" || { echo "missing concrete target: $t"; return 1; }
  done
}

@test "Makefile: pattern rules (local-deploy-%, local-clean-deploy-%) are declared" {
  grep -qE '^local-deploy-%:' "$MF"
  grep -qE '^local-clean-deploy-%:' "$MF"
}

# ── .PHONY coverage ───────────────────────────────────────────────────────────

@test "Makefile: .PHONY declares the core targets" {
  run grep -E '\.PHONY' "$MF"
  [ "$status" -eq 0 ]
  [[ "$output" =~ "local-bootstrap" ]]
  [[ "$output" =~ "local-clean" ]]
  [[ "$output" =~ "promote" ]]
}

# ── help format: all targets have ## doc comments ─────────────────────────────

@test "Makefile: help target uses ## to extract docs (grep -E pattern)" {
  grep -q "grep -E" "$MF"
  # the help target keys off '##' doc comments (matched literally, not as BRE)
  grep -qF '##' "$MF"
}

@test "Makefile: every concrete target carries a ## doc comment (for make help)" {
  # Targets without an inline ## are invisible in 'make help'. Collect the actual
  # target headers ('%' pattern rules are excluded by the char class) and assert
  # each has '##' on its header line. (The previous version selected lines
  # WITHOUT ## then checked FOR ## — a contradiction that made it a no-op.)
  while IFS= read -r t; do
    grep -qE "^${t}:.*##" "$MF" || { echo "target '$t' has no ## doc comment"; return 1; }
  done < <(grep -oE '^[a-zA-Z][a-zA-Z0-9_-]*:' "$MF" | sed 's/:$//' | sort -u)
}

# ── local-up: Tier-3 services deployed through Semaphore ─────────────────────

@test "Makefile: local-up runs local-bootstrap BEFORE the deploy targets" {
  run grep -A10 '^local-up:' "$MF"
  [ "$status" -eq 0 ]
  # Enforce ORDER, not mere presence: the bootstrap line must precede the first
  # local-deploy line within the recipe block.
  local boot_ln deploy_ln
  boot_ln=$(printf '%s\n' "$output" | grep -n 'local-bootstrap' | head -1 | cut -d: -f1)
  deploy_ln=$(printf '%s\n' "$output" | grep -n 'local-deploy' | head -1 | cut -d: -f1)
  [ -n "$boot_ln" ] && [ -n "$deploy_ln" ] && [ "$boot_ln" -lt "$deploy_ln" ]
}

@test "Makefile: local-up deploys o11y, opa, erpnext after bootstrap" {
  run grep -A15 '^local-up:' "$MF"
  [ "$status" -eq 0 ]
  [[ "$output" =~ "local-deploy-o11y" ]]
  [[ "$output" =~ "local-deploy-opa" ]]
  [[ "$output" =~ "local-deploy-erpnext" ]]
}

@test "Makefile: local-up treats n8n as best-effort (leading dash)" {
  # n8n pull can rate-limit; local-up must not abort the whole stack on failure.
  run grep -E '^\s*-.*local-deploy-n8n' "$MF"
  [ "$status" -eq 0 ]
}

# ── Delegation convention: targets delegate to scripts/local-dev.sh ──────────

@test "Makefile: core targets delegate to \$(LOCAL_DEV) with the right subcommand" {
  # Assert the recipe actually invokes $(LOCAL_DEV) <subcommand> — a loose word
  # match (e.g. 'bootstrap') could be satisfied by a comment or unrelated text,
  # missing a regression in the recipe command itself.
  grep -A2 '^local-bootstrap:' "$MF" | grep -qE '\$\(LOCAL_DEV\)[[:space:]]+bootstrap'
  grep -A2 '^local-clean:'     "$MF" | grep -qE '\$\(LOCAL_DEV\)[[:space:]]+clean'
  grep -A2 '^promote:'         "$MF" | grep -qE '\$\(LOCAL_DEV\)[[:space:]]+promote'
  grep -A2 '^local-deploy-%:'  "$MF" | grep -qE '\$\(LOCAL_DEV\)[[:space:]]+deploy'
}
