#!/usr/bin/env bats
# Structural tests for the github-runner service — install/lifecycle + isolation config.
#
# The properties held here are the ones whose failure is invisible until a live job runs
# on a hardened host: a token in argv, isolation that a workflow can opt out of, a
# cleanup hook that fails the job it was meant to tidy up after, or a version pin that
# a self-update quietly undoes.
#
# Run: bats platform/tests/test_service_github_runner.bats

load assert_helpers

setup() {
  D="$BATS_TEST_DIRNAME/../services/github-runner/deployment"
  SH="$D/deploy.sh"
  [ -f "$SH" ]
}

@test "gh-runner: deploy.sh is executable, bash, and sources the shared lib" {
  [ -x "$SH" ]
  head -1 "$SH" | grep -q 'bash'
  grep -qF 'lib/common.sh' "$SH"
}

@test "gh-runner: deploy.sh is lifecycle-only — no secret generation or OpenBao" {
  # Same division every other service keeps: Ansible owns the credential lifecycle,
  # the script owns the process.
  #
  # Checked against EXECUTABLE lines only. The header comment names OpenBao precisely
  # to explain that division, and a check that forbids describing the boundary
  # discourages the comment that makes the boundary legible.
  local code
  code=$(grep -vE '^\s*#' "$SH")
  ! printf '%s' "$code" | grep -qiE 'openbao|bao_client|hashi_vault|gen_secret|put_secret'
}

@test "gh-runner: the registration token never reaches argv" {
  # argv is world-readable through /proc for the life of the process, so --token would
  # expose a live registration credential to every local user. The runner reads
  # ACTIONS_RUNNER_INPUT_<ARG> from the environment and deletes the variable after
  # reading it (Runner.Listener/CommandSettings.cs) — that is the argv-free route, and
  # there is no --token-file option.
  grep -qF 'ACTIONS_RUNNER_INPUT_TOKEN="$token" ./config.sh' "$SH"
  # CODE only: the script's comments name --token to explain why it is NOT used, and
  # forbidding the explanation suppresses it (docs/MISTAKES.md §2.8).
  grep -vE '^[[:space:]]*#' "$SH" > "$BATS_TEST_TMPDIR/code.sh"
  refute_grep -qE '\-\-token[= ]' "$BATS_TEST_TMPDIR/code.sh"
  # And it arrives on stdin, not as a script argument.
  grep -qF 'token=$(cat)' "$SH"
}

@test "gh-runner: the archive digest is verified before anything is executed" {
  grep -qF 'verify_digest' "$SH"
  # A mismatch must delete the artefact and abort, not warn and continue.
  grep -qF 'digest mismatch' "$SH"
  local fn
  fn=$(sed -n '/^verify_digest() {$/,/^}$/p' "$SH")
  assert_contains "$fn" 'rm -f "$file"'
  assert_contains "$fn" 'error "digest mismatch'
  # An absent digest is a refusal, not a skip — "no digest" and "unverified" are the
  # same state from here.
  grep -qF 'refusing to run an unverified artefact' "$SH"
}

@test "gh-runner: the digest is fetched per pinned version, never hardcoded" {
  # A constant in the repo goes stale the first time the pin moves, and a
  # stale-but-present digest reads as verified when it is not.
  refute_grep -qE '^[A-Z_]*SHA256=[0-9a-f]{64}' "$SH"
  grep -qF 'RUNNER_SHA256' "$D/templates/runner.env.j2"
  grep -qF '{{ _runner_sha256 }}' "$D/templates/runner.env.j2"
}

@test "gh-runner: re-running configure leaves an existing registration intact" {
  grep -qF 'already configured — leaving registration intact' "$SH"
}

@test "gh-runner: the version pin cannot be undone by a self-update" {
  grep -qF -- '--disableupdate' "$SH"
  # And a rebuilt host reclaims its own name instead of leaving a second offline entry.
  grep -qF -- '--replace' "$SH"
}

@test "gh-runner: a runner can be withdrawn without destroying its host" {
  grep -qF 'cmd_remove()' "$SH"
  grep -qF 'de-registered' "$SH"
  grep -qF 'host left intact' "$SH"
}

