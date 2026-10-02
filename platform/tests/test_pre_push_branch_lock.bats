#!/usr/bin/env bats
# The pre-push hook runs ONE test run per branch at a time (docs/MISTAKES.md §5.11).
#
# On 2026-09-24 a backgrounded push's buffered output was read as finished and a second
# push of the same branch started while the first was still inside the suites. The hook
# now takes a per-branch lock under the shared git dir and refuses a second push of the
# same branch while the first holds it. These tests drive the real hook against a scratch
# repository, with a fake `bats` on PATH that can be held open on demand, so "the first
# push is still running" is a fact the test controls rather than a timing guess.

load assert_helpers

setup() {
  # This file itself runs under the hook: clear git's exported repository variables, or
  # the scratch repo's git commands reach the real one (docs/MISTAKES.md 3.7).
  unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR GIT_OBJECT_DIRECTORY \
    GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_PREFIX GIT_NAMESPACE
  REPO_ROOT=$(git rev-parse --show-toplevel)
  HOOK="$REPO_ROOT/.githooks/pre-push"
  # CI's sh is dash; macOS's is bash in POSIX mode. HOOK_SH lets either be exercised.
  HOOK_SH=${HOOK_SH:-sh}
  T="$BATS_TEST_TMPDIR"
  mkdir -p "$T/fakebin"
  git init -q "$T/repo"
  mkdir -p "$T/repo/platform/tests"   # so the hook reaches its BATS step
  LOCKS="$T/repo/.git/pre-push-locks"
  # Fake bats: records that the suite started, then, when FAKE_BATS_HOLD names a file,
  # stays "running" until that file exists (bounded, so a broken test cannot hang CI).
  cat > "$T/fakebin/bats" <<'SH'
#!/usr/bin/env sh
[ -n "${FAKE_BATS_STARTED:-}" ] && : > "$FAKE_BATS_STARTED"
if [ -n "${FAKE_BATS_HOLD:-}" ]; then
  i=0
  while [ ! -e "$FAKE_BATS_HOLD" ] && [ "$i" -lt 300 ]; do sleep 0.1; i=$((i + 1)); done
fi
exit "${FAKE_BATS_RC:-0}"
SH
  chmod +x "$T/fakebin/bats"
  BG_PID=""
}

teardown() {
  : > "$T/release"
  [ -n "$BG_PID" ] && wait "$BG_PID" 2>/dev/null || true
}

# One push line per ref, as git writes them on the hook's stdin.
refline() { echo "refs/heads/$1 1111111111111111111111111111111111111111 refs/heads/$1 0000000000000000000000000000000000000000"; }

# push [VAR=value ...] branch... — run the hook in the scratch repo, in the foreground,
# with one stdin line per branch and the given environment.
push() {
  local envs=() b
  while [ $# -gt 0 ] && [[ "$1" == *=* ]]; do envs+=("$1"); shift; done
  for b in "$@"; do refline "$b"; done \
    | (cd "$T/repo" && env ${envs[@]+"${envs[@]}"} PATH="$T/fakebin:$PATH" \
        "$HOOK_SH" "$HOOK" origin https://example.invalid/repo.git)
}

# Start a push of branch $1 that stays inside its suite until $T/release exists.
start_holding_push() {
  ( refline "$1" | (cd "$T/repo" && FAKE_BATS_STARTED="$T/started" FAKE_BATS_HOLD="$T/release" \
      PATH="$T/fakebin:$PATH" exec "$HOOK_SH" "$HOOK" origin https://example.invalid/repo.git) \
      > "$T/first.out" 2>&1 ) &
  BG_PID=$!
  local i=0
  while [ ! -e "$T/started" ] && [ "$i" -lt 100 ]; do sleep 0.1; i=$((i + 1)); done
  [ -e "$T/started" ] || { echo "first push never reached its suite"; cat "$T/first.out"; false; }
}

# No lock file may remain once the hook has exited.
assert_no_locks() {
  local left
  left=$(find "$LOCKS" -mindepth 1 2>/dev/null)
  [ -z "$left" ] || { echo "left behind: $left"; false; }
}

@test "pre-push lock: a second push of the same branch is refused while the first runs" {
  start_holding_push feat
  run push feat
  [ "$status" -ne 0 ] || { echo "second push was allowed: $output"; false; }
  assert_contains "$output" 'PUSH REFUSED'
  assert_contains "$output" 'refs/heads/feat'
  # The refusal names the push that holds the branch.
  holder=$(sed -n 1p "$LOCKS/feat.lock")
  assert_contains "$output" "pid $holder"
  : > "$T/release"
  wait "$BG_PID"; rc=$?; BG_PID=""
  [ "$rc" -eq 0 ] || { echo "first push failed ($rc):"; cat "$T/first.out"; false; }
  assert_no_locks
}

@test "pre-push lock: a push of a different branch is not blocked" {
  start_holding_push feat
  run push other
  [ "$status" -eq 0 ] || { echo "other branch blocked: $output"; false; }
  refute_contains "$output" 'PUSH REFUSED'
  # The first push's lock is untouched, and the second left none of its own.
  [ -f "$LOCKS/feat.lock" ]
  [ ! -e "$LOCKS/other.lock" ]
}

@test "pre-push lock: a refused multi-branch push releases the locks it already took" {
  start_holding_push feat
  run push aaa feat
  [ "$status" -ne 0 ] || { echo "push holding feat was allowed: $output"; false; }
  [ ! -e "$LOCKS/aaa.lock" ] || { echo "aaa lock leaked by a refused push"; false; }
  [ -f "$LOCKS/feat.lock" ]
}

@test "pre-push lock: a stale lock whose pid is gone is reclaimed" {
  mkdir -p "$LOCKS"
  sh -c 'exit 0' & dead=$!
  wait "$dead" || true
  kill -0 "$dead" 2>/dev/null && skip "pid $dead was reused immediately"
  printf '%s\nref=refs/heads/feat remote=origin started=then\n' "$dead" > "$LOCKS/feat.lock"
  run push feat
  [ "$status" -eq 0 ] || { echo "stale lock not reclaimed: $output"; false; }
  assert_contains "$output" 'reclaimed a stale lock'
  assert_no_locks
}

@test "pre-push lock: released after the suites pass" {
  run push feat
  [ "$status" -eq 0 ] || { echo "$output"; false; }
  assert_no_locks
}

@test "pre-push lock: released after a suite fails" {
  run push FAKE_BATS_RC=1 feat
  [ "$status" -eq 1 ] || { echo "expected a blocked push: $output"; false; }
  assert_contains "$output" 'PUSH BLOCKED'
  assert_no_locks
}

@test "pre-push lock: SKIP_TESTS still takes the lock and releases it" {
  run push SKIP_TESTS=1 feat
  [ "$status" -eq 0 ] || { echo "$output"; false; }
  assert_contains "$output" 'TESTS SKIPPED'
  assert_no_locks
  # And skipping the tests does not skip the lock: a skip push is refused while one runs.
  start_holding_push feat
  run push SKIP_TESTS=1 feat
  [ "$status" -ne 0 ] || { echo "SKIP_TESTS bypassed the lock: $output"; false; }
  assert_contains "$output" 'PUSH REFUSED'
}

@test "pre-push lock: released when the push is interrupted (TERM)" {
  start_holding_push feat
  hook_pid=$(sed -n 1p "$LOCKS/feat.lock")
  kill -TERM "$hook_pid"
  : > "$T/release"   # let the foreground suite end so the shell runs its trap
  wait "$BG_PID" || true; BG_PID=""
  assert_no_locks
}
