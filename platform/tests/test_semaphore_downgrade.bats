#!/usr/bin/env bats
# Semaphore's deploy refuses a pinned image older than the running controller (PR 203 review:
# the pin was chosen without reading production's version; change task 0.3).

load assert_helpers

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  DEPLOY="$REPO_ROOT/platform/services/semaphore/deployment/deploy.sh"
  cat > "$BATS_TEST_TMPDIR/engine" <<'STUB'
#!/usr/bin/env bash
[ "$1 $3 $4" = "exec semaphore version" ] && echo "${RUNNING_VERSION}^0-8a4dcf0-1780941924"
STUB
  chmod +x "$BATS_TEST_TMPDIR/engine"
}

check() {  # check <running> <pin> -> runs assert_no_downgrade
  run bash -c "export CONTAINER_ENGINE='$BATS_TEST_TMPDIR/engine' RUNNING_VERSION='$1' SEMAPHORE_IMAGE='docker.io/semaphoreui/semaphore:$2'; source '$DEPLOY'; assert_no_downgrade"
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

@test "semaphore deploy: the version check runs before compose starts anything" {
  sed -n '/^step_start_services()/,/^}/p' "$DEPLOY" > "$BATS_TEST_TMPDIR/start.sh"
  assert_precedes "$BATS_TEST_TMPDIR/start.sh" 'assert_no_downgrade' 'compose up -d'
}
