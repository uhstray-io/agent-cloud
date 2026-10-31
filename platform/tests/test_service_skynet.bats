#!/usr/bin/env bats
# Structural and behavioural tests for the skynet deploy playbooks (deploy-skynet.yml,
# clean-deploy-skynet.yml) and their wiring: the local Semaphore templates, the local
# inventory example, the genesis INI, and AGENTS.md.
#
# The properties here are the ones whose violation is silent: an unpinned image that
# deploys whatever tag is current, a refusal that fires after the repo was placed or a
# secret written, no_log spread over a verify that then hides its own failure, a run
# endpoint that answers without a bearer, and a local INI that drifts from the example.
#
# Refusals are tested BEHAVIOURALLY: the refusal tasks are extracted from the playbook
# itself and executed with ansible-playbook, once per bad value. Editing the playbook's
# regex therefore changes what is tested. A predicate copied into this file would not.
#
# Tests that read the skynet service directory (deployment/compose.yml, deploy.sh,
# templates/env.j2) skip when that directory is absent from the worktree: it is built
# by a separate change. They run, and must pass, once it is merged.
#
# Run: bats platform/tests/test_service_skynet.bats

load assert_helpers

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  PLAYBOOK="$REPO_ROOT/platform/playbooks/deploy-skynet.yml"
  CLEAN_PLAYBOOK="$REPO_ROOT/platform/playbooks/clean-deploy-skynet.yml"
  DEPLOY_DIR="$REPO_ROOT/platform/services/skynet/deployment"
  [ -f "$PLAYBOOK" ]
  [ -f "$CLEAN_PLAYBOOK" ]
}

# Skip a test that reads the service directory (built by a separate change) only when
# the file it needs is absent. A present file is always tested.
skip_unless_service_file() {
  [ -f "$1" ] || skip "skynet deployment file not in this worktree yet: ${1#"$REPO_ROOT"/}"
}

# ── Deploy playbook: guards, ordering, composable shape ─────────────────────

@test "skynet: deploy refuses extra vars that set internal names, before anything runs" {
  assert_grep -qF 'ansible.builtin.import_playbook: refuse-internal-extra-vars.yml' "$PLAYBOOK"
  assert_precedes "$PLAYBOOK" 'import_playbook: refuse-internal-extra-vars\.yml' 'hosts: skynet_svc'
}

@test "skynet: pre-flight group is literal and passed twice (MISTAKES 2.21)" {
  assert_grep -qE '^    preflight_group: skynet_svc$' "$PLAYBOOK"
  assert_grep -qE '^    preflight_group_expected: skynet_svc$' "$PLAYBOOK"
}

@test "skynet: clean deploy wipes via clean-service, then imports the deploy" {
  # Anchored to the task lines: the comments in this file name both files as well.
  assert_grep -qE '^ +ansible\.builtin\.include_tasks: tasks/clean-service\.yml$' "$CLEAN_PLAYBOOK"
  assert_grep -qF 'import_playbook: deploy-skynet.yml' "$CLEAN_PLAYBOOK"
  assert_grep -qF '_extra_var_guard_nested: true' "$CLEAN_PLAYBOOK"
  assert_grep -qE '^ +ansible\.builtin\.import_playbook: refuse-internal-extra-vars\.yml$' "$CLEAN_PLAYBOOK"
  assert_precedes "$CLEAN_PLAYBOOK" 'import_playbook: refuse-internal-extra-vars\.yml' 'include_tasks: tasks/clean-service\.yml'
}

@test "skynet: api_token, db_password and read_token are generate-once; semaphore_api_token is existing and required" {
  assert_grep -qE 'name: api_token, type: random, length: 48' "$PLAYBOOK"
  assert_grep -qE 'name: db_password, type: random, length: 32' "$PLAYBOOK"
  assert_grep -qE 'name: read_token, type: random, length: 48' "$PLAYBOOK"
  assert_grep -qE 'name: semaphore_api_token, type: existing' "$PLAYBOOK"
  assert_grep -qF '_required_existing_secrets: [semaphore_api_token]' "$PLAYBOOK"
  assert_grep -qF 'tasks/manage-secrets.yml' "$PLAYBOOK"
  # The Semaphore token is written by a separate step (Provision skynet Semaphore Access). It must
  # never be generated here: no random or django definition may carry that name.
  refute_grep -qE 'name: semaphore_api_token, type: (random|django)' "$PLAYBOOK"
}

@test "skynet: shared-reads the six agentgateway role keys and nothing else" {
  local block
  block=$(sed -n '/^    _shared_reads:/,/^    _env_templates:/p' "$PLAYBOOK")
  assert_contains "$block" 'from_service: agentgateway'
  for role in infra-agent security-agent o11y-agent service-agent network-agent skynet-eval; do
    assert_contains "$block" "client_$role"
  done
  # The block names exactly six keys, so a seventh read cannot slip in unnoticed.
  [ "$(grep -c '^          - client_' <<<"$block")" -eq 6 ]
}

@test "skynet: env template is .env at 0600 from env.j2" {
  assert_grep -qE "_env_templates:" "$PLAYBOOK"
  assert_grep -qF '{ src: env.j2, dest: .env, mode: "0600" }' "$PLAYBOOK"
}

@test "skynet: the trust bundle is distributed in both modes, before deploy.sh; only production builds the combined bundle" {
  # Local: compose.local.yml mounts ./certs and sets SSL_CERT_FILE to the step-ca bundle. Production:
  # compose.prod.yml sets SSL_CERT_FILE to the combined bundle (the CA root and intermediate plus this
  # VM's public roots), with the root read from the step_ca_svc host. Both must land before deploy.sh.
  assert_grep -qF 'tasks/distribute-ca-root.yml' "$PLAYBOOK"
  [ "$(grep -c 'include_tasks: tasks/distribute-ca-root.yml' "$PLAYBOOK")" -eq 2 ]
  assert_precedes "$PLAYBOOK" 'tasks/distribute-ca-root\.yml' 'bash deploy\.sh'
  local local_block prod_block
  local_block=$(task_block "$PLAYBOOK" 'Distribute the step-ca root for internal TLS trust (local)')
  assert_contains "$local_block" '_ca_root_dest_dir: "{{ _deploy_dir }}"'
  assert_contains "$local_block" 'when: local_mode | default(false) | bool'
  refute_contains "$local_block" '_ca_root_combined_dest'
  prod_block=$(task_block "$PLAYBOOK" 'Distribute the step-ca root and the public roots for internal TLS trust (production)')
  assert_contains "$prod_block" '_ca_root_dest_dir: "{{ _deploy_dir }}"'
  assert_contains "$prod_block" "_ca_host: \"{{ groups['step_ca_svc'] | first }}\""
  assert_contains "$prod_block" "_ca_root_combined_dest: \"{{ _deploy_dir ~ '/certs/ca-bundle-combined.crt' }}\""
  assert_contains "$prod_block" 'when: not (local_mode | default(false) | bool)'
  # The combined bundle is named exactly once in the playbook: the production include.
  [ "$(grep -c '_ca_root_combined_dest' "$PLAYBOOK")" -eq 1 ]
}

