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
# A second check, a ratchet LIST rather than a count, covers two more shapes that
# pass whatever the subject contains (docs/MISTAKES.md 2.10):
#
#   grep -v ... -q PATTERN   # exits 0 when ANY line lacks PATTERN, so it passes on a
#                            # file that DOES contain it. Every spelling: -vq, -qv,
#                            # -v -q, --invert-match --quiet/--silent, and the same
#                            # flags handed to assert_grep / refute_grep.
#   <assertion> || true      # discards the only status that could have failed.
#
# Scope, deliberately narrow so cleanup is not mistaken for an assertion:
#   - only @test bodies. setup(), teardown() and helper functions are not scanned —
#     `podman rm -f x || true` there is cleanup, not a claim;
#   - comments, quoted strings (so `run bash -c "... || true"` and `"command -v x"`)
#     and heredoc bodies are masked before matching;
#   - `|| true` counts only at statement level, on a statement whose command words
#     include grep / egrep / fgrep / [ / [[ / test / diff / cmp / assert_* / refute_*.
#     A capture (`n=$(grep -c . f || true)`, `local x=...`, a multi-line `v=$( ... )`),
#     `run ...`, and a non-assertion command (`kill "$pid" || true`) are not flagged.
# Known limitation: code inside a quoted string handed to `bash -c` is not parsed.
#
# platform/tests/known_inert_assertions.txt lists the offenders that existed when
# this landed. Both directions are enforced: an offender not listed fails, and a
# listed entry that no longer matches fails — so the file only ever shrinks.
#
# Run: bats platform/tests/test_assertions_are_real.bats

load assert_helpers

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  # Lower this whenever assertions are converted. Never raise it.
  BASELINE=53
  RATCHET="$REPO_ROOT/platform/tests/known_inert_assertions.txt"
}

