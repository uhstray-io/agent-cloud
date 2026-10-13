#!/usr/bin/env bats
# Behavioural tests for the NetBox change-aware deploy (change service-deployment-workflow,
# task 7.1, wave 3): platform/services/netbox/deployment/deploy.sh + lib/common.sh.
#
# NetBox keeps its own compose wrapper and library, so it does not go through
# platform/lib/common.sh compose_up_if_changed and test_compose_up_if_changed.bats does not
# cover it. The real deploy.sh runs here against a stub container engine that keeps every
# container's state in files (service, status, exit code, inputs label, image) and logs every
# call, plus stubs for `sudo` (the orb-agent stop) and `sleep`. netbox-docker is a real clone of
# a local bare repo, so step 1 runs unmodified and offline.
#
# What is proven: an unchanged re-run builds nothing, stops nothing, removes nothing and says
# DEPLOY_CHANGED=false; every input that decides what the containers run on, and an image
# or container mismatch, runs the full sequence and says DEPLOY_CHANGED=true.
# Run: bats platform/tests/test_netbox_change_aware.bats

load assert_helpers

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_PREFIX GIT_OBJECT_DIRECTORY GIT_COMMON_DIR
  T="$BATS_TEST_TMPDIR/tree"
  D="$T/deployment"
  S="$BATS_TEST_TMPDIR/state"
  SRC="$REPO_ROOT/platform/services/netbox/deployment"
  mkdir -p "$T/bin" "$S/c" "$D/env" "$D/secrets"
  local f
  for f in docker-compose.yml docker-compose.local-auth.yml Dockerfile-Plugins deploy.sh post-deploy.sh; do
    cp "$SRC/$f" "$D/$f"
  done
  cp -R "$SRC/lib" "$SRC/configuration" "$SRC/discovery" "$D/"
  printf 'SUPERUSER_PASSWORD=one\nORB_AGENT_CLIENT_ID=id-1\nORB_AGENT_CLIENT_SECRET=sec-1\nSNMP_COMMUNITY=public\n' > "$D/.env"
  for f in netbox postgres discovery; do printf 'KEY=v1\n' > "$D/env/$f.env"; done
  printf 'dsn: x\n' > "$D/discovery/hydra.yaml"
  printf 'client-secret\n' > "$D/secrets/netbox_to_diode_client_secret.txt"

  # netbox-docker: a genuine clone of a local origin carrying branch `release`.
  git init -q --bare "$T/up.git"
  git -C "$T/up.git" symbolic-ref HEAD refs/heads/release
  git init -q "$T/upwork"
  git -C "$T/upwork" checkout -q -b release
  mkdir -p "$T/upwork/configuration"; echo 'x = 1' > "$T/upwork/configuration/configuration.py"
  git -C "$T/upwork" add -A
  git -C "$T/upwork" -c user.name=t -c user.email=t@example.test -c commit.gpgsign=false commit -qm init
  git -C "$T/upwork" push -q "$T/up.git" release
  git clone -q --branch release "$T/up.git" "$D/netbox-docker"
  # deploy.sh clones and fetches the public netbox-docker URL: send it to the local origin so
  # no test reaches the network (a fresh clone records the public URL, so every run needs this)
  export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0="url.$T/up.git.insteadOf"
  export GIT_CONFIG_VALUE_0="https://github.com/netbox-community/netbox-docker.git"

  echo 0 > "$S/builds"
  echo 0 > "$S/ups"
  : > "$S/calls.log"

  cat > "$T/bin/sleep" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB
  cat > "$T/bin/sudo" <<'STUB'
#!/usr/bin/env bash
echo "sudo $*" >> "$STUB_STATE/calls.log"
exit 0
STUB
  cat > "$T/bin/engine" <<'STUB'
