#!/usr/bin/env bats
# Structural tests for the github-runner service — install/lifecycle + isolation config.
#
# The properties held here are the ones whose failure is invisible until a live job runs
# on a hardened host: a token in argv, isolation that a workflow can opt out of, a
# cleanup hook that fails the job it was meant to tidy up after, or a version pin that
# a self-update quietly undoes.
#
# Run: bats platform/tests/test_service_github_runner.bats

load assert_helpers

setup() {
  D="$BATS_TEST_DIRNAME/../services/github-runner/deployment"
  SH="$D/deploy.sh"
  [ -f "$SH" ]
}

@test "gh-runner: deploy.sh is executable, bash, and sources the shared lib" {
  [ -x "$SH" ]
  head -1 "$SH" | grep -q 'bash'
  grep -qF 'lib/common.sh' "$SH"
}

@test "gh-runner: deploy.sh is lifecycle-only — no secret generation or OpenBao" {
  # Same division every other service keeps: Ansible owns the credential lifecycle,
  # the script owns the process.
  #
  # Checked against EXECUTABLE lines only. The header comment names OpenBao precisely
  # to explain that division, and a check that forbids describing the boundary
  # discourages the comment that makes the boundary legible.
  local code
  code=$(grep -vE '^\s*#' "$SH")
  ! printf '%s' "$code" | grep -qiE 'openbao|bao_client|hashi_vault|gen_secret|put_secret'
}

@test "gh-runner: the registration token never reaches argv" {
  # argv is world-readable through /proc for the life of the process, so --token would
  # expose a live registration credential to every local user. The runner reads
  # ACTIONS_RUNNER_INPUT_<ARG> from the environment and deletes the variable after
  # reading it (Runner.Listener/CommandSettings.cs) — that is the argv-free route, and
  # there is no --token-file option.
  grep -qF 'ACTIONS_RUNNER_INPUT_TOKEN="$token" ./config.sh' "$SH"
  # CODE only: the script's comments name --token to explain why it is NOT used, and
  # forbidding the explanation suppresses it (docs/MISTAKES.md §2.8).
  grep -vE '^[[:space:]]*#' "$SH" > "$BATS_TEST_TMPDIR/code.sh"
  refute_grep -qE '\-\-token[= ]' "$BATS_TEST_TMPDIR/code.sh"
  # And it arrives on stdin, not as a script argument.
  grep -qF 'token=$(cat)' "$SH"
}

@test "gh-runner: the archive digest is verified before anything is executed" {
  grep -qF 'verify_digest' "$SH"
  # A mismatch must delete the artefact and abort, not warn and continue.
  grep -qF 'digest mismatch' "$SH"
  local fn
  fn=$(sed -n '/^verify_digest() {$/,/^}$/p' "$SH")
  assert_contains "$fn" 'rm -f "$file"'
  assert_contains "$fn" 'error "digest mismatch'
  # An absent digest is a refusal, not a skip — "no digest" and "unverified" are the
  # same state from here.
  grep -qF 'refusing to run an unverified artefact' "$SH"
}

@test "gh-runner: the digest is fetched per pinned version, never hardcoded" {
  # A constant in the repo goes stale the first time the pin moves, and a
  # stale-but-present digest reads as verified when it is not.
  refute_grep -qE '^[A-Z_]*SHA256=[0-9a-f]{64}' "$SH"
  grep -qF 'RUNNER_SHA256' "$D/templates/runner.env.j2"
  grep -qF '{{ _runner_sha256 }}' "$D/templates/runner.env.j2"
}

@test "gh-runner: re-running install or configure is a no-op, not a clobber" {
  # Extracting over a configured runner would destroy .runner/.credentials and
  # de-register the host in place.
  grep -qF '[ -x "${RUNNER_HOME}/run.sh" ] || tar xzf' "$SH"
  grep -qF 'already configured — leaving registration intact' "$SH"
}

@test "gh-runner: the version pin cannot be undone by a self-update" {
  grep -qF -- '--disableupdate' "$SH"
  # And a rebuilt host reclaims its own name instead of leaving a second offline entry.
  grep -qF -- '--replace' "$SH"
}

@test "gh-runner: a runner can be withdrawn without destroying its host" {
  grep -qF 'cmd_remove()' "$SH"
  grep -qF 'de-registered' "$SH"
  grep -qF 'host left intact' "$SH"
}

@test "gh-runner: isolation is host-configured, not workflow-requested" {
  # A workflow-level container declaration is opt-in, and an opt-in isolation boundary
  # fails open on the first job that omits it.
  grep -qF 'ACTIONS_RUNNER_CONTAINER_HOOKS=' "$D/templates/runner-dotenv.j2"
  grep -qF 'ACTIONS_RUNNER_HOOK_JOB_COMPLETED=' "$D/templates/runner-dotenv.j2"
}

@test "gh-runner: the cleanup hook can never fail the job it cleans up after" {
  # A non-zero exit from a job-completed hook marks the job failed, which turns a
  # cleanup hiccup into a red build and teaches people to disable the hook.
  local H="$D/templates/job-cleanup.sh.j2"
  grep -qF 'exit 0' "$H"
  refute_grep -qE '^set -e' "$H"
  # Every destructive step tolerates failure and says so.
  grep -qF '|| log "WARNING' "$H"
}

@test "gh-runner: cleanup clears repository content but keeps the runner's own caches" {
  # Deleting _tool/_temp/_actions every job makes each build slower for no isolation
  # gain — they hold no repository content.
  local H="$D/templates/job-cleanup.sh.j2"
  grep -qF "! -name '_tool'" "$H"
  grep -qF "! -name '_temp'" "$H"
  grep -qF 'rm -rf' "$H"
}

@test "gh-runner: the unit restarts the runner and is not severed mid-build" {
  local U="$D/templates/actions-runner.service.j2"
  assert_grep -qF 'Restart=always' "$U"
  # An in-flight job must not be severed mid-build.
  assert_grep -qF 'TimeoutStopSec=5min' "$U"
}

