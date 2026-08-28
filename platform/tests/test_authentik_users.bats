#!/usr/bin/env bats
# Adding a platform user touches four files: the blueprint declares them, env.j2
# passes their identity and password through to the worker, deploy-authentik
# declares the password so OpenBao generates and REUSES one, and site-config
# carries the real identity. Miss any of the first three and the failure is silent
# in a different way each time —
#
#   no _secret_definitions entry -> !Env resolves empty, the user is created with
#                                   NO password and cannot log in
#   no env.j2 line               -> the blueprint's placeholder default is applied,
#                                   silently creating a fake user in production
#   a mistyped group name        -> !Find matches nothing; the user exists, passes
#                                   no access gate, and every app rejects them
#
# The group names are the trap worth naming: the gate admits platform-admins,
# platform-developers and platform-business, and DENIES platform-user. Singular
# vs plural is one character between full access and none.

load assert_helpers

setup() {
  BP="${BATS_TEST_DIRNAME}/../services/authentik/deployment/blueprints"
  USERS="${BP}/platform-users.yaml"
  GROUPS_BP="${BP}/platform-groups.yaml"
  ENVJ2="${BATS_TEST_DIRNAME}/../services/authentik/deployment/templates/env.j2"
  DEPLOY="${BATS_TEST_DIRNAME}/../playbooks/deploy-authentik.yml"
}

# Every !Env name referenced by the users blueprint, one per line.
_env_names() {
  grep -oE '!Env \[?[A-Z][A-Z0-9_]*' "$USERS" | sed -E 's/^!Env \[?//' | sort -u
}

@test "authentik users: every !Env the blueprint reads is defined in env.j2" {
  # Enumerates what the blueprint ACTUALLY references rather than a hand-kept
  # list, so a user added tomorrow is covered without editing this test.
  local missing="" n
  n=$(_env_names | grep -c . || true)
  [ "$n" -gt 0 ]
  local name
  while read -r name; do
    [ -n "$name" ] || continue
    grep -qE "^${name}=" "$ENVJ2" || missing="${missing} ${name}"
  done < <(_env_names)
  if [ -n "$missing" ]; then
    echo "referenced by platform-users.yaml but absent from env.j2:${missing}" >&2
    return 1
  fi
}

@test "authentik users: every user's password is an OpenBao-managed secret" {
  # A password read from env.j2 must trace to a _secret_definitions entry, or it
  # is generated nowhere and the account is created unusable. Derives the secret
  # name from the env var, so it cannot drift out of step with the blueprint.
  local missing="" name lower
  while read -r name; do
    case "$name" in *_PASSWORD) ;; *) continue ;; esac
    lower=$(printf '%s' "$name" | tr 'A-Z' 'a-z')
    grep -qE "name: ${lower}," "$DEPLOY" || missing="${missing} ${lower}"
  done < <(_env_names)
  if [ -n "$missing" ]; then
    echo "password env vars with no _secret_definitions entry:${missing}" >&2
    return 1
  fi
}

@test "authentik users: every group a user joins is actually declared" {
  # THE SINGULAR/PLURAL TRAP. !Find silently matches nothing on a typo, so the
  # user is created, joins no group, and is refused by every app — which reads as
  # a broken login, not a broken blueprint. Closed: each referenced name must
  # appear as a declared group, no allow-list of "names we expect".
  local declared referenced missing="" g
  declared=$(grep -oE '^\s+name: [a-z][a-z0-9-]*' "$GROUPS_BP" | awk '{print $2}' | sort -u)
  referenced=$(grep -oE '!Find \[authentik_core\.group, \[name, [a-z0-9-]+\]\]' "$USERS" \
               | sed -E 's/.*name, ([a-z0-9-]+)\]\]/\1/' | sort -u)
  [ -n "$referenced" ]
  while read -r g; do
    [ -n "$g" ] || continue
    printf '%s\n' "$declared" | grep -qx "$g" || missing="${missing} ${g}"
  done < <(printf '%s\n' "$referenced")
  if [ -n "$missing" ]; then
    echo "groups joined in platform-users.yaml but not declared in platform-groups.yaml:${missing}" >&2
    echo "declared groups are:" >&2
    printf '%s\n' "$declared" >&2
    return 1
  fi
}

