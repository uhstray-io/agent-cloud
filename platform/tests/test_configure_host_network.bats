#!/usr/bin/env bats
# configure-host-network.yml converges a host's IPv4 configuration to the
# inventory declaration. Two properties matter more than the rest, and both exist
# because of a real incident: a host was found holding another host's declared
# address, so two machines answered one IP and an orchestrated deploy
# authenticated against the wrong one for hours.
#
# Run: bats platform/tests/test_configure_host_network.bats

load assert_helpers

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  PB="$REPO_ROOT/platform/playbooks/configure-host-network.yml"
}

@test "network config: playbook exists and lints as a playbook" {
  [ -f "$PB" ]
  assert_grep -qE '^- name:' "$PB"
}

@test "network config: refuses to reconfigure a host that is not the intended one" {
  # THE identity guard. Connecting to a declared IP reaches whichever machine the
  # local ARP cache points at; reconfiguring the wrong guest is unrecoverable
  # remotely. The host must state its own name and match before anything changes.
  assert_grep -q 'Ask the host what it calls itself' "$PB"
  assert_grep -qE '_remote_hostname\.stdout \| trim\) == \(net_expect_hostname \| default\(inventory_hostname' "$PB"
}

@test "network config: the identity guard runs BEFORE anything is modified" {
  # Order is the whole value. Verified by line number, not by both strings being
  # present, because a guard placed after the apply protects nothing.
  local guard_line apply_line backup_line
  guard_line=$(grep -n 'Refuse to continue if this is not the intended host' "$PB" | head -1 | cut -d: -f1)
  backup_line=$(grep -n 'Back up the configuration the revert will restore' "$PB" | head -1 | cut -d: -f1)
  apply_line=$(grep -n 'Apply the new configuration' "$PB" | head -1 | cut -d: -f1)
  [ -n "$guard_line" ]; [ -n "$backup_line" ]; [ -n "$apply_line" ]
  [ "$guard_line" -lt "$backup_line" ]
  [ "$guard_line" -lt "$apply_line" ]
}

@test "network config: the revert is armed before the apply, disarmed only after proof" {
  # The non-interactive equivalent of `netplan try`. If the new address never
  # answers, the host restores itself — nobody has to be watching.
  local arm_line apply_line disarm_line
  arm_line=$(grep -n 'ARM THE REVERT' "$PB" | head -1 | cut -d: -f1)
  apply_line=$(grep -n 'Apply the new configuration' "$PB" | head -1 | cut -d: -f1)
  disarm_line=$(grep -n 'DISARM the revert' "$PB" | head -1 | cut -d: -f1)
  [ -n "$arm_line" ]; [ -n "$apply_line" ]; [ -n "$disarm_line" ]
  [ "$arm_line" -lt "$apply_line" ]
  [ "$apply_line" -lt "$disarm_line" ]
  # The disarm must be gated on the controller having reached the NEW address.
  assert_grep -q 'Wait for SSH on the new address' "$PB"
  assert_grep -qE 'Fail the run when the new address never answered' "$PB"
}

@test "network config: the apply is fire-and-forget, because it severs its own connection" {
  # A normal task reports failure when the address changes under it, which would
  # mask a successful change and trigger a pointless retry.
  local slice
  slice=$(awk '/Apply the new configuration/{f=1} f&&/^    - name:/&&!/Apply the new/{exit} f{print}' "$PB")
  [ -n "$slice" ]
  assert_contains "$slice" "poll: 0"
  assert_contains "$slice" "async:"
}

@test "network config: applying requires explicit confirmation" {
  # The default is a dry run: render, validate, show the diff, change nothing.
  assert_grep -qE '_confirm: "\{\{ net_confirm \| default\(false\) \| bool \}\}"' "$PB"
  # Every mutating step is gated on it.
  local n
  n=$(grep -c 'when: _confirm' "$PB")
  [ "$n" -ge 4 ]
}

@test "network config: the rendered config is validated before it can be applied" {
  assert_grep -q 'netplan generate' "$PB"
  assert_grep -q 'Validate the rendered configuration before it can be applied' "$PB"
}

@test "network config: a partial declaration is refused" {
  # An address without a gateway is a host unreachable off its own subnet, which
  # is indistinguishable from the lockout this playbook exists to prevent.
  assert_grep -q 'Require a complete network declaration' "$PB"
  assert_grep -qE 'net_gateway is match' "$PB"
  assert_grep -qE 'net_address is match' "$PB"
}
