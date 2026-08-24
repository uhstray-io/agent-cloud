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

@test "verify-host-access: the probe honours a non-default SSH port" {
  # Without this the probe hits 22 regardless and reports a false NO-GO on any
  # host using ansible_port. That fails safe — it blocks hardening rather than
  # authorising it — but a gate that cries wolf gets overridden, and then it
  # protects nothing.
  grep -qF -- "hostvars[inventory_hostname].ansible_port" "$PB"
  awk '/Connect using the key ONLY/{f=1} f&&/^      always:/{exit} f' "$PB" \
    > "$BATS_TEST_TMPDIR/probetask.txt"
  grep -qF -- '- -p' "$BATS_TEST_TMPDIR/probetask.txt"
  grep -qF -- '"{{ _target_port }}"' "$BATS_TEST_TMPDIR/probetask.txt"
}

@test "verify-host-access: known_hosts uses the [host]:port form off port 22" {
  # OpenSSH keys a non-default port as [host]:port. A bare host entry would never
  # match, so StrictHostKeyChecking=yes would refuse — a false NO-GO from the
  # opposite direction.
  #
  # Asserted on the CONTENT line itself, not on the block: the _kh_host variable
  # definition survives even if content: stops using it, so a looser check passed
  # a mutation that swapped content: back to the bare address. Found by mutation-
  # testing this very assertion.
  # Select the known_hosts content line specifically. A plain `head -1` grabs the
  # KEY-MATERIAL task's content line instead — which is how an earlier version of
  # this test passed against the correct file for the wrong reason.
  local content_line
  content_line=$(grep -E '^            content: .*_hostkey\.stdout' "$PB" | head -1)
  [ -n "$content_line" ]
  case "$content_line" in
    *'{{ _kh_host }}'*) ;;
    *) echo "known_hosts content does not use _kh_host: $content_line"; return 1 ;;
  esac
  # And _kh_host must actually switch on the port.
  local kh_def
  kh_def=$(grep -E '^            _kh_host: ' "$PB" | head -1)
  [ -n "$kh_def" ]
  case "$kh_def" in
    *"(_target_port | int) == 22"*) ;;
    *) echo "_kh_host does not switch on the port: $kh_def"; return 1 ;;
  esac
  case "$kh_def" in
    *"']:'"*) ;;
    *) echo "_kh_host does not build the [host]:port form: $kh_def"; return 1 ;;
  esac
}

@test "templates: the Phase 1 gate and credential steps have dev variants" {
  # verify-host-access.yml lands on main only at the next promotion, so the
  # main-bound base cannot run until then. Store SSH Password gets one too, so
  # the whole phase executes ONE branch's code — running the irreversible steps
  # from main while the gate clearing them runs from dev would mean the thing
  # being verified is not the thing being run.
  #
  # Written as two plain extractions rather than a loop with `awk -v`: the
  # variable-injection version broke on quoting and failed against correct code.
  run bash -c "awk '/^  - name: Verify Host Access\$/{f=1;next} f&&/^  - name: /{exit} f' '$TPL' | grep -c 'dev_variant: true'"
  [ "$output" = "1" ]
  run bash -c "awk '/^  - name: Store SSH Password\$/{f=1;next} f&&/^  - name: /{exit} f' '$TPL' | grep -c 'dev_variant: true'"
  [ "$output" = "1" ]
}

@test "the probe play tolerates an unreachable host so the report still renders" {
  # An unreachable host is a verdict, not an error. Without this play keyword
  # Ansible drops the host before the report task, so the report's
  # "host unreachable by any method" branch is dead code and the operator gets
  # exit 4 instead of a NO-GO. `failed_when: false` does not cover it —
  # unreachable is a host state, not a task result.
  #
  # Scoped to the PROBE play's play-level keys: the slice runs from that play's
  # header to its `tasks:` line. A file-wide grep would pass with the keyword on
  # the pre-flight play (where it does nothing) or buried on a single task
  # (where it protects only that task).
  local head
  head=$(awk '/^- name: "Verify host access/{f=1} f{print} f&&/^  tasks:/{exit}' "$PB")
  [ -n "$head" ]
  echo "$head" | grep -qE '^  ignore_unreachable: true$'
}