@test "skynet: transport guard guards every play that reaches OpenBao" {
  # Play 1 (secret reads and writes) and Play 2 (registry login reads the GHCR PAT).
  [ "$(grep -c 'include_tasks: tasks/assert-bao-transport.yml' "$PLAYBOOK")" -eq 2 ]
}

@test "skynet: the OpenBao endpoint falls back to OPENBAO_ADDR, and both guards check that one resolved value" {
  local resolve="_bao_url: \"{{ openbao_addr | default(lookup('env', 'OPENBAO_ADDR'), true) | default('', true) }}\""
  # Play 1 writes secrets and play 2 reads the GHCR PAT; each defines the endpoint the same way.
  [ "$(grep -cF "$resolve" "$PLAYBOOK")" -eq 2 ]
  # Phase 2 names the resolved value, not a second lookup that could disagree with it.
  local play2
  play2=$(sed -n '/^- name: "Phase 2: Start skynet"/,/^- name: "Phase 3: Verify skynet"/p' "$PLAYBOOK")
  assert_contains "$play2" '_assert_bao_url: "{{ _bao_url }}"'
  assert_contains "$play2" "$resolve"
  refute_grep -qF '_assert_bao_url: "{{ openbao_addr' "$PLAYBOOK"
}

@test "skynet: the operator's Semaphore token is no longer read anywhere in the skynet deploy" {
  # The local-dev stopgap (secret/services/semaphore:api_token, operator-owned) is gone in every
  # environment. skynet's Semaphore identity is svc-skynet, whose token is an existing secret at
  # secret/services/skynet:semaphore_api_token.
  refute_grep -qF 'services/semaphore' "$PLAYBOOK"
  refute_grep -qF '_skynet_semaphore_token' "$PLAYBOOK"
  refute_grep -qF 'LOCAL-DEV STOPGAP' "$PLAYBOOK"
  refute_grep -qF 'community.hashi_vault.hashi_vault' "$PLAYBOOK"
  # Before the monorepo is placed, local_mode gates only the three production refusals (the old
  # stopgap refusal gated on it too). Every other refusal runs in every environment.
  local refusals_block
  refusals_block=$(sed -n '/^    - name: "Refuse skynet_image/,/^    - name: "Place the monorepo/p' "$PLAYBOOK")
  assert_contains "$refusals_block" 'Refuse skynet_publish_port'
  [ "$(grep -c 'local_mode' <<<"$refusals_block")" -eq 3 ]
  [ "$(grep -c 'when: not (local_mode | default(false) | bool)' <<<"$refusals_block")" -eq 3 ]
  if [ -f "$DEPLOY_DIR/templates/env.j2" ]; then
    refute_grep -qF '_skynet_semaphore_token' "$DEPLOY_DIR/templates/env.j2"
  fi
}

@test "skynet: the semaphore_api_token requirement is checked by manage-secrets before any OpenBao write" {
  # The deploy declares it as an existing secret. manage-secrets refuses the run by name when it is
  # absent, and that check precedes the store step, so a refused run writes nothing to OpenBao.
  local manage="$REPO_ROOT/platform/playbooks/tasks/manage-secrets.yml"
  assert_precedes "$manage" 'Require operator-owned secrets before any write' 'Store all secrets in OpenBao'
  assert_grep -qF '_required_existing_secrets: [semaphore_api_token]' "$PLAYBOOK"
}

@test "skynet: no_log appears nowhere in the deploy playbook (manage-secrets owns the credential reads)" {
  # no_log is for credential tasks only (AGENTS.md "Credential Handling"). The deploy playbook has
  # no credential task of its own now, and its verify and deploy tasks must keep their output, or a
  # failed probe is undiagnosable.
  python3 - "$PLAYBOOK" <<'PY'
import sys
import yaml

allowed = set()
plays = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
found = []
for play in plays:
    if not isinstance(play, dict):
        continue
    for task in play.get("tasks", []):
        if task.get("no_log") is True:
            found.append(task.get("name"))
extra = [name for name in found if name not in allowed]
missing = sorted(allowed - set(found))
if extra or missing:
    sys.exit(f"no_log tasks outside the credential set: {extra}; missing: {missing}")
PY
}

@test "skynet: registry login runs in local mode too (the GHCR package is private)" {
  refute_grep -q 'local_mode' <<<"$(task_block "$PLAYBOOK" 'Registry login for the private GHCR image')"
  assert_grep -qF 'tasks/registry-login.yml' "$PLAYBOOK"
}

@test "skynet: image pre-flight runs in both modes, skipped only under --check" {
  local block
  block=$(task_block "$PLAYBOOK" 'Pre-flight: pull the pinned skynet image')
  assert_contains "$block" 'not ansible_check_mode'
  refute_grep -q 'local_mode' <<<"$block"
  refute_grep -q 'local_mode' <<<"$(task_block "$PLAYBOOK" 'Pre-flight: require the pinned skynet image before deploying')"
}

@test "skynet: deploy.sh is the lifecycle step and reports its change on DEPLOY_CHANGED" {
  assert_grep -qF 'bash deploy.sh' "$PLAYBOOK"
  assert_grep -qF "'DEPLOY_CHANGED=true' in _deploy.stdout_lines" "$PLAYBOOK"
}

