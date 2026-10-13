#!/usr/bin/env bash
# deploy.sh — install, configure and run one self-hosted GitHub Actions runner.
#
# Lifecycle only. It installs the runner, registers it, and manages its service. It
# does NOT generate, fetch or store secrets: the registration token is minted by
# Ansible from the App credential in OpenBao and handed to this script on STDIN, the
# same division the platform's other services keep (AGENTS.md, Critical Deployment
# Rules 2 and 4).
#
# Why this is a host service and not a container, when the platform's norm is rootless
# podman: the runner's whole job is to CREATE containers for each job it accepts. Running
# it inside one buys nothing and costs the nested-container problem. Isolation is instead
# enforced per job (see the env template) and by the unprivileged account it runs as.
#
# Reads its configuration from an env file rendered by Ansible (config/runner.env).
# Everything it needs is declared there; nothing is discovered at run time.
#
# Usage:
#   printf '%s' "$REG_TOKEN" | ./deploy.sh configure
#   ./deploy.sh start | stop | status | remove   (remove reads a REMOVE token on stdin)
#   ./deploy.sh procs     (read-only: the runner's Listener and Worker processes)
#   ./deploy.sh quiesce   (drain, stop, and empty the unit's cgroup before a start)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
. "${SCRIPT_DIR}/../../../lib/common.sh"

ENV_FILE="${RUNNER_ENV_FILE:-${SCRIPT_DIR}/config/runner.env}"
[ -r "$ENV_FILE" ] || error "missing $ENV_FILE — Ansible renders it before this runs"
set -a
# shellcheck source=/dev/null
. "$ENV_FILE"
set +a

: "${RUNNER_HOME:?RUNNER_HOME must be set in runner.env}"
: "${RUNNER_VERSION:?RUNNER_VERSION must be set in runner.env}"
: "${RUNNER_NAME:?RUNNER_NAME must be set in runner.env}"
: "${RUNNER_LABELS:?RUNNER_LABELS must be set in runner.env}"
: "${RUNNER_GROUP:?RUNNER_GROUP must be set in runner.env}"
: "${RUNNER_ORG_URL:?RUNNER_ORG_URL must be set in runner.env}"
: "${HOOKS_VERSION:?HOOKS_VERSION must be set in runner.env}"

RUNNER_TGZ="actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz"
RUNNER_URL="https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/${RUNNER_TGZ}"
HOOKS_ZIP="actions-runner-hooks-docker-${HOOKS_VERSION}.zip"
HOOKS_URL="https://github.com/actions/runner-container-hooks/releases/download/v${HOOKS_VERSION}/${HOOKS_ZIP}"

# ── integrity ─────────────────────────────────────────────────────────────────
# The expected digest is not hardcoded here. It is published in the release metadata
# for the pinned version, so it is fetched for THAT version and compared — a constant
# copied into this file would silently go stale the first time the pin moves, and a
# stale-but-present digest reads as verified.
verify_digest() {
  local file="$1" expected="$2" actual
  [ -n "$expected" ] || error "no published digest for $(basename "$file") — refusing to run an unverified artefact"
  actual=$(sha256sum "$file" | cut -d' ' -f1)
  if [ "$actual" != "$expected" ]; then
    rm -f "$file"
    error "digest mismatch for $(basename "$file"): expected $expected, got $actual"
  fi
  info "digest verified: $(basename "$file")"
}

fetch() {
  local url="$1" dest="$2"
  [ -f "$dest" ] && { info "already present: $(basename "$dest")"; return 0; }
  info "downloading $(basename "$dest")"
  curl -fsSL --retry 3 --retry-delay 2 -o "$dest" "$url" \
    || error "download failed: $url"
}

cmd_install() {
  mkdir -p "$RUNNER_HOME" "${RUNNER_HOME}/_hooks"
  cd "$RUNNER_HOME"

  fetch "$RUNNER_URL" "${RUNNER_HOME}/${RUNNER_TGZ}"
  verify_digest "${RUNNER_HOME}/${RUNNER_TGZ}" "${RUNNER_SHA256:-}"
  # Extract only when the binary is absent, so a re-run is a no-op rather than
  # clobbering a configured runner's .runner / .credentials files.
  [ -x "${RUNNER_HOME}/run.sh" ] || tar xzf "${RUNNER_HOME}/${RUNNER_TGZ}" -C "$RUNNER_HOME"

  fetch "$HOOKS_URL" "${RUNNER_HOME}/${HOOKS_ZIP}"
  verify_digest "${RUNNER_HOME}/${HOOKS_ZIP}" "${HOOKS_SHA256:-}"
  [ -f "${RUNNER_HOME}/_hooks/index.js" ] || unzip -oq "${RUNNER_HOME}/${HOOKS_ZIP}" -d "${RUNNER_HOME}/_hooks"

  info "installed runner ${RUNNER_VERSION} + container hooks ${HOOKS_VERSION}"
}

