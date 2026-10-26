#!/usr/bin/env bats
# Behavioural tests for ephemeral-supervisor.sh, the ephemeral lifecycle's per-job reset
# (openspec add-ephemeral-github-runners).
#
# The reset kills processes and deletes files BY OWNER, as root, on a live host. What must
# never happen is that it reaches a process or a file that is not the runner account's, or
# that a fault in how it builds its match widens what it removes. These tests drive its
# functions against fabricated /proc, /etc/subuid and /proc/sysvipc trees, with the current
# user standing in for the runner account. Nothing here runs as root, kills a real process
# or touches a path outside the test's own directory.
#
# Run: bats platform/tests/test_github_runner_ephemeral.bats

load assert_helpers

setup() {
  SUP="$BATS_TEST_DIRNAME/../services/github-runner/deployment/ephemeral-supervisor.sh"
  [ -f "$SUP" ]
  # Normalised: the supervisor refuses a path with `//`, which a TMPDIR ending in / makes.
  T=$(cd "$BATS_TEST_TMPDIR" && pwd -P)
  ME=$(id -un)
  MYUID=$(id -u)
  mkdir -p "$T/proc" "$T/sysvipc" "$T/tmp" "$T/run/$MYUID" "$T/state/gh/jit" "$T/homes" "$T/seeds"
  printf '%s:165536:65536\n' "$ME" >"$T/subuid"
}

# Runs one supervisor expression in a fresh bash with the test's paths. The script is
# sourced, so its functions are reachable and its command dispatch does not run.
sup() {
  env RUNNER_EPHEMERAL_ENV=/nonexistent \
    RUNNER_ACCOUNT="$ME" \
    RUNNER_HOME="${HOME_OVERRIDE:-$T/homes/runner}" \
    RUNNER_SEED="${SEED_OVERRIDE:-$T/seeds/runner}" \
    RUNNER_STATE_DIR="$T/state/gh" \
    RUNNER_PROC_ROOT="${PROC_OVERRIDE-$T/proc}" \
    RUNNER_SUBUID_FILE="$T/subuid" \
    RUNNER_SYSVIPC_ROOT="$T/sysvipc" \
    RUNNER_SWEEP_ROOTS="${SWEEP_OVERRIDE-$T/tmp}" \
    RUNNER_RUNTIME_ROOT="$T/run" \
    RUNNER_JIT_MAX_AGE_MINUTES=60 \
    bash -c ". '$SUP'; $1"
}

fake_proc() { # fake_proc <pid> <real> <effective> <saved> <fs>
  mkdir -p "$T/proc/$1"
  printf 'Name:\tx\nUid:\t%s\t%s\t%s\t%s\nGid:\t0\t0\t0\t0\n' "$2" "$3" "$4" "$5" >"$T/proc/$1/status"
}

@test "ephemeral: the script is executable bash, and shellcheck-clean where shellcheck exists" {
  [ -x "$SUP" ]
  head -1 "$SUP" | grep -q bash
  if command -v shellcheck >/dev/null; then shellcheck -S warning "$SUP"; fi
}

@test "ephemeral: processes are matched by any of the four uids, in the account's ids only" {
  fake_proc 101 "$MYUID" "$MYUID" "$MYUID" "$MYUID" # the account
  fake_proc 102 0 0 0 0                             # root
  fake_proc 103 0 165600 0 0                        # a container user (subordinate id)
  fake_proc 104 0 0 0 300000                        # outside every range
  fake_proc 105 0 0 165535 0                        # one below the range
  run sup 'check_config; account_pids | sort'
  [ "$status" -eq 0 ]
  [ "$output" = "$(printf '101\n103')" ]
}

@test "ephemeral: a subordinate range that reaches uid 0 is refused, not used" {
  printf '%s:0:65536\n' "$ME" >"$T/subuid"
  run sup 'check_config'
  [ "$status" -ne 0 ]
  assert_contains "$output" "starts at 0"
}