@test "skynet: deploy.sh stages the start, database first, then skynet, and chains every failure" {
  local deploy="$DEPLOY_DIR/deploy.sh"
  skip_unless_service_file "$deploy"
  # The hook must be defined before main runs, or common.sh falls back to the one-shot start.
  assert_precedes "$deploy" '^compose_staged_up\(\) \{' '^main\(\) \{'
  local block
  block=$(sed -n '/^compose_staged_up() {/,/^}/p' "$deploy")
  printf '%s\n' "$block" > "$BATS_TEST_TMPDIR/staged-up.sh"
  assert_precedes "$BATS_TEST_TMPDIR/staged-up.sh" 'compose up .*skynet-db' 'wait_for_healthy skynet-db'
  assert_precedes "$BATS_TEST_TMPDIR/staged-up.sh" 'wait_for_healthy skynet-db' 'compose up .*--no-deps skynet( |$)'
  # The helper runs the hook with errexit suspended, so each compose call that can fail returns.
  [ "$(grep -c '|| return 1' <<<"$block")" -eq 2 ]
}

@test "skynet: verify is inside the container and records the step result last" {
  local play3 last
  play3=$(sed -n '/^- name: "Phase 3: Verify skynet"/,$p' "$PLAYBOOK")
  assert_contains "$play3" '{{ _engine }}'
  assert_contains "$play3" 'exec'
  assert_contains "$play3" 'skynet'
  # Last task of the verify play, so a failed probe never records a green result.
  last=$(grep -E '^    - name: ' <<<"$play3" | tail -1)
  assert_contains "$last" 'Record the service-deploy step result'
  assert_grep -qF 'tasks/emit-service-deploy-result.yml' "$PLAYBOOK"
}

@test "skynet: verify probes healthz, an authenticated run (400) and an unauthenticated run (401)" {
  local play3
  play3=$(sed -n '/^- name: "Phase 3: Verify skynet"/,$p' "$PLAYBOOK")
  assert_contains "$play3" '/skynet/healthz'
  assert_contains "$play3" '/skynet/runs'
  assert_contains "$play3" '__agent_cloud_verify__'
  # The token is read by the probe inside the container, never placed in argv or the file.
  assert_contains "$play3" "os.environ['SKYNET_API_TOKEN']"
  assert_contains "$(task_block "$PLAYBOOK" 'Assert the authenticated run request got 400')" "== '400'"
  assert_contains "$(task_block "$PLAYBOOK" 'Assert the unauthenticated run request got 401')" "== '401'"
  assert_contains "$(task_block "$PLAYBOOK" 'Assert healthz answered 200')" "== '200'"
}

# ── Deploy playbook: refusals (behavioural) ──────────────────────────────────

@test "skynet: the twelve refusals are present as named assert tasks, all before the repo is placed" {
  local refusals
  refusals=$(python3 - "$PLAYBOOK" <<'PY'
import sys
import yaml

plays = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
play1 = next(p for p in plays if str(p.get("name", "")).startswith("Phase 1"))
names = [t["name"] for t in play1["tasks"] if "ansible.builtin.assert" in t]
print("\n".join(names))
PY
)
  [ "$(grep -c '^Refuse ' <<<"$refusals")" -eq 12 ]
  # The three production refusals name the CA host, the OPA address and the publish bind.
  assert_grep -qx 'Refuse a production run whose skynet_publish_bind is unset or loopback' <<<"$refusals"
  assert_grep -qx 'Refuse a production run without a step_ca_svc host to read the CA root from' <<<"$refusals"
  assert_grep -qx 'Refuse skynet_opa_ip unless it is one non-wildcard IPv4 address in production' <<<"$refusals"
  # The two publish refusals name the declared bind and port, so a reader finds each one.
  assert_grep -qx 'Refuse skynet_publish_bind unless it is one non-wildcard IPv4 address' <<<"$refusals"
  assert_grep -qx 'Refuse skynet_publish_port unless it is a whole port number from 1024 to 65535' <<<"$refusals"
  # Every refusal sits before the monorepo is placed, so a refused run changes nothing.
  assert_precedes "$PLAYBOOK" 'Refuse skynet_image unless' 'tasks/place-monorepo\.yml'
  assert_precedes "$PLAYBOOK" 'Refuse skynet_publish_bind unless' 'tasks/place-monorepo\.yml'
  assert_precedes "$PLAYBOOK" 'Refuse skynet_publish_port unless' 'tasks/place-monorepo\.yml'
  assert_precedes "$PLAYBOOK" 'Refuse skynet_semaphore_project unless' 'tasks/manage-secrets\.yml'
  # The svc-skynet token has no refusal here: it is an existing secret, and manage-secrets refuses
  # the run by name after the placement and before any OpenBao write (the Deploy console precedent).
  refute_grep -qiE 'semaphore (api )?token' <<<"$refusals"
}

@test "skynet: the read token lists runs (200) and cannot start one (401), inside the container" {
  local play3 list post
  play3=$(sed -n '/^- name: "Phase 3: Verify skynet"/,$p' "$PLAYBOOK")
  assert_contains "$play3" "os.environ['SKYNET_READ_TOKEN']"
  list=$(task_block "$PLAYBOOK" 'Read token lists runs (GET /skynet/runs -> 200, inside the container)')
  assert_contains "$list" "method='GET'"
  assert_contains "$list" "os.environ['SKYNET_READ_TOKEN']"
  refute_contains "$list" "SKYNET_API_TOKEN"
  assert_contains "$(task_block "$PLAYBOOK" 'Assert the read token lists runs (200)')" "== '200'"
  post=$(task_block "$PLAYBOOK" 'Read token cannot start a run (POST /skynet/runs -> 401, inside the container)')
  assert_contains "$post" "method='POST'"
  assert_contains "$post" "os.environ['SKYNET_READ_TOKEN']"
  refute_contains "$post" "SKYNET_API_TOKEN"
  assert_contains "$(task_block "$PLAYBOOK" 'Assert the read token is refused on POST /skynet/runs (401)')" "== '401'"
}

@test "skynet: the image refusal names the digest-pinned skynet-tier2 form and nothing looser" {
  local block
  block=$(task_block "$PLAYBOOK" 'Refuse skynet_image unless it is a digest-pinned skynet-tier2 reference')
  assert_contains "$block" "is match('^ghcr[.]io/uhstray-io/skynet-tier2@sha256:[0-9a-f]{64}\$')"
}

