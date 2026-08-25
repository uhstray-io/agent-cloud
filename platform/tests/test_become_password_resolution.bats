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

@test "become: automatic fact gathering is OFF wherever the resolver is used" {
  # THE ONE THAT MATTERS. Fact gathering runs BEFORE tasks, so a play with
  # `become: true` and gathering left on escalates before the resolver can run —
  # the play dies at "Gathering Facts" and the resolver is decoration.
  #
  # An earlier version of this suite asserted only that the resolver was the first
  # TASK. That assertion passes on exactly the broken configuration this one
  # catches: all three playbooks had gather_facts defaulting to true, so the fix
  # was inert and the tests were green. harden-ssh.yml works because it sets
  # gather_facts: false.
  local offenders=""
  for f in "$PBDIR"/*.yml; do
    grep -q 'resolve-become-password' "$f" || continue
    grep -qE '^  become: true' "$f" || continue
    grep -qE '^  gather_facts: (false|no)' "$f" || offenders="$offenders $(basename "$f")"
  done
  if [ -n "$offenders" ]; then
    echo "these escalate at play level and still gather facts automatically ->$offenders" >&2
    echo "the resolver cannot run before gathering, so it has no effect" >&2
    return 1
  fi
}

@test "become: the first task is what makes escalation possible" {
  # THE ORDERING INVARIANT. Play-level become escalates during fact gathering and
  # on the first privileged task, so whatever supplies the sudo password has to run
  # before either. "First task" is the only position that satisfies that.
  #
  # Two shapes are accepted because two exist: the shared resolver include, and
  # harden-ssh.yml's own inline fetch, which predates it. Both make escalation
  # possible; what is asserted is that one of them is FIRST.
  #
  # Selected on the CONDITION (escalates at play level), never on the presence of
  # the fix — selecting on the fix made this vacuous, because deleting the include
  # dropped the file out of scope and the test passed.
  for f in "$PBDIR"/*.yml; do
    grep -qE '^  become: true' "$f" || continue
    [ "$(basename "$f")" = "$KNOWN_GAP" ] && continue
    local first
    first=$(awk '/^  tasks:/{t=1;next} t&&/^    - name:/{n++; if(n>1) exit} t&&n==1{print}' "$f")
    [ -n "$first" ]
    if ! printf '%s' "$first" | grep -qE 'tasks/resolve-become-password\.yml|become_password'; then
      echo "first task of $(basename "$f") does not make escalation possible:" >&2
      printf '%s\n' "$first" >&2
      return 1
    fi
  done
}

@test "become: gathering, where it happens at all, happens after escalation is possible" {
  # Turning automatic gathering off without gathering explicitly would silently
  # remove facts the play depends on — so where a play gathers explicitly, that
  # task must come straight after the resolver, never before it.
  #
  # Conditional on the play HAVING a setup task: harden-ssh.yml never gathered
  # facts and needs none, so requiring one there would assert a shape rather than
  # a property.
  for f in "$PBDIR"/*.yml; do
    grep -qE '^  become: true' "$f" || continue
    [ "$(basename "$f")" = "$KNOWN_GAP" ] && continue
    grep -qE '^ +ansible\.builtin\.setup:' "$f" || continue
    local second
    second=$(awk '/^  tasks:/{t=1;next} t&&/^    - name:/{n++; if(n>2) exit} t&&n==2{print}' "$f")
    [ -n "$second" ]
    assert_grep -qE '^ +ansible\.builtin\.setup:' <<<"$second"
  done
}

@test "become: the resolver guards the transport it sends the credential over" {
  # The AppRole secret_id, the returned token and the sudo password all cross that
  # connection, so the shared cleartext guard applies here as it does to every
  # other play reaching the secret store.
  assert_grep -q 'tasks/assert-bao-transport.yml' "$TASK"
  # ...and it must come BEFORE the lookup it protects.
  local guard_line fetch_line
  guard_line=$(grep -n 'assert-bao-transport' "$TASK" | head -1 | cut -d: -f1)
  fetch_line=$(grep -n 'become_password' "$TASK" | head -1 | cut -d: -f1)
  [ -n "$guard_line" ] && [ -n "$fetch_line" ]
  [ "$guard_line" -lt "$fetch_line" ]
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
