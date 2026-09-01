#!/usr/bin/env bats
# Structural tests for the composable n8n service (platform/services/n8n).
# Verifies the legacy secret-generating deploy.sh path was replaced by the
# composable pattern: env-parameterized compose reading a templated .env,
# container-only deploy.sh (no secret gen / owner setup), an env template that
# pulls the stateful N8N_ENCRYPTION_KEY + DB creds from OpenBao, and the Task-0
# pre-seed playbook for the in-place prod migration.
#
# Run: bats platform/tests/test_service_n8n.bats

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  DEPLOY_DIR="$REPO_ROOT/platform/services/n8n/deployment"
  PB_DIR="$REPO_ROOT/platform/playbooks"
  load assert_helpers
}

@test "n8n: compose env-parameterizes the image and reads the templated .env" {
  local f="$DEPLOY_DIR/compose.yml"
  grep -qE '\$\{N8N_IMAGE' "$f"
  grep -qF 'env_file: .env' "$f"
  ! grep -qF 'env_file: ./config/n8n.env' "$f"
}

@test "n8n: deploy.sh is container-only — no secret gen / owner setup / API key" {
  local f="$DEPLOY_DIR/deploy.sh"
  [ -f "$f" ] && [ -x "$f" ]
  grep -q 'common.sh' "$f"
  grep -qE '\bcompose (pull|up)' "$f"
  ! grep -qE 'gen_secret|put_secret|generate_n8n_env|owner/setup|rawApiKey|store_token_in_openbao' "$f"
}

@test "n8n: env template sources the stateful key + DB creds from OpenBao" {
  local f="$DEPLOY_DIR/templates/n8n.env.j2"
  [ -f "$f" ]
  grep -qF 'N8N_ENCRYPTION_KEY={{ secrets.encryption_key }}' "$f"
  grep -qF 'POSTGRES_PASSWORD={{ secrets.db_admin_password }}' "$f"
  grep -qF 'POSTGRES_NON_ROOT_PASSWORD={{ secrets.db_user_password }}' "$f"
  ! grep -qiE 'LOCAL_FAKE|password=[A-Za-z0-9]{8}' "$f"
}

@test "n8n: env template declares env-managed community nodes, pinned name+version+checksum" {
  local f="$DEPLOY_DIR/templates/n8n.env.j2"
  assert_grep -qF 'N8N_COMMUNITY_PACKAGES_ENABLED=true' "$f"
  assert_grep -qF 'N8N_COMMUNITY_PACKAGES_MANAGED_BY_ENV=true' "$f"
  # inventory-overridable, one default set — no local/prod fork
  assert_grep -qF 'n8n_community_packages | default(_default_community_packages)' "$f"
  # the Postiz node pin: name + exact version + npm dist.integrity checksum
  assert_grep -qF '"name": "n8n-nodes-postiz"' "$f"
  assert_grep -qF '"version": "0.2.17"' "$f"
  # The EXACT integrity value for n8n-nodes-postiz@0.2.17 (npm dist.integrity)
  # — a shape-only check cannot detect checksum drift.
  assert_grep -qF '"checksum": "sha512-+dlEfTLuDGUsaO6aldsw3EYwyFNQmhMwEM1hrP0gSuUtPzvwblOd2kPrwKDqE6GKXZa3EnKcdYDKlBps/M1SrQ=="' "$f"
}

@test "n8n: local overlay adds caps/SELinux/local-dev but does NOT republish ports" {
  local f="$DEPLOY_DIR/compose.local.yml"
  [ -f "$f" ]
  grep -q 'mem_limit:' "$f"
  grep -q 'label=disable' "$f"
  grep -q 'local-dev' "$f"
  ! grep -qE '^[[:space:]]*ports:' "$f"
}

@test "n8n: deploy playbook is composable (place-monorepo + manage-secrets), not the legacy wrapper" {
  local f="$PB_DIR/deploy-n8n.yml"
  grep -q 'tasks/place-monorepo.yml' "$f"
  grep -q 'tasks/manage-secrets.yml' "$f"
  ! grep -q 'import_playbook: deploy-service.yml' "$f"
  # the three secret definitions incl. the stateful encryption key
  grep -q 'name: encryption_key' "$f"
}

@test "n8n: Task-0 pre-seed playbook exists for the in-place prod migration" {
  local f="$PB_DIR/seed-n8n-secrets.yml"
  [ -f "$f" ]
  grep -q "secret/data/services/n8n" "$f"
  grep -q 'encryption_key' "$f"
}

