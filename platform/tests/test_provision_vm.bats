#!/usr/bin/env bats
# Structural tests for provision-vm.yml — specifically that it is reachable from
# the orchestrator at all.
#
# The bug these pin: the playbook read its VM spec from
# site-config/proxmox/vm-specs.yml via a path that resolved INSIDE the
# agent-cloud checkout. site-config is private and the Semaphore runner never
# checks it out, so the file was never there and this playbook had never been
# Semaphore-runnable — while its template sat in the UI looking operational.
#
# Structural only (grep asserts) — no live Proxmox calls.
# Run: bats platform/tests/test_provision_vm.bats

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  PB="$REPO_ROOT/platform/playbooks/provision-vm.yml"
  RESIZE="$REPO_ROOT/platform/playbooks/resize-vm.yml"
}

@test "provision-vm: the vm-specs lookup cannot raise when the file is absent" {
  # Absent is the NORMAL case on the runner, not a failure.
  run bash -c "grep -c \"lookup('file', _project_root + '/proxmox/vm-specs.yml', errors='ignore')\" '$PB'"
  [ "$output" = "1" ]
  # The un-guarded form must be gone.
  ! grep -qE "lookup\('file', _project_root \+ '/proxmox/vm-specs\.yml'\) \| from_yaml" "$PB"
}

@test "provision-vm: every spec field resolves from inventory before the ledger" {
  # The declaring host's vars are hoisted from hostvars, since the play runs on
  # localhost and the target's own vars are not in scope.
  grep -qE "_decl: .*hostvars\[" "$PB"
  for v in vm_vmid vm_node vm_cores vm_memory vm_disk vm_ip vm_gateway vm_nameserver vm_netmask vm_net_bridge vm_disk_storage vm_tags vm_name; do
    grep -qE "_decl\.$v" "$PB" || { echo "missing inventory source for $v"; return 1; }
  done
}

@test "provision-vm: an incomplete declaration is refused before any API write" {
  # Defaulting every field to '' stops the lookup raising, but it also turns a
  # hard failure into a soft one — a host with no declaration would otherwise
  # send a clone request with an empty vmid/node/ip.
  grep -qE 'Require a complete VM declaration before touching Proxmox' "$PB"
  # The gate must precede the plan display and therefore every write.
  local gate plan
  gate=$(grep -n 'Require a complete VM declaration' "$PB" | cut -d: -f1)
  plan=$(grep -n 'Show provisioning plan' "$PB" | cut -d: -f1)
  [ "$gate" -lt "$plan" ]
}

@test "provision-vm and resize-vm read the SAME declaration" {
  # One source of truth for both, rather than two that drift. resize-vm reads the
  # sizing fields; provision-vm reads those plus the provisioning-only ones.
  for v in vm_cores vm_memory vm_disk; do
    grep -qE "$v" "$PB" || { echo "provision-vm missing $v"; return 1; }
    grep -qE "$v" "$RESIZE" || { echo "resize-vm missing $v"; return 1; }
  done
}
