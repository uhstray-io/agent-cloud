#!/usr/bin/env bats
# Behavioural tests for compose_up_if_changed (platform/lib/common.sh), the change-aware start
# authentik and o11y deploy through (operator decision 2026-10-04: recreate only on change —
# a second deploy with identical inputs is a true no-op).
#
# Each service's real deploy.sh runs against a stub container engine and a stub compose
# provider. The stubs keep every container's state in files (running, its inputs label, the
# image it runs) and list the containers only for the project's working-directory label. The
# compose stub plays `up -d --force-recreate`: it records the label the generated overlay
# carries for each service and moves every container to the image its tag names now.
# Run: bats platform/tests/test_compose_up_if_changed.bats

load assert_helpers

# svc_setup <service> <container...>: copy the service's deploy dir into a scratch tree.
svc_setup() {
  local svc="$1"; shift
  T="$BATS_TEST_TMPDIR/tree"
  D="$T/platform/services/$svc/deployment"
  S="$BATS_TEST_TMPDIR/state"
  mkdir -p "$T/platform/lib" "$T/bin" "$S/c" "$(dirname "$D")"
  cp "$REPO_ROOT/platform/lib/common.sh" "$T/platform/lib/"
  cp -R "$REPO_ROOT/platform/services/$svc/deployment" "$D"
  rm -f "$D/.env"
  printf 'SECRET=one\nO11Y_ZONE=example.test\nO11Y_AUTHENTIK_EDGE_IP=192.0.2.1\n' > "$D/.env"
  NAMES="$*"
  echo img-1 > "$S/image_now"
  echo 0 > "$S/recreates"

  cat > "$T/bin/engine" <<'STUB'
#!/usr/bin/env bash
S="$STUB_STATE"
if [ "$1" = image ] && [ "$2" = inspect ]; then cat "$S/image_now"; exit 0; fi
if [ "$1" = ps ]; then
  [ "$*" = "ps -a --filter label=com.docker.compose.project.working_dir=$STUB_DIR --format {{.Names}}" ] || exit 0
  for d in "$S"/c/*; do [ -d "$d" ] && basename "$d"; done; exit 0
fi
if [ "$1" = inspect ]; then
  if [ "$2" = --format ]; then fmt="$3" name="$4"; else fmt="${2#--format=}" name="$3"; fi
  c="$S/c/$name"; [ -d "$c" ] || exit 1
  case "$fmt" in
    *Health*) echo healthy ;;
    *compose.service*) cat "$c/service" ;;
    *State.Running*) cat "$c/running" ;;
    *Labels*) cat "$c/label" 2>/dev/null || echo "<no value>" ;;
    *Config.Image*) echo cr.example/img:v1 ;;
    *'.Image}}'*) cat "$c/image_run" ;;
  esac
  exit 0
fi
exit 0
STUB

  cat > "$T/bin/compose" <<'STUB'
#!/usr/bin/env bash
# STUB_NAMES: container=service pairs the provider would create.
S="$STUB_STATE"
echo "$*" >> "$S/compose.log"
files=() prev="" overlay=""
for a in "$@"; do
  if [ "$prev" = -f ]; then
    case "$a" in *inputs-label.yml) overlay="$a" ;; *) files+=("$a") ;; esac
  fi
  prev="$a"
done
if case " $* " in *" config --services "*) true ;; *) false ;; esac; then
  [ -f "$S/no_config" ] && exit 1
  for f in "${files[@]}"; do
    awk '/^services:/ {s=1; next} s && /^[^ #]/ {s=0} s && /^  [a-z0-9-]+:/ {sub(/^  /,""); sub(/:.*/,""); print}' "$f"
  done | awk '!seen[$0]++'
  exit 0
