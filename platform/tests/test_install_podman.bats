#!/usr/bin/env bats
# Guards the removal recorded in docs/MISTAKES.md: an earlier version of the
# podman install configured a rootless API socket and relied on
# `systemctl --machine`, both built from an assumption about how a consumer
# invokes podman rather than from reading how it actually does. Nothing used
# either, and both dragged in their own requirements.
#
# This asserts the speculation cannot return without a deliberate, reviewable
# change. It does NOT assert podman is installed a particular way — only that
# these two mechanisms are absent.
#
# Run: bats platform/tests/test_install_podman.bats

load assert_helpers

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  PB="$REPO_ROOT/platform/playbooks/install-podman.yml"
}

@test "install-podman: exists and is a playbook" {
  [ -f "$PB" ]
  assert_grep -qE '^- name:' "$PB"
}

@test "install-podman: configures no podman API socket" {
  # The socket was added speculatively for a consumer that turned out to talk to
  # podman by CLI. Enabling it means a listening socket on every service host.
  refute_grep -qE 'podman\.socket|podman\.sock|podman[- ]system[- ]service' "$PB"
}

@test "install-podman: does not depend on systemctl --machine" {
  # `systemctl --machine=<user>@` needs a working user D-Bus, which is exactly
  # what is absent on a freshly provisioned host, so it failed where it was most
  # needed. Linger is handled by tasks/enable-linger.yml instead.
  refute_grep -qE 'systemctl .*--machine' "$PB"
}
