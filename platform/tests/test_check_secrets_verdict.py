"""check-secrets.yml's step verdict, evaluated (workflow step secrets-approle).

The play's own variables are extracted and run against stubbed OpenBao answers, so the test
exercises the verdict the playbook computes, not a copy of it. Requires ansible-playbook.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
PLAYBOOK = REPO / "platform/playbooks/check-secrets.yml"

pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")

class _Unsafe(str):
    """A value Ansible must not template, like the real play's file lookup (the deploy playbook
    read as text is never templated)."""


yaml.SafeDumper.add_representer(_Unsafe, lambda d, v: d.represent_scalar("!unsafe", str(v)))

def _secrets_play() -> dict:
    """The play that checks the secrets (the first play only guards the target group)."""
    return next(p for p in yaml.safe_load(PLAYBOOK.read_text()) if str(p.get("name", "")).startswith("Check secrets"))


DECLARED = [
    {"name": "svc_session", "type": "random", "length": 32},
    {"name": "svc_api_key", "type": "existing"},
    {"name": "svc_optional_integration", "type": "existing"},
]
REQUIRED = ["svc_api_key"]


def _verdict(tmp_path, *, stored, deploy=True, policy_file=False, role=200, policy=200, token_policies=("svc",),
             service="svc", vars_file=None, host_vars=None, deploy_raw=None):
    play = _secrets_play()
    record = next(t for t in play["tasks"] if t.get("name") == "Record the step result")
    # The verdict is computed by the "Decide the secrets step" set_facts; lifted here as vars.
    decided = {k: v for t in play["tasks"] if str(t.get("name", "")).startswith("Decide the secrets step")
               for k, v in t["ansible.builtin.set_fact"].items()}
    pv = dict(play["vars"])
    # Stub the controller-side facts the real play reads from files.
    pv["_deploy_raw"] = _Unsafe(deploy_raw if deploy_raw is not None else (
        yaml.safe_dump([{"vars": {"_secret_definitions": DECLARED, "_required_existing_secrets": REQUIRED}}])
        if deploy else ""))
    pv["_approle_declared"] = policy_file
    pv["service_name"] = service
    pv.update(host_vars or {})
    pv["_bao_data"] = {"status": 200, "json": {"data": {"data": stored}}} if stored is not None else {"status": 404}
    pv["_approle"] = {"results": [
        {"status": role, "json": {"data": {"token_policies": list(token_policies)}}},
        {"status": policy, "json": {}},
    ]}
    step = ("step_result_status", "step_result_error", "step_result_evidence")
    rv = {**decided, **{k: record["vars"][k] for k in step}}
    harness = [{
        "hosts": "localhost", "connection": "local", "gather_facts": False, "vars": {**pv, **rv},
        **({"vars_files": [str(vars_file)]} if vars_file else {}),
        "tasks": [{"ansible.builtin.debug": {"msg": "VERDICT {{ {'status': step_result_status, "
                   "'error': step_result_error, 'evidence': step_result_evidence} | to_json }}"}}],
    }]
    path = tmp_path / "harness.yml"
    path.write_text(yaml.safe_dump(harness))
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env["ANSIBLE_NOCOLOR"] = "1"
    done = subprocess.run(["ansible-playbook", "-i", "localhost,", str(path)], cwd=REPO, env=env,
                          text=True, capture_output=True, check=True)
    line = next(ln for ln in done.stdout.splitlines() if "VERDICT " in ln)
    return json.loads(json.loads(line.split('"msg": ', 1)[1]).split("VERDICT ", 1)[1])


def test_declared_existing_secret_present_passes(tmp_path):
    v = _verdict(tmp_path, stored={"svc_api_key": "k"})
    assert v["status"] == "pass", v
    assert v["evidence"]["approle"] == "not-applicable"


def test_a_missing_required_key_fails(tmp_path):
    # The finding: a path that exists but lacks a declared existing-typed key used to pass.
    v = _verdict(tmp_path, stored={"unrelated": "x"})
    assert v["status"] == "fail"
    assert v["evidence"]["missing"] == ["svc_api_key"]


def test_an_existing_key_the_deploy_does_not_require_is_optional(tmp_path):
    # PR 203 Codex review: Postiz declares ~40 social-platform credentials as `existing` and
    # leaves them empty until seeded; only _required_existing_secrets is the deploy's contract.
    v = _verdict(tmp_path, stored={"svc_api_key": "k"})
    assert v["status"] == "pass" and v["evidence"]["missing"] == [], v


def test_a_computed_required_list_left_in_a_playbook_fails_closed(tmp_path):
    play = [{"vars": {"_secret_definitions": DECLARED,
                      "_required_existing_secrets": "{{ ['svc_api_key'] if x else [] }}"}}]
    v = _verdict(tmp_path, stored={"svc_api_key": "k"}, deploy_raw=yaml.safe_dump(play))
    assert v["status"] == "fail" and "cannot be evaluated here" in v["error"], v


def test_a_generated_secret_absent_before_the_deploy_is_not_required(tmp_path):
    assert _verdict(tmp_path, stored={"svc_api_key": "k"})["evidence"]["missing"] == []


def test_a_declared_approle_that_is_missing_fails(tmp_path):
    v = _verdict(tmp_path, stored={"svc_api_key": "k"}, policy_file=True, role=404)
    assert v["status"] == "fail" and v["evidence"]["approle"] == "missing"


def test_an_approle_without_the_service_policy_fails(tmp_path):
    v = _verdict(tmp_path, stored={"svc_api_key": "k"}, policy_file=True, token_policies=("default",))
    assert v["status"] == "fail" and v["evidence"]["approle"] == "missing"


def test_a_declared_approle_with_its_policy_passes(tmp_path):
    v = _verdict(tmp_path, stored={"svc_api_key": "k"}, policy_file=True)
    assert v["status"] == "pass" and v["evidence"]["approle"] == "present"


def test_no_deploy_declaration_fails_closed(tmp_path):
    v = _verdict(tmp_path, stored={"svc_api_key": "k"}, deploy=False)
    assert v["status"] == "fail" and "cannot be checked" in v["error"]


SECRETS_VARS = REPO / "platform/playbooks/vars/secret-declarations"


def test_a_failed_step_fails_the_task():
    # PR 203 Codex review: the step result said fail while Semaphore reported success.
    tasks = _secrets_play()["tasks"]
    names = [t.get("name") for t in tasks]
    fail = tasks[-1]
    assert names.index("Record the step result") < len(tasks) - 1
    assert "ansible.builtin.fail" in fail and fail["when"] == "_errors | length > 0"


def test_agentgateway_computed_declaration_still_requires_the_upstream_key(tmp_path):
    # PR 203 Codex review: read as playbook text, this declaration was a string, so the
    # required vllm_api_key was invisible and the step passed without it.
    v = _verdict(tmp_path, service="agentgateway", vars_file=SECRETS_VARS / "agentgateway.yml",
                 host_vars={"agw_clients": ["skynet"]}, stored={"agw_db_password": "x"})
    assert v["status"] == "fail" and v["evidence"]["missing"] == ["vllm_api_key"], v


def test_a_keyless_agentgateway_upstream_does_not_require_the_key(tmp_path):
    # PR 203 Codex review: local-dev declares agw_upstream_requires_key=false, which the deploy
    # accepts, and the secrets step failed it anyway.
    v = _verdict(tmp_path, service="agentgateway", vars_file=SECRETS_VARS / "agentgateway.yml",
                 host_vars={"agw_clients": ["skynet"], "agw_upstream_requires_key": False},
                 stored={"agw_db_password": "x"})
    assert v["status"] == "pass", v


def test_an_optional_key_stored_empty_is_not_an_error(tmp_path):
    # PR 203 Codex review: a keyless deploy stores vllm_api_key as '' and the step then failed.
    v = _verdict(tmp_path, service="agentgateway", vars_file=SECRETS_VARS / "agentgateway.yml",
                 host_vars={"agw_clients": ["skynet"], "agw_upstream_requires_key": False},
                 stored={"agw_db_password": "x", "vllm_api_key": ""})
    assert v["status"] == "pass", v


def test_a_required_key_stored_empty_fails(tmp_path):
    v = _verdict(tmp_path, service="agentgateway", vars_file=SECRETS_VARS / "agentgateway.yml",
                 host_vars={"agw_clients": ["skynet"]}, stored={"agw_db_password": "x", "vllm_api_key": ""})
    assert v["status"] == "fail" and v["evidence"]["empty"] == ["vllm_api_key"], v


def test_o11y_requires_the_alert_webhook_only_when_alerts_are_on(tmp_path):
    on = _verdict(tmp_path, service="o11y", vars_file=SECRETS_VARS / "o11y.yml",
                  host_vars={"o11y_alerts_enabled": True}, stored={"grafana_admin_password": "x"})
    assert on["evidence"]["missing"] == ["alert_discord_webhook_url"], on
    off = _verdict(tmp_path, service="o11y", vars_file=SECRETS_VARS / "o11y.yml",
                   host_vars={"o11y_alerts_enabled": False}, stored={"grafana_admin_password": "x"})
    assert off["status"] == "pass", off


def test_a_computed_declaration_left_in_a_playbook_fails_closed(tmp_path):
    play = [{"vars": {"_secret_definitions": "{{ [{'name': 'k', 'type': 'existing'}] }}"}}]
    v = _verdict(tmp_path, stored={"k": "v"}, deploy_raw=yaml.safe_dump(play))
    assert v["status"] == "fail" and "cannot be evaluated here" in v["error"], v


def test_no_deploy_playbook_computes_its_secret_declaration_inline():
    # A computed declaration belongs in vars/secret-declarations/<service>.yml, where every reader
    # evaluates it; inline it is only a string to check-secrets.
    offenders = []
    for path in sorted((REPO / "platform/playbooks").glob("deploy-*.yml")):
        for play in yaml.safe_load(path.read_text()) or []:
            decl = (play.get("vars") or {}).get("_secret_definitions") if isinstance(play, dict) else None
            if isinstance(decl, str):
                offenders.append(path.name)
    assert not offenders, offenders


def test_the_local_controller_may_read_what_the_secrets_step_reads():
    # PR 203 Codex review: Check Secrets (Local) read the service's AppRole and policy with a token
    # whose policy granted only secret/*, so OpenBao answered 403 before a verdict.
    play = _secrets_play()
    probe = next(t for t in play["tasks"] if t.get("name", "").startswith("Read the service AppRole"))
    bootstrap = yaml.safe_load((REPO / "platform/playbooks/bootstrap-local-dev.yml").read_text())[0]["tasks"]
    write = next(t for t in bootstrap if t.get("name") == "Write local-semaphore policy")
    policy = write["ansible.builtin.uri"]["body"]["policy"]
    for prefix in probe["loop"]:
        line = next((ln for ln in policy.splitlines() if ln.strip().startswith(f'path "{prefix}/*"')), "")
        assert '"read"' in line, prefix


def test_uhhcraft_requires_the_keys_its_app_panics_without(tmp_path):
    # PR 203 Codex review: stripe_secret_key is `type: user`, so it was not required, and the step
    # passed a service whose app panics on an empty STRIPE_SECRET_KEY (config.go requireEnv).
    raw = (REPO / "platform/playbooks/deploy-uhhcraft.yml").read_text()
    stored = dict.fromkeys(("stripe_publishable_key", "stripe_webhook_secret", "resend_api_key",
                            "discord_orders_webhook_url", "discord_ops_webhook_url"), "v")
    v = _verdict(tmp_path, service="uhhcraft", stored=stored, deploy_raw=raw)
    assert v["status"] == "fail" and v["evidence"]["missing"] == ["stripe_secret_key"], v


def test_a_deploy_with_no_secret_contract_fails_closed(tmp_path):
    # PR 203 Codex review: github-runner read its key directly and declared nothing, so any stored
    # sibling key made the step pass without checking the one the deploy needs.
    v = _verdict(tmp_path, stored={"other": "x"}, deploy_raw=yaml.safe_dump([{"hosts": "x", "vars": {}}]))
    assert v["status"] == "fail" and "declares no secret contract" in v["error"], v


def test_github_runner_requires_its_app_key(tmp_path):
    raw = (REPO / "platform/playbooks/deploy-github-runner.yml").read_text()
    v = _verdict(tmp_path, service="github-runner", stored={"other": "x"}, deploy_raw=raw)
    assert v["status"] == "fail" and v["evidence"]["missing"] == ["app_private_key"], v
