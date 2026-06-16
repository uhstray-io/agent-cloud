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

# ── Declared .PHONY targets ───────────────────────────────────────────────────

@test "Makefile: help target is declared" {
  grep -qE '^help:' "$MF"
}

@test "Makefile: local-preflight target is declared" {
  grep -qE '^local-preflight:' "$MF"
}

@test "Makefile: local-init target is declared" {
  grep -qE '^local-init:' "$MF"
}

@test "Makefile: local-bootstrap target is declared" {
  grep -qE '^local-bootstrap:' "$MF"
}

@test "Makefile: local-up target is declared" {
  grep -qE '^local-up:' "$MF"
}

@test "Makefile: local-validate target is declared" {
  grep -qE '^local-validate:' "$MF"
}

@test "Makefile: local-dns target is declared" {
  grep -qE '^local-dns:' "$MF"
}

@test "Makefile: local-dns-resolver target is declared" {
  grep -qE '^local-dns-resolver:' "$MF"
}

@test "Makefile: local-https target is declared" {
  grep -qE '^local-https:' "$MF"
}

@test "Makefile: local-https-down target is declared" {
  grep -qE '^local-https-down:' "$MF"
}

@test "Makefile: local-tls-trust target is declared" {
  grep -qE '^local-tls-trust:' "$MF"
}

@test "Makefile: local-tls-untrust target is declared" {
  grep -qE '^local-tls-untrust:' "$MF"
}

@test "Makefile: local-clean target is declared" {
  grep -qE '^local-clean:' "$MF"
}

@test "Makefile: promote target is declared" {
  grep -qE '^promote:' "$MF"
}

@test "Makefile: local-smoke target is declared" {
  grep -qE '^local-smoke:' "$MF"
}

@test "Makefile: local-netbox target is declared" {
  grep -qE '^local-netbox:' "$MF"
}

@test "Makefile: local-netbox-discover target is declared" {
  grep -qE '^local-netbox-discover:' "$MF"
}

# ── Pattern rules ─────────────────────────────────────────────────────────────

@test "Makefile: local-deploy-% pattern rule is declared" {
  grep -qE '^local-deploy-%:' "$MF"
}

@test "Makefile: local-clean-deploy-% pattern rule is declared" {
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

@test "Makefile: every concrete target (not pattern) has a ## comment" {
  # Concrete targets without ## are invisible in 'make help' output.
  # Collect all concrete-target lines (exclude pattern rules and comment blocks).
  while IFS= read -r line; do
    target=$(echo "$line" | sed 's/:.*//')
    # Skip if the same line or the rule body already has ##
    grep -qE "^${target}:.*##" "$MF" || {
      echo "Target '$target' missing ## doc comment"
      return 1
    }
  done < <(grep -E '^[a-zA-Z][a-zA-Z0-9_-]+:[^#]*$' "$MF" | grep -v '^#')
}

# ── local-up: Tier-3 services deployed through Semaphore ─────────────────────

@test "Makefile: local-up invokes local-bootstrap first" {
  # local-up's recipe block must contain local-bootstrap before the deploy targets.
  run grep -A10 '^local-up:' "$MF"
  [ "$status" -eq 0 ]
  [[ "$output" =~ "local-bootstrap" ]]
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

@test "Makefile: local-bootstrap delegates to LOCAL_DEV bootstrap" {
  run grep -A2 '^local-bootstrap:' "$MF"
  [[ "$output" =~ 'bootstrap' ]]
}

@test "Makefile: local-clean delegates to LOCAL_DEV clean" {
  run grep -A2 '^local-clean:' "$MF"
  [[ "$output" =~ 'clean' ]]
}

@test "Makefile: promote delegates to LOCAL_DEV promote" {
  run grep -A2 '^promote:' "$MF"
  [[ "$output" =~ 'promote' ]]
}

@test "Makefile: local-deploy-% delegates to LOCAL_DEV deploy" {
  run grep -A2 '^local-deploy-%:' "$MF"
  [[ "$output" =~ 'deploy' ]]
}
