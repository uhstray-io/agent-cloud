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

# ── §12A order: foundation genesis-deployed before Semaphore ─────────────────
@test "bootstrap genesis-deploys the foundation before Semaphore starts" {
  bp="$PB/bootstrap-local-dev.yml"
  foundation=$(grep -n "Genesis-deploy the secure foundation" "$bp" | head -1 | cut -d: -f1)
  semaphore=$(grep -n "Start local Semaphore" "$bp" | head -1 | cut -d: -f1)
  [ -n "$foundation" ] && [ -n "$semaphore" ]
  [ "$foundation" -lt "$semaphore" ]
}

@test "genesis loop covers dns step-ca caddy authentik in dependency order" {
  run grep -E "loop: \[dns, step-ca, caddy, authentik\]" "$PB/bootstrap-local-dev.yml"
  [ "$status" -eq 0 ]
}

# ── §12A: Semaphore boots last, OIDC-secured + fail-safe ─────────────────────
@test "Semaphore start carries fail-safe OIDC env (jq-validated + step-ca trust)" {
  bp="$PB/bootstrap-local-dev.yml"
  run grep -q "SEMAPHORE_OIDC_PROVIDERS" "$bp"; [ "$status" -eq 0 ]
  run grep -q "jq-validate the OIDC JSON" "$bp"; [ "$status" -eq 0 ]
  run grep -q "SSL_CERT_FILE" "$bp"; [ "$status" -eq 0 ]
  run grep -q "SEMAPHORE_WEB_ROOT" "$bp"; [ "$status" -eq 0 ]
}

@test "OIDC is gated on deps being ready (fail-safe: Semaphore boots without it)" {
  bp="$PB/bootstrap-local-dev.yml"
  # the OIDC run flags are empty unless _oidc_ready
  run grep -q "if (_oidc_ready | bool) else ''" "$bp"
  [ "$status" -eq 0 ]
}

@test "TLS bundle includes the system roots, not just step-ca (else apk/pip break)" {
  # SSL_CERT_FILE replaces the whole trust store — the bundle must carry the
  # image's public roots too. Regression guard for the validation-found bug.
  run grep -q "ca-certificates.crt" "$PB/bootstrap-local-dev.yml"
  [ "$status" -eq 0 ]
}

@test "podman-compose discovery uses shell (command -v is a builtin) + asserts" {
  bp="$PB/bootstrap-local-dev.yml"
  run grep -q "ansible.builtin.shell: command -v podman-compose" "$bp"
  [ "$status" -eq 0 ]
  run grep -q "Require podman-compose for the Mac-direct genesis path" "$bp"
  [ "$status" -eq 0 ]
}

@test "preflight checks podman-compose + jq (genesis prereqs)" {
  run grep -qE "podman-compose .*jq|podman podman-compose" "$REPO_ROOT/scripts/local-dev.sh"
  [ "$status" -eq 0 ]
}