@test "n8n: store-n8n-api-key mints+captures with no_log scoped to the key-bearing steps" {
  local pb="$PB_DIR/store-n8n-api-key.yml"
  [ -f "$pb" ]
  # SELECT only: no mutating SQL anywhere in the play (the mint is HTTP, not SQL).
  refute_grep -qiE 'UPDATE|INSERT|DELETE|ALTER' <<<"$(grep -vE '^[[:space:]]*#' "$pb")"
  # The store path, key and label are fixed, never caller-supplied.
  assert_grep -qE '^    _bao_path: "services/n8n"$' "$pb"
  assert_grep -qE '^    _bao_key: "n8n_api_key"$' "$pb"
  assert_grep -qE '^    _key_label: "agent-cloud-automation"$' "$pb"
  refute_grep -qE '^    (_bao_(path|key)|_key_label): "\{\{' "$pb"
  # Every step that touches the key or a credential is no_log'd — DB read/parse,
  # owner login chain, mint, and the fetch/patch/verify chain in the SHARED
  # write task (tasks/bao-merge-keys.yml, covered by the postiz test too).
  assert_grep -q 'include_tasks: tasks/bao-merge-keys.yml' "$pb"
  local blk n
  while IFS= read -r n; do
    blk=$(task_block "$pb" "$n")
    [ -n "$blk" ]
    assert_grep -q 'no_log: true' <<<"$blk"
  done < <(printf '%s\n' \
    "Look for the automation key" \
    "Parse what the database answered" \
    "Read the n8n owner password" \
    "Log in to n8n as the seeded owner" \
    "Fetch the valid API-key scopes" \
    "Create the API key" \
    "Take the raw key" \
    "Authenticate to OpenBao (AppRole)")
  # ONE AppRole login serves the whole play — a second inline copy is the
  # duplication bao-merge-keys.yml's history warns about.
  [ "$(grep -c 'auth/approle/login' "$pb")" -eq 1 ]
  # ...and no_log is SCOPED there: schema assert + report stay diagnosable.
  for n in "Assert the API-key schema exists" "Require the user_api_keys table" "Report (names and counts only"; do
    blk=$(task_block "$pb" "$n")
    [ -n "$blk" ]
    refute_grep -q 'no_log: true' <<<"$blk"
  done
  # This caller REFUSES to create the path — deploy-n8n's manage-secrets owns it.
  blk=$(task_block "$pb" "Merge the key into the store")
  assert_grep -q '_bm_on_missing: fail' <<<"$blk"
  # The one debug prints a LENGTH, never the value: inside the Report block,
  # every line naming _api_key must pipe it through length — and the play holds
  # exactly one debug task, so no other can render the key.
  local rep
  rep=$(task_block "$pb" "Report (names and counts only")
  [ -n "$rep" ]
  [ -z "$(grep -oE '_api_key[^|]*' <<<"$rep" | grep -v '_api_key \| length')" ]
  assert_grep -q '_api_key | length' <<<"$rep"
  [ "$(grep -c 'ansible.builtin.debug' "$pb")" -eq 1 ]
  # Transport guards precede the first request that carries a credential, and
  # BOTH endpoints (OpenBao and n8n) are guarded.
  assert_guard_precedes_first_uri "$pb"
  assert_grep -q '_assert_url_label: "n8n"' "$pb"
  # Schema verified against a NAMED version, recorded in the header.
  assert_grep -q 'n8n@2.25.7' "$pb"
  assert_grep -q 'user_api_keys' "$pb"
}

@test "n8n: provision-n8n-postiz-credential shared-reads the postiz key and never re-stores it" {
  local pb="$PB_DIR/provision-n8n-postiz-credential.yml"
  [ -f "$pb" ]
  # Single custody: the postiz key is READ from postiz's path; nothing in this
  # play writes to OpenBao at all (no merge task, no POST/PATCH to secret/data).
  assert_grep -q 'secret/data/services/postiz' "$pb"
  refute_grep -q 'bao-merge-keys' "$pb"
  # Fixed name/type/host default, never caller-supplied.
  assert_grep -qE '^    _cred_name: "Postiz \(agent-cloud\)"$' "$pb"
  assert_grep -qE '^    _cred_type: "postizApi"$' "$pb"
  refute_grep -qE '^    _cred_(name|type): "\{\{' "$pb"
  # Every step carrying the n8n key (header) or postiz key (body) is no_log'd.
  local blk n
  while IFS= read -r n; do
    blk=$(task_block "$pb" "$n")
    [ -n "$blk" ]
    assert_grep -q 'no_log: true' <<<"$blk"
  done < <(printf '%s\n' \
    "Authenticate to OpenBao (AppRole)" \
    "Read the n8n API key" \
    "Shared-read the Postiz API key" \
    "List n8n's credentials" \
    "Create the credential" \
    "Update the credential in place" \
    "Test the stored credential")
  # ...and scoped: the report and the assertions stay diagnosable.
  for n in "Report (names, outcome and test status" "Require the Postiz API key"; do
    blk=$(task_block "$pb" "$n")
    [ -n "$blk" ]
    refute_grep -q 'no_log: true' <<<"$blk"
  done
  # The report never renders either key and restates the Postiz rate ceiling.
  local rep
  rep=$(task_block "$pb" "Report (names, outcome and test status")
  [ -n "$rep" ]
  refute_grep -q 'api_key' <<<"$rep"
  assert_grep -q '90 posts/hour' <<<"$rep"
  # Transport guards precede the first credential-carrying request; both
  # endpoints are guarded; endpoints/shape verified at named versions.
  assert_guard_precedes_first_uri "$pb"
  assert_grep -q '_assert_url_label: "n8n"' "$pb"
  assert_grep -q 'n8n@2.25.7' "$pb"
  assert_grep -q 'n8n-nodes-postiz@0.2.17' "$pb"
  # HTTP Request-node usage stays pinned to the Postiz host: the ONE data blob
  # carries the restriction, and both write paths send that blob verbatim.
  assert_grep -qF 'allowedHttpRequestDomains: domains' "$pb"
  assert_grep -qF "allowedDomains: \"{{ _postiz_host | urlsplit('hostname') }}\"" "$pb"
  [ "$(grep -c 'data: "{{ _cred_data }}"' "$pb")" -eq 2 ]
}