@test "authentik users: no user is placed in a group the access gate denies" {
  # platform-user is a declared group that the platform-member gate does NOT
  # admit, so a user placed in it alone reaches no tier-member app — tududi
  # included. That is a legitimate group to declare and a mistake to assign
  # alone, which is exactly the kind of thing a diff does not show.
  local gate="${BATS_TEST_DIRNAME}/../services/authentik/deployment/templates/zz-sso-bindings.yaml.j2"
  [ -f "$gate" ]
  # Read the admitted set out of the gate itself rather than restating it here;
  # a copy would drift and this test would then be asserting yesterday's policy.
  local admitted
  admitted=$(grep -oE 'allowed = \{[^}]*\}' "$gate" | grep -oE '"[a-z-]+"' | tr -d '"' | sort -u)
  [ -n "$admitted" ]
  local referenced g
  referenced=$(grep -oE '!Find \[authentik_core\.group, \[name, [a-z0-9-]+\]\]' "$USERS" \
               | sed -E 's/.*name, ([a-z0-9-]+)\]\]/\1/' | sort -u)
  local stranded=""
  while read -r g; do
    [ -n "$g" ] || continue
    printf '%s\n' "$admitted" | grep -qx "$g" || stranded="${stranded} ${g}"
  done < <(printf '%s\n' "$referenced")
  if [ -n "$stranded" ]; then
    echo "users are placed in groups the access gate does not admit:${stranded}" >&2
    echo "gate admits:" >&2
    printf '%s\n' "$admitted" >&2
    return 1
  fi
}

@test "authentik users: a retired username can never collide with an active one" {
  # `state: absent` DELETES. If a retired entry names the same username variable as
  # an active entry, one converging run both creates and deletes that account, and
  # which wins is entry ORDER rather than intent — a coin flip that looks like a
  # flaky login. Closed: the two sets must not intersect, whatever they contain.
  local overlap
  overlap=$(python3 - "$USERS" <<'PY_ENT'
import re, sys
text = open(sys.argv[1]).read()
# Split on entry boundaries; an entry is a "- model:" item at list indent.
entries = re.split(r'\n(?=  - model:)', text)
active, retired = set(), set()
for e in entries:
    if '- model: authentik_core.user' not in e:
        continue
    m = re.search(r'username:\s*!Env\s*\[?([A-Z][A-Z0-9_]*)', e)
    if not m:
        continue
    (retired if re.search(r'^\s+state:\s*absent\s*$', e, re.M) else active).add(m.group(1))
for name in sorted(active & retired):
    print(name)
PY_ENT
)
  if [ -n "$overlap" ]; then
    echo "username variables used by BOTH an active and an absent entry:" >&2
    printf '%s\n' "$overlap" >&2
    return 1
  fi
  # And there is at least one of each, so this is not passing on an empty set.
  grep -q 'state: absent' "$USERS"
}

@test "authentik users: an unset retired username deletes nothing" {
  # A delete entry whose variable is unset must fall back to a username nothing
  # ever creates. The alternative — a plausible default — means a missing
  # site-config value silently removes a real account, which is the worst possible
  # direction for a default to fail in. Closed: EVERY absent entry uses the one
  # sentinel, so a new retirement cannot invent its own weaker default.
  local bad
  bad=$(python3 - "$USERS" <<'PY_DEF'
import re, sys
text = open(sys.argv[1]).read()
SENTINEL = "retired-account-never-created"
for e in re.split(r'\n(?=  - model:)', text):
    if '- model: authentik_core.user' not in e:
        continue
    if not re.search(r'^\s+state:\s*absent\s*$', e, re.M):
        continue
    m = re.search(r'username:\s*!Env\s*\[([A-Z][A-Z0-9_]*),\s*"([^"]*)"\]', e)
    if not m:
        print("an absent entry has no [VAR, \"default\"] username form")
    elif m.group(2) != SENTINEL:
        print("%s defaults to %r, not the sentinel" % (m.group(1), m.group(2)))
PY_DEF
)
  if [ -n "$bad" ]; then
    echo "unsafe defaults on a deleting entry:" >&2
    printf '%s\n' "$bad" >&2
    return 1
  fi
}

