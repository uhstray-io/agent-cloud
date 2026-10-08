#!/usr/bin/env bats
# A verify playbook reads each endpoint under the same name, with the same default, as
# the playbook that provisions or deploys the thing it verifies.
#
# docs/MISTAKES.md 6.4: verify-tududi-github-sync.yml read the tududi API from
# `tududi_base_url | default('http://127.0.0.1:3002')`. The baked local inventory already
# set that name, to the app's PUBLIC base URL behind Caddy, which the orchestrator cannot
# reach. The provisioner had named the same fact `tududi_sync_tududi_url`, default
# `http://tududi:3002`. The gate failed on every run, and the step carried a bearer header,
# so the run printed `censored` and nothing else.
#
# PAIRING RULE, as checked here:
#   - Population: every platform/playbooks/verify-<X>.yml.
#   - Counterparts: every deploy-<Y>.yml and provision-<Y>.yml in the same directory where
#     X == Y or X starts with "<Y>-" (verify-tududi-github-sync pairs with
#     provision-tududi-github-sync AND deploy-tududi; verify-o11y-service with deploy-o11y).
#     A verify playbook with no counterpart is not checked.
#   - Subject: each `<name>_url | default('<literal>')` in any string of the verify playbook
#     (whitespace and newlines anywhere around `|`, `default` and the paren), where
#     <name> is an inventory-style variable (no leading underscore, not an attribute) and
#     the literal is non-empty. An empty default means "unset" and carries no fact.
#   - Pass: at least one counterpart defaults the SAME name with a literal, and every
#     literal default a counterpart gives that name equals the verify playbook's.
#   - So both halves of 6.4 fail it: a verify playbook that invents its own name for an
#     endpoint (no counterpart has the name), and one that reuses the name with another
#     default.
#
# A RATCHET (known_verify_url_mismatches.txt, `<verify playbook>:<variable>` per line):
# every offender must be named there, and every name there must still be an offender.
#
# Run: bats platform/tests/test_verify_url_defaults.bats

load assert_helpers

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  PB="$REPO_ROOT/platform/playbooks"
  RATCHET="$REPO_ROOT/platform/tests/known_verify_url_mismatches.txt"
}

# _offenders <playbook dir> — one line per offending (verify playbook, variable):
#   <verify playbook>:<variable>\t<reason>
# then `checked <n>` (variables compared) and `matched <list>` (the pairs that agree).
_offenders() {
  PYTHONPATH="$REPO_ROOT/platform/tests" python3 - "$1" <<'PY'
import re
import sys
from pathlib import Path

import playbook_yaml

# Whitespace-tolerant everywhere Jinja is: `x_url|default('v')`, `x_url | default ('v')`,
# and a newline before the filter, the paren or the literal all match. Applied to each
# string VALUE of the parsed YAML, not the raw text, so YAML quoting and escapes are
# already resolved and a commented-out default does not count.
DEFAULT = re.compile(
    r"(?<![\w.\]])([a-z][a-z0-9_]*_url)\s*\|\s*default\s*\(\s*(['\"])(.*?)\2\s*[,)]"
)


def url_defaults(path):
    out = {}
    doc = playbook_yaml.loads(path.read_text())
    for text in playbook_yaml.strings(doc, keys=True):
        for name, _q, literal in DEFAULT.findall(text):
            if literal:
                out.setdefault(name, set()).add(literal)
    return out


pbdir = Path(sys.argv[1])
makers = [
    (p, p.stem.split("-", 1)[1])
    for p in sorted(list(pbdir.glob("deploy-*.yml")) + list(pbdir.glob("provision-*.yml")))
]
checked, matched = 0, []
for verify in sorted(pbdir.glob("verify-*.yml")):
    x = verify.stem[len("verify-"):]
    counterparts = [p for p, y in makers if x == y or x.startswith(y + "-")]
    if not counterparts:
        continue
    theirs = {}
    for p in counterparts:
        for name, literals in url_defaults(p).items():
            for lit in literals:
                theirs.setdefault(name, {}).setdefault(lit, []).append(p.name)
    for name, literals in sorted(url_defaults(verify).items()):
        checked += 1
        key = f"{verify.name}:{name}"
        names = ", ".join(p.name for p in counterparts)
        if name not in theirs:
            print(f"{key}\tno counterpart ({names}) defaults {name}: a second name for an endpoint, or one the provisioner never sets")
            continue
        others = set(theirs[name]) - literals
        if len(literals) > 1 or others:
            detail = "; ".join(f"{lit!r} in {', '.join(ps)}" for lit, ps in sorted(theirs[name].items()))
            print(f"{key}\tdefaults {sorted(literals)} here but {detail}")
            continue
        matched.append(key)
print(f"checked {checked}")
print("matched " + " ".join(matched))
PY
}