@test "n8n: cutover guard diffs stateful values before deploy.sh and prints names only" {
  local f="$PB_DIR/deploy-n8n.yml"
  local shared="$PB_DIR/tasks/guard-stateful-cutover.yml"
  [ -f "$shared" ]
  # deploy-n8n declares exactly the three stateful keys and hands them to the
  # SHARED guard task (one implementation for every held in-place migration).
  # The encryption key carries the live-name alias for the standalone prod
  # layout, which spells it ENCRYPTION_KEY in its .env (design D6).
  assert_grep -qF 'name: N8N_ENCRYPTION_KEY' "$f"
  assert_grep -qF 'live_names: [N8N_ENCRYPTION_KEY, ENCRYPTION_KEY]' "$f"
  assert_grep -qE '^      - POSTGRES_PASSWORD$' "$f"
  assert_grep -qE '^      - POSTGRES_NON_ROOT_PASSWORD$' "$f"
  assert_grep -q 'include_tasks: tasks/guard-stateful-cutover.yml' "$f"
  # It fails closed BEFORE the container lifecycle: the include must appear
  # earlier in the play than the deploy.sh task.
  local g d
  g=$(grep -n 'include_tasks: tasks/guard-stateful-cutover.yml' "$f" | head -1 | cut -d: -f1)
  d=$(grep -n 'Run deploy.sh (container lifecycle)' "$f" | head -1 | cut -d: -f1)
  [ -n "$g" ] && [ -n "$d" ] && [ "$g" -lt "$d" ]
  # In the shared task: value-bearing steps are no_log; the refuse-assert
  # prints key NAMES only; greenfield hosts (no legacy file) skip.
  local blk
  for n in "Read both env files" "Diff the stateful values"; do
    blk=$(task_block "$shared" "$n")
    [ -n "$blk" ]
    assert_grep -q 'no_log: true' <<<"$blk"
  done
  blk=$(task_block "$shared" "Refuse to proceed on a stateful mismatch")
  [ -n "$blk" ]
  refute_grep -qE '_gsc_live_val|_gsc_new_val|_gsc_live_txt|_gsc_new_txt' <<<"$blk"
  assert_grep -q '_gsc_mismatches' <<<"$blk"
  blk=$(task_block "$shared" "Guard the cutover")
  assert_grep -q '_gsc_legacy.stat.exists' <<<"$blk"
}

@test "n8n: shared guard resolves live-name aliases, defaulting to the key's own name" {
  local shared="$PB_DIR/tasks/guard-stateful-cutover.yml"
  # A key entry may be a plain string or {name, live_names}; the live side
  # collects every declared spelling in one anchored alternation so a key
  # duplicated ACROSS spellings is refused as ambiguous like any duplicate.
  assert_grep -qF "_gsc_name: \"{{ item.name | default(item) }}\"" "$shared"
  assert_grep -qF "_gsc_live_names: \"{{ item.live_names | default([_gsc_name]) }}\"" "$shared"
  assert_grep -qF "regex_findall('(?m)^(?:' ~ (_gsc_live_names | join('|')) ~ ')=(.*)$')" "$shared"
  # The rendered side still matches the canonical name only.
  assert_grep -qF "regex_findall('(?m)^' ~ _gsc_name ~ '=(.*)$')" "$shared"
}