@test "credential print: it cannot run without an explicit acknowledgement" {
  # The single most dangerous playbook in this repo — it writes a live credential
  # into durable task output. Its gate must be an explicit opt-in that DEFAULTS
  # to refusing, because the failure direction of a permissive default here is a
  # password sitting in a log nobody knew to delete.
  local pb="${BATS_TEST_DIRNAME}/../playbooks/print-platform-user-credentials.yml"
  [ -f "$pb" ]
  local blk
  blk=$(awk '/^    - name: "Refuse unless the caller has said the quiet part out loud"/ { f = 1; next }
             f && /^    - name:/ { exit }
             f { print }' "$pb")
  [ -n "$blk" ]
  assert_grep -q 'i_understand_this_prints_secrets | default(false) | bool' <<<"$blk"
}

@test "credential print: exactly one task emits credential material" {
  # CLOSED COUNT. Every other task that touches the secret is no_log'd so that the
  # one deliberate exposure is obvious in review. A second unguarded debug — added
  # later while diagnosing something — would leak without changing how the file
  # reads, which is precisely how this goes wrong.
  local pb="${BATS_TEST_DIRNAME}/../playbooks/print-platform-user-credentials.yml"
  local emitters
  emitters=$(grep -cE '_secret\.json\.data\.data\[item ~ .{1}_password.{1}\] \}\}' "$pb")
  if [ "$emitters" != "1" ]; then
    echo "expected exactly 1 task emitting a password, found $emitters" >&2
    grep -nE '_secret\.json\.data\.data' "$pb" >&2
    return 1
  fi
  # And every task that READS the secret without printing it is no_log'd.
  local reads no_logs
  reads=$(grep -cE '^      register: (_bao_auth|_secret)$' "$pb")
  no_logs=$(grep -cE '^      no_log: true$' "$pb")
  [ "$reads" -eq 2 ]
  [ "$no_logs" -ge "$reads" ]
}

@test "credential print: the secret path is fixed, not caller-supplied" {
  # A parameterized path turns a scoped credential hand-off into a general-purpose
  # store dump, from a playbook whose whole job is to print what it reads.
  local pb="${BATS_TEST_DIRNAME}/../playbooks/print-platform-user-credentials.yml"
  assert_grep -qE '^    _bao_path: "secret/data/services/authentik"$' "$pb"
  # No variable indirection anywhere in the declaration.
  refute_grep -qE '_bao_path:.*(default\(|lookup\(|\{\{)' "$pb"
}

@test "credential print: the allow-list is DERIVED from the blueprint, not hand-kept" {
  # A fixed PATH is not a fixed SCOPE. secret/services/authentik also holds
  # db_password, bootstrap_password, the service-account token and every OIDC
  # client secret, and `<name>_password` indexing reaches all of them — so an
  # unrestricted `credential_users` turns a scoped hand-off into
  # `-e credential_users=db` printing the Postgres password.
  #
  # Derived, because a hand-kept list drifts from the blueprint and the drift
  # direction that matters is "a user was added and cannot be handed their login",
  # which looks like a bug rather than a stale list.
  local pb="${BATS_TEST_DIRNAME}/../playbooks/print-platform-user-credentials.yml"
  assert_grep -q "regex_findall('!Env (\[A-Z\]\[A-Z0-9_\]\*)_PASSWORD')" "$pb"
  assert_grep -q "lookup('file', _blueprint)" "$pb"
  # The requested set is FILTERED through it, and what falls out is refused.
  assert_grep -qE "_users: .*_requested \| select\('in', _allowed\)" "$pb"
  assert_grep -qE "_rejected: .*_requested \| reject\('in', _allowed\)" "$pb"
  assert_grep -q '_rejected | length == 0' "$pb"
  # No literal user list anywhere — that would be the hand-kept form this forbids.
  refute_grep -qE "_allowed: *\[" "$pb"
}

@test "authentik deploy: a retired username is checked against the ACTIVE ones, before applying" {
  # The blueprint-level test compares variable NAMES, which are trivially
  # different. The condition that actually deletes someone is two different
  # variables carrying the same VALUE — and those values live in site-config,
  # which this repo never sees. So the real guard has to resolve them at deploy
  # time.
  local dep="${BATS_TEST_DIRNAME}/../playbooks/deploy-authentik.yml"
  assert_grep -q '_active_usernames | intersect(_retired_usernames)' "$dep"

  # EFFECTIVE values, not defined variables. `vars | dict2items` sees only what is
  # DEFINED, so with jacob_username unset it does not contain "jacob" — while
  # env.j2 renders JACOB_USERNAME={{ jacob_username | default('jacob') }} and the
  # account created is called jacob. A retired entry set to "jacob" sailed past
  # the first version of this guard. The declarations are read out of env.j2,
  # which is where the defaults live, and each is resolved through lookup('vars',
  # name, default=...).
  assert_grep -q "regex_findall(\"(\[A-Z0-9_\]\*USERNAME)=" "$dep"
  assert_grep -q "lookup('vars', item\[1\], default=item\[2\])" "$dep"
  # And it cannot pass vacuously if the regex ever stops matching.
  assert_grep -q '_uname_decls | length > 0' "$dep"
  assert_grep -q "selectattr('env', 'search', 'LEGACY_USERNAME\$')" "$dep"
  assert_grep -q "rejectattr('env', 'search', 'LEGACY_USERNAME\$')" "$dep"

  # ORDER IS THE POINT, and no magic number expresses it. An earlier attempt
  # pinned the guard ahead of "Assemble active blueprints" and passed while it sat
  # after "Reset the active-blueprints dir", which had already emptied the
  # directory. A "first three tasks" rule then broke the moment the guard grew a
  # task, which is a test failing for being right about the wrong thing.
  #
  # The precise rule: every task BEFORE the guard must be pure computation.
  # set_fact touches nothing on the target, so anything else preceding the guard
  # means the run has already acted before deciding whether it should.
  local preceding
  preceding=$(python3 - "$dep" <<'PY_ORDER'
import re, sys
lines = open(sys.argv[1]).read().split('\n')
in_tasks = False
for i, line in enumerate(lines):
    if re.match(r'^  tasks:\s*$', line):
        in_tasks = True
        continue
    if not in_tasks:
        continue
    m = re.match(r'^    - name: "(.*)"\s*$', line)
    if not m:
        continue
    if 'Refuse to both create and delete the same account' in m.group(1):
        break
    # Look ahead for this task's module line.
    body = []
    for nxt in lines[i + 1:]:
        if re.match(r'^    - name: ', nxt):
            break
        body.append(nxt)
    if not any(re.match(r'^      ansible\.builtin\.set_fact:', b) for b in body):
        print(m.group(1))
PY_ORDER
)
  if [ -n "$preceding" ]; then
    echo "these run BEFORE the collision guard and are not pure set_fact:" >&2
    printf '%s\n' "$preceding" >&2
    echo "the guard must decide before the run acts on anything." >&2
    return 1
  fi
  # And the guard is actually present, so this cannot pass on an empty scan.
  grep -q 'Refuse to both create and delete the same account' "$dep"
}

@test "authentik deploy: inventory URLs are resolved through lookup('vars'), never vars[]" {
  # The `vars` magic dict returns the RAW string. An inventory value written as
  # "{{ postiz_public_url }}/settings" — a template referencing another var, which
  # is exactly how site-config declares the redirect once and derives the rest —
  # arrives with its braces intact. The promotion guard's two checks (non-empty,
  # no agent-cloud.test) pass on that string, and the verifier one phase later
  # compares the live URL against a literal template and fails. Measured on the
  # first prod deploy after that inventory change, 2026-08-27.
  #
  # CLOSED over both files that read those inventory URLs: neither may use vars[].
  local dep="${BATS_TEST_DIRNAME}/../playbooks/deploy-authentik.yml"
  local ver="${BATS_TEST_DIRNAME}/../services/authentik/deployment/templates/verify-oidc.py.j2"
  [ -f "$dep" ]; [ -f "$ver" ]
  refute_grep -qF 'vars[item]' "$dep"
  refute_grep -qF 'vars[a.verify_redirect]' "$ver"
  assert_grep -q "lookup('vars', item, default='')" "$dep"
  assert_grep -q "lookup('vars', a.verify_redirect, default='')" "$ver"
  # And the guard now refuses an UNRESOLVED template outright, so a broken
  # reference fails at the guard rather than one phase later.
  local blk
  blk=$(awk '/^    - name: "Promotion guard \(prod\): OIDC prod URLs are set/ { f = 1; next }
             f && /^    - name:/ { exit }
             f { print }' "$dep")
  [ -n "$blk" ]
  assert_grep -qF "'{{ '{{' }}' not in" <<<"$blk"
}

@test "authentik deploy: Phase 3 reads the live state back — blueprints applied, accounts present/absent" {
  # Blueprint application is asynchronous in the worker. A deploy that placed the
  # files and saw a healthy server has proven only that the files landed — not
  # that a single account exists, which is what an operator is about to hand
  # someone a password for. Measured 2026-08-27: a deploy reached Phase 3 with the
  # user blueprints assembled and nothing in the run able to say whether the
  # accounts existed.
  local dep="${BATS_TEST_DIRNAME}/../playbooks/deploy-authentik.yml"
  local ver="${BATS_TEST_DIRNAME}/../services/authentik/deployment/templates/verify-users.py.j2"
  [ -f "$ver" ]
  # Rendered and executed inside the container, the same way as the OIDC check.
  assert_grep -q 'templates/verify-users.py.j2' "$dep"
  assert_grep -qE 'exec -i authentik-server python - < /tmp/verify-users\.py' "$dep"
  # NOT gated on prod. The render and run tasks carry no `when:` — a local deploy
  # that creates nobody is just as wrong as a prod one.
  local blk
  blk=$(awk '/^    - name: "Blueprint verify: render the live-state checker"/ { f = 1 }
             f && /^    - name: "Blueprint verify: result"/ { exit }
             f { print }' "$dep")
  [ -n "$blk" ]
  refute_grep -qE '^      when:' <<<"$blk"
  # A non-zero exit fails the deploy; it is not advisory.
  assert_grep -q 'failed_when: _users_verify.rc != 0' <<<"$blk"
  # The checker consumes the SAME resolved lists the collision guard compared, so
  # the create/delete intent and the verified state cannot drift apart.
  assert_grep -q '_active_usernames' "$ver"
  assert_grep -q '_retired_usernames' "$ver"
  # And it checks every leg: applied, present+active, in a group, absent.
  assert_grep -q '"successful"' "$ver"
  assert_grep -q 'is_active' "$ver"
  assert_grep -q 'groups_obj' "$ver"
  assert_grep -q 'EXPECT_ABSENT' "$ver"
  # Exact-match on this side: the API filter's semantics are unstated, so a
  # superstring must never satisfy a check for its prefix.
  assert_grep -q 'u.get("username") == username' "$ver"
}
