#!/usr/bin/env bats
# A ratchet on assertions that cannot fail.
#
# Bats runs a @test body under `set -e`, but bash's `set -e` deliberately ignores
# the status of two constructs — and both are the natural way to write an
# assertion:
#
#   ! some_command       # `!`-inverted pipelines are exempt from set -e
#   [[ "$a" == "$b" ]]   # the [[ ]] keyword does not trigger the failure path
#
# Measured on Bats 1.13.0: a false `[[ ]]`, or a wrongly-true `! cmd`, anywhere
# except the FINAL statement of a test leaves that test PASSING. Such an
# assertion is decoration — it can never fail, and the thing it guards is
# unprotected. The negative form is the more dangerous of the two, because
# "must NOT contain <dangerous thing>" is exactly what a security assertion looks
# like.
#
# Converting them is mechanical (platform/tests/assert_helpers.bash: a function
# CALL is a simple command, so its status does fail the test), but there is a
# backlog. This test ratchets: the count may go DOWN freely and may not go UP.
# When it goes down, lower BASELINE in the same commit.
#
# Run: bats platform/tests/test_assertions_are_real.bats

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  # Lower this whenever assertions are converted. Never raise it.
  BASELINE=53
}

@test "no new assertions that cannot fail" {
  run python3 -c "
import glob, re, sys, os
root = os.environ.get('REPO_ROOT') or '$REPO_ROOT'
neg = dbl = 0
offenders = []
for f in sorted(glob.glob(os.path.join(root, 'platform/tests/*.bats'))):
    lines = open(f).read().split('\n')
    blocks, cur = [], None
    for i, l in enumerate(lines):
        if l.startswith('@test '):
            cur = [i, None]
        elif l == '}' and cur:
            cur[1] = i; blocks.append(tuple(cur)); cur = None
    for a, b in blocks:
        st = [(i, lines[i]) for i in range(a + 1, b)
              if lines[i].strip() and not lines[i].strip().startswith('#')]
        if not st:
            continue
        last = st[-1][0]
        for i, l in st:
            if i == last:
                continue
            if re.match(r'\s*!\s', l):
                neg += 1; offenders.append(f'{os.path.basename(f)}:{i+1} {l.strip()[:60]}')
            elif re.match(r'\s*\[\[', l):
                dbl += 1; offenders.append(f'{os.path.basename(f)}:{i+1} {l.strip()[:60]}')
print('TOTAL %d (neg=%d dbl=%d)' % (neg + dbl, neg, dbl))
for o in offenders[:8]:
    print('  ' + o)
"
  [ "$status" -eq 0 ]
  local total
  total=$(echo "$output" | sed -n 's/^TOTAL \([0-9]*\).*/\1/p')
  [ -n "$total" ]
  # May go DOWN freely; may not go UP.
  [ "$total" -le "$BASELINE" ]
}

@test "the assertion helpers exist and fail mid-body" {
  local h="$REPO_ROOT/platform/tests/assert_helpers.bash"
  [ -f "$h" ]
  # Each helper must RETURN non-zero rather than using `!` or `[[ ]]` internally,
  # since a helper that cannot fail is worse than the construct it replaces.
  run grep -c 'return 1' "$h"
  [ "$status" -eq 0 ]
  [ "$output" -ge 4 ]
}
