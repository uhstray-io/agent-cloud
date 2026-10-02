#!/usr/bin/env bats
# Every Bash script tracked under platform/ and .githooks/ sets pipefail before its
# first pipeline.
#
# Why: a pipeline's exit status is its LAST command's. Without pipefail, `cmd | tail`
# succeeds when cmd fails, and `set -e` never fires. docs/MISTAKES.md 1.3 and 8.1
# record that exact masking reporting failures as success, each enforced only by
# convention. This file moves the script half of that rule into a test.
#
# WHAT IS CHECKED. The population is derived from `git ls-files`, never hand-typed:
#   - a tracked file whose first line is a bash shebang (`#!/bin/bash`,
#     `#!/usr/bin/env bash`), whatever its name — this includes `*.sh.j2` templates
#     that render to a bash script; or
#   - a tracked `*.sh` file with no shebang at all.
# Such a script must carry `set -o pipefail` (or a combined form such as
# `set -euo pipefail`) on a line BEFORE its first non-comment line containing a
# single `|`. The pipe match is deliberately coarse: a `case` alternative or a `|`
# inside a string also counts, which can only make the rule stricter.
#
# WHAT IS NOT CHECKED, AND WHY.
#   - POSIX sh scripts (`#!/bin/sh`, `#!/usr/bin/env sh` — the .githooks hooks and
#     platform/local-dev/https-forward.sh). The shebang decides, not the extension.
#     `set -o pipefail` is not portable to every /bin/sh: dash, Debian's /bin/sh,
#     rejects it (`dash: 1: set: Illegal option -o pipefail`, verified 2026-10-02),
#     so requiring it would break the script. Pipe hygiene there stays a convention.
#   - Sourced libraries. A file in a `lib/` directory whose basename some in-scope
#     script `source`s (or `.`s) is a library: it runs inside its caller's shell and
#     inherits the caller's options, and that caller is itself in the population
#     and so must set pipefail. Setting options in a library would also mutate every
#     caller's shell as a side effect. A `lib/` file nothing sources (for example
#     netbox's lib/generate-secrets.sh) is an executable and IS checked.
#
# Existing offenders are a RATCHET in known_scripts_without_pipefail.txt (same shape
# as known_unguarded_bao_plays.txt): a new gap fails, and a fixed script left in the
# file fails too.

load assert_helpers

setup() {
  # Bats runs this under the pre-push hook, which exports GIT_DIR and friends; a
  # fixture's `git init` must not reach the real repository (docs/MISTAKES.md 3.7).
  unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR GIT_OBJECT_DIRECTORY \
    GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_PREFIX GIT_NAMESPACE
  REPO_ROOT=$(git rev-parse --show-toplevel)
  RATCHET="$REPO_ROOT/platform/tests/known_scripts_without_pipefail.txt"
}

# The line that turns pipefail on: `set -o pipefail`, `set -euo pipefail`,
# `set -Eeuo pipefail`, `set -e -o pipefail`. `+o pipefail` turns it OFF and does
# not match.
PIPEFAIL_RE='^[[:space:]]*set[[:space:]]+([^#;]*[[:space:]])?-[[:alpha:]]*o[[:space:]]+pipefail([[:space:];]|$)'
# A single `|` (not `||`) on a non-comment line.
PIPE_RE='(^|[^|])[|]([^|]|$)'
BASH_SHEBANG_RE='^#![[:space:]]*(/usr/bin/env[[:space:]]+)?(/[^[:space:]]*/)?bash([[:space:]]|$)'

# Prints the in-scope bash scripts of <root>, one relative path per line.
bash_scripts() {
  local root="$1" f first
  git -C "$root" ls-files -- platform .githooks | while IFS= read -r f; do
    [ -f "$root/$f" ] || continue
    first=$(head -n 1 "$root/$f")
    case "$first" in
      '#!'*) printf '%s\n' "$first" | grep -qE "$BASH_SHEBANG_RE" && printf '%s\n' "$f" ;;
      *) case "$f" in *.sh) printf '%s\n' "$f" ;; esac ;;
    esac
  done
}

