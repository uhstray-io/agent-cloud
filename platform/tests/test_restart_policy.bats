#!/usr/bin/env bats
# Containers must come back on their own after a host reboot.
#
# Podman has no daemon. At boot, podman-restart.service (system unit for rootful,
# user unit for rootless) runs `podman start --all --filter restart-policy=always`
# on podman 4.9.3, so it starts ONLY `restart: always` containers. A container
# declared `unless-stopped` stays down: production OpenBao sat stopped for three
# days after the 2026-09-19 reboot this way (docs/MISTAKES.md 10.15). Docker honours
# `always` too, so one policy is correct on both engines.
#
# Run: bats platform/tests/test_restart_policy.bats

load assert_helpers

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  LINGER="$REPO_ROOT/platform/playbooks/tasks/enable-linger.yml"
  PREAMBLE="$REPO_ROOT/platform/playbooks/tasks/place-monorepo.yml"
}

# Every compose file the platform deploys (not the archived plans).
compose_files() {
  git -C "$REPO_ROOT" ls-files -- 'platform/**/*compose*.yml' 'platform/**/*compose*.yaml' \
    'agents/**/*compose*.yml' 'agents/**/*compose*.yaml' | sed "s#^#$REPO_ROOT/#"
}

@test "restart policy: there are compose files to check" {
  [ "$(compose_files | wc -l)" -gt 10 ]
}

@test "restart policy: every declared policy is always or \"no\"" {
  # "no" is for one-shot init containers, which must not restart at all.
  local bad
  bad=$(compose_files | xargs grep -nE '^\s*restart:' \
    | grep -vE 'restart:\s*(always|"no"|'"'"'no'"'"')\s*(#.*)?$' || true)
  [ -z "$bad" ] || { echo "restart policy podman will not start at boot:"; echo "$bad"; false; }
}

@test "restart policy: no container is started with --restart unless-stopped" {
  local bad
  # This file names the flag in a test title, so it excludes itself.
  bad=$(git -C "$REPO_ROOT" grep -nE -- '--restart[= ]+unless-stopped' -- platform agents scripts \
    ':!platform/tests/test_restart_policy.bats' || true)
  [ -z "$bad" ] || { echo "$bad"; false; }
}

@test "restart policy: the shared preamble enables linger for every composable deploy" {
  assert_grep -qF 'include_tasks: enable-linger.yml' "$PREAMBLE"
}

@test "restart policy: linger task enables podman's user boot unit, not just linger" {
  # Linger alone starts an empty user manager: the unit ships disabled.
  assert_grep -qF 'src: /usr/lib/systemd/user/podman-restart.service' "$LINGER"
  assert_grep -qF '.config/systemd/user/default.target.wants' "$LINGER"
  assert_grep -qF 'dest: "{{ _unit_wants_dir }}/podman-restart.service"' "$LINGER"
  # Owned by the container user, not root, so their systemd reads it.
  assert_grep -qF 'become_user: "{{ linger_user | default(ansible_user) }}"' "$LINGER"
}
