# shellcheck shell=bash
# Fake-runner harness for .githooks/pre-push behavior tests.
# It records command ownership and emits controlled diagnostics without running
# the repository suites recursively from inside their own BATS tests.

setup_pre_push_hook_harness() {
  PRE_PUSH_REPO="$BATS_TEST_TMPDIR/pushing"
  PRE_PUSH_FAKEBIN="$BATS_TEST_TMPDIR/fakebin"
  PRE_PUSH_EVENTS="$BATS_TEST_TMPDIR/pre-push-events"
  PRE_PUSH_INPUT="$BATS_TEST_TMPDIR/pre-push-input"
  PRE_PUSH_FIXTURE_VALUE="fixture-value-that-must-not-appear"

  mkdir -p "$PRE_PUSH_REPO/platform/tests" \
    "$PRE_PUSH_REPO/platform/semaphore/tests" "$PRE_PUSH_FAKEBIN"
  PRE_PUSH_REPO_REAL=$(cd "$PRE_PUSH_REPO" && pwd -P)
  export PRE_PUSH_REPO PRE_PUSH_REPO_REAL PRE_PUSH_FAKEBIN PRE_PUSH_EVENTS PRE_PUSH_INPUT PRE_PUSH_FIXTURE_VALUE
  git init -q "$PRE_PUSH_REPO"
  : > "$PRE_PUSH_EVENTS"
  printf '%s\n' \
    'refs/heads/x 1111111111111111111111111111111111111111 refs/heads/x 0000000000000000000000000000000000000000' \
    > "$PRE_PUSH_INPUT"

  cat > "$PRE_PUSH_REPO/pyproject.toml" <<'TOML'
[tool.pytest.ini_options]
testpaths = [
    "platform/tests",
    "platform/semaphore/tests",
]
TOML
  : > "$PRE_PUSH_REPO/platform/semaphore/tests/test_scoped_publication.py"

  cat > "$PRE_PUSH_FAKEBIN/bats" <<'SH'
#!/usr/bin/env sh
printf '%s\n' 'bats' >> "$PRE_PUSH_EVENTS"
if [ "$(pwd -P)" = "$PRE_PUSH_REPO_REAL" ]; then
  printf '%s\n' 'runner-context:bats:root' >> "$PRE_PUSH_EVENTS"
else
  printf '%s\n' 'runner-context:bats:wrong-root' >> "$PRE_PUSH_EVENTS"
fi
if env | grep -Eq '^(GIT_DIR|GIT_WORK_TREE|GIT_INDEX_FILE|GIT_COMMON_DIR|GIT_OBJECT_DIRECTORY|GIT_ALTERNATE_OBJECT_DIRECTORIES|GIT_PREFIX|GIT_NAMESPACE)='; then
  printf '%s\n' 'git-env:bats:dirty' >> "$PRE_PUSH_EVENTS"
else
  printf '%s\n' 'git-env:bats:clean' >> "$PRE_PUSH_EVENTS"
fi
printf '%s\n' 'hook ownership progress' 'ok 1 named BATS scenario'
exit 0
SH

  cat > "$PRE_PUSH_FAKEBIN/python3" <<'SH'
#!/usr/bin/env sh
scenario=${PRE_PUSH_SCENARIO:-healthy}
expected_pp=platform/services/netbox/deployment/workers/proxmox_discovery:platform/services/netbox/deployment/workers/pfsense_sync
if env | grep -Eq '^(GIT_DIR|GIT_WORK_TREE|GIT_INDEX_FILE|GIT_COMMON_DIR|GIT_OBJECT_DIRECTORY|GIT_ALTERNATE_OBJECT_DIRECTORIES|GIT_PREFIX|GIT_NAMESPACE)='; then
  git_state=dirty
else
  git_state=clean
fi
if [ "$(pwd -P)" = "$PRE_PUSH_REPO_REAL" ]; then
  root_state=root
else
  root_state=wrong-root
fi
if [ "${PYTHONPATH:-}" = "$expected_pp" ]; then
  pp_state=expected-pythonpath
else
  pp_state=wrong-pythonpath
fi

if [ "${1:-}" = "-m" ] && [ "${2:-}" = "pytest" ]; then
  case " $* " in
    *' --collect-only '*)
      printf '%s\n' 'pytest-collect' "git-env:pytest-collect:$git_state" \
        "runner-context:pytest-collect:$root_state:$pp_state" >> "$PRE_PUSH_EVENTS"
      case "$scenario" in
        healthy|scoped-fail)
          printf '%s\n' 'platform/semaphore/tests/test_scoped_publication.py::ScopedPublicationTests::test_named_progress'
          exit 0
          ;;
        missing-*)
          printf '%s\n' \
            'ERROR collecting platform/tests/test_unrelated.py' \
            "ModuleNotFoundError: No module named 'unrelated_dependency'"
          exit 2
          ;;
        mixed-missing-syntax)
          printf '%s\n' \
            'ERROR collecting platform/tests/test_unrelated.py' \
            "ModuleNotFoundError: No module named 'unrelated_dependency'" \
            'ERROR collecting platform/tests/test_broken.py' \
            'SyntaxError: invalid syntax' \
            "$PRE_PUSH_FIXTURE_VALUE"
          exit 2
          ;;
        mixed-syntax-missing)
          printf '%s\n' \
            'ERROR collecting platform/tests/test_broken.py' \
            'SyntaxError: invalid syntax' \
            "$PRE_PUSH_FIXTURE_VALUE" \
            'ERROR collecting platform/tests/test_unrelated.py' \
            "ModuleNotFoundError: No module named 'unrelated_dependency'"
          exit 2
          ;;
        mixed-missing-config)
          printf '%s\n' \
            'ERROR collecting platform/tests/test_unrelated.py' \
            "ModuleNotFoundError: No module named 'unrelated_dependency'" \
            'ERROR: pyproject.toml contains an invalid pytest option' \
            "$PRE_PUSH_FIXTURE_VALUE"
          exit 4
          ;;
        mixed-missing-exception)
          printf '%s\n' \
            'ERROR collecting platform/tests/test_unrelated.py' \
            "ModuleNotFoundError: No module named 'unrelated_dependency'" \
            'ERROR collecting platform/tests/test_other_failure.py' \
            'ValueError: collection fixture failed' \
            "$PRE_PUSH_FIXTURE_VALUE"
          exit 2
          ;;
        syntax)
          printf '%s\n' \
            'ERROR collecting platform/tests/test_broken.py' \
            'SyntaxError: invalid syntax' \
            "$PRE_PUSH_FIXTURE_VALUE"
          exit 2
          ;;
        config)
          printf '%s\n' \
            'ERROR: pyproject.toml contains an invalid pytest option' \
            "$PRE_PUSH_FIXTURE_VALUE"
          exit 4
          ;;
      esac
      ;;
    *)
      printf '%s\n' 'pytest-full' "git-env:pytest-full:$git_state" \
        "runner-context:pytest-full:$root_state:$pp_state" >> "$PRE_PUSH_EVENTS"
      if [ "$scenario" = "scoped-fail" ]; then
        printf '%s\n' \
          'platform/semaphore/tests/test_scoped_publication.py::ScopedPublicationTests::test_named_progress FAILED'
        exit 1
      fi
      printf '%s\n' \
        'platform/semaphore/tests/test_scoped_publication.py::ScopedPublicationTests::test_named_progress PASSED'
      exit 0
      ;;
  esac