# 0 if <rel> (under <root>) is a sourced library per the rule above.
is_sourced_library() {
  local root="$1" rel="$2" scripts="$3" base s
  case "$rel" in */lib/*) ;; *) return 1 ;; esac
  case "${rel%/*}" in */lib) ;; *) return 1 ;; esac
  base=${rel##*/}
  while IFS= read -r s; do
    [ "$s" = "$rel" ] && continue
    grep -qE "^[[:space:]]*(source|\\.)[[:space:]]+\"?[^[:space:]\"]*/${base//./\\.}\"?[[:space:]]*\$" \
      "$root/$s" && return 0
  done <<< "$scripts"
  return 1
}

# 0 if <file> sets pipefail before its first pipe-bearing non-comment line.
sets_pipefail_first() {
  local file="$1" pf pipe
  pf=$(grep -nE "$PIPEFAIL_RE" "$file" | head -n 1 | cut -d: -f1)
  [ -n "$pf" ] || return 1
  pipe=$(grep -nE "$PIPE_RE" "$file" | grep -vE '^[0-9]+:[[:space:]]*#' | head -n 1 | cut -d: -f1)
  [ -z "$pipe" ] || [ "$pf" -lt "$pipe" ]
}

# Checks <root> against <ratchet>. Prints one line per violation to stderr; prints
# "checked=<n> libraries=<n>" to stdout. Returns 1 on any violation.
check_pipefail() {
  local root="$1" ratchet="$2" scripts s missing="" bad="" n=0 libs=0 listed
  [ -f "$ratchet" ] || { echo "ratchet file missing: $ratchet" >&2; return 1; }
  scripts=$(bash_scripts "$root")
  while IFS= read -r s; do
    [ -n "$s" ] || continue
    if is_sourced_library "$root" "$s" "$scripts"; then libs=$((libs + 1)); continue; fi
    n=$((n + 1))
    if ! sets_pipefail_first "$root/$s"; then
      missing="${missing}${s}"$'\n'
      grep -qxF "$s" "$ratchet" \
        || bad="${bad}NEW script without pipefail before its first pipeline: ${s}"$'\n'
    fi
  done <<< "$scripts"
  while IFS= read -r listed; do
    case "$listed" in ''|'#'*) continue ;; esac
    [ -f "$root/$listed" ] || { bad="${bad}ratchet names a script that no longer exists: ${listed}"$'\n'; continue; }
    printf '%s' "$missing" | grep -qxF "$listed" \
      || bad="${bad}${listed} now sets pipefail (or left scope) — remove it from the ratchet"$'\n'
  done < "$ratchet"
  echo "checked=$n libraries=$libs"
  if [ -n "$bad" ]; then
    printf '%s' "$bad" >&2
    return 1
  fi
}

# A throwaway repository: <fixture> holds tracked files, ratchet at <fixture>/ratchet.txt.
make_fixture() {
  FX="$BATS_TEST_TMPDIR/fx"
  mkdir -p "$FX/platform/lib" "$FX/platform/svc" "$FX/.githooks"
  git init -q "$FX"
  # shellcheck disable=SC2016  # the literal ${LIB_DIR} is the fixture's content
  printf '#!/usr/bin/env bash\nset -euo pipefail\nsource "${LIB_DIR}/common.sh"\necho a | tail -1\n' > "$FX/platform/svc/deploy.sh"
  printf '#!/usr/bin/env bash\nhelper() { echo x | head -1; }\n' > "$FX/platform/lib/common.sh"
  printf '#!/usr/bin/env sh\nset -eu\necho a | tail -1\n' > "$FX/.githooks/pre-commit"
  printf '# ratchet\n' > "$FX/ratchet.txt"
  git -C "$FX" add platform .githooks
}

