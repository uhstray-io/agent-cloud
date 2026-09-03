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

@test "tududi-sync: token mint playbook — DB-side, non-destructive, no_log scoped" {
  local pb="$REPO_ROOT/platform/playbooks/store-tududi-api-token.yml"
  local js="$REPO_ROOT/platform/playbooks/files/tududi-db-mint.js"
  [ -f "$pb" ] && [ -f "$js" ]
  assert_grep -qF 'import_playbook: preflight-target-group.yml' "$pb"
  # Fixed path/key/label — never caller-supplied.
  assert_grep -qE '^    _bao_path: "services/tududi"$' "$pb"
  assert_grep -qE '^    _bao_key: "api_token"$' "$pb"
  assert_grep -qE '^    _token_label: "agent-cloud-sync"$' "$pb"
  refute_grep -qE '^    (_bao_(path|key)|_token_label): "\{\{' "$pb"
  # NON-DESTRUCTIVE contract (operator requirement): the mint script never
  # deletes, and its only update is the app's own reversible revoked_at,
  # scoped to our label and user. The playbook flips no auth flag and
  # restarts nothing.
  refute_grep -qiE 'destroy|\.drop|DELETE FROM' "$js"
  assert_grep -qF 'revoked_at = new Date()' "$js"
  assert_grep -qF 'where: { user_id: user.id, name: LABEL, revoked_at: null }' "$js"
  # Scoped to LIVE lines — the header comment legitimately names the flag
  # while explaining why the mint is DB-side.
  refute_grep -qiE 'PASSWORD_AUTH_ENABLED|restart' <(grep -vE '^\s*#' "$pb")
  # The raw token travels via stdin (generate + insert), never argv.
  blk=$(task_block "$pb" "Insert the token row")
  [ -n "$blk" ]
  assert_grep -qF 'stdin: "{{ _raw.stdout | trim }}"' <<<"$blk"
  # Token-bearing tasks are no_log'd; state/report tasks stay diagnosable.
  local blk n
  for n in "Authenticate to OpenBao" "Read the tududi secrets" \
           "Generate the raw token" "Insert the token row" \
           "Prove the token end-to-end"; do
    blk=$(task_block "$pb" "$n")
    [ -n "$blk" ]
    assert_grep -q 'no_log: true' <<<"$blk"
  done
  for n in "Read the current state" "Report (names and counts only"; do
    blk=$(task_block "$pb" "$n")
    [ -n "$blk" ]
    refute_grep -q 'no_log: true' <<<"$blk"
  done
  # The mint is PROVEN live (Bearer call must answer 200) before the report.
  assert_grep -qF '/api/profile/api-keys' "$pb"
  # Store write rides the shared sibling-preserving merge, refuse-missing.
  blk=$(task_block "$pb" "Merge the token into the store")
  assert_grep -q '_bm_on_missing: fail' <<<"$blk"
}

@test "tududi-sync: GitHub App refresher — stdin key, scope preflight, update-only" {
  local pb="$REPO_ROOT/platform/playbooks/refresh-tududi-sync-github-token.yml"
  local mt="$REPO_ROOT/platform/playbooks/tasks/mint-tududi-sync-github-token.yml"
  [ -f "$pb" ] && [ -f "$mt" ]
  # The App private key travels via stdin to the platform's existing signer —
  # never argv (world-readable) and never a temp file.
  assert_grep -qF 'stdin: "{{ _mt_github_secret.json.data.data.tududi_sync_app_key }}"' "$mt"
  assert_grep -qF 'github_app_token.py' "$mt"
  refute_grep -qE 'tududi_sync_app_key.*argv|copy:|tempfile' "$mt"
  # Scope preflight: the installation must equal the declared mapping exactly.
  assert_grep -qF 'symmetric_difference' "$mt"
  assert_grep -qF 'github-mapping.yml' "$mt"
  # Update-only: the refresher PATCHes and refuses to create.
  assert_grep -qF 'method: PATCH' "$pb"
  refute_grep -qE 'method: POST.*credentials|credentials.*method: POST' "$pb"
  assert_grep -qF '_matched | length == 1' "$pb"
  # Key/token-bearing steps are no_log; the report is not.
  local blk n
  for n in "Read the tududi-sync GitHub App credentials" "Mint the installation token" "Take the token"; do
    blk=$(task_block "$mt" "$n")
    [ -n "$blk" ]
    assert_grep -q 'no_log: true' <<<"$blk"
  done
  blk=$(task_block "$pb" "Report (names only")
  [ -n "$blk" ]
  refute_grep -q 'no_log: true' <<<"$blk"
  # The cadence is code: the template declares the 45-minute schedule, and
  # setup-templates upserts schedules for BASE templates only.
  local t="$REPO_ROOT/platform/semaphore/templates.yml"
  blk=$(awk '$0=="  - name: Refresh tududi-sync GitHub Token"{f=1;next} f&&/^  - name:/{exit} f{print}' "$t")
  assert_grep -qF 'cron: "*/45 * * * *"' <<<"$blk"
  assert_grep -qF "rejectattr('_generated', 'defined')" "$REPO_ROOT/platform/semaphore/setup-templates.yml"
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
