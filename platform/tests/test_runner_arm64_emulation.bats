#!/usr/bin/env bats
# arm64 image-build emulation on the runner hosts (deploy-github-runner.yml).
#
# The handler is what lets a multi-arch build run arm64 binaries on an amd64 host. Its
# failure is silent at deploy time: the build fails later with "exec format error", on a
# host the deploy reported as ready. So the deploy proves the handler from the kernel's own
# status file, and these tests pin that proof.
#
# Run: bats platform/tests/test_runner_arm64_emulation.bats

load assert_helpers

setup() {
  PLAYBOOK="$BATS_TEST_DIRNAME/../playbooks/deploy-github-runner.yml"
  [ -f "$PLAYBOOK" ]
}

@test "arm64 emulation: qemu-user-static is installed through apt" {
  local install
  install=$(task_block "$PLAYBOOK" 'Install user-mode qemu for arm64 binaries')
  assert_grep -qF 'ansible.builtin.apt:' <<<"$install"
  assert_grep -qF 'name: qemu-user-static' <<<"$install"
}

@test "arm64 emulation: the handler is read from the kernel's status file, before and after" {
  local before after assertion
  before=$(task_block "$PLAYBOOK" 'Read the arm64 binfmt handler'"'"'s state before registering it')
  after=$(task_block "$PLAYBOOK" "Read the arm64 binfmt handler's state after registering it")
  assertion=$(task_block "$PLAYBOOK" 'Require the arm64 binfmt handler to be registered and enabled')
  # Both reads go to the kernel's file, and both are real under --check: a read without
  # check_mode false is silently skipped by a dry run.
  assert_grep -qF 'cat /proc/sys/fs/binfmt_misc/qemu-aarch64' <<<"$before"
  assert_grep -qF 'cat /proc/sys/fs/binfmt_misc/qemu-aarch64' <<<"$after"
  assert_grep -qF 'check_mode: false' <<<"$before"
  assert_grep -qF 'check_mode: false' <<<"$after"
  assert_grep -qF 'changed_when: false' <<<"$after"
  # The assert judges the POST-registration read, not the pre-read it may have skipped.
  assert_grep -qE 'register: _qemu_binfmt$' <<<"$after"
  assert_grep -qF '_qemu_binfmt.stdout_lines' <<<"$assertion"
}

@test "arm64 emulation: the first status line must be exactly 'enabled'" {
  local assertion
  assertion=$(task_block "$PLAYBOOK" 'Require the arm64 binfmt handler to be registered and enabled')
  # Exact compare on the first line: a substring match would pass a handler whose flags or
  # interpreter line happens to contain the word.
  assert_grep -qF "(_qemu_binfmt.stdout_lines | default([]) | first | default('')) == 'enabled'" <<<"$assertion"
}

@test "arm64 emulation: the check-mode skip covers only what the install or restart would change" {
  local assertion
  assertion=$(task_block "$PLAYBOOK" 'Require the arm64 binfmt handler to be registered and enabled')
  # Skipped under --check only when a simulated write means the handler cannot exist yet.
  # Any other case asserts for real, so a dry run cannot pass on an empty read.
  # At the task-key indent (6), not inside the assert's arguments (8): a nested `when` is an
  # unsupported module parameter, and the playbook fails at run time, not at lint.
  assert_grep -qE "^      when: not \(ansible_check_mode and \(\(_qemu_pkg.changed \| default\(false\)\) or \(_qemu_restart.changed \| default\(false\)\)\)\)$" <<<"$assertion"
}

