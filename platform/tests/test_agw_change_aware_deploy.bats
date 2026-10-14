#!/usr/bin/env bats
# Behavioural tests for agentgateway's change-aware deploy (gateway task 1.12, design
# decision 11): deploy.sh recreates the gateway only when what it runs on differs from what
# the running container started with, so a re-run with nothing changed leaves in-flight
# streams alone.
#
# deploy.sh runs for real against a stub container engine and a stub compose provider. The
# stubs keep the gateway's state in files (exists, running, its inputs label, its image) and
# the db's image, and list both containers only for the project's working-directory label.
# The compose stub plays `up -d --force-recreate`: it records the label carried by the
# generated overlay, the way a real recreate starts a container with it, moves BOTH
# containers to the image their tag names now, and the recreated stub container answers
# readiness.
# Run: bats platform/tests/test_agw_change_aware_deploy.bats

load assert_helpers

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  T="$BATS_TEST_TMPDIR/tree"
  D="$T/platform/services/agentgateway/deployment"
  S="$BATS_TEST_TMPDIR/state"
  mkdir -p "$D/certs/agw-server/serial-1" "$T/platform/lib" "$T/bin" "$S"
  cp "$REPO_ROOT/platform/lib/common.sh" "$T/platform/lib/"
  cp "$REPO_ROOT/platform/services/agentgateway/deployment/deploy.sh" "$D/"
  cp "$REPO_ROOT/platform/services/agentgateway/deployment/compose.yml" \
     "$REPO_ROOT/platform/services/agentgateway/deployment/compose.tls.yml" "$D/"
  printf 'AGW_IMAGE=cr.agentgateway.dev/agentgateway:v1.5.0\nVLLM_API_KEY=one\n' > "$D/.env"
  printf 'binds: []\n' > "$D/config.yaml"
  printf 'leaf-1\n' > "$D/certs/agw-server/serial-1/cert.pem"
  ln -s serial-1 "$D/certs/agw-server/current"
  printf '#!/usr/bin/env bash\necho 192.0.2.7\n' > "$D/gateway-addr.sh"
  chmod +x "$D/gateway-addr.sh" "$D/deploy.sh"
  echo img-1 > "$S/image_now"
  echo db-1 > "$S/db_image_now"; echo db-1 > "$S/db_image_run"
  echo 0 > "$S/recreates"

  cat > "$T/bin/engine" <<'STUB'
#!/usr/bin/env bash
S="$STUB_STATE"
printf '%s\n' "$*" >> "$S/engine.log"
if [ "$1" = image ] && [ "$2" = inspect ]; then
  case "${!#}" in *postgres*) cat "$S/db_image_now" ;; *) cat "$S/image_now" ;; esac; exit 0
fi
if [ "$1" = exec ]; then [ -f "$S/not_ready" ] && exit 1; exit 0; fi
if [ "$1" = ps ]; then
  # Only the project's own selector lists anything.
  [ "$*" = "ps -a --filter label=com.docker.compose.project.working_dir=$STUB_DIR --format {{.Names}}" ] || exit 0
  [ -f "$S/exists" ] && echo agentgateway
  echo agentgateway-db; exit 0
fi
if [ "$1" = inspect ]; then
  # Both spellings: deploy.sh passes `--format FMT`, common.sh `--format=FMT`.
  if [ "$2" = --format ]; then fmt="$3" name="$4"; else fmt="${2#--format=}" name="$3"; fi
  if [ "$name" = agentgateway-db ]; then
    case "$fmt" in
      *Config.Image*) echo docker.example/library/postgres:16-alpine ;;
      *'.Image}}'*) cat "$S/db_image_run" ;;
      *) echo healthy ;;
    esac
    exit 0
  fi
  [ -f "$S/exists" ] || exit 1
  case "$fmt" in
    *State.Running*) cat "$S/running" ;;
    *Labels*) cat "$S/label" 2>/dev/null || echo "<no value>" ;;
    *Config.Image*) echo cr.example/agentgateway:v1 ;;
    *'.Image}}'*) cat "$S/image_run" ;;
  esac
  exit 0
fi
exit 0
STUB

  cat > "$T/bin/compose" <<'STUB'
#!/usr/bin/env bash
S="$STUB_STATE"
echo "$*" >> "$S/compose.log"
echo "${AGW_IMAGE:-<unset>}" >> "$S/compose-images.log"
case " $* " in *" up "*) ;; *) exit 0 ;; esac
[ -f "$S/compose_fail" ] && exit 1
prev="" overlay=""
for a in "$@"; do
  [ "$prev" = -f ] && case "$a" in *inputs-label.yml) overlay="$a" ;; esac
  prev="$a"