cmd_configure() {
  cmd_install
  cd "$RUNNER_HOME"

  local token
  # Read from stdin, never a flag. argv is world-readable via /proc, so a registration
  # token passed as --token leaks to every local user for the life of the process.
  token=$(cat)
  [ -n "$token" ] || error "no registration token on stdin"

  if [ -f "${RUNNER_HOME}/.runner" ]; then
    info "already configured — leaving registration intact (re-run is a no-op)"
    return 0
  fi

  # The token goes in the ENVIRONMENT, not argv. The runner reads any
  # ACTIONS_RUNNER_INPUT_<ARG> variable, masks the ones it knows are secret, and then
  # deletes the variable from its own environment block
  # (Runner.Listener/CommandSettings.cs). argv, by contrast, is world-readable through
  # /proc for the life of the process, so `--token "$token"` would expose a live
  # registration credential to every local user. There is no --token-file option; this
  # env path is the argv-free route.
  #
  # --replace so a rebuilt host reclaims its own name instead of accumulating a second
  # offline entry. --disableupdate so the version stays the DECLARED one: a self-update
  # would silently undo the pin and make two hosts differ.
  ACTIONS_RUNNER_INPUT_TOKEN="$token" ./config.sh \
    --unattended \
    --url "$RUNNER_ORG_URL" \
    --name "$RUNNER_NAME" \
    --labels "$RUNNER_LABELS" \
    --runnergroup "$RUNNER_GROUP" \
    --work "_work" \
    --replace \
    --disableupdate \
    || error "runner configuration failed"

  info "registered $RUNNER_NAME (labels: $RUNNER_LABELS, group: $RUNNER_GROUP)"
}

cmd_remove() {
  cd "$RUNNER_HOME"
  [ -f "${RUNNER_HOME}/.runner" ] || { info "not configured — nothing to remove"; return 0; }
  local token
  token=$(cat)
  [ -n "$token" ] || error "no remove token on stdin"
  ACTIONS_RUNNER_INPUT_TOKEN="$token" ./config.sh remove || error "de-registration failed"
  info "de-registered $RUNNER_NAME (host left intact)"
}

cmd_status() {
  systemctl --user is-active "actions-runner.service" 2>/dev/null || true
  if [ -f "${RUNNER_HOME}/.runner" ]; then
    info "configured"
  else
    warn "NOT configured"
  fi
}

# ── the runner's processes ────────────────────────────────────────────────────
# The unit's main process is run.sh, not the runner: run.sh starts run-helper.sh, which
# starts bin/Runner.Listener, which holds the session and starts one Runner.Worker per
# job. Until 2026-10-12 every restart signalled run.sh alone and left the old Listener
# serving (docs/MISTAKES.md 10.27), so these commands look at the Listener and the Worker
# themselves, read from the kernel.
UNIT="actions-runner.service"
# Overridable only so the tests can hand in a fabricated tree.
PROC_ROOT="${RUNNER_PROC_ROOT:-/proc}"
CGROUP_ROOT="${RUNNER_CGROUP_ROOT:-/sys/fs/cgroup}"

count() { echo "$#"; }

# Every pid of this account whose command name is $1. "Runner.Listener" is 15
# characters, the kernel's limit for a command name, so it is never truncated.
account_pids() {
  local name="$1" uid d
  uid=$(id -u)
  for d in "$PROC_ROOT"/[0-9]*; do
    if [ "$(cat "$d/comm" 2>/dev/null)" = "$name" ] \
       && [ "$(awk '$1 == "Uid:" { print $2 }' "$d/status" 2>/dev/null)" = "$uid" ]; then
      echo "${d##*/}"
    fi
  done
}

# A file's modification time in epoch seconds (GNU stat, else BSD stat).
file_mtime() {
  stat -c %Y "$1" 2>/dev/null || stat -f %m "$1"
}