@test "gh-runner: the unit leaves no_new_privs off, so rootless podman can map ids" {
  # Jobs run rootless podman, which maps the account's subordinate range through the
  # setuid newuidmap/newgidmap. no_new_privs makes setuid bits inert on execve and is
  # inherited by every job, so with NoNewPrivileges=true every container job failed with
  # "newuidmap: write to uid_map failed: Operation not permitted" (2026-10-11).
  local U="$D/templates/actions-runner.service.j2"
  grep -vE '^[[:space:]]*#' "$U" > "$BATS_TEST_TMPDIR/unit"
  assert_grep -qxF 'NoNewPrivileges=false' "$BATS_TEST_TMPDIR/unit"
  refute_grep -qiE '^NoNewPrivileges=(true|yes|on|1)' "$BATS_TEST_TMPDIR/unit"
  # A user manager lacks CAP_SYS_ADMIN, so systemd sets no_new_privs for every
  # seccomp-backed option (exec-invoke.c context_has_seccomp, v255). In a user unit the
  # namespace and capability options imply PrivateUsers=, which maps only the account's
  # own uid. DynamicUser= forces no_new_privs outright. Each breaks rootless podman just
  # as NoNewPrivileges=true did. Code lines only: the unit's comment names them to say
  # why they are absent.
  refute_grep -qE '^(SystemCallFilter|SystemCallArchitectures|SystemCallLog|RestrictNamespaces|RestrictAddressFamilies|RestrictRealtime|RestrictSUIDSGID|LockPersonality|MemoryDenyWriteExecute|PrivateDevices|ProtectClock|ProtectHostname|ProtectKernelTunables|ProtectKernelModules|ProtectKernelLogs|PrivateUsers|PrivateTmp|PrivateNetwork|PrivateIPC|PrivateMounts|ProtectHome|ReadWritePaths|ReadOnlyPaths|InaccessiblePaths|ExecPaths|NoExecPaths|TemporaryFileSystem|CapabilityBoundingSet|AmbientCapabilities|DynamicUser)=' "$BATS_TEST_TMPDIR/unit"
}

@test "gh-runner: no templates carry real addresses or a literal token" {
  refute_grep -rqE '(192\.168\.[0-9]+\.[0-9]+|10\.[0-9]+\.[0-9]+\.[0-9]+|172\.(1[6-9]|2[0-9]|3[01])\.[0-9]+\.[0-9]+)' "$D"
  ! grep -rqiE '(gh[pousr]_[A-Za-z0-9]{16}|A[A-Z0-9]{20,})' "$D/templates"
}

@test "gh-runner: the deploy proves rootless podman maps a uid range for the runner account" {
  # Container jobs run through the hooks as the runner account. Without newuidmap or a
  # subuid range every one of them fails, and the deploy used to report success anyway.
  local PB="$BATS_TEST_DIRNAME/../playbooks/deploy-github-runner.yml"
  assert_grep -qF 'podman unshare cat /proc/self/uid_map' "$PB"
  assert_grep -qF 'podman unshare cat /proc/self/gid_map' "$PB"
  assert_grep -qF 'podman system migrate' "$PB"
  assert_grep -qF "(_uidmap.stdout_lines.index('gid_map') - _uidmap.stdout_lines.index('uid_map') - 1) < 2" "$PB"
}

@test "gh-runner: the rootless check runs where jobs run, not in a sudo session" {
  # The 2026-10-11 deploy passed its podman check through become_user, a sudo session
  # outside the runner unit, while every job (a descendant of a NoNewPrivileges=true unit)
  # failed. The check must run under the user manager with the unit's own setting, and
  # the live service's effective no_new_privs bit must be read from the kernel.
  local PB="$BATS_TEST_DIRNAME/../playbooks/deploy-github-runner.yml"
  assert_grep -qF 'systemd-run --user --wait --pipe --collect --quiet' "$PB"
  assert_grep -qF -- '--property=NoNewPrivileges="$nnp"' "$PB"
  assert_grep -qF 'systemctl --user show --property=NoNewPrivileges --value actions-runner.service' "$PB"
  # The effective bit is read from the kernel for the processes that run jobs (the
  # Listener and any Worker) as well as the unit's main process, and the deploy fails
  # unless none carries it (MISTAKES 10.27: MainPID alone is run.sh).
  assert_grep -qF '"$PROC_ROOT/$pid/status"' "$SH"
  assert_grep -qF 'for pid in $(account_pids Runner.Listener); do describe_proc listener "$pid"; done' "$SH"
  assert_grep -qF 'for pid in $(account_pids Runner.Worker); do describe_proc worker "$pid"; done' "$SH"
  assert_grep -qxF '      - no_new_privs=0' "$PB"
  assert_grep -qxF '      - private_userns=0' "$PB"
  # A changed unit must reach the running process, which keeps the properties it was
  # started with, so the deploy restarts the service when the unit file changed.
  assert_grep -qF "{{ (_runner_unit.changed | default(false))" "$PB"
}

@test "gh-runner: the deploy requires a subordinate uid and gid range for the account" {
  local PB="$BATS_TEST_DIRNAME/../playbooks/deploy-github-runner.yml"
  assert_grep -qF 'loop: [/etc/subuid, /etc/subgid]' "$PB"
  assert_grep -qF '_subid_counts[0] >= 65536' "$PB"
  assert_grep -qF '_subid_counts[1] >= 65536' "$PB"
}

@test "gh-runner: the deploy strips unused setuid helpers and keeps the ones it needs" {
  # no_new_privs is off, so every setuid program is reachable from a job. The ones a
  # runner host never uses lose the bit through dpkg-statoverride, which survives package
  # upgrades; sudo (Ansible's escalation) and newuidmap/newgidmap (rootless podman) stay.
  local PB="$BATS_TEST_DIRNAME/../playbooks/deploy-github-runner.yml"
  local block
  block=$(sed -n '/^    - name: "Remove the setuid and setgid bits a runner host does not use"$/,/^    - name: "Read back the mode/p' "$PB")
  [ -n "$block" ]
  assert_contains "$block" 'dpkg-statoverride --update --add'
  local f
  for f in /usr/bin/chfn /usr/bin/chsh /usr/bin/gpasswd /usr/bin/newgrp /usr/bin/passwd \
           /usr/bin/su /usr/bin/chage /usr/bin/expiry /usr/bin/crontab \
           /usr/lib/openssh/ssh-keysign /usr/lib/polkit-1/polkit-agent-helper-1; do
    assert_contains "$block" "path: $f,"
  done
  local keep
  for keep in /usr/bin/sudo /usr/bin/newuidmap /usr/bin/newgidmap; do
    refute_contains "$block" "path: $keep,"
  done
  # And the result is read back from the inode, not trusted from the override database.
  assert_grep -qF '_setuid_still | length == 0' "$PB"
}

@test "gh-runner: a dry run does not judge the registration it skipped" {
  local PB="$BATS_TEST_DIRNAME/../playbooks/deploy-github-runner.yml"
  local block
  block="$(awk '/- name: "Fail if the runner did not register"/,/^$/' "$PB")"
  assert_contains "$block" "when: not ansible_check_mode"
}

@test "gh-runner: a changed .env restarts the runner, which reads it only at start" {
  # Runner.Listener/Program.cs loads .env once, when the process starts. A new hook
  # variable written to a running runner's .env does nothing until the next restart.
  local PB="$BATS_TEST_DIRNAME/../playbooks/deploy-github-runner.yml"
  assert_grep -qF 'register: _runner_files' "$PB"
  assert_grep -qF "or ('runner-dotenv.j2' in (_runner_files.results | default([])" "$PB"
  assert_grep -qF '_runner_restart: >-' "$PB"
}

