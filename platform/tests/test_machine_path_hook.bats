#!/usr/bin/env bats
# scripts/check-machine-paths.sh refuses a machine account's home directory in staged
# lines (docs/MISTAKES.md 3.9). Fixture paths are assembled at run time so this file does
# not trip the hook it tests.
load assert_helpers

setup() {
  HOOK="$BATS_TEST_DIRNAME/../../scripts/check-machine-paths.sh"
}

scan() { printf '%s\n' "$@" | "$HOOK" -; }

@test "machine-path hook: a macOS home directory is refused" {
  run scan "+++ b/docs/x.md" "+  wrote over /$(printf Users)/alice/.ssh/known_hosts"
  [ "$status" -eq 1 ]
  assert_contains "$output" "alice"
}

@test "machine-path hook: a Linux home directory is refused" {
  run scan "+  key: /$(printf home)/bob/.ssh/id_ed25519"
  [ "$status" -eq 1 ]
}

@test "machine-path hook: placeholders and service accounts pass" {
  run scan "+  /home/<user>/.config" "+  /home/{{ ansible_user }}/x" "+  /home/\${USER}/x" \
    "+  /home/step/certs" "+  /home/frappe/bench" "+  /home/node/app"
  [ "$status" -eq 0 ]
}

@test "machine-path hook: an allowed path does not hide a personal one on the same line" {
  run scan "+  cp /home/step/ca.crt /$(printf Users)/carol/ca.crt"
  [ "$status" -eq 1 ]
}

@test "machine-path hook: removed and context lines are not scanned" {
  run scan "-  /$(printf Users)/dave/old" "   /$(printf Users)/dave/context"
  [ "$status" -eq 0 ]
}

@test "machine-path hook: a longer name that starts like an allowed one is refused" {
  run scan "+  /$(printf home)/stepper/.ssh"
  [ "$status" -eq 1 ]
}
