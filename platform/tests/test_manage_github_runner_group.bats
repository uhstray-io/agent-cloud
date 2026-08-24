#!/usr/bin/env bats
# Structural tests for manage-github-runner-group.yml.
#
# This playbook IS the control that keeps the self-hosted plane away from public
# repositories. The organisation's pre-existing default group has visibility "all", so
# the failure mode is concrete and close by, not hypothetical.
#
# Run: bats platform/tests/test_manage_github_runner_group.bats

load assert_helpers

setup() {
  PLAYBOOK="$BATS_TEST_DIRNAME/../playbooks/manage-github-runner-group.yml"
  [ -f "$PLAYBOOK" ]
}

@test "runner-group: a public repository in the declaration aborts the run" {
  # The load-bearing check. Every declared name is resolved and its visibility read, so
  # the exclusion is a property of the automation rather than of whoever last edited
  # the list.
  grep -qF "selectattr('json.private', 'equalto', false)" "$PLAYBOOK"
  grep -qF 'must not be granted self-hosted' "$PLAYBOOK"
}

@test "runner-group: the group is created with selected visibility, never opened first" {
  # Creating it open and narrowing afterwards leaves a window in which a runner joining
  # it is reachable by every repository in the organisation.
  # Window scoped to the TASK, not a guessed line count: a fixed -A window breaks the
  # moment a comment is added inside the task, which is a test failing for its own
  # reasons (§2.2 territory).
  local create_task
  create_task=$(sed -n '/Create the group with selected visibility/,/^    - name:/p' "$PLAYBOOK")
  printf '%s' "$create_task" | grep -qF 'visibility: "selected"'
  printf '%s' "$create_task" | grep -qF 'allows_public_repositories: false'
  ! grep -qF 'visibility: "all"' "$PLAYBOOK"
}

@test "runner-group: convergence REPLACES the access list, so a stray grant is removed" {
  # An access list that only ever grows cannot enforce an exclusion, which is the whole
  # purpose. PUT replaces wholesale; that removal is the point.
  grep -A6 "Replace the group's repository access list" "$PLAYBOOK" | grep -qF 'method: PUT'
}

@test "runner-group: dry run is the DEFAULT" {
  # A playbook that mutates org-level access by default is one nobody can safely use to
  # look at the current state.
  grep -qF '_dry_run: "{{ dry_run | default(true) | bool }}"' "$PLAYBOOK"
  # Every mutating task is gated on it.
  local muts gated
  muts=$(grep -cE 'method: (POST|PATCH|PUT)' "$PLAYBOOK")
  gated=$(grep -cF 'not _dry_run' "$PLAYBOOK")
  [ "$muts" -gt 0 ]
  [ "$gated" -ge "$muts" ]
}

@test "runner-group: no other group is ever modified or deleted" {
  # Three groups already exist, one of them the org default with visibility "all".
  # Touching them is out of scope and would be destructive.
  refute_grep -qF 'method: DELETE' "$PLAYBOOK"
  # Every mutating URL targets either the collection (create) or an id this run
  # resolved for ITS OWN group — never a literal id, which would be a guess at another
  # group's identity.
  grep -qF '_existing.id' "$PLAYBOOK"
  ! grep -qE 'runner-groups/[0-9]+' "$PLAYBOOK"
}

@test "runner-group: the declaration lists private repos and excludes agent-cloud" {
  grep -qF "github_runner_repos | default(['zerds', 'atlas', 'zerds-website', 'weft', 'scientific-business'])" "$PLAYBOOK"
  # agent-cloud must not appear in the default declaration at all.
  ! grep -qE "default\(\['.*agent-cloud" "$PLAYBOOK"
}

@test "runner-group: an empty declaration is refused rather than silently converged" {
  # Converging to "admits nothing" is safe but is almost certainly a mistake, and a run
  # that reports success over doing nothing is the false-green shape (§2).
  grep -qF '_repos | length > 0' "$PLAYBOOK"
}

@test "runner-group: no_log is scoped to the credential boundary only" {
  local n
  n=$(grep -c 'no_log: true' "$PLAYBOOK")
  [ "$n" -eq 2 ]
  # The convergence report must stay visible — it is the audit record of what changed.
  ! grep -A6 'Report the converged access list' "$PLAYBOOK" | grep -q 'no_log: true'
}

@test "runner-group: the client secret is never referenced" {
  ! grep -qiE 'client_secret|GITHUB_APP_SECRET' "$PLAYBOOK"
}