@test "every tracked bash script sets pipefail before its first pipeline, or is in the ratchet" {
  local out rc=0
  out=$(check_pipefail "$REPO_ROOT" "$RATCHET") || rc=$?
  [ "$rc" -eq 0 ]
  # Not vacuous: the deploy.sh population alone is over twenty scripts, and the
  # three shared libraries were recognised as libraries rather than skipped blind.
  local checked=${out#checked=}; checked=${checked%% *}
  [ "$checked" -ge 30 ] || { echo "only $checked scripts checked: $out"; false; }
  [ "$out" = "checked=$checked libraries=3" ] || { echo "library exemptions changed (review the new one, then update this count): $out"; false; }
}

@test "fixture: a clean tree with a sourced library and a POSIX sh hook passes" {
  make_fixture
  run check_pipefail "$FX" "$FX/ratchet.txt"
  [ "$status" -eq 0 ]
  [ "$output" = "checked=1 libraries=1" ]
}

@test "fixture: a new bash script without pipefail fails the check and is named" {
  make_fixture
  printf '#!/bin/bash\nset -e\nfind . | wc -l\n' > "$FX/platform/svc/entry.sh"
  git -C "$FX" add platform
  run check_pipefail "$FX" "$FX/ratchet.txt"
  [ "$status" -eq 1 ]
  assert_contains "$output" "NEW script without pipefail before its first pipeline: platform/svc/entry.sh"
}

@test "fixture: pipefail set AFTER the first pipeline does not count" {
  make_fixture
  printf '#!/usr/bin/env bash\nls | head -1\nset -o pipefail\n' > "$FX/platform/svc/late.sh"
  git -C "$FX" add platform
  run check_pipefail "$FX" "$FX/ratchet.txt"
  [ "$status" -eq 1 ]
  assert_contains "$output" "platform/svc/late.sh"
}

@test "fixture: a shebang-less .sh file is bash and is checked" {
  make_fixture
  printf 'echo a | tail -1\n' > "$FX/platform/svc/bare.sh"
  git -C "$FX" add platform
  run check_pipefail "$FX" "$FX/ratchet.txt"
  [ "$status" -eq 1 ]
  assert_contains "$output" "platform/svc/bare.sh"
}

@test "fixture: a lib/ script nothing sources is an executable and is checked" {
  make_fixture
  printf '#!/bin/bash\nset -e\nls | head -1\n' > "$FX/platform/lib/generate.sh"
  git -C "$FX" add platform
  run check_pipefail "$FX" "$FX/ratchet.txt"
  [ "$status" -eq 1 ]
  assert_contains "$output" "platform/lib/generate.sh"
}

@test "fixture: an offender named in the ratchet passes" {
  make_fixture
  printf '#!/bin/bash\nset -e\nfind . | wc -l\n' > "$FX/platform/svc/entry.sh"
  git -C "$FX" add platform
  printf 'platform/svc/entry.sh\n' >> "$FX/ratchet.txt"
  run check_pipefail "$FX" "$FX/ratchet.txt"
  [ "$status" -eq 0 ]
}

@test "fixture: a ratchet entry for a script that now sets pipefail fails (stale ratchet)" {
  make_fixture
  printf 'platform/svc/deploy.sh\n' >> "$FX/ratchet.txt"
  run check_pipefail "$FX" "$FX/ratchet.txt"
  [ "$status" -eq 1 ]
  assert_contains "$output" "platform/svc/deploy.sh now sets pipefail (or left scope) — remove it from the ratchet"
}

@test "fixture: a ratchet entry for a deleted script fails" {
  make_fixture
  printf 'platform/svc/gone.sh\n' >> "$FX/ratchet.txt"
  run check_pipefail "$FX" "$FX/ratchet.txt"
  [ "$status" -eq 1 ]
  assert_contains "$output" "ratchet names a script that no longer exists: platform/svc/gone.sh"
}

@test "the pipefail pattern accepts every enabling form and refuses +o" {
  local form
  for form in 'set -o pipefail' 'set -euo pipefail' 'set -Eeuo pipefail' \
    '  set -e -o pipefail' 'set -o errexit -o pipefail' 'set -euo pipefail  # strict'; do
    printf '%s\n' "$form" | grep -qE "$PIPEFAIL_RE" || { echo "rejected: $form"; false; }
  done
  for form in 'set +o pipefail' '# set -o pipefail' 'echo set -o pipefail' 'set -eu'; do
    refute_contains "$(printf '%s\n' "$form" | grep -E "$PIPEFAIL_RE" || true)" "pipefail" \
      || { echo "accepted: $form"; false; }
  done
}
