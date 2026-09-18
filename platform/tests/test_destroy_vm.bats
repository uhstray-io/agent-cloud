#!/usr/bin/env bats
# Structural tests for destroy-vm.yml — a destructive template must refuse by
# default and only act on the VM the declaration names.
load assert_helpers

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  PB="$REPO_ROOT/platform/playbooks/destroy-vm.yml"
}

@test "destroy-vm: inventory-first, refuses a vmid mismatch, name mismatch and node mismatch" {
  [ -f "$PB" ]
  assert_grep -q 'import_playbook: preflight-target-group.yml' "$PB"
  assert_grep -qF "(_live.name | default('')) == _name" "$PB"
  assert_grep -qF "(_live.node | default('')) == _node" "$PB"
}

@test "destroy-vm: the launch must name the vmid (confirm_destroy), with no default" {
  assert_grep -qF "(confirm_destroy | default('') | string) == (_vmid | string)" "$PB"
  local t="$REPO_ROOT/platform/semaphore/templates.yml"
  grep -A22 '^  - name: Destroy VM$' "$t" | grep -q 'name: confirm_destroy'
  ! grep -A22 '^  - name: Destroy VM$' "$t" | grep -A4 'name: confirm_destroy' | grep -q 'default_value'
}

@test "destroy-vm: stops, deletes with purge + unreferenced disks, verifies the task and the absence" {
  assert_grep -q 'status/stop' "$PB"
  assert_grep -qF '?purge=1&destroy-unreferenced-disks=1' "$PB"
  assert_grep -q 'method: DELETE' "$PB"
  assert_grep -q 'Verify the destroy task succeeded' "$PB"
  assert_grep -q 'Confirm the vmid is gone from the cluster' "$PB"
  # A vmid already absent is a clean no-op, not a failure.
  assert_grep -q 'End play when the VM is already gone' "$PB"
  # uri POST/DELETE never report changed on their own.
  refute_grep -q 'is changed' "$PB"
}

@test "provision-vm: refuses a declared address that already answers on the network (MISTAKES 3.6)" {
  local pb="$REPO_ROOT/platform/playbooks/provision-vm.yml"
  assert_grep -q 'Refuse a declared address that already answers on the network' "$pb"
  assert_grep -q 'Stop when something answered at the declared address' "$pb"
  # Create path only: after the existence check, before the clone.
  local g e c
  g=$(grep -n 'Refuse a declared address that already answers' "$pb" | cut -d: -f1)
  e=$(grep -n 'name: "Check for existing VM"' "$pb" | cut -d: -f1)
  c=$(grep -n 'name: "Clone template to new VM' "$pb" | cut -d: -f1)
  [ "$e" -lt "$g" ] && [ "$g" -lt "$c" ]
}
