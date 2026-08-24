#!/usr/bin/env bats
# install-podman.yml — the removed speculation, and the opt-in Docker compatibility.
#
# Two concerns, merged from both sides of a branch that changed this file:
#
# 1. Guards a removal recorded in docs/MISTAKES.md: an earlier version configured a
#    rootless API socket and relied on `systemctl --machine`, both built from an
#    assumption about how a consumer invokes podman rather than from reading how it
#    actually does. Nothing used either, and both dragged in their own requirements.
#
# 2. Guards the opt-in Docker CLI shim. Some consumers shell out to a `docker` BINARY
#    rather than talking to the container API — the GitHub Actions runner container hooks
#    invoke `exec.getExecOutput('docker', args)` — so rootless podman alone is not enough
#    on a CI runner host. Both options must default OFF, so a host that already runs this
#    playbook installs nothing new.
#
# Negative assertions use refute_grep, never `! grep`: on Bats 1.13.0 a `!`-inverted
# command anywhere but the final statement of a body leaves the test PASSING, which makes
# a "must NOT contain" assertion decoration. See assert_helpers.bash.
#
# Run: bats platform/tests/test_install_podman.bats

load assert_helpers

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  PB="$REPO_ROOT/platform/playbooks/install-podman.yml"
  LINGER="$REPO_ROOT/platform/playbooks/tasks/enable-linger.yml"
  WAIT="$REPO_ROOT/platform/playbooks/tasks/wait-for-apt.yml"
  [ -f "$PB" ]
  [ -f "$LINGER" ]
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

@test "install-podman: the Docker CLI shim is opt-in and defaults OFF" {
  assert_grep -qF '_docker_cli: "{{ podman_docker_cli | default(false) | bool }}"' "$PB"
  # Every compat task gated on it, so an existing host installs nothing new.
  local total gated
  total=$(grep -c 'podman-docker\|/etc/containers/nodocker\|/usr/bin/docker' "$PB")
  gated=$(grep -c 'when: _docker_cli' "$PB")
  [ "$total" -gt 0 ]
  [ "$gated" -ge 3 ]
}

@test "install-podman: the shim package and its banner marker are both handled" {
  assert_grep -qF 'name: podman-docker' "$PB"
  assert_grep -qF 'path: /etc/containers/nodocker' "$PB"
}

@test "install-podman: the banner marker touch is idempotent" {
  # A bare `state: touch` reports changed on every run, turning an idempotent playbook
  # into a noisy one and hiding real changes.
  assert_grep -qF 'modification_time: preserve' "$PB"
  assert_grep -qF 'access_time: preserve' "$PB"
}

@test "install-podman: verification checks presence, not a rootful engine call" {
  # `docker ps` as root would initialise ROOTFUL podman state on a rootless host and
  # would prove nothing about the account that actually calls the shim.
  assert_grep -qF 'path: /usr/bin/docker' "$PB"
  refute_grep -qE 'ansible\.builtin\.command:\s*"docker ps"' "$PB"
}

@test "install-podman: waits for a freshly-booted host before installing" {
  # A just-provisioned VM is still running cloud-init, which holds the dpkg lock. An
  # install issued immediately after provisioning fails on that lock — a transient race
  # that reads as a broken playbook.
  assert_grep -qF 'tasks/wait-for-apt.yml' "$PB"
  local wait_line inst_line
  wait_line=$(grep -n 'wait-for-apt.yml' "$PB" | head -1 | cut -d: -f1)
  inst_line=$(grep -n 'name: podman-docker\|- podman$' "$PB" | head -1 | cut -d: -f1)
  [ "$wait_line" -lt "$inst_line" ]
  # The wait must not depend on a package that may be absent on a fresh host.
  assert_grep -qF 'apt-get check' "$WAIT"
  refute_grep -qE '^[^#]*(fuser|lsof)' "$WAIT"
}

@test "enable-linger: accepts an explicit linger_user, defaulting to ansible_user" {
  # A CI runner runs under a DEDICATED unprivileged account, because ansible_user holds
  # NOPASSWD sudo after hardening and the runner must hold none. Existing callers pass
  # nothing and keep lingering ansible_user.
  assert_grep -qF 'linger_user | default(ansible_user)' "$LINGER"
  refute_grep -qE 'loginctl (show-user|enable-linger) \{\{ ansible_user \}\}' "$LINGER"
}
