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

@test "tududi-sync: the mapping declares exactly the nine reviewed pairs" {
  local f="$SYNC_DIR/github-mapping.yml"
  [ -f "$f" ]
  # Exactly nine entries, each carrying all three fields.
  [ "$(grep -c '  - tududi_project:' "$f")" -eq 9 ]
  [ "$(grep -c '    github_repo: uhstray-io/' "$f")" -eq 9 ]
  [ "$(grep -c '    enabled: ' "$f")" -eq 9 ]
  # The enabled flag is a real boolean, never a string an implementer must parse.
  refute_grep -qE 'enabled: "(true|false)"' "$f"
  # Every repo lives in the org; a bare or foreign repo name is a typo.
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
  grep -qF "ALL CORE SCENARIOS PASS" <<<"$output"
}

@test "tududi-sync: the workflow template RENDERS — full nine-pair and enabled-subset, schema-checked" {
  # Task 2.4's gate, behavioural: the exact render the provisioning playbook
  # performs (Jinja2 + the committed mapping + the embedded JS), asserted as
  # valid JSON with the reviewed node graph, credentials by name/id only, the
  # embedded pairs equal to the declaration, and no DELETE method anywhere.
  python3 -c 'import jinja2, yaml' 2>/dev/null || skip "jinja2/pyyaml not available"
  run python3 "$SYNC_DIR/tests/render-check.py"
  [ "$status" -eq 0 ]
  grep -qF "RENDER CHECK PASS" <<<"$output"
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
  # deletes or revokes. Existing access survives every proof failure.
  # The playbook flips no auth flag and
  # restarts nothing.
  refute_grep -qiE 'destroy|\.drop|DELETE FROM' "$js"
  refute_grep -qE 'revoked_at =|revoke-label|row.save' "$js"
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
           "Prove the STORED token" "Prove the token end-to-end"; do
    blk=$(task_block "$pb" "$n")
    [ -n "$blk" ]
    assert_grep -q 'no_log: true' <<<"$blk"
  done
  for n in "Read the current state" "Report (names and counts only"; do
    blk=$(task_block "$pb" "$n")
    [ -n "$blk" ]
    refute_grep -q 'no_log: true' <<<"$blk"
  done
  # The mint is PROVEN live (Bearer call must answer 200) before the report —
  # from INSIDE the container on loopback, the one address identical on
  # local-dev and prod. The public edge URL (tududi_base_url) resolves to
  # 127.0.0.1 inside the local Semaphore container and every earlier "green"
  # run had carried a launch-time override to dodge it.
  blk=$(task_block "$pb" "Prove the token end-to-end")
  assert_grep -qE '^\s+- prove$' <<<"$blk"
  assert_grep -qF 'stdin: "{{ _raw.stdout | trim }}"' <<<"$blk"
  refute_grep -qE 'tududi_base_url|_tududi_base' <(grep -vE '^\s*#' "$pb")
  assert_grep -qF "path: '/api/profile/api-keys'" "$js"
  assert_grep -qF 'if (status !== 200) throw' "$js"
  # Convergence is PROOF of the stored value (live AND owned by the configured
  # user), never presence: a live token minted for a different user reported
  # "converged" and the identity change silently never re-minted.
  assert_grep -qE '^\s+_converged: "\{\{ _bao_has_token and \(_stored_proof\.rc' "$pb"
  assert_grep -qF 'await bcrypt.compare(raw, row.token_hash)' "$js"
  refute_grep -qE 'when: \(_active_rows \| int > 0\) and _bao_has_token' "$pb"
  # Store write rides the shared sibling-preserving merge, refuse-missing.
  blk=$(task_block "$pb" "Merge the token into the store")
  assert_grep -q '_bm_on_missing: fail' <<<"$blk"
  # Inventory names the sync identity; task 6.0 gates operator visibility
  # separately. Validation must not replace it with the operator's identity.
  assert_grep -qF '_login_email: "{{ tududi_sync_user_email }}"' "$pb"
  assert_grep -qF 'tududi_sync_user_email' "$REPO_ROOT/platform/inventory/local-dev.yml.example"
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

@test "tududi-sync: provisioning — kill switch first, named refusals, preservation" {
  local pb="$REPO_ROOT/platform/playbooks/provision-tududi-github-sync.yml"
  [ -f "$pb" ]
  assert_grep -qF 'import_playbook: preflight-target-group.yml' "$pb"
  # Ownership names are fixed, never caller-supplied.
  assert_grep -qE '^    _prefix: "tududi-github-sync:"$' "$pb"
  assert_grep -qE '^    _workflow_name: "tududi-github-sync: cycle"$' "$pb"
  refute_grep -qE '^    (_prefix|_workflow_name|_tududi_cred_name|_github_cred_name): "\{\{' "$pb"
  # THE kill-switch ordering (task 4.1): the sync_enabled branch sits BEFORE
  # the mapping read and both credential validations, and uses only the n8n
  # API key. Line order is the invariant.
  local k m t g
  k=$(grep -n 'Deactivate the owned workflows and stop' "$pb" | head -1 | cut -d: -f1)
  m=$(grep -n 'Read the committed mapping' "$pb" | head -1 | cut -d: -f1)
  t=$(grep -n 'Validate the tududi token against the live API' "$pb" | head -1 | cut -d: -f1)
  g=$(grep -n 'Mint + scope-validate the GitHub App token' "$pb" | head -1 | cut -d: -f1)
  [ -n "$k" ] && [ -n "$m" ] && [ -n "$t" ] && [ -n "$g" ]
  [ "$k" -lt "$m" ] && [ "$k" -lt "$t" ] && [ "$k" -lt "$g" ]
  # Both credential validations precede every engine write (task 3.3): the
  # first upsert comes after both probes.
  local u
  u=$(grep -n 'Upsert the tududi credential' "$pb" | head -1 | cut -d: -f1)
  [ "$t" -lt "$u" ] && [ "$g" -lt "$u" ]
  # Refusals are named: malformed mapping, absent token, dead token.
  assert_grep -qF 'Refuse an unparseable or shapeless mapping (named)' "$pb"
  assert_grep -qF 'Refuse an unsuccessful tududi probe (named)' "$pb"
  # ...and an enabled pair whose project the token's user cannot see — the
  # project list IS the token probe, and the refusal precedes every write.
  assert_grep -qF 'Refuse an enabled pair whose project the sync identity cannot see (named)' "$pb"
  blk=$(task_block "$pb" "Validate the tududi token against the live API")
  assert_grep -qF '/api/v1/projects' <<<"$blk"
  local v
  v=$(grep -n 'Refuse an enabled pair whose project the sync identity cannot see' "$pb" | head -1 | cut -d: -f1)
  [ -n "$v" ] && [ "$v" -lt "$u" ]
  # Deactivation only addresses prefix-owned names; deletion is unsupported.
  refute_grep -qiE 'method[[:space:]]*:.*DELETE' "$pb"
  assert_grep -qF "selectattr('name', 'search', '^' ~ _prefix)" "$pb"
  # Secret-bearing steps are no_log; the reports are not.
  local blk n
  for n in "Authenticate to OpenBao" "Read the n8n API key" \
           "Upsert the tududi credential" "Upsert the GitHub credential" \
           "Render the cycle workflow"; do
    blk=$(task_block "$pb" "$n")
    [ -n "$blk" ]
    assert_grep -q 'no_log: true' <<<"$blk"
  done
  for n in "Report (names and counts only" "Report the kill switch"; do
    blk=$(task_block "$pb" "$n")
    [ -n "$blk" ]
    refute_grep -q 'no_log: true' <<<"$blk"
  done
}

@test "tududi-sync: the verification gate BEHAVES — pass on converged, fail on drift" {
  # verify-pair.js is the promotion gate (task 5.1); drive the real file with
  # fixtures: a converged linked pair passes; a diverged one, a failed cycle,
  # and an undeclared-project trace each fail with a named reason.
  command -v node >/dev/null 2>&1 || skip "node not available"
  local lib="$SYNC_DIR/lib"
  base='{"pair":{"tududi_project":"huhhb","github_repo":"uhstray-io/huhhb"},"syncTag":"gh-sync","syncLogin":"bot","writeCap":10,"tasks":[],"issues":[],"undeclaredMarkers":[]}'
  # PASS: successful cycle, empty converged snapshot.
  run bash -c "python3 - <<PYEOF | node '$lib/verify-pair.js'
import json
d = json.loads('''$base''')
d['lastExecution'] = {'status': 'success'}
print(json.dumps(d))
PYEOF"
  [ "$status" -eq 0 ]
  grep -qF '"pass":true' <<<"$output"
  # FAIL: last cycle errored.
  run bash -c "python3 - <<PYEOF | node '$lib/verify-pair.js'
import json
d = json.loads('''$base''')
d['lastExecution'] = {'status': 'error'}
print(json.dumps(d))
PYEOF"
  [ "$status" -eq 1 ]
  grep -qF 'not success' <<<"$output"
  # FAIL: divergence — a tagged task with no issue means a pending create op.
  run bash -c "python3 - <<PYEOF | node '$lib/verify-pair.js'
import json
d = json.loads('''$base''')
d['lastExecution'] = {'status': 'success'}
d['tasks'] = [{'uid': 'u1', 'title': 'T', 'note': '', 'status': 0, 'tags': ['gh-sync'], 'updated_at': '1', 'tagged': True}]
print(json.dumps(d))
PYEOF"
  [ "$status" -eq 1 ]
  grep -qF 'not converged' <<<"$output"
  # FAIL: sync trace outside the declaration.
  run bash -c "python3 - <<PYEOF | node '$lib/verify-pair.js'
import json
d = json.loads('''$base''')
d['lastExecution'] = {'status': 'success'}
d['undeclaredMarkers'] = ['u9']
print(json.dumps(d))
PYEOF"
  [ "$status" -eq 1 ]
  grep -qF 'UNDECLARED' <<<"$output"
}

@test "tududi-sync: hierarchy (D8) — subtasks flattened, add_sub_issue routed, no subtasks[] on PATCH, no removal" {
  local tpl="$SYNC_DIR/templates/tududi-github-sync.workflow.json.j2"
  local core="$SYNC_DIR/lib/sync-core.js"
  # Read side: children arrive from the parent's embedded list with the
  # parent's uid and their OWN tag state; sub-issue parents come from one
  # GraphQL query per pair — the REST list's parent_issue_url is null under
  # the App installation token (measured on dev-test), so nothing may read it.
  grep -qF 't.subtasks' "$tpl"
  grep -qF 'shape(s, t.uid)' "$tpl"
  grep -qF 'https://api.github.com/graphql' "$tpl"
  grep -qF 'parent{ number }' "$tpl"
  # The audit-key suppression (D6) is inert unless the engine sees comments.
  # They ride the SAME per-pair query as the parents — no per-issue call —
  # and the gate reads them too, or it renders a different verdict.
  grep -qF 'comments(last:20)' "$tpl"
  grep -qF 'comments(last:20)' "$REPO_ROOT/platform/playbooks/tasks/verify-tududi-sync-pair.yml"
  refute_grep -qF 'comments: []' "$tpl"
  refute_grep -qF 'parent_issue_url ?' "$tpl"
  # Write side: the one hierarchy op is an attach, POSTed to the parent's
  # sub_issues collection with the child's issue id.
  grep -qF "'add_sub_issue' ? '/' + \$json.op.parent_number + '/sub_issues'" "$tpl"
  grep -qF 'sub_issue_id: $json.op.sub_issue_id' "$tpl"
  # tududi replaces the WHOLE child set when a PATCH carries subtasks — the
  # engine's patch never names that key.
  refute_grep -qE "subtasks *:" "$core"
  # No detach/removal at any level, in the engine or the rendered routes.
  refute_grep -qE "type: '(remove_sub_issue|delete_[a-z_]+)'" "$core"
  refute_grep -qF 'remove_sub_issue' "$tpl"
  refute_grep -qF "'DELETE'" "$tpl"
  # The 5.1 gate runs the SAME engine, so its snapshot must carry the same
  # hierarchy — a gate blind to subtasks reports every child as a dangling
  # marker and fails a converged pair (observed before this was wired).
  local vp="$REPO_ROOT/platform/playbooks/tasks/verify-tududi-sync-pair.yml"
  grep -qF 't.subtasks' "$vp"
  grep -qF 'parent_uid' "$vp"
  grep -qF 'https://api.github.com/graphql' "$vp"
  grep -qF 'parent_number' "$vp"
}

@test "tududi-sync: no credential, token, or address in any committed sync artifact" {
  # The mapping (and later the workflow templates) hold names only — secrets
  # live in OpenBao, endpoints in inventory (design D3/D7, contract doc).
  local f
  while IFS= read -r f; do
    refute_grep -qiE 'tt_[0-9a-f]{8}|ghp_|github_pat_|(^|[^A-Za-z0-9_])(api[_-]?key|token):|password' "$f"
    refute_grep -qE '(10\.[0-9]+|192\.168\.|172\.(1[6-9]|2[0-9]|3[01]))\.' "$f"
  done < <(find "$SYNC_DIR" -type f \( -name '*.yml' -o -name '*.j2' -o -name '*.json' -o -name '*.js' \))
}

@test "tududi-sync: priority — declared field id, Urgent folds to high, writes never wipe sibling fields" {
  local tpl="$SYNC_DIR/templates/tududi-github-sync.workflow.json.j2"
  local core="$SYNC_DIR/lib/sync-core.js"
  local map="$SYNC_DIR/github-mapping.yml"
  local vp="$REPO_ROOT/platform/playbooks/tasks/verify-tududi-sync-pair.yml"
  # The org's Priority field id is DECLARED, because the App installation
  # token is denied /orgs/{org}/issue-fields and cannot discover it.
  grep -qE '^github_priority_field_id: [0-9]+$' "$map"
  # Urgent has no tududi equivalent: it folds onto high at the projection,
  # which is what stops a tududi 'high' from demoting an Urgent issue.
  grep -qF "urgent: 'high'" "$core"
  # A priority write echoes every OTHER field value back — GitHub's
  # issue_field_values PATCH replaces the set rather than merging it.
  grep -qF 'v.field_id !== priorityFieldId' "$core"
  # Both the cycle and the 5.1 gate read the same two things, or they render
  # different verdicts from the same engine.
  grep -qF 'issue_field_values' "$tpl"
  grep -qF 'priority: t.priority' "$tpl"
  grep -qF 'priorityFieldId' "$vp"
  grep -qF "'priority': s.priority" "$vp"
  # Every field-value TYPE survives a write. A date/number/text field carries
  # its value in `value` with single_select_option null, so echoing back only
  # the option name would send null and wipe it — the PATCH replaces the set.
  grep -qF 'data_type' "$tpl"
  grep -qF 'data_type' "$vp"
  # The gate must run the engine over the SAME snapshot-completeness inputs as
  # the cycle, or it PASSES a pair from a truncated read the cycle refuses.
  grep -qF 'issuesTruncated' "$SYNC_DIR/lib/verify-pair.js"
  grep -qF 'issuesTruncated' "$vp"
  # GitHub-origin work lands PLANNED, not NOT_STARTED.
  grep -qF 'status: TUDUDI_STATUS.PLANNED' "$core"
  # A subtask inherits its parent's tag (tududi's UI cannot tag one).
  grep -qF 'const effTagged' "$core"
}

@test "tududi-sync: the kill switch works under a broken mapping and dead credentials (task 4.2)" {
  local pb="$REPO_ROOT/platform/playbooks/provision-tududi-github-sync.yml"
  # The kill-switch path must be able to run when EVERY other input is
  # unusable — a broken mapping or a dead provider credential is exactly when
  # an operator reaches for it. Structurally that means it terminates the host
  # BEFORE the mapping is read and before either provider is contacted.
  local stop map tval gval
  stop=$(grep -n 'ansible.builtin.meta: end_host' "$pb" | head -1 | cut -d: -f1)
  map=$(grep -n 'Read the committed mapping' "$pb" | head -1 | cut -d: -f1)
  tval=$(grep -n 'Validate the tududi token against the live API' "$pb" | head -1 | cut -d: -f1)
  gval=$(grep -n 'Mint + scope-validate the GitHub App token' "$pb" | head -1 | cut -d: -f1)
  [ -n "$stop" ] && [ -n "$map" ] && [ -n "$tval" ] && [ -n "$gval" ]
  [ "$stop" -lt "$map" ]
  [ "$stop" -lt "$tval" ]
  [ "$stop" -lt "$gval" ]
  # The only credential on that path is the n8n API key: the deactivate loop
  # authenticates with it and nothing else.
  local blk
  blk=$(task_block "$pb" "Deactivate every owned workflow")
  [ -n "$blk" ]
  assert_grep -qF 'include_tasks: tasks/deactivate-tududi-sync-workflows.yml' <<<"$blk"
  local deactivate="$REPO_ROOT/platform/playbooks/tasks/deactivate-tududi-sync-workflows.yml"
  assert_grep -qF 'X-N8N-API-KEY' "$deactivate"
  refute_grep -qiE 'Authorization:|api\.github\.com|method[[:space:]]*:.*DELETE' "$deactivate"
  # ...and it only ever addresses prefix-owned workflows, kill switch included.
  assert_grep -qF "selectattr('name', 'search', '^' ~ _prefix)" <<<"$blk"
  # A malformed mapping is refused by ASSERTION on the parsed shape, not by a
  # bare lookup that would traceback; the refusal names what it wanted.
  blk=$(task_block "$pb" "Refuse an unparseable or shapeless mapping (named)")
  [ -n "$blk" ]
  assert_grep -qF '_mapping is mapping' <<<"$blk"
  assert_grep -qF '_mapping.sync_pairs is defined' <<<"$blk"
  assert_grep -qF 'fail_msg' <<<"$blk"
  # "No pairs" has exactly one legitimate spelling: an all-DISABLED mapping,
  # which renders an empty enabled set and preserves all objects (the
  # specified rollback). A MISSING or empty sync_pairs list is a malformed
  # file, not a rollback, and is refused — otherwise a truncated mapping
  # would silently read as "tear the sync down".
  assert_grep -qF '_mapping.sync_pairs | length > 0' <<<"$blk"
  assert_grep -qF 'all-DISABLED mapping' <<<"$blk"
  # The disable branch works off the ENABLED subset, so all-disabled is empty.
  blk=$(task_block "$pb" "Take the enabled pairs and the declared Priority field")
  assert_grep -qF "selectattr('enabled')" <<<"$blk"
  blk=$(task_block "$pb" "Deactivate and preserve the sync when all pairs are disabled")
  assert_grep -qF 'when: _pairs | length == 0' <<<"$blk"
}

@test "tududi-sync: provisioning preserves objects and token guards refuse unsafe writes" {
  command -v ansible-playbook >/dev/null 2>&1 || skip "ansible-playbook not available"
  python3 -c 'import yaml' 2>/dev/null || skip "pyyaml not available"
  run python3 "$SYNC_DIR/tests/provisioning-safety.py"
  [ "$status" -eq 0 ]
  grep -qF "PROVISIONING SAFETY PASS" <<<"$output"
}

@test "tududi-sync: container token helper cannot revoke existing access" {
  command -v node >/dev/null 2>&1 || skip "node not available"
  run node "$SYNC_DIR/tests/token-safety.js"
  [ "$status" -eq 0 ]
  grep -qF "TOKEN SAFETY PASS" <<<"$output"
}