#!/usr/bin/env bash
S="$STUB_STATE"
echo "engine $*" >> "$S/calls.log"
ref_of() { case "$1" in netbox|netbox-worker) echo netbox:latest-plugins ;; *) echo other:v1 ;; esac; }
mkc() {  # <service> <label>
  local n="netbox-$1-1" c="$S/c/netbox-$1-1"
  rm -rf "$c"; mkdir -p "$c"
  echo "$1" > "$c/service"; echo "$2" > "$c/label"; ref_of "$1" > "$c/ref"
  if [ "$1" = hydra-migrate ]; then echo exited > "$c/status"; echo 0 > "$c/exit"; else echo running > "$c/status"; echo 0 > "$c/exit"; fi
  if [ "$(cat "$c/ref")" = netbox:latest-plugins ]; then cp "$S/image_id" "$c/image_run"; else echo static-id > "$c/image_run"; fi
  : "$n"
}
case "$1" in
  image)
    [ "$2" = inspect ] || exit 0
    case "$4" in
      *Labels*) [ -f "$S/image_id" ] || exit 1; cat "$S/image_label" 2>/dev/null; exit 0 ;;
      *Id*) if [ "$5" = netbox:latest-plugins ]; then [ -f "$S/image_id" ] || exit 1; cat "$S/image_id"; else echo static-id; fi ;;
    esac ;;
  build)
    echo "build $*" >> "$S/builds.log"
    for a in "$@"; do case "$a" in io.agent-cloud.build-inputs-sha256=*) echo "${a#*=}" > "$S/image_label" ;; esac; done
    n=$(( $(cat "$S/builds") + 1 )); echo "$n" > "$S/builds"; echo "img-b$n" > "$S/image_id" ;;
  ps)
    case "$*" in
      *service=*) all="$*"; svc="${all##*service=}"; svc="${svc%% *}"
        for d in "$S"/c/*; do [ -d "$d" ] && [ "$(cat "$d/service")" = "$svc" ] && basename "$d"; done ;;
      *) for d in "$S"/c/*; do [ -d "$d" ] && basename "$d"; done ;;
    esac; exit 0 ;;
  inspect)
    fmt="$3" name="$4"; c="$S/c/$name"; [ -d "$c" ] || exit 1
    case "$fmt" in
      *compose.service*) cat "$c/service" ;;
      *inputs-sha256*) cat "$c/label" ;;
      *State.Status*) cat "$c/status" ;;
      *State.ExitCode*) cat "$c/exit" ;;
      *Config.Image*) cat "$c/ref" ;;
      '{{.Image}}') cat "$c/image_run" ;;
    esac; exit 0 ;;
  volume) exit 1 ;;
  compose)
    shift; files=()
    while [ $# -gt 0 ]; do
      case "$1" in
        --project-name) shift 2 ;;
        -f) files+=("$2"); shift 2 ;;
        *) break ;;
      esac
    done
    sub="$1"; shift
    case "$sub" in
      config)
        for f in "${files[@]}"; do
          awk '/^services:/ {s=1; next} s && /^[^ #]/ {s=0} s && /^  [a-z0-9-]+:[[:space:]]*$/ {sub(/^  /,""); sub(/:.*/,""); print}' "$f"
        done | awk '!seen[$0]++' ;;
      down) echo "compose down" >> "$S/calls.log"; for d in "$S"/c/*; do [ -d "$d" ] && rm -rf "$d"; done ;;
      up)
        label=""
        for f in "${files[@]}"; do
          case "$f" in */.deploy-inputs-label.yml) label=$(sed -n 's/^ *io\.agent-cloud\.inputs-sha256: "\(.*\)"$/\1/p' "$f" | sort -u) ;; esac
        done
        echo "compose up label=$label $*" >> "$S/calls.log"
        echo $(( $(cat "$S/ups") + 1 )) > "$S/ups"
        named=()
        for a in "$@"; do case "$a" in -*) ;; *) named+=("$a") ;; esac; done
        if [ "${#named[@]}" -eq 0 ]; then
          for f in "${files[@]}"; do
            awk '/^services:/ {s=1; next} s && /^[^ #]/ {s=0} s && /^  [a-z0-9-]+:[[:space:]]*$/ {sub(/^  /,""); sub(/:.*/,""); print}' "$f"
          done | awk '!seen[$0]++' > "$S/all"
          while IFS= read -r svc; do named+=("$svc"); done < "$S/all"
        fi
        for svc in "${named[@]}"; do
          c="$S/c/netbox-$svc-1"
          # compose keeps a container whose config (label included) is unchanged
          if [ -d "$c" ] && [ "$(cat "$c/label")" = "$label" ]; then continue; fi
          mkc "$svc" "$label"
        done ;;
      ps) printf '[{"Service":"netbox","Name":"netbox-netbox-1","Status":"Up 1 minute (healthy)"}]\n' ;;
    esac
    exit 0 ;;
  *) exit 0 ;;
esac
STUB
  chmod +x "$T/bin/sleep" "$T/bin/sudo" "$T/bin/engine"
}

deploy() {
  run env STUB_STATE="$S" CONTAINER_ENGINE="$T/bin/engine" PATH="$T/bin:$PATH" \
    NETBOX_HOST_IP="${HOSTIP:-}" FORCE_RECREATE="${FORCE:-}" NETBOX_FORCE_REBUILD="${REBUILD:-}" \
    bash "$D/deploy.sh" --no-pull
}
changed() { printf '%s\n' "${lines[@]}" | grep -x "DEPLOY_CHANGED=$1"; }
builds() { cat "$S/builds"; }
ups() { cat "$S/ups"; }