# _compare <offender output> <ratchet file> <playbook dir> — fails on any disagreement.
_compare() {
  local out="$1" ratchet="$2" pbdir="$3" bad="" key reason listed keys
  keys=$(printf '%s\n' "$out" | grep -vE '^(checked|matched)( |$)' | cut -f1)
  while IFS=$'\t' read -r key reason; do
    case "$key" in ''|checked\ *|matched|matched\ *) continue ;; esac
    grep -qxF "$key" "$ratchet" || bad="${bad}NEW verify url default mismatch: ${key} (${reason})"$'\n'
  done <<< "$out"
  while read -r listed; do
    case "$listed" in ''|'#'*) continue ;; esac
    [ -f "$pbdir/${listed%%:*}" ] || { bad="${bad}ratchet names a playbook that no longer exists: ${listed}"$'\n'; continue; }
    printf '%s\n' "$keys" | grep -qxF "$listed" \
      || bad="${bad}${listed} now matches its counterpart — remove it from the ratchet"$'\n'
  done < "$ratchet"
  if [ -n "$bad" ]; then
    printf '%s' "$bad" >&2
    return 1
  fi
}

@test "every verify playbook url default matches its provisioner's (ratchet)" {
  [ -f "$RATCHET" ]
  run _offenders "$PB"
  [ "$status" -eq 0 ]
  _compare "$output" "$RATCHET" "$PB"
  local n
  n=$(printf '%s\n' "$output" | sed -n 's/^checked //p')
  # Not vacuous: two variables were compared when this landed.
  [ "$n" -ge 2 ]
}

@test "the 6.4 fix is recognised: the sync gate reads tududi under the provisioner's name and default" {
  # The pair that was wrong in 6.4 and is right now. If the pairing rule or the default
  # parser regressed, this pair would stop being compared, and the ratchet test above
  # could still pass on an empty population.
  run _offenders "$PB"
  [ "$status" -eq 0 ]
  assert_contains "$output" "verify-tududi-github-sync.yml:tududi_sync_tududi_url"
  refute_contains "$output" $'verify-tududi-github-sync.yml:tududi_sync_tududi_url\t'
}

# ── The checker itself, on fixtures ─────────────────────────────────────────

_fixture() {
  FX="$BATS_TEST_TMPDIR/pb"
  mkdir -p "$FX"
  cat > "$FX/provision-thing.yml" <<'YML'
- hosts: thing_svc
  vars:
    _api: "{{ thing_api_url | default('http://thing:8080') }}"
    _blank: "{{ thing_blank_url | default('') }}"
YML
}

@test "checker: the 6.4 shape, the same name with another default, is an offender" {
  _fixture
  cat > "$FX/verify-thing.yml" <<'YML'
- hosts: thing_svc
  vars:
    _api: "{{ thing_api_url | default('http://127.0.0.1:8080') }}"
YML
  run _offenders "$FX"
  assert_contains "$output" $'verify-thing.yml:thing_api_url\tdefaults'
}

