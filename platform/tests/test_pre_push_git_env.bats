#!/usr/bin/env bats
# The pre-push hook must run the suites with a CLEAN git environment. Git exports
# GIT_DIR (and friends) to hooks; left set, a test's scratch-repo `git init` /
# `git config` writes the real repository's shared .git/config. On 2026-09-23 that
# set core.bare=true and a fake user.email for every checkout (docs/MISTAKES.md 3.7).
#
# Replays it: the hook runs as git would, with GIT_DIR pointing at a VICTIM repo, and a
# fake `bats` on PATH that does exactly what the offending setup did.

load assert_helpers

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  HOOK="$REPO_ROOT/.githooks/pre-push"
  cd "$BATS_TEST_TMPDIR"
  git init -q victim
  mkdir -p pushing/platform/tests fakebin
  git init -q pushing
  cat > fakebin/bats <<'SH'
#!/usr/bin/env sh
# What the 2026-09-23 test setup did, from a scratch directory.
cd "$(mktemp -d)" && git init -q scratch && cd scratch \
  && git config user.email hacked@example.invalid && git config user.name hacked
exit 0
SH
  chmod +x fakebin/bats
}

@test "pre-push: a suite's git commands cannot reach the pushing repository" {
  cd pushing
  echo "refs/heads/x 1111111111111111111111111111111111111111 refs/heads/x 0000000000000000000000000000000000000000" \
    | GIT_DIR="$BATS_TEST_TMPDIR/victim/.git" PATH="$BATS_TEST_TMPDIR/fakebin:$PATH" \
      sh "$HOOK" origin https://example.invalid/repo.git >/dev/null 2>&1 || true
  run git -C "$BATS_TEST_TMPDIR/victim" config --local user.email
  [ -z "$output" ] || { echo "victim user.email became: $output"; false; }
  run git -C "$BATS_TEST_TMPDIR/victim" config --local core.bare
  [ "$output" = "false" ] || { echo "victim core.bare became: $output"; false; }
}

@test "pre-push: the git environment is cleared before any suite runs" {
  assert_precedes "$HOOK" '^unset GIT_DIR GIT_WORK_TREE' 'if ! bats platform/tests/'
}

# The shared bats helper clears the same variables for every test that loads it
# (platform/tests/assert_helpers.bash). The two lists are kept identical by this test,
# so a variable added to one cannot be missed in the other.
_unset_list() {
  awk '/^unset GIT_DIR/ { on = 1 } on { print; if ($0 !~ /\\$/) exit }' "$1" \
    | tr -d '\\' | tr -s ' \t' '\n' | grep '^GIT_' | sort
}

@test "pre-push: assert_helpers clears exactly the git variables the hook clears" {
  hook=$(_unset_list "$HOOK")
  helper=$(_unset_list "$REPO_ROOT/platform/tests/assert_helpers.bash")
  [ "$(printf '%s\n' "$hook" | wc -l)" -ge 8 ]
  [ "$hook" = "$helper" ]
}
