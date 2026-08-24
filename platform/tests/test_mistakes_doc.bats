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
idx = set(re.findall(r'^\| (\d+\.\d+) \|', doc, re.M))
bodies = set(re.findall(r'^### (\d+\.\d+) ', doc, re.M))
missing = sorted(idx - bodies)
extra = sorted(bodies - idx)
print('OK' if not missing and not extra else f'index/body mismatch missing={missing} unindexed={extra}')
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
body = '\n'.join(blocks)
problems = []
# The shared-mutation rule must default the target rather than reference it bare.
if 'not _declared_as_code(object.get(input, \"target\", \"\"))' not in body:
    problems.append('shared-mutation rule does not default a missing target')
if re.search(r'not _declared_as_code\(input\.target\)', body):
    problems.append('bare input.target reintroduced (fails OPEN)')
# The payload rule must likewise tolerate an absent markers field.
if 'object.get(input, \"payload_markers\", [])' not in body:
    problems.append('payload rule does not default absent markers')
print('OK' if not problems else '; '.join(problems))
"
  [ "$output" = "OK" ]
}

@test "mistakes doc: the tests it names actually exist" {
  # Entry 8.2 is an invented cross-reference. This lists the referenced test
  # names explicitly rather than discovering them: an auto-discovering version
  # needed enough regex to mistake code fragments for prose, and a test that
  # fails for its own reasons teaches people to edit the test (entry 2.2).
  local suite
  suite=$(cat "$REPO_ROOT"/platform/tests/*.bats | tr '\n' ' ')
  local n
  for n in \
    "every play that resolves an OpenBao URL includes the transport guard" \
    "the transport pattern accepts internal endpoints and refuses public ones" \
    "postiz: the seed playbook uses the shared cleartext OpenBao guard"; do
    # named in the doc ...
    [[ "$(tr '\n' ' ' < "$DOC")" == *"$n"* ]]
    # ... and present in the suite
    [[ "$suite" == *"@test \"$n\""* ]]
  done
}
