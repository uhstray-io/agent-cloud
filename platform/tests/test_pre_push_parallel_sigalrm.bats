#!/usr/bin/env bats
# The pre-push hook runs BATS through GNU parallel only when parallel starts its jobs with
# SIGALRM at the default disposition (AC-743; the probe in .githooks/pre-push).
#
# GNU parallel 20261022 ignores SIGALRM in every job it starts, and a job inherits that.
# ansible-core's task `timeout:` then fails with "An existing alarm handler was present", so
# test_provision_vm.bats passed alone and failed under the hook. A wrapper cannot repair
# that, because parallel sets the disposition itself, so the hook detects it instead.
#
# These drive the real hook in a scratch repository with no pyproject.toml, so the Python
# suite is skipped and each run is quick. A fake `bats` records the argv the hook chose.
# Fake `parallel` binaries answer --version, then run their job with SIGALRM ignored, at
# default, or fail without running it.

load assert_helpers

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  HOOK="$REPO_ROOT/.githooks/pre-push"
  HOOK_SH=${HOOK_SH:-sh}
  T="$BATS_TEST_TMPDIR"
  REPO="$T/repo"
  git init -q "$REPO"
  mkdir -p "$REPO/platform/tests" "$T/fakebin"
  # A developer's exported name would make every case probe the wrong binary.
  unset BATS_PARALLEL_BINARY_NAME
  export FAKE_BATS_ARGS="$T/bats.args"
  cat > "$T/fakebin/bats" <<'SH'
#!/usr/bin/env sh
echo "$*" > "$FAKE_BATS_ARGS"
exit 0
SH
  chmod +x "$T/fakebin/bats"
}

# fake_parallel NAME ignore|default|fail: a fake GNU parallel at fakebin/NAME. It answers
# --version like GNU parallel does. Otherwise it takes the first argument that is not an
# option as its job and runs it under sh, with SIGALRM ignored (ignore) or at default
# (default). fail exits 1 without running anything.
#
# default sets SIGALRM to DEFAULT explicitly. A test run under `bats --jobs` inherits SIGALRM
# ignored from GNU parallel 20261022, and a shell cannot reset a signal it inherited as
# ignored (POSIX), so passing the inherited state through would make this case fail there.
# perl can restore the default disposition, and the disposition survives its exec.
fake_parallel() {
  local f="$T/fakebin/$1"
  cat > "$f" <<'SH'
#!/usr/bin/env sh
case "$1" in --version) echo "GNU parallel 20261022 (fake)"; exit 0 ;; esac
SH
  case "$2" in
    fail)
      echo 'exit 1' >> "$f" ;;
    ignore)
      cat >> "$f" <<'SH'
for a; do case $a in -*) ;; *) job=$a; break ;; esac; done
trap '' ALRM
sh -c "$job"
SH
      ;;
    default)
      cat >> "$f" <<'SH'
for a; do case $a in -*) ;; *) job=$a; break ;; esac; done
exec perl -e '$SIG{ALRM} = "DEFAULT"; exec @ARGV' sh -c "$job"
SH
      ;;
  esac
  chmod +x "$f"
}

# push [VAR=value ...] — run the hook in the scratch repository with one pushed branch, with
# fakebin first on PATH and the given environment.
push() {
  printf 'refs/heads/feat 1111111111111111111111111111111111111111 refs/heads/feat 0000000000000000000000000000000000000000\n' \
    | (cd "$REPO" && env PATH="$T/fakebin:$PATH" ${1+"$@"} \
        "$HOOK_SH" "$HOOK" origin https://example.invalid/repo.git)
}

@test "pre-push parallel: a parallel that starts jobs with SIGALRM ignored makes BATS run serially" {
  fake_parallel parallel ignore
  run push
  [ "$status" -eq 0 ] || { echo "$output"; false; }
  [ "$(cat "$T/bats.args")" = 'platform/tests/' ] \
    || { echo "bats args were: $(cat "$T/bats.args")"; false; }
  assert_contains "$output" 'GNU parallel 20261022 (fake) starts its jobs with SIGALRM ignored'
  assert_contains "$output" 'runs serially'
}

@test "pre-push parallel: a parallel whose jobs keep SIGALRM at default gets CI's flags" {
  fake_parallel parallel default
  run push
  [ "$status" -eq 0 ] || { echo "$output"; false; }
  [ "$(cat "$T/bats.args")" = '--jobs 4 --no-parallelize-within-files platform/tests/' ] \
    || { echo "bats args were: $(cat "$T/bats.args")"; false; }
  refute_contains "$output" 'SIGALRM ignored'
}

@test "pre-push parallel: a probe that fails for any reason runs BATS serially" {
  fake_parallel parallel fail
  run push
  [ "$status" -eq 0 ] || { echo "$output"; false; }
  [ "$(cat "$T/bats.args")" = 'platform/tests/' ] \
    || { echo "bats args were: $(cat "$T/bats.args")"; false; }
  assert_contains "$output" 'runs serially'
}

@test "pre-push parallel: the probe reads the binary name bats uses (BATS_PARALLEL_BINARY_NAME)" {
  # `parallel` ignores SIGALRM, `renamed` does not. Only a probe that reads the name bats
  # would call gets the flags, so this fails if the hook probes `parallel` instead.
  fake_parallel parallel ignore
  fake_parallel renamed default
  run push BATS_PARALLEL_BINARY_NAME=renamed
  [ "$status" -eq 0 ] || { echo "$output"; false; }
  [ "$(cat "$T/bats.args")" = '--jobs 4 --no-parallelize-within-files platform/tests/' ] \
    || { echo "bats args were: $(cat "$T/bats.args")"; false; }
}
