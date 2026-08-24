#!/usr/bin/env bats
# Structural tests for install-podman.yml — Podman + optional Docker compatibility.
#
# The Docker-compat option exists because some consumers shell out to a `docker`
# BINARY rather than talking to the container API. The GitHub Actions runner container
# hooks are the motivating case: they invoke `exec.getExecOutput('docker', args)`, so a
# plain rootless Podman install is not sufficient on a CI runner host.
#
# Both options must default OFF, so every host that already runs this playbook is
# unaffected — that is the regression these tests exist to hold.
#
# Run: bats platform/tests/test_install_podman.bats

setup() {
  PLAYBOOK="$BATS_TEST_DIRNAME/../playbooks/install-podman.yml"
  LINGER="$BATS_TEST_DIRNAME/../playbooks/tasks/enable-linger.yml"
  [ -f "$PLAYBOOK" ]
  [ -f "$LINGER" ]
}

@test "install-podman: the Docker CLI shim is opt-in and defaults OFF" {
  grep -qF '_docker_cli: "{{ podman_docker_cli | default(false) | bool }}"' "$PLAYBOOK"
  # Every compat task must be gated on it, so an existing host installs nothing new.
  local gated total
  total=$(grep -c 'podman-docker\|/etc/containers/nodocker\|/usr/bin/docker' "$PLAYBOOK")
  gated=$(grep -c 'when: _docker_cli' "$PLAYBOOK")
  [ "$total" -gt 0 ]
  [ "$gated" -ge 3 ]
}

@test "install-podman: the shim package and its banner marker are both handled" {
  grep -qF 'name: podman-docker' "$PLAYBOOK"
  grep -qF 'path: /etc/containers/nodocker' "$PLAYBOOK"
}

@test "install-podman: the banner marker touch is idempotent" {
  # A bare `state: touch` reports changed on every run, which turns an idempotent
  # playbook into a noisy one and hides real changes.
  grep -qF 'modification_time: preserve' "$PLAYBOOK"
  grep -qF 'access_time: preserve' "$PLAYBOOK"
}

@test "install-podman: verification checks presence, not a rootful engine call" {
  # `docker ps` as root would initialise ROOTFUL podman state on a rootless host and
  # would prove nothing about the account that actually calls the shim.
  grep -qF 'path: /usr/bin/docker' "$PLAYBOOK"
  ! grep -qE 'ansible\.builtin\.command:\s*"docker ps"' "$PLAYBOOK"
}

@test "install-podman: no speculative API socket is configured" {
  # The consumer shells out to a docker BINARY and never opens the API socket, so no
  # socket is enabled here — and in particular nothing depends on `systemctl
  # --machine`, which would pull in an undeclared systemd-container dependency.
  ! grep -q 'podman_socket_user' "$PLAYBOOK"
  ! grep -q 'podman.socket' "$PLAYBOOK"
  ! grep -q -- '--machine=' "$PLAYBOOK"
}

@test "enable-linger: accepts an explicit linger_user, defaulting to ansible_user" {
  # A CI runner must run under a DEDICATED unprivileged account, because ansible_user
  # holds NOPASSWD sudo after hardening and the runner must hold none. Existing callers
  # pass nothing and keep lingering ansible_user.
  grep -qF 'linger_user | default(ansible_user)' "$LINGER"
  # No occurrence of the bare ansible_user form may remain in the loginctl calls.
  ! grep -qE 'loginctl (show-user|enable-linger) \{\{ ansible_user \}\}' "$LINGER"
}

@test "install-podman: waits for a freshly-booted host before installing" {
  # A just-provisioned VM is still running cloud-init, which holds the dpkg lock. Any
  # install issued immediately after provisioning fails on that lock — a transient race
  # that reads as a broken playbook.
  grep -qF 'tasks/wait-for-apt.yml' "$PLAYBOOK"
  local wait_line inst_line
  wait_line=$(grep -n 'wait-for-apt.yml' "$PLAYBOOK" | head -1 | cut -d: -f1)
  inst_line=$(grep -n 'name: podman-docker\|- podman$' "$PLAYBOOK" | head -1 | cut -d: -f1)
  [ "$wait_line" -lt "$inst_line" ]
  # And the wait must not depend on a package that may be absent.
  local W="$BATS_TEST_DIRNAME/../playbooks/tasks/wait-for-apt.yml"
  grep -qF 'apt-get check' "$W"
  # Executable lines only. The rationale NAMES fuser and lsof to explain why they are
  # avoided, and a check that forbids naming the hazard suppresses the comment that
  # documents it — the third time this exact over-strictness has bitten in this change.
  ! grep -vE '^[[:space:]]*#' "$W" | grep -qE '(fuser|lsof)'
}
