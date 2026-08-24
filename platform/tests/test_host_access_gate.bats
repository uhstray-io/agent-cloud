#!/usr/bin/env bats
# Structural tests for the host-access verification gate and the onboarding
# templates it sits between.
#
# What these pin: the hardening sequence has exactly one irreversible step
# (disabling password auth), and the rule protecting it used to be a human
# remembering to check first. These assert the gate exists as a runnable
# artifact, that it cannot itself cause a lockout, and that it cannot report
# success against nothing.
#
# Run: bats platform/tests/test_host_access_gate.bats

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  PB="$REPO_ROOT/platform/playbooks/verify-host-access.yml"
  TPL="$REPO_ROOT/platform/semaphore/templates.yml"
}

@test "verify-host-access: exists and is read-only" {
  [ -f "$PB" ]
  # A verification step that can itself change the host could cause the very
  # lockout it exists to prevent. No writing modules at all.
  ! grep -qE 'ansible\.builtin\.(copy|file|template|lineinfile|blockinfile|replace|service|systemd|user|authorized_key):' "$PB"
  ! grep -qE 'method: (POST|PUT|PATCH|DELETE)' "$PB"
}

@test "verify-host-access: cannot report success against an empty group" {
  # "skipping: no hosts matched" followed by SUCCESS is a false green that reads
  # exactly like a passing check — and this playbook's whole job is telling the
  # truth about access.
  grep -qE 'Refuse to report success against an empty group' "$PB"
  grep -qE 'ansible_play_hosts_all' "$PB"
}

@test "verify-host-access: every probe tolerates failure and still reports" {
  # A partial answer is what you need when access is the thing in question, so no
  # probe may abort the run before the report.
  local probes failed_when
  probes=$(grep -c 'ansible.builtin.raw:' "$PB")
  failed_when=$(grep -c 'failed_when: false' "$PB")
  [ "$probes" -ge 4 ]
  [ "$failed_when" -ge "$probes" ]
}

@test "verify-host-access: reads sshd's EFFECTIVE config, not the main file" {
  # A grep of sshd_config misses sshd_config.d drop-ins; `sshd -T` accounts for
  # them, which is what decides whether password auth is actually still enabled.
  grep -qE 'sshd -T' "$PB"
}

@test "verify-host-access: checks what harden-ssh will need before it runs" {
  # harden-ssh sources the sudo password from the store to write the NOPASSWD
  # drop-in. Absent, that run fails partway — better to know beforehand.
  grep -qE 'become_password' "$PB"
  grep -qE 'Store SSH Password first' "$PB"
}

@test "verify-host-access: ends in an explicit GO / NO-GO" {
  # So the decision is not left to interpreting scrolled-past output.
  grep -qE 'Verdict: is it safe to run harden-ssh' "$PB"
  grep -qE "'GO —" "$PB"
  grep -qE "NO-GO" "$PB"
}

@test "verify-host-access: says it cannot prove the operator's own path" {
  # Only the orchestrator's path is provable from here. The second direction
  # matters because the single working path may be the one about to be removed.
  grep -qiE 'cannot (tell you|prove).*workstation|two directions' "$PB"
}

@test "templates: Store SSH Password exists and stores no secret in Semaphore" {
  grep -qE '^  - name: Store SSH Password$' "$TPL"
  # Semaphore PERSISTS survey values, so the password must be a launch-time extra
  # var — never a survey field.
  run bash -c "awk '/^  - name: Store SSH Password\$/{f=1;next} f&&/^  - name: /{exit} f' '$TPL' | grep -cE '^    survey_vars:'"
  [ "$output" = "0" ]
}

@test "templates: Verify Host Access requires a target group" {
  grep -qE '^  - name: Verify Host Access$' "$TPL"
  run bash -c "awk '/^  - name: Verify Host Access\$/{f=1;next} f&&/^  - name: /{exit} f' '$TPL' | grep -c 'required: true'"
  [ "$output" = "1" ]
}

@test "templates: Check Secrets can no longer false-green on no hosts" {
  # Without target_service it matched no hosts and still reported success.
  run bash -c "awk '/^  - name: Check Secrets\$/{f=1;next} f&&/^  - name: /{exit} f' '$TPL' | grep -c 'name: target_service'"
  [ "$output" = "1" ]
}
