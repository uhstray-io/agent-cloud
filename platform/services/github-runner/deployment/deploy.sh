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
#   ./deploy.sh install                          (install or upgrade to the declared versions)
#   ./deploy.sh start | stop | status | remove   (remove reads a REMOVE token on stdin)
#   ./deploy.sh procs     (read-only: the runner's Listener and Worker processes)
#   ./deploy.sh quiesce   (drain, stop, and empty the unit's cgroup before a start)
#
# Upgrades: raising RUNNER_VERSION or HOOKS_VERSION and re-running converges the host on
# the new version. Lowering one is refused unless RUNNER_ALLOW_DOWNGRADE=1.

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

# ── installed versions ──────────────────────────────────────────────────────
# What is installed is read from the install itself, never from a note of what was last
# deployed: a note records an intent, the binary reports what runs (docs/MISTAKES.md
# 1.22). An earlier cmd_install extracted only when run.sh was absent and then logged
# "installed runner <declared version>" regardless, so a bumped pin changed nothing on
# an existing host while the log said it had.
VERSION_RE='^[0-9]+\.[0-9]+\.[0-9]+$'

# Prints the runner version installed in $1 (default RUNNER_HOME), or nothing when no
# runner is installed there. A binary that exists but reports no version is a hard
# error: guessing would turn a damaged install into a silent no-op or a silent swap.
installed_runner_version() {
  local dir="${1:-$RUNNER_HOME}" v
  [ -e "${dir}/bin/Runner.Listener" ] || return 0
  v=$("${dir}/bin/Runner.Listener" --version 2>/dev/null | tr -d '[:space:]') || true
  [[ "$v" =~ $VERSION_RE ]] \
    || error "${dir}/bin/Runner.Listener reports no usable version ('${v}'); refusing to guess what is installed"
  printf '%s' "$v"
}

# The hooks archive holds a single index.js and carries no version a script can read,
# so the version is recorded in a marker written in the same step that places the
# files it describes. No marker means "unknown", which is installed over: the hooks are
# stateless, so a reinstall cannot lose anything.
HOOKS_MARKER=".hooks-version"
installed_hooks_version() {
  local m="${RUNNER_HOME}/_hooks/${HOOKS_MARKER}"
  [ -r "$m" ] && tr -d '[:space:]' < "$m"
  return 0
}

# True when version $1 sorts strictly before version $2.
version_lt() {
  [ "$1" != "$2" ] && [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -n1)" = "$1" ]
}

# A downgrade is refused unless RUNNER_ALLOW_DOWNGRADE=1. A declared version below the
# installed one is far more often a stale declaration (an old branch, a reverted
# inventory) than an intent, and acting on it silently would move a host backwards,
# possibly behind the window in which GitHub still queues jobs to a runner that does
# not self-update. A broken release still needs a way back, so the escape is explicit
# and logged rather than absent.
check_downgrade() {
  local what="$1" have="$2" want="$3"
  version_lt "$want" "$have" || return 0
  if [ "${RUNNER_ALLOW_DOWNGRADE:-0}" = "1" ]; then
    warn "downgrading ${what} ${have} -> ${want} (RUNNER_ALLOW_DOWNGRADE=1)"
    return 0
  fi
  error "refusing to downgrade ${what} from ${have} to ${want}; declare ${have} or newer, or re-run with github_runner_allow_downgrade=true if the rollback is intended"
}

# ── service control ───────────────────────────────────────────────────────────
# systemctl --user needs XDG_RUNTIME_DIR, which sudo does not set. The playbook passes
# it; the fallback derives it from the account this runs as, never a hardcoded uid.
user_systemctl() {
  XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}" systemctl --user "$@"
}

RUNNER_STOP_WAIT="${RUNNER_STOP_WAIT:-60}"

# PIDs of this account's Runner.Listener / Runner.Worker started from RUNNER_HOME.
runner_pids() {
  local home_re
  # shellcheck disable=SC2016 # the sed expression is literal on purpose
  home_re=$(printf '%s' "$RUNNER_HOME" | sed 's/[.[\*^$()+?{|]/\\&/g')
  pgrep -u "$(id -u)" -f "^${home_re}/bin/Runner\.(Listener|Worker)( |\$)" || true
}

