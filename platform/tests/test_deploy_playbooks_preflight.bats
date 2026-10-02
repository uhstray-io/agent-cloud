#!/usr/bin/env bats
# Every deploy playbook that targets an inventory group refuses an empty one first.
#
# A play whose `hosts:` matches nothing prints "skipping: no hosts matched" and exits
# 0, so Semaphore records a deploy that did nothing as a success. It happened twice
# (docs/MISTAKES.md 2.21): deploy-postiz.yml on 2026-08-24 and deploy-agentgateway.yml
# on 2026-09-17, the second written by copying a deploy that had no guard. The guard
# exists (preflight-target-group.yml) but is opt-in per playbook, so this test derives
# the population from the playbooks themselves instead of naming a few.
#
# A deploy playbook is compliant when every play that targets something other than
# localhost is preceded by a pre-flight covering that target. Accepted forms, each
# taken from a playbook in this repository:
#   1. `import_playbook: preflight-target-group.yml` with `preflight_group` and
#      `preflight_group_expected` both set, non-empty and equal (deploy-postiz.yml,
#      deploy-agentgateway.yml, deploy-o11y.yml). It covers that exact group; a
#      `hosts: a:b` union needs both a and b covered.
#   2. An inline localhost play with an assert condition that reads `groups`, names the
#      target (its literal group, or the first Jinja variable of a templated target),
#      and is ONE comparison of the `length` that only a populated group passes: `> 0`,
#      `!= 0`, or `>=`/`==` a positive integer, with no `and`/`or`/`not`/`if`
#      (check-secrets.yml, verify-service-health.yml).
# An `import_playbook` of anything else is followed, so a thin wrapper over
# deploy-service.yml is judged by the plays it actually runs.
#
# A RATCHET (known_deploy_without_preflight.txt), not an allow-list: every offender
# must be named there, and every name there must still be an offender. It only shrinks.
#
# Run: bats platform/tests/test_deploy_playbooks_preflight.bats

load assert_helpers

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  PB="$REPO_ROOT/platform/playbooks"
  RATCHET="$REPO_ROOT/platform/tests/known_deploy_without_preflight.txt"
}

