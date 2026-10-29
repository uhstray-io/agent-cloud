#!/usr/bin/env bats
# Local placement must use GNU rsync, never macOS openrsync.
#
# macOS ships /usr/bin/rsync as openrsync, which ignores the `--filter=':- .gitignore'`
# rule on --delete. The local placement then deleted gitignored generated files such as
# Authentik's blueprints-active/ on every genesis bootstrap, and the running worker kept a
# stale bind mount, so its verify step failed. GNU rsync 3.x honours the filter. The
# placement refuses any rsync that is not 3.x, the Brewfile declares Homebrew's rsync, and
# the local-dev preflight checks for it before anything runs.
#
# Run: bats platform/tests/test_place_monorepo_rsync.bats

load assert_helpers

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  PM="$REPO_ROOT/platform/playbooks/tasks/place-monorepo.yml"
}

@test "rsync: place-monorepo refuses a non-GNU rsync before the local copy" {
  # The gate must be a real task (not a comment) that runs before the --delete copy.
  grep -qE '^- name: "Refuse an rsync that ignores .gitignore' "$PM"
  # It accepts only GNU rsync 3.x: the version regex is the same one preflight uses.
  grep -qF '^rsync +version 3\\.' "$PM"
  assert_precedes "$PM" '^- name: "Refuse an rsync that ignores' 'rsync -rlpti'
}

@test "rsync: the Brewfile declares GNU rsync" {
  grep -qE '^brew "rsync"' "$REPO_ROOT/Brewfile"
}

@test "rsync: local-dev preflight requires GNU rsync 3.x" {
  # Textual check on the preflight() body only, so a match elsewhere in the script cannot pass it.
  sed -n '/^preflight()/,/^}/p' "$REPO_ROOT/scripts/local-dev.sh" | grep -qF 'rsync +version 3\.'
}
