#!/usr/bin/env bats
# Every Bash script tracked under platform/ and .githooks/ sets pipefail, and has it
# on at every pipeline.
#
# Why: a pipeline's exit status is its LAST command's. Without pipefail, `cmd | tail`
# succeeds when cmd fails, and `set -e` never fires. docs/MISTAKES.md 1.3 and 8.1
# record that exact masking reporting failures as success, each enforced only by
# convention. This file moves the script half of that rule into a test.
#
# WHAT IS CHECKED. The population is derived from `git ls-files`, never hand-typed:
#   - a tracked file whose first line is a bash shebang (`#!/bin/bash`,
#     `#!/usr/bin/env bash`, `#!/usr/bin/env -S bash -e`), whatever its name — this
#     includes `*.sh.j2` templates that render to a bash script; or
#   - a tracked `*.sh` file with no shebang at all.
# Such a script must turn pipefail on (`set -o pipefail`, or a combined form such as
# `set -euo pipefail`), and pipefail must be ON at every non-comment line containing
# a single `|`. The option is tracked line by line: a later `set +o pipefail` leaves
# the pipelines after it uncovered. The pipe match is deliberately coarse: a `case`
# alternative or a `|` inside a string also counts, which can only make the rule
# stricter.
#
# WHAT IS NOT CHECKED, AND WHY.
#   - POSIX sh scripts (`#!/bin/sh`, `#!/usr/bin/env sh` — the .githooks hooks and
#     platform/local-dev/https-forward.sh). The shebang decides, not the extension.
#     `set -o pipefail` is not portable to every /bin/sh: dash, Debian's /bin/sh,
#     rejects it (`dash: 1: set: Illegal option -o pipefail`, verified 2026-10-02),
#     so requiring it would break the script. Pipe hygiene there stays a convention.
#   - Sourced libraries. A file in a `lib/` directory that an in-scope script
#     provably `source`s (or `.`s) BY ITS EXACT PATH — resolved from the script's own
#     directory through the variable chain it builds (see "Resolving what a script
#     sources" below) — is a library: it runs inside its caller's shell and inherits
#     the caller's options, and that caller is itself in the population and so must
#     set pipefail. Setting options in a library would also mutate every caller's
#     shell as a side effect. A `lib/` file nothing provably sources (for example
#     netbox's lib/generate-secrets.sh) is an executable and IS checked; a source
#     line that cannot be resolved exempts nothing.
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
# not match; PIPEFAIL_OFF_RE matches that instead. Neither uses a backslash, because
# awk -v processes escapes in the value it is given.
PIPEFAIL_RE='^[[:space:]]*set[[:space:]]+([^#;]*[[:space:]])?-[[:alpha:]]*o[[:space:]]+pipefail([[:space:];]|$)'
PIPEFAIL_OFF_RE='^[[:space:]]*set[[:space:]]+([^#;]*[[:space:]])?[+][[:alpha:]]*o[[:space:]]+pipefail([[:space:];]|$)'
# A single `|` (not `||`) on a non-comment line.
PIPE_RE='(^|[^|])[|]([^|]|$)'
# bash as the interpreter: `#!/bin/bash`, `#!/usr/local/bin/bash`, `#!/usr/bin/env bash`,
# and env with options or assignments before it (`#!/usr/bin/env -S bash -e`,
# `#!/usr/bin/env -u X bash`, `#!/usr/bin/env FOO=1 bash`).
BASH_SHEBANG_RE='^#![[:space:]]*(/[^[:space:]]*/)?(env([[:space:]]+(-u[[:space:]]+[A-Za-z_][A-Za-z0-9_]*|-[^[:space:]]+|[A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*))*[[:space:]]+)?(/[^[:space:]]*/)?bash([[:space:]]|$)'

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

