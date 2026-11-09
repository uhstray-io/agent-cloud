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

# Stops only the stub launch gate a test started. The PID is the one that test recorded with $!; nothing
# is matched by name, so no other process is touched.
teardown() {
  kill "$(cat "$BATS_TEST_TMPDIR/stub.pid" 2>/dev/null)" 2>/dev/null || true
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
  block=$(sed -n '/^    _skynet_agentgateway_reads:/,/^    _skynet_launch_gate_reads:/p' "$PLAYBOOK")
  assert_contains "$block" 'from_service: agentgateway'
  for role in infra-agent security-agent o11y-agent service-agent network-agent skynet-eval; do
    assert_contains "$block" "client_$role"
  done
  # The block names exactly six keys, so a seventh read cannot slip in unnoticed.
  [ "$(grep -c '^          - client_' <<<"$block")" -eq 6 ]
  # _shared_reads is the agentgateway list plus the optional launch-gate read, and nothing else.
  assert_grep -qF '_shared_reads: "{{ _skynet_agentgateway_reads + _skynet_launch_gate_reads }}"' "$PLAYBOOK"
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
  # Before the monorepo is placed, local_mode gates only the four production refusals (the three
  # long-standing ones and the launch-gate IP, which is also gated on the gate being declared). Every
  # other refusal runs in every environment.
  local refusals_block
  refusals_block=$(sed -n '/^    - name: "Refuse skynet_image/,/^    - name: "Place the monorepo/p' "$PLAYBOOK")
  assert_contains "$refusals_block" 'Refuse skynet_publish_port'
  [ "$(grep -c 'local_mode' <<<"$refusals_block")" -eq 4 ]
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

@test "skynet: the fourteen refusals are present as named assert tasks, all before the repo is placed" {
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
  [ "$(grep -c '^Refuse ' <<<"$refusals")" -eq 14 ]
  # The launch-gate refusals: the URL is https in every mode, and its IP is checked in production.
  assert_grep -qx 'Refuse skynet_launch_gate_url unless it is https' <<<"$refusals"
  assert_grep -qx 'Refuse skynet_launch_gate_ip unless it is one non-wildcard IPv4 address in production' <<<"$refusals"
  assert_precedes "$PLAYBOOK" 'Refuse skynet_launch_gate_url unless' 'tasks/place-monorepo\.yml'
  assert_precedes "$PLAYBOOK" 'Refuse skynet_launch_gate_ip unless' 'tasks/place-monorepo\.yml'
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
if len(tasks) != 14:
    sys.exit(f"expected 14 refusal asserts in Phase 1, found {len(tasks)}")
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

@test "skynet: the launch gate's token is shared-read only when skynet_launch_gate_url is declared" {
  command -v ansible-playbook >/dev/null 2>&1 || skip "ansible-playbook not available"
  # Evaluate the real Phase 1 vars (not a copy) and write _shared_reads out, once undeclared and once declared.
  python3 - "$PLAYBOOK" "$BATS_TEST_TMPDIR/shared-reads.yml" <<'PY'
import sys
import yaml

plays = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
phase1 = next(p for p in plays if str(p.get("name", "")).startswith("Phase 1"))
probe = {
    "name": "shared reads",
    "hosts": "localhost",
    "connection": "local",
    "gather_facts": False,
    "vars": phase1["vars"],
    "tasks": [{"ansible.builtin.copy": {"content": "{{ _shared_reads | to_json }}", "dest": "{{ reads_out }}"}}],
}
with open(sys.argv[2], "w", encoding="utf-8") as fh:
    yaml.safe_dump([probe], fh, sort_keys=False)
PY
  run ansible-playbook -i localhost, -c local "$BATS_TEST_TMPDIR/shared-reads.yml" -e "reads_out=$BATS_TEST_TMPDIR/reads-unset.json"
  [ "$status" -eq 0 ]
  run ansible-playbook -i localhost, -c local "$BATS_TEST_TMPDIR/shared-reads.yml" \
    -e "reads_out=$BATS_TEST_TMPDIR/reads-set.json" -e skynet_launch_gate_url=https://launch-gate.example.test:8443
  [ "$status" -eq 0 ]
  python3 - "$BATS_TEST_TMPDIR/reads-unset.json" "$BATS_TEST_TMPDIR/reads-set.json" <<'PY'
import json
import sys

unset = json.load(open(sys.argv[1], encoding="utf-8"))
declared = json.load(open(sys.argv[2], encoding="utf-8"))
assert [item["from_service"] for item in unset] == ["agentgateway"], unset
assert [item["from_service"] for item in declared] == ["agentgateway", "launch-gate"], declared
assert declared[0] == unset[0], "the agentgateway read changed when the gate was declared"
assert declared[1] == {"from_service": "launch-gate", "read_keys": ["caller_skynet_token"]}, declared[1]
PY
}

@test "skynet: the launch gate URL refuses cleartext http in every mode, and a declared https URL is accepted" {
  command -v ansible-playbook >/dev/null 2>&1 || skip "ansible-playbook not available"
  extract_refusals
  run accepted skynet_launch_gate_url '"http://launch-gate.example.test:8443"'
  [ "$status" -ne 0 ]
  run accepted skynet_launch_gate_url '"launch-gate.example.test:8443"'
  [ "$status" -ne 0 ]
  # No host: urlsplit renders an empty name, deploy.sh would skip the mapping, and the verify would fail late.
  run accepted skynet_launch_gate_url '"https://"'
  [ "$status" -ne 0 ]
  run accepted skynet_launch_gate_url '"https:///v1"'
  [ "$status" -ne 0 ]
  # User info: urlsplit gives no host for "https://@" and "https://user:pw@", and the whole URL, credential
  # included, is rendered into .env as SKYNET_LAUNCH_GATE_URL. A query or fragment is not part of the address.
  run accepted skynet_launch_gate_url '"https://@"'
  [ "$status" -ne 0 ]
  run accepted skynet_launch_gate_url '"https://user:pw@"'
  [ "$status" -ne 0 ]
  run accepted skynet_launch_gate_url '"https://user:pw@launch-gate.example.test:8443"'
  [ "$status" -ne 0 ]
  run accepted skynet_launch_gate_url '"https://launch-gate.example.test:8443/?x=1"'
  [ "$status" -ne 0 ]
  run accepted skynet_launch_gate_url '"https://launch-gate.example.test:8443"'
  [ "$status" -eq 0 ]
  run accepted skynet_launch_gate_url '"https://launch-gate.example.test:8443/"'
  [ "$status" -eq 0 ]
  run accepted skynet_launch_gate_url '"https://launch-gate.local.agent-cloud.test:8443"'
  [ "$status" -eq 0 ]
}

# gate_prod_accepted <json-value>: production, gate declared, the IP set to the value. An empty value leaves
# the IP undeclared. Exit 0 only when the playbook accepts it.
gate_prod_accepted() {
  prod_inventory
  local extra=(-e "skynet_launch_gate_url=https://launch-gate.example.test:8443")
  if [ -n "$1" ]; then
    extra+=(-e "{\"skynet_launch_gate_ip\": $1}")
  fi
  ansible-playbook -i "$BATS_TEST_TMPDIR/prod-inventory.ini" "$BATS_TEST_TMPDIR/refusals.yml" \
    -e "@$BATS_TEST_TMPDIR/base.json" -e local_mode=false -e skynet_opa_ip=203.0.113.20 -e skynet_publish_bind=203.0.113.10 \
    "${extra[@]}" >/dev/null 2>&1
}

@test "skynet: production with the gate declared refuses an undeclared or malformed skynet_launch_gate_ip; a non-wildcard address is accepted" {
  command -v ansible-playbook >/dev/null 2>&1 || skip "ansible-playbook not available"
  extract_refusals
  run gate_prod_accepted ''
  [ "$status" -ne 0 ]
  run gate_prod_accepted '"0.0.0.0"'
  [ "$status" -ne 0 ]
  run gate_prod_accepted '"launch-gate.example.test"'
  [ "$status" -ne 0 ]
  run gate_prod_accepted '"203.0.113"'
  [ "$status" -ne 0 ]
  run gate_prod_accepted '"203.0.113.256"'
  [ "$status" -ne 0 ]
  run gate_prod_accepted '"00.0.0.0"'
  [ "$status" -ne 0 ]
  run gate_prod_accepted '""'
  [ "$status" -ne 0 ]
  run gate_prod_accepted '"203.0.113.30"'
  [ "$status" -eq 0 ]
}

@test "skynet: an undeclared gate adds no refusal in production, and local mode does not check the gate IP" {
  command -v ansible-playbook >/dev/null 2>&1 || skip "ansible-playbook not available"
  extract_refusals
  prod_inventory
  # Production, no gate declared, no gate IP: the run that passed before the gate existed still passes.
  run ansible-playbook -i "$BATS_TEST_TMPDIR/prod-inventory.ini" "$BATS_TEST_TMPDIR/refusals.yml" \
    -e "@$BATS_TEST_TMPDIR/base.json" -e local_mode=false -e skynet_opa_ip=203.0.113.20 -e skynet_publish_bind=203.0.113.10
  [ "$status" -eq 0 ]
  # Local mode, gate declared: the gate is reached by its local-dev alias, so no IP is required or checked.
  run ansible-playbook -i localhost, -c local "$BATS_TEST_TMPDIR/refusals.yml" -e "@$BATS_TEST_TMPDIR/base.json" \
    -e skynet_launch_gate_url=https://launch-gate.example.test:8443 -e '{"skynet_launch_gate_ip": "0.0.0.0"}'
  [ "$status" -eq 0 ]
}

@test "skynet: env renders the launch gate only when declared, and the production host mapping only outside local mode" {
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
  python3 - "$env" "$BATS_TEST_TMPDIR" <<'PY'
import json
import sys

env, tmp = sys.argv[1], sys.argv[2]
base = {
    "env_src": env,
    "secrets": {"api_token": "a", "db_password": "b", "read_token": "c",
                "semaphore_api_token": "d", "caller_skynet_token": "g"},
    "skynet_image": "ghcr.io/uhstray-io/skynet-tier2@sha256:" + "a" * 64,
    "skynet_gateway_url": "https://inference.example.test",
    "skynet_model": "test-model",
    "skynet_opa_url": "https://opa.example.test:8281",
    "skynet_opa_ip": "203.0.113.20",
    "skynet_semaphore_url": "https://semaphore.example.test",
    "skynet_semaphore_project": 1,
    "skynet_semaphore_catalog": "production",
}
gate = {"skynet_launch_gate_url": "https://launch-gate.example.test:8443"}
cases = {
    "unset": {**base, "local_mode": False},
    "local": {**base, **gate, "local_mode": True},
    "prod": {**base, **gate, "skynet_launch_gate_ip": "203.0.113.30", "local_mode": False},
}
for name, values in cases.items():
    values["env_dest"] = f"{tmp}/rendered-{name}.env"
    with open(f"{tmp}/vars-{name}.json", "w", encoding="utf-8") as fh:
        json.dump(values, fh)
PY
  local name
  for name in unset local prod; do
    ansible-playbook "$BATS_TEST_TMPDIR/render.yml" -e "@$BATS_TEST_TMPDIR/vars-$name.json" >/dev/null
  done
  refute_grep -qE '^SKYNET_LAUNCH_GATE_' "$BATS_TEST_TMPDIR/rendered-unset.env"
  assert_grep -qx 'SKYNET_LAUNCH_GATE_URL=https://launch-gate.example.test:8443' "$BATS_TEST_TMPDIR/rendered-local.env"
  assert_grep -qx 'SKYNET_LAUNCH_GATE_TOKEN=g' "$BATS_TEST_TMPDIR/rendered-local.env"
  refute_grep -qE '^SKYNET_LAUNCH_GATE_(HOSTNAME|IP)=' "$BATS_TEST_TMPDIR/rendered-local.env"
  assert_grep -qx 'SKYNET_LAUNCH_GATE_URL=https://launch-gate.example.test:8443' "$BATS_TEST_TMPDIR/rendered-prod.env"
  assert_grep -qx 'SKYNET_LAUNCH_GATE_TOKEN=g' "$BATS_TEST_TMPDIR/rendered-prod.env"
  assert_grep -qx 'SKYNET_LAUNCH_GATE_HOSTNAME=launch-gate.example.test' "$BATS_TEST_TMPDIR/rendered-prod.env"
  assert_grep -qx 'SKYNET_LAUNCH_GATE_IP=203.0.113.30' "$BATS_TEST_TMPDIR/rendered-prod.env"
}

@test "skynet: deploy.sh loads the launch-gate overlay only outside LOCAL_MODE and only when .env maps the gate" {
  local deploy="$DEPLOY_DIR/deploy.sh"
  skip_unless_service_file "$deploy"
  # Run the real gate block, extracted from the file, against a .env in each directory under each LOCAL_MODE.
  local gate="$BATS_TEST_TMPDIR/gate-overlay.sh"
  sed -n '/^if .*SKYNET_LAUNCH_GATE_HOSTNAME=/,/^fi$/p' "$deploy" > "$gate"
  [ -s "$gate" ]
  local mapped="$BATS_TEST_TMPDIR/mapped" blank="$BATS_TEST_TMPDIR/blank" absent="$BATS_TEST_TMPDIR/absent"
  mkdir -p "$mapped" "$blank" "$absent"
  printf 'SKYNET_LAUNCH_GATE_HOSTNAME=launch-gate.example.test\n' > "$mapped/.env"
  printf 'SKYNET_LAUNCH_GATE_HOSTNAME=\n' > "$blank/.env"
  # overlays_for <directory holding .env> <LOCAL_MODE value, or empty for unset>
  overlays_for() {
    if [ -n "$2" ]; then
      (cd "$1" && env -u COMPOSE_OVERLAYS LOCAL_MODE="$2" bash -c 'set -u; source "$1"; printf "%s" "${COMPOSE_OVERLAYS:-}"' _ "$gate")
    else
      (cd "$1" && env -u COMPOSE_OVERLAYS -u LOCAL_MODE bash -c 'set -u; source "$1"; printf "%s" "${COMPOSE_OVERLAYS:-}"' _ "$gate")
    fi
  }
  assert_contains "$(overlays_for "$mapped" '')" 'compose.launch-gate.yml'
  refute_contains "$(overlays_for "$mapped" true)" 'compose.launch-gate.yml'
  refute_contains "$(overlays_for "$blank" '')" 'compose.launch-gate.yml'
  refute_contains "$(overlays_for "$absent" '')" 'compose.launch-gate.yml'
}

@test "skynet: the launch-gate overlay is a skynet-only override that maps the gate name from two required variables" {
  local overlay="$DEPLOY_DIR/compose.launch-gate.yml"
  skip_unless_service_file "$overlay"
  assert_grep -qF -- '- "${SKYNET_LAUNCH_GATE_HOSTNAME:?set skynet_launch_gate_url in the inventory}:${SKYNET_LAUNCH_GATE_IP:?set skynet_launch_gate_ip in the inventory}"' "$overlay"
  python3 - "$overlay" <<'PY'
import sys
import yaml

doc = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
assert list(doc["services"]) == ["skynet"], doc["services"]
assert set(doc["services"]["skynet"]) == {"extra_hosts"}, doc["services"]["skynet"]
PY
}

@test "skynet: the launch-gate probes run inside the container, take the URL and token from its environment, and run only when declared" {
  local play3 anon authed block
  play3=$(sed -n '/^- name: "Phase 3: Verify skynet"/,$p' "$PLAYBOOK")
  anon=$(task_block "$PLAYBOOK" 'Launch gate refuses a request with no token (POST /v1/launch -> 401, inside the container)')
  authed=$(task_block "$PLAYBOOK" 'Launch gate accepts the caller token (POST /v1/launch -> 400 bad_request, inside the container)')
  # Both probes run one shared script, defined once in the verify play's vars. The mode in argv is the only
  # difference between them, and the anonymous probe's argv names no token.
  assert_contains "$anon" '"{{ _skynet_gate_probe_script }}"'
  assert_contains "$authed" '"{{ _skynet_gate_probe_script }}"'
  assert_grep -qE '^ +- anon$' <<<"$anon"
  assert_grep -qE '^ +- caller$' <<<"$authed"
  # Both run isolated (python -I), so the container's working directory is not put on the import path.
  assert_grep -qE '^ +- -I$' <<<"$anon"
  assert_grep -qE '^ +- -I$' <<<"$authed"
  # The script is a linted file loaded by lookup; the playbook carries no copy of it.
  assert_contains "$play3" "_skynet_gate_probe_script: \"{{ lookup('ansible.builtin.file', playbook_dir ~ '/files/skynet-launch-gate-probe.py') }}\""
  [ -f "$REPO_ROOT/platform/playbooks/files/skynet-launch-gate-probe.py" ]
  # The unauthenticated probe must not carry the token at all.
  refute_contains "$anon" 'SKYNET_LAUNCH_GATE_TOKEN'
  for block in "$anon" "$authed"; do
    assert_contains "$block" 'check_mode: false'
    assert_contains "$block" 'changed_when: false'
    assert_contains "$block" "when: (skynet_launch_gate_url | default('', true) | string | trim | length) > 0"
    refute_contains "$block" 'no_log'
  done
  assert_contains "$(task_block "$PLAYBOOK" 'Assert the launch gate refused a request with no token (401 unauthorized)')" "'401 unauthorized'"
  assert_contains "$(task_block "$PLAYBOOK" 'Assert the launch gate accepted the caller token (400 bad_request)')" "'400 bad_request'"
  # The token name appears nowhere in the verify play but inside the container's environment read above.
  refute_contains "$play3" 'caller_skynet_token'
  assert_contains "$(task_block "$PLAYBOOK" 'Report')" 'skynet_launch_gate_url'
}

# The launch-gate probe as the verify play runs it, read straight from its linted file, against a stub gate on
# loopback. The stub derives from the suite's DrainingHandler, so it reads each request body before it answers.
# It answers 400 to the token t0k and 401 to any other request, and appends each Authorization header (NONE when
# absent) to seen.log. probe_expect <mode> <status> <output> [VAR=value...] runs the probe with only the given
# environment, reads the calling test's $probe, and checks the exit status and the one output line.
probe_expect() {
  run env -i PATH="$PATH" "${@:4}" python3 -I "$probe" "$1"
  [ "$status" -eq "$2" ]
  [ "$output" = "$3" ]
}

@test "skynet: the launch-gate probe sends the token only in caller mode, and reports env-missing and bad-mode without a request" {
  command -v python3 >/dev/null 2>&1 || skip "python3 not available"
  local probe="$REPO_ROOT/platform/playbooks/files/skynet-launch-gate-probe.py" stub="$BATS_TEST_TMPDIR/stub.py" url i
  cat > "$stub" <<'PY'
import http.server
import json
import os
import sys

from fake_http import DrainingHandler

out = sys.argv[1]


class Gate(DrainingHandler):
    def do_POST(self):
        auth = self.headers.get("Authorization")
        with open(os.path.join(out, "seen.log"), "a", encoding="utf-8") as fh:
            fh.write((auth if auth is not None else "NONE") + "\n")
        if auth == "Bearer t0k":
            status, payload = 400, {"error": "bad_request"}
        else:
            status, payload = 401, {"error": "unauthorized"}
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


server = http.server.HTTPServer(("127.0.0.1", 0), Gate)
tmp = os.path.join(out, "stub.port.tmp")
with open(tmp, "w", encoding="utf-8") as fh:
    fh.write(str(server.server_address[1]))
os.replace(tmp, os.path.join(out, "stub.port"))
server.serve_forever()
PY
  PYTHONPATH="$REPO_ROOT/platform/tests" python3 "$stub" "$BATS_TEST_TMPDIR" >/dev/null 2>&1 &
  echo $! > "$BATS_TEST_TMPDIR/stub.pid"
  for i in $(seq 1 100); do
    [ -s "$BATS_TEST_TMPDIR/stub.port" ] && break
    sleep 0.05
  done
  [ -s "$BATS_TEST_TMPDIR/stub.port" ]
  url="http://127.0.0.1:$(cat "$BATS_TEST_TMPDIR/stub.port")"
  # Anonymous mode sends no Authorization header, even with the token in its environment.
  probe_expect anon 0 '401 unauthorized' SKYNET_LAUNCH_GATE_URL="$url" SKYNET_LAUNCH_GATE_TOKEN=t0k
  # Caller mode sends the token and gets the gate's 400 for the empty body.
  probe_expect caller 0 '400 bad_request' SKYNET_LAUNCH_GATE_URL="$url" SKYNET_LAUNCH_GATE_TOKEN=t0k
  # Anonymous mode needs no token at all: with the URL alone it still gets its 401.
  probe_expect anon 0 '401 unauthorized' SKYNET_LAUNCH_GATE_URL="$url"
  # Caller mode without the token, and either mode without the URL, reports env-missing and sends nothing.
  probe_expect caller 0 'env-missing' SKYNET_LAUNCH_GATE_URL="$url"
  probe_expect anon 0 'env-missing' SKYNET_LAUNCH_GATE_TOKEN=t0k
  probe_expect caller 0 'env-missing' SKYNET_LAUNCH_GATE_TOKEN=t0k
  # Any other mode is refused with exit 2 before a request is sent.
  probe_expect x 2 'bad-mode' SKYNET_LAUNCH_GATE_URL="$url" SKYNET_LAUNCH_GATE_TOKEN=t0k
  # Exactly three requests reached the gate, in order: anonymous, caller, then anonymous with the URL alone.
  [ "$(cat "$BATS_TEST_TMPDIR/seen.log")" = "$(printf 'NONE\nBearer t0k\nNONE')" ]
}

# The verify report states the launch gate's answers only when the probes observed them. The Report task is
# rendered from the real playbook: one play per case, each named for its case, so the output splits by case.
@test "skynet: the verify report says the launch gate was not probed when the caller probe reported env-missing, and states its answers only otherwise" {
  command -v ansible-playbook >/dev/null 2>&1 || skip "ansible-playbook not available"
  python3 - "$PLAYBOOK" "$BATS_TEST_TMPDIR" <<'PY'
import sys
import yaml

src, out = sys.argv[1], sys.argv[2]
plays = yaml.safe_load(open(src, encoding="utf-8"))
verify = next(p for p in plays if p.get("name") == "Phase 3: Verify skynet")
report = next(t for t in verify["tasks"] if t.get("name") == "Report")
url = "https://launch-gate.example.test:8443"


def probe(line):
    return {"rc": 0, "stdout_lines": [line], "stderr_lines": []}


cases = {
    # The container has the URL but no token: the anonymous probe answers, the caller probe reports env-missing.
    "authed-missing": {"skynet_launch_gate_url": url, "_skynet_gate_anon": probe("401 unauthorized"),
                       "_skynet_gate_authed": probe("env-missing")},
    "answered": {"skynet_launch_gate_url": url, "_skynet_gate_anon": probe("401 unauthorized"),
                 "_skynet_gate_authed": probe("400 bad_request")},
    "undeclared": {"skynet_launch_gate_url": ""},
}
plays = [{"name": name, "hosts": "localhost", "connection": "local", "gather_facts": False,
          "vars": {"skynet_opa_url": "https://opa.example.test", **extra}, "tasks": [report]}
         for name, extra in cases.items()]
with open(f"{out}/report.yml", "w", encoding="utf-8") as fh:
    yaml.safe_dump(plays, fh, sort_keys=False, allow_unicode=True)
PY
  run ansible-playbook -i localhost, -c local "$BATS_TEST_TMPDIR/report.yml"
  [ "$status" -eq 0 ] || { echo "report did not render"; return 1; }
  # The lines of one case: those after its PLAY header, up to the next play or the recap.
  section() { awk -v h="PLAY [$1]" 'index($0, h) == 1 { on = 1; next } /^PLAY /{ on = 0 } on' <<<"$output"; }
  assert_contains "$(section authed-missing)" "The launch gate was not probed: skynet's running container"
  refute_contains "$(section authed-missing)" 'answered 401'
  assert_contains "$(section answered)" 'answered 401 to a request with no token and 400 to skynet'
  refute_contains "$(section answered)" 'The launch gate was not probed:'
  assert_contains "$(section undeclared)" 'is not declared'
  refute_contains "$(section undeclared)" 'The launch gate was not probed:'
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

@test "skynet: a container without the gate variables passes the gate asserts only under --check" {
  command -v ansible-playbook >/dev/null 2>&1 || skip "ansible-playbook not available"
  # A dry run does not rewrite .env or recreate the container, so the first gate-enabled --check meets a
  # container without SKYNET_LAUNCH_GATE_*. The probes print env-missing; only check mode may accept it.
  # One play per assert, so a regression in either one alone turns this red.
  python3 - "$PLAYBOOK" "$BATS_TEST_TMPDIR" <<'PY'
import sys, yaml
plays = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
names = {"anon": "Assert the launch gate refused a request with no token (401 unauthorized)",
         "authed": "Assert the launch gate accepted the caller token (400 bad_request)"}
fake = {"rc": 0, "stdout_lines": ["{{ probe_line }}"], "stderr_lines": []}
for key, name in names.items():
    tasks = [t for p in plays for t in p.get("tasks", []) if t.get("name") == name]
    assert len(tasks) == 1, name
    play = [{"hosts": "localhost", "gather_facts": False, "connection": "local",
             "vars": {"skynet_launch_gate_url": "https://launch-gate.example.test:8443",
                      "_skynet_gate_anon": fake, "_skynet_gate_authed": fake},
             "tasks": tasks}]
    yaml.safe_dump(play, open(f"{sys.argv[2]}/gate-assert-{key}.yml", "w"))
PY
  for key in anon authed; do
    run ansible-playbook -i localhost, "$BATS_TEST_TMPDIR/gate-assert-$key.yml" --check -e probe_line=env-missing
    [ "$status" -eq 0 ] || { echo "$key: env-missing refused under --check"; return 1; }
    run ansible-playbook -i localhost, "$BATS_TEST_TMPDIR/gate-assert-$key.yml" -e probe_line=env-missing
    [ "$status" -ne 0 ] || { echo "$key: env-missing accepted on a real run"; return 1; }
  done
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
import re
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
    if key == "skynet_launch_gate_url":
        # The INI renders its zone from _dev_zone, whose default bootstrap-local-dev.yml declares. Compare the
        # rendered value with the example's literal, substituting only that one variable.
        raw_text = open(sys.argv[2], encoding="utf-8").read()
        zone = re.search(r"_dev_zone: \"\{\{ dev_zone \| default\('([^']+)'\) \}\}\"", raw_text).group(1)
        rendered = ini[key].replace("{{ _dev_zone }}", zone)
        assert rendered == str(value), f"{key}: INI renders {rendered!r} != example {value!r}"
        continue
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

@test "skynet: verify proves skynet cannot write to OPA (PUT /v1/data -> 401), inside the container and after the decision check" {
  # OPA's authorization policy admits decisions and health only, so skynet's own transport must be
  # refused a write. The probe is a real PUT through urllib, the same trust path as the decision.
  python3 - "$PLAYBOOK" <<'PY'
import sys
import yaml

plays = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
verify = next(p for p in plays if str(p.get("name", "")).startswith("Phase 3"))
tasks = verify["tasks"]
names = [t.get("name", "") for t in tasks]
probe = next(t for t in tasks if t.get("name", "") == "Skynet cannot write to OPA (PUT /v1/data -> 401, inside the container)")
argv = probe["ansible.builtin.command"]["argv"]
code = argv[-1]
assert argv[1:3] == ["exec", "skynet"], argv
assert "os.environ['SKYNET_OPA_URL'] + '/v1/data/agentcloud_write_probe'" in code, code
assert "method='PUT'" in code, code
assert "print(urllib.request.urlopen(request, timeout=10).status)" in code, code
assert "except urllib.error.HTTPError as error:" in code, code
assert probe.get("check_mode") is False, probe
check = next(t for t in tasks if t.get("name") == "Assert skynet's OPA write was refused (401)")
that = check["ansible.builtin.assert"]["that"]
assert "_skynet_opa_write.stdout | trim == '401'" in that, that
decision = "OPA decision through skynet's own client (default deny, inside the container)"
decision_check = "Assert skynet reached OPA and was denied by default"
assert names.index(decision) < names.index(decision_check) < names.index(probe["name"]) < names.index(check["name"]) < names.index("Report"), names
assert names.index(check["name"]) < names.index("Record the service-deploy step result"), names
PY
}
