"""The Dispatch Console's AppRole, its deploy wiring and its monthly rotation (plan 17, D26-D27).

- console.hcl grants exactly D27: read on the console's own data paths, list and read on secret metadata.
- provision-console-approle.yml binds that policy through manage-approle.yml and overrides none of its limits.
- deploy-console.yml reads the AppRole only when console_openbao_url is set, and the Grafana token only when
  console_grafana_api_enabled AND console_grafana_url are both set. env.j2 renders each group only behind its switch.
- rotate-console-credentials.yml runs pin, drift check, mint, Grafana renewal, redeploy, then prove and retire.
  Every refusal that can happen before the mint comes before it. The retire comes after the prove. Every
  credential task is no_log, and a dry run skips every write.
- The rotation is a Dev-bound monthly schedule and the AppRole provision is a Dev variant.
"""

import ast
import json
import os
import re
import subprocess
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
DRIFT = "Refuse a rotation the controller cannot reproduce from the pinned commit"
MINT = "Create a new secret_id for the console AppRole"
PROVE = "Prove the new secret_id, then retire the replaced one"
RETIRE = "Retire the replaced secret_id (destroy it on the role)"
PROOF = "Prove the stored secret_id logs in (200)"
PROOF_CHECK = "Require the stored secret_id to be the one minted this run, and to log in"
STORE_INCLUDE = "tasks/bao-merge-keys.yml"
# The paths whose committed content the pinned commit must still match on the controller (rotate playbook).
CONSOLE_INPUTS = [
    "platform/services/console/deployment",
    "platform/playbooks/deploy-console.yml",
    "platform/playbooks/tasks/manage-secrets.yml",
    "platform/workflows/service-onboarding/registry.yml",
    "platform/services/authentik/deployment/app-catalog.yml",
    "platform/playbooks/tasks/place-monorepo.yml",
    "platform/playbooks/tasks/distribute-ca-root.yml",
]

# A credential-bearing task: its text names a secret_id, role_id, token header or a registered credential result.
CREDENTIAL = re.compile(r"secret_id|role_id|client_token|X-Vault-Token|_bm_token|BAO_SECRET_ID|bao_secret_id|_rot_")
# A message a task prints (assert, fail, debug) is prose; only a template inside it could carry a value.
MESSAGE_KEYS = ("msg", "fail_msg")
# A message template that interpolates a credential fact or a registered credential result.
MESSAGE_LEAK = re.compile(r"\{\{[^}]*(_rot_|_console_retired|_console_store_holds)")
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
    return [
        e["ansible.builtin.import_playbook"] if "ansible.builtin.import_playbook" in e else e["name"]
        for e in playbook_yaml.load(ROTATE)
    ]


def _imports() -> list:
    return [e for e in playbook_yaml.load(ROTATE) if "ansible.builtin.import_playbook" in e]


def _phase1_vars() -> dict:
    return next(p for p in playbook_yaml.plays(DEPLOY) if p.get("name") == PHASE1)["vars"]


_TRUE = {"y", "yes", "on", "1", "true", "t", "1.0"}
_FALSE = {"n", "no", "off", "0", "false", "f", "0.0", ""}


def _to_bool(value) -> bool:
    # Ansible's `bool` filter (ansible.utils.vars.boolean): plain Jinja2 has none, so the renderers below add it.
    if isinstance(value, bool):
        return value
    text = str(value).lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ValueError(f"not a boolean: {value!r}")


def _jinja(**options) -> jinja2.Environment:
    env = jinja2.Environment(**options)
    env.filters["bool"] = _to_bool
    return env


def _render_expr(var: str, variables: dict):
    return ast.literal_eval(_jinja().from_string(_phase1_vars()[var]).render(**variables))


def _render_bool(expr: str, variables: dict) -> str:
    return _jinja().from_string("{{ " + " ".join(expr.split()) + " }}").render(**variables)