@test "gh-runner: the job-started hook is wired and renders a script that makes every bind source" {
  # Podman, unlike Docker, refuses a bind mount whose source is missing. The runner
  # deletes _actions at the start of every job and makes the tool cache only on demand,
  # so a container job that used no action failed in "Initialize containers".
  local H="$D/templates/job-started.sh.j2"
  assert_grep -qF 'ACTIONS_RUNNER_HOOK_JOB_STARTED={{ github_runner_home }}/job-started.sh' "$D/templates/runner-dotenv.j2"
  assert_grep -qF '{ src: "job-started.sh.j2", dest: "{{ _home }}/job-started.sh", mode: "0750" }' \
    "$BATS_TEST_DIRNAME/../playbooks/deploy-github-runner.yml"
  # Rendered and run, not grepped: the directories must exist afterwards.
  local home="$BATS_TEST_TMPDIR/home"
  mkdir -p "$home"
  sed "s#{{ github_runner_home }}#$home#g" "$H" > "$BATS_TEST_TMPDIR/job-started.sh"
  refute_grep -qF '{{' "$BATS_TEST_TMPDIR/job-started.sh"
  run env -u RUNNER_TEMP -u RUNNER_TOOL_CACHE bash "$BATS_TEST_TMPDIR/job-started.sh"
  [ "$status" -eq 0 ]
  local d
  for d in _actions _tool _temp/_github_home _temp/_github_workflow; do
    [ -d "$home/_work/$d" ] || { echo "missing $d"; false; }
  done
  # In a job the runner exports its own temp and tool-cache paths; those win.
  run env RUNNER_TEMP="$BATS_TEST_TMPDIR/rt" RUNNER_TOOL_CACHE="$BATS_TEST_TMPDIR/tc" \
    bash "$BATS_TEST_TMPDIR/job-started.sh"
  [ "$status" -eq 0 ]
  [ -d "$BATS_TEST_TMPDIR/rt/_github_home" ]
  [ -d "$BATS_TEST_TMPDIR/rt/_github_workflow" ]
  [ -d "$BATS_TEST_TMPDIR/tc" ]
}

@test "gh-runner: the job-started hook never fails a job" {
  # Most jobs declare no container. A failing job-started hook would fail every one of
  # them for a directory only a container job needs.
  local H="$D/templates/job-started.sh.j2"
  refute_grep -qE '^set -e' "$H"
  assert_grep -qxF 'exit 0' "$H"
  local home="$BATS_TEST_TMPDIR/ro"
  mkdir -p "$home"
  : > "$home/_work"   # a file where the work directory should be: every mkdir fails
  sed "s#{{ github_runner_home }}#$home#g" "$H" > "$BATS_TEST_TMPDIR/job-started.sh"
  run env -u RUNNER_TEMP -u RUNNER_TOOL_CACHE bash "$BATS_TEST_TMPDIR/job-started.sh"
  [ "$status" -eq 0 ]
  assert_contains "$output" 'WARNING: could not create'
}

@test "gh-runner: the Docker socket path leads to the runner account's rootless socket, never root's" {
  # The runner mounts /var/run/docker.sock into every job container (hardcoded upstream).
  # podman-docker links it to root's socket, which the account cannot reach. It is
  # pointed at the account's own socket by a tmpfiles.d file that replaces the package's.
  local PB="$BATS_TEST_DIRNAME/../playbooks/deploy-github-runner.yml"
  local T="$BATS_TEST_DIRNAME/../playbooks/tasks/podman-user-docker-socket.yml"
  assert_grep -qF 'ansible.builtin.include_tasks: tasks/podman-user-docker-socket.yml' "$PB"
  assert_grep -qF 'pds_user: "{{ _account }}"' "$PB"
  assert_grep -qF 'pds_uid: "{{ _runner_uid }}"' "$PB"
  assert_grep -qF 'dest: /etc/tmpfiles.d/podman-docker.conf' "$T"
  assert_grep -qF 'L+ /run/docker.sock - - - - /run/user/{{ pds_uid }}/podman/podman.sock' "$T"
  assert_grep -qF 'name: podman.socket' "$T"
  assert_grep -qF 'scope: user' "$T"
  # Root's socket is never a target, and nothing widens access to it.
  grep -vE '^[[:space:]]*#' "$T" > "$BATS_TEST_TMPDIR/code"
  refute_grep -qE 'L\+?[[:space:]].*/run/podman/podman\.sock' "$BATS_TEST_TMPDIR/code"
  refute_grep -qE 'chmod|setfacl|chgrp|SocketGroup|SocketMode' "$BATS_TEST_TMPDIR/code"
  refute_grep -qE 'SocketGroup|SocketMode|/run/podman' "$D/templates/runner-dotenv.j2"
  # And the effective tmpfiles.d config is read back, so a package file under another
  # name cannot keep the old link.
  assert_grep -qF 'systemd-tmpfiles --cat-config' "$T"
  assert_grep -qF '_pds_lines | length == 1' "$T"
}

@test "gh-runner: the deploy proves a hook-shaped docker create where jobs run" {
  # The uid-map check passed while every container job failed on its mounts, so the deploy
  # repeats the hooks' create with the runner's full mount set, inside the user manager.
  local PB="$BATS_TEST_DIRNAME/../playbooks/deploy-github-runner.yml"
  local block
  block=$(sed -n '/^    - name: "Confirm a job container can be created with the runner.s mounts/,/^    - name: "Report"$/p' "$PB")
  [ -n "$block" ]
  assert_contains "$block" 'systemd-run --user --wait --pipe --collect --quiet'
  assert_contains "$block" '--property=NoNewPrivileges="$nnp"'
  assert_contains "$block" '"$H/job-started.sh"'
  local m
  for m in '-v=/var/run/docker.sock:/var/run/docker.sock' '-v="$W":/__w' \
           '-v="$H/externals":/__e:ro' '-v="$W/_temp":/__w/_temp' \
           '-v="$W/_actions":/__w/_actions' '-v="$W/_tool":/__w/_tool' \
           '-v="$W/_temp/_github_home":/github/home' \
           '-v="$W/_temp/_github_workflow":/github/workflow'; do
    assert_contains "$block" "$m"
  done
  assert_contains "$block" "'create=ok' not in _mountprobe.stdout_lines"
  assert_contains "$block" "'ping=OK' not in _mountprobe.stdout_lines"
}