# Prints one line about process $2 in role $1 and adds it to the counts cmd_procs
# reports. A process that exited after it was listed is skipped.
describe_proc() {
  local role="$1" pid="$2" st ticks start cg nnp ns
  st=$(cat "$PROC_ROOT/$pid/stat" 2>/dev/null) || return 0
  # Field 22 of /proc/<pid>/stat is the start time in clock ticks after boot (proc(5)).
  # The fields after the command name start at field 3, so it is the 20th of them.
  st=${st##*) }
  # shellcheck disable=SC2086
  set -- $st
  ticks=${20}
  start=$((_btime + ticks / _hz))
  cg=$(sed -n 's/^0:://p' "$PROC_ROOT/$pid/cgroup" 2>/dev/null || true)
  nnp=$(awk '$1 == "NoNewPrivs:" { print $2 }' "$PROC_ROOT/$pid/status" 2>/dev/null || true)
  ns=private
  if [ "$(readlink "$PROC_ROOT/$pid/ns/user" 2>/dev/null)" = "$_host_ns" ]; then ns=host; fi
  echo "$role pid=$pid start=$start cgroup=$cg NoNewPrivs=${nnp:-unknown} userns=$ns"
  if [ "$nnp" != 0 ]; then _nnp=$((_nnp + 1)); fi
  if [ "$ns" != host ]; then _private=$((_private + 1)); fi
  case "$role" in
    listener)
      _listeners=$((_listeners + 1))
      if [ -z "$_unit_cg" ] || [ "$cg" != "$_unit_cg" ]; then _outside=$((_outside + 1)); fi
      # To the second: the deploy writes .env many seconds before it starts the runner.
      if [ "$start" -lt "$_env_mtime" ]; then _stale=$((_stale + 1)); fi
      ;;
    worker) _workers=$((_workers + 1)) ;;
  esac
}

# Read-only. Reports the unit's main process and every Listener and Worker of this
# account, then one verdict per line, which the deploy compares exactly:
#   listeners          Runner.Listener processes of this account (want 1)
#   outside_cgroup     of those, not in the unit's cgroup (want 0)
#   started_before_env of those, started before .env was last written (want 0)
#   no_new_privs       main process, Listeners and Workers with NoNewPrivs set (want 0)
#   private_userns     the same, in a user namespace other than the host's (want 0)
#   run_sh_traps       whether run.sh honours RUNNER_MANUALLY_TRAP_SIG (want yes)
cmd_procs() {
  local main pid
  _unit_cg=$(systemctl --user show --property=ControlGroup --value "$UNIT")
  main=$(systemctl --user show --property=MainPID --value "$UNIT")
  _env_mtime=$(file_mtime "${RUNNER_HOME}/.env")
  _btime=$(awk '$1 == "btime" { print $2 }' "$PROC_ROOT/stat")
  _hz=$(getconf CLK_TCK)
  _host_ns=$(readlink "$PROC_ROOT/self/ns/user")
  _listeners=0 _workers=0 _outside=0 _stale=0 _nnp=0 _private=0
  echo "unit_cgroup=$_unit_cg"
  echo "env_mtime=$_env_mtime"
  if [ "${main:-0}" -gt 0 ]; then describe_proc main "$main"; fi
  for pid in $(account_pids Runner.Listener); do describe_proc listener "$pid"; done
  for pid in $(account_pids Runner.Worker); do describe_proc worker "$pid"; done
  echo "listeners=$_listeners"
  echo "workers=$_workers"
  echo "outside_cgroup=$_outside"
  echo "started_before_env=$_stale"
  echo "no_new_privs=$_nnp"
  echo "private_userns=$_private"
  if grep -q 'RUNNER_MANUALLY_TRAP_SIG' "${RUNNER_HOME}/run.sh" 2>/dev/null; then
    echo "run_sh_traps=yes"
  else
    echo "run_sh_traps=no"
  fi
}

# The pids among $@ that are still a Runner.Listener.
live_listeners() {
  local p
  for p in "$@"; do
    if [ "$(cat "$PROC_ROOT/$p/comm" 2>/dev/null)" = Runner.Listener ]; then echo "$p"; fi
  done
}