@test "n8n: pre-seed accepts both encryption-key spellings and stays exactly-one" {
  local f="$PB_DIR/seed-n8n-secrets.yml"
  # The standalone prod compose project writes ENCRYPTION_KEY; the monorepo
  # legacy path wrote N8N_ENCRYPTION_KEY. One anchored alternation collects
  # both, and the exactly-one assert refuses a file carrying both spellings.
  assert_grep -qF "regex_findall('(?m)^(?:N8N_ENCRYPTION_KEY|ENCRYPTION_KEY)=(.*)$')" "$f"
  blk=$(task_block "$f" "Require exactly one active assignment per stateful key")
  [ -n "$blk" ]
  assert_grep -qF '_enc_matches | length == 1' <<<"$blk"
}

@test "n8n: backup playbook dumps read-only into an owner-only dir, verified, no secrets" {
  local f="$PB_DIR/backup-n8n-db.yml"
  [ -f "$f" ]
  # Parameterized container so the cutover can point at the legacy project.
  assert_grep -qF "n8n_pg_container | default('workflow-n8n-postgres')" "$f"
  # Owner-only backup dir; dump content bypasses the run record via redirect.
  blk=$(task_block "$f" "Ensure the owner-only backup directory exists")
  [ -n "$blk" ]
  assert_grep -qF 'mode: "0700"' <<<"$blk"
  assert_grep -qF -- '--clean --if-exists' "$f"
  assert_grep -qF 'set -o pipefail' "$f"
  # The dump is verified real, not assumed.
  blk=$(task_block "$f" "Verify the dump is real")
  [ -n "$blk" ]
  assert_grep -qF 'PostgreSQL database dump' <<<"$blk"
  # Read-only against the store and the DB: no OpenBao calls, no psql writes.
  refute_grep -qE 'X-Vault-Token|approle/login' "$f"
}

@test "n8n: restore playbook demands an explicit dump and sequences stop -> restore -> start" {
  local f="$PB_DIR/restore-n8n-db.yml"
  [ -f "$f" ]
  # Never "the latest dump" — the file is a required extra var.
  blk=$(task_block "$f" "Require an explicit dump file")
  [ -n "$blk" ]
  assert_grep -qF 'n8n_dump_file is defined' <<<"$blk"
  # All-or-nothing restore.
  assert_grep -qF -- '-v ON_ERROR_STOP=1 --single-transaction' "$f"
  # App containers are stopped before the restore and started after it,
  # and health is verified rather than assumed.
  local s r t h
  s=$(grep -n 'Stop the n8n app containers' "$f" | head -1 | cut -d: -f1)
  r=$(grep -n 'Restore the dump' "$f" | head -1 | cut -d: -f1)
  t=$(grep -n 'Start the n8n app containers' "$f" | head -1 | cut -d: -f1)
  h=$(grep -n 'Wait for n8n health' "$f" | head -1 | cut -d: -f1)
  [ -n "$s" ] && [ -n "$r" ] && [ -n "$t" ] && [ -n "$h" ]
  [ "$s" -lt "$r" ] && [ "$r" -lt "$t" ] && [ "$t" -lt "$h" ]
}

@test "n8n: every lifecycle concern is a declared Semaphore template, destructive one marked" {
  local t="${BATS_TEST_DIRNAME}/../semaphore/templates.yml"
  local n
  for n in "Deploy n8n" "Seed n8n Secrets" "Clean Deploy n8n" "Store n8n API Key" "Provision n8n Postiz Credential" "Update n8n" "Back Up n8n DB" "Restore n8n DB"; do
    assert_grep -qE "^  - name: $n\$" "$t"
  done
  # The destructive ones say so, right in their declarations.
  local blk n
  for n in "Clean Deploy n8n" "Restore n8n DB"; do
    blk=$(awk -v name="$n" '$0=="  - name: "name{f=1;next} f&&/^  - name:/{exit} f{print}' "$t")
    [ -n "$blk" ]
    assert_grep -q 'DESTRUCTIVE' <<<"$blk"
  done
  # The local (Dev) flow exists for the chain phase 5 exercises, and the
  # upgrade pair is proven locally before it carries the prod cutover.
  for n in "Deploy n8n" "Store n8n API Key" "Provision n8n Postiz Credential" "Clean Deploy n8n" "Back Up n8n DB" "Restore n8n DB"; do
    blk=$(awk -v name="$n" '$0=="  - name: "name{f=1;next} f&&/^  - name:/{exit} f{print}' "$t")
    [ -n "$blk" ]
    assert_grep -q 'dev_variant: true' <<<"$blk"
  done
  # The worktree-bound (Local) variants exist too.
  local tl="${BATS_TEST_DIRNAME}/../semaphore/templates-local.yml"
  for n in "Back Up n8n DB (Local)" "Restore n8n DB (Local)"; do
    assert_grep -qF "  - name: \"$n\"" "$tl"
  done
}
