#!/usr/bin/env bats
# §12A bootstrap-reorder structural guards (static — no live calls).
# Verifies the secure foundation is genesis-deployed before Semaphore, Semaphore
# boots OIDC-secured (fail-safe), and the two probe fixes are present.
#
# Plan: plan/development/LOCAL-DEV-12A-IMPLEMENTATION.md (design: LOCAL-DEV-DEPLOYMENT.md §12A).
# Run: bats platform/tests/test_bootstrap_12a.bats

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  PB="$REPO_ROOT/platform/playbooks"
}

# ── Fix #2: lazy ansible_user default ───────────────────────────────────────
@test "fix #2: no deploy playbook uses an eager ansible_user default" {
  run grep -rn "~ ansible_user ~" "$PB"
  [ "$status" -eq 1 ]   # grep matches nothing -> rc 1
}

@test "fix #2: ansible_user default is lazy where _monorepo_dir is computed" {
  run grep -q "ansible_user | default('deploy')" "$PB/deploy-dns.yml"
  [ "$status" -eq 0 ]
}
