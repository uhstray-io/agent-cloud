# shellcheck shell=bash
# assert_helpers.bash — assertion helpers that actually fail the test.
#
# WHY THIS EXISTS. Bats runs a @test body under `set -e`, but bash's `set -e`
# deliberately ignores the status of two constructs, and both are the obvious way
# to write an assertion:
#
#   bang-inverted pipeline    (set -e is documented to ignore its status)
#   [[ "$x" == "$y" ]]      # the [[ ]] keyword does not trigger the failure path
#
# Measured on Bats 1.13.0: a false `[[ ]]` or a wrongly-true `! cmd` in the MIDDLE
# of a test body leaves the test PASSING. Only as the final statement does the
# body's exit status carry it. So every such assertion before the last line is
# decoration — it can never fail, and the code it guards is unprotected.
#
# This is not theoretical: an audit of this suite found 76 such assertions, and
# the negative ones are exactly the security-critical checks ("must NOT contain
# the loopback address", "must NOT source the config", "no playbook keeps its own
# copy of the guard").
#
# The helpers below work because a FUNCTION CALL is a simple command: its non-zero
# return status does trigger set -e, wherever it appears in the body. `[ ]` (the
# test builtin) also works and is fine to keep using.
#
# Usage:
#   load assert_helpers
#
#   refute_grep -qF 'forbidden' "$file"     # fails if the pattern IS present
#   assert_contains "$haystack" 'needle'    # fails if absent
#   refute_contains "$haystack" 'needle'    # fails if present

# Fails when grep FINDS something. Takes the same arguments as grep.
#
# Only exit status 1 — "matched nothing" — counts as the pattern being absent.
# Treating every nonzero status as absence is a false green: grep exits 2 for an
# unreadable or missing file, so a mistyped path made a "must NOT contain"
# assertion pass unconditionally. Verified: grep on a missing file returns 2.
refute_grep() {
  local rc=0
  grep "$@" >/dev/null 2>&1 || rc=$?
  case "$rc" in
    0) echo "refute_grep: forbidden pattern matched: grep $*" >&2; return 1 ;;
    1) return 0 ;;
    *) echo "refute_grep: grep failed (status $rc) — treating as a failure, not as absence: grep $*" >&2
       return 1 ;;
  esac
}

# Fails when grep finds NOTHING. Same arguments as grep.
#
# Distinguishes "not found" (1) from "grep could not run" (2+), so a bad path is
# reported as an error rather than as a missing pattern.
assert_grep() {
  local rc=0
  grep "$@" >/dev/null 2>&1 || rc=$?
  case "$rc" in
    0) return 0 ;;
    1) echo "assert_grep: expected pattern not found: grep $*" >&2; return 1 ;;
    *) echo "assert_grep: grep failed (status $rc): grep $*" >&2; return 1 ;;
  esac
}

# Substring assertions over a string, replacing `[[ "$x" == *y* ]]`.
assert_contains() {
  case "$1" in
    *"$2"*) return 0 ;;
    *) echo "assert_contains: expected to find [$2]" >&2; return 1 ;;
  esac
}

refute_contains() {
  case "$1" in
    *"$2"*) echo "refute_contains: found forbidden [$2]" >&2; return 1 ;;
    *) return 0 ;;
  esac
}
