#!/usr/bin/env bats
# The pre-push hook must run the suites with a CLEAN git environment. Git exports
# GIT_DIR (and friends) to hooks; left set, a test's scratch-repo `git init` /
# `git config` writes the real repository's shared .git/config. On 2026-09-23 that
# set core.bare=true and a fake user.email for every checkout (docs/MISTAKES.md 3.7).
#
# Replays it: the hook runs as git would, with GIT_DIR pointing at a VICTIM repo, and a
# fake `bats` on PATH that does exactly what the offending setup did.

load assert_helpers
load pre_push_hook_harness

setup() {
  # This file itself runs under the hook: clear the same variables first.
  unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR GIT_OBJECT_DIRECTORY \
    GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_PREFIX GIT_NAMESPACE
  REPO_ROOT=$(git rev-parse --show-toplevel)
  HOOK="$REPO_ROOT/.githooks/pre-push"
  setup_pre_push_hook_harness
  cd "$BATS_TEST_TMPDIR"
  git init -q victim
}

assert_fixture_value_suppressed() {
  case "$output" in
    *"$PRE_PUSH_FIXTURE_VALUE"*)
      echo "captured fixture value reached hook output" >&2
      return 1
      ;;
  esac
}

assert_mixed_collection_failure_blocks() {
  local scenario=$1
  local expected_summary=$2
  run_pre_push_scenario "$scenario"
  [ "$status" -eq 1 ]
  assert_pre_push_event_count pytest-full 0
  assert_pre_push_event_count scoped-fallback 0
  assert_contains "$output" 'Python collection FAILED'
  assert_contains "$output" "$expected_summary"
  assert_fixture_value_suppressed
  assert_contains "$output" 'PUSH BLOCKED: a test suite failed.'
}

@test "pre-push: a suite's git commands cannot reach the pushing repository" {
  cat > "$PRE_PUSH_FAKEBIN/bats" <<'SH'
#!/usr/bin/env sh
# What the 2026-09-23 test setup did, from a scratch directory.
cd "$(mktemp -d)" && git init -q scratch && cd scratch \
  && git config user.email hacked@example.invalid && git config user.name hacked
exit 0
SH
  chmod +x "$PRE_PUSH_FAKEBIN/bats"

  cd "$PRE_PUSH_REPO"
  GIT_DIR="$BATS_TEST_TMPDIR/victim/.git" sh "$HOOK" \
    origin https://example.invalid/repo.git < "$PRE_PUSH_INPUT" >/dev/null 2>&1 || true
  run git -C "$BATS_TEST_TMPDIR/victim" config --local user.email
  [ -z "$output" ] || { echo "victim user.email became: $output"; false; }
  run git -C "$BATS_TEST_TMPDIR/victim" config --local core.bare
  [ "$output" = "false" ] || { echo "victim core.bare became: $output"; false; }
}

@test "pre-push: the git environment is cleared before any suite runs" {
  assert_precedes "$HOOK" '^unset GIT_DIR GIT_WORK_TREE' '^[[:space:]]+if ! bats platform/tests/; then$'
  run_pre_push_scenario healthy
  [ "$status" -eq 0 ]
  assert_pre_push_event_count 'git-env:bats:clean' 1
  assert_pre_push_event_count 'git-env:pytest-collect:clean' 1
  assert_pre_push_event_count 'git-env:pytest-full:clean' 1
}

@test "pre-push: healthy collection runs pytest once and no scoped fallback" {
  run_pre_push_scenario healthy
  [ "$status" -eq 0 ] || { echo "$output"; false; }
  assert_pre_push_event_count bats 1
  assert_pre_push_event_count pytest-collect 1
  assert_pre_push_event_count pytest-full 1
  assert_pre_push_event_count scoped-fallback 0
  assert_pre_push_event_count 'runner-context:bats:root' 1
  assert_pre_push_event_count 'runner-context:pytest-collect:root:expected-pythonpath' 1
  assert_pre_push_event_count 'runner-context:pytest-full:root:expected-pythonpath' 1
  assert_contains "$output" 'ScopedPublicationTests::test_named_progress PASSED'
  assert_contains "$output" 'BATS suite START'
  assert_contains "$output" 'BATS suite END status=passed elapsed='
  assert_contains "$output" 'Python collection START'
  assert_contains "$output" 'Python collection END status=passed elapsed='
  assert_contains "$output" 'Python suite END status=passed elapsed='
}

