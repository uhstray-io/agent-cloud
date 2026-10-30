"""The Dispatch Console's AppRole, its deploy wiring and its monthly rotation (plan 17, D26-D27).

- console.hcl grants exactly D27: read on the console's own data paths, list and read on secret metadata.
- provision-console-approle.yml binds that policy through manage-approle.yml and overrides none of its limits.
- deploy-console.yml reads the AppRole only when console_openbao_url is set, and the Grafana token only when
  console_grafana_url is set. env.j2 renders each group only behind its switch.
- rotate-console-credentials.yml runs pin, mint, Grafana renewal, redeploy, then prove and retire. The retire
  comes after the prove. Every credential task is no_log, and a dry run skips every write.
- The rotation is a Dev-bound monthly schedule and the AppRole provision is a Dev variant.
"""

import ast
import json
import re
from pathlib import Path

import jinja2
import playbook_yaml
import yaml

REPO = Path(__file__).resolve().parents[2]
POLICIES = REPO / "platform/services/openbao/deployment/config/policies"
PLAYBOOKS = REPO / "platform/playbooks"
CONSOLE = REPO / "platform/services/console/deployment"
TEMPLATES = REPO / "platform/semaphore/templates.yml"
PROVISION = PLAYBOOKS / "provision-console-approle.yml"
ROTATE = PLAYBOOKS / "rotate-console-credentials.yml"
DEPLOY = PLAYBOOKS / "deploy-console.yml"
ENV_TEMPLATE = CONSOLE / "templates/env.j2"

PHASE1 = "Phase 1: Place repo + manage console secrets and config"
PIN = "Pin the console's deployed commit (no code change)"
MINT = "Create a new secret_id for the console AppRole"
PROVE = "Prove the new secret_id, then retire the replaced one"
RETIRE = "Retire the replaced secret_id (destroy it on the role)"
PROOF = "Prove the stored secret_id logs in (200)"
PROOF_CHECK = "Require the stored secret_id to be the one minted this run, and to log in"

# A credential-bearing task: its text names a secret_id, role_id, token header or a registered credential result.
CREDENTIAL = re.compile(r"secret_id|role_id|client_token|X-Vault-Token|_bm_token|BAO_SECRET_ID|bao_secret_id|_rot_")
# Each path block of an HCL policy: `path "..." { capabilities = [...] }`.
PATH_BLOCK = re.compile(r'path\s+"([^"]+)"\s*\{\s*capabilities\s*=\s*\[([^\]]*)\]')

ENV_SECRETS = {
    "console_oidc_client_secret": "oidc-value",
    "semaphore_api_token": "semaphore-value",
    "console_api_token": "netbox-value",
    "console_token_id": "proxmox-id-value",
    "console_token": "proxmox-value",
    "role_id": "ROLE-ID-VALUE",
    "secret_id": "SECRET-ID-VALUE",
    "console_grafana_token": "GRAFANA-TOKEN-VALUE",
}
OPENBAO_URL = "https://openbao.agent-cloud.test"
GRAFANA_URL = "https://grafana.agent-cloud.test"


def _blocks(text: str) -> dict:
    return {path: re.findall(r'"([^"]+)"', caps) for path, caps in PATH_BLOCK.findall(text)}


def _play(name: str) -> dict:
    return next(p for p in playbook_yaml.plays(ROTATE) if p.get("name") == name)


def _labels() -> list:
    # The whole document, guard import included: an import has no name, so its target stands for it.
    return [e["ansible.builtin.import_playbook"] if "ansible.builtin.import_playbook" in e else e["name"]
            for e in playbook_yaml.load(ROTATE)]


def _phase1_vars() -> dict:
    return next(p for p in playbook_yaml.plays(DEPLOY) if p.get("name") == PHASE1)["vars"]


def _render_expr(var: str, variables: dict):
    return ast.literal_eval(jinja2.Environment().from_string(_phase1_vars()[var]).render(**variables))


