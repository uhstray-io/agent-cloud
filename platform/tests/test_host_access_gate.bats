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

@test "verify-host-access: the key-only probe refuses password and interactive auth" {
  # Scoped to the ssh task itself. A file-wide grep passes if the token survives
  # in a comment or an unrelated task after the probe changes.
  awk '/Connect using the key ONLY/{f=1;next} f&&/^          register:/{exit} f' "$PB" \
    > "$BATS_TEST_TMPDIR/probe.txt"
  [ -s "$BATS_TEST_TMPDIR/probe.txt" ]
  local o
  for o in PreferredAuthentications=publickey PubkeyAuthentication=yes \
           PasswordAuthentication=no KbdInteractiveAuthentication=no BatchMode=yes; do
    grep -qF -- "$o" "$BATS_TEST_TMPDIR/probe.txt" || { echo "probe missing $o"; return 1; }
  done
}

@test "verify-host-access: the probe pins the host key instead of accepting any" {
  # accept-new lets an on-path server impersonate the host, accept our public
  # key, and make the probe report success — proving key auth against an attacker
  # while the real host has none.
  awk '/Connect using the key ONLY/{f=1;next} f&&/^          register:/{exit} f' "$PB" \
    > "$BATS_TEST_TMPDIR/probe.txt"
  grep -qF -- "StrictHostKeyChecking=yes" "$BATS_TEST_TMPDIR/probe.txt"
  ! grep -qF -- "accept-new" "$BATS_TEST_TMPDIR/probe.txt"
  grep -qF -- "UserKnownHostsFile=" "$BATS_TEST_TMPDIR/probe.txt"
  # And the pin must be derived from the host over the existing connection.
  grep -qE 'Read the target.s own SSH host key over the existing connection' "$PB"
}

@test "verify-host-access: the ssh task cannot run with an undefined key path" {
  # failed_when: false does NOT catch task-ARGUMENT templating failures, so an
  # undefined _keyfile.path aborts the play instead of reporting NO-GO —
  # defeating the whole always-report design.
  awk '/Connect using the key ONLY/{f=1} f&&/^      always:/{exit} f' "$PB" \
    > "$BATS_TEST_TMPDIR/probetask.txt"
  grep -qF -- "_keyfile.path is defined" "$BATS_TEST_TMPDIR/probetask.txt"
  grep -qF -- "_svc_key | length > 0" "$BATS_TEST_TMPDIR/probetask.txt"
}

@test "verify-host-access: cleanup removes BOTH temp files, always" {
  awk '/^      always:/{f=1} f' "$PB" > "$BATS_TEST_TMPDIR/cleanup.txt"
  [ -s "$BATS_TEST_TMPDIR/cleanup.txt" ]
  grep -qF -- "state: absent" "$BATS_TEST_TMPDIR/cleanup.txt"
  grep -qF -- "{{ _keyfile.path }}" "$BATS_TEST_TMPDIR/cleanup.txt"
  grep -qF -- ".known_hosts" "$BATS_TEST_TMPDIR/cleanup.txt"
}

@test "verify-host-access: the verdict itself gates on the key probe" {
  # Scoped to the verdict expression, not the file.
  awk '/Verdict: is it safe to run harden-ssh/{f=1} f' "$PB" > "$BATS_TEST_TMPDIR/verdict.txt"
  [ -s "$BATS_TEST_TMPDIR/verdict.txt" ]
  grep -qF -- "_keyprobe.rc" "$BATS_TEST_TMPDIR/verdict.txt"
  grep -qF -- "PENDING-OPERATOR" "$BATS_TEST_TMPDIR/verdict.txt"
  grep -qF -- "NO-GO" "$BATS_TEST_TMPDIR/verdict.txt"
  # An unqualified GO would claim both directions are proven.
  ! grep -qF -- "'GO —" "$BATS_TEST_TMPDIR/verdict.txt"
  # And it must tell the three failure modes apart — they have different fixes.
  grep -qF -- "_svc_key_len" "$BATS_TEST_TMPDIR/verdict.txt"
  grep -qF -- "_hostkey_ok" "$BATS_TEST_TMPDIR/verdict.txt"
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

@test "verify-host-access: connection params are resolved OUTSIDE the delegated block" {
  # Inside delegate_to: localhost, bare ansible_host/ansible_user resolve in the
  # DELEGATED context — so the probe could pin and connect to localhost instead
  # of the target, and localhost may have no ansible_user at all (an
  # argument-templating failure that failed_when: false does not catch).
  grep -qE "Resolve the target's connection parameters" "$PB"
  # Must read explicitly from the target's hostvars.
  grep -qF -- "hostvars[inventory_hostname].ansible_host" "$PB"
  grep -qF -- "hostvars[inventory_hostname].ansible_user" "$PB"
  # And that resolution must come BEFORE the delegated probe.
  local resolve probe
  resolve=$(grep -n "Resolve the target's connection parameters" "$PB" | head -1 | cut -d: -f1)
  probe=$(grep -n 'Key-only reachability probe' "$PB" | head -1 | cut -d: -f1)
  [ "$resolve" -lt "$probe" ]
  # The play must NOT carry a lazily-evaluated _target_addr play var.
  ! grep -qE '^    _target_addr: ' "$PB"
}

@test "verify-host-access: the probe refuses to run without a resolved user" {
  # An empty user templates to "@host" and fails as an argument error rather than
  # a probe failure — indistinguishable from an abort.
  awk '/Connect using the key ONLY/{f=1} f&&/^      always:/{exit} f' "$PB" \
    > "$BATS_TEST_TMPDIR/probetask.txt"
  grep -qF -- "_target_user | default('') | trim | length) > 0" "$BATS_TEST_TMPDIR/probetask.txt"
  # And the ssh target uses the resolved fact, not the bare connection var.
  grep -qF -- '{{ _target_user }}@{{ _target_addr }}' "$BATS_TEST_TMPDIR/probetask.txt"
  ! grep -qF -- '{{ ansible_user }}@' "$BATS_TEST_TMPDIR/probetask.txt"
}
