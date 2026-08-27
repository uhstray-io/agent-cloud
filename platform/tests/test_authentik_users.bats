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