@test "gh-runner: isolation is host-configured, not workflow-requested" {
  # A workflow-level container declaration is opt-in, and an opt-in isolation boundary
  # fails open on the first job that omits it.
  grep -qF 'ACTIONS_RUNNER_CONTAINER_HOOKS=' "$D/templates/runner-dotenv.j2"
  grep -qF 'ACTIONS_RUNNER_HOOK_JOB_COMPLETED=' "$D/templates/runner-dotenv.j2"
}

@test "gh-runner: the cleanup hook can never fail the job it cleans up after" {
  # A non-zero exit from a job-completed hook marks the job failed, which turns a
  # cleanup hiccup into a red build and teaches people to disable the hook.
  local H="$D/templates/job-cleanup.sh.j2"
  grep -qF 'exit 0' "$H"
  refute_grep -qE '^set -e' "$H"
  # Every destructive step tolerates failure and says so.
  grep -qF '|| log "WARNING' "$H"
}

@test "gh-runner: cleanup clears repository content but keeps the runner's own caches" {
  # Deleting _tool/_temp/_actions every job makes each build slower for no isolation
  # gain — they hold no repository content.
  local H="$D/templates/job-cleanup.sh.j2"
  grep -qF "! -name '_tool'" "$H"
  grep -qF "! -name '_temp'" "$H"
  grep -qF 'rm -rf' "$H"
}

@test "gh-runner: the unit restarts the runner and forbids privilege escalation" {
  local U="$D/templates/actions-runner.service.j2"
  grep -qF 'Restart=always' "$U"
  grep -qF 'NoNewPrivileges=true' "$U"
  # An in-flight job must not be severed mid-build.
  grep -qF 'TimeoutStopSec=5min' "$U"
}

@test "gh-runner: no templates carry real addresses or a literal token" {
  refute_grep -rqE '(192\.168\.[0-9]+\.[0-9]+|10\.[0-9]+\.[0-9]+\.[0-9]+|172\.(1[6-9]|2[0-9]|3[01])\.[0-9]+\.[0-9]+)' "$D"
  ! grep -rqiE '(gh[pousr]_[A-Za-z0-9]{16}|A[A-Z0-9]{20,})' "$D/templates"
}

@test "gh-runner: the deploy proves rootless podman maps a uid range for the runner account" {
  # Container jobs run through the hooks as the runner account. Without newuidmap or a
  # subuid range every one of them fails, and the deploy used to report success anyway.
  local PB="$BATS_TEST_DIRNAME/../playbooks/deploy-github-runner.yml"
  assert_grep -qF 'podman unshare cat /proc/self/uid_map' "$PB"
  assert_grep -qF 'podman system migrate' "$PB"
  assert_grep -qF '(_uidmap.stdout_lines | default([]) | length) < 2' "$PB"
}

# ── install / upgrade, exercised ──────────────────────────────────────────────
# These run the real deploy.sh install against a sandboxed RUNNER_HOME. curl serves
# locally built releases whose bin/Runner.Listener is a script that reports its
# version, and systemctl records what it was asked to do. A grep for an upgrade
# function would pass for a function that never runs (docs/MISTAKES.md 2.13); these
# assert what the install leaves on disk and what it logs.

# Build a fake runner release $1 into the served directory. Its top level matches the
# real tarball's (2.336.0 and 2.338.0 ship bin/, externals/, config.sh, env.sh, run.sh,
# safe_sleep.sh and the run-helper templates). Extra entries can be added with $2.
_fake_runner_release() {
  local ver="$1" extra="${2:-}" src="$BATS_TEST_TMPDIR/src-$1"
  rm -rf "$src"
  mkdir -p "$src/bin" "$src/externals/node24"
  printf '#!/bin/sh\n[ "$1" = "--version" ] && echo "%s"\n' "${REPORTS:-$ver}" > "$src/bin/Runner.Listener"
  chmod +x "$src/bin/Runner.Listener"
  echo "$ver" > "$src/bin/release.txt"
  for f in config.sh env.sh run.sh safe_sleep.sh run-helper.sh.template run-helper.cmd.template; do
    printf '#!/bin/sh\n# %s %s\n' "$f" "$ver" > "$src/$f"
    chmod +x "$src/$f"
  done
  [ -z "$extra" ] || echo "shipped by the release" > "$src/$extra"
  tar czf "$SERVE/actions-runner-linux-x64-${ver}.tar.gz" -C "$src" .
}