# _scan_inert <dir of *.bats> <ratchet file>
# Prints NEW / STALE lines and a SCANNED summary; exits 1 on any NEW or STALE.
# Ratchet entries are `<shape>TAB<file>TAB<stripped source line>` — keyed on the
# text, not the line number, so an unrelated edit above an entry does not move it.
_scan_inert() {
  python3 - "$@" <<'PY'
import glob, os, re, sys

# argv: <dir holding *.bats> <ratchet file>
src_dir, ratchet = sys.argv[1], sys.argv[2]

VERBS = {'grep', 'egrep', 'fgrep', '[', '[[', 'test', 'diff', 'cmp'}
GREP_CMDS = {'grep', 'egrep', 'fgrep', 'assert_grep', 'refute_grep'}
KEYWORDS = {'if', 'then', 'elif', 'else', 'do', 'while', 'until', '{', '(', '!', 'time'}


def mask(body):
    """Return (code, depth): `code` is `body` with quoted text, comments and
    heredoc bodies replaced by '_' (newlines kept, so offsets and line numbers
    survive); depth[i] is how many $( ) / <( ) substitutions enclose char i."""
    out, depth = list(body), [0] * len(body)
    stack = []                 # 'sub', 'par', 'dq', 'sq'
    pending_heredocs = []      # (delimiter, strip_tabs)
    i, n = 0, len(body)

    def subs():
        return sum(1 for s in stack if s == 'sub')

    while i < n:
        c = body[i]
        top = stack[-1] if stack else 'code'
        depth[i] = subs()
        if top == 'sq':
            if c == "'":
                stack.pop()
            out[i] = '_' if c != '\n' else '\n'
            i += 1
            continue
        if top == 'dq':
            if c == '\\' and i + 1 < n:
                out[i] = out[i + 1] = '_'
                if body[i + 1] == '\n':
                    out[i + 1] = '\n'
                depth[i + 1] = subs()
                i += 2
                continue
            if c == '"':
                stack.pop(); out[i] = '_'; i += 1; continue
            if body.startswith('$(', i):
                stack.append('sub'); depth[i] = depth[i + 1] = subs(); i += 2; continue
            out[i] = '_' if c != '\n' else '\n'
            i += 1
            continue
        # code / sub / par
        if c == '\n':
            for delim, strip in pending_heredocs:
                j = i + 1
                while j < n:
                    k = body.find('\n', j)
                    k = n if k == -1 else k
                    line = body[j:k]
                    for p in range(j, k):
                        out[p] = '_'; depth[p] = subs()
                    j = k + 1
                    if (line.lstrip('\t') if strip else line) == delim:
                        break
                i = j - 1
            pending_heredocs = []
            i += 1
            continue
        if c == '\\' and i + 1 < n:
            out[i] = ' '
            if body[i + 1] == '\n':
                out[i + 1] = ' '          # line continuation joins the statement
            depth[i + 1] = subs()
            i += 2
            continue
        if c == '#' and (i == 0 or body[i - 1] in ' \t\n;'):
            while i < n and body[i] != '\n':
                out[i] = '_'; depth[i] = subs(); i += 1
            continue
        if c == "'":
            stack.append('sq'); out[i] = '_'; i += 1; continue
        if c == '"':
            stack.append('dq'); out[i] = '_'; i += 1; continue
        if body.startswith('$(', i) or body.startswith('<(', i):
            stack.append('sub'); depth[i] = depth[i + 1] = subs(); i += 2; continue
        if c == '(':
            stack.append('par'); i += 1; continue
        if c == ')' and top in ('sub', 'par'):
            stack.pop(); i += 1; continue
        m = re.match(r'<<(-?)\s*([\'"]?)([A-Za-z_][A-Za-z0-9_]*)\2', body[i:])
        if m and not body.startswith('<<<', i):
            pending_heredocs.append((m.group(3), m.group(1) == '-'))
            i += m.end()
            continue
        i += 1
    return ''.join(out), depth


def test_bodies(text):
    lines = text.split('\n')
    cur = None
    for i, l in enumerate(lines):
        if l.startswith('@test '):
            cur = i
        elif l == '}' and cur is not None:
            yield cur + 1, '\n'.join(lines[cur + 1:i])
            cur = None


def first_words(stmt):
    """Command words of a depth-0 statement: the first word of each pipeline
    element / list element, keywords skipped."""
    words = []
    for piece in re.split(r'\|\|?|&&|;', stmt):
        toks = piece.split()
        while toks and toks[0] in KEYWORDS:
            toks = toks[1:]
        if toks:
            words.append(toks[0])
    return words


found = {}    # key -> 'file:line'
n_tests = 0
for f in sorted(glob.glob(os.path.join(src_dir, '*.bats'))):
    base = os.path.basename(f)
    for start, body in test_bodies(open(f).read()):
        n_tests += 1
        code, depth = mask(body)
        src_lines = body.split('\n')

        def where(pos):
            ln = body.count('\n', 0, pos)
            return ln, src_lines[ln].strip()

        # (a) grep -v ... -q: exit status answers "is there ANY line without the
        # pattern", which is true for almost every file containing the pattern.
        for m in re.finditer(r'(?<![\w./-])(e?grep|fgrep|assert_grep|refute_grep)(?![\w-])', code):
            seg = re.split(r'[|;&()<>\n]', code[m.end():], maxsplit=1)[0]
            flags, longs = set(), set()
            for t in seg.split():
                if t == '--':
                    break
                if t.startswith('--'):
                    longs.add(t.split('=')[0])
                elif re.fullmatch(r'-[A-Za-z]+', t):
                    flags.update(t[1:])
            inv = 'v' in flags or '--invert-match' in longs
            quiet = 'q' in flags or longs & {'--quiet', '--silent'}
            if inv and quiet:
                ln, txt = where(m.start())
                found.setdefault(f'grep-v-q\t{base}\t{txt}', f'{base}:{start + ln + 1}')

        # (b) `|| true` on an assertion: discards the only status that could fail.
        # Counted only at statement level (depth 0) — `x=$(grep -c . f || true)`
        # is a capture, not an assertion. Substitution contents are blanked from
        # the statement, so a capture or `run ...` never shows a verb at depth 0.
        for m in re.finditer(r'\|\|\s*true(?![\w-])', code):
            if depth[m.start()] != 0:
                continue
            s = max(code.rfind('\n', 0, m.start()), code.rfind(';', 0, m.start())) + 1
            e = m.end()
            stmt = ''.join(ch if depth[p] == 0 else ' ' for p, ch in enumerate(code[s:e], s))
            words = first_words(stmt)
            if any(w in VERBS or w.startswith(('assert_', 'refute_')) for w in words):
                ln, txt = where(s if code[s] != '\n' else s + 1)
                found.setdefault(f'or-true\t{base}\t{txt}', f'{base}:{start + ln + 1}')

listed = set()
for l in open(ratchet):
    l = l.rstrip('\n')
    if l and not l.startswith('#'):
        listed.add(l)

bad = 0
for k in sorted(found):
    if k not in listed:
        bad += 1
        print(f'NEW inert assertion at {found[k]} — not in the ratchet: {k!r}')
for k in sorted(listed - set(found)):
    bad += 1
    print(f'STALE ratchet entry (no longer matches any assertion) — remove it: {k!r}')
print(f'SCANNED {n_tests} tests, {len(found)} inert, {len(listed)} listed')
sys.exit(1 if bad else 0)
PY
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

@test "no new grep -v -q or || true assertions outside the ratchet" {
  [ -f "$RATCHET" ]
  run _scan_inert "$REPO_ROOT/platform/tests" "$RATCHET"
  echo "$output"
  [ "$status" -eq 0 ]
  # Not vacuous: the scan really walked the suite's test bodies.
  local n
  n=$(echo "$output" | sed -n 's/^SCANNED \([0-9]*\) tests.*/\1/p')
  [ -n "$n" ]
  [ "$n" -ge 500 ]
}

@test "inert-assertion scan: flags every grep -v -q spelling" {
  local d="$BATS_TEST_TMPDIR/fx"
  mkdir -p "$d"
  printf '%s\n' '@test "x" {' \
    '  printf %s "$report" | grep -vqF _existing.results' \
    '  grep -v -q x f' \
    '  grep -qv x f' \
    '  grep --invert-match --quiet x f' \
    '  refute_grep -vq x f' \
    '  [ "$(grep -vq x f && echo y)" = y ]' \
    '  true' '}' > "$d/bad.bats"
  : > "$d/ratchet.txt"
  run _scan_inert "$d" "$d/ratchet.txt"
  echo "$output"
  [ "$status" -ne 0 ]
  [ "$(printf '%s\n' "$output" | grep -c '^NEW .*grep-v-q')" -eq 6 ]
}

@test "inert-assertion scan: flags || true on an assertion" {
  local d="$BATS_TEST_TMPDIR/fx"
  mkdir -p "$d"
  printf '%s\n' '@test "x" {' \
    '  grep -q x f || true' \
    '  [ -f x ] || true' \
    '  assert_grep -qF x f || true' \
    '  if grep -q x f || true; then :; fi' \
    '  grep -E a f \' \
    '    | grep -q b || true' \
    '  true' '}' > "$d/bad.bats"
  : > "$d/ratchet.txt"
  run _scan_inert "$d" "$d/ratchet.txt"
  echo "$output"
  [ "$status" -ne 0 ]
  [ "$(printf '%s\n' "$output" | grep -c '^NEW .*or-true')" -eq 5 ]
}

