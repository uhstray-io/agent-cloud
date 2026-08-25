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

@test "access gate: the sudo password is resolved before escalation is attempted" {
  # The gate delegates to the shared resolver rather than carrying its own copy,
  # so every pre-hardening playbook names one secret location. What still matters
  # here is ORDER: the resolve must precede the tasks that escalate, or they abort
  # before it runs.
  local res_line sshd_line esc_line
  res_line=$(grep -n 'tasks/resolve-become-password.yml' "$PB" | head -1 | cut -d: -f1)
  sshd_line=$(grep -n "Read sshd's effective configuration" "$PB" | head -1 | cut -d: -f1)
  esc_line=$(grep -n 'Check whether privilege escalation works' "$PB" | head -1 | cut -d: -f1)
  [ -n "$res_line" ] && [ -n "$sshd_line" ] && [ -n "$esc_line" ]
  [ "$res_line" -lt "$sshd_line" ]
  [ "$res_line" -lt "$esc_line" ]
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

@test "access gate: it carries no second copy of the credential fetch" {
  # The credential fetch and its no_log live in the shared resolver (asserted in
  # test_become_password_resolution.bats). The gate must not reintroduce its own
  # set_fact of ansible_become_password, which would be a second declaration that
  # can drift from the one harden-ssh agrees with.
  refute_grep -qE '^\s*ansible_become_password:' "$PB"
  assert_grep -q 'tasks/resolve-become-password.yml' "$PB"
}

@test "access gate: the verdict still cannot reach an unqualified GO" {
  # By design it tops out at PENDING-OPERATOR: only one direction of proof is
  # available from the orchestrator, and the path that works may be the one
  # harden-ssh removes. This fix must not have relaxed that.
  assert_grep -q 'PENDING-OPERATOR' "$PB"
  refute_grep -qE "'GO —|\"GO —" "$PB"
}

@test "access gate: both of its OpenBao lookups sit behind the transport guard" {
  # The AppRole secret_id, the returned token, the per-service private key and the
  # sudo password all cross that connection. no_log protects the log, not the wire.
  assert_grep -q 'tasks/assert-bao-transport.yml' "$PB"
  local guard first last
  guard=$(grep -n 'assert-bao-transport' "$PB" | head -1 | cut -d: -f1)
  first=$(grep -n 'hashi_vault' "$PB" | head -1 | cut -d: -f1)
  last=$(grep -n 'hashi_vault' "$PB" | tail -1 | cut -d: -f1)
  [ -n "$guard" ] && [ -n "$first" ] && [ -n "$last" ]
  [ "$guard" -lt "$first" ]
  [ "$guard" -lt "$last" ]
}

@test "access gate: it resolves the store address the same way the resolver does" {
  # When only the environment variable was set, the resolver found the sudo
  # password while the gate's own address stayed empty — which skipped both of its
  # lookups and made the verdict report NO-GO with the secrets available. One value
  # that two paths depend on must have one resolution rule.
  local task="${BATS_TEST_DIRNAME}/../playbooks/tasks/resolve-become-password.yml"
  [ -f "$task" ]
  assert_grep -qE "lookup\('env', 'OPENBAO_ADDR'\)" "$PB"
  assert_grep -qE "lookup\('env', 'OPENBAO_ADDR'\)" "$task"
}
