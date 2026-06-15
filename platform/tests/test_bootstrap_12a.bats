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

# ── Fix #1: COMPOSE_CMD forced on the Mac-direct genesis path ────────────────
@test "fix #1: foundation deploys pass COMPOSE_CMD through from local_compose_cmd" {
  for svc in dns step-ca caddy authentik; do
    run grep -q "COMPOSE_CMD: \"{{ local_compose_cmd | default(omit) }}\"" "$PB/deploy-${svc}.yml"
    [ "$status" -eq 0 ] || { echo "deploy-${svc}.yml missing COMPOSE_CMD passthrough"; return 1; }
  done
}