# _offenders <playbook dir> — one line per non-compliant deploy-*.yml:
#   <basename>\t<reason>
# The last line is `checked <n>`, the number of deploy playbooks that target a group.
_offenders() {
  PYTHONPATH="$REPO_ROOT/platform/tests" python3 - "$1" <<'PY'
import re
import sys
from pathlib import Path

import playbook_yaml

LOCAL = {"localhost", "127.0.0.1"}
IMPORT_KEYS = ("ansible.builtin.import_playbook", "import_playbook")
ASSERT_KEYS = ("ansible.builtin.assert", "assert")
PREFLIGHT = "preflight-target-group.yml"


def plays(path, seen):
    """The plays a playbook runs, in order, with non-preflight imports expanded."""
    if path in seen or not path.is_file():
        return
    seen = seen | {path}
    doc = playbook_yaml.loads(path.read_text())
    for play in doc if isinstance(doc, list) else []:
        if not isinstance(play, dict):
            continue
        target = next((play[k] for k in IMPORT_KEYS if k in play), None)
        if isinstance(target, str) and Path(target).name != PREFLIGHT:
            yield from plays(path.parent / target, seen)
        else:
            yield play


# A condition counts only if it is ONE comparison of a membership count that a populated
# group alone satisfies: `> 0`, `!= 0`, or `>=`/`==` a positive integer, written right
# after `length` (closing parens allowed). So `length >= 0`, `> -1`, a reversed operand
# order, a second comparison, or any `and`/`or`/`not`/`if` (which can neutralise or
# displace the comparison: `... | length > 0 or true`) is not recognised and fails safe.
POSITIVE = re.compile(r"\blength\b\s*\)*\s*(?:>\s*0(?![\d.])|!=\s*0(?![\d.])|(?:>=|==)\s*[1-9]\d*(?![\d.]))")
COMPARATOR = re.compile(r"[<>!=]=|[<>]")
NEUTRALISER = re.compile(r"\b(?:and|or|not|if|else)\b")


def inline_guard_items(play):
    """Each assert condition in a localhost play that requires a group to be populated."""
    out = []
    for task in playbook_yaml.tasks(play.get("tasks")):
        for key in ASSERT_KEYS:
            body = task.get(key)
            if not isinstance(body, dict):
                continue
            that = body.get("that")
            for item in that if isinstance(that, list) else [that]:
                if not isinstance(item, str):
                    continue
                if (
                    "groups" in item
                    and len(COMPARATOR.findall(item)) == 1
                    and POSITIVE.search(item)
                    and not NEUTRALISER.search(item)
                ):
                    out.append(item)
    return out


def parts(hosts):
    """The subjects a hosts value needs covered: each group of a literal union, or the
    whole expression when it is templated."""
    return [hosts] if "{{" in hosts else [p.strip() for p in hosts.split(":") if p.strip()]


def covered_inline(subject, texts):
    if "{{" in subject:
        m = re.search(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)", subject)
        name = m.group(1) if m else None
    else:
        name = subject
    return bool(name) and any(re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", t) for t in texts)


checked = 0
for path in sorted(Path(sys.argv[1]).glob("deploy-*.yml")):
    groups, inline, reasons, targets = set(), [], [], False
    for play in plays(path, frozenset()):
        target = next((play[k] for k in IMPORT_KEYS if k in play), None)
        if isinstance(target, str):  # only the pre-flight import reaches here
            v = play.get("vars") or {}
            g, e = v.get("preflight_group"), v.get("preflight_group_expected")
            if g is None or e is None or not str(g).strip() or str(g) != str(e):
                reasons.append("pre-flight import without preflight_group == preflight_group_expected")
            else:
                groups.add(str(g))
            continue
        hosts = play.get("hosts")
        if hosts is None:
            continue
        hosts = ",".join(hosts) if isinstance(hosts, list) else str(hosts)
        if hosts.strip() in LOCAL:
            inline.extend(inline_guard_items(play))
            continue
        targets = True
        for subject in parts(hosts):
            if subject not in groups and not covered_inline(subject, inline):
                reasons.append(f"play {play.get('name')!r} targets {subject!r} before any pre-flight covers it")
    if targets:
        checked += 1
    if reasons:
        print(f"{path.name}\t{reasons[0]}")
print(f"checked {checked}")
PY
}

# _compare <offender output> <ratchet file> — prints each disagreement, fails on any.
_compare() {
  local out="$1" ratchet="$2" pbdir="$3" bad="" base listed
  local names
  names=$(printf '%s\n' "$out" | grep -v '^checked ' | cut -f1)
  while IFS=$'\t' read -r base reason; do
    case "$base" in ''|checked\ *) continue ;; esac
    grep -qxF "$base" "$ratchet" || bad="${bad}NEW deploy playbook without a pre-flight: ${base} (${reason})"$'\n'
  done <<< "$out"
  while read -r listed; do
    case "$listed" in ''|'#'*) continue ;; esac
    [ -f "$pbdir/$listed" ] || { bad="${bad}ratchet names a playbook that no longer exists: ${listed}"$'\n'; continue; }
    printf '%s\n' "$names" | grep -qxF "$listed" \
      || bad="${bad}${listed} now runs the pre-flight — remove it from the ratchet"$'\n'
  done < "$ratchet"
  if [ -n "$bad" ]; then
    printf '%s' "$bad" >&2
    return 1
  fi
}

@test "every deploy playbook targeting a group runs the zero-hosts pre-flight first (ratchet)" {
  [ -f "$RATCHET" ]
  run _offenders "$PB"
  [ "$status" -eq 0 ]
  _compare "$output" "$RATCHET" "$PB"
  # Not vacuous: all 26 deploy playbooks targeted a group when this landed.
  local n
  n=$(printf '%s\n' "$output" | sed -n 's/^checked //p')
  [ "$n" -ge 20 ]
}

@test "the known compliant deploys are recognised as compliant" {
  # The three playbooks that carry form 1 today. If the checker stopped recognising it,
  # the test above would report them as NEW offenders; this one names the cause, and
  # cannot be satisfied by adding them to the ratchet.
  run _offenders "$PB"
  [ "$status" -eq 0 ]
  refute_contains "$output" "deploy-postiz.yml"
  refute_contains "$output" "deploy-agentgateway.yml"
  refute_contains "$output" "deploy-o11y.yml"
}

