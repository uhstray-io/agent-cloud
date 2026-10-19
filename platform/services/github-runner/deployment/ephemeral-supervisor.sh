#!/usr/bin/env bash
# ephemeral-supervisor.sh: one job per runner, with the runner account's processes and
# files destroyed between jobs. Root-owned system service; installed by
# deploy-github-runner.yml when the host declares github_runner_lifecycle: ephemeral.
# Design: plan/development/openspec/changes/add-ephemeral-github-runners/design.md.
#
# One cycle:
#   1. reset   kill every process of the runner account and of its subordinate uid range
#              (container processes run as those), wherever its cgroup; remove every file
#              those ids own on the local filesystems, the home included; remove their System
#              V IPC objects; clear the account's keyrings. Verify all of it, or quarantine.
#   2. seed    copy the root-owned seed (the home exactly as the deploy built it, without
#              any registration) into the empty home, as the account.
#   3. take    the oldest just-in-time runner configuration from the root-only stock. The
#              orchestrator mints the stock (stock-github-runner-jit.yml); this host never
#              holds the App key, as in the persistent design.
#   4. run     start the account's actions-runner user unit with that configuration. The
#              service gives a just-in-time runner exactly one job and then removes it, and
#              the unit does not restart, so the cycle ends when the job does.
#
# Why a reaper is safe here when the persistent design refused one ("one that cannot tell
# a leftover process from the runner's own is more dangerous than the gap"): reset runs only
# when no runner of this account should exist. Every process of the account is a leftover
# by definition, so there is nothing to tell apart.
#
# The JIT configuration never appears in argv (world-readable through /proc). It travels
# root file -> this script's stdin redirection -> a 0600 file in the account's runtime
# directory that the unit reads with EnvironmentFile= and removes in ExecStartPost=. The
# runner reads ACTIONS_RUNNER_INPUT_JITCONFIG, masks it as a secret and deletes the variable
# (actions/runner v2.336.0, Runner.Listener/CommandSettings.cs).
#
# Usage:
#   ephemeral-supervisor.sh loop       the service's main loop (never returns on success)
#   ephemeral-supervisor.sh reset      one reset; clears a quarantine when it is clean
#   ephemeral-supervisor.sh snapshot   copy the deployed home into the seed (deploy only)
#   ephemeral-supervisor.sh status     read-only: quarantine, stock and current runner
#
# Exit status 3 means quarantined: the unit declares RestartPreventExitStatus=3, so a host
# that cannot prove a clean reset stops taking jobs instead of retrying forever.

set -euo pipefail

CONF="${RUNNER_EPHEMERAL_ENV:-/etc/github-runner/ephemeral.env}"
if [ -r "$CONF" ]; then
  set -a
  # shellcheck source=/dev/null
  . "$CONF"
  set +a
fi

: "${RUNNER_ACCOUNT:?RUNNER_ACCOUNT must be set in $CONF}"
: "${RUNNER_HOME:?RUNNER_HOME must be set in $CONF}"
: "${RUNNER_SEED:?RUNNER_SEED must be set in $CONF}"
: "${RUNNER_STATE_DIR:?RUNNER_STATE_DIR must be set in $CONF}"
JIT_MAX_AGE_MINUTES="${RUNNER_JIT_MAX_AGE_MINUTES:-1200}"
MAX_START_FAILURES="${RUNNER_MAX_START_FAILURES:-3}"
STOCK_POLL_SECONDS="${RUNNER_STOCK_POLL_SECONDS:-15}"
# A runner that exits sooner than this without running a job counts as a failed start.
FAST_EXIT_SECONDS="${RUNNER_FAST_EXIT_SECONDS:-120}"

# Overridable only so the tests can hand in fabricated trees.
PROC_ROOT="${RUNNER_PROC_ROOT:-/proc}"
SUBUID_FILE="${RUNNER_SUBUID_FILE:-/etc/subuid}"
SYSVIPC_ROOT="${RUNNER_SYSVIPC_ROOT:-/proc/sysvipc}"
SWEEP_ROOTS="${RUNNER_SWEEP_ROOTS:-}"
RUNTIME_ROOT="${RUNNER_RUNTIME_ROOT:-/run/user}"

