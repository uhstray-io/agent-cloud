#!/usr/bin/env bats
# The access gate must RENDER A VERDICT on a host that has not been hardened yet
# — which is the only kind of host it is ever run against.
#
# It aborted instead. Its two escalation tasks need root, and a host without
# NOPASSWD sudo makes sudo ask for a password; Ansible then raises "Missing sudo
# password" from the become plugin BEFORE the module runs, where the tasks'
# `failed_when: false` cannot reach it. Measured on a freshly keyed host: the
# key-only proof succeeded and the run died before any verdict.

load assert_helpers

setup() {
  PB="${BATS_TEST_DIRNAME}/../playbooks/verify-host-access.yml"
  [ -f "$PB" ]
}

@test "access gate: the sudo password is fetched before escalation is attempted" {
  local fetch_line sshd_line esc_line
  fetch_line=$(grep -n 'Fetch the sudo password so escalation can be attempted' "$PB" | head -1 | cut -d: -f1)
  sshd_line=$(grep -n "Read sshd's effective configuration" "$PB" | head -1 | cut -d: -f1)
  esc_line=$(grep -n 'Check whether privilege escalation works' "$PB" | head -1 | cut -d: -f1)
  [ -n "$fetch_line" ] && [ -n "$sshd_line" ] && [ -n "$esc_line" ]
  [ "$fetch_line" -lt "$sshd_line" ]
  [ "$fetch_line" -lt "$esc_line" ]
}

@test "access gate: it reads the sudo password from the same place harden-ssh does" {
  # If the two disagreed about where the password lives, the gate could clear a
  # host that harden-ssh then fails on — the false green this gate exists to stop.
  local hs="${BATS_TEST_DIRNAME}/../playbooks/harden-ssh.yml"
  [ -f "$hs" ]
  assert_grep -q 'secret/data/services/ssh:become_password' "$PB"
  assert_grep -q 'secret/data/services/ssh:become_password' "$hs"
}

@test "access gate: an absent sudo password skips escalation instead of aborting" {
  # The absence is a finding the report states, not a reason to abort. Both
  # escalation tasks must therefore be gated on having obtained one.
  local task
  for t in "Read sshd's effective configuration" "Check whether privilege escalation works"; do
    task=$(awk -v pat="$t" 'index($0,pat){f=1} f&&/^    - name:/&&!index($0,pat){exit} f{print}' "$PB")
    [ -n "$task" ]
    assert_grep -qE '_become_pw' <<<"$task"
  done
}

@test "access gate: the fetched password is never logged" {
  # It is a credential; the fetch and the assignment both carry no_log.
  local task
  for t in "Fetch the sudo password so escalation can be attempted" "Use it for escalation when one was found"; do
    task=$(awk -v pat="$t" 'index($0,pat){f=1} f&&/^    - name:/&&!index($0,pat){exit} f{print}' "$PB")
    [ -n "$task" ]
    assert_grep -qE 'no_log: true' <<<"$task"
  done
}

@test "access gate: the verdict still cannot reach an unqualified GO" {
  # By design it tops out at PENDING-OPERATOR: only one direction of proof is
  # available from the orchestrator, and the path that works may be the one
  # harden-ssh removes. This fix must not have relaxed that.
  assert_grep -q 'PENDING-OPERATOR' "$PB"
  refute_grep -qE "'GO —|\"GO —" "$PB"
}