def _env(variables: dict) -> str:
    # Ansible renders templates with trim_blocks on (its Templar default), so the same settings are used here.
    env = _jinja(trim_blocks=True, keep_trailing_newline=True)
    return env.from_string(ENV_TEMPLATE.read_text()).render(secrets=ENV_SECRETS, **variables)


def _template(name: str) -> dict:
    found = [t for t in yaml.safe_load(TEMPLATES.read_text())["templates"] if t.get("name") == name]
    assert len(found) == 1, (name, len(found))
    return found[0]


def _when(task: dict) -> str:
    when = task.get("when", [])
    return " ".join(when) if isinstance(when, list) else str(when)


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _leaves(tasks, inherited=()):
    """Every task that runs, paired with the `when` it runs under: its own, then each enclosing block's."""
    for task in tasks:
        scope = inherited + tuple(_as_list(task.get("when")))
        if "block" in task:
            for part in ("block", "rescue", "always"):
                yield from _leaves(task.get(part, []), scope)
        else:
            yield task, " ".join(scope)


def _find_leaf(play: dict, predicate):
    hits = [(task, when) for task, when in _leaves(play["tasks"]) if predicate(task)]
    assert len(hits) == 1, [t.get("name") for t, _ in hits]
    return hits[0]


def _checked(task: dict) -> dict:
    """The part of a task a no_log decision turns on: its name and its prose messages left out."""
    out = {}
    for key, value in task.items():
        if key == "name":
            continue
        if isinstance(value, dict):
            value = {k: v for k, v in value.items() if k not in MESSAGE_KEYS}
        out[key] = value
    return out


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
    # manage-approle.yml binds the policy under the AppRole's name, and apply-openbao-policies.yml names it
    # after the file.
    assert (POLICIES / "console.hcl").stem == playbook_yaml.plays(PROVISION)[0]["vars"]["_approle_name"]


# --- deploy: the two gated shared reads ----------------------------------------------------------------------


def test_the_console_approle_is_read_only_when_its_url_is_set():
    assert _render_expr("_console_openbao_reads", {}) == []
    assert _render_expr("_console_openbao_reads", {"console_openbao_url": ""}) == []
    assert _render_expr("_console_openbao_reads", {"console_openbao_url": OPENBAO_URL}) == [
        {"from_service": "approles/console", "read_keys": ["role_id", "secret_id"]}
    ]


def test_the_grafana_token_is_read_only_when_the_api_switch_and_its_url_are_both_set():
    assert _render_expr("_console_grafana_reads", {}) == []
    # The deep-link URL alone reads nothing: it is not a credential and needs no token.
    assert _render_expr("_console_grafana_reads", {"console_grafana_url": GRAFANA_URL}) == []
    assert _render_expr("_console_grafana_reads", {"console_grafana_api_enabled": True}) == []
    assert _render_expr(
        "_console_grafana_reads", {"console_grafana_api_enabled": True, "console_grafana_url": GRAFANA_URL}
    ) == [{"from_service": "o11y", "read_keys": ["console_grafana_token"]}]


def test_both_new_reads_are_appended_to_shared_reads():
    shared = _phase1_vars()["_shared_reads"]
    assert "_console_openbao_reads" in shared and "_console_grafana_reads" in shared


def test_shared_keys_stay_unique_with_every_gated_read_on():
    urls = {
        "console_netbox_url": "https://netbox.agent-cloud.test",
        "console_proxmox_url": "https://pve.agent-cloud.test:8006",
        "console_openbao_url": OPENBAO_URL,
        "console_grafana_url": GRAFANA_URL,
        "console_grafana_api_enabled": True,
    }
    reads = list(_phase1_vars()["_console_oidc_reads"])
    for var in ("_console_netbox_reads", "_console_proxmox_reads", "_console_openbao_reads", "_console_grafana_reads"):
        reads += _render_expr(var, urls)
    keys = [k for r in reads for k in r["read_keys"]]
    assert len(keys) == len(set(keys)), keys
    assert {"role_id", "secret_id", "console_grafana_token"} <= set(keys)
    # A shared key may not shadow the console's own local secret (manage-secrets refuses that by name).
    assert "semaphore_api_token" not in keys