@test "gh-runner: the hooks find the runner's docker wrapper first on the runner's PATH" {
  # The hooks resolve `docker` with the runner's own PATH (@actions/exec looks the tool up
  # with the hook process's PATH, not the child's). The runner is started by run.sh, which
  # does not read .path, so the PATH line in .env is what puts _bin first.
  local PB="$BATS_TEST_DIRNAME/../playbooks/deploy-github-runner.yml"
  assert_grep -qxF 'PATH={{ github_runner_home }}/_bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin' \
    "$D/templates/runner-dotenv.j2"
  # The runner applies .env only when it starts; a .env change already restarts it.
  assert_grep -qF "or ('runner-dotenv.j2' in (_runner_files.results | default([])" "$PB"
  local t
  t=$(task_block "$PB" 'Install the docker wrapper the container hooks run')
  [ -n "$t" ]
  assert_contains "$t" 'docker-wrapper.sh.j2"'
  assert_contains "$t" 'dest: "{{ _home }}/_bin/docker"'
  assert_contains "$t" 'mode: "0750"'
  assert_contains "$t" 'github_runner_uid: "{{ _runner_uid }}"'
  # Not the runner's own bin/, which holds the runner's binaries from its archive.
  refute_contains "$t" '{{ _home }}/bin/'
  # After the uid is known (XDG_RUNTIME_DIR is /run/user/<uid>), and in place before the
  # runner restarts with the new PATH.
  assert_precedes "$PB" '_runner_uid: "\{\{ getent_passwd' 'name: "Install the docker wrapper the container hooks run"'
  assert_precedes "$PB" 'name: "Install the docker wrapper the container hooks run"' 'name: "Reload the account.s systemd and start the runner"'
}

@test "gh-runner: the docker wrapper gives podman the account's directories and keeps the job's HOME" {
  # The hooks start `docker create` with exactly the job's variables: HOME=/github/home
  # and no PATH or XDG_* directory. Podman then resolved HOME on the host and failed with
  # `cannot resolve /github/home`, and would have used a different image store from the
  # one `docker pull` (no HOME) wrote to. Rendered and run, not grepped.
  local T="$D/templates/docker-wrapper.sh.j2"
  assert_grep -qxF 'exec /usr/bin/podman "$@"' "$T"
  local home="$BATS_TEST_TMPDIR/home" stub="$BATS_TEST_TMPDIR/podman" w="$BATS_TEST_TMPDIR/docker"
  mkdir -p "$home"
  # The stub prints what it was GIVEN (/usr/bin/env), not what a shell would assume: a
  # shell sets itself an unexported default PATH when it receives none.
  cat > "$stub" <<'STUB'
#!/bin/sh
/usr/bin/env
for a in "$@"; do echo "arg=$a"; done
STUB
  chmod +x "$stub"
  sed -e "s#{{ github_runner_home }}#$home#g" -e 's#{{ github_runner_uid }}#1001#g' \
      -e "s#^exec /usr/bin/podman #exec $stub #" "$T" > "$w"
  refute_grep -qF '{{' "$w"
  assert_grep -qxF "exec $stub \"\$@\"" "$w"
  # As the hooks run `create`: the job's variables only.
  local sys='PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
  run env -i HOME=/github/home GITHUB_ACTIONS=true CI=true /bin/sh "$w" create -e HOME --name 'a b' img
  [ "$status" -eq 0 ]
  assert_contains "$output" 'HOME=/github/home'
  assert_contains "$output" "XDG_CONFIG_HOME=$home/.config"
  assert_contains "$output" "XDG_DATA_HOME=$home/.local/share"
  assert_contains "$output" 'XDG_RUNTIME_DIR=/run/user/1001'
  # An exported PATH, so podman can find newuidmap/newgidmap.
  assert_contains "$output" "$sys"
  # Arguments pass through unchanged, one per word, including one with a space.
  assert_contains "$output" "$(printf 'arg=create\narg=-e\narg=HOME\narg=--name\narg=a b\narg=img')"
  # As the hooks run `pull`: no HOME at all. The wrapper must not invent one, and the
  # store directory is the same as for `create`.
  run env -i /bin/sh "$w" pull img
  [ "$status" -eq 0 ]
  printf '%s\n' "$output" > "$BATS_TEST_TMPDIR/pull"
  refute_grep -qE '^HOME=' "$BATS_TEST_TMPDIR/pull"
  assert_contains "$output" "XDG_DATA_HOME=$home/.local/share"
  # A caller's directories and PATH are replaced: the wrapper's own _bin can never be
  # found again, and a job-chosen store is never used.
  run env -i PATH="$BATS_TEST_TMPDIR:/usr/bin" XDG_DATA_HOME=/job/data \
    XDG_CONFIG_HOME=/job/config XDG_RUNTIME_DIR=/tmp/r /bin/sh "$w" info
  [ "$status" -eq 0 ]
  assert_contains "$output" "$sys"
  assert_contains "$output" "XDG_DATA_HOME=$home/.local/share"
  assert_contains "$output" "XDG_CONFIG_HOME=$home/.config"
  assert_contains "$output" 'XDG_RUNTIME_DIR=/run/user/1001'
  # And no code line assigns HOME: `-e HOME` must hand the container the job's home.
  grep -vE '^[[:space:]]*#' "$T" > "$BATS_TEST_TMPDIR/code"
  refute_grep -qE '(^|[[:space:];])(export[[:space:]]+)?HOME=' "$BATS_TEST_TMPDIR/code"
}

@test "gh-runner: the docker wrapper hands a job its own PATH and XDG values, and no others change" {
  # The hooks pass each job variable as a bare `-e NAME`, and the engine fills that from
  # its own environment, which the wrapper replaces. A job's PATH or XDG_* value must
  # still reach its container; a bare -e of one the job did not set is dropped, as
  # docker drops a bare -e of an unset variable. The stub records argv NUL-separated, so
  # the comparison is byte for byte.
  local T="$D/templates/docker-wrapper.sh.j2"
  local stub="$BATS_TEST_TMPDIR/podman" w="$BATS_TEST_TMPDIR/docker" out="$BATS_TEST_TMPDIR/argv"
  cat > "$stub" <<STUB
#!/bin/sh
printf '%s\0' "\$@" > "$out"
/usr/bin/printenv XDG_DATA_HOME
STUB
  chmod +x "$stub"
  sed -e "s#{{ github_runner_home }}#/opt/r#g" -e 's#{{ github_runner_uid }}#1001#g' \
      -e "s#^exec /usr/bin/podman #exec $stub #" "$T" > "$w"
  refute_grep -qF '{{' "$w"
  # (a) every form, for all four names; a value with a space and one ending in a newline.
  run env -i HOME=/github/home PATH=/job/bin:/usr/bin XDG_CONFIG_HOME='/job/c fg' \
    XDG_DATA_HOME=$'/job/d\n' XDG_RUNTIME_DIR=/job/r /bin/sh "$w" create \
    -e PATH --env XDG_CONFIG_HOME -e=XDG_DATA_HOME --env=XDG_RUNTIME_DIR -e HOME \
    --name 'a b' -e 'K=v=w x' -e=OTHER --env-file f -v=/x:/y img cmd -e
  [ "$status" -eq 0 ]
  # The engine itself still gets the account's store.
  [ "$output" = '/opt/r/.local/share' ]
  printf '%s\0' create -e PATH=/job/bin:/usr/bin --env 'XDG_CONFIG_HOME=/job/c fg' \
    -e $'XDG_DATA_HOME=/job/d\n' --env XDG_RUNTIME_DIR=/job/r -e HOME \
    --name 'a b' -e 'K=v=w x' -e=OTHER --env-file f -v=/x:/y img cmd -e > "$BATS_TEST_TMPDIR/want"
  cmp "$BATS_TEST_TMPDIR/want" "$out"
  # (b) names the caller did not set: the pair is dropped, everything else kept.
  run env -i HOME=/github/home /bin/sh "$w" create -e PATH --env=XDG_DATA_HOME \
    --env XDG_RUNTIME_DIR -e=XDG_CONFIG_HOME -e HOME --name 'a b' img
  [ "$status" -eq 0 ]
  printf '%s\0' create -e HOME --name 'a b' img > "$BATS_TEST_TMPDIR/want"
  cmp "$BATS_TEST_TMPDIR/want" "$out"
  # (c) a call with no -e of these names passes through byte for byte.
  run env -i /bin/sh "$w" pull 'img:x y' --format='{{.ID}} a=b' ''
  [ "$status" -eq 0 ]
  printf '%s\0' pull 'img:x y' --format='{{.ID}} a=b' '' > "$BATS_TEST_TMPDIR/want"
  cmp "$BATS_TEST_TMPDIR/want" "$out"
}

