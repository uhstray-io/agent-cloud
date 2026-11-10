#!/usr/bin/env bats
# The pre-push hook runs the Python suite on the uv-locked environment when it can.
#
# On 2026-11-09 a push skipped pytest entirely: the hook used the workstation's python3,
# which had no `ansible`, and a missing module is fail-open. The hook now runs
# `uv sync --locked` before both suites and puts the checkout's .venv/bin FIRST on PATH, so
# `python3`, `pytest` and the `ansible-playbook` the playbook tests look up on PATH are the
# locked versions. It falls back (with a warning) when uv is absent, the sync fails, or the
# sync produced no .venv. On the locked environment a missing module is an undeclared
# import, so the push fails instead of skipping.
#
# These tests drive the real hook in a scratch repository with fake `uv`, `python3` and
# `bats` on PATH. Each fake records its own path and the first PATH entry it saw, and the
# fake uv records its arguments, so what ran is read from logs rather than from output.

load assert_helpers

setup() {
  # This file itself runs under the hook: clear git's exported repository variables, or
  # the scratch repo's git commands reach the real one (docs/MISTAKES.md 3.7).
  unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR GIT_OBJECT_DIRECTORY \
    GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_PREFIX GIT_NAMESPACE
  REPO_ROOT=$(git rev-parse --show-toplevel)
  HOOK="$REPO_ROOT/.githooks/pre-push"
  HOOK_SH=${HOOK_SH:-sh}
  T="$BATS_TEST_TMPDIR"
  REPO="$T/repo"
  LOG="$T/python.log"
  mkdir -p "$T/fakebin" "$T/nouv"
  git init -q "$REPO"
  mkdir -p "$REPO/platform/tests"
  : > "$REPO/pyproject.toml"
  : > "$REPO/uv.lock"

  # Fake bats: records the first PATH entry it ran under.
  cat > "$T/fakebin/bats" <<'SH'
#!/usr/bin/env sh
echo "${PATH%%:*}" >> "$FAKE_BATS_LOG"
exit 0
SH

  # Fake python: logs its own path, then answers collection with FAKE_COLLECT_RC and
  # FAKE_COLLECT_OUT; every other call (import xdist, the real run) succeeds.
  cat > "$T/fakepython" <<'SH'
#!/usr/bin/env sh
echo "$0 first-path=${PATH%%:*}" >> "$FAKE_PY_LOG"
case "$*" in
  *--collect-only*) printf '%s\n' "${FAKE_COLLECT_OUT:-}"; exit "${FAKE_COLLECT_RC:-0}" ;;
esac
exit 0
SH
  chmod +x "$T/fakepython"
  cp "$T/fakepython" "$T/fakebin/python3"
  cp "$T/fakepython" "$T/nouv/python3"
  printf '#!/usr/bin/env sh\nexit 0\n' > "$T/nouv/bats"

  # Fake uv: records its arguments, exits FAKE_UV_RC, and on success creates .venv/bin's
  # python and python3 as the real sync does, unless FAKE_UV_NOVENV=1 (a sync into
  # UV_PROJECT_ENVIRONMENT elsewhere succeeds and leaves no .venv).
  cat > "$T/fakebin/uv" <<SH
#!/usr/bin/env sh
echo "\$*" >> "\$FAKE_UV_LOG"
[ "\${FAKE_UV_RC:-0}" = 0 ] || exit "\$FAKE_UV_RC"
[ "\${FAKE_UV_NOVENV:-0}" = 1 ] && exit 0
mkdir -p .venv/bin && cp "$T/fakepython" .venv/bin/python && cp "$T/fakepython" .venv/bin/python3
SH
  chmod +x "$T/fakebin/bats" "$T/fakebin/uv" "$T/nouv/bats"
}

refline() { echo "refs/heads/$1 1111111111111111111111111111111111111111 refs/heads/$1 0000000000000000000000000000000000000000"; }

# push BIN_DIR [VAR=value ...] — run the hook with BIN_DIR first on PATH. The nouv
# directory is paired with /usr/bin:/bin only, so no real uv can be found.
push() {
  local bin=$1; shift
  local path="$bin:$PATH"
  [ "$bin" = "$T/nouv" ] && path="$bin:/usr/bin:/bin"
  refline feat | (cd "$REPO" && env FAKE_PY_LOG="$LOG" FAKE_BATS_LOG="$T/bats.log" \
    FAKE_UV_LOG="$T/uv.log" "$@" PATH="$path" \
    "$HOOK_SH" "$HOOK" origin https://example.invalid/repo.git)
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
  run push "$T/fakebin" FAKE_COLLECT_RC=2 "FAKE_COLLECT_OUT=E   ModuleNotFoundError: No module named 'nope'"
  [ "$status" -ne 0 ]
  assert_grep 'Python collection FAILED' <(printf '%s' "$output")
}

@test "pre-push uv: a missing module on the python3 fallback still skips" {
  run push "$T/nouv" FAKE_COLLECT_RC=2 "FAKE_COLLECT_OUT=E   ModuleNotFoundError: No module named 'nope'"
  [ "$status" -eq 0 ]
  assert_grep 'not collectable here' <(printf '%s' "$output")
}