@test "arm64 emulation: the handler is registered now through the module, gated on its absence" {
  local restart
  restart=$(task_block "$PLAYBOOK" 'Register the arm64 binfmt handler now, not only at the next boot')
  assert_grep -qF 'ansible.builtin.systemd_service:' <<<"$restart"
  assert_grep -qF 'name: systemd-binfmt.service' <<<"$restart"
  assert_grep -qF 'state: restarted' <<<"$restart"
  assert_grep -qF "!= 'enabled'" <<<"$restart"
  # Gated on the state read BEFORE registering: an undefined or later variable would restart
  # systemd-binfmt on every deploy, re-registering handlers under running emulated builds.
  assert_grep -qF "when: (_qemu_binfmt_before.stdout_lines" <<<"$restart"
  # A shell systemctl here would be a host write the check-mode contract has to guard; the
  # module simulates under --check by itself.
  refute_grep -qE 'systemctl' <<<"$restart"
}

@test "arm64 emulation: placed after the sudo refusal and before the setuid strip" {
  assert_precedes "$PLAYBOOK" 'Refuse to proceed if the runner account can escalate' 'Install user-mode qemu for arm64 binaries'
  assert_precedes "$PLAYBOOK" 'Require the arm64 binfmt handler to be registered and enabled' 'Remove the setuid and setgid bits a runner host does not use'
}

@test "arm64 emulation: the playbook's own assert passes only on an enabled first line" {
  # Runs the real assert task, not a copy of its predicate, so an edit to the playbook is
  # what gets judged (docs/MISTAKES.md §2.5, §1.5: a guard is not designed until it has
  # been observed refusing the thing it forbids).
  command -v ansible-playbook >/dev/null 2>&1 || skip "ansible-playbook not available"
  local task pb out
  task="$BATS_TEST_TMPDIR/assert-task.yml"
  pb="$BATS_TEST_TMPDIR/assert.yml"
  out="$BATS_TEST_TMPDIR/out.txt"
  # task_block starts AFTER the `- name:` line, so the header is put back for a valid task.
  {
    printf '%s\n' '    - name: "Require the arm64 binfmt handler to be registered and enabled"'
    task_block "$PLAYBOOK" 'Require the arm64 binfmt handler to be registered and enabled'
  } > "$task"
  # A zero-task playbook passes, so an empty extraction must fail the test here, not pass it.
  [ -s "$task" ]
  {
    echo '- hosts: localhost'
    echo '  connection: local'
    echo '  gather_facts: false'
    echo '  vars:'
    echo '    _qemu_pkg: {changed: false}'
    echo '    _qemu_restart: {changed: false}'
    echo '  tasks:'
    cat "$task"
  } > "$pb"

  run_fixture() {
    printf '%s' "$1" > "$BATS_TEST_TMPDIR/fixture.json"
    ansible-playbook "$pb" -e "@$BATS_TEST_TMPDIR/fixture.json" > "$out" 2>&1
  }

  # Enabled: the assert passes and prints the interpreter and flags it verified. The values are
  # what Ubuntu 24.04's qemu-user-static registers (debian/binfmt-install): flags OPF, no C.
  run run_fixture '{"_qemu_binfmt":{"stdout_lines":["enabled","interpreter /usr/libexec/qemu-binfmt/aarch64-binfmt-P","flags: OPF"]}}'
  [ "$status" -eq 0 ]
  assert_grep -qF 'arm64 binfmt handler enabled' "$out"

  # Disabled, enabled-on-a-later-line, and absent all refuse with the named message.
  run run_fixture '{"_qemu_binfmt":{"stdout_lines":["disabled","interpreter /usr/bin/qemu-aarch64-static"]}}'
  [ "$status" -ne 0 ]
  assert_grep -qF 'The arm64 binfmt handler is not enabled' "$out"

  run run_fixture '{"_qemu_binfmt":{"stdout_lines":["disabled","enabled"]}}'
  [ "$status" -ne 0 ]
  assert_grep -qF 'The arm64 binfmt handler is not enabled' "$out"

  run run_fixture '{"_qemu_binfmt":{"stdout_lines":[]}}'
  [ "$status" -ne 0 ]
  assert_grep -qF 'The arm64 binfmt handler is not enabled' "$out"
}
