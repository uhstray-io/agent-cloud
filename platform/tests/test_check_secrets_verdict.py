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

DECLARED = [
    {"name": "svc_session", "type": "random", "length": 32},
    {"name": "svc_api_key", "type": "existing"},
]


def _verdict(tmp_path, *, stored, deploy=True, policy_file=False, role=200, policy=200, token_policies=("svc",)):
    play = yaml.safe_load(PLAYBOOK.read_text())[0]
    record = next(t for t in play["tasks"] if t.get("name") == "Record the step result")
    pv = dict(play["vars"])
    # Stub the controller-side facts the real play reads from files.
    pv["_deploy_raw"] = yaml.safe_dump([{"vars": {"_secret_definitions": DECLARED}}]) if deploy else ""
    pv["_approle_declared"] = policy_file
    pv["service_name"] = "svc"
    pv["_bao_data"] = {"status": 200, "json": {"data": {"data": stored}}} if stored is not None else {"status": 404}
    pv["_approle"] = {"results": [
        {"status": role, "json": {"data": {"token_policies": list(token_policies)}}},
        {"status": policy, "json": {}},
    ]}
    rv = {k: v for k, v in record["vars"].items() if not k.startswith("step_result_")}
    rv.update({k: record["vars"][k] for k in ("step_result_status", "step_result_error", "step_result_evidence")})
    harness = [{
        "hosts": "localhost", "connection": "local", "gather_facts": False, "vars": {**pv, **rv},
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