# ── Resolving what a script sources ─────────────────────────────────────────
# A library is exempt only when a script is PROVEN to source that exact path; two
# files both named common.sh must not vouch for each other. Paths are resolved
# symbolically, never by executing repository code, and only for the forms these
# scripts actually use:
#   X="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"     the script's own directory
#   X="$(dirname "$Y")" (nested), X="${Y}/lib", X="$Y/../x"  built from resolved vars
#   source "${X}/common.sh"  /  . "$X/../../lib/common.sh"
# Anything else — an unknown variable, a default expansion, a backtick, a path that
# climbs out of the repository — is unresolved, and an unresolved source exempts
# nothing. Paths carry a /R prefix standing for the repository root.
# Bash 3.2 compatible (macOS /bin/bash runs bats): no associative arrays.

# Normalises an absolute /R/... path (`.`, `..`, doubled slashes). Fails if it leaves /R.
norm_path() {
  local part
  local -a parts out=()
  case "$1" in /R|/R/*) ;; *) return 1 ;; esac
  IFS=/ read -r -a parts <<< "${1#/}"
  for part in "${parts[@]}"; do
    case "$part" in
      ''|.) ;;
      ..) [ "${#out[@]}" -gt 1 ] || return 1
          unset "out[$(( ${#out[@]} - 1 ))]"; out=("${out[@]}") ;;
      *) out+=("$part") ;;
    esac
  done
  local IFS=/
  printf '/%s\n' "${out[*]}"
}

# The last value recorded for <name> in <vars> ("NAME=value" lines). Fails when the
# name was never assigned or its last assignment did not resolve ("!").
lookup_var() {
  local l v="" found=1
  while IFS= read -r l; do
    case "$l" in "$1="*) v=${l#*=}; found=0 ;; esac
  done <<< "$2"
  [ "$found" -eq 0 ] && [ "$v" != '!' ] && printf '%s' "$v"
}

# Resolves one shell word (outer quotes already removed) to a normalised /R path.
#   resolve_word <word> <script dir as /R path> <vars>
resolve_word() {
  local e="$1" val d m
  # shellcheck disable=SC2016  # the literal text of the idiom is what is matched
  local self_dir='$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)'
  # ${NAME} or $NAME only: `${NAME:-default}` and other expansions stay unresolved.
  local var_re='[$]([{]([A-Z_][A-Z0-9_]*)[}]|([A-Z_][A-Z0-9_]*))'
  local dirname_re='[$][(]dirname "([^"$]*)"[)]'
  e=${e//"$self_dir"/$2}
  while [[ $e =~ $var_re ]]; do
    m=${BASH_REMATCH[0]}
    val=$(lookup_var "${BASH_REMATCH[2]}${BASH_REMATCH[3]}" "$3") || return 1
    e=${e/"$m"/$val}
  done
  while [[ $e =~ $dirname_re ]]; do
    m=${BASH_REMATCH[0]}
    d=$(norm_path "${BASH_REMATCH[1]}") || return 1
    d=${d%/*}
    [ -n "$d" ] || return 1
    e=${e/"$m"/$d}
  done
  case "$e" in *'$'*|*'"'*|*'`'*) return 1 ;; esac
  norm_path "$e"
}

# Prints, repository-relative, every path <rel> provably sources.
sourced_paths() {
  local root="$1" rel="$2" line vars="" sd p
  local assign_re='^[[:space:]]*([A-Z_][A-Z0-9_]*)="(.*)"[[:space:]]*$'
  local source_re='^[[:space:]]*(source|[.])[[:space:]]+"?([^"[:space:]]+)"?[[:space:]]*$'
  sd="/R/${rel%/*}"
  [ "$sd" != "/R/$rel" ] || sd=/R
  while IFS= read -r line || [ -n "$line" ]; do
    if [[ $line =~ $assign_re ]]; then
      p=$(resolve_word "${BASH_REMATCH[2]}" "$sd" "$vars") || p='!'
      vars="${vars}${BASH_REMATCH[1]}=${p}"$'\n'
    elif [[ $line =~ $source_re ]]; then
      p=$(resolve_word "${BASH_REMATCH[2]}" "$sd" "$vars") && printf '%s\n' "${p#/R/}"
    fi
  done < "$root/$rel"
}

# 0 if <rel> is a sourced library: in a lib/ directory AND its exact path is in
# <sourced> (the union of sourced_paths over every in-scope script).
is_sourced_library() {
  local rel="$1" sourced="$2"
  case "${rel%/*}" in lib|*/lib) ;; *) return 1 ;; esac
  printf '%s\n' "$sourced" | grep -qxF "$rel"
}