JIT_DIR="$RUNNER_STATE_DIR/jit"
QUARANTINE="$RUNNER_STATE_DIR/quarantine"
CURRENT="$RUNNER_STATE_DIR/current"
UNIT="actions-runner.service"
EXIT_QUARANTINED=3

# The seed keeps everything the deploy built except what is per job or per registration.
# Registration files must never be in the seed: a copied .credentials would hand every job
# the same runner identity, which is the persistent design's weakness this change removes.
SEED_EXCLUDE=(_work _diag .runner .credentials .credentials_rsaparams .local/share/containers
  .cache .config/systemd/user/default.target.wants/actions-runner.service)

# Diagnostics go to stderr (the journal either way), so a function whose stdout is captured,
# like take_jit, returns only its value.
log() { printf '[ephemeral] %s\n' "$*" >&2; }
die() {
  log "ERROR: $*"
  exit 1
}

# ── configuration sanity ──────────────────────────────────────────────────────
# reset deletes files by owner and removes the home, so every value it derives a path or
# an id from is checked before anything is deleted. A wrong value here must stop the
# script, never widen what it removes.
check_config() {
  local p
  for p in "$RUNNER_HOME" "$RUNNER_SEED" "$RUNNER_STATE_DIR"; do
    case "$p" in
      *//* | */./* | */../* | */. | */..) die "not a normalised path: $p" ;;
      /?*/?*) ;;
      *) die "must be an absolute path below a top-level directory: $p" ;;
    esac
  done
  [ "${RUNNER_HOME%/}" != "${RUNNER_SEED%/}" ] || die "RUNNER_HOME and RUNNER_SEED must differ"
  case "${RUNNER_SEED%/}/" in "${RUNNER_HOME%/}/"*) die "RUNNER_SEED must not be inside RUNNER_HOME" ;; esac
  [ "$RUNNER_ACCOUNT" != root ] || die "the runner account must not be root"
  UID_=$(id -u "$RUNNER_ACCOUNT") || die "no account $RUNNER_ACCOUNT"
  GROUP_=$(id -gn "$RUNNER_ACCOUNT")
  [ "$UID_" -gt 0 ] || die "the runner account must not have uid 0"
  load_ranges
}

# The ids that belong to the account: its uid, and each subordinate range /etc/subuid gives
# it (by name or by uid). Rootless podman runs a container's non-root users as these ids, so
# a process or file a job container left behind is owned by one of them, not by the uid.
RANGES_LO=()
RANGES_HI=()
load_ranges() {
  local lo hi
  RANGES_LO=("$UID_")
  RANGES_HI=("$UID_")
  while read -r lo hi; do
    [ -n "$lo" ] || continue
    # A range that reaches uid 0 would make root's files the account's. Refused.
    [ "$lo" -gt 0 ] || die "a subordinate range of $RUNNER_ACCOUNT starts at 0"
    RANGES_LO+=("$lo")
    RANGES_HI+=("$hi")
  done < <(awk -F: -v n="$RUNNER_ACCOUNT" -v u="$UID_" \
    '($1 == n || $1 == u) && $2 ~ /^[0-9]+$/ && $3 ~ /^[0-9]+$/ && $3 > 0 { print $2, $2 + $3 - 1 }' \
    "$SUBUID_FILE" 2>/dev/null || true)
}

owned_id() {
  local v="$1" i
  case "$v" in '' | *[!0-9]*) return 1 ;; esac
  for i in "${!RANGES_LO[@]}"; do
    if [ "$v" -ge "${RANGES_LO[$i]}" ] && [ "$v" -le "${RANGES_HI[$i]}" ]; then return 0; fi
  done
  return 1
}