# Build the refusal playbook from the deploy playbook's own Phase 1 assert tasks, plus a
# baseline of values every case shares. Each case then changes exactly one value.
extract_refusals() {
  python3 - "$PLAYBOOK" "$BATS_TEST_TMPDIR" <<'PY'
import json
import sys
import yaml

src, out = sys.argv[1], sys.argv[2]
plays = yaml.safe_load(open(src, encoding="utf-8"))
play1 = next(p for p in plays if str(p.get("name", "")).startswith("Phase 1"))
tasks = [
    t for t in play1["tasks"]
    if "ansible.builtin.assert" in t and t["name"].startswith("Refuse ")
]
if len(tasks) != 12:
    sys.exit(f"expected 12 refusal asserts in Phase 1, found {len(tasks)}")
play = [{"name": "extracted refusals", "hosts": "localhost", "connection": "local",
         "gather_facts": False, "tasks": tasks}]
with open(f"{out}/refusals.yml", "w", encoding="utf-8") as fh:
    yaml.safe_dump(play, fh, sort_keys=False)
base = {
    "skynet_image": "ghcr.io/uhstray-io/skynet-tier2@sha256:" + "a" * 64,
    "skynet_gateway_url": "https://inference.agent-cloud.test",
    "skynet_opa_url": "https://opa.agent-cloud.test",
    "skynet_semaphore_url": "https://semaphore.agent-cloud.test",
    "skynet_semaphore_catalog": "local",
    "skynet_semaphore_project": 1,
    "skynet_model": "test-model",
    # Local mode skips the production refusals, so the baseline needs no CA host and no OPA address.
    "local_mode": True,
}
with open(f"{out}/base.json", "w", encoding="utf-8") as fh:
    json.dump(base, fh)
PY
}

# refused <var> <json-value>: exit 0 only when the playbook ACCEPTS the one changed value.
accepted() {
  ansible-playbook -i localhost, -c local "$BATS_TEST_TMPDIR/refusals.yml" \
    -e "@$BATS_TEST_TMPDIR/base.json" -e "{\"$1\": $2}" >/dev/null 2>&1
}

@test "skynet: the baseline values pass every refusal (the extraction is runnable)" {
  command -v ansible-playbook >/dev/null 2>&1 || skip "ansible-playbook not available"
  extract_refusals
  run ansible-playbook -i localhost, -c local "$BATS_TEST_TMPDIR/refusals.yml" -e "@$BATS_TEST_TMPDIR/base.json"
  [ "$status" -eq 0 ]
}

@test "skynet: skynet_image refuses a tag, a 63-hex digest, another repo and an empty value" {
  command -v ansible-playbook >/dev/null 2>&1 || skip "ansible-playbook not available"
  extract_refusals
  run accepted skynet_image '"ghcr.io/uhstray-io/skynet-tier2:latest"'
  [ "$status" -ne 0 ]
  run accepted skynet_image "\"ghcr.io/uhstray-io/skynet-tier2@sha256:$(printf 'a%.0s' {1..63})\""
  [ "$status" -ne 0 ]
  run accepted skynet_image '"ghcr.io/uhstray-io/other@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"'
  [ "$status" -ne 0 ]
  run accepted skynet_image '""'
  [ "$status" -ne 0 ]
}

@test "skynet: each gateway, OPA and Semaphore URL refuses a cleartext http:// value" {
  command -v ansible-playbook >/dev/null 2>&1 || skip "ansible-playbook not available"
  extract_refusals
  run accepted skynet_gateway_url '"http://inference.agent-cloud.test"'
  [ "$status" -ne 0 ]
  run accepted skynet_opa_url '"http://opa.agent-cloud.test"'
  [ "$status" -ne 0 ]
  run accepted skynet_semaphore_url '"http://semaphore.agent-cloud.test"'
  [ "$status" -ne 0 ]
}

@test "skynet: the catalog accepts only production or local" {
  command -v ansible-playbook >/dev/null 2>&1 || skip "ansible-playbook not available"
  extract_refusals
  run accepted skynet_semaphore_catalog '"staging"'
  [ "$status" -ne 0 ]
  run accepted skynet_semaphore_catalog '"production"'
  [ "$status" -eq 0 ]
}

@test "skynet: the Semaphore project id refuses a name, a boolean, zero and a trailing-garbage value" {
  command -v ansible-playbook >/dev/null 2>&1 || skip "ansible-playbook not available"
  extract_refusals
  run accepted skynet_semaphore_project '"abc"'
  [ "$status" -ne 0 ]
  run accepted skynet_semaphore_project 'true'
  [ "$status" -ne 0 ]
  run accepted skynet_semaphore_project '0'
  [ "$status" -ne 0 ]
  run accepted skynet_semaphore_project '"1x"'
  [ "$status" -ne 0 ]
  run accepted skynet_semaphore_project '"7"'
  [ "$status" -eq 0 ]
}

@test "skynet: an empty, blank or undefined skynet_model is refused; a declared one is accepted" {
  command -v ansible-playbook >/dev/null 2>&1 || skip "ansible-playbook not available"
  extract_refusals
  run accepted skynet_model '""'
  [ "$status" -ne 0 ]
  run accepted skynet_model '"   "'
  [ "$status" -ne 0 ]
  run accepted skynet_model '"test-model"'
  [ "$status" -eq 0 ]
  # Undefined: the baseline without the key, so no extra var supplies a value.
  python3 - "$BATS_TEST_TMPDIR/base.json" <<'PY'
import json
import sys

path = sys.argv[1]
with open(path, encoding="utf-8") as fh:
    base = json.load(fh)
del base["skynet_model"]
with open(path, "w", encoding="utf-8") as fh:
    json.dump(base, fh)
PY
  run ansible-playbook -i localhost, -c local "$BATS_TEST_TMPDIR/refusals.yml" -e "@$BATS_TEST_TMPDIR/base.json"
  [ "$status" -ne 0 ]
  assert_contains "$output" 'Declare skynet_model'
}