def _env(variables: dict) -> str:
    # Ansible renders templates with trim_blocks on (its Templar default), so the same settings are used here.
    env = jinja2.Environment(trim_blocks=True, keep_trailing_newline=True)
    return env.from_string(ENV_TEMPLATE.read_text()).render(secrets=ENV_SECRETS, **variables)


def _template(name: str) -> dict:
    found = [t for t in yaml.safe_load(TEMPLATES.read_text())["templates"] if t.get("name") == name]
    assert len(found) == 1, (name, len(found))
    return found[0]


def _when(task: dict) -> str:
    when = task.get("when", [])
    return " ".join(when) if isinstance(when, list) else str(when)


def _tasks_of(play: dict) -> list:
    return play["tasks"]


# --- the policy: exactly D27 ----------------------------------------------------------------------------------

def test_console_policy_grants_exactly_the_d27_paths_and_nothing_else():
    text = (POLICIES / "console.hcl").read_text()
    assert _blocks(text) == {
        "secret/data/services/console/*": ["read"],
        "secret/metadata/*": ["list", "read"],
    }
    assert text.count('path "') == 2, "a third path block would be a grant beyond D27"


def test_console_policy_is_listed_in_the_policies_readme():
    readme = (POLICIES / "README.md").read_text()
    assert "`console.hcl`" in readme


# --- provisioning: the console policy through manage-approle --------------------------------------------------

def test_provision_binds_console_hcl_through_manage_approle_with_the_defaults():
    play = playbook_yaml.plays(PROVISION)[0]
    assert play["vars"]["_approle_name"] == "console"
    assert "policies/console.hcl" in play["vars"]["_approle_policy"]
    includes = [t["ansible.builtin.include_tasks"] for t in play["tasks"] if "ansible.builtin.include_tasks" in t]
    # The transport guard runs before the admin login; manage-approle.yml is the only AppRole writer.
    assert includes.count("tasks/manage-approle.yml") == 1, includes
    assert includes[0] == "tasks/assert-bao-transport.yml" and includes[-1] == "tasks/manage-approle.yml", includes
    # manage-approle.yml's limits are the defaults: the provision must not override any of them.
    overrides = {k for k in play["vars"] if k.startswith("_approle_") and k not in ("_approle_name", "_approle_policy")}
    assert overrides == set(), overrides


def test_the_policy_file_name_is_the_approle_name():
    # manage-approle.yml binds the policy under the AppRole's name, and apply-openbao-policies.yml names it after the file.
    assert (POLICIES / "console.hcl").stem == playbook_yaml.plays(PROVISION)[0]["vars"]["_approle_name"]


# --- deploy: the two gated shared reads ----------------------------------------------------------------------

def test_the_console_approle_is_read_only_when_its_url_is_set():
    assert _render_expr("_console_openbao_reads", {}) == []
    assert _render_expr("_console_openbao_reads", {"console_openbao_url": ""}) == []
    assert _render_expr("_console_openbao_reads", {"console_openbao_url": OPENBAO_URL}) == [
        {"from_service": "approles/console", "read_keys": ["role_id", "secret_id"]}]


def test_the_grafana_token_is_read_only_when_its_url_is_set():
    assert _render_expr("_console_grafana_reads", {}) == []
    assert _render_expr("_console_grafana_reads", {"console_grafana_url": GRAFANA_URL}) == [
        {"from_service": "o11y", "read_keys": ["console_grafana_token"]}]


def test_both_new_reads_are_appended_to_shared_reads():
    shared = _phase1_vars()["_shared_reads"]
    assert "_console_openbao_reads" in shared and "_console_grafana_reads" in shared