# ── processes ─────────────────────────────────────────────────────────────────
# Every process whose real, effective, saved or filesystem uid belongs to the account. Read
# from /proc, not from a cgroup: a job's processes are in the runner unit's cgroup, in scopes
# rootless podman asks the user manager for, or anywhere else the account could start them.
account_pids() {
  local d tag r e s f
  for d in "$PROC_ROOT"/[0-9]*; do
    [ -r "$d/status" ] || continue
    read -r tag r e s f < <(grep '^Uid:' "$d/status" 2>/dev/null || true) || true
    [ "${tag:-}" = "Uid:" ] || continue
    if owned_id "$r" || owned_id "$e" || owned_id "$s" || owned_id "$f"; then
      echo "${d##*/}"
    fi
  done
}

as_user() {
  runuser -u "$RUNNER_ACCOUNT" -- env XDG_RUNTIME_DIR="$RUNTIME_ROOT/$UID_" "$@"
}

# The user manager first, so its units (the runner, podman's API service, any unit or timer a
# job created) stop through systemd and their cgroups are killed whole; then SIGKILL, from
# /proc, to whatever is left, until nothing is.
kill_account() {
  local n pids
  loginctl terminate-user "$RUNNER_ACCOUNT" >/dev/null 2>&1 || true
  systemctl stop "user@${UID_}.service" >/dev/null 2>&1 || true
  systemctl kill --signal=SIGKILL "user-${UID_}.slice" >/dev/null 2>&1 || true
  # Unmounts /run/user/<uid>, so the sweep below does not meet a busy mount point.
  systemctl stop "user-runtime-dir@${UID_}.service" >/dev/null 2>&1 || true
  for n in 1 2 3 4 5 6 7 8 9 10; do
    pids=$(account_pids | tr '\n' ' ')
    [ -n "${pids// /}" ] || return 0
    # shellcheck disable=SC2086
    kill -KILL $pids 2>/dev/null || true
    sleep "0.$n"
  done
  pids=$(account_pids | tr '\n' ' ')
  [ -n "${pids// /}" ] || return 0
  log "processes of $RUNNER_ACCOUNT survived SIGKILL: $pids"
  return 1
}

# ── System V IPC ──────────────────────────────────────────────────────────────
# A shared memory segment, message queue or semaphore set outlives the process that made it,
# so a job could leave data in one for the next job to read. Listed from /proc/sysvipc with
# the columns located by header name, and removed when its owner or creator is the account's.
ipc_owned() {
  local kind f
  for kind in shm msg sem; do
    f="$SYSVIPC_ROOT/$kind"
    [ -r "$f" ] || continue
    awk -v k="$kind" 'NR == 1 {
        for (i = 1; i <= NF; i++) {
          if (!idc && $i ~ /id$/ && $i != "uid" && $i != "cuid") idc = i
          if ($i == "uid") uc = i
          if ($i == "cuid") cc = i
        }
        next
      }
      idc && uc && cc { print k, $idc, $uc, $cc }' "$f" | while read -r k id u cu; do
      if owned_id "$u" || owned_id "$cu"; then echo "$k $id"; fi
    done
  done
}

sweep_ipc() {
  local k id flag
  while read -r k id; do
    case "$k" in shm) flag=-m ;; msg) flag=-q ;; sem) flag=-s ;; *) continue ;; esac
    ipcrm "$flag" "$id" 2>/dev/null || log "WARNING: could not remove $k $id"
  done < <(ipc_owned)
}

# ── keyrings ──────────────────────────────────────────────────────────────────
# The account's user, user-session and persistent keyrings are keyed by uid and can outlive
# its processes (the persistent one for days, keyrings(7)). Cleared from a process of the
# account itself, which is then killed with everything else.
clear_keyrings() {
  command -v keyctl >/dev/null 2>&1 || {
    log "keyctl is missing (package keyutils): the account's keyrings cannot be cleared"
    return 1
  }
  # shellcheck disable=SC2016
  as_user sh -c 'keyctl clear @u && keyctl clear @us \
    && { k=$(keyctl get_persistent @s 2>/dev/null) || exit 0; keyctl clear "$k"; }' >/dev/null 2>&1 || {
    log "clearing the keyrings of $RUNNER_ACCOUNT failed"
    return 1
  }
}

