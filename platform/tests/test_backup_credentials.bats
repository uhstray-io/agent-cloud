#!/usr/bin/env bats
# backup-credentials-to-site-config.yml moves live credentials from OpenBao into
# the private repo without any of them reaching a log. Two bugs found by running
# it end-to-end against a mock store and a local bare remote — neither visible to
# a syntax check, a linter, or any amount of reading:
#
#   1. `command: cmd:` word-splits, so `git config user.name Joseph A. Wisneski IV`
#      arrived as four arguments and git answered "no action specified".
#   2. A `vars:` entry holding lookup('pipe', 'date ...') is re-evaluated at EVERY
#      reference, so the branch created and the branch pushed were named one second
#      apart. The push failed with "src refspec does not match any" — AFTER the
#      credentials had been written and committed into the clone.
#
# The second is the dangerous shape: a half-finished run that leaves material in a
# clone and reports failure. Both are pinned below.

load assert_helpers

setup() {
  PBDIR="${BATS_TEST_DIRNAME}/../playbooks"
  PB="${PBDIR}/backup-credentials-to-site-config.yml"
  SSHPB="${PBDIR}/backup-service-ssh-key.yml"
  CLONE="${PBDIR}/tasks/site-config-clone.yml"
  PUSH="${PBDIR}/tasks/site-config-push.yml"
  SEM="${BATS_TEST_DIRNAME}/../semaphore"
}

@test "cred backup: the playbook exists and guards its transport" {
  [ -f "$PB" ]
  local n_url n_inc
  n_url=$(grep -cE '^    _bao_url:' "$PB")
  n_inc=$(grep -cE 'include_tasks: tasks/assert-bao-transport\.yml' "$PB")
  [ "$n_url" -gt 0 ]
  [ "$n_inc" -eq "$n_url" ]
}