@test "inert-assertion scan: leaves cleanup, captures, comments and strings alone" {
  local d="$BATS_TEST_TMPDIR/fx"
  mkdir -p "$d"
  printf '%s\n' \
    'setup() {' '  grep -vq x f || true' '  podman rm -f x || true' '}' \
    'teardown() { kill "$pid" || true; }' \
    'helper() {' '  grep -q x f || true' '}' \
    '@test "x" {' \
    '  # grep -vq x f || true' \
    '  n=$(grep -c . f || true)' \
    '  local hits=$(grep -q x f || true)' \
    '  v=$(' '    grep -nE x f \' '      | grep -viE y || true' '  )' \
    '  sh "$HOOK" origin >/dev/null 2>&1 || true' \
    '  run bash -c "grep -q x f || true; grep -vq y f"' \
    '  grep -q "command -v podman" f' \
    '  refute_grep -qE "a|b" <(grep -vE "^#" "$pb")' \
    '  refute_contains "$(printf x | grep -v R || true)" diff-filter' \
    '  cat > f <<EOF' 'grep -vq x f || true' '[ -f x ] || true' 'EOF' \
    '  kill "$pid" || true' \
    '  true' '}' > "$d/good.bats"
  : > "$d/ratchet.txt"
  run _scan_inert "$d" "$d/ratchet.txt"
  echo "$output"
  [ "$status" -eq 0 ]
  # The body was scanned, not skipped: one test found, nothing flagged.
  [ "$(printf '%s\n' "$output" | grep -c '^SCANNED 1 tests, 0 inert')" -eq 1 ]
}

