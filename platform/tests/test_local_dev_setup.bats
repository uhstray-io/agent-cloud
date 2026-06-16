#!/usr/bin/env bats
# Structural tests for local-dev setup files added in this PR:
#   - .gitignore (local-dev.yml entry)
#   - Brewfile (developer toolchain)
#
# Run: bats platform/tests/test_local_dev_setup.bats

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  GITIGNORE="$REPO_ROOT/.gitignore"
  BREWFILE="$REPO_ROOT/Brewfile"
}

# ── .gitignore — local-dev working inventory ──────────────────────────────────

@test ".gitignore: platform/inventory/local-dev.yml is gitignored (working copy must not leak)" {
  grep -q 'platform/inventory/local-dev.yml' "$GITIGNORE"
}

@test ".gitignore: platform/inventory/local-dev.yml.example is NOT gitignored (template must be committed)" {
  # The example is the committed source of truth; it must never be gitignored.
  ! grep -qE '^platform/inventory/local-dev\.yml\.example' "$GITIGNORE"
}

@test ".gitignore: local-dev.yml entry is active (not commented out)" {
  # A commented-out entry would silently allow the working inventory to be
  # committed. One anchored grep: optional leading whitespace then the path with
  # no leading '#'. The trailing (\s|$) avoids matching the .yml.example line.
  grep -qE '^[[:space:]]*platform/inventory/local-dev\.yml([[:space:]]|$)' "$GITIGNORE"
}

# NOTE: the generic secret-file patterns (secrets/, *.secret, *.key, *.pem, and
# runtime .env) are owned + asserted by test_credential_leaks.bats — not
# duplicated here (this file covers only the local-dev-specific entries).

# ── Brewfile — developer toolchain ───────────────────────────────────────────

@test "Brewfile: file exists at repo root" {
  [ -f "$BREWFILE" ]
}

@test "Brewfile: ansible is listed (required for playbooks)" {
  grep -qE '^brew "ansible"' "$BREWFILE"
}

@test "Brewfile: ansible-lint is listed (CI quality gate)" {
  grep -qE '^brew "ansible-lint"' "$BREWFILE"
}

@test "Brewfile: bats-core is listed (test runner)" {
  grep -qE '^brew "bats-core"' "$BREWFILE"
}

@test "Brewfile: jq is listed (required by bootstrap scripts)" {
  grep -qE '^brew "jq"' "$BREWFILE"
}

@test "Brewfile: podman is listed (primary container engine)" {
  grep -qE '^brew "podman"' "$BREWFILE"
}

@test "Brewfile: podman-compose is listed (compose wrapper)" {
  grep -qE '^brew "podman-compose"' "$BREWFILE"
}

@test "Brewfile: shellcheck is listed (shell lint in CI)" {
  grep -qE '^brew "shellcheck"' "$BREWFILE"
}

@test "Brewfile: socat is listed (privileged-port forwarder for make local-https)" {
  grep -qE '^brew "socat"' "$BREWFILE"
}

@test "Brewfile: yamllint is listed (YAML validation in CI)" {
  grep -qE '^brew "yamllint"' "$BREWFILE"
}

@test "Brewfile: gh is listed (used by promote workflow)" {
  grep -qE '^brew "gh"' "$BREWFILE"
}

@test "Brewfile: make is listed" {
  grep -qE '^brew "make"' "$BREWFILE"
}

@test "Brewfile: Docker Desktop is optional/commented, not a hard dependency" {
  # Docker Desktop requires root for some services and is NOT the default engine.
  # It must appear only in a comment/optional block, never as an uncommented brew.
  ! grep -qE '^brew "docker"' "$BREWFILE"
  ! grep -qE '^cask "docker"' "$BREWFILE"
}

@test "Brewfile: socat comment references make local-https (documents purpose)" {
  grep -q 'local-https' "$BREWFILE"
}