_fake_hooks_release() {
  local ver="$1" src="$BATS_TEST_TMPDIR/hooks-$1"
  rm -rf "$src"
  mkdir -p "$src"
  echo "// hooks $ver" > "$src/index.js"
  (cd "$src" && zip -q "$SERVE/actions-runner-hooks-docker-${ver}.zip" index.js)
}

_sha() { sha256sum "$1" | cut -d' ' -f1; }

# Write runner.env for the declared versions, with the served artefacts' real digests.
_declare() {
  local rver="$1" hver="$2"
  cat > "$ENVF" <<EOF
RUNNER_HOME=$RH
RUNNER_NAME=gh-runner-test
RUNNER_ORG_URL=https://github.com/example-org
RUNNER_GROUP=test-group
RUNNER_LABELS=self-hosted,test
RUNNER_VERSION=$rver
HOOKS_VERSION=$hver
RUNNER_SHA256=$(_sha "$SERVE/actions-runner-linux-x64-${rver}.tar.gz")
HOOKS_SHA256=$(_sha "$SERVE/actions-runner-hooks-docker-${hver}.zip")
EOF
}

_install() {
  run env -u XDG_RUNTIME_DIR PATH="$STUBS:$PATH" RUNNER_ENV_FILE="$ENVF" RUNNER_STOP_WAIT=1 "$@" "$SH" install
}

# A configured runner at version $1 with hooks $2: what an existing host looks like.
_existing_host() {
  _fake_runner_release "$1"
  _fake_hooks_release "$2"
  _declare "$1" "$2"
  _install
  [ "$status" -eq 0 ]
  for f in .runner .credentials .credentials_rsaparams .env .path; do
    echo "registration $f" > "$RH/$f"
  done
  mkdir -p "$RH/config" "$RH/_work/repo"
  echo "declared" > "$RH/config/runner.env.keep"
  echo "job" > "$RH/_work/repo/file"
  : > "$CALLS"
}

_setup_sandbox() {
  RH="$BATS_TEST_TMPDIR/home"
  SERVE="$BATS_TEST_TMPDIR/serve"
  STUBS="$BATS_TEST_TMPDIR/stubs"
  ENVF="$BATS_TEST_TMPDIR/runner.env"
  CALLS="$BATS_TEST_TMPDIR/calls"
  mkdir -p "$RH" "$SERVE" "$STUBS"
  : > "$CALLS"
  # curl -fsSL --retry 3 --retry-delay 2 -o DEST URL: serve URL's basename locally.
  cat > "$STUBS/curl" <<EOF
#!/usr/bin/env bash
dest=""; url=""
while [ \$# -gt 0 ]; do
  case "\$1" in -o) dest="\$2"; shift 2 ;; --retry|--retry-delay) shift 2 ;; -*) shift ;; *) url="\$1"; shift ;; esac
done
echo "curl \$url" >> "$CALLS"
cp "$SERVE/\$(basename "\$url")" "\$dest"
EOF
  cat > "$STUBS/systemctl" <<EOF
#!/usr/bin/env bash
echo "systemctl \$* XDG_RUNTIME_DIR=\${XDG_RUNTIME_DIR:-}" >> "$CALLS"
EOF
  # No runner process is alive in the sandbox unless a test says so.
  cat > "$STUBS/pgrep" <<EOF