@test "gh-runner: the docker wrapper rewrites only the options before the image, never the container's command" {
  # Podman reads everything from the IMAGE (CONTAINER for exec) on as the container's
  # command (SetInterspersed(false)), so `echo -e PATH` there must reach podman as-is.
  # The rewrite is limited to create/run/exec and stops at the first positional
  # argument; an option it does not know ends it early (fail safe).
  local T="$D/templates/docker-wrapper.sh.j2"
  local stub="$BATS_TEST_TMPDIR/podman" w="$BATS_TEST_TMPDIR/docker" out="$BATS_TEST_TMPDIR/argv"
  cat > "$stub" <<STUB
#!/bin/sh
printf '%s\0' "\$@" > "$out"
STUB
  chmod +x "$stub"
  sed -e "s#{{ github_runner_home }}#/opt/r#g" -e 's#{{ github_runner_uid }}#1001#g' \
      -e "s#^exec /usr/bin/podman #exec $stub #" "$T" > "$w"
  local want="$BATS_TEST_TMPDIR/want"
  # Hooks' create shape (container.ts containerCreate), from a real job log, plus a
  # command after the image that contains `-e PATH`.
  run env -i HOME=/github/home PATH=/job/bin /bin/sh "$w" create \
    --label=a1b2 --network=github_network_1 --name job_c -p 8080:80 -e PATH -e HOME \
    -e GITHUB_ACTIONS=true -e CI=true -v=/var/run/docker.sock:/var/run/docker.sock \
    -v=/w:/__w:ro --entrypoint tail img:1 -f /dev/null -e PATH --env=PATH
  [ "$status" -eq 0 ]
  printf '%s\0' create \
    --label=a1b2 --network=github_network_1 --name job_c -p 8080:80 -e PATH=/job/bin -e HOME \
    -e GITHUB_ACTIONS=true -e CI=true -v=/var/run/docker.sock:/var/run/docker.sock \
    -v=/w:/__w:ro --entrypoint tail img:1 -f /dev/null -e PATH --env=PATH > "$want"
  cmp "$want" "$out"
  # Hooks' run shape (containerRun): `-v` with a separate value, a boolean, a command.
  run env -i PATH=/job/bin /bin/sh "$w" run --rm --name step_1 --workdir=/__w/r \
    -e PATH -v /a:/b --entrypoint sh img echo -e PATH
  [ "$status" -eq 0 ]
  printf '%s\0' run --rm --name step_1 --workdir=/__w/r \
    -e PATH=/job/bin -v /a:/b --entrypoint sh img echo -e PATH > "$want"
  cmp "$want" "$out"
  # Hooks' exec shape (containerExecStep): rewritten before the CONTAINER only; an unset
  # name is dropped; the hooks' own `-e PATH=...` is untouched.
  run env -i PATH='/job bin' /bin/sh "$w" exec -i --workdir=/__w/r -e PATH -e XDG_DATA_HOME \
    -e PATH=/pre:/usr/bin cid0 sh -e PATH
  [ "$status" -eq 0 ]
  printf '%s\0' exec -i --workdir=/__w/r -e 'PATH=/job bin' \
    -e PATH=/pre:/usr/bin cid0 sh -e PATH > "$want"
  cmp "$want" "$out"
  # A boolean right before the CONTAINER must not swallow it: the command that follows
  # (here one starting with -e) stays untouched.
  run env -i PATH=/job/bin /bin/sh "$w" exec -i cid0 -e PATH
  [ "$status" -eq 0 ]
  printf '%s\0' exec -i cid0 -e PATH > "$want"
  cmp "$want" "$out"
  # Fail safe: an unknown option without `=`, a combined short form, or `--` ends the
  # rewrite; what follows passes through unchanged.
  local stop
  for stop in --not-a-podman-flag -it --; do
    run env -i PATH=/job/bin /bin/sh "$w" create --name n "$stop" -e PATH img
    [ "$status" -eq 0 ]
    printf '%s\0' create --name n "$stop" -e PATH img > "$want"
    cmp "$want" "$out" || { echo "rewrote after $stop"; false; }
  done
  # Any other subcommand passes through unchanged, even with a job PATH set.
  local sub
  for sub in start pull logs; do
    run env -i PATH=/job/bin /bin/sh "$w" "$sub" -e PATH x
    [ "$status" -eq 0 ]
    printf '%s\0' "$sub" -e PATH x > "$want"
    cmp "$want" "$out" || { echo "rewrote for $sub"; false; }
  done
}