done
case " $* " in *" --force-recreate "*) ;; *) exit 0 ;; esac
[ -n "$overlay" ] || { echo "recreate without the label overlay" >&2; exit 1; }
sed -n 's/^ *io\.agent-cloud\.inputs-sha256: "\(.*\)"$/\1/p' "$overlay" > "$S/label"
touch "$S/exists"; echo true > "$S/running"; cp "$S/image_now" "$S/image_run"; cp "$S/db_image_now" "$S/db_image_run"; rm -f "$S/not_ready"
echo $(( $(cat "$S/recreates") + 1 )) > "$S/recreates"
STUB
  chmod +x "$T/bin/engine" "$T/bin/compose"
}

deploy() {
  run env STUB_STATE="$S" STUB_DIR="${STUB_DIR:-$D}" CONTAINER_ENGINE="$T/bin/engine" COMPOSE_CMD="$T/bin/compose" \
    LOCAL_MODE="" COMPOSE_OVERLAYS="${OVERLAYS:-}" "$D/deploy.sh" --no-pull
}

pull_only() {
  run env STUB_STATE="$S" STUB_DIR="${STUB_DIR:-$D}" CONTAINER_ENGINE="$T/bin/engine" COMPOSE_CMD="$T/bin/compose" \
    LOCAL_MODE="" COMPOSE_OVERLAYS="${OVERLAYS:-}" "$D/deploy.sh" --pull-only
}

verify_only() {
  run env STUB_STATE="$S" STUB_DIR="${STUB_DIR:-$D}" CONTAINER_ENGINE="$T/bin/engine" COMPOSE_CMD="$T/bin/compose" \
    LOCAL_MODE="" COMPOSE_OVERLAYS="${OVERLAYS:-}" "$D/deploy.sh" --verify-only
}

assert_verify_engine_calls_read_only() {
  local call
  while IFS= read -r call; do
    # step_decide reads the project container list and each image ID as well as
    # inspecting containers and probing readiness; no lifecycle command is allowed.
    case "$call" in
      inspect*|exec*|ps*|image\ inspect*) ;;
      *) echo "Unexpected engine command during verify-only: $call" >&2; return 1 ;;
    esac
  done < "$S/engine.log"
}

recreates() { cat "$S/recreates"; }
# The run's last output line (bash 3.2 has no negative array index).
last() { printf '%s' "${lines[${#lines[@]}-1]}"; }

@test "agw deploy: pull-only prepares compose images without changing containers" {
  run env STUB_STATE="$S" STUB_DIR="$D" CONTAINER_ENGINE="$T/bin/engine" COMPOSE_CMD="$T/bin/compose" \
    AGW_IMAGE=cr.example/attacker:9 LOCAL_MODE="" "$D/deploy.sh" --pull-only
  [ "$status" -eq 0 ]
  [ "$(last)" = "image-pull-result: complete" ]
  assert_grep -q ' pull$' "$S/compose.log"
  [ "$(cat "$S/compose-images.log")" = "cr.agentgateway.dev/agentgateway:v1.5.0" ]
  refute_grep -q ' up ' "$S/compose.log"
  refute_contains "$output" "Step 3: Comparing"
  [ "$(recreates)" -eq 0 ]
}

@test "agw deploy: lifecycle ignores an inherited AGW_IMAGE override" {
  run env STUB_STATE="$S" STUB_DIR="$D" CONTAINER_ENGINE="$T/bin/engine" COMPOSE_CMD="$T/bin/compose" \
    AGW_IMAGE=cr.example/attacker:9 LOCAL_MODE="" "$D/deploy.sh" --no-pull
  [ "$status" -eq 0 ]
  [ "$(last)" = "deploy-result: recreated (no gateway container)" ]
  [ "$(cat "$S/compose-images.log")" = "cr.agentgateway.dev/agentgateway:v1.5.0" ]
  assert_grep -q ' up -d --force-recreate' "$S/compose.log"
}

@test "agw deploy: pull-only and no-pull cannot be combined" {
  run env STUB_STATE="$S" STUB_DIR="$D" CONTAINER_ENGINE="$T/bin/engine" COMPOSE_CMD="$T/bin/compose" \
    "$D/deploy.sh" --pull-only --no-pull
  [ "$status" -ne 0 ]
  assert_contains "$output" "cannot be combined"
  [ ! -f "$S/compose.log" ]
}

