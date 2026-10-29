#!/usr/bin/env bash
# hooks-cycle.sh: run the docker container hooks through one job's whole cycle
# (prepare_job, run_script_step, cleanup_job) twice, the way the runner calls them, and
# report whether a `container:` job would start. Runs as root inside the hooks-smoke
# image (Containerfile beside this file), started by run.sh. Never deployed.
#
# The two `docker` binaries the hooks can be given (design D4, docs/MISTAKES.md 10.29):
#   shim    a script that runs podman as a CLI, as podman-docker's /usr/bin/docker does.
#           Expected to FAIL in prepare_job with `cannot resolve /github/home`, the
#           failure seen on both runners: if it does not, this harness no longer
#           reproduces the bug and its pass below proves nothing.
#   client  Docker's CLI client (/usr/bin/docker from docker-ce-cli), with DOCKER_HOST at
#           the runner account's rootless podman API socket, as the runner's .env sets it.
#           Expected to PASS the whole cycle.
#
# Each hook call is made as the runner does it: as the runner account, `node index.js`
# with the hook input as ONE line on stdin (hooklib getInputFromStdin keeps only the last
# line), an existing response file, and the runner's own environment, which the hooks
# reduce to the job's variables plus DOCKER_* before they start `docker`. The job's
# variables carry HOME=/github/home, as the runner's do. The mount set is the runner's
# (actions/runner v2.336.0, ContainerOperationProvider.cs:301-318, ContainerInfo.cs:57-60).
#
# Exit status: 0 when the shim fails as expected AND the client passes; 1 otherwise.
set -euo pipefail

U=ghrunner
H=/opt/actions-runner
W="$H/_work"
UID_N=$(id -u "$U")
RT="/run/user/$UID_N"
SOCK="$RT/podman/podman.sock"
SYS_PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
IMAGE=${HOOKS_SMOKE_IMAGE:-docker.io/library/ubuntu:22.04}
LABEL_NAME=gh-runner-smoke

log() { printf '[hooks-smoke] %s\n' "$*"; }

# ── the host state a runner deploy leaves ───────────────────────────────────
# Subordinate ranges (useradd gives a new account none inside this image).
grep -q "^$U:" /etc/subuid || echo "$U:100000:65536" >> /etc/subuid
grep -q "^$U:" /etc/subgid || echo "$U:100000:65536" >> /etc/subgid
# This podman runs inside a container, whose root file system is overlayfs; the kernel
# overlay driver cannot stack on it, so podman needs fuse-overlayfs here. Not a host need.
mkdir -p "$H/.config/containers"
printf '[storage]\ndriver = "overlay"\n[storage.options.overlay]\nmount_program = "/usr/bin/fuse-overlayfs"\n' \
  > "$H/.config/containers/storage.conf"
# The bind sources job-started.sh makes, and a workspace.
mkdir -p "$W/_temp/_github_home" "$W/_temp/_github_workflow" "$W/_actions" "$W/_tool" \
  "$W/smoke/smoke" "$H/externals"
chown -R "$U:$U" "$H"
# The account's runtime directory and its podman API service. On a host these are the
# account's lingering user manager and its podman.socket unit
# (tasks/podman-user-docker-socket.yml); here there is no systemd, so the service is
# started directly, with the account's own environment.
mkdir -p "$RT/podman"
chown -R "$U:$U" "$RT"
chmod 0700 "$RT"
su "$U" -s /bin/bash -c \
  "XDG_RUNTIME_DIR=$RT nohup podman system service --time=0 unix://$SOCK >/tmp/podman-service.log 2>&1 &"
for _ in $(seq 60); do [ -S "$SOCK" ] && break; sleep 0.5; done
[ -S "$SOCK" ] || { log "podman API socket did not appear"; cat /tmp/podman-service.log; exit 1; }
# The Docker socket path leads to the account's socket (tasks/podman-user-docker-socket.yml).
ln -sfn "$SOCK" /run/docker.sock

# The shim: what podman-docker installs as /usr/bin/docker, minus its stderr banner.
SHIM_DIR=/opt/podman-docker-shim
mkdir -p "$SHIM_DIR"
printf '#!/bin/sh\nexec /usr/bin/podman "$@"\n' > "$SHIM_DIR/docker"
chmod 0755 "$SHIM_DIR/docker"

log "podman: $(podman --version); docker client: $(/usr/bin/docker --version)"

# ── one hook call, as the runner makes it ───────────────────────────────────
# $1 = hook PATH, $2 = extra runner variables (DOCKER_HOST), stdin = the hook input JSON.
hook() {
  local path=$1 extra=$2 input
  input=$(node -e 'let s="";process.stdin.on("data",d=>s+=d).on("end",()=>process.stdout.write(JSON.stringify(JSON.parse(s))))')
  # shellcheck disable=SC2086  # $extra is a list of NAME=value words, split on purpose
  printf '%s\n' "$input" | su "$U" -s /bin/bash -c "cd $H && exec env -i PATH=$path HOME=$H USER=$U \
    RUNNER_NAME=$LABEL_NAME RUNNER_TEMP=$W/_temp GITHUB_WORKSPACE=$W/smoke/smoke $extra \
    node $H/_hooks/index.js"
}

job_env='"HOME":"/github/home","GITHUB_ACTIONS":"true","CI":"true"'

