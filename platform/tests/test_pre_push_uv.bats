#!/usr/bin/env bats
# The pre-push hook: the Python suite on the uv-locked environment, the fast lane, the
# classification of a collection failure, and BATS running beside pytest.
#
# On 2026-11-09 a push skipped pytest entirely: the hook used the workstation's python3,
# which had no `ansible`, and a missing module is fail-open. The hook now runs
# `uv sync --locked` before both suites and puts the checkout's .venv/bin FIRST on PATH, so
# `python3`, `pytest` and the `ansible-playbook` the playbook tests look up on PATH are the
# locked versions. It falls back (with a warning) when uv is absent, the sync fails, or the
# sync produced no .venv. On the locked environment a missing module is an undeclared
# import, so the push fails instead of skipping.
#
# Plan 21, phase 4 folded the separate `--collect-only` pass into the one run, so a
# collection failure is read from that run's output: pytest's banner (`ERROR collecting
# <file>`) or a conftest that will not load. A failing test is never classified as a
# collection failure, whatever its output says.
#
# These tests drive the real hook in a scratch repository with fake `uv`, `python3`, `bats`
# and `parallel` on PATH. Each fake records its own path and the first PATH entry it saw,
# and the fake uv records its arguments, so what ran is read from logs rather than from
# output.

load assert_helpers

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  HOOK="$REPO_ROOT/.githooks/pre-push"
  HOOK_SH=${HOOK_SH:-sh}
  T="$BATS_TEST_TMPDIR"
  REPO="$T/repo"
  LOG="$T/python.log"
  mkdir -p "$T/fakebin" "$T/nouv" "$T/tmp"
  git init -q "$REPO"
  mkdir -p "$REPO/platform/tests"
  : > "$REPO/pyproject.toml"
  : > "$REPO/uv.lock"

  # Fake bats: records the first PATH entry it ran under and, when FAKE_BATS_ARGS names a
  # file, its arguments. FAKE_BATS_MARK is created on start; FAKE_BATS_PIDS records the
  # hook's pid and its own; FAKE_BATS_HOLD names a file that ends the bounded wait for the
  # suite to finish. It exits with FAKE_BATS_RC.
  cat > "$T/fakebin/bats" <<'SH'
#!/usr/bin/env sh
echo "${PATH%%:*}" >> "$FAKE_BATS_LOG"
[ -n "${FAKE_BATS_ARGS:-}" ] && echo "$*" > "$FAKE_BATS_ARGS"
[ -n "${FAKE_BATS_MARK:-}" ] && : > "$FAKE_BATS_MARK"
[ -n "${FAKE_BATS_PIDS:-}" ] && echo "$PPID $$" > "$FAKE_BATS_PIDS"
if [ -n "${FAKE_BATS_HOLD:-}" ]; then
  i=0
  while [ ! -e "$FAKE_BATS_HOLD" ] && [ "$i" -lt 300 ]; do sleep 0.1; i=$((i + 1)); done
fi
exit "${FAKE_BATS_RC:-0}"
SH

  # Fake python: logs its own path for every call. The one pytest run also logs the fast
  # lane's environment, copies the changed-path list it was given (the hook removes that
  # file), waits for FAKE_WAIT to appear (bounded; logs whether it did), then prints
  # FAKE_COLLECT_OUT and exits FAKE_COLLECT_RC. Every other call succeeds.
  cat > "$T/fakepython" <<'SH'
