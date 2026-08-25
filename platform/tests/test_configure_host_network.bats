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
  apply_line=$(grep -n 'Apply it (fire-and-forget' "$PB" | head -1 | cut -d: -f1)
  [ -n "$guard_line" ]; [ -n "$backup_line" ]; [ -n "$apply_line" ]
  [ "$guard_line" -lt "$backup_line" ]
  [ "$guard_line" -lt "$apply_line" ]
}

@test "network config: the revert is armed before the apply, disarmed only after proof" {
  # The non-interactive equivalent of `netplan try`. If the new address never
  # answers, the host restores itself — nobody has to be watching.
  local arm_line apply_line disarm_line
  arm_line=$(grep -n 'ARM THE REVERT' "$PB" | head -1 | cut -d: -f1)
  apply_line=$(grep -n 'Apply it (fire-and-forget' "$PB" | head -1 | cut -d: -f1)
  disarm_line=$(grep -n 'DISARM the revert' "$PB" | head -1 | cut -d: -f1)
  [ -n "$arm_line" ]; [ -n "$apply_line" ]; [ -n "$disarm_line" ]
  [ "$arm_line" -lt "$apply_line" ]
  [ "$apply_line" -lt "$disarm_line" ]
  # The disarm must be gated on the controller having reached the NEW address...
  assert_grep -q 'Wait for SSH on the new address' "$PB"
  assert_grep -qE 'Fail the run when the new address never answered' "$PB"

  # ...AND on that address belonging to the intended host. An open port proves
  # only that SOME machine answers; if another already holds the address, the run
  # would report success while the real host silently reverted.
  local ident_line
  ident_line=$(grep -n 'Refuse to disarm unless it is the intended host' "$PB" | head -1 | cut -d: -f1)
  [ -n "$ident_line" ]
  [ "$ident_line" -lt "$disarm_line" ]
  assert_grep -qE '_new_hostname\.stdout \| trim\) == _expect_hostname' "$PB"
}

@test "network config: the confirmation probe honours a non-default SSH port" {
  # A host on another port would otherwise be probed on 22, time out, and have its
  # revert fire after a perfectly good apply.
  assert_grep -qE 'ansible_port: "\{\{ ansible_port \| default\(22\) \}\}"' "$PB"
  local slice
  slice=$(awk '/Wait for SSH on the new address/{f=1} f&&/^    - name:/&&!/Wait for SSH/{exit} f{print}' "$PB")
  [ -n "$slice" ]
  assert_contains "$slice" 'port: "{{ ansible_port | default(22) }}"'
  refute_contains "$slice" "port: 22"
}

@test "network config: the apply is fire-and-forget, because it severs its own connection" {
  # A normal task reports failure when the address changes under it, which would
  # mask a successful change and trigger a pointless retry.
  local slice
  slice=$(awk '/Apply it .fire-and-forget/{f=1} f&&/^    - name:/&&!/Apply it/{exit} f{print}' "$PB")
  [ -n "$slice" ]
  assert_contains "$slice" "poll: 0"
  assert_contains "$slice" "async:"
}

@test "network config: a dry run leaves nothing behind on the host" {
  # The default is a dry run, and it must be a TRUE one. An earlier version staged
  # the candidate file under /etc/netplan and relied on `changed_when: false`,
  # which hides the write from the report without preventing it.
  assert_grep -qE '_confirm: "\{\{ net_confirm \| default\(false\) \| bool \}\}"' "$PB"

  # The dry run must STOP before any mutating task, rather than gating each one.
  assert_grep -q 'Stop here on a dry run' "$PB"
  local stop_line backup_line install_line
  stop_line=$(grep -n 'Stop here on a dry run' "$PB" | head -1 | cut -d: -f1)
  backup_line=$(grep -n 'Back up the configuration' "$PB" | head -1 | cut -d: -f1)
  install_line=$(grep -n 'Install the validated configuration' "$PB" | head -1 | cut -d: -f1)
  [ "$stop_line" -lt "$backup_line" ]
  [ "$stop_line" -lt "$install_line" ]

  # And nothing may be written under /etc/netplan before that stop.
  local before
  before=$(sed -n "1,${stop_line}p" "$PB")
  refute_contains "$before" "dest: /etc/netplan"
  refute_contains "$before" ".staged"
}

@test "network config: validation never touches the live /etc/netplan" {
  # `netplan generate --root-dir` reads and writes only under the given directory.
  # The previous approach moved the candidate into place under its real name to run
  # the check — which OVERWROTE an existing managed file from a prior confirmed run
  # and then removed it, before the backup that could have restored it was taken.
  assert_grep -q 'netplan generate --root-dir' "$PB"
  refute_grep -qE 'mv /etc/netplan/99-agent-cloud\.yaml' "$PB"
}

@test "network config: the rendered config is validated before it can be applied" {
  assert_grep -q 'netplan generate' "$PB"
  assert_grep -q 'Validate the rendered configuration in an isolated root' "$PB"
}

@test "network config: a partial declaration is refused" {
  # An address without a gateway is a host unreachable off its own subnet, which
  # is indistinguishable from the lockout this playbook exists to prevent.
  assert_grep -q 'Require a complete network declaration' "$PB"
  assert_grep -qE 'net_gateway is match' "$PB"
  assert_grep -qE 'net_address is match' "$PB"
}