def test_deploy_refuses_the_api_switch_without_its_url_by_name():
    phase1 = next(p for p in playbook_yaml.plays(DEPLOY) if p.get("name") == PHASE1)
    tasks = [
        t for t in phase1["tasks"] if t.get("name") == "Refuse console_grafana_api_enabled without console_grafana_url"
    ]
    assert len(tasks) == 1, len(tasks)
    expr = tasks[0]["ansible.builtin.assert"]["that"][0]
    assert _render_bool(expr, {"console_grafana_api_enabled": True}) == "False"
    assert _render_bool(expr, {"console_grafana_api_enabled": True, "console_grafana_url": GRAFANA_URL}) == "True"
    assert _render_bool(expr, {"console_grafana_url": GRAFANA_URL}) == "True"
    assert _render_bool(expr, {}) == "True"


# --- env.j2: the rendered groups follow their switches --------------------------------------------------------


def test_env_renders_no_openbao_or_grafana_token_group_when_no_url_is_set():
    text = _env({})
    for needle in (
        "CONSOLE_OPENBAO_",
        "CONSOLE_GRAFANA_TOKEN",
        "CONSOLE_GRAFANA_URL",
        "ROLE-ID-VALUE",
        "SECRET-ID-VALUE",
        "GRAFANA-TOKEN-VALUE",
    ):
        assert needle not in text, needle


def test_env_renders_the_grafana_url_alone_as_a_deep_link_and_no_token():
    lines = set(_env({"console_grafana_url": GRAFANA_URL}).splitlines())
    assert f"CONSOLE_GRAFANA_URL={GRAFANA_URL}" in lines
    assert "CONSOLE_GRAFANA_TOKEN=GRAFANA-TOKEN-VALUE" not in lines
    assert not any(line.startswith("CONSOLE_GRAFANA_TOKEN") for line in lines)


def test_env_renders_the_openbao_group_and_the_grafana_token_when_the_switch_and_url_are_set():
    lines = set(
        _env(
            {
                "console_openbao_url": OPENBAO_URL,
                "console_grafana_url": GRAFANA_URL,
                "console_grafana_api_enabled": True,
            }
        ).splitlines()
    )
    assert f"CONSOLE_OPENBAO_URL={OPENBAO_URL}" in lines
    assert "CONSOLE_OPENBAO_ROLE_ID=ROLE-ID-VALUE" in lines
    assert "CONSOLE_OPENBAO_SECRET_ID=SECRET-ID-VALUE" in lines
    assert f"CONSOLE_GRAFANA_URL={GRAFANA_URL}" in lines
    assert "CONSOLE_GRAFANA_TOKEN=GRAFANA-TOKEN-VALUE" in lines


def test_env_renders_each_group_only_behind_its_own_switch():
    only_openbao = _env({"console_openbao_url": OPENBAO_URL})
    assert "CONSOLE_OPENBAO_SECRET_ID=SECRET-ID-VALUE" in only_openbao
    assert "CONSOLE_GRAFANA_TOKEN" not in only_openbao
    # The switch without its URL renders no token either: the token needs both.
    switch_only = _env({"console_grafana_api_enabled": True})
    assert "CONSOLE_GRAFANA_TOKEN" not in switch_only and "CONSOLE_GRAFANA_URL" not in switch_only
    assert "CONSOLE_OPENBAO_" not in switch_only
    both = _env({"console_grafana_url": GRAFANA_URL, "console_grafana_api_enabled": True})
    assert "CONSOLE_GRAFANA_TOKEN=GRAFANA-TOKEN-VALUE" in both.splitlines()
    assert "CONSOLE_OPENBAO_" not in both


# --- rotation: order, guards and secrets ---------------------------------------------------------------------


