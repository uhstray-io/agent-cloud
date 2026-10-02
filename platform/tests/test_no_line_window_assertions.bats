#!/usr/bin/env bats
# Refuses line-count windows as assertion subjects (docs/MISTAKES.md 2.6, 2.8).
#
# A window sized by a guessed line count — `grep -A14 '<anchor>'`, `sed -n '3,9p'`,
# `head -20 file` — breaks the first time a line is added inside the construct it
# was meant to cover, often a comment explaining the very property under test. The
# subject was right and the test was wrong, three times over (2.6), and then a
# fourth (2.8). The fix shape is to scope by construct: `task_block` from
# assert_helpers.bash, or `sed -n '/<start>/,/<next boundary>/p'`, then assert on
# that.
#
# A RATCHET, the same shape as known_unguarded_bao_plays.txt (2.18): the population
# is derived from every *.bats and *.bash file in platform/tests/, and
#   - a window not named in known_line_window_assertions.txt fails  (no new ones)
#   - an entry there that no longer occurs fails                    (list only shrinks)
# Existing offenders are listed, not fixed: rewriting ~30 assertions is its own change.
#
# HEURISTIC — what counts as a window, on an executable line (comment lines are
# skipped, so explaining the hazard is never punished — 2.8):
#   grep with -A/-B/-C N (any spelling: -A3, -A 3, -nA3, --after-context=3)
#   sed in quiet mode (-n, -En, -n -E, --quiet) with a numeric range 'N,Mp'
#     (spaces allowed: '3,9 p'), a relative range ',+N' / ',~N', or a single
#     numeric line 'Np' other than line 1
#   head / tail with a line count N > 1 (-n N, -N, --lines=N, --lines N)
# A backslash-continued command is joined before scanning and reported by its
# first physical line, so `grep -F x \` + `-A3 f` on the next line is still seen.
# Not windows, and deliberately exempt:
#   head/tail -1 (and -n 1): the first or last line/match is a position, not a width
#     (shebang checks, `grep -n X | head -1 | cut -d: -f1` ordering checks)
#   head/tail -N reading straight from a match-selecting grep: that keeps the first
#     N MATCHES, not N lines of the file (a context grep upstream is already flagged)
#   tail -n +N: "everything after line N" has no fixed width
#   sed ranges whose bounds are not both literal numbers ("1,${stop_line}p",
#     '/start/,/end/p'): they are located by an anchor, which is the fix shape
#   a window whose OWN pipeline stage goes to stderr (`grep -A3 x f >&2`): a
#     diagnostic, not an assertion subject. Commands are split on ; && || and
#     stages on |, quote-aware, so `grep -A3 x f | grep -q y || echo no >&2` is
#     still flagged — that `>&2` belongs to a different command.
# Every other use counts as an assertion subject: a test file's executable lines
# exist to assert, and a window captured into a variable or a pipe is asserted on.
#
# Entries are `<file>: <line, stripped>` — keyed on content, not line number, so an
# edit elsewhere in the file never makes the ratchet stale. Editing an offending line
# does; fix it by construct-scoping it rather than by re-listing it.
#
# Run: bats platform/tests/test_no_line_window_assertions.bats

load assert_helpers

setup() {
  TESTS_DIR="$BATS_TEST_DIRNAME"
  RATCHET="$TESTS_DIR/known_line_window_assertions.txt"
  SELF="$(basename "$BATS_TEST_FILENAME")"
}