@test "checker: a name no counterpart uses is an offender" {
  _fixture
  cat > "$FX/verify-thing-sync.yml" <<'YML'
- hosts: thing_svc
  vars:
    _api: "{{ thing_public_url | default('http://thing:8080', true) }}"
YML
  run _offenders "$FX"
  assert_contains "$output" $'verify-thing-sync.yml:thing_public_url\tno counterpart (provision-thing.yml)'
}

@test "checker: a matching default passes; blank defaults, private vars and unpaired verifies are skipped" {
  _fixture
  cat > "$FX/verify-thing.yml" <<'YML'
- hosts: thing_svc
  vars:
    _api: "{{ override_url | default(thing_api_url | default('http://thing:8080')) }}"
    _blank: "{{ health_url | default('', true) }}"
    _priv: "{{ _local_url | default('http://x') }}"
YML
  cat > "$FX/verify-elsewhere.yml" <<'YML'
- hosts: other_svc
  vars:
    _api: "{{ elsewhere_url | default('http://nowhere') }}"
YML
  run _offenders "$FX"
  [ "$status" -eq 0 ]
  assert_contains "$output" "checked 1"
  assert_contains "$output" "matched verify-thing.yml:thing_api_url"
}

@test "ratchet: a new offender not listed fails, and a listed pair that matches fails" {
  _fixture
  cat > "$FX/verify-thing.yml" <<'YML'
- hosts: thing_svc
  vars:
    _api: "{{ thing_api_url | default('http://127.0.0.1:8080') }}"
    _ok: "{{ thing_api_url | default('http://thing:8080') }}"
YML
  local r="$BATS_TEST_TMPDIR/ratchet.txt" out
  out=$(_offenders "$FX")
  : > "$r"
  run _compare "$out" "$r" "$FX"
  [ "$status" -ne 0 ]
  assert_contains "$output" "NEW verify url default mismatch: verify-thing.yml:thing_api_url"
  # Fix the fixture; the stale ratchet line must now fail.
  cat > "$FX/verify-thing.yml" <<'YML'
- hosts: thing_svc
  vars:
    _api: "{{ thing_api_url | default('http://thing:8080') }}"
YML
  out=$(_offenders "$FX")
  printf 'verify-thing.yml:thing_api_url\n' > "$r"
  run _compare "$out" "$r" "$FX"
  [ "$status" -ne 0 ]
  assert_contains "$output" "verify-thing.yml:thing_api_url now matches its counterpart"
  : > "$r"
  run _compare "$out" "$r" "$FX"
  [ "$status" -eq 0 ]
}

@test "checker: spacing and line breaks around the filter do not hide a default" {
  # `default (`, a newline before the paren, before the `|`, and a no-space `|default(`
  # are all valid Jinja; each carries the 6.4 mismatch and must be reported.
  _fixture
  cat > "$FX/verify-thing.yml" <<'YML'
- hosts: thing_svc
  vars:
    _a: "{{ thing_api_url | default ('http://127.0.0.1:1') }}"
    _b: |-
      {{ thing_b_url | default
         ('http://wrong-b') }}
    _c: |-
      {{ thing_c_url
         | default('http://wrong-c') }}
    _d: "{{ thing_d_url|default( \"http://wrong-d\" , true) }}"
YML
  cat >> "$FX/provision-thing.yml" <<'YML'
    _b: "{{ thing_b_url | default('http://b') }}"
    _c: "{{ thing_c_url | default('http://c') }}"
    _d: "{{ thing_d_url | default('http://d') }}"
YML
  run _offenders "$FX"
  [ "$status" -eq 0 ]
  assert_contains "$output" $'verify-thing.yml:thing_api_url\tdefaults'
  assert_contains "$output" $'verify-thing.yml:thing_b_url\tdefaults'
  assert_contains "$output" $'verify-thing.yml:thing_c_url\tdefaults'
  assert_contains "$output" $'verify-thing.yml:thing_d_url\tdefaults'
  assert_contains "$output" "checked 4"
}