@test "first deploy builds, runs the full sequence and labels every container with a bare digest" {
  deploy
  [ "$status" -eq 0 ]; changed true
  [ "$(builds)" -eq 1 ]
  assert_grep -qF -- '--no-cache' "$S/builds.log"
  assert_grep -qE -- '--label io\.agent-cloud\.build-inputs-sha256=[0-9a-f]{64}' "$S/builds.log"
  assert_grep -qx 'compose down' "$S/calls.log"
  assert_grep -q '^sudo .* stop netbox-orb-agent' "$S/calls.log"
  local d n=0
  for d in "$S"/c/*; do
    grep -qE '^[0-9a-f]{64}$' "$d/label"
    n=$((n + 1))
  done
  [ "$n" -ge 12 ]
  # the label overlay is on every `compose up` of the sequence, never only some of them
  grep '^compose up' "$S/calls.log" | while IFS= read -r l; do
    [[ "$l" =~ label=[0-9a-f]{64}\  ]]
  done
  [ ! -e "$D/.deploy-inputs-label.yml" ]
}

@test "an unchanged re-run is a true no-op: no build, no down, no up, no orb-agent stop" {
  deploy; [ "$status" -eq 0 ]
  : > "$S/calls.log"
  deploy
  [ "$status" -eq 0 ]; changed false
  [ "$(builds)" -eq 1 ]
  refute_grep -qE 'compose down|compose up|^engine build|^sudo|\brm\b|\bstop\b' "$S/calls.log"
  printf '%s\n' "${lines[@]}" | refute_grep -q 'Stopping services'
}

@test "the credential refresh's own writes to .env do not make the next run a change" {
  deploy; [ "$status" -eq 0 ]
  sed -i.bak -e 's/^ORB_AGENT_CLIENT_ID=.*/ORB_AGENT_CLIENT_ID=id-2/' \
    -e 's/^ORB_AGENT_CLIENT_SECRET=.*/ORB_AGENT_CLIENT_SECRET=sec-2/' \
    -e 's/^SNMP_COMMUNITY=.*/SNMP_COMMUNITY=other/' "$D/.env"
  deploy
  [ "$status" -eq 0 ]; changed false
}

@test "every input that decides what the containers run on triggers the full sequence" {
  deploy; [ "$status" -eq 0 ]
  local f
  for f in .env env/netbox.env env/postgres.env env/discovery.env docker-compose.yml \
    discovery/hydra.yaml discovery/nginx.conf discovery/init-db.sh \
    secrets/netbox_to_diode_client_secret.txt netbox-docker/configuration/configuration.py; do
    echo "# changed $f" >> "$D/$f"
    : > "$S/calls.log"
    deploy
    [ "$status" -eq 0 ] || { echo "$f: $output"; return 1; }
    changed true || { echo "$f did not register as a change"; return 1; }
    assert_grep -qx 'compose down' "$S/calls.log"
    assert_grep -q '^sudo .* stop netbox-orb-agent' "$S/calls.log"
    deploy
    changed false || { echo "$f: the run after was not stable"; return 1; }
  done
}

@test "a changed override file or the published address triggers the sequence" {
  deploy; [ "$status" -eq 0 ]
  HOSTIP=127.0.0.1 deploy
  [ "$status" -eq 0 ]; changed true
  HOSTIP=127.0.0.1 deploy; changed false
  # an overlay named in COMPOSE_OVERLAYS is an input too
  run env STUB_STATE="$S" \
    CONTAINER_ENGINE="$T/bin/engine" PATH="$T/bin:$PATH" NETBOX_HOST_IP=127.0.0.1 \
    COMPOSE_OVERLAYS=docker-compose.local-auth.yml bash "$D/deploy.sh" --no-pull
  [ "$status" -eq 0 ]; changed true
}

@test "build inputs: an unchanged image is not rebuilt, a changed Dockerfile or plugins.py is" {
  deploy; [ "$status" -eq 0 ]
  [ "$(builds)" -eq 1 ]
  echo "# x" >> "$D/Dockerfile-Plugins"
  deploy; [ "$status" -eq 0 ]; changed true
  [ "$(builds)" -eq 2 ]
  echo "# x" >> "$D/configuration/plugins.py"
  deploy; [ "$status" -eq 0 ]; changed true
  [ "$(builds)" -eq 3 ]
  deploy; changed false
  [ "$(builds)" -eq 3 ]
}

@test "a missing image is built, and the rebuilt image recreates the containers" {
  deploy; [ "$status" -eq 0 ]
  rm -f "$S/image_id" "$S/image_label"
  deploy
  [ "$status" -eq 0 ]; changed true
  [ "$(builds)" -eq 2 ]
}

