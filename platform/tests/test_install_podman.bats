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
# 3. Guards the opt-in Docker CLI client (podman_docker_api_client): Docker's own client,
#    pinned, from Docker's repository, with no Docker engine, pointed by its consumer at a
#    podman API socket. It replaces the shim for the GitHub runner (design D4,
#    docs/MISTAKES.md 10.28), so the two are mutually exclusive.
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
  REPO_TASK="$REPO_ROOT/platform/playbooks/tasks/add-docker-apt-repo.yml"
  INSTALL_DOCKER="$REPO_ROOT/platform/playbooks/install-docker.yml"
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

@test "install-podman: the Docker CLI shim and the Docker CLI client are opt-in and default OFF" {
  assert_grep -qF '_docker_cli: "{{ podman_docker_cli | default(false) | bool }}"' "$PB"
  assert_grep -qF '_docker_api_client: "{{ podman_docker_api_client | default(false) | bool }}"' "$PB"

  # PER TASK, not whole-file counts. Two independent totals can agree while an ungated
  # task installs the shim — three other tasks carrying the guard would satisfy a count.
  # The script is written to a file rather than passed with -c: it contains quotes, and
  # escaping them through the shell is how the first attempt broke.
  cat > "$BATS_TEST_TMPDIR/gate.py" <<'PYSCRIPT'
import re, sys
src = open(sys.argv[1]).read()
# Only real TASKS. The first chunk is the play preamble, whose header comments name the
# shim while gating nothing — counting it reported a false ungated task.
tasks = [x for x in re.split(r'\n(?=    - name: )', src) if x.lstrip().startswith('- name:')]
# An assert or a debug changes nothing on the host, so it needs no gate: the guard that
# refuses both options and the closing report name both without acting on either.
touch = [x for x in tasks
         if re.search(r'podman-docker|/etc/containers/nodocker|/usr/bin/docker|docker-ce-cli|add-docker-apt-repo', x)
         and not re.search(r'ansible\.builtin\.(assert|debug):', x)]
if not touch:
    print('no task touches the docker shim at all'); sys.exit(0)
bad = []
for x in touch:
    m = re.search(r'- name: "([^"]+)"', x)
    # Each task is gated by one of the two opt-ins (`when: X` or a `when:` list naming X).
    if not re.search(r'when:\s*(?:\n\s*-\s*)?_docker_(?:cli|api_client)\b', x):
        bad.append(m.group(1) if m else '<unnamed>')
print('OK' if not bad else 'ungated compat tasks: ' + '; '.join(bad))
PYSCRIPT
  run python3 "$BATS_TEST_TMPDIR/gate.py" "$PB"
  [ "$status" -eq 0 ]
  [ "$output" = "OK" ]
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

@test "install-podman: installs uidmap, without which rootless podman cannot start a container" {
  # podman only RECOMMENDS uidmap. Both GitHub runners lacked it (2026-10-10), and every
  # job that declared a container failed on a missing newuidmap.
  assert_grep -qE '^\s+- uidmap$' "$PB"
}

@test "install-podman: the shim and the Docker CLI client are refused together" {
  # Both put a `docker` at /usr/bin/docker and their packages conflict; apt would keep
  # whichever task ran last.
  local t
  t=$(task_block "$PB" 'Refuse both Docker options at once')
  [ -n "$t" ]
  assert_contains "$t" 'that: not (_docker_cli and _docker_api_client)'
  refute_contains "$t" 'when:'
  assert_precedes "$PB" 'name: "Refuse both Docker options at once"' 'tasks/wait-for-apt.yml'
}

@test "install-podman: the Docker CLI client is pinned, client-only, and replaces the shim" {
  local rm inst
  rm=$(task_block "$PB" 'Remove the podman-docker shim, which the Docker CLI client replaces')
  [ -n "$rm" ]
  assert_contains "$rm" 'name: podman-docker'
  assert_contains "$rm" 'state: absent'
  assert_contains "$rm" 'when: _docker_api_client'
  inst=$(task_block "$PB" 'Install the pinned Docker CLI client, and nothing of the Docker engine')
  [ -n "$inst" ]
  # Pinned to a declared version, never `latest`; a drifted host is moved back.
  assert_contains "$inst" 'name: "docker-ce-cli=5:{{ _docker_api_client_version }}-*"'
  assert_contains "$inst" 'allow_downgrade: true'
  assert_contains "$inst" 'install_recommends: false'
  assert_grep -qF "_docker_api_client_version: \"{{ podman_docker_api_client_version | default('" "$PB"
  # No Docker engine: the owner wants podman as the engine. Nothing in this playbook's
  # code installs one, and the verify fails if one is present.
  grep -vE '^[[:space:]]*#' "$PB" > "$BATS_TEST_TMPDIR/code"
  refute_grep -qE '^[[:space:]]+-[[:space:]]+(docker-ce|containerd\.io|docker\.io)[[:space:]]*$' "$BATS_TEST_TMPDIR/code"
  refute_grep -qE 'name:[[:space:]]+"?(docker-ce|containerd\.io|docker\.io)"?[[:space:]]*$' "$BATS_TEST_TMPDIR/code"
  local v
  v=$(task_block "$PB" "Require Docker's CLI client at the pinned version, and no Docker engine")
  [ -n "$v" ]
  assert_contains "$v" "'owner=docker-ce-cli' in _docker_api_client_state.stdout_lines"
  assert_contains "$v" "select('match', '^engine=')"
  # The removal comes before the install, so the swap does not rest on apt's conflict
  # resolution.
  assert_precedes "$PB" 'name: "Remove the podman-docker shim' 'name: "Install the pinned Docker CLI client'
}

@test "docker apt repository: one shared task, used by both install playbooks" {
  [ -f "$REPO_TASK" ]
  assert_grep -qF 'signed-by=/etc/apt/keyrings/docker.asc' "$REPO_TASK"
  assert_grep -qF 'url: https://download.docker.com/linux/ubuntu/gpg' "$REPO_TASK"
  assert_grep -qF 'ansible.builtin.include_tasks: tasks/add-docker-apt-repo.yml' "$PB"
  assert_grep -qF 'ansible.builtin.include_tasks: tasks/add-docker-apt-repo.yml' "$INSTALL_DOCKER"
  # Declared once: neither playbook carries its own copy of the source line.
  refute_grep -qF 'download.docker.com' "$PB"
  refute_grep -qF 'download.docker.com' "$INSTALL_DOCKER"
}