# Stop the service AND wait for the runner's own processes to go. The unit uses
# KillMode=process, so stopping it signals run.sh alone and can leave Runner.Listener
# (and a Runner.Worker with a job) alive. Those are asked to shut down with SIGINT, the
# runner's graceful-stop signal; an in-flight job is cancelled, which is why an upgrade
# is best run while the runner is idle. If they outlive RUNNER_STOP_WAIT the swap is
# refused: starting a second listener beside a live one fights over one registration.
stop_runner() {
  local pids waited=0
  user_systemctl stop actions-runner.service || error "could not stop actions-runner.service"
  pids=$(runner_pids)
  # shellcheck disable=SC2086 # one pid per word is intended
  [ -z "$pids" ] || kill -INT $pids 2>/dev/null || true
  while [ -n "$(runner_pids)" ]; do
    [ "$waited" -lt "$RUNNER_STOP_WAIT" ] \
      || error "runner processes still alive ${RUNNER_STOP_WAIT}s after the service stopped; refusing to swap binaries under them (the old install is intact; stop them, then re-run)"
    sleep 1
    waited=$((waited + 1))
  done
  info "runner stopped"
}

# ── install / upgrade ─────────────────────────────────────────────────────────
# Names in RUNNER_HOME that the tarball must never replace: the registration written by
# config.sh (.runner, .credentials, .credentials_rsaparams, .path), the job environment
# Ansible renders (.env), and everything else this deploy or the runner owns there. The
# 2.336.0 and 2.338.0 tarballs ship only bin/, externals/, config.sh, env.sh, run.sh,
# safe_sleep.sh and two run-helper templates, so none of these is touched; the guard
# below makes that a checked property of each new release rather than an assumption.
PRESERVED=(.runner .credentials .credentials_rsaparams .env .path
           config _hooks _work _diag job-cleanup.sh .config)

# Restore every entry moved aside into $1, after a failed swap.
restore_previous() {
  local prev="$1" entry name
  while IFS= read -r -d '' entry; do
    name=$(basename "$entry")
    rm -rf "${RUNNER_HOME:?}/${name}"
    mv "$entry" "${RUNNER_HOME}/${name}"
  done < <(find "$prev" -mindepth 1 -maxdepth 1 -print0)
}

# Replace exactly the entries the staged release ships, moving the old ones aside
# first. Moving rather than extracting over the top mirrors the runner's own updater
# (bin/update.sh.template renames bin and externals before placing the new ones): an
# overlay would leave the previous release's files mixed into the new bin/, and a
# rename is safe for a process still holding the old files open.
swap_in_release() {
  local stage="$1" prev="$2" entry name
  rm -rf "$prev"
  mkdir -p "$prev"
  while IFS= read -r -d '' entry; do
    name=$(basename "$entry")
    if [ -e "${RUNNER_HOME}/${name}" ] || [ -L "${RUNNER_HOME}/${name}" ]; then
      mv "${RUNNER_HOME}/${name}" "${prev}/${name}" \
        || { restore_previous "$prev"; error "could not move ${name} aside; previous install restored"; }
    fi
    mv "$entry" "${RUNNER_HOME}/${name}" \
      || { restore_previous "$prev"; error "could not place ${name}; previous install restored"; }
  done < <(find "$stage" -mindepth 1 -maxdepth 1 -print0)
}

