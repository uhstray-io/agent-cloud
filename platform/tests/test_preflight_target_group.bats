#!/usr/bin/env bats
# The false-green this prevents: Ansible treats a play whose `hosts:` matches
# nothing as a no-op — it prints "skipping: no hosts matched" and exits 0 — so an
# orchestrator records it as SUCCESS. Measured on 2026-08-24: postiz_svc was
# absent from the local control plane's inventory and the deploy "succeeded"
# three times with zero containers created.
#
# Run: bats platform/tests/test_preflight_target_group.bats

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  PF="$REPO_ROOT/platform/playbooks/preflight-target-group.yml"
}

@test "preflight: exists and is a localhost-only play" {
  [ -f "$PF" ]
  # It MUST NOT target the group it is checking — a play cannot guard its own
  # hosts: expression, since that is resolved before any task in it runs. That is
  # the whole reason this is a separate importable playbook.
  grep -qE '^  hosts: localhost$' "$PF"
  ! grep -qE '^  hosts: .*preflight_group' "$PF"
}

@test "preflight: refuses to pass when no group name was supplied" {
  # A guard that passes when its own input is missing is worse than no guard:
  # every caller that forgot the vars: block would appear to be protected.
  grep -qE 'preflight_group is defined' "$PF"
  grep -qE 'preflight_group \| string \| trim \| length > 0' "$PF"
}

@test "preflight: checks group membership, not merely group existence" {
  # `groups` contains a declared-but-empty group, so testing for the KEY passes
  # while the play still matches no hosts. Length is the assertion.
  grep -qE 'groups\[preflight_group\] \| default\(\[\]\)\) \| length > 0' "$PF"
}

@test "preflight: the postiz deploy imports it before its first phase" {
  # Order matters: imported after Phase 1 it would guard nothing that Phase 1 did.
  local dep="$REPO_ROOT/platform/playbooks/deploy-postiz.yml"
  local pf_line phase1_line
  pf_line=$(grep -n 'import_playbook: preflight-target-group.yml' "$dep" | head -1 | cut -d: -f1)
  phase1_line=$(grep -n '^- name: "Phase 1' "$dep" | head -1 | cut -d: -f1)
  [ -n "$pf_line" ]
  [ -n "$phase1_line" ]
  [ "$pf_line" -lt "$phase1_line" ]
  # And it must name the group this playbook actually deploys.
  grep -qE '^    preflight_group: postiz_svc$' "$dep"
}
