#!/usr/bin/env bats
# A play that escalates at PLAY level dies at "Gathering Facts" with ok=0 on any
# host that still requires a sudo password — the become plugin raises before any
# module runs, so no per-task `failed_when` can catch it.
#
# That is precisely the state of a host these playbooks exist to PREPARE. Measured
# 2026-08-25: install-podman.yml failed that way against a freshly keyed host, and
# of the six playbooks escalating at play level only harden-ssh.yml resolved the
# password.

load assert_helpers

setup() {
  PBDIR="${BATS_TEST_DIRNAME}/../playbooks"
  TASK="${PBDIR}/tasks/resolve-become-password.yml"
  # deploy-github-runner.yml is a KNOWN, DELIBERATE gap: it belongs to work in
  # progress on another branch and is not this change's to edit. Recorded here so
  # the gap is visible rather than silently excluded.
  KNOWN_GAP="deploy-github-runner.yml"
}

@test "become: the shared resolver task exists and does not itself need escalation" {
  [ -f "$TASK" ]
  # If the fetch escalated, it could not run on the host it is enabling.
  assert_grep -qE 'become: false' "$TASK"
  assert_grep -qE 'no_log: true' "$TASK"
}

@test "become: the resolver reads the same secret path harden-ssh does" {
  # A disagreement here lets a host be prepared by one path and then fail to
  # harden by another — the two must name one location.
  local hs="${PBDIR}/harden-ssh.yml"
  [ -f "$hs" ]
  assert_grep -q 'secret/data/services/ssh:become_password' "$TASK"
  assert_grep -q 'secret/data/services/ssh:become_password' "$hs"
}

@test "become: every play-level-become playbook resolves a sudo password" {
  local missing=""
  for f in "$PBDIR"/*.yml; do
    grep -qE '^  become: true' "$f" || continue
    local base; base=$(basename "$f")
    [ "$base" = "$KNOWN_GAP" ] && continue
    if ! grep -qE 'resolve-become-password|become_password' "$f"; then
      missing="$missing $base"
    fi
  done
  if [ -n "$missing" ]; then
    echo "these escalate at play level yet resolve no sudo credential ->$missing" >&2
    echo "they will die at 'Gathering Facts' on any host that is not already hardened" >&2
    return 1
  fi
}

@test "become: the resolver runs FIRST in every play-level-become playbook" {
  # Play-level become escalates during fact gathering, so a resolver placed after
  # another task never runs — the play is already dead. Where escalation is
  # per-task instead (play-level become: false), ordering only has to precede the
  # escalating tasks, which each such playbook asserts for itself.
  for f in "$PBDIR"/*.yml; do
    grep -qE '^  become: true' "$f" || continue
    grep -q 'resolve-become-password' "$f" || continue
    local first
    first=$(awk '/^  tasks:/{f=1;next} f&&/^    - name:/{print;exit}' "$f")
    assert_contains "$first" 'Resolve the sudo password'
  done
}

@test "become: the known gap is still the only one, and is named" {
  # If someone fixes deploy-github-runner.yml, this test should start failing so
  # the exclusion gets deleted rather than outliving its reason.
  local f="${PBDIR}/${KNOWN_GAP}"
  if [ -f "$f" ] && grep -qE 'resolve-become-password|become_password' "$f"; then
    echo "$KNOWN_GAP now resolves a sudo password — remove it from KNOWN_GAP" >&2
    return 1
  fi
}