# ── files ─────────────────────────────────────────────────────────────────────
# Local filesystems a job can write to outside its home: /tmp, /var/tmp, /dev/shm, /run,
# /dev/mqueue and anything else mounted from a local disk or memory. Network and pseudo
# filesystems are not listed; the runner hosts mount none that a job can write.
sweep_roots() {
  if [ -n "$SWEEP_ROOTS" ]; then
    tr ':' '\n' <<<"$SWEEP_ROOTS"
    return 0
  fi
  findmnt -rn -o TARGET,FSTYPE \
    | awk '$2 ~ /^(ext2|ext3|ext4|xfs|btrfs|tmpfs|mqueue|vfat)$/ && $1 !~ /^\/(proc|sys)(\/|$)/ { print $1 }'
}

# find's predicate for "owned by one of the account's ids". Built only from the validated
# ranges; with no range it would match everything, so that is refused, not defaulted.
owner_predicate() {
  local i
  [ "${#RANGES_LO[@]}" -gt 0 ] || die "no owner ranges: refusing to build an unbounded predicate"
  printf '%s\n' '('
  for i in "${!RANGES_LO[@]}"; do
    [ "$i" -eq 0 ] || printf '%s\n' '-o'
    printf '%s\n' '(' -uid "+$((RANGES_LO[i] - 1))" -uid "-$((RANGES_HI[i] + 1))" ')'
  done
  printf '%s\n' ')'
}

# Paths owned by the account outside the home, NUL-terminated: a job chooses its file names,
# and a name with a newline in it must not split into two paths. A matched directory is pruned:
# it is removed whole, so listing its contents adds nothing. A sweep root itself, and any
# mount point (the account's /run/user/<uid> while it is still mounted), is never listed:
# each mounted filesystem is swept as its own root.
owned_paths() {
  local root p pred=()
  mapfile -t pred < <(owner_predicate)
  while read -r root; do
    [ -d "$root" ] || continue
    while IFS= read -r -d '' p; do
      if command -v mountpoint >/dev/null 2>&1 && mountpoint -q -- "$p"; then continue; fi
      printf '%s\0' "$p"
    done < <(find "$root" -xdev -mindepth 1 -path "$RUNNER_HOME" -prune -o "${pred[@]}" -print0 -prune 2>/dev/null || true)
  done < <(sweep_roots)
}

file_owner() { stat -c %u -- "$1" 2>/dev/null || stat -f %u -- "$1"; }

# Never crosses into another mounted filesystem where rm can promise that (GNU rm; the
# runner hosts). The tests also run on BSD rm, which has no such option.
rm_tree() {
  if rm --help 2>&1 | grep -q -- '--one-file-system'; then
    rm -rf --one-file-system -- "$1"
  else
    rm -rf -- "$1"
  fi
}

# Removes the home and every listed path, each re-checked for ownership first. The second
# check is what makes a fault in the predicate fail safe: nothing whose owner is not the
# account's is removed, whatever find printed.
sweep_files() {
  local p
  if [ -e "$RUNNER_HOME" ]; then
    rm_tree "$RUNNER_HOME" 2>/dev/null || log "WARNING: could not fully remove $RUNNER_HOME"
  fi
  while IFS= read -r -d '' p; do
    [ -n "$p" ] && [ "$p" != / ] || continue
    if owned_id "$(file_owner "$p")"; then
      rm_tree "$p" 2>/dev/null || log "WARNING: could not remove $p"
    fi
  done < <(owned_paths)
}

# ── reset ─────────────────────────────────────────────────────────────────────
do_reset() {
  local left
  kill_account || return 1
  sweep_ipc
  # The keyrings are cleared by a process of the account, which the second kill removes.
  clear_keyrings || return 1
  kill_account || return 1
  sweep_files

  left=$(account_pids | tr '\n' ' ')
  if [ -n "${left// /}" ]; then
    log "reset: processes remain: $left"
    return 1
  fi
  left=$(ipc_owned | tr '\n' ' ')
  if [ -n "${left// /}" ]; then
    log "reset: IPC objects remain: $left"
    return 1
  fi
  left=$(owned_paths | tr '\0' '\n' | head -n 20 | tr '\n' ' ')
  if [ -n "${left// /}" ]; then
    log "reset: files remain: $left"
    return 1
  fi

  install -d -o "$RUNNER_ACCOUNT" -g "$GROUP_" -m 0750 "$RUNNER_HOME"
  start_user_manager
}

