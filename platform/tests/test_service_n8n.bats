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
  assert_grep -qE '"checksum": "sha512-[A-Za-z0-9+/]+={0,2}"' "$f"
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
    "Authenticate to OpenBao for the owner password" \
    "Read the n8n owner password" \
    "Log in to n8n as the seeded owner" \
    "Fetch the valid API-key scopes" \
    "Create the API key" \
    "Take the raw key" \
    "Authenticate to OpenBao (AppRole)")
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
  # every line naming _api_key must pipe it through length.
  local rep
  rep=$(awk '/- name: "Report \(names and counts only/{f=1;next} f&&/^    - name:/{exit} f{print}' "$pb")
  [ -n "$rep" ]
  [ -z "$(grep -oE '_api_key[^|]*' <<<"$rep" | grep -v '_api_key \| length')" ]
  assert_grep -q '_api_key | length' <<<"$rep"
  # No other debug task can render the key either.
  refute_grep -qE 'debug:[[:space:]]*$' <<<"$(grep -A2 '_api_key' "$pb" | grep -v no_log | grep 'ansible.builtin.debug' || true)"
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
    "Re-test after the update" \
    "Test the stored credential")
  # ...and scoped: the report and the assertions stay diagnosable.
  for n in "Report (names, outcome and test status" "Require the Postiz API key"; do
    blk=$(task_block "$pb" "$n")
    [ -n "$blk" ]
    refute_grep -q 'no_log: true' <<<"$blk"
  done
  # The report never renders either key and restates the Postiz rate ceiling.
  local rep
  rep=$(awk '/- name: "Report \(names, outcome and test status/{f=1;next} f&&/^    - name:/{exit} f{print}' "$pb")
  [ -n "$rep" ]
  refute_grep -q 'api_key' <<<"$rep"
  assert_grep -q '90 posts/hour' <<<"$rep"
  # Transport guards precede the first credential-carrying request; both
  # endpoints are guarded; endpoints/shape verified at named versions.
  assert_guard_precedes_first_uri "$pb"
  assert_grep -q '_assert_url_label: "n8n"' "$pb"
  assert_grep -q 'n8n@2.25.7' "$pb"
  assert_grep -q 'n8n-nodes-postiz@0.2.17' "$pb"
  # HTTP Request-node usage stays pinned to the Postiz host, on create AND on
  # the in-place update (PATCH replaces the whole data blob).
  [ "$(grep -c 'allowedHttpRequestDomains: domains' "$pb")" -eq 2 ]
  [ "$(grep -c "allowedDomains: \"{{ _postiz_host | urlsplit('hostname') }}\"" "$pb")" -eq 2 ]
}

@test "n8n: cutover guard diffs stateful values before deploy.sh and prints names only" {
  local f="$PB_DIR/deploy-n8n.yml"
  # The guard covers exactly the three stateful keys.
  assert_grep -qF '_stateful_keys: [N8N_ENCRYPTION_KEY, POSTGRES_PASSWORD, POSTGRES_NON_ROOT_PASSWORD]' "$f"
  # It fails closed BEFORE the container lifecycle: the refuse-assert must
  # appear earlier in the play than the deploy.sh task.
  local g d
  g=$(grep -n 'Refuse to proceed on a stateful mismatch' "$f" | head -1 | cut -d: -f1)
  d=$(grep -n 'Run deploy.sh (container lifecycle)' "$f" | head -1 | cut -d: -f1)
  [ -n "$g" ] && [ -n "$d" ] && [ "$g" -lt "$d" ]
  # Value-bearing steps are no_log; the assert prints key NAMES only.
  local blk
  for n in "Read both env files" "Diff the stateful values"; do
    blk=$(task_block "$f" "$n")
    [ -n "$blk" ]
    assert_grep -q 'no_log: true' <<<"$blk"
  done
  blk=$(task_block "$f" "Refuse to proceed on a stateful mismatch")
  [ -n "$blk" ]
  refute_grep -qE '_live_val|_new_val|_live_txt|_new_txt' <<<"$blk"
  assert_grep -q '_stateful_mismatches' <<<"$blk"
  # Greenfield hosts skip: the whole block is gated on the legacy file existing.
  blk=$(task_block "$f" "Guard the cutover")
  assert_grep -q '_legacy.stat.exists' <<<"$blk"
}

@test "n8n: every lifecycle concern is a declared Semaphore template, destructive one marked" {
  local t="${BATS_TEST_DIRNAME}/../semaphore/templates.yml"
  local n
  for n in "Deploy n8n" "Seed n8n Secrets" "Clean Deploy n8n" "Store n8n API Key" "Provision n8n Postiz Credential" "Update n8n"; do
    assert_grep -qE "^  - name: $n\$" "$t"
  done
  # The destructive one says so, right in its declaration.
  local blk
  blk=$(awk '/^  - name: Clean Deploy n8n$/{f=1;next} f&&/^  - name:/{exit} f{print}' "$t")
  [ -n "$blk" ]
  assert_grep -q 'DESTRUCTIVE' <<<"$blk"
  # The local (Dev) flow exists for the chain phase 5 exercises.
  for n in "Deploy n8n" "Store n8n API Key" "Provision n8n Postiz Credential" "Clean Deploy n8n"; do
    blk=$(awk -v name="$n" '$0=="  - name: "name{f=1;next} f&&/^  - name:/{exit} f{print}' "$t")
    [ -n "$blk" ]
    assert_grep -q 'dev_variant: true' <<<"$blk"
  done
}
