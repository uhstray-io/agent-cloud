#!/usr/bin/env bats
# `make local-foundation-down` stops the genesis foundation and removes nothing. The function is
# run against a `podman` stub that records its argv, so no container is touched.
# Run: bats platform/tests/test_local_dev_foundation_down.bats

load assert_helpers

# Reverse genesis order; the script's own list must match this exactly.
ORDER="local-semaphore authentik-worker authentik-server authentik-redis authentik-postgresql caddy step-ca dns local-openbao"

setup() {
  unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE
  REPO_ROOT=$(git rev-parse --show-toplevel)
  SCRIPT="$REPO_ROOT/scripts/local-dev.sh"
  MF="$REPO_ROOT/Makefile"
  LOG="$BATS_TEST_TMPDIR/podman.log"
  : > "$LOG"
  mkdir -p "$BATS_TEST_TMPDIR/bin"
  # Stub: `inspect` answers from STATE_<name> (running | stopped | absent, default running);
  # `stop` fails for any name in FAIL_STOP; every call is logged.
  cat > "$BATS_TEST_TMPDIR/bin/podman" <<'STUB'
#!/usr/bin/env bash
echo "$*" >> "$LOG"
case "$1" in
  inspect)
    name="${!#}"
    var="STATE_${name//-/_}"
    case "${!var:-running}" in
      running) echo true ;;
      stopped) echo false ;;
      *) exit 125 ;;
    esac ;;
  stop)
    case " ${FAIL_STOP:-} " in *" $2 "*) exit 1 ;; esac ;;
esac
STUB
  chmod +x "$BATS_TEST_TMPDIR/bin/podman"
}

_fn() {
  awk '/^foundation_down\(\) \{/ {f = 1} f {print} f && /^}/ {exit}' "$SCRIPT"
}

# Run foundation_down with the stub first on PATH; extra env assignments are passed through.
_run_down() {
  run env LOG="$LOG" PATH="$BATS_TEST_TMPDIR/bin:$PATH" "$@" bash -c '
    info() { printf "[local-dev] %s\n" "$*"; }
    die()  { printf "[local-dev] ERROR: %s\n" "$*" >&2; exit 1; }
    '"$(_fn)"'
    foundation_down
  '
}

_stops() { grep '^stop ' "$LOG" | sed 's/^stop //' | tr '\n' ' ' | sed 's/ $//'; }

@test "foundation-down: every container is stopped once, in reverse genesis order" {
  _run_down
  [ "$status" -eq 0 ]
  [ "$(_stops)" = "$ORDER" ]
  assert_contains "$output" "make local-bootstrap"
}

@test "foundation-down: never removes a container or touches a volume" {
  _run_down
  [ "$status" -eq 0 ]
  # Only `inspect` and `stop` may ever be issued (anchored: `--format` contains "rm").
  refute_grep -qvE '^(inspect|stop) ' "$LOG"
  refute_grep -qE ' (rm|volume|kill|down)( |$)' "$LOG"
}

@test "foundation-down: re-run with everything already stopped makes no stop call and exits 0" {
  _run_down STATE_local_semaphore=stopped STATE_authentik_worker=stopped \
    STATE_authentik_server=stopped STATE_authentik_redis=stopped \
    STATE_authentik_postgresql=stopped STATE_caddy=stopped STATE_step_ca=stopped \
    STATE_dns=stopped STATE_local_openbao=stopped
  [ "$status" -eq 0 ]
  [ -z "$(_stops)" ]
  assert_contains "$output" "still running among the foundation containers: none"
}

@test "foundation-down: a container that does not exist is skipped, the rest still stop" {
  _run_down STATE_caddy=absent
  [ "$status" -eq 0 ]
  assert_contains "$output" "caddy: not present, skipped"
  [ "$(_stops)" = "${ORDER/caddy /}" ]
}

@test "foundation-down: a failing stop on a running container is a non-zero exit" {
  _run_down FAIL_STOP=step-ca
  [ "$status" -ne 0 ]
  assert_contains "$output" "could not stop step-ca"
}

@test "Makefile: local-foundation-down calls the foundation-down subcommand and is a phony target" {
  run make -n -C "$REPO_ROOT" local-foundation-down
  [ "$status" -eq 0 ]
  assert_contains "$output" "foundation-down"
  assert_contains "$(grep '^\.PHONY' "$MF")" "local-foundation-down"
}

@test "local-dev.sh: foundation-down is dispatched and listed in the usage text" {
  assert_contains "$(grep -E '^  foundation-down\)' "$SCRIPT")" "foundation_down"
  run bash "$SCRIPT" no-such-subcommand
  assert_contains "$output" "foundation-down"
}