@test "skynet: skynet_publish_bind refuses 0.0.0.0, a name, a short address, an octet over 255 and empty; a non-wildcard address and loopback are accepted" {
  command -v ansible-playbook >/dev/null 2>&1 || skip "ansible-playbook not available"
  extract_refusals
  run accepted skynet_publish_bind '"0.0.0.0"'
  [ "$status" -ne 0 ]
  run accepted skynet_publish_bind '"localhost"'
  [ "$status" -ne 0 ]
  run accepted skynet_publish_bind '"10.0.0"'
  [ "$status" -ne 0 ]
  run accepted skynet_publish_bind '"999.1.1.1"'
  [ "$status" -ne 0 ]
  run accepted skynet_publish_bind '"00.0.0.0"'
  [ "$status" -ne 0 ]
  run accepted skynet_publish_bind '"203.0.113.010"'
  [ "$status" -ne 0 ]
  run accepted skynet_publish_bind '""'
  [ "$status" -ne 0 ]
  run accepted skynet_publish_bind '"203.0.113.10"'
  [ "$status" -eq 0 ]
  run accepted skynet_publish_bind '"127.0.0.1"'
  [ "$status" -eq 0 ]
}

@test "skynet: skynet_publish_port refuses a privileged, out-of-range, fractional or boolean value; 8380 is accepted" {
  command -v ansible-playbook >/dev/null 2>&1 || skip "ansible-playbook not available"
  extract_refusals
  run accepted skynet_publish_port '80'
  [ "$status" -ne 0 ]
  run accepted skynet_publish_port '1023'
  [ "$status" -ne 0 ]
  run accepted skynet_publish_port '65536'
  [ "$status" -ne 0 ]
  run accepted skynet_publish_port '8380.5'
  [ "$status" -ne 0 ]
  run accepted skynet_publish_port 'true'
  [ "$status" -ne 0 ]
  run accepted skynet_publish_port '"8380x"'
  [ "$status" -ne 0 ]
  run accepted skynet_publish_port '1024'
  [ "$status" -eq 0 ]
  run accepted skynet_publish_port '8380'
  [ "$status" -eq 0 ]
  run accepted skynet_publish_port '65535'
  [ "$status" -eq 0 ]
}

# ── Clean deploy and local wiring ────────────────────────────────────────────

# Production variant of accepted(): local_mode false, and an inventory declaring one step_ca_svc host.
# skynet_opa_ip is a valid address unless the case overrides it (a later -e wins).
prod_inventory() {
  printf 'localhost ansible_connection=local\n\n[step_ca_svc]\nlocalhost\n' > "$BATS_TEST_TMPDIR/prod-inventory.ini"
}

prod_accepted() {
  prod_inventory
  ansible-playbook -i "$BATS_TEST_TMPDIR/prod-inventory.ini" "$BATS_TEST_TMPDIR/refusals.yml" \
    -e "@$BATS_TEST_TMPDIR/base.json" -e local_mode=false -e skynet_opa_ip=203.0.113.20 -e skynet_publish_bind=203.0.113.10 \
    -e "{\"$1\": $2}" >/dev/null 2>&1
}

@test "skynet: production refuses a run with no step_ca_svc host, and accepts one with a host" {
  command -v ansible-playbook >/dev/null 2>&1 || skip "ansible-playbook not available"
  extract_refusals
  # No inventory group: the refusal names the missing CA host.
  run ansible-playbook -i localhost, -c local "$BATS_TEST_TMPDIR/refusals.yml" \
    -e "@$BATS_TEST_TMPDIR/base.json" -e local_mode=false -e skynet_opa_ip=203.0.113.20 -e skynet_publish_bind=203.0.113.10
  [ "$status" -ne 0 ]
  assert_contains "$output" 'step_ca_svc'
  run prod_accepted skynet_opa_ip '"203.0.113.20"'
  [ "$status" -eq 0 ]
}

@test "skynet: production refuses skynet_opa_ip unless it is one non-wildcard IPv4 address" {
  command -v ansible-playbook >/dev/null 2>&1 || skip "ansible-playbook not available"
  extract_refusals
  run prod_accepted skynet_opa_ip '"0.0.0.0"'
  [ "$status" -ne 0 ]
  run prod_accepted skynet_opa_ip '"opa.example.test"'
  [ "$status" -ne 0 ]
  run prod_accepted skynet_opa_ip '"203.0.113"'
  [ "$status" -ne 0 ]
  run prod_accepted skynet_opa_ip '"203.0.113.256"'
  [ "$status" -ne 0 ]
  run prod_accepted skynet_opa_ip '"00.0.0.0"'
  [ "$status" -ne 0 ]
  run prod_accepted skynet_opa_ip '""'
  [ "$status" -ne 0 ]
  run prod_accepted skynet_opa_ip '"203.0.113.20"'
  [ "$status" -eq 0 ]
}

@test "skynet: production refuses an unset or loopback skynet_publish_bind; local mode keeps the loopback default" {
  command -v ansible-playbook >/dev/null 2>&1 || skip "ansible-playbook not available"
  extract_refusals
  prod_inventory
  run ansible-playbook -i "$BATS_TEST_TMPDIR/prod-inventory.ini" "$BATS_TEST_TMPDIR/refusals.yml" \
    -e "@$BATS_TEST_TMPDIR/base.json" -e local_mode=false -e skynet_opa_ip=203.0.113.20
  [ "$status" -ne 0 ]
  assert_contains "$output" 'skynet_publish_bind'
  run prod_accepted skynet_publish_bind '"127.0.0.1"'
  [ "$status" -ne 0 ]
  run prod_accepted skynet_publish_bind '"203.0.113.10"'
  [ "$status" -eq 0 ]
  run ansible-playbook -i localhost, -c local "$BATS_TEST_TMPDIR/refusals.yml" -e "@$BATS_TEST_TMPDIR/base.json"
  [ "$status" -eq 0 ]
}

