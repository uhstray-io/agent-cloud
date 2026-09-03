#!/usr/bin/env bats
# Structural tests for the tududi↔GitHub issue sync (design D1-D7,
# integrate-tududi-github-issue-sync). The mapping is the reviewed source of
# truth the workflows are rendered from; these tests pin its shape and keep
# credentials/addresses out of every committed sync artifact.
#
# Run: bats platform/tests/test_tududi_sync.bats

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  SYNC_DIR="$REPO_ROOT/platform/services/tududi/sync"
  load assert_helpers
}

@test "tududi-sync: the mapping declares exactly the six reviewed pairs" {
  local f="$SYNC_DIR/github-mapping.yml"
  [ -f "$f" ]
  # Exactly six entries, each carrying all three fields.
  [ "$(grep -c '  - tududi_project:' "$f")" -eq 6 ]
  [ "$(grep -c '    github_repo: uhstray-io/' "$f")" -eq 6 ]
  [ "$(grep -c '    enabled: ' "$f")" -eq 6 ]
  # The enabled flag is a real boolean, never a string an implementer must parse.
  refute_grep -qE 'enabled: "(true|false)"' "$f"
  # Every repo lives in the org; a bare or foreign repo name is a typo.
  refute_grep -qE 'github_repo: (?!uhstray-io/)' "$f" 2>/dev/null || \
    [ "$(grep -c '    github_repo: ' "$f")" -eq "$(grep -c '    github_repo: uhstray-io/' "$f")" ]
}

@test "tududi-sync: the sync core BEHAVES — creation gates, LWW, audit keys, write cap" {
  # Behavioural (the test_apply_firewall.bats precedent): runs the exact JS
  # the workflows embed, through eight committed scenarios — tagged creation
  # (sync tag never propagates), suspect-duplicate blocks creation with a
  # recovery error, quiet cycle is a no-op, different-fields merge cleanly,
  # same-field conflict resolves by the systems' own timestamps with the
  # losing value in a KEYED comment (retry suppressed), un-tag closes
  # not-planned, exceeding the write cap refuses the whole cycle, and no
  # delete op exists in the module at all.
  command -v node >/dev/null 2>&1 || skip "node not available"
  run node "$SYNC_DIR/tests/core-scenarios.js"
  [ "$status" -eq 0 ]
  [[ "$output" == *"ALL CORE SCENARIOS PASS"* ]]
}

@test "tududi-sync: the workflow template RENDERS — full six-pair and enabled-subset, schema-checked" {
  # Task 2.4's gate, behavioural: the exact render the provisioning playbook
  # performs (Jinja2 + the committed mapping + the embedded JS), asserted as
  # valid JSON with the reviewed node graph, credentials by name/id only, the
  # embedded pairs equal to the declaration, and no DELETE method anywhere.
  python3 -c 'import jinja2, yaml' 2>/dev/null || skip "jinja2/pyyaml not available"
  run python3 "$SYNC_DIR/tests/render-check.py"
  [ "$status" -eq 0 ]
  [[ "$output" == *"RENDER CHECK PASS"* ]]
}

@test "tududi-sync: no credential, token, or address in any committed sync artifact" {
  # The mapping (and later the workflow templates) hold names only — secrets
  # live in OpenBao, endpoints in inventory (design D3/D7, contract doc).
  local f
  while IFS= read -r f; do
    refute_grep -qiE 'tt_[0-9a-f]{8}|ghp_|github_pat_|api[_-]?key:|token:|password' "$f"
    refute_grep -qE '(10\.[0-9]+|192\.168\.|172\.(1[6-9]|2[0-9]|3[01]))\.' "$f"
  done < <(find "$SYNC_DIR" -type f \( -name '*.yml' -o -name '*.j2' -o -name '*.json' -o -name '*.js' \))
}
