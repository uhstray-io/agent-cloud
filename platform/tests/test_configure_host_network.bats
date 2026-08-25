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

  # Asserted inside the identity task's OWN body. A whole-file search is satisfied
  # by a matching expression in any other task, so it cannot tell a working gate
  # from a regressed one.
  local ident_task
  ident_task=$(awk '/Refuse to disarm unless it is the intended host/{f=1} f&&/^    - name:/&&!/Refuse to disarm/{exit} f{print}' "$PB")
  [ -n "$ident_task" ]
  assert_contains "$ident_task" '_new_hostname.stdout | trim) == _expect_hostname'
}

@test "network config: the revert window is long enough to confirm within" {
  # The confirmation window is (net_revert_seconds - 30). At 30 or below it is
  # zero or negative: the wait returns immediately, the identity check never runs,
  # and the revert fires over an apply that actually worked.
  local task
  task=$(awk '/Require a revert window long enough to confirm within/{f=1} f&&/^    - name:/&&!/Require a revert window/{exit} f{print}' "$PB")
  [ -n "$task" ]
  assert_contains "$task" '(_revert_seconds | int) >= 60'
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
  # Scoped to the validation task's own body, so a match elsewhere cannot stand in
  # for it, and so the absence of a live write is asserted where it matters.
  local vtask
  vtask=$(awk '/Validate the rendered configuration in an isolated root/{f=1} f&&/^    - name:/&&!/Validate the rendered/{exit} f{print}' "$PB")
  [ -n "$vtask" ]
  assert_contains "$vtask" 'netplan generate --root-dir'

  # Enumerating forbidden verbs does not work: a refute listing `mv` and `dest:`
  # was walked straight past by `cp "$root/..." /etc/netplan/` (demonstrated by
  # mutation). Assert the INVARIANT instead — the live directory may be READ
  # (existing config is copied INTO the sandbox so netplan sees the full picture)
  # but never WRITTEN. So every bare, non-sandbox mention of it must be the one
  # permitted read; anything else is a write by some verb or other.
  local bare
  bare=$(printf '%s\n' "$vtask" | sed 's|\$root/etc/netplan|SANDBOX|g' | grep -n '/etc/netplan' || true)
  [ -n "$bare" ]   # the read itself must still be there; an empty result means the awk slice missed
  local offending
  offending=$(printf '%s\n' "$bare" | grep -v 'cp -a /etc/netplan/\. "SANDBOX' || true)
  if [ -n "$offending" ]; then
    echo "validation task references the LIVE /etc/netplan outside the permitted read:" >&2
    printf '%s\n' "$offending" >&2
    return 1
  fi

  # A path-based invariant structurally cannot see a command that acts on the live
  # system without naming a path, and `netplan apply` is exactly that — it survived
  # the check above while being the worst thing that could appear here: it would
  # apply the config during VALIDATION, before the revert timer is armed, so a bad
  # address would strand the host with nothing scheduled to undo it. netplan has
  # exactly one applying subcommand, so naming it is enumeration over a closed set,
  # not the open-ended verb blacklist rejected above.
  refute_grep -qE 'netplan[[:space:]]+(apply|try)' <<<"$vtask"
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
