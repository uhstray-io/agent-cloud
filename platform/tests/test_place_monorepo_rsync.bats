#!/usr/bin/env bats
# Local placement must use GNU rsync, never macOS openrsync.
#
# macOS ships /usr/bin/rsync as openrsync, which ignores the `--filter=':- .gitignore'`
# rule on --delete. The local placement then deleted gitignored generated files such as
# Authentik's blueprints-active/ on every genesis bootstrap, and the running worker kept a
# stale bind mount, so its verify step failed. GNU rsync 3.x honours the filter.
#
# One script decides (scripts/require-gnu-rsync.sh); the local preflight and place-monorepo.yml
# both run it. These tests run it against stub rsync binaries, so breaking its logic fails them.
# Run: bats platform/tests/test_place_monorepo_rsync.bats

load assert_helpers

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  PM="$REPO_ROOT/platform/playbooks/tasks/place-monorepo.yml"
  CHECK="$REPO_ROOT/scripts/require-gnu-rsync.sh"
  STUBS="$BATS_TEST_TMPDIR/bin"
  mkdir -p "$STUBS"
}

# stub_rsync <first line of --version>: an rsync on a PATH that holds nothing else of note.
stub_rsync() {
  printf '#!/bin/sh\necho "%s"\necho "second line"\n' "$1" > "$STUBS/rsync"
  chmod +x "$STUBS/rsync"
}

run_check() { run env PATH="$STUBS:/usr/bin:/bin" bash "$CHECK"; }

@test "rsync check: GNU rsync 3.x passes" {
  stub_rsync 'rsync  version 3.4.3  protocol version 32'
  run_check
  [ "$status" -eq 0 ]
  assert_contains "$output" 'rsync  version 3.4.3'
}

@test "rsync check: a v-prefixed GNU 3.x banner passes" {
  stub_rsync 'rsync  version v3.2.7  protocol version 31'
  run_check
  [ "$status" -eq 0 ]
}

@test "rsync check: openrsync is refused, with the fix in the message" {
  stub_rsync 'openrsync: protocol version 29'
  run_check
  [ "$status" -eq 1 ]
  assert_contains "$output" 'brew install rsync'
}

@test "rsync check: GNU rsync 2.x is refused" {
  stub_rsync 'rsync  version 2.6.9  protocol version 29'
  run_check
  [ "$status" -eq 1 ]
}

@test "rsync check: no rsync on PATH is refused" {
  # A PATH with the script's own tools (sed, grep) and no rsync at all, on any host.
  local t
  for t in sed grep; do ln -s "$(command -v "$t")" "$STUBS/$t"; done
  run env PATH="$STUBS" "$(command -v bash)" "$CHECK"
  [ "$status" -eq 1 ]
  assert_contains "$output" 'not found'
}

@test "rsync: place-monorepo runs the check before the local copy, in local mode only" {
  grep -qF 'scripts/require-gnu-rsync.sh"' "$PM"
  assert_precedes "$PM" '^- name: "Refuse an rsync that ignores' 'rsync -rlpti'
  # Scoped like the copy it guards: local mode, not a dry run.
  sed -n '/^- name: "Refuse an rsync that ignores/,/^- name: /p' "$PM" | grep -qF -- '- local_mode | default(false) | bool'
  sed -n '/^- name: "Refuse an rsync that ignores/,/^- name: /p' "$PM" | grep -qF -- '- not ansible_check_mode'
}

@test "rsync: the Brewfile declares GNU rsync and the preflight runs the same check" {
  grep -qE '^brew "rsync"' "$REPO_ROOT/Brewfile"
  sed -n '/^preflight()/,/^}/p' "$REPO_ROOT/scripts/local-dev.sh" | grep -qF 'scripts/require-gnu-rsync.sh'
}