# ── one job's cycle ────────────────────────────────────────────────────────
# $1 = name, $2 = hook PATH, $3 = extra runner variables. Prints RESULT lines.
cycle() {
  local name=$1 path=$2 extra=$3 resp="/tmp/response-$1.json" cid net
  : > "$resp"
  chmod 0666 "$resp"
  log "[$name] prepare_job"
  if ! hook "$path" "$extra" > "/tmp/prepare-$name.log" 2>&1 <<JSON
{"command":"prepare_job","responseFile":"$resp","state":{},"args":{"container":{
 "image":"$IMAGE","workingDirectory":"/__w/smoke/smoke","createOptions":"",
 "environmentVariables":{$job_env},"userMountVolumes":[],
 "systemMountVolumes":[
  {"sourceVolumePath":"/var/run/docker.sock","targetVolumePath":"/var/run/docker.sock","readOnly":false},
  {"sourceVolumePath":"$W","targetVolumePath":"/__w","readOnly":false},
  {"sourceVolumePath":"$H/externals","targetVolumePath":"/__e","readOnly":true},
  {"sourceVolumePath":"$W/_temp","targetVolumePath":"/__w/_temp","readOnly":false},
  {"sourceVolumePath":"$W/_actions","targetVolumePath":"/__w/_actions","readOnly":false},
  {"sourceVolumePath":"$W/_tool","targetVolumePath":"/__w/_tool","readOnly":false},
  {"sourceVolumePath":"$W/_temp/_github_home","targetVolumePath":"/github/home","readOnly":false},
  {"sourceVolumePath":"$W/_temp/_github_workflow","targetVolumePath":"/github/workflow","readOnly":false}],
 "registry":null,"portMappings":[]},"services":[]}}
JSON
  then
    sed 's/^/    /' "/tmp/prepare-$name.log"
    echo "RESULT $name prepare_job=failed"
    return 0
  fi
  echo "RESULT $name prepare_job=ok"
  cid=$(node -e 'console.log(JSON.parse(require("fs").readFileSync(process.argv[1],"utf8")).state.container)' "$resp")
  net=$(node -e 'console.log(JSON.parse(require("fs").readFileSync(process.argv[1],"utf8")).state.network)' "$resp")

  # The step: reports who and where it runs, and writes a marker into the workspace.
  cat > "$W/_temp/smoke-step.sh" <<'STEP'
echo "step home=$HOME"
echo "step image=$(. /etc/os-release && echo "$ID-$VERSION_ID")"
test -S /var/run/docker.sock && echo "step docker_sock=present"
echo marker > /__w/smoke/smoke/marker
STEP
  chown "$U:$U" "$W/_temp/smoke-step.sh"
  rm -f "$W/smoke/smoke/marker"
  log "[$name] run_script_step"
  if hook "$path" "$extra" > "/tmp/step-$name.log" 2>&1 <<JSON
{"command":"run_script_step","responseFile":null,"state":{"network":"$net","container":"$cid"},
 "args":{"entryPoint":"bash","entryPointArgs":["-e","/__w/_temp/smoke-step.sh"],
 "environmentVariables":{$job_env},"prependPath":[],"workingDirectory":"/__w/smoke/smoke"}}
JSON
  then
    echo "RESULT $name run_script_step=ok"
  else
    echo "RESULT $name run_script_step=failed"
  fi
  sed -n 's/^\(step .*\)$/    \1/p' "/tmp/step-$name.log"
  if grep -qx 'step home=/github/home' "/tmp/step-$name.log"; then echo "RESULT $name step_home=job"; fi
  if [ -f "$W/smoke/smoke/marker" ]; then echo "RESULT $name workspace_mount=ok"; fi

  log "[$name] cleanup_job"
  if hook "$path" "$extra" > "/tmp/cleanup-$name.log" 2>&1 <<JSON
{"command":"cleanup_job","responseFile":null,"state":{"network":"$net","container":"$cid"},"args":{}}
JSON
  then
    echo "RESULT $name cleanup_job=ok"
  else
    echo "RESULT $name cleanup_job=failed"
  fi
  local left
  left=$(su "$U" -s /bin/bash -c "XDG_RUNTIME_DIR=$RT podman ps -aq" | wc -l)
  echo "RESULT $name containers_left=$((left))"
}

out=/tmp/hooks-smoke.results
{
  cycle shim "$SHIM_DIR:$SYS_PATH" ""
  cycle client "$SYS_PATH" "DOCKER_HOST=unix://$SOCK"
} | tee "$out"

# ── verdict ────────────────────────────────────────────────────────────────
fail=0
want() { grep -qxF "$1" "$out" || { log "MISSING: $1"; fail=1; }; }
want 'RESULT shim prepare_job=failed'
if ! grep -qF 'cannot resolve /github/home' /tmp/prepare-shim.log; then
  log "MISSING: the shim's failure is not 'cannot resolve /github/home'; the harness no longer reproduces the bug"
  fail=1
fi
for r in prepare_job=ok run_script_step=ok step_home=job workspace_mount=ok cleanup_job=ok containers_left=0; do
  want "RESULT client $r"
done
if [ "$fail" -eq 0 ]; then
  log "PASS: the shim fails as on the runners; Docker's CLI client against the account's podman API runs the whole job cycle."
else
  log "FAIL"
fi
exit "$fail"
