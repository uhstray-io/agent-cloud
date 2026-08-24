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
  # copy/file/tempfile appear ONLY inside the delegate_to: localhost key probe,
  # writing a short-lived key on the RUNNER. Nothing writes to the target: assert
  # no host-targeted write module, and that the ones present are runner-scoped.
  ! grep -qE 'ansible\.builtin\.(template|lineinfile|blockinfile|replace|service|systemd|user|authorized_key):' "$PB"
  run bash -c "grep -c 'delegate_to: localhost' '$PB'"
  [ "$output" -ge 2 ]
  ! grep -qE 'method: (POST|PUT|PATCH|DELETE)' "$PB"
}

@test "verify-host-access: the empty-group guard runs OUTSIDE the targeted play" {
  # A guard inside the host-targeted play cannot catch an empty group: `hosts:`
  # resolves first, so Ansible skips the whole play and returns SUCCESS without
  # running any task. The guard has to be its own localhost play, before it.
  grep -qE '^- name: "Pre-flight: validate the target group"' "$PB"
  run bash -c "awk '/^- name: \"Pre-flight/{f=1} f&&/^- name: \"Verify host access/{exit} f' '$PB' | grep -c 'hosts: localhost'"
  [ "$output" = "1" ]
  # It must assert group existence AND non-emptiness.
  run bash -c "awk '/^- name: \"Pre-flight/{f=1} f&&/^- name: \"Verify host access/{exit} f' '$PB' | grep -c 'target_service in groups'"
  [ "$output" = "1" ]
  run bash -c "awk '/^- name: \"Pre-flight/{f=1} f&&/^- name: \"Verify host access/{exit} f' '$PB' | grep -c 'groups\[target_service\] | default(\[\]) | length) > 0'"
  [ "$output" = "1" ]
  # `ungrouped` is a REAL group that resolves to zero hosts, so a fallback to it
  # makes the play skip and report SUCCESS. Any lint-only default must be a name
  # that cannot exist in an inventory.
  ! grep -qE "default\('ungrouped'\)|target_service: \"ungrouped\"" "$PB"
  grep -qE "default\('__verify_host_access_no_target__'\)" "$PB"
  # And the pre-flight must come FIRST in the file.
  local pre ver
  pre=$(grep -n '^- name: "Pre-flight: validate the target group"' "$PB" | cut -d: -f1)
  ver=$(grep -n '^- name: "Verify host access' "$PB" | cut -d: -f1)
  [ "$pre" -lt "$ver" ]
}

@test "verify-host-access: EVERY raw probe individually tolerates failure" {
  # A file-wide count does not prove this: the OpenBao lookups also carry
  # failed_when: false, so one raw probe could lose its tolerance and the totals
  # would still balance. Check each raw task block on its own.
  run python3 -c "
import re,sys
txt=open('$PB').read()
# split into task blocks at 4-space '- name:' boundaries
blocks=re.split(r'\n    - name:', txt)
bad=[]
n=0
for b in blocks:
    if 'ansible.builtin.raw:' in b:
        n+=1
        if 'failed_when: false' not in b:
            bad.append(b.split(chr(10))[0].strip()[:60])
print(f'{n}|' + (';'.join(bad) if bad else 'ALL_TOLERANT'))
"
  local count="${output%%|*}"
  local verdict="${output##*|}"
  [ "$count" -ge 4 ]
  [ "$verdict" = "ALL_TOLERANT" ]
}

@test "verify-host-access: a GO verdict REQUIRES key-only auth" {
  # The trap this closes: while password auth is still on, the orchestrator may
  # be connecting WITH the password. Reachability + working sudo would then yield
  # GO on a host with no key path at all, and harden-ssh would remove the only
  # way in. Success must be attributable to the key.
  grep -qE 'Key-only reachability probe' "$PB"
  grep -qE 'PasswordAuthentication=no' "$PB"
  grep -qE 'KbdInteractiveAuthentication=no' "$PB"
  grep -qE 'PreferredAuthentications=publickey' "$PB"
  # The verdict expression must gate on the probe result.
  run bash -c "awk '/Verdict: is it safe to run harden-ssh/{f=1} f' '$PB' | grep -c '_keyprobe.rc'"
  [ "$output" -ge 2 ]
}

@test "verify-host-access: the probe key is always removed" {
  # It needs the private key on disk to use it. That file must not survive a
  # failed probe.
  run bash -c "awk '/Key-only reachability probe/{f=1} f&&/^    - name: \"Read sshd/{exit} f' '$PB' | grep -c 'always:'"
  [ "$output" = "1" ]
  grep -qE 'Remove the probe key' "$PB"
  grep -qE 'state: absent' "$PB"
}

@test "verify-host-access: best possible verdict is PENDING-OPERATOR, never GO" {
  # An unqualified GO would imply both directions are proven, and only the
  # orchestrator's own path is provable from the orchestrator.
  grep -qE 'PENDING-OPERATOR' "$PB"
  ! grep -qE "'GO —" "$PB"
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

@test "verify-host-access: ends in an explicit verdict" {
  # So the decision is not left to interpreting scrolled-past output. The
  # positive verdict is PENDING-OPERATOR rather than GO — see the test above for
  # why an unqualified GO would be a lie.
  grep -qE 'Verdict: is it safe to run harden-ssh' "$PB"
  grep -qE 'PENDING-OPERATOR' "$PB"
  grep -qE 'NO-GO' "$PB"
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
