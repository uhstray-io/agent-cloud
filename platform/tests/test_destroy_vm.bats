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
  # target_host must belong to the service group; both token-bearing endpoints are transport-guarded.
  assert_grep -qF '_decl_host in _group_hosts' "$PB"
  [ "$(grep -c 'include_tasks: tasks/assert-bao-transport.yml' "$PB")" -eq 2 ]
  assert_grep -q '_assert_url_label: "Proxmox"' "$PB"
}

@test "destroy-vm: the launch must name the vmid (confirm_destroy), with no default" {
  assert_grep -qF "(confirm_destroy | default('') | string) == (_vmid | string)" "$PB"
  local t="$REPO_ROOT/platform/semaphore/templates.yml"
  grep -A22 '^  - name: Destroy VM$' "$t" | grep -q 'name: confirm_destroy'
  ! grep -A22 '^  - name: Destroy VM$' "$t" | grep -A4 'name: confirm_destroy' | grep -q 'default_value'
}

@test "destroy-vm: stops, deletes with purge, sweeps active image storages for leftovers, verifies task and absence" {
  assert_grep -q 'status/stop' "$PB"
  assert_grep -qF '?purge=1' "$PB"
  # Proxmox's own unreferenced-disk scan aborts on a node missing a cluster-wide storage,
  # so the play sweeps ACTIVE image storages itself and frees every leftover volume.
  refute_grep -q 'destroy-unreferenced-disks=1' "$PB"
  assert_grep -qF "selectattr('active', 'equalto', 1)" "$PB"
  assert_grep -qF "content?vmid={{ _vmid }}" "$PB"
  assert_grep -q 'Free every leftover volume' "$PB"
  assert_grep -q 'Re-check: nothing tagged with this vmid remains' "$PB"
  [ "$(grep -c 'method: DELETE' "$PB")" -eq 2 ]
  assert_grep -q 'Verify the destroy task succeeded' "$PB"
  assert_grep -q 'Confirm the vmid is gone from the cluster' "$PB"
  # A vmid already absent is a clean no-op, not a failure.
  assert_grep -q 'End play when the VM is already gone' "$PB"
  # uri POST/DELETE never report changed on their own.
  refute_grep -q 'is changed' "$PB"
}

@test "provision-vm: refuses a declared address that already answers on the network (MISTAKES 3.6)" {
  local pb="$REPO_ROOT/platform/playbooks/provision-vm.yml"
  assert_grep -q 'Probe the declared address from the controller' "$pb"
  assert_grep -q 'Stop when something answered at the declared address, or when the probe could not run' "$pb"
  # Fails closed when the probe cannot run; explicit audited override only.
  assert_grep -q 'allow_unverified_address' "$pb"
  # Create path only: after the existence check, before the clone.
  local g e c
  g=$(grep -n 'Probe the declared address from the controller' "$pb" | cut -d: -f1)
  e=$(grep -n 'name: "Check for existing VM"' "$pb" | cut -d: -f1)
  c=$(grep -n 'name: "Clone template to new VM' "$pb" | cut -d: -f1)
  [ "$e" -lt "$g" ] && [ "$g" -lt "$c" ]
}