# Bring the runner in RUNNER_HOME to RUNNER_VERSION: no-op, upgrade, or refusal.
install_runner() {
  local have stage="${RUNNER_HOME}/_install.${RUNNER_VERSION}" prev="${RUNNER_HOME}/_previous" f staged now
  have=$(installed_runner_version)
  if [ "$have" = "$RUNNER_VERSION" ]; then
    info "runner ${have} already installed (declared ${RUNNER_VERSION}); nothing to change"
    return 0
  fi
  [ -z "$have" ] || check_downgrade runner "$have" "$RUNNER_VERSION"

  # Fetched and verified BEFORE the service is touched: a bad download leaves the
  # running runner exactly as it was.
  fetch "$RUNNER_URL" "${RUNNER_HOME}/${RUNNER_TGZ}"
  verify_digest "${RUNNER_HOME}/${RUNNER_TGZ}" "${RUNNER_SHA256:-}"

  rm -rf "$stage"
  mkdir -p "$stage"
  tar xzf "${RUNNER_HOME}/${RUNNER_TGZ}" -C "$stage" || { rm -rf "$stage"; error "could not extract ${RUNNER_TGZ}"; }
  for f in "${PRESERVED[@]}"; do
    [ ! -e "${stage}/${f}" ] \
      || { rm -rf "$stage"; error "${RUNNER_TGZ} ships ${f}, which this host owns; refusing to replace it"; }
  done
  staged=$(installed_runner_version "$stage") || staged=""
  [ "$staged" = "$RUNNER_VERSION" ] \
    || { rm -rf "$stage"; error "${RUNNER_TGZ} contains runner '${staged}', not the declared ${RUNNER_VERSION}; refusing to install it"; }

  # Only an existing install is serving; a fresh host has nothing to stop, and is not
  # registered yet, so the playbook starts it after configure.
  if [ -n "$have" ]; then
    stop_runner
    # From here every failure path restores the previous files and exits through
    # error(); this brings the service back on whatever install is then in place, so
    # a refused upgrade does not also leave the host serving nothing.
    if [ -f "${RUNNER_HOME}/.runner" ]; then
      trap 'user_systemctl start actions-runner.service >/dev/null 2>&1 || true' EXIT
    fi
  fi
  swap_in_release "$stage" "$prev"
  rm -rf "$stage"

  now=$(installed_runner_version) || now=""
  if [ "$now" != "$RUNNER_VERSION" ]; then
    restore_previous "$prev"
    error "after the swap the runner reports '${now}', not ${RUNNER_VERSION}; previous install restored"
  fi
  rm -rf "$prev"
  # The previous release's tarball is dead weight on a small root disk.
  find "$RUNNER_HOME" -maxdepth 1 -name 'actions-runner-linux-x64-*.tar.gz' ! -name "$RUNNER_TGZ" -delete

  if [ -n "$have" ]; then
    if [ -f "${RUNNER_HOME}/.runner" ]; then
      trap - EXIT
      user_systemctl start actions-runner.service || error "upgraded to ${now} but the service did not start"
    fi
    info "upgraded runner ${have} -> ${now}"
  else
    info "installed runner ${now}"
  fi
}

# Bring the container hooks in RUNNER_HOME/_hooks to HOOKS_VERSION, by the same rules.
install_hooks() {
  local have new="${RUNNER_HOME}/_hooks.new" old="${RUNNER_HOME}/_hooks.old"
  have=$(installed_hooks_version)
  if [ "$have" = "$HOOKS_VERSION" ] && [ -f "${RUNNER_HOME}/_hooks/index.js" ]; then
    info "container hooks ${have} already installed (declared ${HOOKS_VERSION}); nothing to change"
    return 0
  fi
  [ -z "$have" ] || check_downgrade "container hooks" "$have" "$HOOKS_VERSION"

  fetch "$HOOKS_URL" "${RUNNER_HOME}/${HOOKS_ZIP}"
  verify_digest "${RUNNER_HOME}/${HOOKS_ZIP}" "${HOOKS_SHA256:-}"

  rm -rf "$new" "$old"
  mkdir -p "$new"
  unzip -oq "${RUNNER_HOME}/${HOOKS_ZIP}" -d "$new" || { rm -rf "$new"; error "could not extract ${HOOKS_ZIP}"; }
  [ -f "${new}/index.js" ] || { rm -rf "$new"; error "${HOOKS_ZIP} has no index.js; refusing to install it"; }
  printf '%s\n' "$HOOKS_VERSION" > "${new}/${HOOKS_MARKER}"
  # Two renames, no overlay: a job starting its hook mid-swap sees the old directory or
  # the new one, never a half-written file. No service stop is needed; the runner
  # starts the hook afresh for each job.
  [ ! -e "${RUNNER_HOME}/_hooks" ] || mv "${RUNNER_HOME}/_hooks" "$old"
  mv "$new" "${RUNNER_HOME}/_hooks" || { [ ! -e "$old" ] || mv "$old" "${RUNNER_HOME}/_hooks"; error "could not place the container hooks"; }
  rm -rf "$old"
  find "$RUNNER_HOME" -maxdepth 1 -name 'actions-runner-hooks-docker-*.zip' ! -name "$HOOKS_ZIP" -delete

  if [ -n "$have" ]; then
    info "upgraded container hooks ${have} -> $(installed_hooks_version)"
  else
    info "installed container hooks $(installed_hooks_version)"
  fi
}