fi

printf '%s\n' 'scoped-fallback' "git-env:scoped-fallback:$git_state" \
  "runner-context:scoped-fallback:$root_state:$pp_state" >> "$PRE_PUSH_EVENTS"
case "$scenario" in
  missing-pass|mixed-*|syntax|config)
    printf '%s\n' \
      'test_scope (__main__.ScopedPublicationTests.test_scope) ... ok' \
      '' \
      'Ran 40 tests in 1.000s' \
      '' \
      'OK'
    exit 0
    ;;
  missing-fail)
    printf '%s\n' \
      'test_rejects_drift (__main__.ScopedPublicationTests.test_rejects_drift) ... FAIL' \
      '======================================================================' \
      'FAIL: test_rejects_drift (__main__.ScopedPublicationTests.test_rejects_drift)' \
      "$PRE_PUSH_FIXTURE_VALUE" \
      'Ran 40 tests in 1.000s' \
      'FAILED (failures=1)'
    exit 1
    ;;
  missing-fallback-dep)
    printf '%s\n' \
      'Traceback (most recent call last):' \
      "$PRE_PUSH_FIXTURE_VALUE" \
      "ModuleNotFoundError: No module named 'scoped_dependency'"
    exit 1
    ;;
  *)
    printf '%s\n' 'unexpected fallback invocation' >&2
    exit 3
    ;;
esac
SH

  chmod +x "$PRE_PUSH_FAKEBIN/bats" "$PRE_PUSH_FAKEBIN/python3"
  PATH="$PRE_PUSH_FAKEBIN:$PATH"
  export PATH
}

run_pre_push_scenario() {
  PRE_PUSH_SCENARIO=$1
  export PRE_PUSH_SCENARIO
  cd "$PRE_PUSH_REPO" || return 1
  run sh "$HOOK" origin https://example.invalid/repo.git < "$PRE_PUSH_INPUT"
}

assert_pre_push_event_count() {
  local event=$1
  local expected=$2
  local actual
  actual=$(awk -v wanted="$event" '$0 == wanted { count++ } END { print count + 0 }' "$PRE_PUSH_EVENTS")
  [ "$actual" -eq "$expected" ] || {
    echo "expected event '$event' $expected time(s), observed $actual" >&2
    cat "$PRE_PUSH_EVENTS" >&2
    return 1
  }
}
