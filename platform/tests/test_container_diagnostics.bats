#!/usr/bin/env bats
# A failed container wait prints the container's state and a redacted log tail
# (Semaphore task 1207: agentgateway's readiness timed out and the run said only
# "see podman logs" on a host nobody was logged into).

load assert_helpers

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  COMMON="$REPO_ROOT/platform/lib/common.sh"
  cat > "$BATS_TEST_TMPDIR/engine" <<'STUB'
#!/usr/bin/env bash
case "$1" in
  inspect) echo "state=exited exit=1 restarts=3 started=then error=" ;;
  logs) printf 'Error: failed to connect postgres://agw:hunter2@agentgateway-db:5432/agw\nAuthorization: Bearer abc.def\n' ;;
  *) exit 1 ;;
esac
STUB
  chmod +x "$BATS_TEST_TMPDIR/engine"
}

redact() { bash -c "source '$COMMON'; redact_secrets" <<<"$1"; }

@test "redact_secrets: blanks URL passwords, Bearer tokens and labelled values" {
  [ "$(redact 'postgres://agw:s3cr3t@db:5432/agw')" = 'postgres://agw:***@db:5432/agw' ]
  [ "$(redact 'Authorization: Bearer abc.def-ghi')" = 'Authorization: Bearer ***' ]
  [ "$(redact 'password=hunter2 token: "xyz" API_KEY=k1, secret=abc}')" = 'password=*** token: "***" API_KEY=***, secret=***}' ]
  [ "$(redact 'a harmless line')" = 'a harmless line' ]
}

@test "dump_container_diagnostics: state and a redacted log tail, never fails" {
  run bash -c "source '$COMMON'; CONTAINER_ENGINE='$BATS_TEST_TMPDIR/engine'; dump_container_diagnostics agentgateway"
  [ "$status" -eq 0 ]
  assert_contains "$output" "state=exited exit=1 restarts=3"
  assert_contains "$output" "postgres://agw:***@agentgateway-db"
  refute_contains "$output" "hunter2"
  refute_contains "$output" "abc.def"
}

@test "dump_container_diagnostics: an engine that fails does not fail the caller" {
  run bash -c "source '$COMMON'; CONTAINER_ENGINE=false; dump_container_diagnostics missing"
  [ "$status" -eq 0 ]
}

@test "wait_for_healthy and the agentgateway readiness wait dump before they fail" {
  sed -n '/^wait_for_healthy()/,/^}/p' "$COMMON" > "$BATS_TEST_TMPDIR/wfh.sh"
  assert_precedes "$BATS_TEST_TMPDIR/wfh.sh" 'dump_container_diagnostics ' 'did not become healthy'
  sed -n '/^step_wait_ready()/,/^}/p' "$REPO_ROOT/platform/services/agentgateway/deployment/deploy.sh" > "$BATS_TEST_TMPDIR/ready.sh"
  assert_precedes "$BATS_TEST_TMPDIR/ready.sh" 'dump_container_diagnostics agentgateway' 'readiness did not respond'
}