@test "NETBOX_FORCE_REBUILD=true rebuilds with unchanged inputs; any other value does not" {
  deploy; [ "$status" -eq 0 ]
  local v
  for v in false yes 1 True ""; do
    REBUILD=$v deploy
    [ "$status" -eq 0 ]; changed false
    [ "$(builds)" -eq 1 ]
  done
  REBUILD=true deploy
  [ "$status" -eq 0 ]; changed true
  [ "$(builds)" -eq 2 ]
  assert_grep -qF -- '--no-cache' "$S/builds.log"
}

@test "FORCE_RECREATE=true recreates with unchanged inputs; any other value does not" {
  deploy; [ "$status" -eq 0 ]
  local v
  for v in false yes 1 True; do
    FORCE=$v deploy
    [ "$status" -eq 0 ]; changed false
  done
  FORCE=true deploy
  [ "$status" -eq 0 ]; changed true
  printf '%s\n' "${lines[@]}" | grep -q 'FORCE_RECREATE=true'
  [ "$(builds)" -eq 1 ]
  deploy; changed false
}

@test "a stopped, missing, orphaned, mislabelled or re-pulled container triggers the sequence" {
  deploy; [ "$status" -eq 0 ]
  echo exited > "$S/c/netbox-netbox-worker-1/status"
  deploy; [ "$status" -eq 0 ]; changed true
  deploy; changed false
  rm -rf "$S/c/netbox-redis-1"
  deploy; [ "$status" -eq 0 ]; changed true
  deploy; changed false
  mkdir -p "$S/c/netbox-gone-1"; echo gone > "$S/c/netbox-gone-1/service"
  deploy; [ "$status" -eq 0 ]; changed true
  deploy; changed false
  echo stale > "$S/c/netbox-postgres-1/label"
  deploy; [ "$status" -eq 0 ]; changed true
  deploy; changed false
  echo img-pulled > "$S/image_id"
  deploy; [ "$status" -eq 0 ]; changed true
}

@test "the one-shot migration exiting 0 is healthy, exiting non-zero is not" {
  deploy; [ "$status" -eq 0 ]
  deploy; changed false
  echo 1 > "$S/c/netbox-hydra-migrate-1/exit"
  deploy; [ "$status" -eq 0 ]; changed true
}

@test "a changed run owes post-deploy again; an unchanged one leaves the marker alone" {
  deploy; [ "$status" -eq 0 ]
  touch "$D/.post-deploy-complete"
  deploy; [ "$status" -eq 0 ]; changed false
  [ -e "$D/.post-deploy-complete" ]
  echo "# x" >> "$D/env/netbox.env"
  deploy; [ "$status" -eq 0 ]; changed true
  [ ! -e "$D/.post-deploy-complete" ]
}

@test "post-deploy.sh records completion only after its last step" {
  local code
  code=$(grep -vE '^\s*#' "$SRC/post-deploy.sh")
  [ "$(printf '%s\n' "$code" | grep -c 'touch "\${SCRIPT_DIR}/\${NETBOX_POST_DEPLOY_MARKER}"')" -eq 1 ]
  assert_precedes "$SRC/post-deploy.sh" '^verify_services ' '^touch '
}

@test "deploy.sh forces a recreate when it replaces the netbox-docker clone it bind-mounts" {
  deploy; [ "$status" -eq 0 ]
  deploy; changed false
  # a copy without .git is moved aside and cloned fresh (from the local origin, through git's
  # insteadOf, so this stays offline); the running containers still hold the old directory
  rm -rf "$D/netbox-docker/.git"
  deploy
  [ "$status" -eq 0 ]; changed true
  printf '%s\n' "${lines[@]}" | grep -q 'netbox-docker clone was replaced'
  [ -d "$D/netbox-docker/.git" ]
  ls -d "$D"/netbox-docker.stale-* > /dev/null
  deploy; changed false
}

@test "an unreadable input refuses rather than guessing" {
  [ "$(id -u)" -ne 0 ] || skip "root reads a mode-000 file"
  deploy; [ "$status" -eq 0 ]
  chmod 000 "$D/discovery/nginx.conf"
  deploy
  chmod 644 "$D/discovery/nginx.conf"
  [ "$status" -ne 0 ]
  printf '%s\n' "${lines[@]}" | grep -q 'refusing to guess'
}

@test "the build and the digest only ever print digests, never file content" {
  deploy; [ "$status" -eq 0 ]
  printf '%s\n' "${lines[@]}" | refute_grep -qE 'client-secret|sec-1|id-1'
  refute_grep -qE 'client-secret|sec-1' "$S/calls.log"
}