@test "cred backup: no non-deterministic lookup sits in a play's vars block" {
  # THE LAZY-EVALUATION TRAP, as a CLOSED rule across every playbook rather than a
  # note on this one. A `vars:` entry is re-evaluated at each reference, so any
  # lookup whose result can change between two references silently yields two
  # different values — and the failure surfaces far from the cause. A value that
  # must be stable belongs in set_fact, which evaluates once and stores.
  #
  # Scoped to lookups that are actually non-deterministic. lookup('file', ...) in
  # vars is fine and is used deliberately elsewhere; re-reading a file gives the
  # same answer.
  local offenders
  offenders=$(python3 - "$PBDIR" <<'PY_VARS'
import re, sys, pathlib
root = pathlib.Path(sys.argv[1])
# pipe/date/random/now: results differ between two evaluations.
BAD = re.compile(r"lookup\(\s*['\"](pipe|url|random_choice)['\"]|\bnow\(\)|\|\s*random\b")
for path in sorted(root.rglob('*.yml')):
    lines = path.read_text().split('\n')
    in_vars = False
    vars_indent = 0
    for n, line in enumerate(lines, 1):
        # Flow-style `vars: { a: "{{ lookup('pipe', ...) }}" }` sits on one line and
        # a block-style scanner walks straight past it. Check the declaration line
        # itself when it carries an inline mapping.
        f = re.match(r'^(\s*)vars:\s*\{.*$', line)
        if f:
            if BAD.search(line):
                print("%s:%d: %s" % (path, n, line.strip()[:80]))
            continue
        m = re.match(r'^(\s*)vars:\s*$', line)
        if m:
            in_vars, vars_indent = True, len(m.group(1))
            continue
        if in_vars:
            stripped = line.strip()
            if stripped and (len(line) - len(line.lstrip())) <= vars_indent:
                in_vars = False
            elif BAD.search(line):
                print("%s:%d: %s" % (path, n, stripped[:80]))
PY_VARS
)
  if [ -n "$offenders" ]; then
    echo "non-deterministic lookup inside a play vars block:" >&2
    printf '%s\n' "$offenders" >&2
    echo "move it to set_fact — a vars entry is re-evaluated at every reference." >&2
    return 1
  fi
  # And the shared clone task does freeze its branch name that way.
  assert_grep -q '_sc_branch: >-' "$CLONE"
  local blk
  blk=$(awk '/^- name: "Freeze the branch name/ { f = 1; next }
             f && /^- name:/ { exit }
             f { print }' "$CLONE")
  [ -n "$blk" ]
  assert_grep -q 'ansible.builtin.set_fact' <<<"$blk"
}

@test "cred backup: every git call carrying a space uses argv, not cmd" {
  # `command` never invokes a shell, so the quotes that look like they protect a
  # multi-word argument are consumed by YAML instead. A cmd string is only safe
  # when no argument contains a space, and the two that do here are a person's
  # name and a commit message.
  local bad
  # [[:space:]], not \s — awk's ERE has no \s, so the anchor matched nothing and
  # every selector silently inspected zero lines (the reviewer caught it; the
  # fixed form is mutation-tested by reintroducing a cmd: in the Commit task).
  bad=$(awk '/^[[:space:]]*- name: "Identify the commit"/,/changed_when/ { if (/cmd:/) print "identity task uses cmd:" }' "$CLONE"
        awk '/^[[:space:]]*- name: "Commit"/,/chdir:/ { if (/cmd:/) print "commit task uses cmd:" }
             /^[[:space:]]*- name: "Stage only the path this run wrote"/,/chdir:/ { if (/cmd:/) print "stage task uses cmd:" }' "$PUSH")
  if [ -n "$bad" ]; then
    printf '%s\n' "$bad" >&2
    return 1
  fi
  assert_grep -q 'argv: \["git", "config", "user.{{ item.k }}", "{{ item.v }}"\]' "$CLONE"
}

@test "cred backup: the scratch dir is wiped even when the run fails" {
  # It holds every credential fetched. Leaving it on the orchestrator after a
  # failure is worse than the failure. `always`, not a final task — a final task
  # does not run when an earlier one aborts the block. BOTH callers of the shared
  # clone task, since the scratch dir is created inside the include and only the
  # caller can wrap it.
  local pb blk
  for pb in "$PB" "$SSHPB"; do
    assert_grep -qE '^      always:' "$pb"
    blk=$(awk '/^      always:/ { f = 1; next } f { print }' "$pb")
    [ -n "$blk" ]
    assert_grep -q 'state: absent' <<<"$blk"
    assert_grep -q '_sc_wd' <<<"$blk"
  done
}

@test "cred backup: nothing it prints can carry a credential value" {
  # CLOSED: every debug msg in this playbook is checked, not just the ones that
  # look risky. The rule is that a message may reference field NAMES, counts and
  # paths — never an indexed read of the secret payload, which is what a value is.
  local leaks
  leaks=$(awk '/ansible\.builtin\.debug:/ { d = 1 } d && /_secret\.json\.data\.data\[/ { print NR": "$0 }
               /^    - name:/ { d = 0 }' "$PB")
  if [ -n "$leaks" ]; then
    echo "a debug task indexes the secret payload — that prints a value:" >&2
    printf '%s\n' "$leaks" >&2
    return 1
  fi
  # The tasks that DO touch values are no_log'd.
  local writes
  writes=$(awk '/- name: "Write each field to its own file"/ { f = 1 } f && /no_log: true/ { print "ok"; exit }' "$PB")
  [ "$writes" = "ok" ]
}

@test "cred backup: the store path is built from a validated name, not taken whole" {
  # A caller-supplied path would let one invocation walk the entire store into a
  # git repo. The service name is constrained because it is interpolated into both
  # an OpenBao path and a filesystem path.
  assert_grep -qE '^    _bao_path: "secret/data/services/\{\{ credential_service' "$PB"
  assert_grep -q "credential_service is match('\^\[a-z0-9\]\[a-z0-9_-\]\*\$')" "$PB"
  refute_grep -qE '^    _bao_path: "\{\{ (bao_path|credential_path)' "$PB"
}

@test "cred backup: the deploy key exists on the runner only inside the dir that is always wiped" {
  # The key is read from OpenBao with the token the play already holds, written
  # 0600 into the scratch dir, and dies with that dir. Pin each piece: the store
  # path is fixed (not caller-supplied), the write is 0600 and no_log, ssh is told
  # to use exactly that file and nothing the runner's agent or ~/.ssh may hold,
  # host keys are pinned, and the key file lives UNDER _sc_wd — anywhere else and
  # the always: wipe would miss it.
  local pb
  for pb in "$PB" "$SSHPB"; do
    assert_grep -qE '^    _key_path: "secret/data/services/ssh/site-config"$' "$pb"
    refute_grep -qE '^    _key_path: "\{\{' "$pb"
    assert_grep -q '_sc_deploy_key: "{{ _key.json.data.data.private_key }}"' "$pb"
  done
  local blk
  blk=$(awk '/^- name: "Materialise the deploy key/ { f = 1; next } f && /^- name:/ { exit } f { print }' "$CLONE")
  [ -n "$blk" ]
  assert_grep -qE '^    mode: "0600"' <<<"$blk"
  assert_grep -qE '^  no_log: true' <<<"$blk"
  assert_grep -qE '^    dest: "\{\{ _sc_key \}\}"' <<<"$blk"
  assert_grep -qE '^    _sc_key: "\{\{ _sc_wd_result.path \}\}/deploy_key"' "$CLONE"
  # Exactly one GIT_SSH_COMMAND definition; it names that file, IdentitiesOnly, and
  # strict pinned host keys. Comments may name accept-new; code may not.
  local code
  code=$(grep -vE '^[[:space:]]*#' "$CLONE")
  [ "$(grep -c 'GIT_SSH_COMMAND:' <<<"$code")" -eq 1 ]
  blk=$(awk '/GIT_SSH_COMMAND: >-/ { f = 1; next } f && /^[^ ]/ { exit } f { print }' "$CLONE")
  assert_grep -q 'ssh -i {{ _sc_wd_result.path }}/deploy_key -o IdentitiesOnly=yes' <<<"$blk"
  assert_grep -q 'StrictHostKeyChecking=yes' <<<"$blk"
  assert_grep -q 'UserKnownHostsFile=' <<<"$blk"
  refute_grep -q 'accept-new' <<<"$code"
  # And the key is written only AFTER the scratch dir exists, never before.
  local mk wr
  mk=$(grep -nE '^- name: "Create a scratch dir' "$CLONE" | cut -d: -f1)
  wr=$(grep -nE '^- name: "Materialise the deploy key' "$CLONE" | cut -d: -f1)
  [ -n "$mk" ]; [ -n "$wr" ]; [ "$mk" -lt "$wr" ]
}

@test "cred backup: the key is never taken from a task parameter, and the seed path says why" {
  # Semaphore persists a task's extra vars and serves them back over its API, so a
  # deploy key passed as -e would live in the orchestrator's database in plaintext.
  # The only way in is the store; the only way into the store is the Seed template
  # with the value as an environment SECRET — which is what every "key missing"
  # message must point at.
  local pb
  for pb in "$PB" "$SSHPB"; do
    refute_grep -qE 'deploy_key \| default|site_config_deploy_key|lookup\(.env., .(SITE_CONFIG|DEPLOY)' "$pb"
    assert_grep -q 'BAO_VALUE' "$pb"
  done
  assert_grep -q 'BAO_VALUE' "$CLONE"
}