@test "pre-push: unrelated missing dependency runs the scoped fallback once" {
  run_pre_push_scenario missing-pass
  [ "$status" -eq 0 ] || { echo "$output"; false; }
  assert_pre_push_event_count pytest-collect 1
  assert_pre_push_event_count pytest-full 0
  assert_pre_push_event_count scoped-fallback 1
  assert_pre_push_event_count 'runner-context:scoped-fallback:root:expected-pythonpath' 1
  assert_contains "$output" 'Python suite not collectable here — a dependency is missing'
  assert_contains "$output" 'dependency: unrelated_dependency'
  assert_contains "$output" 'test_scope (__main__.ScopedPublicationTests.test_scope) ... ok'
  assert_contains "$output" 'Scoped publication fallback END status=passed elapsed='
}

@test "pre-push: a scoped fallback assertion failure is sanitized and blocks" {
  run_pre_push_scenario missing-fail
  [ "$status" -eq 1 ]
  assert_pre_push_event_count pytest-full 0
  assert_pre_push_event_count scoped-fallback 1
  assert_contains "$output" 'test_rejects_drift (__main__.ScopedPublicationTests.test_rejects_drift) ... FAIL'
  assert_contains "$output" 'FAILED (failures=1)'
  assert_contains "$output" 'Scoped publication fallback END status=failed elapsed='
  refute_contains "$output" "$PRE_PUSH_FIXTURE_VALUE"
  assert_contains "$output" 'PUSH BLOCKED: a test suite failed.'
}

@test "pre-push: a missing dependency inside the scoped fallback blocks" {
  run_pre_push_scenario missing-fallback-dep
  [ "$status" -eq 1 ]
  assert_pre_push_event_count scoped-fallback 1
  assert_contains "$output" 'dependency: scoped_dependency'
  assert_contains "$output" 'Scoped publication fallback END status=failed elapsed='
  refute_contains "$output" "$PRE_PUSH_FIXTURE_VALUE"
  refute_contains "$output" 'Traceback'
  assert_contains "$output" 'PUSH BLOCKED: a test suite failed.'
}

@test "pre-push: missing dependency plus syntax error blocks without fallback" {
  assert_mixed_collection_failure_blocks mixed-missing-syntax \
    'collection exception: SyntaxError'
}

@test "pre-push: syntax error before missing dependency still blocks without fallback" {
  assert_mixed_collection_failure_blocks mixed-syntax-missing \
    'collection exception: SyntaxError'
}

@test "pre-push: missing dependency plus configuration failure blocks without fallback" {
  assert_mixed_collection_failure_blocks mixed-missing-config \
    'collection error: pytest configuration'
}

@test "pre-push: missing dependency plus another collection exception blocks without fallback" {
  assert_mixed_collection_failure_blocks mixed-missing-exception \
    'collection exception: ValueError'
}

@test "pre-push: syntax collection failure blocks without fallback" {
  run_pre_push_scenario syntax
  [ "$status" -eq 1 ]
  assert_pre_push_event_count pytest-full 0
  assert_pre_push_event_count scoped-fallback 0
  assert_contains "$output" 'Python collection FAILED'
  assert_contains "$output" 'collection exception: SyntaxError'
  assert_fixture_value_suppressed
}

@test "pre-push: pytest configuration failure blocks without fallback" {
  run_pre_push_scenario config
  [ "$status" -eq 1 ]
  assert_pre_push_event_count pytest-full 0
  assert_pre_push_event_count scoped-fallback 0
  assert_contains "$output" 'Python collection FAILED'
  assert_contains "$output" 'collection error: pytest configuration'
  assert_fixture_value_suppressed
}

@test "pre-push: normal scoped pytest failure blocks without fallback" {
  run_pre_push_scenario scoped-fail
  [ "$status" -eq 1 ]
  assert_pre_push_event_count pytest-full 1
  assert_pre_push_event_count scoped-fallback 0
  assert_contains "$output" 'ScopedPublicationTests::test_named_progress FAILED'
  assert_contains "$output" 'Python suite END status=failed elapsed='
  assert_contains "$output" 'PUSH BLOCKED: a test suite failed.'
}