def test_rotation_runs_pin_drift_mint_grafana_redeploy_then_prove_and_retire():
    labels = _labels()
    order = [
        "refuse-internal-extra-vars.yml",
        "preflight-target-group.yml",
        PIN,
        DRIFT,
        MINT,
        "provision-console-grafana-token.yml",
        "deploy-console.yml",
        PROVE,
    ]
    positions = [labels.index(step) for step in order]
    assert positions == sorted(positions) and len(set(positions)) == len(positions), labels


def test_every_refusal_that_can_happen_before_a_mint_comes_before_the_first_mint():
    labels = _labels()
    mint = labels.index(MINT)
    # Both preflights (console_svc and the gated o11y_svc) and the pin/drift guards precede the mint.
    preflights = [i for i, label in enumerate(labels) if label == "preflight-target-group.yml"]
    assert len(preflights) == 2 and all(i < mint for i in preflights), labels
    assert labels.index(PIN) < labels.index(DRIFT) < mint, labels


def test_the_o11y_preflight_and_the_grafana_renewal_are_gated_on_the_api_switch():
    gated = [e for e in _imports() if "console_grafana_api_enabled" in str(e.get("when", ""))]
    targets = sorted(e["ansible.builtin.import_playbook"] for e in gated)
    assert targets == ["preflight-target-group.yml", "provision-console-grafana-token.yml"], targets
    o11y = next(e for e in gated if e["ansible.builtin.import_playbook"] == "preflight-target-group.yml")
    assert o11y["vars"] == {"preflight_group": "o11y_svc", "preflight_group_expected": "o11y_svc"}


def test_the_pin_play_refuses_the_api_switch_without_its_url_before_anything_is_minted():
    checks = [
        t
        for t in _tasks_of(_play(PIN))
        if "ansible.builtin.assert" in t
        and "console_grafana_api_enabled" in json.dumps(t["ansible.builtin.assert"]["that"])
    ]
    assert len(checks) == 1, checks
    expr = checks[0]["ansible.builtin.assert"]["that"][0]
    assert _render_bool(expr, {"console_grafana_api_enabled": True}) == "False"
    assert _render_bool(expr, {"console_grafana_api_enabled": True, "console_grafana_url": GRAFANA_URL}) == "True"


def test_the_replaced_secret_id_is_destroyed_only_after_the_new_one_is_proven():
    tasks = _tasks_of(_play(PROVE))
    names = [t.get("name") for t in tasks]
    proof, check, retire = names.index(PROOF), names.index(PROOF_CHECK), names.index(RETIRE)
    assert proof < check < retire, names
    destroy = tasks[retire]
    assert (
        destroy["ansible.builtin.uri"]["url"]
        == "{{ _bao_url }}/v1/auth/approle/role/{{ _console_role }}/secret-id/destroy"
    )
    assert destroy["ansible.builtin.uri"]["body"] == {"secret_id": "{{ _console_retired_secret_id }}"}
    assert "not ansible_check_mode" in _when(destroy) and "_rot_new is not skipped" in _when(destroy)


def test_the_proof_token_is_revoked_right_after_the_proof_login_and_before_the_retire():
    tasks = _tasks_of(_play(PROVE))
    names = [t.get("name") for t in tasks]
    revoke = next(
        t
        for t in tasks
        if str((t.get("ansible.builtin.uri") or {}).get("url", "")).endswith("/v1/auth/token/revoke-self")
    )
    assert names.index(PROOF) < tasks.index(revoke) < names.index(PROOF_CHECK) < names.index(RETIRE), names
    assert revoke["ansible.builtin.uri"]["method"] == "POST"
    assert revoke["ansible.builtin.uri"]["headers"]["X-Vault-Token"] == "{{ _rot_proof.json.auth.client_token }}"
    assert revoke.get("no_log") is True
    assert "not ansible_check_mode" in _when(revoke)


