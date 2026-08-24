#!/usr/bin/env bats
# Keeps docs/MISTAKES.md honest about the two things in it that are executable:
# the proposed OPA rules, and the claim that each entry names where it is
# enforced.
#
# These do NOT need the opa binary — the Unit Tests CI job installs pytest and
# bats only. The behavioural validation of the Rego (opa check, opa test 18/18,
# and the fail-closed cases) is recorded in the doc itself and was run against
# openpolicyagent/opa with the real policy tree; what is checked here is that the
# snippet cannot silently regress to the fail-OPEN form it started as.
#
# Run: bats platform/tests/test_mistakes_doc.bats

load assert_helpers

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  DOC="$REPO_ROOT/docs/MISTAKES.md"
}

@test "mistakes doc: exists and is indexed" {
  [ -f "$DOC" ]
  # Every numbered entry in the index must have a matching section body, or a
  # reader following the index lands nowhere.
  run python3 -c "
import re
doc = open('$DOC').read()
import collections
# Only the Index table — section 7 has its own table using the same row shape,
# and counting those rows as index entries produced phantom duplicates.
idx_tbl = doc.split('## Index')[1].split(chr(10) + '---')[0]
idx_list = re.findall(r'^\| (\d+\.\d+) \|', idx_tbl, re.M)
body_list = re.findall(r'^### (\d+\.\d+) ', doc, re.M)
sect_list = re.findall(r'^## (\d+)\.', doc, re.M)
# Sets hide collisions, and a reused identifier makes every reference ambiguous.
dup_idx = sorted(k for k, v in collections.Counter(idx_list).items() if v > 1)
dup_body = sorted(k for k, v in collections.Counter(body_list).items() if v > 1)
dup_sect = sorted(k for k, v in collections.Counter(sect_list).items() if v > 1)
missing = sorted(set(idx_list) - set(body_list))
extra = sorted(set(body_list) - set(idx_list))
problems = []
if dup_idx: problems.append(f'duplicate index ids {dup_idx}')
if dup_body: problems.append(f'duplicate body ids {dup_body}')
if dup_sect: problems.append(f'duplicate section numbers {dup_sect}')
if missing: problems.append(f'indexed but no body {missing}')
if extra: problems.append(f'body but not indexed {extra}')
print('OK' if not problems else '; '.join(problems))
"
  [ "$output" = "OK" ]
}

@test "mistakes doc: every index row names where the rule is enforced" {
  # A row with a blank enforcement column is the failure this doc is about —
  # a rule recorded but not placed anywhere it can fire.
  run python3 -c "
import re
doc = open('$DOC').read()
rows = re.findall(r'^\| (\d+\.\d+) \| [^|]+ \| [^|]+ \| ([^|]*) \|', doc, re.M)
assert rows, 'no index rows parsed'
blank = [n for n, enf in rows if not enf.strip()]
print('OK' if not blank else f'rows with no enforcement: {blank}')
"
  [ "$output" = "OK" ]
}

@test "mistakes doc: the proposed OPA rules fail CLOSED on a missing field" {
  # The first draft used a bare input.target, which ALLOWED an action that
  # omitted the field — the opposite of a guardrail. The corrected form defaults
  # the field so the deny rule still evaluates. Verified by evaluation against
  # openpolicyagent/opa; pinned here so an edit cannot quietly undo it.
  run python3 -c "
import re
doc = open('$DOC').read()
blocks = re.findall(r'\`\`\`rego\n(.*?)\`\`\`', doc, re.S)
assert blocks, 'no rego blocks found in the doc'
problems = []

def rule_for(marker):
    # The deny rule containing a marker, plus the helper it calls. Searching the
    # concatenation of every block lets a match in an unrelated block or comment
    # satisfy the check while the rule itself has regressed.
    for b in blocks:
        for stanza in re.split(r'\n(?=deny if \{|_[a-z_]+ *(?:\(|:=))', b):
            if marker in stanza and stanza.lstrip().startswith('deny if'):
                return stanza, b
    return None, None

mut_rule, mut_block = rule_for('update_key')
if mut_rule is None:
    problems.append('no deny rule found for shared-object mutation')
else:
    if 'not _declared_as_code(object.get(input, \"target\", \"\"))' not in mut_rule:
        problems.append('shared-mutation rule does not default a missing target')
    if re.search(r'not _declared_as_code\(input\.target\)', mut_rule):
        problems.append('bare input.target reintroduced (fails OPEN)')

pay_rule, pay_block = rule_for('write_secret')
if pay_rule is None:
    problems.append('no deny rule found for secret writes')
elif 'object.get(input, \"payload_markers\", [])' not in pay_block:
    problems.append('payload rule does not default absent markers')
print('OK' if not problems else '; '.join(problems))
"
  [ "$output" = "OK" ]
}

@test "mistakes doc: the four tests named below exist" {
  # Entry 8.2 is an invented cross-reference. This lists the names explicitly
  # rather than discovering them: an auto-discovering version needed enough regex
  # to mistake code fragments for prose, and a test that fails for its own reasons
  # teaches people to edit the test (entry 2.2).
  #
  # DELIBERATELY NOT exhaustive over every path the document mentions. The file is
  # appended to by more than one line of work at a time, so an exhaustive check
  # fails on another author's in-flight entry rather than on anything here. The
  # four below are the ones this branch is responsible for.
  local suite
  suite=$(cat "$REPO_ROOT"/platform/tests/*.bats | tr '\n' ' ')
  local n
  for n in \
    "every play that resolves an OpenBao URL includes the transport guard" \
    "the transport pattern accepts internal endpoints and refuses public ones" \
    "postiz: the seed playbook uses the shared cleartext OpenBao guard" \
    "install-podman: configures no podman API socket"; do
    # named in the doc ...
    assert_contains "$(tr '\n' ' ' < "$DOC")" "$n"
    # ... and present in the suite
    assert_contains "$suite" "@test \"$n\""
  done
}