# Converges the install on the declared versions. Same version is a no-op; a different
# one is fetched, verified, and swapped in; a lower one is refused (check_downgrade).
cmd_install() {
  [[ "$RUNNER_VERSION" =~ $VERSION_RE ]] || error "RUNNER_VERSION '${RUNNER_VERSION}' is not MAJOR.MINOR.PATCH"
  [[ "$HOOKS_VERSION" =~ $VERSION_RE ]] || error "HOOKS_VERSION '${HOOKS_VERSION}' is not MAJOR.MINOR.PATCH"
  mkdir -p "$RUNNER_HOME"
  cd "$RUNNER_HOME"
  install_runner
  install_hooks
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
  user_systemctl is-active "actions-runner.service" 2>/dev/null || true
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
#   3. SIGINT every Listener of this account, in the unit's cgroup or not: the ones a
#      stop under KillMode=process left behind. SIGINT is the Listener's own graceful stop (Ctrl-C,
#      Runner.cs CtrlCHandler): it cancels its job through the Worker, reports it, and
#      deletes its session. Wait up to RUNNER_STOP_GRACE_SECONDS for them to exit.
#   4. SIGKILL whatever is still in the cgroup and any Listener that survived, and
#      require both to be gone.
cmd_quiesce() {
  local drain="${RUNNER_DRAIN_SECONDS:-600}" grace="${RUNNER_STOP_GRACE_SECONDS:-300}"
  local uid deadline rc=0 cg procs="" listeners="" left stray
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
  # No cgroup found means an empty, removed cgroup, or a layout the search above does not
  # know. The cgroup sweep is then skipped; the Listeners below are found from /proc
  # either way.
  if [ -n "$procs" ] && [ ! -r "$procs" ]; then procs=""; fi

  # Every Listener of this account, in the unit's cgroup or not. The account exists only
  # for this runner, so any Listener of it is this runner's registration, and one left
  # running beside the next start would hold the session (the 2026-10-12 state).
  listeners=$(account_pids Runner.Listener | tr '\n' ' ')
  # shellcheck disable=SC2086
  echo "signalled=$(count $listeners)"
  if [ -n "$listeners" ]; then
    # shellcheck disable=SC2086
    kill -INT $listeners 2>/dev/null || true
    deadline=$((SECONDS + grace))
    # shellcheck disable=SC2086
    while [ -n "$(live_listeners $listeners)" ] && [ "$SECONDS" -lt "$deadline" ]; do sleep 2; done
  fi

  left=""
  if [ -n "$procs" ]; then
    # A Listener's run-helper.sh exits once the Listener has; give the scripts a moment.
    deadline=$((SECONDS + 10))
    while [ -n "$(cat "$procs" 2>/dev/null || true)" ] && [ "$SECONDS" -lt "$deadline" ]; do sleep 1; done
    left=$(cat "$procs" 2>/dev/null | tr '\n' ' ' || true)
  fi
  # Listeners that survived SIGINT, wherever they are, join the SIGKILL.
  # shellcheck disable=SC2086
  stray=$(live_listeners $listeners | tr '\n' ' ')
  # shellcheck disable=SC2086
  echo "killed=$(count $left $stray)"
  if [ -n "$left$stray" ]; then
    # shellcheck disable=SC2086
    kill -KILL $left $stray 2>/dev/null || true
    deadline=$((SECONDS + 30))
    # shellcheck disable=SC2086
    while { [ -n "$procs" ] && [ -n "$(cat "$procs" 2>/dev/null || true)" ]; } \
          || [ -n "$(live_listeners $stray)" ]; do
      [ "$SECONDS" -lt "$deadline" ] || break
      sleep 1
    done
  fi
  left=""
  if [ -n "$procs" ]; then left=$(cat "$procs" 2>/dev/null | tr '\n' ' ' || true); fi
  # shellcheck disable=SC2086
  stray=$(live_listeners $stray | tr '\n' ' ')
  # shellcheck disable=SC2086
  echo "remaining=$(count $left $stray)"
  [ -z "$left" ] || error "still in the cgroup of $UNIT after SIGKILL: $left"
  [ -z "$stray" ] || error "Listeners of this account still running after SIGKILL: $stray"
}

case "${1:-}" in
  install)   cmd_install ;;
  configure) cmd_configure ;;
  remove)    cmd_remove ;;
  start)     user_systemctl enable --now actions-runner.service; info "started" ;;
  stop)      stop_runner ;;
  status)    cmd_status ;;
  procs)     cmd_procs ;;
  quiesce)   cmd_quiesce ;;
  *)         error "usage: $0 {install|configure|remove|start|stop|status|procs|quiesce}" ;;
esac