#!/usr/bin/env bash
[ -f "$BATS_TEST_TMPDIR/alive" ] && cat "$BATS_TEST_TMPDIR/alive"
exit 0
EOF
  chmod +x "$STUBS"/*
  # macOS has shasum, not sha256sum; CI (Ubuntu) has both.
  if ! command -v sha256sum >/dev/null 2>&1; then
    printf '#!/bin/sh\nexec shasum -a 256 "$@"\n' > "$STUBS/sha256sum"
    chmod +x "$STUBS/sha256sum"
    PATH="$STUBS:$PATH"
  fi
}

@test "gh-runner: a fresh install reports the version it actually installed" {
  _setup_sandbox
  _fake_runner_release 2.338.0
  _fake_hooks_release 0.8.1
  _declare 2.338.0 0.8.1
  _install
  [ "$status" -eq 0 ]
  assert_contains "$output" "installed runner 2.338.0"
  assert_contains "$output" "installed container hooks 0.8.1"
  [ "$("$RH/bin/Runner.Listener" --version)" = "2.338.0" ]
  [ "$(cat "$RH/_hooks/.hooks-version")" = "0.8.1" ]
  # Nothing was serving, so nothing was stopped or started.
  refute_grep -q 'systemctl' "$CALLS"
}

@test "gh-runner: the same declared version is a no-op" {
  _setup_sandbox
  _existing_host 2.338.0 0.8.1
  _install
  [ "$status" -eq 0 ]
  assert_contains "$output" "runner 2.338.0 already installed"
  assert_contains "$output" "container hooks 0.8.1 already installed"
  refute_contains "$output" "installed runner"
  refute_contains "$output" "upgraded"
  # No download, no service stop: the steady state does nothing at all.
  [ ! -s "$CALLS" ]
}

@test "gh-runner: a raised pin upgrades the runner and keeps every registration file" {
  _setup_sandbox
  _existing_host 2.336.0 0.8.1
  local before
  before=$(cd "$RH" && cat .runner .credentials .credentials_rsaparams .env .path config/runner.env.keep _work/repo/file)
  _fake_runner_release 2.338.0
  _declare 2.338.0 0.8.1
  _install
  [ "$status" -eq 0 ]
  assert_contains "$output" "upgraded runner 2.336.0 -> 2.338.0"
  [ "$("$RH/bin/Runner.Listener" --version)" = "2.338.0" ]
  [ "$(cat "$RH/bin/release.txt")" = "2.338.0" ]
  # The registration, the rendered config and the job workspace are byte-identical.
  [ "$(cd "$RH" && cat .runner .credentials .credentials_rsaparams .env .path config/runner.env.keep _work/repo/file)" = "$before" ]
  # Stopped before the swap and started after it, on the account's own user bus.
  assert_grep -q '^systemctl --user stop actions-runner.service XDG_RUNTIME_DIR=/run/user/' "$CALLS"
  assert_grep -q '^systemctl --user start actions-runner.service' "$CALLS"
  [ "$(grep -n 'systemctl --user stop' "$CALLS" | cut -d: -f1)" -lt "$(grep -n 'systemctl --user start' "$CALLS" | cut -d: -f1)" ]
  # No staging or rollback copies, and no stale tarball, are left behind.
  [ ! -e "$RH/_previous" ]
  [ ! -e "$RH/_install.2.338.0" ]
  [ ! -e "$RH/actions-runner-linux-x64-2.336.0.tar.gz" ]
}

@test "gh-runner: a lower declared version is refused unless the downgrade is explicit" {
  _setup_sandbox
  _existing_host 2.338.0 0.8.1
  _fake_runner_release 2.336.0
  _declare 2.336.0 0.8.1
  _install
  [ "$status" -ne 0 ]
  assert_contains "$output" "refusing to downgrade runner from 2.338.0 to 2.336.0"
  refute_contains "$output" "installed runner"
  # Refused before anything was fetched or stopped; the install is untouched.
  [ ! -s "$CALLS" ]
  [ "$("$RH/bin/Runner.Listener" --version)" = "2.338.0" ]

  _install RUNNER_ALLOW_DOWNGRADE=1
  [ "$status" -eq 0 ]
  assert_contains "$output" "downgrading runner 2.338.0 -> 2.336.0"
  [ "$("$RH/bin/Runner.Listener" --version)" = "2.336.0" ]
  [ "$(cat "$RH/.credentials")" = "registration .credentials" ]
}

@test "gh-runner: the container hooks upgrade, no-op and refuse a downgrade the same way" {
  _setup_sandbox
  _existing_host 2.338.0 0.8.0
  _fake_hooks_release 0.8.1
  _declare 2.338.0 0.8.1
  _install
  [ "$status" -eq 0 ]
  assert_contains "$output" "upgraded container hooks 0.8.0 -> 0.8.1"
  assert_grep -qF '// hooks 0.8.1' "$RH/_hooks/index.js"
  [ ! -e "$RH/actions-runner-hooks-docker-0.8.0.zip" ]
  _declare 2.338.0 0.8.0
  _install
  [ "$status" -ne 0 ]
  assert_contains "$output" "refusing to downgrade container hooks from 0.8.1 to 0.8.0"
  assert_grep -qF '// hooks 0.8.1' "$RH/_hooks/index.js"
}

@test "gh-runner: a bad digest on upgrade leaves the running install alone" {
  _setup_sandbox
  _existing_host 2.336.0 0.8.1
  _fake_runner_release 2.338.0
  _declare 2.338.0 0.8.1
  sed -i.bak 's/^RUNNER_SHA256=.*/RUNNER_SHA256=0000000000000000000000000000000000000000000000000000000000000000/' "$ENVF"
  _install
  [ "$status" -ne 0 ]
  assert_contains "$output" "digest mismatch"
  refute_grep -q 'systemctl' "$CALLS"
  [ "$("$RH/bin/Runner.Listener" --version)" = "2.336.0" ]
}