@test "store-ssh-password lets the environment secret win over an extra var" {
  # Semaphore persists a task's extra-var JSON and serves it back over its API, so
  # `-e ssh_password=...` leaves the credential in plaintext outside OpenBao. An
  # environment secret is encrypted at rest and not API-readable.
  #
  # ORDER is the assertion. With the extra var first, a run that passed BOTH would
  # use the persisted one even though the secret was configured — the leak, still
  # open. The environment must be the first operand.
  local pb="$REPO_ROOT/platform/playbooks/store-ssh-password.yml"
  [ -f "$pb" ]
  grep -qE "_ssh_password:.*lookup\('env', *'SSH_PASSWORD'\) *\| *default\(ssh_password" "$pb"
  # The reverse order must NOT be present.
  ! grep -qE "_ssh_password:.*ssh_password *\| *default\(lookup" "$pb"

  # The write must consume the resolved fact. A bare `ssh_password` here would
  # store an EMPTY password whenever the value came from the environment.
  grep -qE "'become_password': _ssh_password" "$pb"
  grep -qE "'login_password': _ssh_password" "$pb"
  ! grep -qE "^ *- ssh_password is defined" "$pb"

  # Credentials cross this connection, so public cleartext is refused. The rule now
  # lives in ONE shared task (tasks/assert-bao-transport.yml) rather than a copy
  # per playbook; the pattern itself is tested in test_credential_leaks.bats.
  grep -qE "include_tasks: tasks/assert-bao-transport\.yml" "$pb"

  # And the operator-facing template must not still teach the leaking form.
  local tpl="$REPO_ROOT/platform/semaphore/templates.yml"
  local slice
  slice=$(awk '/^  - name: Store SSH Password$/{f=1} f&&/^  - name: Verify Host Access$/{exit} f{print}' "$tpl")
  [ -n "$slice" ]
  echo "$slice" | grep -q "SSH_PASSWORD"
  ! echo "$slice" | grep -qE '^ *# *-e ssh_password=<the bootstrap password>$'
}

@test "distribute-ssh-keys offers the bootstrap password only when one exists" {
  # A freshly built VM with no cloud-init key has no way in: this playbook writes
  # authorized_keys over a connection it does not create. login_password has always
  # been written to OpenBao for this and was read by nothing.
  #
  # The guard is what matters. An EMPTY ansible_password is NOT harmless — it makes
  # Ansible shell out to sshpass, so every already-keyed host would fail on a runner
  # without sshpass. Asserted on the add_host task's own slice, because a file-wide
  # grep for `when:` would pass on any of the playbook's other conditionals.
  local pb="$REPO_ROOT/platform/playbooks/distribute-ssh-keys.yml"
  [ -f "$pb" ]

  local slice
  slice=$(awk '/^    - name: "Offer it as a password fallback/{f=1} f{print} f&&/^$/{exit}' "$pb")
  [ -n "$slice" ]
  echo "$slice" | grep -qE 'ansible_password: "\{\{ _bootstrap_pw \}\}"'
  echo "$slice" | grep -qE '^      when: _bootstrap_pw \| length > 0$'

  # The read is an enhancement, so failing to read it must not abort the run.
  # errors='"'"'ignore'"'"' alone does not cover a lookup that raises while the task
  # arguments are templated (missing hvac, bad creds, unreachable OpenBao).
  grep -qE '^      rescue:$' "$pb"
  grep -qE '_bootstrap_pw: ""' "$pb"
}

@test "harden-ssh: a display variable cannot abort the verification it describes" {
  # service_name appears only in report strings, but an undefined one aborted the whole
  # "Confirm password auth disabled" task — so a host that had been correctly hardened
  # reported as a failure. A label must never be able to fail the check it labels.
  local PB="$BATS_TEST_DIRNAME/../playbooks/harden-ssh.yml"
  local bare
  bare=$(grep -c '{{ service_name }}' "$PB" || true)
  [ "$bare" -eq 0 ]
  grep -qF 'service_name | default(inventory_hostname)' "$PB"
}
