#!/usr/bin/env bats
# scripts/check-machine-paths.sh refuses a machine account's home directory in staged
# lines (docs/MISTAKES.md 4.11). Fixture paths are assembled at run time so this file does
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

@test "machine-path hook: an added line that itself starts with ++ is still scanned" {
  run scan "+++ b/docs/x.md" "+++ /$(printf Users)/erin/notes"
  [ "$status" -eq 1 ]
}

@test "machine-path hook: a renamed file's added lines are scanned" {
  repo="$BATS_TEST_TMPDIR/repo"
  env -u GIT_DIR -u GIT_WORK_TREE -u GIT_INDEX_FILE git init -q "$repo"
  cd "$repo"
  # Enough unchanged lines that git reports a rename (R), not a delete plus an add.
  seq 1 40 > a.txt
  env -u GIT_DIR -u GIT_WORK_TREE -u GIT_INDEX_FILE git add a.txt
  env -u GIT_DIR -u GIT_WORK_TREE -u GIT_INDEX_FILE git -c user.name=t -c user.email=t@example.com commit -qm init
  env -u GIT_DIR -u GIT_WORK_TREE -u GIT_INDEX_FILE git mv a.txt b.txt
  printf '/%s/frank/x\n' Users >> b.txt
  env -u GIT_DIR -u GIT_WORK_TREE -u GIT_INDEX_FILE git add b.txt
  run env -u GIT_DIR -u GIT_WORK_TREE -u GIT_INDEX_FILE "$HOOK"
  [ "$status" -eq 1 ]
}

@test "staged-diff gates: every git diff --cached filter includes renamed files" {
  # A --diff-filter without R skips a renamed file's content, so a leak moved in by a
  # rename passes the gate (review of #339; the private-IP, credential and .env hooks had
  # ACM until 2026-09-29).
  cfg="$BATS_TEST_DIRNAME/../../.pre-commit-config.yaml"
  hook="$BATS_TEST_DIRNAME/../../scripts/check-machine-paths.sh"
  run grep -hoE -- '--diff-filter=[A-Z]+' "$cfg" "$hook"
  [ "$status" -eq 0 ]
  [ "$(printf '%s\n' "$output" | wc -l | tr -d ' ')" -ge 4 ]
  refute_contains "$(printf '%s\n' "$output" | grep -v R || true)" "diff-filter"
}