def test_a_refused_store_write_destroys_the_minted_secret_id_only_when_the_store_does_not_hold_it():
    block = next(t for t in _tasks_of(_play(MINT)) if "block" in t)
    rescue = block["rescue"]
    names = [t.get("name") for t in rescue]
    read_back = names.index(next(n for n in names if n.startswith("Read back what the store holds")))
    decide = names.index(next(n for n in names if n.startswith("Decide whether the store holds")))
    destroy_task = next(
        t for t in rescue if str((t.get("ansible.builtin.uri") or {}).get("url", "")).endswith("/secret-id/destroy")
    )
    destroy = rescue.index(destroy_task)
    assert read_back < decide < destroy, names
    assert destroy_task["ansible.builtin.uri"]["body"] == {"secret_id": "{{ _rot_new.json.data.secret_id }}"}
    assert destroy_task.get("no_log") is True
    assert _when(destroy_task) == "not _console_store_holds_new | bool"
    # Both outcomes end in a named refusal: the store took it, or it did not. Neither message names a value.
    fails = [t for t in rescue if "ansible.builtin.fail" in t]
    assert len(fails) == 2, names
    assert {_when(t) for t in fails} == {"not _console_store_holds_new | bool", "_console_store_holds_new | bool"}


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
    flagged = {
        e["ansible.builtin.import_playbook"]: (e.get("vars") or {}).get("_extra_var_guard_nested") for e in _imports()
    }
    assert flagged["provision-console-grafana-token.yml"] is True, flagged
    assert flagged["deploy-console.yml"] is True, flagged


def test_the_pin_accepts_only_a_full_commit_sha():
    text = json.dumps(_play(PIN))
    assert "^[0-9a-f]{40}$" in text


# --- the drift check: structure, then the real command run against a real git history -----------------------


def _drift_task(play_name: str, subcommand: str) -> dict:
    tasks = [t for t in _tasks_of(_play(play_name)) if "ansible.builtin.command" in t]
    return next(t for t in tasks if t["ansible.builtin.command"]["argv"][3] == subcommand)


def test_the_drift_check_compares_the_pin_with_head_over_the_console_inputs_only():
    argv = _drift_task(DRIFT, "diff")["ansible.builtin.command"]["argv"]
    assert argv[:3] == ["git", "-C", "{{ playbook_dir }}/../.."], argv
    assert argv[3:8] == ["diff", "--quiet", "{{ _drift_pin }}", "HEAD", "--"], argv
    assert argv[8:] == CONSOLE_INPUTS, argv


def test_the_drift_check_verifies_the_pin_exists_before_it_compares():
    argv = _drift_task(DRIFT, "cat-file")["ansible.builtin.command"]["argv"]
    assert argv[:3] == ["git", "-C", "{{ playbook_dir }}/../.."], argv
    assert argv[3:6] == ["cat-file", "-e", "{{ _drift_pin }}^{commit}"], argv


def test_the_drift_refusal_is_visible_names_the_short_pin_and_says_nothing_was_minted():
    assertion = next(t for t in _tasks_of(_play(DRIFT)) if "ansible.builtin.assert" in t)
    assert "no_log" not in assertion, "an assert with no_log hides its own fail_msg"
    assert set(assertion["ansible.builtin.assert"]["that"]) == {"_drift_present.rc == 0", "_drift_diff.rc == 0"}
    msg = assertion["ansible.builtin.assert"]["fail_msg"]
    assert "the controller's console deploy inputs differ from what the console runs" in msg
    assert "{{ _drift_pin[:12] }}" in msg
    assert "redeploy the console from this branch first, then rerun" in msg
    assert "Nothing was rotated" in msg


def test_every_drift_task_is_skipped_under_local_mode_and_without_a_pin():
    for task in _tasks_of(_play(DRIFT)):
        when = _when(task)
        assert "local_mode" in when and "_drift_pin | length > 0" in when, task.get("name")


# Hooks and some shells export GIT_DIR and friends. A test repo must never inherit them, or the drift
# command would read whichever checkout the variable names instead of the temporary one.
GIT_ENV = {
    **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.test",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.test",
}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], env=GIT_ENV, check=True, capture_output=True)