# _line_windows <mode> <dir> [ratchet]
#   mode=list  : print every offender in <dir> as `<file>: <line>`, sorted
#   mode=check : compare against <ratchet>; print NEW/STALE lines, exit 1 on any
# This file is excluded from the scan by name: its fixtures ARE windows.
_line_windows() {
  python3 - "$1" "$2" "${3:-}" "$SELF" <<'PY'
import collections, glob, os, re, sys

mode, root, ratchet, self_name = sys.argv[1:5]

# grep -A/-B/-C N in any spelling: -A3, -A 3, -nA3, --after-context=3, --context 3.
CTX_GREP = re.compile(
    r'\bgrep\b.*?(?:\s-[a-zA-Z]*[ABC]\s*\d|\s--(?:after-|before-)?context(?:=|\s+)\d)')
# sed in quiet mode (-n, -En, -nE, -n -E, --quiet, --silent), then the address,
# allowing other option tokens (-e, -E) before it and spaces inside it.
_SED_Q = (r'\bsed\b.*?\s(?:-[a-zA-Z]*n[a-zA-Z]*|--quiet|--silent)'
          r'(?:\s+-[a-zA-Z-]+)*\s*[\'"]?')
SED_RANGE = re.compile(_SED_Q + r'(?:\d+\s*,\s*\d+\s*p|[^\'"]*?,\s*[+~]\s*\d+)')
SED_SINGLE = re.compile(_SED_Q + r'(\d+)\s*p')
# head/tail with a line count: -n N, -nN, -N, --lines=N, --lines N (other flags first ok).
# `+N` (tail -n +2) never matches: no digit follows the flag directly.
COUNT = re.compile(r'(?:^|[\s($])(?:head|tail)(?:\s+-[a-mo-zA-Z]+)*\s+'
                   r'(?:-n\s*|--lines(?:=|\s+)|-)(\d+)\b')
MATCH_GREP = re.compile(r'(?:^|[\s($])grep\s')


def split_commands(s):
    """Quote-aware split into commands (on ;, &&, ||) of pipe stages (on |).
    A quoted string is opaque, so `run bash -c "a | b"` is one stage."""
    cmds, stages, cur, q, i = [], [], [], None, 0
    while i < len(s):
        c = s[i]
        if q:
            cur.append(c)
            if c == '\\' and q == '"' and i + 1 < len(s):
                cur.append(s[i + 1]); i += 1
            elif c == q:
                q = None
        elif c == '\\' and i + 1 < len(s):
            cur.append(c); cur.append(s[i + 1]); i += 1
        elif c in '\'"':
            q = c; cur.append(c)
        elif s.startswith('||', i) or s.startswith('&&', i) or c == ';':
            stages.append(''.join(cur)); cmds.append(stages); stages, cur = [], []
            i += 1 if c == ';' else 2
            continue
        elif c == '|':
            stages.append(''.join(cur)); cur = []
        else:
            cur.append(c)
        i += 1
    stages.append(''.join(cur)); cmds.append(stages)
    return cmds


def stage_is_window(stages, i):
    st = stages[i]
    if CTX_GREP.search(st) or SED_RANGE.search(st):
        return True
    m = SED_SINGLE.search(st)
    if m and int(m.group(1)) > 1:
        return True
    m = COUNT.search(st)
    if m and int(m.group(1)) > 1:
        reads_pipe = i > 0 and re.match(r'\s*(?:head|tail)\s', st)
        prev = stages[i - 1] if i > 0 else ''
        if reads_pipe and MATCH_GREP.search(prev) and not CTX_GREP.search(prev):
            return False  # first/last N matches, not N lines of a file
        return True
    return False


def is_window(line):
    for stages in split_commands(line):
        for i, st in enumerate(stages):
            # Exempt only when the WINDOW'S OWN stage writes to stderr: a
            # `>&2` elsewhere on the line (`... || echo failed >&2`) is not it.
            if stage_is_window(stages, i) and '>&2' not in st:
                return True
    return False


def logical_lines(path):
    """Yield (first physical line, joined text). A backslash-continued command
    is scanned as one, and reported by its first line so ratchet keys are stable."""
    with open(path, encoding='utf-8') as fh:
        lines = fh.read().split('\n')
    i = 0
    while i < len(lines):
        first = lines[i].strip()
        text = first
        if not first.startswith('#'):
            while text.endswith('\\') and (len(text) - len(text.rstrip('\\'))) % 2 == 1 \
                    and i + 1 < len(lines):
                i += 1
                text = text[:-1] + ' ' + lines[i].strip()
        i += 1
        yield first, text


found = []
files = sorted(glob.glob(os.path.join(root, '*.bats')) + glob.glob(os.path.join(root, '*.bash')))
files = [f for f in files if os.path.basename(f) != self_name]
for f in files:
    for first, text in logical_lines(f):
        if not first or first.startswith('#'):
            continue
        if is_window(text):
            found.append(f'{os.path.basename(f)}: {first}')
found.sort()

if mode == 'list':
    print('SCANNED %d' % len(files))
    for e in found:
        print(e)
    sys.exit(0)

listed = [l.strip() for l in open(ratchet, encoding='utf-8')
          if l.strip() and not l.lstrip().startswith('#')]
have, want = collections.Counter(found), collections.Counter(listed)
bad = 0
for e in sorted((have - want).elements()):
    bad += 1
    print('NEW line-count window (scope it by construct — task_block, or '
          "sed -n '/<start>/,/<boundary>/p'): " + e)
for e in sorted((want - have).elements()):
    bad += 1
    print('STALE ratchet entry, no longer occurs — remove it: ' + e)
print('SCANNED %d FOUND %d LISTED %d' % (len(files), len(found), len(listed)))
sys.exit(1 if bad else 0)
PY
}

@test "no new line-count windows in platform/tests, and the ratchet holds no stale entry" {
  [ -f "$RATCHET" ]
  run _line_windows check "$TESTS_DIR" "$RATCHET"
  [ "$status" -eq 0 ] || { printf '%s\n' "$output" >&2; return 1; }
  # Not vacuous: the scan really covered the suite.
  local scanned
  scanned=$(printf '%s\n' "$output" | sed -n 's/^SCANNED \([0-9]*\) .*/\1/p')
  [ -n "$scanned" ]
  [ "$scanned" -ge 50 ]
}