@test "gh-runner: the deploy probe runs docker the way the hooks do, environment included" {
  # The 10.24 probe ran `docker create` with the user manager's environment and passed
  # while every container job failed with `cannot resolve /github/home` (MISTAKES 10.25).
  local PB="$BATS_TEST_DIRNAME/../playbooks/deploy-github-runner.yml"
  local block
  block=$(sed -n '/^    - name: "Confirm a job container can be created with the runner.s mounts/,/^    - name: "Report"$/p' "$PB")
  [ -n "$block" ]
  # `docker` is found the way the hooks find it, and it is the wrapper.
  assert_contains "$block" "runner_path=\$(sed -n 's/^PATH=//p' \"\$H/.env\")"
  assert_contains "$block" 'dk=$(PATH="$runner_path" command -v docker)'
  assert_contains "$block" '[ "$dk" = "$H/_bin/docker" ]'
  # `pull`-shaped calls: no environment at all. And no `system migrate`: it stops running
  # containers, so it could stop a job that started after the uid-map check.
  printf '%s\n' "$block" | grep -vE '^[[:space:]]*#' > "$BATS_TEST_TMPDIR/probe-code"
  refute_grep -qF 'system migrate' "$BATS_TEST_TMPDIR/probe-code"
  assert_contains "$block" 'env -i "$dk" import "$src/rootfs.tar" "$img"'
  assert_contains "$block" 'env -i "$dk" image exists "$img"'
  # The `create`: the job's variables only, with -e HOME, on the same image.
  assert_contains "$block" 'env -i HOME=/github/home GITHUB_ACTIONS=true CI=true "$dk" create --name "$probe"'
  assert_contains "$block" '-e HOME -e GITHUB_ACTIONS=true -e CI=true'
  assert_contains "$block" "grep -qF '\"HOME=/github/home\"' <<<\"\$cfg\""
  # No engine call in the probe body runs outside the hooks' environment.
  printf '%s\n' "$block" > "$BATS_TEST_TMPDIR/block"
  refute_grep -qE '^[[:space:]]*(docker|podman|"\$dk")[[:space:]]' "$BATS_TEST_TMPDIR/block"
  local r
  for r in "('docker=' ~ _home ~ '/_bin/docker') not in _mountprobe.stdout_lines" \
           "'import=ok' not in _mountprobe.stdout_lines" \
           "'create=ok' not in _mountprobe.stdout_lines" \
           "'home=job' not in _mountprobe.stdout_lines" \
           "'ping=OK' not in _mountprobe.stdout_lines"; do
    assert_contains "$block" "$r"
  done
}

# ── restarts reach the Listener (docs/MISTAKES.md 10.27) ─────────────────────────
# Until 2026-10-12 the unit had KillMode=process and started run.sh, which passes no
# signal on: every restart signalled run.sh alone, the Listener that held the session
# kept running and taking jobs, and a new one was stacked beside it. Both hosts carried
# three Listeners, and the one serving jobs dated from boot, so no unit or .env change
# since then had reached it.

@test "gh-runner: a stop reaches the Listener through run.sh, and leaves nothing in the cgroup" {
  local U="$D/templates/actions-runner.service.j2"
  grep -vE '^[[:space:]]*#' "$U" > "$BATS_TEST_TMPDIR/unit"
  # run.sh turns TERM into SIGINT for run-helper.sh's process group, the Listener's own
  # graceful stop, only when this is set (actions/runner run.sh, runWithManualTrap).
  assert_grep -qxF 'Environment=RUNNER_MANUALLY_TRAP_SIG=1' "$BATS_TEST_TMPDIR/unit"
  assert_grep -qxF 'ExecStart={{ github_runner_home }}/run.sh' "$BATS_TEST_TMPDIR/unit"
  # SIGTERM to run.sh only, then SIGKILL to whatever is left in the cgroup.
  assert_grep -qxF 'KillMode=mixed' "$BATS_TEST_TMPDIR/unit"
  assert_grep -qxF 'KillSignal=SIGTERM' "$BATS_TEST_TMPDIR/unit"
  refute_grep -qE '^KillMode=(process|none)' "$BATS_TEST_TMPDIR/unit"
  refute_grep -qE '^SendSIGKILL=(no|false|off|0)' "$BATS_TEST_TMPDIR/unit"
  # run.sh exits 143 after a trapped TERM.
  assert_grep -qxF 'SuccessExitStatus=143' "$BATS_TEST_TMPDIR/unit"
}

@test "gh-runner: the deploy never restarts the unit in place; it quiesces it, then starts it" {
  local PB="$BATS_TEST_DIRNAME/../playbooks/deploy-github-runner.yml"
  # Structural, not textual. A line grep missed `state: "restarted"` and could not tell the
  # runner's unit from another one. Every task in every play (blocks, rescue, always and
  # handlers included) that restarts a unit, by a service module or by systemctl in a shell,
  # must name exactly systemd-binfmt.service: the one restart this playbook makes, which
  # registers the arm64 emulator ("Register the arm64 binfmt handler now"). The runner's own
  # unit is never restarted in place, because a restart stops the Listener holding a job; it
  # goes through the quiesce, then the start. The walk must reach that start task, or it has
  # checked nothing.
  python3 - "$PB" <<'PYTHON'
import re
import sys

import yaml

ALLOWED_RESTART_UNITS = {"systemd-binfmt.service"}
RUNNER_UNIT = "actions-runner.service"
SERVICE_MODULES = {"systemd_service", "systemd", "service"}
SHELL_MODULES = {"command", "shell"}


def short(key):
    for prefix in ("ansible.builtin.", "ansible.legacy."):
        if key.startswith(prefix):
            return key[len(prefix):]
    return key


def walk(tasks):
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        yield task
        for section in ("block", "rescue", "always"):
            yield from walk(task.get(section))


with open(sys.argv[1]) as source:
    plays = yaml.safe_load(source)

tasks = []
for play in plays:
    for section in ("pre_tasks", "tasks", "post_tasks", "handlers"):
        tasks.extend(walk(play.get(section)))

failures = []
reached_runner_unit = False
for task in tasks:
    for key, args in task.items():
        module = short(key)
        if module in SERVICE_MODULES and isinstance(args, dict):
            if args.get("name") == RUNNER_UNIT:
                reached_runner_unit = True
            if args.get("state") == "restarted" and args.get("name") not in ALLOWED_RESTART_UNITS:
                failures.append(f"{task.get('name')!r} restarts {args.get('name')!r} in place")
        if module in SHELL_MODULES:
            cmd = args if isinstance(args, str) else (args or {}).get("cmd", "")
            if re.search(r"systemctl\b.*\brestart\b", str(cmd)):
                failures.append(f"{task.get('name')!r} restarts a unit from a shell command")

if not reached_runner_unit:
    failures.append(f"the walk never reached the {RUNNER_UNIT} task, so nothing was checked")
if failures:
    print("\n".join(failures), file=sys.stderr)
    sys.exit(1)
PYTHON
  local q s
  q=$(grep -nF '    - name: "Drain, stop and empty the runner'"'"'s unit"' "$PB" | cut -d: -f1)
  s=$(grep -nF '    - name: "Reload the account'"'"'s systemd and start the runner"' "$PB" | cut -d: -f1)
  [ -n "$q" ]
  [ -n "$s" ]
  # The quiesce runs BEFORE the daemon reload: on a host still on the KillMode=process
  # unit, a reload first would make the stop SIGKILL the Listener that holds the job.
  [ "$q" -lt "$s" ]
  local block
  block=$(sed -n '/^    - name: "Drain, stop and empty the runner.s unit"$/,/^    - name: "Reload the account/p' "$PB")
  assert_contains "$block" 'ansible.builtin.command: "{{ _deploy_dir }}/deploy.sh quiesce"'
  assert_contains "$block" 'when: _runner_restart | bool and not ansible_check_mode'
  assert_contains "$block" "RUNNER_DRAIN_SECONDS: \"{{ github_runner_drain_seconds | default(600) }}\""
  # A restart is also due when the processes are not in the promised state, so a run
  # after a failed one converges, not only one that changed a file.
  assert_grep -qF "or (_runner_converged | difference(_procs_before.stdout_lines) | length > 0)" "$PB"
  # A dry run reads the host's checkout as it is, which may predate `procs`: it reports a
  # restart instead of failing. A real run fails on any probe error.
  assert_grep -qF 'failed_when: _procs_before.rc != 0 and not ansible_check_mode' "$PB"
  assert_grep -qF 'failed_when: _procs.rc != 0 and not ansible_check_mode' "$PB"
}