@test "skynet: production refuses an undeclared skynet_opa_ip; local mode needs neither it nor a CA host" {
  command -v ansible-playbook >/dev/null 2>&1 || skip "ansible-playbook not available"
  extract_refusals
  prod_inventory
  run ansible-playbook -i "$BATS_TEST_TMPDIR/prod-inventory.ini" "$BATS_TEST_TMPDIR/refusals.yml" \
    -e "@$BATS_TEST_TMPDIR/base.json" -e local_mode=false -e skynet_publish_bind=203.0.113.10
  [ "$status" -ne 0 ]
  assert_contains "$output" 'skynet_opa_ip'
  # Local mode: the baseline carries no skynet_opa_ip and no step_ca_svc group, and must pass.
  run ansible-playbook -i localhost, -c local "$BATS_TEST_TMPDIR/refusals.yml" -e "@$BATS_TEST_TMPDIR/base.json"
  [ "$status" -eq 0 ]
}

@test "skynet: the production overlay is a skynet-only override: certs read-only, combined bundle, OPA mapping from two required variables" {
  local overlay="$DEPLOY_DIR/compose.prod.yml"
  skip_unless_service_file "$overlay"
  assert_grep -qF -- '- ./certs:/certs:ro' "$overlay"
  assert_grep -qF 'SSL_CERT_FILE: /certs/ca-bundle-combined.crt' "$overlay"
  assert_grep -qF -- '- "${SKYNET_OPA_HOSTNAME:?set skynet_opa_url in the inventory}:${SKYNET_OPA_IP:?set skynet_opa_ip in the inventory}"' "$overlay"
  python3 - "$overlay" <<'PY'
import sys
import yaml

doc = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
assert list(doc["services"]) == ["skynet"], doc["services"]
assert set(doc["services"]["skynet"]) == {"volumes", "environment", "extra_hosts"}, doc["services"]["skynet"]
PY
}

@test "skynet: deploy.sh adds the production overlay only outside LOCAL_MODE" {
  local deploy="$DEPLOY_DIR/deploy.sh"
  skip_unless_service_file "$deploy"
  # Run the real gate block, extracted from the file, under each LOCAL_MODE value.
  local gate="$BATS_TEST_TMPDIR/overlay-gate.sh" prod_out local_out
  sed -n '/^if \[ "\${LOCAL_MODE:-}" != "true" \]; then$/,/^fi$/p' "$deploy" > "$gate"
  [ -s "$gate" ]
  prod_out=$(env -u LOCAL_MODE -u COMPOSE_OVERLAYS bash -c 'set -u; source "$1"; printf "%s" "${COMPOSE_OVERLAYS:-}"' _ "$gate")
  local_out=$(env -u COMPOSE_OVERLAYS LOCAL_MODE=true bash -c 'set -u; source "$1"; printf "%s" "${COMPOSE_OVERLAYS:-}"' _ "$gate")
  assert_contains "$prod_out" 'compose.prod.yml'
  refute_contains "$local_out" 'compose.prod.yml'
}

@test "skynet: env renders the OPA host mapping outside local mode only, with the hostname from skynet_opa_url" {
  command -v ansible-playbook >/dev/null 2>&1 || skip "ansible-playbook not available"
  local env="$DEPLOY_DIR/templates/env.j2"
  skip_unless_service_file "$env"
  cat > "$BATS_TEST_TMPDIR/render.yml" <<'YAML'
- hosts: localhost
  connection: local
  gather_facts: false
  tasks:
    - name: Render the skynet env file for this case
      ansible.builtin.template:
        src: "{{ env_src }}"
        dest: "{{ env_dest }}"
        mode: "0600"
YAML
  cat > "$BATS_TEST_TMPDIR/render-vars.json" <<JSON
{"env_src": "$env", "env_dest": "$BATS_TEST_TMPDIR/rendered.env", "local_mode": false,
 "secrets": {"api_token": "a", "db_password": "b", "read_token": "c", "semaphore_api_token": "d"},
 "skynet_image": "ghcr.io/uhstray-io/skynet-tier2@sha256:$(printf 'a%.0s' {1..64})",
 "skynet_gateway_url": "https://inference.example.test", "skynet_model": "test-model",
 "skynet_opa_url": "https://opa.example.test:8281", "skynet_opa_ip": "203.0.113.20",
 "skynet_semaphore_url": "https://semaphore.example.test", "skynet_semaphore_project": 1,
 "skynet_semaphore_catalog": "production"}
JSON
  run ansible-playbook "$BATS_TEST_TMPDIR/render.yml" -e "@$BATS_TEST_TMPDIR/render-vars.json"
  [ "$status" -eq 0 ]
  assert_grep -qx 'SKYNET_OPA_HOSTNAME=opa.example.test' "$BATS_TEST_TMPDIR/rendered.env"
  assert_grep -qx 'SKYNET_OPA_IP=203.0.113.20' "$BATS_TEST_TMPDIR/rendered.env"
  # Same inputs in local mode: the OPA mapping is not rendered.
  sed 's/"local_mode": false/"local_mode": true/' "$BATS_TEST_TMPDIR/render-vars.json" > "$BATS_TEST_TMPDIR/render-vars-local.json"
  run ansible-playbook "$BATS_TEST_TMPDIR/render.yml" -e "@$BATS_TEST_TMPDIR/render-vars-local.json"
  [ "$status" -eq 0 ]
  refute_grep -qE '^SKYNET_OPA_(HOSTNAME|IP)=' "$BATS_TEST_TMPDIR/rendered.env"
}

@test "skynet: the two local files pin the same skynet digest" {
  local digest="sha256:36d0bb9e63c888b44be988598dd28c0e15e4b95fcfae9279296a589d4a459596"
  assert_grep -qF "skynet-tier2@$digest" "$REPO_ROOT/platform/inventory/local-dev.yml.example"
  assert_grep -qF "skynet-tier2@$digest" "$REPO_ROOT/platform/playbooks/bootstrap-local-dev.yml"
  refute_grep -qF 'd1460ee9eabd71f1e809a2c05fdb15873c6f6fa4e6d2f7058ccee56179f929da' "$REPO_ROOT/platform/inventory/local-dev.yml.example"
  refute_grep -qF 'd1460ee9eabd71f1e809a2c05fdb15873c6f6fa4e6d2f7058ccee56179f929da' "$REPO_ROOT/platform/playbooks/bootstrap-local-dev.yml"
}