fi
case " $* " in *" up "*) ;; *) exit 0 ;; esac
case " $* " in *" --force-recreate "*) ;; *) exit 0 ;; esac
[ -n "$overlay" ] || { echo "recreate without the label overlay" >&2; exit 1; }
label=$(sed -n 's/^ *io\.agent-cloud\.inputs-sha256: "\(.*\)"$/\1/p' "$overlay" | sort -u)
[ "$(printf '%s\n' "$label" | grep -c .)" -eq 1 ] || { echo "labels differ" >&2; exit 1; }
svcs=$(sed -n 's/^  \([a-z0-9-]*\):$/\1/p' "$overlay")
if case " $* " in *" --remove-orphans "*) true ;; *) false ;; esac; then
  for d in "$S"/c/*; do
    [ -d "$d" ] || continue
    printf '%s\n' "$svcs" | grep -qx "$(cat "$d/service")" || rm -rf "$d"
  done
fi
for pair in $STUB_NAMES; do
  n="${pair%%=*}" svc="${pair#*=}"
  printf '%s\n' "$svcs" | grep -qx "$svc" || continue
  mkdir -p "$S/c/$n"; echo true > "$S/c/$n/running"; echo "$label" > "$S/c/$n/label"
  echo "$svc" > "$S/c/$n/service"; cp "$S/image_now" "$S/c/$n/image_run"
done
printf '%s\n' "$svcs" | grep -c . > "$S/labelled_services"
echo $(( $(cat "$S/recreates") + 1 )) > "$S/recreates"
STUB
  chmod +x "$T/bin/engine" "$T/bin/compose"
}

deploy() {
  run env STUB_STATE="$S" STUB_DIR="$D" STUB_NAMES="$NAMES" CONTAINER_ENGINE="$T/bin/engine" \
    COMPOSE_CMD="$T/bin/compose" LOCAL_MODE="${LOCAL:-}" COMPOSE_OVERLAYS="${OVERLAYS:-}" FORCE_RECREATE="${FORCE:-}" bash "$D/${SCRIPT:-deploy.sh}" --no-pull
}
recreates() { cat "$S/recreates"; }
changed() { printf '%s\n' "${lines[@]}" | grep -x "DEPLOY_CHANGED=$1"; }

setup() { REPO_ROOT=$(git rev-parse --show-toplevel); }

# ── authentik ────────────────────────────────────────────────────────────────
a_setup() {
  svc_setup authentik authentik-postgresql=postgresql authentik-redis=redis authentik-server=server authentik-worker=worker
  mkdir -p "$D/blueprints-active"; printf 'version: 1\n' > "$D/blueprints-active/a.yaml"
}

@test "authentik: first deploy creates and labels every service with a bare digest" {
  a_setup
  deploy
  [ "$status" -eq 0 ]; changed true
  [ "$(recreates)" -eq 1 ]
  [ "$(cat "$S/labelled_services")" -eq 4 ]
  grep -qE '^[0-9a-f]{64}$' "$S/c/authentik-server/label"
  refute_grep -q 'SECRET' "$S/c/authentik-server/label"
}

@test "authentik: same inputs twice is a true no-op (no compose up at all)" {
  a_setup
  deploy; [ "$status" -eq 0 ]
  : > "$S/compose.log"
  deploy
  [ "$status" -eq 0 ]; changed false
  [ "$(recreates)" -eq 1 ]
  refute_grep -q ' up ' "$S/compose.log"
}

@test "authentik: FORCE_RECREATE=true recreates with unchanged inputs; other values do not" {
  a_setup
  deploy; [ "$status" -eq 0 ]
  FORCE=true deploy
  [ "$status" -eq 0 ]; changed true
  [ "$(recreates)" -eq 2 ]
  printf '%s\n' "${lines[@]}" | grep -q 'FORCE_RECREATE=true'
  local v
  for v in false yes 1 True; do
    FORCE=$v deploy
    [ "$status" -eq 0 ]; changed false
    [ "$(recreates)" -eq 2 ]
  done
}

@test "deploy playbooks pass deploy_force_recreate to deploy.sh, default off" {
  local pb
  for pb in deploy-authentik.yml deploy-o11y.yml; do
    python3 - "$REPO_ROOT/platform/playbooks/$pb" <<'PY2'
import sys, yaml
plays = yaml.safe_load(open(sys.argv[1]))
t = [t for p in plays for t in p.get("tasks", []) if t.get("name") == "Run deploy.sh (container lifecycle)"][0]
import jinja2
expr = t["environment"]["FORCE_RECREATE"]
render = lambda **kw: jinja2.Environment().from_string(expr).render(**kw)
assert render() == "", "default must not force"
for v in (True, "true", "True", "TRUE"):
    assert render(deploy_force_recreate=v) == "true", v
for v in ("yes", "1", 1, "on", "y", False, "false", ""):
    assert render(deploy_force_recreate=v) == "", v
PY2
  done
}

@test "authentik: a changed .env, env/*.env, blueprint or compose file recreates" {
  a_setup
  local n=1 f
  mkdir -p "$D/env"; echo X=1 > "$D/env/extra.env"
  deploy; [ "$status" -eq 0 ]
  for f in .env env/extra.env blueprints-active/a.yaml compose.yml; do
    echo "# changed" >> "$D/$f"
    deploy; [ "$status" -eq 0 ]; changed true
    n=$((n + 1)); [ "$(recreates)" -eq "$n" ]
    deploy; changed false
  done
}

@test "authentik: a re-pulled tag (new image id) recreates" {
  a_setup
  deploy; [ "$status" -eq 0 ]
  echo img-2 > "$S/image_now"
  deploy; [ "$status" -eq 0 ]; changed true
  [ "$(recreates)" -eq 2 ]
  deploy; changed false
}

@test "authentik: a stopped or missing container recreates" {
  a_setup
  deploy; [ "$status" -eq 0 ]
  echo false > "$S/c/authentik-worker/running"
  deploy; changed true; [ "$(recreates)" -eq 2 ]
  rm -rf "$S/c/authentik-redis"
  deploy
  changed true; [ "$(recreates)" -eq 3 ]
  deploy; changed false
}

@test "authentik: an unreadable input refuses rather than guessing" {
  [ "$(id -u)" -ne 0 ] || skip "root reads a mode-000 file"
  a_setup
  deploy; [ "$status" -eq 0 ]
  chmod 000 "$D/blueprints-active/a.yaml"
  deploy
  chmod 644 "$D/blueprints-active/a.yaml"
  [ "$status" -ne 0 ]
  grep -q 'refusing to guess' <<< "$output"
  [ "$(recreates)" -eq 1 ]
}

@test "authentik: an unlistable input directory refuses rather than guessing" {
  [ "$(id -u)" -ne 0 ] || skip "root lists a mode-000 directory"
  a_setup
  deploy; [ "$status" -eq 0 ]
  mkdir "$D/blueprints-active/sub"; chmod 000 "$D/blueprints-active/sub"
  deploy
  chmod 755 "$D/blueprints-active/sub"
  [ "$status" -ne 0 ]
  grep -q 'Cannot list' <<< "$output"
  [ "$(recreates)" -eq 1 ]
}

# ── o11y ─────────────────────────────────────────────────────────────────────
o_setup() {
  svc_setup o11y o11y-node-exporter=node-exporter o11y-prometheus=prometheus o11y-loki=loki o11y-alloy=alloy o11y-tempo=tempo o11y-pyroscope=pyroscope o11y-grafana=grafana
}

@test "o11y: first deploy creates, second identical deploy is a no-op" {
  o_setup
  deploy; [ "$status" -eq 0 ]; changed true
  [ "$(cat "$S/labelled_services")" -eq 7 ]
  : > "$S/compose.log"
  deploy; [ "$status" -eq 0 ]; changed false
  refute_grep -q ' up ' "$S/compose.log"
}

@test "o11y: a changed .env, config file or the prod overlay recreates; an image change too" {
  o_setup
  local n=1 f
  deploy; [ "$status" -eq 0 ]
  for f in .env config/loki-config.yml compose.prod.yml; do
    echo "# changed" >> "$D/$f"
    deploy; [ "$status" -eq 0 ]; changed true
    n=$((n + 1)); [ "$(recreates)" -eq "$n" ]
    deploy; changed false
  done
  echo img-9 > "$S/image_now"
  deploy; changed true
}

@test "o11y: switching local mode changes the compose files in effect and recreates" {
  o_setup
  deploy; [ "$status" -eq 0 ]
  LOCAL=true deploy; [ "$status" -eq 0 ]; changed true
  LOCAL=true deploy; changed false
}

# ── synthetic project: overlay-only and removed services ─────────────────────
# A minimal deploy dir whose deploy.sh is just compose_up_if_changed, so the overlay topology
# (postiz's compose.search.yml shape) is tested without migrating another service.
syn_setup() {
  svc_setup authentik base-c=base extra-c=extra
  D="$T/platform/services/syn/deployment"; mkdir -p "$D"
  printf 'services:\n  base:\n    image: img\n' > "$D/compose.yml"
  printf 'services:\n  extra:\n    image: img\n' > "$D/compose.extra.yml"
  printf 'K=v\n' > "$D/.env"
  cat > "$D/deploy.sh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
source ../../../lib/common.sh
compose_up_if_changed
SH
}

@test "overlay-only service is labelled and the second run is a no-op" {
  syn_setup
  OVERLAYS=compose.extra.yml deploy; [ "$status" -eq 0 ]; changed true
  [ "$(cat "$S/labelled_services")" -eq 2 ]
  [ -d "$S/c/extra-c" ]
  OVERLAYS=compose.extra.yml deploy; [ "$status" -eq 0 ]; changed false
  [ "$(recreates)" -eq 1 ]
}

@test "provider without config --services: every compose file is parsed instead" {
  syn_setup
  touch "$S/no_config"
  OVERLAYS=compose.extra.yml deploy; [ "$status" -eq 0 ]; changed true
  [ "$(cat "$S/labelled_services")" -eq 2 ]
  OVERLAYS=compose.extra.yml deploy; changed false
}

@test "a removed service: one recreate removes its orphan, then a no-op" {
  syn_setup
  OVERLAYS=compose.extra.yml deploy; [ "$status" -eq 0 ]
  deploy; [ "$status" -eq 0 ]; changed true
  grep -q -- '--remove-orphans' "$S/compose.log"
  [ ! -d "$S/c/extra-c" ]
  deploy; changed false
  [ "$(recreates)" -eq 2 ]
}

@test "an orphan with current inputs (service gone, files unchanged) recreates once, then a no-op" {
  syn_setup
  deploy; [ "$status" -eq 0 ]
  cp -R "$S/c/base-c" "$S/c/stale-c"; echo gone > "$S/c/stale-c/service"
  deploy; [ "$status" -eq 0 ]; changed true
  [ ! -d "$S/c/stale-c" ]
  deploy; changed false
}

@test "a directory symlink loop refuses rather than hanging or guessing" {
  a_setup
  deploy; [ "$status" -eq 0 ]
  mkdir "$D/blueprints-active/sub"; ln -s .. "$D/blueprints-active/sub/up"
  deploy
  [ "$status" -ne 0 ]
  grep -q 'Symlink loop' <<< "$output"
  rm "$D/blueprints-active/sub/up"; ln -s . "$D/blueprints-active/sub/self"
  deploy
  [ "$status" -ne 0 ]
  [ "$(recreates)" -eq 1 ]
}

# ── playbooks read the line ──────────────────────────────────────────────────
@test "deploy playbooks derive changed from DEPLOY_CHANGED, not changed_when: true" {
  local pb
  for pb in deploy-authentik.yml deploy-o11y.yml; do
    python3 - "$REPO_ROOT/platform/playbooks/$pb" <<'PY'
import sys, yaml
plays = yaml.safe_load(open(sys.argv[1]))
tasks = [t for p in plays for t in p.get("tasks", []) if t.get("name") == "Run deploy.sh (container lifecycle)"]
assert len(tasks) == 1, tasks
assert tasks[0]["changed_when"] == "'DEPLOY_CHANGED=true' in _deploy.stdout_lines", tasks[0]["changed_when"]
PY
  done
}