@test "gh-runner: quiesce drains, stops, signals Listeners gracefully, then kills the rest" {
  local fn
  fn=$(sed -n '/^cmd_quiesce() {$/,/^}$/p' "$SH")
  [ -n "$fn" ]
  assert_contains "$fn" 'account_pids Runner.Worker'
  assert_contains "$fn" 'systemctl --user stop "$UNIT" || rc=$?'
  assert_contains "$fn" 'kill -INT $listeners'
  assert_contains "$fn" 'kill -KILL $left'
  assert_contains "$fn" 'error "still in the cgroup of $UNIT after SIGKILL: $left"'
  # Order: drain, stop, SIGINT, SIGKILL.
  local d st i k
  d=$(printf '%s\n' "$fn" | grep -nF 'account_pids Runner.Worker' | head -n 1 | cut -d: -f1)
  st=$(printf '%s\n' "$fn" | grep -nF 'systemctl --user stop' | head -n 1 | cut -d: -f1)
  i=$(printf '%s\n' "$fn" | grep -nF 'kill -INT' | head -n 1 | cut -d: -f1)
  k=$(printf '%s\n' "$fn" | grep -nF 'kill -KILL' | head -n 1 | cut -d: -f1)
  [ "$d" -lt "$st" ]
  [ "$st" -lt "$i" ]
  [ "$i" -lt "$k" ]
}

# A fabricated /proc and a stub systemctl for `deploy.sh procs` and `quiesce`.
_fake_runner() {
  F="$BATS_TEST_TMPDIR/fake"
  P="$F/proc"
  RH="$F/home"
  mkdir -p "$P/self/ns" "$RH" "$F/bin" "$F/cgroup"
  printf 'RUNNER_HOME=%s\nRUNNER_VERSION=2.336.0\nRUNNER_NAME=t\nRUNNER_LABELS=t\nRUNNER_GROUP=t\nRUNNER_ORG_URL=https://example.invalid\nHOOKS_VERSION=0.8.1\n' \
    "$RH" > "$F/runner.env"
  : > "$RH/.env"
  printf 'if [[ -z "$RUNNER_MANUALLY_TRAP_SIG" ]]; then run; else runWithManualTrap; fi\n' > "$RH/run.sh"
  ENV_MTIME=$(stat -c %Y "$RH/.env" 2>/dev/null || stat -f %m "$RH/.env")
  HZ=$(getconf CLK_TCK)
  BTIME=$((ENV_MTIME - 10000))
  printf 'cpu 0 0 0 0\nbtime %s\n' "$BTIME" > "$P/stat"
  ln -s 'user:[4026531837]' "$P/self/ns/user"
  UCG=/user.slice/user-1001.slice/user@1001.service/app.slice/actions-runner.service
  MAIN=0
  STOP_RC=0
}

# _fake_proc PID COMM UID SECONDS_AFTER_ENV CGROUP NNP USERNS
_fake_proc() {
  local d="$P/$1" ticks
  ticks=$(( (10000 + $4) * HZ ))
  mkdir -p "$d/ns"
  printf '%s\n' "$2" > "$d/comm"
  printf 'Name:\t%s\nUid:\t%s\t%s\t%s\t%s\nNoNewPrivs:\t%s\n' "$2" "$3" "$3" "$3" "$3" "$6" > "$d/status"
  printf '%s (%s) S 1 1 1 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 %s 0 0\n' "$1" "$2" "$ticks" > "$d/stat"
  printf '0::%s\n' "$5" > "$d/cgroup"
  ln -s "user:[$7]" "$d/ns/user"
}

_run_deploy() {
  cat > "$F/bin/systemctl" <<STUB
#!/usr/bin/env bash
case "\$*" in
  *--property=ControlGroup*) echo "${STUB_CG-$UCG}" ;;
  *--property=MainPID*) echo "$MAIN" ;;
  *" stop "*) exit $STOP_RC ;;
esac
STUB
  chmod +x "$F/bin/systemctl"
  run env PATH="$F/bin:$PATH" RUNNER_ENV_FILE="$F/runner.env" RUNNER_PROC_ROOT="$P" \
    RUNNER_CGROUP_ROOT="$F/cgroup" RUNNER_DRAIN_SECONDS=0 RUNNER_STOP_GRACE_SECONDS=0 \
    "$SH" "$@"
}

@test "gh-runner: procs sees the stacked Listeners that a MainPID check could not" {
  # The state found on both hosts on 2026-10-12: run.sh fresh from the last restart, the
  # Listener that holds the session dating from boot with its Worker, and two Listeners
  # from later restarts. The main process alone reads clean.
  _fake_runner
  local me
  me=$(id -u)
  MAIN=300
  _fake_proc 300 bash "$me" 60 "$UCG" 0 4026531837
  _fake_proc 1020 Runner.Listener "$me" -5000 "$UCG" 1 4026531837
  _fake_proc 1500 Runner.Worker "$me" 50 "$UCG" 1 4026531837
  _fake_proc 2001 Runner.Listener "$me" 30 "$UCG" 0 4026531837
  _fake_proc 2002 Runner.Listener "$me" 70 "$UCG" 0 4026531837
  _run_deploy procs
  [ "$status" -eq 0 ]
  assert_contains "$output" "main pid=300 start=$((ENV_MTIME + 60)) cgroup=$UCG NoNewPrivs=0 userns=host"
  assert_contains "$output" "listener pid=1020 start=$((ENV_MTIME - 5000)) cgroup=$UCG NoNewPrivs=1 userns=host"
  printf '%s\n' "$output" > "$BATS_TEST_TMPDIR/out"
  assert_grep -qxF 'listeners=3' "$BATS_TEST_TMPDIR/out"
  assert_grep -qxF 'workers=1' "$BATS_TEST_TMPDIR/out"
  assert_grep -qxF 'outside_cgroup=0' "$BATS_TEST_TMPDIR/out"
  assert_grep -qxF 'started_before_env=1' "$BATS_TEST_TMPDIR/out"
  assert_grep -qxF 'no_new_privs=2' "$BATS_TEST_TMPDIR/out"
  assert_grep -qxF 'private_userns=0' "$BATS_TEST_TMPDIR/out"
}