@test "ephemeral: paths that would widen a reset are refused before anything runs" {
  HOME_OVERRIDE=/opt run sup 'check_config'
  [ "$status" -ne 0 ]
  HOME_OVERRIDE="$T/homes/../homes/runner" run sup 'check_config'
  [ "$status" -ne 0 ]
  SEED_OVERRIDE="$T/homes/runner/seed" run sup 'check_config'
  [ "$status" -ne 0 ]
  SEED_OVERRIDE="$T/homes/runner" run sup 'check_config'
  [ "$status" -ne 0 ]
  run env RUNNER_EPHEMERAL_ENV=/nonexistent RUNNER_ACCOUNT=root RUNNER_HOME="$T/homes/runner" \
    RUNNER_SEED="$T/seeds/runner" RUNNER_STATE_DIR="$T/state/gh" bash -c ". '$SUP'; check_config"
  [ "$status" -ne 0 ]
  run sup 'check_config && echo ok'
  [ "$status" -eq 0 ]
  [ "$output" = ok ]
}

@test "ephemeral: the owner predicate is one bounded uid range per id range, never empty" {
  run sup 'check_config; owner_predicate | tr "\n" " "'
  [ "$status" -eq 0 ]
  [ "$output" = "( ( -uid +$((MYUID - 1)) -uid -$((MYUID + 1)) ) -o ( -uid +165535 -uid -231072 ) ) " ]
  run sup 'RANGES_LO=(); RANGES_HI=(); owner_predicate'
  [ "$status" -ne 0 ]
}

@test "ephemeral: System V IPC objects of the account are found by owner or creator" {
  cat >"$T/sysvipc/shm" <<EOF
       key      shmid perms                  size  cpid  lpid nattch   uid   gid  cuid  cgid      atime      dtime      ctime                   rss                  swap
         0         11   600                  4096   10    10      0  $MYUID   0     0     0          0          0          0                     0                     0
         0         12   600                  4096   10    10      0     0     0     0     0          0          0          0                     0                     0
         0         13   600                  4096   10    10      0     0     0 165540     0          0          0          0                     0                     0
EOF
  cat >"$T/sysvipc/msg" <<EOF
       key      msqid perms      cbytes       qnum lspid lrpid   uid   gid  cuid  cgid      stime      rtime      ctime
         0         21   600           0          0     0     0     0     0     0     0          0          0          0
EOF
  cat >"$T/sysvipc/sem" <<EOF
       key      semid perms      nsems   uid   gid  cuid  cgid      otime      ctime
         0         31   600          1 $MYUID     0 $MYUID     0          0          0
EOF
  run sup 'check_config; ipc_owned'
  [ "$status" -eq 0 ]
  [ "$output" = "$(printf 'shm 11\nshm 13\nsem 31')" ]
}

@test "ephemeral: the sweep removes the home and the account's files outside it" {
  mkdir -p "$T/homes/runner/_work/repo" "$T/tmp/leftover-dir/sub"
  : >"$T/homes/runner/_work/repo/marker"
  : >"$T/tmp/leftover-file"
  : >"$T/tmp/leftover-dir/sub/marker"
  run sup 'check_config; sweep_files; owned_paths | tr "\0" "\n"'
  [ "$status" -eq 0 ]
  [ -z "$output" ]
  [ ! -e "$T/homes/runner" ]
  [ ! -e "$T/tmp/leftover-file" ]
  [ ! -e "$T/tmp/leftover-dir" ]
  [ -d "$T/tmp" ]
}

@test "ephemeral: a path whose owner is not the account's is never removed, whatever find printed" {
  # The re-check is what makes a fault in the predicate fail safe. Simulated by reporting
  # one path as root's: it must survive the sweep.
  : >"$T/tmp/not-ours"
  : >"$T/tmp/ours"
  run sup "check_config; file_owner() { case \"\$1\" in *not-ours) echo 0 ;; *) echo $MYUID ;; esac; }; sweep_files"
  [ "$status" -eq 0 ]
  [ -e "$T/tmp/not-ours" ]
  [ ! -e "$T/tmp/ours" ]
}