# 0 if <file> turns pipefail on AND it is on at every pipe-bearing non-comment line.
# The first half keeps a script with no pipeline yet honest for the one it gains
# later. The option is tracked line by line, so `set -o pipefail` later undone by
# `set +o pipefail` does not cover the pipelines after it; the last set before a
# pipeline wins. A set and a pipe on one line count set-first.
pipefail_covers_pipes() {
  awk -v on_re="$PIPEFAIL_RE" -v off_re="$PIPEFAIL_OFF_RE" -v pipe_re="$PIPE_RE" '
    /^[[:space:]]*#/ { next }
    $0 ~ on_re  { on = 1; seen = 1 }
    $0 ~ off_re { on = 0 }
    $0 ~ pipe_re && !on { bad = 1; exit }
    END { exit (bad || !seen) }
  ' "$1"
}

# Checks <root> against <ratchet>. Prints one line per violation to stderr; prints
# "checked=<n> libraries=<n>" to stdout. Returns 1 on any violation.
check_pipefail() {
  local root="$1" ratchet="$2" scripts s sourced="" missing="" bad="" n=0 libs=0 listed
  [ -f "$ratchet" ] || { echo "ratchet file missing: $ratchet" >&2; return 1; }
  scripts=$(bash_scripts "$root")
  while IFS= read -r s; do
    [ -n "$s" ] || continue
    sourced="${sourced}$(sourced_paths "$root" "$s")"$'\n'
  done <<< "$scripts"
  while IFS= read -r s; do
    [ -n "$s" ] || continue
    if is_sourced_library "$s" "$sourced"; then libs=$((libs + 1)); continue; fi
    n=$((n + 1))
    if ! pipefail_covers_pipes "$root/$s"; then
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
# The deploy script sources platform/lib/common.sh with the exact variable chain the
# real deploy scripts use.
make_fixture() {
  FX="$BATS_TEST_TMPDIR/fx"
  mkdir -p "$FX/platform/lib" "$FX/platform/svc" "$FX/platform/services/svc/deployment" "$FX/.githooks"
  git init -q "$FX"
  cat > "$FX/platform/services/svc/deployment/deploy.sh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$(dirname "$(dirname "$(dirname "$SCRIPT_DIR")")")/lib"
source "${LIB_DIR}/common.sh"
echo a | tail -1
SH
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
  printf 'platform/services/svc/deployment/deploy.sh\n' >> "$FX/ratchet.txt"
  run check_pipefail "$FX" "$FX/ratchet.txt"
  [ "$status" -eq 1 ]
  assert_contains "$output" "platform/services/svc/deployment/deploy.sh now sets pipefail (or left scope) — remove it from the ratchet"
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

@test "the bash shebang pattern accepts env -S and env options, and refuses other interpreters" {
  local sb
  for sb in '#!/bin/bash' '#! /bin/bash' '#!/usr/local/bin/bash' '#!/usr/bin/env bash' \
    '#!/usr/bin/env -S bash' '#!/usr/bin/env -S bash -e' '#!/usr/bin/env -S bash -euo pipefail' \
    '#!/usr/bin/env -u FOO bash' '#!/usr/bin/env FOO=1 bash' '#!/bin/bash -e'; do
    printf '%s\n' "$sb" | grep -qE "$BASH_SHEBANG_RE" || { echo "rejected: $sb"; false; }
  done
  for sb in '#!/bin/sh' '#!/usr/bin/env sh' '#!/usr/bin/env bats' '#!/usr/bin/env python3' \
    '#!/bin/bashful' '#!/usr/bin/env -S sh -c bash' '#!/bin/sh bash'; do
    refute_contains "$(printf '%s\n' "$sb" | grep -E "$BASH_SHEBANG_RE" || true)" "#!" \
      || { echo "accepted: $sb"; false; }
  done
}

@test "fixture: an extensionless env -S bash script without pipefail is checked" {
  make_fixture
  printf '#!/usr/bin/env -S bash -e\nls | head -1\n' > "$FX/platform/svc/runner"
  git -C "$FX" add platform
  run check_pipefail "$FX" "$FX/ratchet.txt"
  [ "$status" -eq 1 ]
  assert_contains "$output" "NEW script without pipefail before its first pipeline: platform/svc/runner"
}

@test "fixture: an unsourced lib/common.sh is checked even when another common.sh is sourced" {
  make_fixture
  mkdir -p "$FX/platform/other/lib"
  printf '#!/usr/bin/env bash\nls | head -1\n' > "$FX/platform/other/lib/common.sh"
  git -C "$FX" add platform
  run check_pipefail "$FX" "$FX/ratchet.txt"
  [ "$status" -eq 1 ]
  assert_contains "$output" "NEW script without pipefail before its first pipeline: platform/other/lib/common.sh"
  refute_contains "$output" "platform/lib/common.sh"
}

@test "fixture: a source line that cannot be resolved exempts nothing" {
  make_fixture
  # shellcheck disable=SC2016  # the literal ${UNKNOWN} is the fixture's content
  sed -i.bak 's|source "${LIB_DIR}/common.sh"|source "${UNKNOWN}/common.sh"|' \
    "$FX/platform/services/svc/deployment/deploy.sh"
  rm "$FX/platform/services/svc/deployment/deploy.sh.bak"
  run check_pipefail "$FX" "$FX/ratchet.txt"
  [ "$status" -eq 1 ]
  assert_contains "$output" "NEW script without pipefail before its first pipeline: platform/lib/common.sh"
}

@test "source resolution follows the forms the real scripts use, and refuses the rest" {
  local vars sd=/R/platform/services/x/deployment
  vars="SCRIPT_DIR=$sd"$'\n'"ROOT_DIR=/R/platform/services/x"$'\n'"LIB_DIR=/R/platform/lib"$'\n'"BAD=!"
  # shellcheck disable=SC2016  # literal shell words are the input under test
  {
    [ "$(resolve_word '$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)' "$sd" "")" = "$sd" ]
    [ "$(resolve_word '$(dirname "$(dirname "$(dirname "$SCRIPT_DIR")")")/lib' "$sd" "$vars")" = /R/platform/lib ]
    [ "$(resolve_word '${LIB_DIR}/common.sh' "$sd" "$vars")" = /R/platform/lib/common.sh ]
    [ "$(resolve_word '${SCRIPT_DIR}/../../../lib/common.sh' "$sd" "$vars")" = /R/platform/lib/common.sh ]
    [ "$(resolve_word '${ROOT_DIR}/lib/common.sh' "$sd" "$vars")" = /R/platform/services/x/lib/common.sh ]
    run resolve_word '${UNKNOWN}/common.sh' "$sd" "$vars"; [ "$status" -ne 0 ]
    run resolve_word '${BAD}/common.sh' "$sd" "$vars"; [ "$status" -ne 0 ]
    run resolve_word '${LIB_DIR:-/x}/common.sh' "$sd" "$vars"; [ "$status" -ne 0 ]
    run resolve_word '${SCRIPT_DIR}/../../../../../common.sh' "$sd" "$vars"; [ "$status" -ne 0 ]
    run resolve_word 'lib/common.sh' "$sd" "$vars"; [ "$status" -ne 0 ]
  }
}

@test "fixture: pipefail turned off again does not cover the pipelines after it" {
  make_fixture
  printf '#!/usr/bin/env bash\nset -o pipefail\nset +o pipefail\nls | head -1\n' > "$FX/platform/svc/off.sh"
  git -C "$FX" add platform
  run check_pipefail "$FX" "$FX/ratchet.txt"
  [ "$status" -eq 1 ]
  assert_contains "$output" "platform/svc/off.sh"
}

@test "fixture: pipefail turned back on before a pipeline covers it" {
  make_fixture
  printf '#!/usr/bin/env bash\nset +o pipefail\nset -euo pipefail\nls | head -1\n' > "$FX/platform/svc/on.sh"
  git -C "$FX" add platform
  run check_pipefail "$FX" "$FX/ratchet.txt"
  [ "$status" -eq 0 ]
}
