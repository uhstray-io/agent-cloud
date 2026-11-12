#!/usr/bin/env bats
# BATS jobs under GNU parallel get SIGALRM at its default (AC-743).
#
# GNU parallel starts every job with SIGALRM ignored, and ansible-core then refuses a task
# `timeout:`, so test_provision_vm.bats passed alone and failed under `bats --jobs`. The
# pre-push hook runs parallel's jobs through platform/tests/bin/parallel-job-shell
# (PARALLEL_SHELL), which restores the default. These check the wrapper itself, and that the
# hook hands it to bats and to nothing else.
#
# The hook runs in a scratch repository with no pyproject.toml, so the Python suite is skipped
# and each run is quick. A fake `bats` records its argv and the PARALLEL_SHELL it was given,
# each in its own file.

load assert_helpers

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  HOOK="$REPO_ROOT/.githooks/pre-push"
  JOB_SHELL="$REPO_ROOT/platform/tests/bin/parallel-job-shell"
  T="$BATS_TEST_TMPDIR"
  REPO="$T/repo"
  git init -q "$REPO"
  mkdir -p "$REPO/platform/tests/bin" "$T/fakebin"
  unset BATS_PARALLEL_BINARY_NAME PARALLEL_SHELL
  export FAKE_BATS_ARGS="$T/bats.args" FAKE_BATS_SHELL="$T/bats.shell"
  cat > "$T/fakebin/bats" <<'SH'
#!/usr/bin/env sh
echo "$*" > "$FAKE_BATS_ARGS"
echo "${PARALLEL_SHELL:-unset}" > "$FAKE_BATS_SHELL"
SH
  printf '#!/bin/sh\nexit 0\n' > "$T/fakebin/parallel"
  chmod +x "$T/fakebin/bats" "$T/fakebin/parallel"
}

# Run the hook in the scratch repository for one pushed branch, fakebin first on PATH.
push() {
  printf 'refs/heads/feat 1111111111111111111111111111111111111111 refs/heads/feat 0000000000000000000000000000000000000000\n' \
    | (cd "$REPO" && env PATH="$T/fakebin:$PATH" ${1+"$@"} sh "$HOOK" origin https://example.invalid/repo.git)
}

# The exit status of a job that exits 0 only when SIGALRM is at its default, started with
# SIGALRM ignored (as GNU parallel starts it) through the shell named in $1.
job_status_under() {
  perl -e '$SIG{ALRM} = "IGNORE"; exec @ARGV' "$1" -c \
    'python3 -c "import signal, sys; sys.exit(signal.getsignal(signal.SIGALRM) != signal.SIG_DFL)"'
}

@test "parallel-job-shell: a job started with SIGALRM ignored runs with it at default" {
  command -v perl >/dev/null || skip "perl not installed"
  # The control proves the setup really hands the job an ignored SIGALRM.
  run job_status_under /bin/sh
  [ "$status" -eq 1 ] || { echo "control job saw SIGALRM at default: setup is wrong"; false; }
  run job_status_under "$JOB_SHELL"
  [ "$status" -eq 0 ] || { echo "the wrapper left SIGALRM ignored"; false; }
}

@test "pre-push parallel: bats gets CI's flags and the job shell" {
  cp "$JOB_SHELL" "$REPO/platform/tests/bin/"
  run push
  [ "$status" -eq 0 ] || { echo "$output"; false; }
  [ "$(cat "$FAKE_BATS_ARGS")" = '--jobs 4 --no-parallelize-within-files platform/tests/' ]
  # The hook works from `git rev-parse --show-toplevel`, the resolved path.
  [ "$(cat "$FAKE_BATS_SHELL")" = "$(git -C "$REPO" rev-parse --show-toplevel)/platform/tests/bin/parallel-job-shell" ]
  refute_contains "$output" 'runs serially'
}

@test "pre-push parallel: a checkout without the job shell runs BATS serially, saying why" {
  run push
  [ "$status" -eq 0 ] || { echo "$output"; false; }
  [ "$(cat "$FAKE_BATS_ARGS")" = 'platform/tests/' ]
  [ "$(cat "$FAKE_BATS_SHELL")" = 'unset' ]
  assert_contains "$output" 'parallel-job-shell is missing from this checkout'
}

@test "pre-push parallel: the presence check reads the binary name bats uses (BATS_PARALLEL_BINARY_NAME)" {
  cp "$JOB_SHELL" "$REPO/platform/tests/bin/"
  run push BATS_PARALLEL_BINARY_NAME=no-such-parallel
  [ "$status" -eq 0 ] || { echo "$output"; false; }
  [ "$(cat "$FAKE_BATS_ARGS")" = 'platform/tests/' ]
  assert_contains "$output" 'GNU parallel is not installed'
}