def _run_drift(subcommand_argv: list, root: Path, pin: str) -> int:
    # The playbook's own argv, with its three placeholders filled the way Ansible would fill them.
    argv = [
        arg.replace("{{ playbook_dir }}/../..", str(root))
        .replace("{{ _drift_pin }}^{commit}", f"{pin}^{{commit}}")
        .replace("{{ _drift_pin }}", pin)
        for arg in subcommand_argv
    ]
    return subprocess.run(argv, env=GIT_ENV, capture_output=True).returncode


def test_the_drift_argv_refuses_exactly_when_a_console_input_differs_from_the_pin(tmp_path):
    root = tmp_path / "controller"
    for rel in [*CONSOLE_INPUTS, "platform/other.txt"]:
        # The deploy directory is a directory of files; every other input is a single file.
        path = root / rel / "Dockerfile" if rel.endswith("/deployment") else root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("v1\n")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "pinned")
    pin = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], env=GIT_ENV, capture_output=True, text=True, check=True
    ).stdout.strip()

    diff_argv = _drift_task(DRIFT, "diff")["ansible.builtin.command"]["argv"]
    cat_argv = _drift_task(DRIFT, "cat-file")["ansible.builtin.command"]["argv"]
    assert _run_drift(cat_argv, root, pin) == 0
    assert _run_drift(cat_argv, root, "0" * 40) != 0, "a pin absent from the checkout must be refused"

    # A change outside the console inputs leaves the rotation free to run.
    (root / "platform/other.txt").write_text("v2\n")
    _git(root, "commit", "-qam", "unrelated")
    assert _run_drift(diff_argv, root, pin) == 0

    # A change to any one console input is drift.
    (root / "platform/playbooks/deploy-console.yml").write_text("v2\n")
    _git(root, "commit", "-qam", "console input")
    assert _run_drift(diff_argv, root, pin) == 1


# --- no_log: the credential rule, the message rule, and the write guards -------------------------------------


def test_every_credential_task_is_no_log_and_no_message_interpolates_a_credential():
    checked = 0
    for play in playbook_yaml.plays(ROTATE):
        for task, _ in _leaves(play.get("tasks", [])):
            for module in (v for v in task.values() if isinstance(v, dict)):
                for key in MESSAGE_KEYS:
                    if key in module:
                        assert not MESSAGE_LEAK.search(json.dumps(module[key])), task.get("name")
            if CREDENTIAL.search(json.dumps(_checked(task))):
                checked += 1
                assert task.get("no_log") is True, task.get("name")
    assert checked >= 9, checked


def test_an_assert_or_fail_message_is_visible_so_a_named_refusal_is_not_hidden():
    for play in playbook_yaml.plays(ROTATE):
        for task, _ in _leaves(play.get("tasks", [])):
            if "ansible.builtin.assert" in task or "ansible.builtin.fail" in task:
                assert task.get("no_log") is not True, task.get("name")


def test_every_post_is_a_write_guarded_by_check_mode_or_a_login_that_skips_under_it():
    for play in playbook_yaml.plays(ROTATE):
        for task, when in _leaves(play.get("tasks", [])):
            uri = task.get("ansible.builtin.uri") or {}
            if uri.get("method") == "POST":
                guarded = "not ansible_check_mode" in when
                assert guarded or task.get("check_mode") is False, task.get("name")


def test_the_mint_and_the_store_are_skipped_under_check_mode():
    mint = next(
        t for t in _tasks_of(_play(MINT)) if t.get("name") == "Mint a new secret_id (the role_id does not change)"
    )
    assert "not ansible_check_mode" in _when(mint)
    _store, store_when = _find_leaf(_play(MINT), lambda t: t.get("ansible.builtin.include_tasks") == STORE_INCLUDE)
    assert "not ansible_check_mode" in store_when


def test_the_store_is_guarded_by_the_replaced_secret_id():
    store, _ = _find_leaf(_play(MINT), lambda t: t.get("ansible.builtin.include_tasks") == STORE_INCLUDE)
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
