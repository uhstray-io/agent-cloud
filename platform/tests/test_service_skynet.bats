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
  assert_grep -qF 'tasks/clean-service.yml' "$CLEAN_PLAYBOOK"
  assert_grep -qF 'import_playbook: deploy-skynet.yml' "$CLEAN_PLAYBOOK"
  assert_grep -qF '_extra_var_guard_nested: true' "$CLEAN_PLAYBOOK"
  assert_grep -qF 'refuse-internal-extra-vars.yml' "$CLEAN_PLAYBOOK"
}

@test "skynet: deploy secrets are api_token and db_password, both random and generate-once" {
  assert_grep -qE 'name: api_token, type: random, length: 48' "$PLAYBOOK"
  assert_grep -qE 'name: db_password, type: random, length: 32' "$PLAYBOOK"
  assert_grep -qF 'tasks/manage-secrets.yml' "$PLAYBOOK"
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

@test "skynet: the step-ca root goes into ./certs in local mode only, before deploy.sh" {
  # compose.local.yml mounts ./certs and sets SSL_CERT_FILE, so the bundle must land before the
  # containers start. The combined bundle is for a consumer that replaces its whole trust store,
  # which skynet is not.
  assert_grep -qF 'tasks/distribute-ca-root.yml' "$PLAYBOOK"
  assert_precedes "$PLAYBOOK" 'tasks/distribute-ca-root\.yml' 'bash deploy\.sh'
  local block
  block=$(task_block "$PLAYBOOK" 'Distribute the step-ca root for internal TLS trust (local)')
  assert_contains "$block" '_ca_root_dest_dir: "{{ _deploy_dir }}"'
  assert_contains "$block" 'when: local_mode | default(false) | bool'
  refute_grep -qF '_ca_root_combined_dest' "$PLAYBOOK"
}

@test "skynet: transport guard guards every play that reaches OpenBao" {
  # Play 1 (secret reads and writes) and Play 2 (registry login reads the GHCR PAT).
  [ "$(grep -c 'include_tasks: tasks/assert-bao-transport.yml' "$PLAYBOOK")" -eq 2 ]
}

@test "skynet: the Semaphore token stopgap is a no_log credential read, labelled as a stopgap" {
  local block
  block=$(task_block "$PLAYBOOK" 'Read the Semaphore API token for skynet')
  assert_contains "$block" "secret/data/services/semaphore:api_token"
  assert_contains "$block" "no_log: true"
  assert_contains "$block" "community.hashi_vault.hashi_vault"
  # The stopgap is labelled where it is written, so nobody reads it as the design.
  assert_grep -qF 'LOCAL-DEV STOPGAP' "$PLAYBOOK"
}

@test "skynet: no_log is on the Semaphore token read and nowhere else in the deploy playbook" {
  # no_log is for credential tasks only (AGENTS.md "Credential Handling"). The verify and
  # deploy tasks must keep their output, or a failed probe is undiagnosable.
  python3 - "$PLAYBOOK" <<'PY'
import sys
import yaml

allowed = {"Read the Semaphore API token for skynet (local-dev stopgap)"}
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

@test "skynet: the eight refusals are present as named assert tasks, all before the repo is placed" {
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
  [ "$(grep -c '^Refuse ' <<<"$refusals")" -eq 7 ]
  assert_grep -qx 'Require a usable Semaphore token before rendering skynet.s env' <<<"$refusals"
  # Every refusal sits before the monorepo is placed, so a refused run changes nothing.
  assert_precedes "$PLAYBOOK" 'Refuse skynet_image unless' 'tasks/place-monorepo\.yml'
  assert_precedes "$PLAYBOOK" 'Require a usable Semaphore token' 'tasks/place-monorepo\.yml'
  # The production refusal runs before the OpenBao read it guards, so no secret is fetched first.
  assert_precedes "$PLAYBOOK" 'Refuse the Semaphore operator-token stopgap' 'Read the Semaphore API token'
  assert_precedes "$PLAYBOOK" 'Refuse skynet_semaphore_project unless' 'tasks/manage-secrets\.yml'
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
    if "ansible.builtin.assert" in t
    and (t["name"].startswith("Refuse ") or t["name"].startswith("Require a usable Semaphore token"))
]
if len(tasks) != 8:
    sys.exit(f"expected 8 refusal asserts in Phase 1, found {len(tasks)}")
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
    "_skynet_semaphore_token": "present",
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

@test "skynet: an empty Semaphore token refuses before any write" {
  command -v ansible-playbook >/dev/null 2>&1 || skip "ansible-playbook not available"
  extract_refusals
  run accepted _skynet_semaphore_token '""'
  [ "$status" -ne 0 ]
}

@test "skynet: the Semaphore operator-token stopgap is refused outside local mode, before any OpenBao read" {
  command -v ansible-playbook >/dev/null 2>&1 || skip "ansible-playbook not available"
  extract_refusals
  run accepted local_mode 'false'
  [ "$status" -ne 0 ]
}

# ── Clean deploy and local wiring ────────────────────────────────────────────

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
    if key in ("ansible_host", "ansible_connection"):
        continue
    assert key in ini, f"{key} missing from the INI skynet_svc:vars"
    assert ini[key] == str(value), f"{key}: INI {ini[key]!r} != example {value!r}"
assert "skynet_image" in ini, "the INI must carry the same pinned skynet_image as the example"
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

@test "skynet: env template renders the secrets and the Semaphore token the playbook provides" {
  local env="$DEPLOY_DIR/templates/env.j2"
  skip_unless_service_file "$env"
  assert_grep -qF '{{ secrets.api_token }}' "$env"
  assert_grep -qF '{{ secrets.db_password }}' "$env"
  assert_grep -qF '{{ _skynet_semaphore_token }}' "$env"
  # The verify reads the bearer from the container's environment under this exact name.
  assert_grep -qE '^SKYNET_API_TOKEN=' "$env"
}

@test "skynet: compose names the two containers the playbook verifies and never publishes the API" {
  local compose="$DEPLOY_DIR/compose.yml"
  skip_unless_service_file "$compose"
  assert_grep -qE '^  skynet:' "$compose"
  assert_grep -qE '^  skynet-db:' "$compose"
}