@test "gh-runner: a release reporting another version is never logged as installed" {
  _setup_sandbox
  _existing_host 2.336.0 0.8.1
  REPORTS=2.337.0 _fake_runner_release 2.338.0
  _declare 2.338.0 0.8.1
  _install
  [ "$status" -ne 0 ]
  assert_contains "$output" "contains runner '2.337.0', not the declared 2.338.0"
  refute_contains "$output" "installed runner"
  refute_contains "$output" "upgraded runner"
  refute_grep -q 'systemctl' "$CALLS"
  [ "$("$RH/bin/Runner.Listener" --version)" = "2.336.0" ]
}

@test "gh-runner: a release that ships a host-owned file is refused" {
  _setup_sandbox
  _existing_host 2.336.0 0.8.1
  _fake_runner_release 2.338.0 .credentials
  _declare 2.338.0 0.8.1
  _install
  [ "$status" -ne 0 ]
  assert_contains "$output" "ships .credentials, which this host owns"
  [ "$(cat "$RH/.credentials")" = "registration .credentials" ]
  [ "$("$RH/bin/Runner.Listener" --version)" = "2.336.0" ]
}

@test "gh-runner: an upgrade refuses to swap binaries under a runner that will not stop" {
  _setup_sandbox
  _existing_host 2.336.0 0.8.1
  _fake_runner_release 2.338.0
  _declare 2.338.0 0.8.1
  echo 2147483646 > "$BATS_TEST_TMPDIR/alive"
  _install
  [ "$status" -ne 0 ]
  assert_contains "$output" "refusing to swap binaries under them"
  refute_grep -q 'systemctl --user start' "$CALLS"
  [ "$("$RH/bin/Runner.Listener" --version)" = "2.336.0" ]
}

@test "gh-runner: the playbook pins 2.338.0 and gates on the installed version" {
  local PB="$BATS_TEST_DIRNAME/../playbooks/deploy-github-runner.yml"
  assert_grep -qF "github_runner_version | default('2.338.0')" "$PB"
  # The install runs with the user bus it needs to stop and start the service.
  task_block "$PB" "Install and register the runner" > "$BATS_TEST_TMPDIR/task"
  assert_grep -qF 'XDG_RUNTIME_DIR: "/run/user/{{ _runner_uid }}"' "$BATS_TEST_TMPDIR/task"
  assert_grep -qF 'RUNNER_ALLOW_DOWNGRADE:' "$BATS_TEST_TMPDIR/task"
  assert_precedes "$PB" "Resolve the runner account's uid" "Install and register the runner"
  # The report states the version the binary reports, not the pin.
  assert_grep -qF '{{ _home }}/bin/Runner.Listener --version' "$PB"
  assert_grep -qF "(_installed_version.stdout | default('') | trim) != _rver" "$PB"
  assert_grep -qF 'runner v{{ _installed_version.stdout | trim }}' "$PB"
}
