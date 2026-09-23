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

DECLARED = [
    {"name": "svc_session", "type": "random", "length": 32},
    {"name": "svc_api_key", "type": "existing"},
]


def _verdict(tmp_path, *, stored, deploy=True, policy_file=False, role=200, policy=200, token_policies=("svc",),
             service="svc", vars_file=None, host_vars=None, deploy_raw=None):
    play = yaml.safe_load(PLAYBOOK.read_text())[0]
    record = next(t for t in play["tasks"] if t.get("name") == "Record the step result")
    pv = dict(play["vars"])
    # Stub the controller-side facts the real play reads from files.
    pv["_deploy_raw"] = _Unsafe(deploy_raw if deploy_raw is not None else (
        yaml.safe_dump([{"vars": {"_secret_definitions": DECLARED}}]) if deploy else ""))
    pv["_approle_declared"] = policy_file
    pv["service_name"] = service
    pv.update(host_vars or {})
    pv["_bao_data"] = {"status": 200, "json": {"data": {"data": stored}}} if stored is not None else {"status": 404}
    pv["_approle"] = {"results": [
        {"status": role, "json": {"data": {"token_policies": list(token_policies)}}},
        {"status": policy, "json": {}},
    ]}
    rv = {k: v for k, v in record["vars"].items() if not k.startswith("step_result_")}
    rv.update({k: record["vars"][k] for k in ("step_result_status", "step_result_error", "step_result_evidence")})
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
