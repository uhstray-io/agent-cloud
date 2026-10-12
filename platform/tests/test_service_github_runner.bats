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
  assert_grep -qF 'systemctl --user show --property=MainPID --value actions-runner.service' "$PB"
  assert_grep -qF '"/proc/$pid/status"' "$PB"
  assert_grep -qF "'NoNewPrivs=0' not in _runner_privs.stdout_lines" "$PB"
  assert_grep -qF "'userns=host' not in _runner_privs.stdout_lines" "$PB"
  # A changed unit must reach the running process, which keeps the properties it was
  # started with, so the deploy restarts the service when the unit file changed.
  assert_grep -qF "if (_runner_unit.changed | default(false))" "$PB"
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
  assert_grep -qF "else 'started' }}" "$PB"
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