# systemctl --user reaches the manager through the user bus, or its private socket.
user_bus_up() { [ -S "$RUNTIME_ROOT/$UID_/bus" ] || [ -S "$RUNTIME_ROOT/$UID_/systemd/private" ]; }

start_user_manager() {
  local n
  systemctl start "user@${UID_}.service"
  for n in $(seq 1 30); do
    user_bus_up && return 0
    sleep 1
  done
  log "the user manager of $RUNNER_ACCOUNT did not come up"
  return 1
}

seed_home() {
  [ -x "$RUNNER_SEED/run.sh" ] || {
    log "no runner in the seed $RUNNER_SEED: deploy the host before starting this service"
    return 1
  }
  runuser -u "$RUNNER_ACCOUNT" -- cp -R --preserve=mode,timestamps,links "$RUNNER_SEED/." "$RUNNER_HOME/"
  [ -x "$RUNNER_HOME/run.sh" ]
}

# ── stock ─────────────────────────────────────────────────────────────────────
# The oldest usable configuration in the stock, after discarding expired ones. GitHub removes
# an ephemeral runner that has not connected for a day, so a configuration older than the
# declared age is dropped here; the stock playbook deletes its runner record.
take_jit() {
  local f
  for f in "$JIT_DIR"/*.jit; do
    [ -f "$f" ] || continue
    if [ -n "$(find "$f" -mmin +"$JIT_MAX_AGE_MINUTES" 2>/dev/null)" ]; then
      rm -f "$f"
      log "discarded expired $(basename "$f" .jit)"
    fi
  done
  # shellcheck disable=SC2012
  ls -1tr "$JIT_DIR"/*.jit 2>/dev/null | head -n 1 || true
}

stock_count() {
  local f n=0
  for f in "$JIT_DIR"/*.jit; do [ -f "$f" ] && n=$((n + 1)); done
  echo "$n"
}

start_runner() {
  local cfg="$1" name envf
  name=$(basename "$cfg" .jit)
  envf="$RUNTIME_ROOT/$UID_/actions-runner-jit.env"
  install -o "$RUNNER_ACCOUNT" -g "$GROUP_" -m 0600 /dev/null "$envf"
  {
    printf 'ACTIONS_RUNNER_INPUT_JITCONFIG='
    tr -d '[:space:]' <"$cfg"
    printf '\n'
  } >"$envf"
  # Consumed before it is used: a configuration is good for one runner, and a crash between
  # here and the start must not hand it to a second one.
  rm -f "$cfg"
  printf '%s\n' "$name" >"$CURRENT"
  as_user systemctl --user start "$UNIT"
  log "started runner=$name"
}

runner_state() { as_user systemctl --user is-active "$UNIT" 2>/dev/null || true; }

wait_runner() {
  while :; do
    case "$(runner_state)" in
      active | activating | deactivating | reloading) sleep 5 ;;
      *) return 0 ;;
    esac
  done
}

# A Worker log is written only when the runner accepted a job.
job_ran() { compgen -G "$RUNNER_HOME/_diag/Worker_*.log" >/dev/null; }

quarantine() {
  mkdir -p "$RUNNER_STATE_DIR"
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >"$QUARANTINE"
  log "QUARANTINED: $*. No job is taken until a deploy proves a clean reset."
  exit "$EXIT_QUARANTINED"
}

# ── commands ──────────────────────────────────────────────────────────────────
cmd_reset() {
  check_config
  if do_reset; then
    rm -f "$QUARANTINE" "$CURRENT"
    echo "reset=clean"
  else
    echo "reset=failed"
    return 1
  fi
}

# Path, mode, owner and group of every entry under $1, relative and sorted.
seed_modes() {
  (cd "$1" && find . -printf '%p %m %U %G\n' 2>/dev/null | LC_ALL=C sort)
}

cmd_snapshot() {
  check_config
  local new="${RUNNER_SEED%/}.new" old="${RUNNER_SEED%/}.old" x
  [ -x "$RUNNER_HOME/run.sh" ] || die "$RUNNER_HOME has no runner install to snapshot"
  # A persistent registration in the home means the deploy did not reset it first.
  [ ! -e "$RUNNER_HOME/.runner" ] || die "$RUNNER_HOME holds a runner registration; refusing to copy it into the seed"
  rm -rf "$new" "$old"
  cp -a "$RUNNER_HOME" "$new"
  for x in "${SEED_EXCLUDE[@]}"; do rm -rf "${new:?}/$x"; done
  # The downloaded archives: the extracted runner is what a job needs.
  rm -f "$new"/actions-runner-linux-*.tar.gz "$new"/actions-runner-hooks-*.zip
  # Readable by the account's group, writable by nobody but root, so a job cannot change what
  # the next job starts from.
  chown -R "root:$GROUP_" "$new"
  chmod -R g+rX,g-w,o-rwx "$new"
  # Unchanged when content, links and modes match the current seed, so a deploy that
  # changed nothing reports nothing changed (the home itself is rebuilt on every deploy).
  if [ -d "$RUNNER_SEED" ] && diff -r --no-dereference "$new" "$RUNNER_SEED" >/dev/null 2>&1 \
    && [ "$(seed_modes "$new")" = "$(seed_modes "$RUNNER_SEED")" ]; then
    rm -rf "$new"
    echo "snapshot=unchanged"
    return 0
  fi
  if [ -e "$RUNNER_SEED" ]; then mv "$RUNNER_SEED" "$old"; fi
  mv "$new" "$RUNNER_SEED"
  rm -rf "$old"
  echo "snapshot=changed"
}

cmd_status() {
  if [ -e "$QUARANTINE" ]; then
    echo "quarantine=yes $(cat "$QUARANTINE")"
  else
    echo "quarantine=no"
  fi
  echo "stock=$(stock_count)"
  echo "current=$(cat "$CURRENT" 2>/dev/null || true)"
}

cmd_loop() {
  local cfg name started waited failures=0
  check_config
  if [ -e "$QUARANTINE" ]; then
    log "quarantined since $(cat "$QUARANTINE"); not starting"
    exit "$EXIT_QUARANTINED"
  fi
  mkdir -p "$JIT_DIR"
  chmod 0700 "$RUNNER_STATE_DIR" "$JIT_DIR"
  trap 'log "stopping"; exit 0' TERM INT

  # A restart of this service must not cancel the job a runner it started is running, so a
  # runner that is still up is waited for, not reset.
  if user_bus_up && [ "$(runner_state)" = active ]; then
    log "adopting the running runner $(cat "$CURRENT" 2>/dev/null || true)"
    wait_runner
  fi

  while :; do
    do_reset || quarantine "reset did not prove a clean account"
    seed_home || quarantine "the home could not be seeded"
    waited=0
    while :; do
      cfg=$(take_jit)
      [ -z "$cfg" ] || break
      if [ $((waited % 20)) -eq 0 ]; then log "stock empty: waiting for stock-github-runner-jit.yml"; fi
      waited=$((waited + 1))
      sleep "$STOCK_POLL_SECONDS"
    done
    name=$(basename "$cfg" .jit)
    started=$SECONDS
    start_runner "$cfg"
    wait_runner
    if job_ran; then
      failures=0
      log "cycle runner=$name job=yes seconds=$((SECONDS - started))"
    else
      log "cycle runner=$name job=no seconds=$((SECONDS - started))"
      if [ $((SECONDS - started)) -lt "$FAST_EXIT_SECONDS" ]; then failures=$((failures + 1)); fi
      if [ "$failures" -ge "$MAX_START_FAILURES" ]; then
        quarantine "$failures runners in a row exited without taking a job"
      fi
    fi
  done
}

# Sourced by the tests to reach the functions; run otherwise.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  case "${1:-}" in
    loop) cmd_loop ;;
    reset) cmd_reset ;;
    snapshot) cmd_snapshot ;;
    status) cmd_status ;;
    *) die "usage: $0 {loop|reset|snapshot|status}" ;;
  esac
fi