#!/usr/bin/env sh
echo "$0 first-path=${PATH%%:*}" >> "$FAKE_PY_LOG"
case "$*" in
  *"-m pytest"*)
    echo "pytest PYTEST_FAST=${PYTEST_FAST:-unset} PYTEST_FAST_CHANGED=${PYTEST_FAST_CHANGED:-unset}" >> "$FAKE_PY_LOG"
    if [ -n "${PYTEST_FAST_CHANGED:-}" ] && [ -n "${FAKE_FAST_COPY:-}" ]; then
      cp "$PYTEST_FAST_CHANGED" "$FAKE_FAST_COPY"
    fi
    if [ -n "${FAKE_WAIT:-}" ]; then
      i=0
      while [ ! -e "$FAKE_WAIT" ] && [ "$i" -lt 50 ]; do sleep 0.1; i=$((i + 1)); done
      [ -e "$FAKE_WAIT" ] && echo "bats-started-during-pytest" >> "$FAKE_PY_LOG"
    fi
    printf '%s\n' "${FAKE_COLLECT_OUT:-}"
    exit "${FAKE_COLLECT_RC:-0}" ;;
esac
exit 0
SH
  chmod +x "$T/fakepython"
  cp "$T/fakepython" "$T/fakebin/python3"
  cp "$T/fakepython" "$T/nouv/python3"

  # Fake uv: records its arguments, exits FAKE_UV_RC, and on success creates .venv/bin's
  # python and python3 as the real sync does, unless FAKE_UV_NOVENV=1 (a sync that reports
  # success but leaves no .venv/bin/python).
  cat > "$T/fakebin/uv" <<SH
#!/usr/bin/env sh
echo "\$*" >> "\$FAKE_UV_LOG"
[ "\${FAKE_UV_RC:-0}" = 0 ] || exit "\$FAKE_UV_RC"
[ "\${FAKE_UV_NOVENV:-0}" = 1 ] && exit 0
mkdir -p .venv/bin && cp "$T/fakepython" .venv/bin/python && cp "$T/fakepython" .venv/bin/python3
SH

  # Fake GNU parallel, on the locked PATH only. The hook asks for it by name, so every
  # case that is not about the fallback sees the same answer on every machine.
  printf '#!/usr/bin/env sh\nexit 0\n' > "$T/fakebin/parallel"
  cp "$T/fakebin/bats" "$T/nouv/bats"
  chmod +x "$T/fakebin/bats" "$T/fakebin/uv" "$T/fakebin/parallel" "$T/nouv/bats"
}