@test "gh-runner: procs reports a converged runner with every line the deploy requires" {
  _fake_runner
  local me line
  me=$(id -u)
  MAIN=300
  _fake_proc 300 bash "$me" 60 "$UCG" 0 4026531837
  _fake_proc 301 Runner.Listener "$me" 61 "$UCG" 0 4026531837
  _fake_proc 302 Runner.Worker "$me" 90 "$UCG" 0 4026531837
  # Another account's process of the same name is not this runner's.
  _fake_proc 400 Runner.Listener "$((me + 1))" -5000 /system.slice/other.service 1 4026531837
  _run_deploy procs
  [ "$status" -eq 0 ]
  printf '%s\n' "$output" > "$BATS_TEST_TMPDIR/out"
  # Every line of the playbook's _runner_converged list, read from the playbook.
  local PB="$BATS_TEST_DIRNAME/../playbooks/deploy-github-runner.yml"
  sed -n '/^    _runner_converged:$/,/^$/p' "$PB" | sed -n 's/^      - //p' > "$BATS_TEST_TMPDIR/want"
  [ "$(wc -l < "$BATS_TEST_TMPDIR/want")" -eq 6 ]
  while IFS= read -r line; do
    assert_grep -qxF "$line" "$BATS_TEST_TMPDIR/out"
  done < "$BATS_TEST_TMPDIR/want"
}

@test "gh-runner: procs flags a Listener outside the unit's cgroup, in a private namespace, or old" {
  _fake_runner
  local me
  me=$(id -u)
  _fake_proc 301 Runner.Listener "$me" 61 /user.slice/user-1001.slice/session-3.scope 0 4026532999
  _fake_proc 302 Runner.Listener "$me" -1 "$UCG" 0 4026531837
  _run_deploy procs
  [ "$status" -eq 0 ]
  printf '%s\n' "$output" > "$BATS_TEST_TMPDIR/out"
  assert_grep -qxF 'listeners=2' "$BATS_TEST_TMPDIR/out"
  assert_grep -qxF 'outside_cgroup=1' "$BATS_TEST_TMPDIR/out"
  assert_grep -qxF 'private_userns=1' "$BATS_TEST_TMPDIR/out"
  assert_grep -qxF 'started_before_env=1' "$BATS_TEST_TMPDIR/out"
  # And a run.sh that does not honour the trap variable is reported.
  : > "$RH/run.sh"
  _run_deploy procs
  printf '%s\n' "$output" > "$BATS_TEST_TMPDIR/out"
  assert_grep -qxF 'run_sh_traps=no' "$BATS_TEST_TMPDIR/out"
}

@test "gh-runner: quiesce on a first deploy finds nothing to stop, and a failed stop fails it" {
  _fake_runner
  STUB_CG='' STOP_RC=5 _run_deploy quiesce
  [ "$status" -eq 0 ]
  printf '%s\n' "$output" > "$BATS_TEST_TMPDIR/out"
  assert_grep -qxF 'drained=yes' "$BATS_TEST_TMPDIR/out"
  assert_grep -qxF 'signalled=0' "$BATS_TEST_TMPDIR/out"
  assert_grep -qxF 'remaining=0' "$BATS_TEST_TMPDIR/out"
  STOP_RC=1 _run_deploy quiesce
  [ "$status" -ne 0 ]
}

@test "gh-runner: quiesce with no cgroup found still signals the account's Listeners, and fails closed" {
  # A cgroup layout the search does not know must not let a leftover Listener through
  # (CodeRabbit on #563). The fabricated Listener is no real process, so it survives
  # SIGINT and SIGKILL, and quiesce must fail rather than report success.
  _fake_runner
  _fake_proc 999999 Runner.Listener "$(id -u)" -5000 "$UCG" 0 4026531837
  STUB_CG='' _run_deploy quiesce
  [ "$status" -ne 0 ]
  printf '%s\n' "$output" > "$BATS_TEST_TMPDIR/out"
  assert_grep -qxF 'signalled=1' "$BATS_TEST_TMPDIR/out"
  assert_grep -qxF 'remaining=1' "$BATS_TEST_TMPDIR/out"
  assert_grep -qF 'Listeners of this account still running after SIGKILL: 999999' "$BATS_TEST_TMPDIR/out"
}

@test "gh-runner: quiesce also stops a Listener of the account outside the unit's cgroup" {
  # procs counts it (outside_cgroup), so quiesce must stop it too, or every later deploy
  # fails its verify with no way to converge (CodeRabbit on #563).
  _fake_runner
  mkdir -p "$F/cgroup$UCG"
  : > "$F/cgroup$UCG/cgroup.procs"
  _fake_proc 999998 Runner.Listener "$(id -u)" -5000 /user.slice/user-1001.slice/session-3.scope 0 4026531837
  _run_deploy quiesce
  [ "$status" -ne 0 ]
  printf '%s\n' "$output" > "$BATS_TEST_TMPDIR/out"
  assert_grep -qxF 'signalled=1' "$BATS_TEST_TMPDIR/out"
  assert_grep -qxF 'remaining=1' "$BATS_TEST_TMPDIR/out"
  assert_grep -qF 'Listeners of this account still running after SIGKILL: 999998' "$BATS_TEST_TMPDIR/out"
}

@test "gh-runner: the deploy verifies the Listener after the start, not only the unit" {
  local PB="$BATS_TEST_DIRNAME/../playbooks/deploy-github-runner.yml"
  local block
  block=$(sed -n '/^    - name: "Require one Listener, in the unit.s cgroup, started after .env, with no no_new_privs"$/,/^    - name: /p' "$PB")
  [ -n "$block" ]
  assert_contains "$block" '- _runner_converged | difference(_procs.stdout_lines) | length == 0'
  assert_contains "$block" 'when: not ansible_check_mode'
  # The probe it judges is the one taken after the start.
  local probe
  probe=$(sed -n '/^    - name: "Read the runner.s Listener and Worker processes after the start"$/,/^    - name: /p' "$PB")
  assert_contains "$probe" 'ansible.builtin.command: "{{ _deploy_dir }}/deploy.sh procs"'
  assert_contains "$probe" 'register: _procs'
  local p v
  p=$(grep -nF '    - name: "Read the runner'"'"'s Listener and Worker processes after the start"' "$PB" | cut -d: -f1)
  v=$(grep -nF '    - name: "Reload the account'"'"'s systemd and start the runner"' "$PB" | cut -d: -f1)
  [ "$v" -lt "$p" ]
}