@test "ephemeral: a file name with a newline is one path, not two" {
  # A job chooses its file names. Split on the newline, the sweep would re-check an
  # unrelated path and leave the real file, and the verification would then quarantine.
  : >"$T/tmp/evil
name"
  run sup 'check_config; owned_paths | tr "\0" "@"'
  [ "$status" -eq 0 ]
  [ "$output" = "$T/tmp/evil
name@" ]
  run sup 'check_config; sweep_files; owned_paths | tr "\0" "\n"'
  [ -z "$output" ]
}

# do_reset's three checks each fail closed. The stubs make the earlier steps no-ops, so only the
# check under test can end the reset: a lister that cannot run must not read as an empty listing
# and let the reset report a clean account.
reset_with() {
  sup "check_config; kill_account() { :; }; clear_keyrings() { :; }; sweep_ipc() { :; }; sweep_files() { :; }; $1; do_reset"
}

@test "ephemeral: a reset whose process listing fails is an error, not a clean account" {
  run reset_with 'account_pids() { return 1; }'
  [ "$status" -ne 0 ]
  assert_contains "$output" "reset: cannot list processes"
  refute_contains "$output" "remain"
}

@test "ephemeral: a reset whose IPC listing fails is an error, not a clean account" {
  prod_fixtures
  run reset_with 'ipc_owned() { return 1; }'
  [ "$status" -ne 0 ]
  assert_contains "$output" "reset: cannot list IPC objects"
  refute_contains "$output" "remain"
}

@test "ephemeral: a reset whose file listing fails is an error, not a clean account" {
  prod_fixtures
  run reset_with 'owned_paths() { return 1; }'
  [ "$status" -ne 0 ]
  assert_contains "$output" "reset: cannot list files"
  refute_contains "$output" "remain"
}

@test "ephemeral: a reset whose listings are all empty passes every check" {
  prod_fixtures
  run reset_with ':'
  [ "$status" -eq 0 ]
  refute_contains "$output" "cannot list"
  refute_contains "$output" "remain"
}

# The real listers, on fixtures that are complete and empty: /proc holds one process that is not
# the account's (init), and each SysV table is present with its header and no rows. Each test
# below breaks one real lister and runs do_reset as cmd_loop does, where a failed reset is a
# quarantine.
prod_fixtures() {
  fake_proc 1 0 0 0 0
  printf '%s\n' '       key      shmid perms size uid gid cuid cgid' >"$T/sysvipc/shm"
  printf '%s\n' '       key      msqid perms cbytes qnum lspid lrpid uid gid cuid cgid' >"$T/sysvipc/msg"
  printf '%s\n' '       key      semid perms nsems uid gid cuid cgid' >"$T/sysvipc/sem"
}

# Stubbed: the steps that act on the live host (kill, keyring clear, IPC removal). Real: the
# three checks and their listers, and the file sweep, which can only reach the test's directories.
reset_prod() {
  sup "check_config; kill_account() { :; }; clear_keyrings() { :; }; sweep_ipc() { :; }; $1; do_reset || echo QUARANTINED"
}

@test "ephemeral: a reset whose sweep roots cannot be listed quarantines, and leaves the files" {
  prod_fixtures
  : >"$T/tmp/leftover"
  SWEEP_OVERRIDE= run reset_prod 'findmnt() { return 1; }'
  assert_contains "$output" "reset: cannot list files"
  assert_contains "$output" QUARANTINED
  [ -e "$T/tmp/leftover" ]
}

@test "ephemeral: a findmnt that lists no sweepable root, and succeeds, quarantines the reset" {
  prod_fixtures
  : >"$T/tmp/leftover"
  SWEEP_OVERRIDE= run reset_prod "findmnt() { printf '%s %s\\n' /proc proc; }"
  assert_contains "$output" "reset: cannot list files"
  assert_contains "$output" QUARANTINED
  [ -e "$T/tmp/leftover" ]
}

@test "ephemeral: a reset whose process table is missing quarantines" {
  prod_fixtures
  PROC_OVERRIDE=/nonexistent run reset_prod ':'
  assert_contains "$output" "reset: cannot list processes"
  assert_contains "$output" QUARANTINED
}

@test "ephemeral: a reset whose SysV IPC table is unreadable quarantines" {
  prod_fixtures
  chmod 000 "$T/sysvipc/msg"
  run reset_prod ':'
  assert_contains "$output" "reset: cannot list IPC objects"
  assert_contains "$output" QUARANTINED
}

@test "ephemeral: a SysV table that fails to read is not masked by the table after it" {
  prod_fixtures
  run reset_prod 'awk() { case "$*" in *sysvipc/msg) return 2 ;; *) command awk "$@" ;; esac; }'
  assert_contains "$output" "reset: cannot list IPC objects"
  assert_contains "$output" QUARANTINED
}

@test "ephemeral: a reset over real empty listers and an empty sweep root passes" {
  prod_fixtures
  run reset_prod ':'
  [ "$status" -eq 0 ]
  refute_contains "$output" QUARANTINED
  refute_contains "$output" "cannot list"
}

# find wrappers for the walk. Each lists the real tree as find does, then fails with the stderr
# given. The wrapper is a function, so owned_paths' LC_ALL=C find call reaches it.
@test "ephemeral: a path that vanishes mid-walk does not quarantine the reset" {
  prod_fixtures
  : >"$T/tmp/leftover"
  run reset_prod 'find() { command find "$@" 2>/dev/null; echo "find: /x: No such file or directory" >&2; return 1; }'
  [ "$status" -eq 0 ]
  refute_contains "$output" QUARANTINED
  refute_contains "$output" "cannot list"
  [ ! -e "$T/tmp/leftover" ]
}

@test "ephemeral: a walk that is refused with Permission denied quarantines the reset" {
  prod_fixtures
  run reset_prod 'find() { command find "$@" 2>/dev/null; echo "find: /x: Permission denied" >&2; return 1; }'
  assert_contains "$output" "Permission denied"
  assert_contains "$output" "reset: cannot list files"
  assert_contains "$output" QUARANTINED
}

@test "ephemeral: a walk with a vanished path and a refused one quarantines the reset" {
  prod_fixtures
  run reset_prod 'find() { command find "$@" 2>/dev/null; printf "%s\n" "find: /x: No such file or directory" "find: /y: Permission denied" >&2; return 1; }'
  assert_contains "$output" "reset: cannot list files"
  assert_contains "$output" QUARANTINED
}

@test "ephemeral: a walk that fails without saying why quarantines the reset" {
  prod_fixtures
  run reset_prod 'find() { command find "$@" 2>/dev/null; return 1; }'
  assert_contains "$output" "without a message"
  assert_contains "$output" QUARANTINED
}

@test "ephemeral: the oldest stocked configuration is taken and an expired one is discarded" {
  printf 'OLD' >"$T/state/gh/jit/gh-runner-01-jit-a.jit"
  printf 'MID' >"$T/state/gh/jit/gh-runner-01-jit-b.jit"
  printf 'NEW' >"$T/state/gh/jit/gh-runner-01-jit-c.jit"
  touch -t 202001010000 "$T/state/gh/jit/gh-runner-01-jit-a.jit"
  touch -t "$(date -v-30M +%Y%m%d%H%M 2>/dev/null || date -d '-30 min' +%Y%m%d%H%M)" "$T/state/gh/jit/gh-runner-01-jit-b.jit"
  run sup 'check_config; take_jit 2>"'"$T"'/log"'
  [ "$status" -eq 0 ]
  # Only the path on stdout: the caller captures it.
  [ "$output" = "$T/state/gh/jit/gh-runner-01-jit-b.jit" ]
  assert_contains "$(cat "$T/log")" "discarded expired gh-runner-01-jit-a"
  [ ! -e "$T/state/gh/jit/gh-runner-01-jit-a.jit" ]
  run sup 'stock_count'
  [ "$output" = 2 ]
}

@test "ephemeral: a configuration reaches the runner through a 0600 file, never argv, and is used once" {
  printf ' Q09ORklH\n' >"$T/state/gh/jit/gh-runner-01-jit-x.jit"
  # install(1) with an owner needs root; the stand-in only creates the file.
  run sup "check_config
    install() { : >\"\${@: -1}\"; chmod 600 \"\${@: -1}\"; }
    as_user() { printf '%s\n' \"\$*\" >>'$T/as_user.log'; }
    start_runner '$T/state/gh/jit/gh-runner-01-jit-x.jit'"
  [ "$status" -eq 0 ]
  [ "$(cat "$T/run/$MYUID/actions-runner-jit.env")" = "ACTIONS_RUNNER_INPUT_JITCONFIG=Q09ORklH" ]
  [ ! -e "$T/state/gh/jit/gh-runner-01-jit-x.jit" ]
  [ "$(cat "$T/state/gh/current")" = gh-runner-01-jit-x ]
  [ "$(cat "$T/as_user.log")" = "systemctl --user start actions-runner.service" ]
  refute_grep -q Q09ORklH "$T/as_user.log"
}

@test "ephemeral: no code line puts the configuration on a command line" {
  grep -vE '^[[:space:]]*#' "$SUP" >"$BATS_TEST_TMPDIR/code.sh"
  refute_grep -qE -- '--jitconfig' "$BATS_TEST_TMPDIR/code.sh"
  refute_grep -qE 'setenv|set-environment' "$BATS_TEST_TMPDIR/code.sh"
  # The only writer of the variable is printf (a builtin) into the 0600 file.
  [ "$(grep -c 'ACTIONS_RUNNER_INPUT_JITCONFIG' "$BATS_TEST_TMPDIR/code.sh")" -eq 1 ]
  grep -q "printf 'ACTIONS_RUNNER_INPUT_JITCONFIG='" "$BATS_TEST_TMPDIR/code.sh"
}

@test "ephemeral: a job is detected only from a Worker log" {
  mkdir -p "$T/homes/runner/_diag"
  run sup 'job_ran'
  [ "$status" -ne 0 ]
  : >"$T/homes/runner/_diag/Runner_1.log"
  run sup 'job_ran'
  [ "$status" -ne 0 ]
  : >"$T/homes/runner/_diag/Worker_20261018-1.log"
  run sup 'job_ran'
  [ "$status" -eq 0 ]
}

@test "ephemeral: a quarantine is recorded and exits 3, which the unit does not restart" {
  run sup 'quarantine "reset did not prove a clean account"'
  [ "$status" -eq 3 ]
  assert_contains "$(cat "$T/state/gh/quarantine")" "reset did not prove a clean account"
  run sup 'cmd_status'
  assert_contains "$output" "quarantine=yes"
  run sup 'check_config; cmd_loop'
  [ "$status" -eq 3 ]
  grep -q '^RestartPreventExitStatus=3$' "$BATS_TEST_DIRNAME/../services/github-runner/deployment/templates/github-runner-ephemeral.service.j2"
}

@test "ephemeral: the seed never carries a registration, a workspace or an image store" {
  local x
  for x in .runner .credentials .credentials_rsaparams _work _diag .local/share/containers \
    .config/systemd/user/default.target.wants/actions-runner.service; do
    grep -qF "$x" <(sed -n '/^SEED_EXCLUDE=(/,/)$/p' "$SUP")
  done
  mkdir -p "$T/homes/runner"
  printf '#!/bin/sh\n' >"$T/homes/runner/run.sh"
  chmod +x "$T/homes/runner/run.sh"
  : >"$T/homes/runner/.runner"
  run sup 'cmd_snapshot'
  [ "$status" -ne 0 ]
  assert_contains "$output" "holds a runner registration"
  [ ! -e "$T/seeds/runner" ]
}

@test "ephemeral: each cycle seeds the home before it starts the user manager" {
  # The manager reads its units and their enablement links (podman.socket among them) when
  # it starts; started on an empty home it would miss them (CodeRabbit review of #716).
  local fn
  fn=$(sed -n '/^cmd_loop() {$/,/^}$/p' "$SUP")
  local r s m
  r=$(printf '%s\n' "$fn" | grep -n 'do_reset ||' | cut -d: -f1)
  s=$(printf '%s\n' "$fn" | grep -n 'seed_home ||' | cut -d: -f1)
  m=$(printf '%s\n' "$fn" | grep -n 'start_user_manager ||' | cut -d: -f1)
  [ -n "$r" ] && [ -n "$s" ] && [ -n "$m" ]
  [ "$r" -lt "$s" ]
  [ "$s" -lt "$m" ]
  # do_reset itself starts nothing.
  refute_grep -q 'start_user_manager' <(sed -n '/^do_reset() {$/,/^}$/p' "$SUP")
}
