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

@test "provision-vm: each field's OWN assignment prefers inventory over the ledger" {
  # Proving `_decl.vm_x` appears somewhere in the file proves nothing about
  # precedence. Check each resolved assignment on its own line: the inventory
  # source must appear, and must come BEFORE any ledger source on that line.
  grep -qE "_decl: .*hostvars\\[" "$PB"
  local pairs="_vmid:vm_vmid _node:vm_node _name:vm_name _cores:vm_cores _mem:vm_memory \
               _disk:vm_disk _ip:vm_ip _gw:vm_gateway _dns:vm_nameserver \
               _storage:vm_disk_storage _bridge:vm_net_bridge _netmask:vm_netmask _tags:vm_tags"
  local pair var inv line before
  for pair in $pairs; do
    var="${pair%%:*}"; inv="${pair##*:}"
    line=$(grep -E "^    ${var}: " "$PB" | head -1)
    [ -n "$line" ] || { echo "no assignment found for $var"; return 1; }
    case "$line" in
      *"_decl.$inv"*) ;;
      *) echo "$var does not read _decl.$inv"; return 1 ;;
    esac
    # Everything left of the inventory source must contain no ledger source.
    before="${line%%_decl.$inv*}"
    case "$before" in
      *"_svc."*|*"_defaults."*) echo "$var reads the ledger before inventory"; return 1 ;;
    esac
  done
}

@test "provision-vm: the completeness gate asserts every required field" {
  # Comparing the gate's position to one debug task proves neither that it checks
  # every field nor that it precedes the writes. Extract the gate and inspect it.
  awk '/Require a complete VM declaration before touching Proxmox/{f=1;next} f&&/^    - name: /{exit} f' "$PB" > "$BATS_TEST_TMPDIR/gate.txt"
  [ -s "$BATS_TEST_TMPDIR/gate.txt" ]
  local v
  for v in _vmid _node _cores _mem _disk _ip _gw _dns _storage; do
    grep -qF "($v | string | length) > 0" "$BATS_TEST_TMPDIR/gate.txt" \
      || { echo "gate does not assert $v"; return 1; }
  done
}

@test "provision-vm: the gate precedes EVERY Proxmox write" {
  # Not merely the plan display: no POST/PUT/DELETE to the Proxmox API may appear
  # before the gate.
  local gate first_write
  gate=$(grep -n 'Require a complete VM declaration' "$PB" | head -1 | cut -d: -f1)
  first_write=$(grep -nE 'method: (POST|PUT|DELETE)' "$PB" | head -1 | cut -d: -f1)
  [ -n "$gate" ]
  [ -n "$first_write" ]
  [ "$gate" -lt "$first_write" ]
}

@test "provision-vm and resize-vm read the SAME declaration" {
  # One source of truth for both, rather than two that drift. resize-vm reads the
  # sizing fields; provision-vm reads those plus the provisioning-only ones.
  for v in vm_cores vm_memory vm_disk; do
    grep -qE "$v" "$PB" || { echo "provision-vm missing $v"; return 1; }
    grep -qE "$v" "$RESIZE" || { echo "resize-vm missing $v"; return 1; }
  done
}

@test "provision-vm: a multi-host service must name which declaration to provision" {
  # `first` is an accident of file order. With two declared hosts — a pool of CI runners,
  # say — it would provision one of them and silently apply the other's overrides to it,
  # which against a live VM is a rebuild. Refusing beats guessing.
  local PB="$BATS_TEST_DIRNAME/../playbooks/provision-vm.yml"
  grep -qF '_decl_host: "{{ target_host | default(_group_hosts | first' "$PB"
  grep -qF "(_group_hosts | length) <= 1 or (target_host | default('') | length > 0)" "$PB"
  # And a named host that is not in the group is refused, not silently defaulted.
  grep -qF '_decl_host | length == 0 or _decl_host in _group_hosts' "$PB"
}

@test "proxmox-validate: a guest on an OFFLINE node cannot abort the pre-flight check" {
  # The cluster API omits `name` for a guest whose node is down. A bare item.name aborted
  # the whole validation play — and since provision-vm.yml imports it as a precondition,
  # one powered-off hypervisor blocked provisioning anywhere on the cluster. A pre-flight
  # check that fails on a degraded fleet member is inverted: that is what it is for.
  local PV="$BATS_TEST_DIRNAME/../playbooks/proxmox-validate.yml"
  grep -qF "item.name | default(" "$PV"
  grep -qF "item.node | default(" "$PV"
  grep -qF "item.status | default(" "$PV"
  # No undefaulted field may remain in that message.
  ! grep -qE 'msg: "\{\{ item\.vmid \}\}: \{\{ item\.name \}\}' "$PV"
}