# ── The checker itself, on fixtures ─────────────────────────────────────────

_fixture() {
  FX="$BATS_TEST_TMPDIR/pb"
  mkdir -p "$FX"
}

@test "checker: a deploy with no pre-flight is an offender" {
  _fixture
  cat > "$FX/deploy-bare.yml" <<'YML'
- name: "Phase 1"
  hosts: bare_svc
  tasks: []
YML
  run _offenders "$FX"
  assert_contains "$output" "deploy-bare.yml"
}

@test "checker: a pre-flight imported AFTER the first group play does not count" {
  _fixture
  cat > "$FX/deploy-late.yml" <<'YML'
- name: "Phase 1"
  hosts: late_svc
  tasks: []
- name: "Pre-flight"
  ansible.builtin.import_playbook: preflight-target-group.yml
  vars:
    preflight_group: late_svc
    preflight_group_expected: late_svc
YML
  run _offenders "$FX"
  assert_contains "$output" "deploy-late.yml"
}

@test "checker: a pre-flight for a different group, or with one var, does not count" {
  _fixture
  cat > "$FX/deploy-other.yml" <<'YML'
- name: "Pre-flight"
  ansible.builtin.import_playbook: preflight-target-group.yml
  vars:
    preflight_group: other_svc
    preflight_group_expected: other_svc
- name: "Phase 1"
  hosts: real_svc
  tasks: []
YML
  cat > "$FX/deploy-single.yml" <<'YML'
- name: "Pre-flight"
  ansible.builtin.import_playbook: preflight-target-group.yml
  vars:
    preflight_group: single_svc
- name: "Phase 1"
  hosts: single_svc
  tasks: []
YML
  run _offenders "$FX"
  assert_contains "$output" "deploy-other.yml"
  assert_contains "$output" "deploy-single.yml"
}

@test "checker: a union target needs every group covered" {
  _fixture
  cat > "$FX/deploy-union.yml" <<'YML'
- name: "Pre-flight"
  import_playbook: preflight-target-group.yml
  vars:
    preflight_group: a_svc
    preflight_group_expected: a_svc
- name: "Phase 1"
  hosts: a_svc:b_svc
  tasks: []
YML
  run _offenders "$FX"
  assert_contains "$output" "deploy-union.yml"
}

@test "checker: both accepted forms, and a wrapper over a guarded playbook, are compliant" {
  _fixture
  cat > "$FX/deploy-good.yml" <<'YML'
- name: "Pre-flight"
  ansible.builtin.import_playbook: preflight-target-group.yml
  vars:
    preflight_group: good_svc
    preflight_group_expected: good_svc
- name: "Local check"
  hosts: localhost
  tasks: []
- name: "Phase 1"
  hosts: good_svc
  tasks: []
YML
  cat > "$FX/deploy-inline.yml" <<'YML'
- name: "Refuse a target group that matches no hosts"
  hosts: localhost
  tasks:
    - name: "Require target_service to name a populated group"
      ansible.builtin.assert:
        that: (groups.get(target_service | default(''), []) | length) > 0
- name: "Work"
  hosts: "{{ target_service }}"
  tasks: []
YML
  cat > "$FX/guarded.yml" <<'YML'
- name: "Pre-flight"
  ansible.builtin.import_playbook: preflight-target-group.yml
  vars:
    preflight_group: wrapped_svc
    preflight_group_expected: wrapped_svc
- name: "Phase 1"
  hosts: wrapped_svc
  tasks: []
YML
  cat > "$FX/deploy-wrapper.yml" <<'YML'
- name: "Deploy wrapped"
  import_playbook: guarded.yml
YML
  run _offenders "$FX"
  [ "$status" -eq 0 ]
  refute_contains "$output" "deploy-good.yml"
  refute_contains "$output" "deploy-inline.yml"
  refute_contains "$output" "deploy-wrapper.yml"
  assert_contains "$output" "checked 3"
}