@test "agw deploy: verify-only is read-only and refuses inputs that differ from the running label" {
  deploy
  [ "$status" -eq 0 ]
  : > "$S/compose.log"
  : > "$S/engine.log"
  verify_only
  [ "$status" -eq 0 ]
  assert_verify_engine_calls_read_only
  assert_contains "$output" "runtime-inputs-result: pass"
  [ ! -s "$S/compose.log" ]
  [ "$(recreates)" -eq 1 ]

  printf '# changed after start\n' >> "$D/config.yaml"
  : > "$S/engine.log"
  verify_only
  [ "$status" -ne 0 ]
  assert_verify_engine_calls_read_only
  assert_contains "$output" "runtime-inputs-result: refused"
  assert_contains "$output" "inputs changed"
  [ ! -s "$S/compose.log" ]
  [ "$(recreates)" -eq 1 ]
}

@test "agw deploy: verify-only refuses a running gateway that does not answer readiness" {
  deploy
  [ "$status" -eq 0 ]
  : > "$S/compose.log"
  : > "$S/engine.log"
  touch "$S/not_ready"
  verify_only
  [ "$status" -ne 0 ]
  assert_verify_engine_calls_read_only
  assert_contains "$output" "runtime-inputs-result: refused"
  assert_contains "$output" "readiness not answering"
  [ ! -s "$S/compose.log" ]
  [ "$(recreates)" -eq 1 ]
}

@test "agw change-aware: first deploy creates the gateway and labels it with its inputs" {
  deploy
  [ "$status" -eq 0 ]
  [ "$(last)" = "deploy-result: recreated (no gateway container)" ]
  [ "$(recreates)" -eq 1 ]
  # The label is a bare sha256 digest, never file content.
  grep -qE '^[0-9a-f]{64}$' "$S/label"
  refute_grep -q 'VLLM_API_KEY' "$S/label"
}

@test "agw change-aware: a re-run with nothing changed leaves the gateway alone" {
  deploy; [ "$status" -eq 0 ]
  : > "$S/compose.log"
  deploy
  [ "$status" -eq 0 ]
  [ "$(last)" = "deploy-result: unchanged" ]
  [ "$(recreates)" -eq 1 ]
  # No `compose up` at all: nothing is restarted, created or started.
  refute_grep -q ' up ' "$S/compose.log"
}

@test "agw change-aware: a changed config.yaml, .env, compose file or certificate recreates" {
  local n=1 f
  deploy; [ "$status" -eq 0 ]
  for f in config.yaml .env compose.yml certs/agw-server/serial-1/cert.pem; do
    echo "# changed" >> "$D/$f"
    deploy
    [ "$status" -eq 0 ]
    [ "$(last)" = "deploy-result: recreated (inputs changed)" ]
    n=$((n + 1)); [ "$(recreates)" -eq "$n" ]
    deploy
    [ "$(last)" = "deploy-result: unchanged" ]
  done
}

@test "agw change-aware: a renewal that swaps certs/<leaf>/current recreates" {
  deploy; [ "$status" -eq 0 ]
  mkdir "$D/certs/agw-server/serial-2"; printf 'leaf-2\n' > "$D/certs/agw-server/serial-2/cert.pem"
  ln -sfn serial-2 "$D/certs/agw-server/current"
  deploy
  [ "$(last)" = "deploy-result: recreated (inputs changed)" ]
}

@test "agw change-aware: turning on the listener-TLS overlay recreates, and the overlay stays applied" {
  deploy; [ "$status" -eq 0 ]
  OVERLAYS=compose.tls.yml deploy
  [ "$status" -eq 0 ]
  [ "$(last)" = "deploy-result: recreated (inputs changed)" ]
  assert_grep -q -- '-f compose.tls.yml -f .*/inputs-label.yml up -d --force-recreate' "$S/compose.log"
  OVERLAYS=compose.tls.yml deploy
  [ "$(last)" = "deploy-result: unchanged" ]
}

@test "agw change-aware: a re-pulled image, a stopped container, or no readiness recreates" {
  deploy; [ "$status" -eq 0 ]
  echo img-2 > "$S/image_now"
  deploy; [ "$(last)" = "deploy-result: recreated (image changed: agentgateway)" ]
  echo false > "$S/running"
  deploy; [ "$(last)" = "deploy-result: recreated (gateway container not running)" ]
  # A gateway left by a recreate that never became ready: same label, still not converged.
  touch "$S/not_ready"
  deploy; [ "$(last)" = "deploy-result: recreated (readiness not answering)" ]
  [ "$(recreates)" -eq 4 ]
}

