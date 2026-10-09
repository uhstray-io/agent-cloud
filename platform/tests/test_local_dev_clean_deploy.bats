#!/usr/bin/env bats
# `make local-clean-deploy-<svc>` must carry the confirmation every Clean Deploy now requires
# (tasks/assert-reset-confirmed.yml). The dispatcher never fills it in: a confirmation the tool
# supplies is not one. Run: bats platform/tests/test_local_dev_clean_deploy.bats

load assert_helpers

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  SCRIPT="$REPO_ROOT/scripts/local-dev.sh"
  MF="$REPO_ROOT/Makefile"
}

# The clean_deploy function alone, so it can run against stubs instead of a live Semaphore.
_clean_deploy_fn() {
  awk '/^clean_deploy\(\) \{/ {f = 1} f {print} f && /^}/ {exit}' "$SCRIPT"
}

@test "local-dev clean-deploy: no confirmation dies before anything is dispatched" {
  run bash "$SCRIPT" clean-deploy dns
  [ "$status" -ne 0 ]
  assert_contains "$output" "CONFIRM_RESET=<host>"
  assert_contains "$output" "dns-local"
  refute_contains "$output" "dispatched"
}

@test "local-dev clean-deploy: an empty confirmation dies too" {
  run bash "$SCRIPT" clean-deploy dns ""
  [ "$status" -ne 0 ]
  assert_contains "$output" "CONFIRM_RESET=<host>"
}

@test "local-dev clean-deploy: the confirmation reaches the template as a confirm_reset JSON extra var" {
  # Stubs: die and guard as in the script's contract, _run_template records its arguments.
  # The confirmation carries a quote and a shell word to prove it is encoded, not spliced.
  tricky='dns-local", "x": "$(touch '"$BATS_TEST_TMPDIR"'/pwned)'
  run env OUT="$BATS_TEST_TMPDIR/args" TRICKY="$tricky" bash -c '
    die() { echo "die: $*" >&2; exit 1; }
    guard() { :; }
    INV=unused
    _run_template() { printf "%s\n%s\n" "$1" "$2" > "$OUT"; }
    '"$(_clean_deploy_fn)"'
    clean_deploy dns "$TRICKY"
  '
  [ "$status" -eq 0 ]
  [ ! -e "$BATS_TEST_TMPDIR/pwned" ]
  [ "$(sed -n 1p "$BATS_TEST_TMPDIR/args")" = "platform/playbooks/clean-deploy-dns.yml" ]
  python3 - "$BATS_TEST_TMPDIR/args" "$tricky" <<'PY'
import json, sys
lines = open(sys.argv[1]).read().split("\n")
assert json.loads(lines[1]) == {"confirm_reset": sys.argv[2]}, lines
PY
}

@test "local-dev clean-deploy: the function does not invent a confirmation" {
  # Scoped to the function: it reads the caller's second argument and never derives a host name
  # (its usage message, which names <svc>-local to the human, is left out of the check).
  body=$(_clean_deploy_fn)
  assert_contains "$body" 'confirm="${2:-}"'
  refute_contains "$(printf '%s\n' "$body" | grep -v 'die ')" '-local'
}

@test "Makefile: local-clean-deploy-% hands CONFIRM_RESET to the dispatcher" {
  run awk -v t='local-clean-deploy-%:' 'f && !/^\t/ {exit} f {print} index($0, t) == 1 {f = 1}' "$MF"
  assert_contains "$output" 'clean-deploy $* "$(CONFIRM_RESET)"'
}