# Brings the runner to a stop that leaves nothing behind, so the next start makes the
# only Listener. In order:
#   1. Wait up to RUNNER_DRAIN_SECONDS for no Worker to run, so a job in flight can
#      finish. Any stop cancels a running job (the Listener cancels it through the Worker
#      on shutdown, JobDispatcher.cs ShutdownAsync), so waiting is the only way to let
#      one finish. Bounded, because a busy runner may never be idle.
#   2. Stop the unit with the configuration systemd has loaded.
#   3. SIGINT every Listener still in the unit's cgroup: the ones a stop under
#      KillMode=process left behind. SIGINT is the Listener's own graceful stop (Ctrl-C,
#      Runner.cs CtrlCHandler): it cancels its job through the Worker, reports it, and
#      deletes its session. Wait up to RUNNER_STOP_GRACE_SECONDS for them to exit.
#   4. SIGKILL whatever is still in the cgroup, and require the cgroup to be empty.
cmd_quiesce() {
  local drain="${RUNNER_DRAIN_SECONDS:-600}" grace="${RUNNER_STOP_GRACE_SECONDS:-300}"
  local uid deadline rc=0 cg procs="" p listeners="" left
  uid=$(id -u)

  deadline=$((SECONDS + drain))
  while [ -n "$(account_pids Runner.Worker)" ] && [ "$SECONDS" -lt "$deadline" ]; do sleep 10; done
  if [ -n "$(account_pids Runner.Worker)" ]; then echo "drained=no"; else echo "drained=yes"; fi

  # rc 5: the unit is not loaded, which is a first deploy.
  systemctl --user stop "$UNIT" || rc=$?
  case "$rc" in
    0 | 5) ;;
    *) error "systemctl --user stop $UNIT failed (rc=$rc)" ;;
  esac

  cg=$(systemctl --user show --property=ControlGroup --value "$UNIT")
  if [ -n "$cg" ]; then
    procs="$CGROUP_ROOT$cg/cgroup.procs"
  else
    # An inactive unit may not report its cgroup, while leftover processes keep the
    # directory in place under the account's user manager.
    procs=$(find "$CGROUP_ROOT/user.slice/user-$uid.slice/user@$uid.service" \
      -path "*/$UNIT/cgroup.procs" -type f 2>/dev/null | head -n 1 || true)
  fi
  if [ -z "$procs" ] || [ ! -r "$procs" ]; then
    # No cgroup found: an empty, removed cgroup, or a layout the search above does not
    # know. Fall back to this account's Listeners from /proc, so a leftover Listener is
    # never skipped, and fail closed if one survives SIGINT and then SIGKILL.
    listeners=$(account_pids Runner.Listener | tr '\n' ' ')
    # shellcheck disable=SC2086
    echo "signalled=$(count $listeners)"
    left=""
    if [ -n "$listeners" ]; then
      # shellcheck disable=SC2086
      kill -INT $listeners 2>/dev/null || true
      deadline=$((SECONDS + grace))
      # shellcheck disable=SC2086
      while [ -n "$(live_listeners $listeners)" ] && [ "$SECONDS" -lt "$deadline" ]; do sleep 2; done
      # shellcheck disable=SC2086
      left=$(live_listeners $listeners | tr '\n' ' ')
    fi
    # shellcheck disable=SC2086
    echo "killed=$(count $left)"
    if [ -n "$left" ]; then
      # shellcheck disable=SC2086
      kill -KILL $left 2>/dev/null || true
      deadline=$((SECONDS + 30))
      # shellcheck disable=SC2086
      while [ -n "$(live_listeners $left)" ] && [ "$SECONDS" -lt "$deadline" ]; do sleep 1; done
      # shellcheck disable=SC2086
      left=$(live_listeners $left | tr '\n' ' ')
    fi
    # shellcheck disable=SC2086
    echo "remaining=$(count $left)"
    [ -z "$left" ] || error "Listeners still running, and no cgroup found for $UNIT: $left"
    return 0
  fi

  for p in $(cat "$procs" 2>/dev/null || true); do
    if [ "$(cat "$PROC_ROOT/$p/comm" 2>/dev/null)" = Runner.Listener ]; then listeners="$listeners $p"; fi
  done
  # shellcheck disable=SC2086
  echo "signalled=$(count $listeners)"
  if [ -n "$listeners" ]; then
    # shellcheck disable=SC2086
    kill -INT $listeners 2>/dev/null || true
    deadline=$((SECONDS + grace))
    # shellcheck disable=SC2086
    while [ -n "$(live_listeners $listeners)" ] && [ "$SECONDS" -lt "$deadline" ]; do sleep 2; done
  fi

  # A Listener's run-helper.sh exits once the Listener has; give the scripts a moment.
  deadline=$((SECONDS + 10))
  while [ -n "$(cat "$procs" 2>/dev/null || true)" ] && [ "$SECONDS" -lt "$deadline" ]; do sleep 1; done
  left=$(cat "$procs" 2>/dev/null || true)
  # shellcheck disable=SC2086
  echo "killed=$(count $left)"
  if [ -n "$left" ]; then
    # shellcheck disable=SC2086
    kill -KILL $left 2>/dev/null || true
    deadline=$((SECONDS + 30))
    while [ -n "$(cat "$procs" 2>/dev/null || true)" ] && [ "$SECONDS" -lt "$deadline" ]; do sleep 1; done
  fi
  left=$(cat "$procs" 2>/dev/null || true)
  # shellcheck disable=SC2086
  echo "remaining=$(count $left)"
  [ -z "$left" ] || error "still in the cgroup of $UNIT after SIGKILL: $left"
}

case "${1:-}" in
  install)   cmd_install ;;
  configure) cmd_configure ;;
  remove)    cmd_remove ;;
  start)     systemctl --user enable --now actions-runner.service; info "started" ;;
  stop)      systemctl --user stop actions-runner.service; info "stopped" ;;
  status)    cmd_status ;;
  procs)     cmd_procs ;;
  quiesce)   cmd_quiesce ;;
  *)         error "usage: $0 {install|configure|remove|start|stop|status|procs|quiesce}" ;;
esac