# hook_push SHA BIN_DIR [VAR=value ...] — run the hook with BIN_DIR first on PATH, pushing
# branch feat at SHA. The nouv directory is paired with /usr/bin:/bin only, so no real uv
# can be found on it. The hook's temporary files go to $T/tmp, so a test can see them.
hook_push() {
  local sha=$1 bin=$2
  shift 2
  local path="$bin:$PATH"
  [ "$bin" = "$T/nouv" ] && path="$bin:/usr/bin:/bin"
  printf 'refs/heads/feat %s refs/heads/feat 0000000000000000000000000000000000000000\n' "$sha" \
    | (cd "$REPO" && env FAKE_PY_LOG="$LOG" FAKE_BATS_LOG="$T/bats.log" \
      FAKE_UV_LOG="$T/uv.log" TMPDIR="$T/tmp" "$@" PATH="$path" \
      "$HOOK_SH" "$HOOK" origin https://example.invalid/repo.git)
}

# push BIN_DIR [VAR=value ...] — as hook_push, pushing a sha that does not exist.
push() {
  hook_push 1111111111111111111111111111111111111111 "$@"
}

# commit_file REPO PATH MESSAGE — one commit that touches PATH; prints the new commit's sha.
# Identity and signing are given on the command line, so no repository config is written.
commit_file() {
  mkdir -p "$1/$(dirname "$2")"
  printf '%s\n' "$3" >> "$1/$2"
  git -C "$1" add -- "$2"
  git -C "$1" -c user.name=test -c user.email=test@example.invalid -c commit.gpgsign=false \
    commit -q -m "$3"
  git -C "$1" rev-parse HEAD
}

# Pytest's output for the failures this hook must classify, in the shapes it prints.
# The serial shape interrupts the run (exit 2); the parallel shape (xdist) goes on (exit 1).

# A test module that does not import: serial shape.
out_missing_module() {
  printf '%s\n' \
    '_______________________ ERROR collecting test_mod.py ________________________' \
    "E   ModuleNotFoundError: No module named 'nope'" \
    '=========================== short test summary info ============================' \
    'ERROR test_mod.py' \
    '!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!' \
    '1 error in 0.03s'
}

# A test module with a syntax error: serial shape.
out_syntax_error() {
  printf '%s\n' \
    '_______________________ ERROR collecting test_mod.py ________________________' \
    'E   SyntaxError: invalid syntax' \
    '=========================== short test summary info ============================' \
    'ERROR test_mod.py' \
    '!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!' \
    '1 error in 0.05s'
}

# The same syntax error: parallel shape, the run goes on and names the file alone.
out_syntax_error_parallel() {
  printf '%s\n' \
    '_______________________ ERROR collecting test_mod.py ________________________' \
    'E   SyntaxError: invalid syntax' \
    '=========================== short test summary info ============================' \
    'ERROR test_mod.py - SyntaxError: invalid syntax' \
    '1 error in 0.70s'
}

# A module that does not import, in the parallel shape, next to a passing test.
out_missing_module_parallel() {
  printf '%s\n' \
    '_______________________ ERROR collecting test_mod.py ________________________' \
    "E   ModuleNotFoundError: No module named 'nope'" \
    '=========================== short test summary info ============================' \
    'ERROR test_mod.py - ImportError while importing test module' \
    '1 passed, 1 error in 0.66s'
}

# A conftest that does not load (exit 4, no banner, in either shape).
out_conftest_missing() {
  printf '%s\n' \
    "ImportError while loading conftest '/repo/conftest.py'." \
    'conftest.py:1: in <module>' \
    "E   ModuleNotFoundError: No module named 'nope'"
}

# A test that fails and whose own message says "No module named": no collection error.
out_failing_test() {
  printf '%s\n' \
    '_______________________ test_fail _______________________' \
    'E       AssertionError: No module named widgets' \
    'FAILED test_fail.py::test_fail - AssertionError: No module named widgets' \
    '1 failed in 0.01s'
}

# A module that does not import AND a test that fails: both must block on the fallback.
out_collection_and_failure() {
  printf '%s\n' \
    '_______________________ ERROR collecting test_mod.py ________________________' \
    "E   ModuleNotFoundError: No module named 'nope'" \
    '=========================== short test summary info ============================' \
    'FAILED test_fail.py::test_fail - AssertionError: No module named widgets' \
    'ERROR test_mod.py - ImportError while importing test module' \
    '1 failed, 1 error in 0.53s'
}

@test "pre-push uv: a successful locked sync puts .venv/bin first on PATH for both suites" {
  run push "$T/fakebin"
  [ "$status" -eq 0 ]
  assert_grep '^sync --locked' "$T/uv.log"
  assert_grep '\.venv/bin/python3 first-path=.*/\.venv/bin$' "$LOG"
  refute_grep 'fakebin/python3 ' "$LOG"
  assert_grep '/\.venv/bin$' "$T/bats.log"
  refute_grep 'WARNING' <(printf '%s' "$output")
}

@test "pre-push uv: a sync that leaves no .venv falls back with a warning, not a blocked push" {
  run push "$T/fakebin" FAKE_UV_NOVENV=1
  [ "$status" -eq 0 ]
  assert_grep 'fakebin/python3 ' "$LOG"
  refute_grep '\.venv/bin' "$LOG"
  assert_grep 'did not create .venv' <(printf '%s' "$output")
}

@test "pre-push uv: UV_PROJECT_ENVIRONMENT elsewhere ignores a stale .venv and falls back" {
  mkdir -p "$REPO/.venv/bin"
  cp "$T/fakepython" "$REPO/.venv/bin/python"
  cp "$T/fakepython" "$REPO/.venv/bin/python3"
  run push "$T/fakebin" UV_PROJECT_ENVIRONMENT="$T/elsewhere"
  [ "$status" -eq 0 ]
  assert_grep 'fakebin/python3 ' "$LOG"
  refute_grep '\.venv/bin' "$LOG"
  assert_grep 'UV_PROJECT_ENVIRONMENT points outside' <(printf '%s' "$output")
  # The hook must not sync at all here: a sync would land outside the checkout.
  [ ! -s "$T/uv.log" ] || { echo "uv was invoked: $(cat "$T/uv.log")"; false; }
}

@test "pre-push uv: a failed sync falls back to python3 with a warning" {
  run push "$T/fakebin" FAKE_UV_RC=1
  [ "$status" -eq 0 ]
  assert_grep 'fakebin/python3 ' "$LOG"
  refute_grep '\.venv/bin' "$LOG"
  assert_grep 'uv sync --locked. failed' <(printf '%s' "$output")
}

@test "pre-push uv: without uv the suite runs on python3 with a warning" {
  run push "$T/nouv"
  [ "$status" -eq 0 ]
  assert_grep 'nouv/python3 ' "$LOG"
  assert_grep 'uv not found' <(printf '%s' "$output")
}

@test "pre-push uv: a missing module on the locked environment fails the push" {
  run push "$T/fakebin" FAKE_COLLECT_RC=2 "FAKE_COLLECT_OUT=$(out_missing_module)"
  [ "$status" -ne 0 ]
  assert_grep 'Python collection FAILED' <(printf '%s' "$output")
}

@test "pre-push uv: a missing module on the python3 fallback still skips, and names the module" {
  run push "$T/nouv" FAKE_COLLECT_RC=2 "FAKE_COLLECT_OUT=$(out_missing_module)"
  [ "$status" -eq 0 ]
  assert_grep 'not collectable here' <(printf '%s' "$output")
  assert_contains "$output" "No module named 'nope'"
}

@test "pre-push uv: a missing module in the parallel shape still skips on the python3 fallback" {
  run push "$T/nouv" FAKE_COLLECT_RC=1 "FAKE_COLLECT_OUT=$(out_missing_module_parallel)"
  [ "$status" -eq 0 ]
  assert_contains "$output" 'not collectable here'
  refute_contains "$output" 'PUSH BLOCKED'
}

@test "pre-push uv: a collection failure that is not a missing module blocks, locked and fallback" {
  run push "$T/fakebin" FAKE_COLLECT_RC=2 "FAKE_COLLECT_OUT=$(out_syntax_error)"
  [ "$status" -ne 0 ] || { echo "locked: a syntax error did not block: $output"; false; }
  assert_contains "$output" 'Python collection FAILED'
  run push "$T/nouv" FAKE_COLLECT_RC=2 "FAKE_COLLECT_OUT=$(out_syntax_error)"
  [ "$status" -ne 0 ] || { echo "fallback: a syntax error did not block: $output"; false; }
  assert_contains "$output" 'Python collection FAILED'
  refute_contains "$output" 'not collectable here'
}

@test "pre-push uv: a syntax error in the parallel shape blocks, locked and fallback" {
  run push "$T/fakebin" FAKE_COLLECT_RC=1 "FAKE_COLLECT_OUT=$(out_syntax_error_parallel)"
  [ "$status" -ne 0 ] || { echo "locked: parallel-shape syntax error did not block: $output"; false; }
  assert_contains "$output" 'Python collection FAILED'
  run push "$T/nouv" FAKE_COLLECT_RC=1 "FAKE_COLLECT_OUT=$(out_syntax_error_parallel)"
  [ "$status" -ne 0 ] || { echo "fallback: parallel-shape syntax error did not block: $output"; false; }
  refute_contains "$output" 'not collectable here'
}

@test "pre-push uv: a failing test that prints No module named blocks on the python3 fallback" {
  run push "$T/nouv" FAKE_COLLECT_RC=1 "FAKE_COLLECT_OUT=$(out_failing_test)"
  [ "$status" -ne 0 ] || { echo "a failing test was skipped: $output"; false; }
  refute_contains "$output" 'not collectable here'
  assert_contains "$output" 'PUSH BLOCKED'
}

@test "pre-push uv: a missing module next to a failing test blocks on the python3 fallback" {
  run push "$T/nouv" FAKE_COLLECT_RC=1 "FAKE_COLLECT_OUT=$(out_collection_and_failure)"
  [ "$status" -ne 0 ] || { echo "a failing test was hidden by a collection skip: $output"; false; }
  refute_contains "$output" 'not collectable here'
  assert_contains "$output" 'PUSH BLOCKED'
}

@test "pre-push uv: a conftest that will not load skips on the fallback and blocks on the locked environment" {
  run push "$T/nouv" FAKE_COLLECT_RC=4 "FAKE_COLLECT_OUT=$(out_conftest_missing)"
  [ "$status" -eq 0 ] || { echo "fallback did not skip a conftest missing a module: $output"; false; }
  assert_contains "$output" 'not collectable here'
  assert_contains "$output" "No module named 'nope'"
  run push "$T/fakebin" FAKE_COLLECT_RC=4 "FAKE_COLLECT_OUT=$(out_conftest_missing)"
  [ "$status" -ne 0 ] || { echo "locked environment did not block a conftest missing a module: $output"; false; }
  assert_contains "$output" 'Python collection FAILED'
}

@test "pre-push fast lane: PYTEST_FAST and exactly the changed paths reach pytest" {
  base=$(commit_file "$REPO" platform/tests/existing_test.py base)
  git -C "$REPO" update-ref refs/remotes/origin/dev "$base"
  commit_file "$REPO" platform/tests/test_new.py new-test >/dev/null
  tip=$(commit_file "$REPO" platform/lib/helper.sh helper)
  run hook_push "$tip" "$T/fakebin" "FAKE_FAST_COPY=$T/fast-list.txt"
  [ "$status" -eq 0 ] || { echo "$output"; false; }
  assert_grep 'pytest PYTEST_FAST=1 PYTEST_FAST_CHANGED=' "$LOG"
  assert_contains "$output" 'FAST LANE'
  assert_contains "$output" 'CI runs the full suite'
  # The list is exactly the two paths the push changed: not the base's file, and no extra line.
  expected=$(printf '%s\n%s' platform/lib/helper.sh platform/tests/test_new.py)
  [ "$(cat "$T/fast-list.txt")" = "$expected" ] || { echo "changed list was: $(cat "$T/fast-list.txt")"; false; }
  [ "$(wc -l < "$T/fast-list.txt" | tr -d ' ')" = "2" ] || { echo "changed list has extra lines"; false; }
}

@test "pre-push fast lane: a push with no measurable base runs the full suite, and says why" {
  base=$(commit_file "$REPO" platform/tests/existing_test.py base)
  git -C "$REPO" update-ref refs/remotes/origin/dev "$base"
  run push "$T/fakebin"   # the pushed sha does not exist, so there is no merge base
  [ "$status" -eq 0 ] || { echo "$output"; false; }
  assert_grep 'pytest PYTEST_FAST=unset PYTEST_FAST_CHANGED=unset' "$LOG"
  assert_contains "$output" 'full Python suite, not the fast lane'
  assert_contains "$output" 'no merge base'
}

@test "pre-push uv: a failing BATS run blocks the push even when pytest passes" {
  run push "$T/fakebin" FAKE_BATS_RC=1
  [ "$status" -ne 0 ] || { echo "a failing BATS run did not block: $output"; false; }
  assert_contains "$output" 'PUSH BLOCKED'
  assert_grep 'pytest PYTEST_FAST' "$LOG"
  refute_contains "$output" 'Python suite FAILED'
}

@test "pre-push uv: BATS takes CI's flags when GNU parallel is installed" {
  run push "$T/fakebin" "FAKE_BATS_ARGS=$T/bats.args"
  [ "$status" -eq 0 ] || { echo "$output"; false; }
  [ "$(cat "$T/bats.args")" = '--jobs 4 --no-parallelize-within-files platform/tests/' ] \
    || { echo "bats args were: $(cat "$T/bats.args")"; false; }
}

@test "pre-push uv: without GNU parallel BATS runs serially, with a note" {
  # A system GNU parallel on the fallback PATH would be found; this case cannot run there.
  if [ -e /usr/bin/parallel ] || [ -e /bin/parallel ]; then
    skip "GNU parallel is installed system-wide"
  fi
  run push "$T/nouv" "FAKE_BATS_ARGS=$T/bats.args"
  [ "$status" -eq 0 ] || { echo "$output"; false; }
  [ "$(cat "$T/bats.args")" = 'platform/tests/' ] || { echo "bats args were: $(cat "$T/bats.args")"; false; }
  assert_contains "$output" 'GNU parallel is not installed'
}

@test "pre-push uv: BATS is already running while pytest runs" {
  run push "$T/fakebin" "FAKE_BATS_MARK=$T/bats.started" "FAKE_WAIT=$T/bats.started"
  [ "$status" -eq 0 ] || { echo "$output"; false; }
  assert_grep 'bats-started-during-pytest' "$LOG"
}

@test "pre-push uv: a finished run leaves no temporary file behind" {
  run push "$T/fakebin"
  [ "$status" -eq 0 ] || { echo "$output"; false; }
  [ -z "$(ls -A "$T/tmp")" ] || { echo "left behind: $(ls -A "$T/tmp")"; false; }
}

@test "pre-push uv: TERM stops the BATS job, removes the hook's temporary files and frees the lock" {
  # The fake BATS holds the suite open until released; the hook is then signalled.
  ( printf 'refs/heads/feat 1111111111111111111111111111111111111111 refs/heads/feat 0000000000000000000000000000000000000000\n' \
      | (cd "$REPO" && env FAKE_PY_LOG="$LOG" FAKE_BATS_LOG="$T/bats.log" FAKE_UV_LOG="$T/uv.log" \
        FAKE_BATS_PIDS="$T/bats.pids" FAKE_BATS_HOLD="$T/release" TMPDIR="$T/tmp" \
        PATH="$T/fakebin:$PATH" "$HOOK_SH" "$HOOK" origin https://example.invalid/repo.git) ) \
    > "$T/term.out" 2>&1 &
  local i=0
  while [ ! -s "$T/bats.pids" ] && [ "$i" -lt 100 ]; do sleep 0.1; i=$((i + 1)); done
  [ -s "$T/bats.pids" ] || { echo "BATS never started:"; cat "$T/term.out"; : > "$T/release"; false; }
  read -r hook_pid bats_pid < "$T/bats.pids"
  kill -TERM "$hook_pid" 2>/dev/null || { : > "$T/release"; echo "hook already gone"; false; }
  i=0
  while kill -0 "$hook_pid" 2>/dev/null && [ "$i" -lt 50 ]; do sleep 0.1; i=$((i + 1)); done
  i=0
  while kill -0 "$bats_pid" 2>/dev/null && [ "$i" -lt 50 ]; do sleep 0.1; i=$((i + 1)); done
  : > "$T/release"
  if kill -0 "$bats_pid" 2>/dev/null; then
    echo "the BATS job survived TERM to the hook (pid $bats_pid)"
    false
  fi
  [ -z "$(ls -A "$T/tmp")" ] || { echo "left behind: $(ls -A "$T/tmp")"; false; }
  [ -z "$(ls -A "$REPO/.git/pre-push-locks" 2>/dev/null)" ] || { echo "lock left behind"; false; }
}
