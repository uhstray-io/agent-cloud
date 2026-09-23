#!/usr/bin/env bats
# Semaphore's deploy refuses a pinned image older than the running controller (PR 203 review:
# the pin was chosen without reading production's version; change task 0.3).

load assert_helpers

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  DEPLOY="$REPO_ROOT/platform/services/semaphore/deployment/deploy.sh"
  # The stub engine: EXISTS=0 means no controller container; RUNNING_VERSION empty means
  # `semaphore version` does not answer (stopped); IMAGE is what the container was created from.
  cat > "$BATS_TEST_TMPDIR/engine" <<'STUB'
#!/usr/bin/env bash
case "$1 $2 $3" in
  "container inspect --format") echo "${IMAGE}" ;;
  "container inspect workflow-semaphore") [ "${EXISTS:-1}" = 1 ] ;;
  "exec workflow-semaphore semaphore")
    [ -n "${RUNNING_VERSION}" ] || exit 1; echo "${RUNNING_VERSION}^0-8a4dcf0-1780941924" ;;
  *) exit 1 ;;
esac
STUB
  chmod +x "$BATS_TEST_TMPDIR/engine"
}

check() {  # check <running> <pin> [image] [exists] -> runs assert_no_downgrade
  run bash -c "export CONTAINER_ENGINE='$BATS_TEST_TMPDIR/engine' RUNNING_VERSION='$1' SEMAPHORE_IMAGE='docker.io/semaphoreui/semaphore:$2' IMAGE='${3:-}' EXISTS='${4:-1}'; source '$DEPLOY'; assert_no_downgrade"
}

@test "semaphore deploy: a pin older than the running controller is refused" {
  check v2.19.12 v2.19.11
  [ "$status" -ne 0 ]
  [[ "$output" == *"Refusing to downgrade Semaphore: v2.19.12 is running, the pin is v2.19.11"* ]]
}

@test "semaphore deploy: an equal or newer pin passes" {
  check v2.18.12 v2.19.11
  [ "$status" -eq 0 ]
  check v2.19.11 v2.19.11
  [ "$status" -eq 0 ]
}

@test "semaphore deploy: version order is numeric, not lexical" {
  check v2.9.0 v2.19.11
  [ "$status" -eq 0 ]
}

@test "semaphore deploy: an unversioned pin (latest) has nothing to compare" {
  check v2.19.12 latest
  [ "$status" -eq 0 ]
}

@test "semaphore deploy: a stopped controller is compared by the image it was created from" {
  check "" v2.19.11 docker.io/semaphoreui/semaphore:v2.19.12
  [ "$status" -ne 0 ]
  assert_contains "$output" "v2.19.12 is running, the pin is v2.19.11"
  check "" v2.19.11 docker.io/semaphoreui/semaphore:v2.18.12
  [ "$status" -eq 0 ]
}

@test "semaphore deploy: an existing controller whose version cannot be read is refused" {
  # PR 203 Codex review: a stopped controller used to pass, so the pin could start over a
  # newer database.
  check "" v2.19.11 docker.io/semaphoreui/semaphore:latest
  [ "$status" -ne 0 ]
  [[ "$output" == *"workflow-semaphore exists but its version cannot be read"* ]]
}

@test "semaphore deploy: no controller container yet has nothing to compare" {
  check "" v2.19.11 "" 0
  [ "$status" -eq 0 ]
}

@test "semaphore deploy: the version check runs before compose starts anything" {
  sed -n '/^step_start_services()/,/^}/p' "$DEPLOY" > "$BATS_TEST_TMPDIR/start.sh"
  assert_precedes "$BATS_TEST_TMPDIR/start.sh" 'assert_no_downgrade' 'compose up -d'
}