# The heuristic itself, against fixtures — so a regex change that stops catching a
# window (or starts flagging an exempt form) is seen here, not in review.
_fixture_dir() {
  local d="$BATS_TEST_TMPDIR/fx"
  mkdir -p "$d"
  printf '%s\n' "$@" > "$d/test_fixture.bats"
  printf '%s\n' "$d"
}

@test "the scanner flags every fixed-width window form" {
  local d
  d=$(_fixture_dir \
    "  grep -A3 'name: x' \"\$f\" | grep -q 'y'" \
    "  grep -A 14 'x' \"\$f\" | grep -q y" \
    "  block=\$(grep -nA3 -F 'x' \"\$f\")" \
    "  grep -B2 x f | grep -q y" \
    "  run grep -C 1 x f" \
    "  grep --after-context=4 x f | grep -q y" \
    "  sed -n '3,9p' \"\$f\" | grep -q y" \
    "  sed -n '/x/,+4p' \"\$f\" | grep -q y" \
    "  sed -n '5p' \"\$f\"" \
    "  head -n 5 \"\$f\" | grep -q y" \
    "  head -20 \"\$f\" | grep -q y" \
    "  sed -n '/a/,/b/p' f | head -5 | grep -q y" \
    "  tail -3 log | grep -q y" \
    "  sed -En '3,9p' \"\$f\" | grep -q y" \
    "  sed -n '3,9 p' \"\$f\" | grep -q y" \
    "  sed -n -E '/x/,+4p' \"\$f\" | grep -q y" \
    "  head --lines 5 \"\$f\" | grep -q y" \
    "  head --lines=5 \"\$f\" | grep -q y" \
    "  tail --lines 4 log | grep -q y" \
    "  grep -A3 x f | grep -q y || echo failed >&2" \
    "  grep -F 'x' \"\$f\" \\" \
    "    -A3 | grep -q y")
  run _line_windows list "$d"
  [ "$status" -eq 0 ]
  local n
  n=$(printf '%s\n' "$output" | grep -c '^test_fixture.bats: ')
  [ "$n" -eq 21 ] || { printf '%s\n' "$output" >&2; return 1; }
}

@test "the scanner leaves positions, anchored ranges, comments and diagnostics alone" {
  local d
  d=$(_fixture_dir \
    "  # never use grep -A5 'x' here: the window is sized by line count" \
    "  a=\$(grep -n 'x' \"\$f\" | head -1 | cut -d: -f1)" \
    "  first=\$(grep -n 'x' \"\$f\" | head -3)" \
    "  last=\$(grep -n 'x' \"\$f\" | tail -1 | cut -d: -f1)" \
    "  head -1 \"\$f\" | grep -qE '^#!/usr/bin/env bash'" \
    "  [ \"\$(head -n 1 \"\$c\")\" = \"{\" ]" \
    "  before=\$(sed -n \"1,\${stop_line}p\" \"\$PB\")" \
    "  sed -n '/name: X/,/^\$/p' \"\$f\" > \"\$BATS_TEST_TMPDIR/x.yml\"" \
    "  sed -n '1p' \"\$f\"" \
    "  tail -n +2 \"\$f\" | grep -q y" \
    "  grep -A3 'x' \"\$f\" >&2" \
    "  head -c 2 \"\$f\"" \
    "  grep -qE 'a|b' \"\$f\"" \
    "  grep -n 'x' \"\$f\" \\" \
    "    | head -1 | cut -d: -f1" \
    "  run bash -c \"grep -n x '\$f' | head -1\"")
  run _line_windows list "$d"
  [ "$status" -eq 0 ]
  refute_contains "$output" 'test_fixture.bats: '
}

@test "the ratchet refuses a new window and refuses a stale entry" {
  local d
  d=$(_fixture_dir \
    "  grep -A3 'listed' f | grep -q y" \
    "  grep -A3 'unlisted' f | grep -q y")
  printf '%s\n' '# comment' '' \
    "test_fixture.bats: grep -A3 'listed' f | grep -q y" \
    "test_fixture.bats: grep -A3 'gone' f | grep -q y" > "$BATS_TEST_TMPDIR/ratchet.txt"
  run _line_windows check "$d" "$BATS_TEST_TMPDIR/ratchet.txt"
  [ "$status" -eq 1 ]
  assert_contains "$output" "NEW line-count window"
  assert_contains "$output" "grep -A3 'unlisted' f"
  assert_contains "$output" "STALE ratchet entry, no longer occurs — remove it: test_fixture.bats: grep -A3 'gone' f"
  # Exactly one of each: the listed, still-present window is accepted.
  [ "$(printf '%s\n' "$output" | grep -c '^NEW ')" -eq 1 ]
  [ "$(printf '%s\n' "$output" | grep -c '^STALE ')" -eq 1 ]
}