@test "skynet: local templates offer Deploy and Clean Deploy, the clean one behind confirm_reset" {
  python3 - "$REPO_ROOT/platform/semaphore/templates-local.yml" <<'PY'
import sys
import yaml

templates = {t["name"]: t for t in yaml.safe_load(open(sys.argv[1], encoding="utf-8"))["templates"]}
deploy = templates["Deploy skynet (Local)"]
assert deploy["playbook"] == "platform/playbooks/deploy-skynet.yml", deploy
clean = templates["Clean Deploy skynet (Local)"]
assert clean["playbook"] == "platform/playbooks/clean-deploy-skynet.yml", clean
names = [v["name"] for v in clean.get("survey_vars", [])]
assert names == ["confirm_reset"], names
assert clean["survey_vars"][0].get("required") is True, clean
PY
}

@test "skynet: the inventory example pins a digest that passes the deploy's own refusal regex" {
  python3 - "$REPO_ROOT/platform/inventory/local-dev.yml.example" "$PLAYBOOK" <<'PY'
import re
import sys
import yaml

inv = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
host = inv["all"]["children"]["skynet_svc"]["hosts"]["skynet-local"]
assert host["ansible_connection"] == "local" and host["ansible_host"] == "127.0.0.1", host
assert host["service_name"] == "skynet" and host["container_engine"] == "podman", host
assert host["monorepo_deploy_path"] == "platform/services/skynet/deployment", host
assert host["skynet_semaphore_catalog"] == "local" and host["skynet_semaphore_project"] == 1, host
# The pinned digest is active, and it passes the regex the deploy's own refusal applies. That regex
# is read from the playbook, so the example cannot drift from what the deploy accepts.
play1 = next(p for p in yaml.safe_load(open(sys.argv[2], encoding="utf-8")) if str(p.get("name", "")).startswith("Phase 1"))
refusal = next(t for t in play1["tasks"] if str(t.get("name", "")).startswith("Refuse skynet_image"))
pattern = re.search(r"is match\('([^']+)'\)", refusal["ansible.builtin.assert"]["that"][0]).group(1)
assert re.match(pattern, host["skynet_image"]), host["skynet_image"]
assert re.match(pattern, "ghcr.io/uhstray-io/skynet-tier2:latest") is None
PY
}

@test "skynet: the example's agentgateway route is commented out, not active" {
  python3 - "$REPO_ROOT/platform/inventory/local-dev.yml.example" <<'PY'
import sys
import yaml

inv = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
host = inv["all"]["children"]["agentgateway_svc"]["hosts"]["agentgateway-local"]
for key in ("agw_skynet_route_enabled", "agw_skynet_backend", "agw_skynet_route_identities"):
    assert key not in host, f"{key} must stay commented until skynet is deployed"
PY
  assert_grep -qE '^ +#   agw_skynet_route_enabled: true$' "$REPO_ROOT/platform/inventory/local-dev.yml.example"
  assert_grep -qE '^ +#   agw_skynet_backend: skynet:8080$' "$REPO_ROOT/platform/inventory/local-dev.yml.example"
}

@test "skynet: the genesis INI carries skynet_svc and agrees with the example on every value" {
  python3 - "$REPO_ROOT/platform/inventory/local-dev.yml.example" "$REPO_ROOT/platform/playbooks/bootstrap-local-dev.yml" <<'PY'
import configparser
import sys
import yaml

example = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
host = example["all"]["children"]["skynet_svc"]["hosts"]["skynet-local"]

def find(node, key):
    if isinstance(node, dict):
        if key in node:
            return node[key]
        for value in node.values():
            hit = find(value, key)
            if hit is not None:
                return hit
    elif isinstance(node, list):
        for value in node:
            hit = find(value, key)
            if hit is not None:
                return hit
    return None

ini_text = find(yaml.safe_load(open(sys.argv[2], encoding="utf-8")), "_inv_ini")
assert ini_text, "no _inv_ini in bootstrap-local-dev.yml"
# The INI is a Jinja template: a {{ }} value is not an INI literal, so read the keys only.
lines = [ln.strip() for ln in ini_text.splitlines()]
start = lines.index("[skynet_svc:vars]")
section = []
for ln in lines[start + 1:]:
    if ln.startswith("["):
        break
    if ln and not ln.startswith("#"):
        section.append(ln)
ini = dict(ln.split("=", 1) for ln in section)
for key, value in host.items():
    if key in ("ansible_host", "ansible_connection", "skynet_semaphore_project"):
        continue
    assert key in ini, f"{key} missing from the INI skynet_svc:vars"
    assert ini[key] == str(value), f"{key}: INI {ini[key]!r} != example {value!r}"
assert "skynet_image" in ini, "the INI must carry the same pinned skynet_image as the example"
# The project id is resolved live by name (_pid), so the INI names that lookup, not the example's literal.
assert ini["skynet_semaphore_project"] == "{{ _pid }}", ini["skynet_semaphore_project"]
PY
}

@test "skynet: the genesis INI forwards the agentgateway skynet route switch from local-dev, off by default" {
  # bootstrap renders the Semaphore inventory from the operator's local-dev.yml, so the route keys
  # set on agentgateway-local there must be forwarded here, or enabling the route does nothing.
  python3 - "$REPO_ROOT/platform/playbooks/bootstrap-local-dev.yml" <<'PY'
import sys
import yaml

def find(node, key):
    if isinstance(node, dict):
        if key in node:
            return node[key]
        for value in node.values():
            hit = find(value, key)
            if hit is not None:
                return hit
    elif isinstance(node, list):
        for value in node:
            hit = find(value, key)
            if hit is not None:
                return hit
    return None

ini_text = find(yaml.safe_load(open(sys.argv[1], encoding="utf-8")), "_inv_ini")
assert ini_text, "no _inv_ini in bootstrap-local-dev.yml"
lines = [ln.strip() for ln in ini_text.splitlines()]
start = lines.index("[agentgateway_svc:vars]")
section = []
for ln in lines[start + 1:]:
    if ln.startswith("["):
        break
    if ln and not ln.startswith("#"):
        section.append(ln)
ini = dict(ln.split("=", 1) for ln in section)
forwarded = {
    "agw_skynet_route_enabled": "{{ hostvars['agentgateway-local'].agw_skynet_route_enabled | default(false) }}",
    "agw_skynet_backend": "{{ hostvars['agentgateway-local'].agw_skynet_backend | default('') }}",
    "agw_skynet_route_identities": "{{ hostvars['agentgateway-local'].agw_skynet_route_identities | default([]) | to_json }}",
}
for key, value in forwarded.items():
    assert ini.get(key) == value, f"{key}: INI {ini.get(key)!r} != {value!r}"
PY
}