def test_shared_keys_stay_unique_with_every_gated_read_on():
    urls = {"console_netbox_url": "https://netbox.agent-cloud.test", "console_proxmox_url": "https://pve.agent-cloud.test:8006",
            "console_openbao_url": OPENBAO_URL, "console_grafana_url": GRAFANA_URL}
    reads = list(_phase1_vars()["_console_oidc_reads"])
    for var in ("_console_netbox_reads", "_console_proxmox_reads", "_console_openbao_reads", "_console_grafana_reads"):
        reads += _render_expr(var, urls)
    keys = [k for r in reads for k in r["read_keys"]]
    assert len(keys) == len(set(keys)), keys
    assert {"role_id", "secret_id", "console_grafana_token"} <= set(keys)
    # A shared key may not shadow the console's own local secret (manage-secrets refuses that by name).
    assert "semaphore_api_token" not in keys


# --- env.j2: the rendered groups follow their switches --------------------------------------------------------

def test_env_renders_no_openbao_or_grafana_token_group_when_no_url_is_set():
    text = _env({})
    for needle in ("CONSOLE_OPENBAO_", "CONSOLE_GRAFANA_TOKEN", "CONSOLE_GRAFANA_URL", "ROLE-ID-VALUE", "SECRET-ID-VALUE",
                   "GRAFANA-TOKEN-VALUE"):
        assert needle not in text, needle


def test_env_renders_the_openbao_group_and_the_grafana_token_when_both_urls_are_set():
    lines = set(_env({"console_openbao_url": OPENBAO_URL, "console_grafana_url": GRAFANA_URL}).splitlines())
    assert f"CONSOLE_OPENBAO_URL={OPENBAO_URL}" in lines
    assert "CONSOLE_OPENBAO_ROLE_ID=ROLE-ID-VALUE" in lines
    assert "CONSOLE_OPENBAO_SECRET_ID=SECRET-ID-VALUE" in lines
    assert f"CONSOLE_GRAFANA_URL={GRAFANA_URL}" in lines
    assert "CONSOLE_GRAFANA_TOKEN=GRAFANA-TOKEN-VALUE" in lines


def test_env_renders_each_group_only_behind_its_own_switch():
    only_openbao = _env({"console_openbao_url": OPENBAO_URL})
    assert "CONSOLE_OPENBAO_SECRET_ID=SECRET-ID-VALUE" in only_openbao
    assert "CONSOLE_GRAFANA_TOKEN" not in only_openbao
    only_grafana = _env({"console_grafana_url": GRAFANA_URL})
    assert "CONSOLE_GRAFANA_TOKEN=GRAFANA-TOKEN-VALUE" in only_grafana
    assert "CONSOLE_OPENBAO_" not in only_grafana


# --- rotation: order, guards and secrets ---------------------------------------------------------------------

def test_rotation_runs_pin_mint_grafana_redeploy_then_prove_and_retire():
    labels = _labels()
    order = ["refuse-internal-extra-vars.yml", "preflight-target-group.yml", PIN, MINT,
             "provision-console-grafana-token.yml", "deploy-console.yml", PROVE]
    positions = [labels.index(step) for step in order]
    assert positions == sorted(positions) and len(set(positions)) == len(positions), labels


def test_the_replaced_secret_id_is_destroyed_only_after_the_new_one_is_proven():
    tasks = _tasks_of(_play(PROVE))
    names = [t.get("name") for t in tasks]
    proof, check, retire = names.index(PROOF), names.index(PROOF_CHECK), names.index(RETIRE)
    assert proof < check < retire, names
    destroy = tasks[retire]
    assert destroy["ansible.builtin.uri"]["url"] == \
        "{{ _bao_url }}/v1/auth/approle/role/{{ _console_role }}/secret-id/destroy"
    assert destroy["ansible.builtin.uri"]["body"] == {"secret_id": "{{ _console_retired_secret_id }}"}
    assert "not ansible_check_mode" in _when(destroy) and "_rot_new is not skipped" in _when(destroy)


def test_the_pin_is_skipped_for_an_explicit_branch_and_for_local_mode():
    pins = [t for t in _tasks_of(_play(PIN)) if "when" in t]
    assert pins, "the pin play has no guarded tasks"
    for task in pins:
        assert "service_branch is defined" in _when(task) and "local_mode" in _when(task), task["name"]


