#!/usr/bin/env bats
# Structural tests for the declared Semaphore repository records.
#
# The point of these records is removing global mutable state: one record per
# branch, named by the templates that use it, instead of one shared record whose
# branch gets flipped. These tests pin that property and the SSH-URL guard.
#
# Structural only (grep/parse asserts) — no live Semaphore calls.
# Run: bats platform/tests/test_semaphore_repositories.bats

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  DECL="$REPO_ROOT/platform/semaphore/repositories.yml"
  BOOT="$REPO_ROOT/platform/semaphore/bootstrap-semaphore-repositories.yml"
  SETUP="$REPO_ROOT/platform/semaphore/setup-templates.yml"
  TPL="$REPO_ROOT/platform/semaphore/templates.yml"
  OLD="$REPO_ROOT/platform/playbooks/set-semaphore-branch.yml"
}

@test "repositories: both main and dev records are declared" {
  [ -f "$DECL" ]
  grep -qE '^\s+- name: agent-cloud$' "$DECL"
  grep -qE '^\s+- name: agent-cloud dev$' "$DECL"
  grep -qE 'git_branch: main' "$DECL"
  grep -qE 'git_branch: dev' "$DECL"
}

@test "repositories: no SSH clone URL is paired with the 'none' key" {
  # SSH always authenticates — there is no anonymous SSH, even for a public
  # repo — so this combination fails every checkout.
  ! grep -qE 'git_url:.*(git@|ssh://)' "$DECL"
  grep -qE 'git_url: "https://' "$DECL"
}

@test "repositories: the bootstrap playbook REFUSES the SSH-without-key combo" {
  grep -qE 'Refuse an SSH clone URL with no key attached' "$BOOT"
  grep -qE "is match\('\^\(ssh://\|git@\)'\)" "$BOOT"
}

@test "repositories: bootstrap is create-or-correct and never deletes" {
  grep -qE 'Create missing repository records' "$BOOT"
  grep -qE 'Correct records that drifted from the declaration' "$BOOT"
  # No DELETE anywhere — a hand-made record still in use must not vanish.
  ! grep -qE 'method: DELETE' "$BOOT"
}

@test "repositories: bootstrap only writes when something actually differs" {
  grep -qE '_existing\.git_url != item\.git_url' "$BOOT"
  grep -qE '_existing\.git_branch != item\.git_branch' "$BOOT"
}

@test "repositories: bootstrap verifies by re-reading, not by assuming" {
  grep -qE 'Re-read to confirm' "$BOOT"
  grep -qE '_repos_after' "$BOOT"
}

@test "templates: setup resolves repositories by NAME, not one hardcoded id" {
  # The hardcoded single id is what forced branch-flipping in the first place.
  grep -qE '_repo_ids:' "$SETUP"
  grep -qE "_repo_ids\[item\.repository \| default\('agent-cloud'\)\]" "$SETUP"
  ! grep -qE "_repo_id: .*selectattr\('name', 'equalto', 'agent-cloud'\)" "$SETUP"
}

@test "templates: a template naming a missing repository fails loudly" {
  grep -qE 'Require every repository a template names to exist' "$SETUP"
  grep -qE 'Require the default repository record to exist' "$SETUP"
}

@test "templates: omitting 'repository' still binds to main (no behavior change)" {
  # Every existing template omits the field, so the default must be the main
  # record or this change would silently repoint the whole project.
  grep -qE "default\('agent-cloud'\)" "$SETUP"
  ! grep -qE '^\s+repository: ' "$TPL"
}

@test "set-semaphore-branch: marked deprecated but still functional" {
  # Kept deliberately as a manual one-record fix; the header must say why.
  grep -qE 'DEPRECATED' "$OLD"
  grep -qE 'bootstrap-semaphore-repositories\.yml' "$OLD"
  grep -qE 'global mutable state' "$OLD"
  # Still a real playbook, not a stub.
  grep -qE '^\s+hosts: localhost' "$OLD"
}

@test "docs: AGENTS.md documents the declarative path and the deprecation" {
  local f="$REPO_ROOT/AGENTS.md"
  grep -qE 'bootstrap-semaphore-repositories\.yml' "$f"
  grep -qE 'deprecated' "$f"
}