@test "agw change-aware: a moved db image tag recreates, naming the db container" {
  deploy; [ "$status" -eq 0 ]
  # postgres:16-alpine is a mutable tag: a pull can move it while every input file is the same.
  echo db-2 > "$S/db_image_now"
  deploy
  [ "$status" -eq 0 ]
  [ "$(last)" = "deploy-result: recreated (image changed: agentgateway-db)" ]
  [ "$(recreates)" -eq 2 ]
  [ "$(cat "$S/db_image_run")" = db-2 ]
  deploy
  [ "$(last)" = "deploy-result: unchanged" ]
}

@test "agw change-aware: a container listing that misses the gateway recreates instead of matching" {
  deploy; [ "$status" -eq 0 ]
  # The stub lists nothing for any other selector; an empty comparison must not pass.
  STUB_DIR=/elsewhere deploy
  [ "$status" -eq 0 ]
  [ "$(last)" = "deploy-result: recreated (project containers not listed)" ]
}

@test "agw change-aware: a container from before this change (no label) recreates once" {
  touch "$S/exists"; echo true > "$S/running"; echo img-1 > "$S/image_run"
  deploy
  [ "$(last)" = "deploy-result: recreated (inputs changed)" ]
  deploy
  [ "$(last)" = "deploy-result: unchanged" ]
}

@test "agw change-aware: a deploy that fails after the render is converged by the next run" {
  deploy; [ "$status" -eq 0 ]
  local before; before=$(cat "$S/label")
  echo "# rotated" >> "$D/config.yaml"
  touch "$S/compose_fail"
  deploy
  [ "$status" -ne 0 ]
  # The running container still names the inputs it started with...
  [ "$(cat "$S/label")" = "$before" ]
  rm "$S/compose_fail"
  # ...so the next plain run sees the difference, though the files were already current.
  deploy
  [ "$status" -eq 0 ]
  [ "$(last)" = "deploy-result: recreated (inputs changed)" ]
}

@test "agw change-aware: an unreadable input fails the deploy instead of guessing" {
  [ "$(id -u)" -ne 0 ] || skip "root reads a 0000 file"
  chmod 000 "$D/.env"
  deploy
  chmod 600 "$D/.env"
  [ "$status" -ne 0 ]
  assert_contains "$output" "Cannot read .env"
  [ "$(recreates)" -eq 0 ]
}

@test "agw change-aware: an unreadable certificate fails the deploy naming it" {
  [ "$(id -u)" -ne 0 ] || skip "root reads a 0000 file"
  deploy; [ "$status" -eq 0 ]
  chmod 000 "$D/certs/agw-server/serial-1/cert.pem"
  deploy
  chmod 644 "$D/certs/agw-server/serial-1/cert.pem"
  [ "$status" -ne 0 ]
  assert_contains "$output" "Cannot read certs/agw-server/"
  assert_contains "$output" "cert.pem to hash"
  [ "$(recreates)" -eq 1 ]
}

@test "agw change-aware: a certs listing that fails fails the deploy instead of hashing part of it" {
  [ "$(id -u)" -ne 0 ] || skip "root lists a 0000 directory"
  deploy; [ "$status" -eq 0 ]
  # find cannot enter the leaf directory, so it lists nothing under it and exits non-zero.
  chmod 000 "$D/certs/agw-server/serial-1"
  deploy
  chmod 755 "$D/certs/agw-server/serial-1"
  [ "$status" -ne 0 ]
  assert_contains "$output" "Cannot read certs: listing its files failed"
  [ "$(recreates)" -eq 1 ]
}

@test "agw change-aware: the deploy task's changed status comes from deploy.sh's result line" {
  local pb="$REPO_ROOT/platform/playbooks/deploy-agentgateway.yml"
  local blk; blk=$(task_block "$pb" 'Run deploy.sh (container lifecycle)')
  assert_contains "$blk" "changed_when: \"_deploy.stdout_lines | select('match', '^deploy-result: recreated')"
  refute_contains "$blk" "changed_when: true"
  # The listener-TLS overlay is still passed to deploy.sh: through the one environment mapping
  # the deploy, the rollback and the runtime verify share.
  assert_contains "$blk" 'environment: "{{ _agw_deploy_env }}"'
  assert_grep -qF "compose.tls.yml" "$REPO_ROOT/platform/playbooks/vars/agw-deploy-env.yml"
}