def test_the_pin_reaches_the_deploy_through_an_internal_name_only():
    # A computed value needs an underscore name (test_executor_internal_overrides.py), so the pin is internal and
    # deploy-console's _branch reads it before the survey's service_branch.
    pin = next(t for t in _tasks_of(_play(PIN)) if "ansible.builtin.set_fact" in t)
    assert set(pin["ansible.builtin.set_fact"]) == {"_console_pinned_branch"}
    assert _phase1_vars()["_branch"].startswith("{{ _console_pinned_branch | default(service_branch")


def test_the_mid_run_imports_pass_the_nested_flag():
    # A guarded playbook imported after the first entry must pass the nested flag (the nested-import rule).
    flagged = {e["ansible.builtin.import_playbook"]: (e.get("vars") or {}).get("_extra_var_guard_nested")
               for e in playbook_yaml.load(ROTATE) if "ansible.builtin.import_playbook" in e}
    assert flagged["provision-console-grafana-token.yml"] is True, flagged
    assert flagged["deploy-console.yml"] is True, flagged


def test_the_pin_accepts_only_a_full_commit_sha():
    text = json.dumps(_play(PIN))
    assert "^[0-9a-f]{40}$" in text


def test_every_credential_task_is_no_log_and_no_debug_prints_a_registered_value():
    checked = 0
    for play in playbook_yaml.plays(ROTATE):
        for task in play.get("tasks", []):
            if "ansible.builtin.debug" in task:
                msg = json.dumps(task["ansible.builtin.debug"])
                assert not re.search(r"\{\{[^}]*(_rot_|_console_retired)", msg), task.get("name")
                continue
            body = {k: v for k, v in task.items() if k != "name"}
            if CREDENTIAL.search(json.dumps(body)):
                checked += 1
                assert task.get("no_log") is True, task.get("name")
    assert checked >= 9, checked


def test_every_post_is_a_write_guarded_by_check_mode_or_a_login_that_skips_under_it():
    for play in playbook_yaml.plays(ROTATE):
        for task in play.get("tasks", []):
            uri = task.get("ansible.builtin.uri") or {}
            if uri.get("method") == "POST":
                guarded = "not ansible_check_mode" in _when(task)
                assert guarded or task.get("check_mode") is False, task.get("name")


def test_the_mint_and_the_store_are_skipped_under_check_mode():
    mint = next(t for t in _tasks_of(_play(MINT)) if t.get("name") == "Mint a new secret_id (the role_id does not change)")
    assert "not ansible_check_mode" in _when(mint)
    store = next(t for t in _tasks_of(_play(MINT)) if str(t.get("name", "")).startswith("Store the new secret_id"))
    assert "not ansible_check_mode" in _when(store)


def test_the_store_is_guarded_by_the_replaced_secret_id():
    store = next(t for t in _tasks_of(_play(MINT)) if str(t.get("name", "")).startswith("Store the new secret_id"))
    vars_ = store["vars"]
    assert vars_["_bm_guard_key"] == "secret_id"
    assert vars_["_bm_guard_value"] == "{{ _console_retired_secret_id }}"
    assert vars_["_bm_path"] == "{{ _console_approle_path }}"


# --- templates.yml: the AppRole variant and the Dev-bound monthly rotation -------------------------------------

def test_the_rotation_is_dev_bound_monthly_with_no_survey():
    entry = _template("Rotate Console Credentials (Dev)")
    assert entry["playbook"] == "platform/playbooks/rotate-console-credentials.yml"
    assert entry["repository"] == "agent-cloud dev"
    assert entry["schedule"] == {"cron": "0 7 1 * *"}
    assert "survey_vars" not in entry and "dev_variant" not in entry


def test_the_console_approle_provision_is_a_dev_variant_without_a_schedule():
    entry = _template("Provision Console AppRole")
    assert entry["playbook"] == "platform/playbooks/provision-console-approle.yml"
    assert entry["dev_variant"] is True
    assert "schedule" not in entry
