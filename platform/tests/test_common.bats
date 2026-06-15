#!/usr/bin/env bats
# Tests for platform/lib/common.sh pure functions.

setup() {
  export CONTAINER_ENGINE="docker"
  export COMPOSE_CMD="docker compose"
  source "$BATS_TEST_DIRNAME/../lib/common.sh"
  TEST_DIR=$(mktemp -d)
}

teardown() {
  rm -rf "$TEST_DIR"
}

# ── gen_secret ──────────────────────────────────────────────────────

@test "gen_secret: default length >= 20, alphanumeric, unique values" {
  a=$(gen_secret)
  b=$(gen_secret)
  [ ${#a} -ge 20 ]
  [[ ! "$a" =~ [/+=] ]]
  [ "$a" != "$b" ]
}

@test "gen_secret: custom length" {
  result=$(gen_secret 48 16)
  [ ${#result} -eq 16 ]
}

# ── needs_gen ───────────────────────────────────────────────────────

@test "needs_gen: true for empty/placeholder values" {
  needs_gen ""
  needs_gen "REPLACE_ME"
  needs_gen "changeme123"
  needs_gen "placeholder_value"
}

@test "needs_gen: false for real values" {
  ! needs_gen "s3cur3_p4ssw0rd"
  ! needs_gen "550e8400-e29b-41d4-a716-446655440000"
}

# ── get_secret / put_secret ─────────────────────────────────────────

@test "put/get_secret: write, read, permissions, overwrite, missing" {
  put_secret "$TEST_DIR" "key1" "val1"
  [ "$(cat "$TEST_DIR/key1.txt")" = "val1" ]
  [ "$(get_secret "$TEST_DIR" "key1")" = "val1" ]

  # Cross-platform permission check (macOS vs Linux stat)
  if stat -f "%Lp" "$TEST_DIR/key1.txt" >/dev/null 2>&1; then
    perms=$(stat -f "%Lp" "$TEST_DIR/key1.txt")
  else
    perms=$(stat -c "%a" "$TEST_DIR/key1.txt")
  fi
  [ "$perms" = "600" ]

  put_secret "$TEST_DIR" "key1" "val2"
  [ "$(cat "$TEST_DIR/key1.txt")" = "val2" ]

  [ -z "$(get_secret "$TEST_DIR" "nonexistent")" ]
}

# ── detect_runtime ──────────────────────────────────────────────────

@test "detect_runtime: respects CONTAINER_ENGINE env" {
  export CONTAINER_ENGINE="podman"
  detect_runtime
  [ "$CONTAINER_ENGINE" = "podman" ]
}

@test "detect_runtime: derives COMPOSE_CMD for podman when COMPOSE_CMD unset" {
  # Regression: the old code returned early when CONTAINER_ENGINE was set,
  # leaving COMPOSE_CMD empty. Verify the fix: COMPOSE_CMD is derived.
  export CONTAINER_ENGINE="podman"
  unset COMPOSE_CMD
  # Create a fake podman-compose in PATH so the primary branch is taken
  local fake_bin
  fake_bin=$(mktemp -d)
  ln -s /bin/true "${fake_bin}/podman-compose"
  PATH="${fake_bin}:${PATH}" detect_runtime
  [ -n "$COMPOSE_CMD" ]
  rm -rf "$fake_bin"
}

@test "detect_runtime: COMPOSE_CMD for podman falls back to 'podman compose' when podman-compose absent" {
  export CONTAINER_ENGINE="podman"
  unset COMPOSE_CMD
  # Remove podman-compose from PATH so the fallback branch is taken
  local saved_path="$PATH"
  PATH=$(echo "$PATH" | tr ':' '\n' | grep -v 'podman-compose' | tr '\n' ':' | sed 's/:$//')
  # Strip any dir that might have podman-compose by creating a fake podman only
  local fake_bin
  fake_bin=$(mktemp -d)
  ln -s /bin/true "${fake_bin}/podman"
  # podman-compose is deliberately NOT created here
  PATH="${fake_bin}" detect_runtime
  [ "$COMPOSE_CMD" = "podman compose" ]
  rm -rf "$fake_bin"
  PATH="$saved_path"
}

@test "detect_runtime: derives COMPOSE_CMD for docker when COMPOSE_CMD unset" {
  export CONTAINER_ENGINE="docker"
  unset COMPOSE_CMD
  detect_runtime
  [ "$COMPOSE_CMD" = "docker compose" ]
}

@test "detect_runtime: preserves existing COMPOSE_CMD when CONTAINER_ENGINE=podman already set" {
  export CONTAINER_ENGINE="podman"
  export COMPOSE_CMD="my-custom-compose"
  detect_runtime
  [ "$COMPOSE_CMD" = "my-custom-compose" ]
}

@test "detect_runtime: preserves existing COMPOSE_CMD when CONTAINER_ENGINE=docker already set" {
  export CONTAINER_ENGINE="docker"
  export COMPOSE_CMD="my-custom-compose"
  detect_runtime
  [ "$COMPOSE_CMD" = "my-custom-compose" ]
}

@test "detect_runtime: errors on unknown CONTAINER_ENGINE value" {
  # error() writes to stderr and exits 1; redirect stderr so run captures it.
  run bash -c "source '${BATS_TEST_DIRNAME}/../lib/common.sh' && CONTAINER_ENGINE=runc COMPOSE_CMD= detect_runtime 2>&1"
  [ "$status" -ne 0 ]
  [[ "$output" =~ "Unknown CONTAINER_ENGINE" ]]
}

# ── info/warn ───────────────────────────────────────────────────────

@test "info: outputs timestamped message" {
  result=$(info "test message")
  [[ "$result" =~ "test message" ]]
}

@test "warn: outputs WARN to stderr" {
  run bash -c "source '${BATS_TEST_DIRNAME}/../lib/common.sh' && warn 'test warning' 2>&1 >/dev/null"
  [[ "$output" =~ "WARN" ]]
}

# ── compose (local-dev overlay) ─────────────────────────────────────

@test "compose: overlay appended only when LOCAL_MODE=true AND compose.local.yml exists" {
  cd "$TEST_DIR"
  touch compose.yml
  # Stub the runtime so compose() echoes its argv instead of invoking an engine
  detect_runtime() { COMPOSE_CMD="echo"; }

  # No overlay file, no LOCAL_MODE -> base file only
  run compose up -d
  [ "$output" = "-f compose.yml up -d" ]

  # Overlay present but LOCAL_MODE unset -> still base only (prod behavior)
  touch compose.local.yml
  run compose up -d
  [ "$output" = "-f compose.yml up -d" ]

  # LOCAL_MODE=true + overlay present -> overlay appended
  export LOCAL_MODE=true
  run compose up -d
  [ "$output" = "-f compose.yml -f compose.local.yml up -d" ]

  # LOCAL_MODE=true but overlay absent -> base only
  rm compose.local.yml
  run compose up -d
  [ "$output" = "-f compose.yml up -d" ]
  unset LOCAL_MODE
}