@test "ratchet: a new offender not listed fails, and a listed playbook that complies fails" {
  _fixture
  cat > "$FX/deploy-bare.yml" <<'YML'
- name: "Phase 1"
  hosts: bare_svc
  tasks: []
YML
  cat > "$FX/deploy-good.yml" <<'YML'
- name: "Pre-flight"
  ansible.builtin.import_playbook: preflight-target-group.yml
  vars:
    preflight_group: good_svc
    preflight_group_expected: good_svc
- name: "Phase 1"
  hosts: good_svc
  tasks: []
YML
  local r="$BATS_TEST_TMPDIR/ratchet.txt" out
  out=$(_offenders "$FX")
  : > "$r"
  run _compare "$out" "$r" "$FX"
  [ "$status" -ne 0 ]
  assert_contains "$output" "NEW deploy playbook without a pre-flight: deploy-bare.yml"
  printf 'deploy-bare.yml\ndeploy-good.yml\n' > "$r"
  run _compare "$out" "$r" "$FX"
  [ "$status" -ne 0 ]
  assert_contains "$output" "deploy-good.yml now runs the pre-flight"
  printf 'deploy-bare.yml\n' > "$r"
  run _compare "$out" "$r" "$FX"
  [ "$status" -eq 0 ]
}

# _inline_fixture <file> <condition> — a deploy whose only guard is one inline assert.
_inline_fixture() {
  cat > "$FX/$1" <<YML
- name: "Inline guard"
  hosts: localhost
  tasks:
    - name: "Require a populated group"
      ansible.builtin.assert:
        that: $2
- name: "Work"
  hosts: "{{ target_service }}"
  tasks: []
YML
}

@test "checker: the repository's own inline guards are recognised" {
  # The real form, not a fixture's idea of it: copied under deploy-* names.
  _fixture
  cp "$PB/check-secrets.yml" "$FX/deploy-check-secrets.yml"
  cp "$PB/verify-service-health.yml" "$FX/deploy-verify-service-health.yml"
  run _offenders "$FX"
  [ "$status" -eq 0 ]
  refute_contains "$output" "deploy-check-secrets.yml"
  refute_contains "$output" "deploy-verify-service-health.yml"
  assert_contains "$output" "checked 2"
}

@test "checker: an inline assert that an EMPTY group also passes does not count" {
  _fixture
  _inline_fixture deploy-ge0.yml "\"(groups.get(target_service, []) | length) >= 0\""
  _inline_fixture deploy-gtneg.yml "\"(groups.get(target_service, []) | length) > -1\""
  _inline_fixture deploy-or.yml "\"(groups.get(target_service, []) | length) > 0 or true\""
  _inline_fixture deploy-and.yml "\"(groups.get(target_service, []) | length) >= 0 and ([1] | length) > 0\""
  _inline_fixture deploy-twocmp.yml "\"((groups.get(target_service, []) | length) >= 0) == (([1] | length) > 0)\""
  _inline_fixture deploy-not.yml "\"not (groups.get(target_service, []) | length) > 0\""
  _inline_fixture deploy-reversed.yml "\"0 < (groups.get(target_service, []) | length)\""
  _inline_fixture deploy-nolen.yml "\"groups.get(target_service) is defined\""
  _inline_fixture deploy-othergroup.yml "\"(groups.get(other_var, []) | length) > 0\""
  run _offenders "$FX"
  [ "$status" -eq 0 ]
  local f
  for f in ge0 gtneg or and twocmp not reversed nolen othergroup; do
    assert_contains "$output" "deploy-$f.yml"
  done
}

@test "checker: each accepted positive comparison counts" {
  _fixture
  _inline_fixture deploy-gt0.yml "\"(groups.get(target_service, []) | length) > 0\""
  _inline_fixture deploy-ge1.yml "\"groups[target_service] | length >= 1\""
  _inline_fixture deploy-eq1.yml "\"groups[target_service] | length == 1\""
  _inline_fixture deploy-ne0.yml "\"(groups.get(target_service, []) | length) != 0\""
  run _offenders "$FX"
  [ "$status" -eq 0 ]
  refute_contains "$output" $'.yml\t'
  assert_contains "$output" "checked 4"
}