@test "inert-assertion scan: a listed offender passes, a stale entry fails" {
  local d="$BATS_TEST_TMPDIR/fx"
  mkdir -p "$d"
  printf '%s\n' '@test "x" {' '  grep -vq x f' '  true' '}' > "$d/bad.bats"
  printf '# header\ngrep-v-q\tbad.bats\tgrep -vq x f\n' > "$d/ratchet.txt"
  run _scan_inert "$d" "$d/ratchet.txt"
  echo "$output"
  [ "$status" -eq 0 ]
  # The offender is fixed but its entry stays behind: the ratchet must say so.
  printf '%s\n' '@test "x" {' '  refute_grep -q x f' '  true' '}' > "$d/bad.bats"
  run _scan_inert "$d" "$d/ratchet.txt"
  echo "$output"
  [ "$status" -ne 0 ]
  [ "$(printf '%s\n' "$output" | grep -c '^STALE ')" -eq 1 ]
}

@test "the assertion helpers exist" {
  [ -f "$REPO_ROOT/platform/tests/assert_helpers.bash" ]
}

# The helpers are the mechanism this whole file relies on, so their BEHAVIOUR is
# asserted, not their source text. Counting `return 1` occurrences passed while a
# helper returned the wrong status — which is the same "assert a token, not the
# construct" mistake the suite keeps making.
#
# `run` captures the status instead of letting it fail this test, which is what
# makes it possible to assert that a helper FAILS.

@test "refute_grep: fails when the pattern is present" {
  printf 'needle\n' > "$BATS_TEST_TMPDIR/f"
  run refute_grep -qF 'needle' "$BATS_TEST_TMPDIR/f"
  [ "$status" -ne 0 ]
}

@test "refute_grep: passes when the pattern is absent" {
  printf 'needle\n' > "$BATS_TEST_TMPDIR/f"
  run refute_grep -qF 'haystack' "$BATS_TEST_TMPDIR/f"
  [ "$status" -eq 0 ]
}

@test "refute_grep: fails on an unreadable path rather than reading it as absence" {
  # grep exits 2 for a missing file. Treating every nonzero status as absence made
  # a mistyped path pass any "must NOT contain" assertion unconditionally.
  run refute_grep -qF 'needle' "$BATS_TEST_TMPDIR/does-not-exist"
  [ "$status" -ne 0 ]
}

@test "assert_grep: passes when present, fails when absent, fails on a bad path" {
  printf 'needle\n' > "$BATS_TEST_TMPDIR/f"
  run assert_grep -qF 'needle' "$BATS_TEST_TMPDIR/f"
  [ "$status" -eq 0 ]
  run assert_grep -qF 'haystack' "$BATS_TEST_TMPDIR/f"
  [ "$status" -ne 0 ]
  run assert_grep -qF 'needle' "$BATS_TEST_TMPDIR/nope"
  [ "$status" -ne 0 ]
}

@test "assert_contains / refute_contains: both directions" {
  run assert_contains "haystack" "stack"
  [ "$status" -eq 0 ]
  run assert_contains "haystack" "needle"
  [ "$status" -ne 0 ]
  run refute_contains "haystack" "needle"
  [ "$status" -eq 0 ]
  run refute_contains "haystack" "stack"
  [ "$status" -ne 0 ]
}