@test "skynet: AGENTS.md lists the deploy, clean deploy, secret path and sub-directory doc" {
  local agents="$REPO_ROOT/AGENTS.md"
  assert_grep -qF '| Deploy skynet | `deploy-skynet.yml` |' "$agents"
  assert_grep -qF '| Clean Deploy skynet | `clean-deploy-skynet.yml` |' "$agents"
  assert_grep -qF '| `secret/services/skynet` |' "$agents"
  assert_grep -qF '`platform/services/skynet/deployment/CLAUDE.md`' "$agents"
}

# ── Part 1 contract (skip until the service directory is in this worktree) ───

@test "skynet: env renders the svc-skynet Semaphore token, the read token and the distinct publish names" {
  local env="$DEPLOY_DIR/templates/env.j2"
  skip_unless_service_file "$env"
  assert_grep -qF '{{ secrets.api_token }}' "$env"
  assert_grep -qF '{{ secrets.db_password }}' "$env"
  assert_grep -qF 'SKYNET_SEMAPHORE_TOKEN={{ secrets.semaphore_api_token }}' "$env"
  assert_grep -qF 'SKYNET_READ_TOKEN={{ secrets.read_token }}' "$env"
  assert_grep -qF "SKYNET_PUBLISH_BIND={{ skynet_publish_bind | default('127.0.0.1') }}" "$env"
  assert_grep -qF 'SKYNET_PUBLISH_PORT={{ skynet_publish_port | default(8380) }}' "$env"
  # The verify reads the bearer from the container's environment under this exact name.
  assert_grep -qE '^SKYNET_API_TOKEN=' "$env"
  # The image sets SKYNET_BIND=0.0.0.0 and SKYNET_PORT; rendering either here would move the listener.
  refute_grep -qE '^SKYNET_(BIND|PORT)=' "$env"
  refute_grep -qF '_skynet_semaphore_token' "$env"
}

@test "skynet: compose publishes the API on the declared bind and port, loopback by default; skynet-db publishes nothing" {
  local compose="$DEPLOY_DIR/compose.yml"
  skip_unless_service_file "$compose"
  assert_grep -qE '^  skynet:' "$compose"
  assert_grep -qE '^  skynet-db:' "$compose"
  assert_grep -qF '      - "${SKYNET_PUBLISH_BIND:-127.0.0.1}:${SKYNET_PUBLISH_PORT:-8380}:8080"' "$compose"
  # One ports list, and it sits inside the skynet service, before skynet-db.
  [ "$(grep -c '^    ports:' "$compose")" -eq 1 ]
  assert_precedes "$compose" '^  skynet:' '^    ports:'
  assert_precedes "$compose" '^    ports:' '^  skynet-db:'
  # The image's own SKYNET_BIND and SKYNET_PORT are never substituted as the publish knobs (a
  # comment may name them; a ${...} substitution may not).
  refute_grep -qE '[$][{]SKYNET_(BIND|PORT)[}:-]' "$compose"
}

@test "skynet: production templates offer Deploy and Clean Deploy, dev-bound, the clean one behind confirm_reset" {
  python3 - "$REPO_ROOT/platform/semaphore/templates.yml" <<'PY'
import sys
import yaml

templates = {t["name"]: t for t in yaml.safe_load(open(sys.argv[1], encoding="utf-8"))["templates"]}
deploy = templates["Deploy skynet"]
assert deploy["playbook"] == "platform/playbooks/deploy-skynet.yml", deploy
assert deploy.get("dev_variant") is True, deploy
fields = {v["name"]: v for v in deploy.get("survey_vars", [])}
assert set(fields) == {"service_branch", "deploy_force_recreate"}, sorted(fields)
assert fields["service_branch"].get("default_value") == "main", fields["service_branch"]
clean = templates["Clean Deploy skynet"]
assert clean["playbook"] == "platform/playbooks/clean-deploy-skynet.yml", clean
assert clean.get("dev_variant") is True, clean
cfields = {v["name"]: v for v in clean.get("survey_vars", [])}
assert set(cfields) == {"confirm_reset", "service_branch"}, sorted(cfields)
reset = cfields["confirm_reset"]
assert reset.get("required") is True and "default_value" not in reset, reset
PY
}

@test "skynet: verify proves skynet's own OPA client gets OPA's default deny, inside the container" {
  python3 - "$PLAYBOOK" <<'PY'
import sys
import yaml

plays = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
verify = next(p for p in plays if str(p.get("name", "")).startswith("Phase 3"))
tasks = verify["tasks"]
names = [t.get("name", "") for t in tasks]
probe = next(t for t in tasks if t.get("name", "").startswith("OPA decision through skynet's own client"))
argv = probe["ansible.builtin.command"]["argv"]
code = argv[-1]
# skynet's real client, against the URL the container was given: not a hand-rolled request.
assert argv[1:3] == ["exec", "skynet"], argv
assert "from skynet.policy.opa import DecisionInput, OPAClient" in code, code
assert "OPAClient(os.environ['SKYNET_OPA_URL']" in code, code
assert "agent='skynet'" in code and "service='__agent_cloud_verify__'" in code, code
# The printed verdict is OPA's, not a constant: allow/deny is mapped from decision.allowed.
assert "print('decision=' + ('allow' if decision.allowed else 'deny'))" in code, code
assert probe.get("check_mode") is False, probe
check = next(t for t in tasks if t.get("name") == "Assert skynet reached OPA and was denied by default")
that = check["ansible.builtin.assert"]["that"]
assert "_skynet_opa.rc == 0" in that, that
assert "(_skynet_opa.stdout_lines | last | default('')) == 'decision=deny'" in that, that
# Runs after the read-token checks and before the step result is recorded.
assert names.index(probe["name"]) < names.index(check["name"]) < names.index("Record the service-deploy step result"), names
PY
}
