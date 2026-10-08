"""agentgateway's secret declaration evaluates to a list on current ansible-core.

`_secret_definitions` is `[...] + _client_defs`. When `_client_defs` rendered as text, ansible-core
2.16 converted it and 2.19+ did not, so the deploy failed on the production controller image
(semaphore v2.19.11, ansible-core 2.20.8) with "can only concatenate list". CI installs the latest
ansible-core, so this loads the declaration file there exactly as the deploy does (vars_files).
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
DECLARATION = REPO / "platform/playbooks/vars/secret-declarations/agentgateway.yml"
PLAYBOOK = REPO / "platform/playbooks/deploy-agentgateway.yml"

pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")


def _names(tmp_path, clients):
    harness = [{
        "hosts": "localhost", "connection": "local", "gather_facts": False,
        "vars": {"agw_clients": clients}, "vars_files": [str(DECLARATION)],
        "tasks": [{"ansible.builtin.debug": {"msg": "DEFS {{ _secret_definitions | to_json }}"}}],
    }]
    path = tmp_path / "harness.yml"
    path.write_text(yaml.safe_dump(harness))
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env["ANSIBLE_NOCOLOR"] = "1"
    done = subprocess.run(["ansible-playbook", "-i", "localhost,", str(path)], cwd=REPO, env=env,
                          text=True, capture_output=True, check=True)
    line = next(ln for ln in done.stdout.splitlines() if "DEFS " in ln)
    return json.loads(json.loads(line.split('"msg": ', 1)[1]).split("DEFS ", 1)[1])


def test_client_keys_join_the_declaration_as_entries(tmp_path):
    defs = _names(tmp_path, ["skynet", "openhands"])
    assert [d["name"] for d in defs] == [
        "vllm_api_key", "agw_db_password", "agw_oidc_cookie_seed", "client_skynet", "client_openhands"]
    assert all(d["type"] == "random" and d["length"] == 48 for d in defs[3:])


def test_no_clients_still_declares_the_service_secrets(tmp_path):
    assert [d["name"] for d in _names(tmp_path, [])] == ["vllm_api_key", "agw_db_password", "agw_oidc_cookie_seed"]


def _upstream_key_gate(tmp_path, resolved, requires_key):
    """Run the deploy's real upstream-key assert with manage-secrets' facts: `_resolved` is set,
    `secrets` is not (it exists only inside manage-secrets' template task)."""
    play = next(p for p in yaml.safe_load(PLAYBOOK.read_text()) if p.get("name", "").startswith("Phase 1"))
    gate = next(t for t in play["tasks"] if t.get("name", "").startswith("Refuse to deploy without the upstream key"))
    harness = [{
        "hosts": "localhost", "connection": "local", "gather_facts": False,
        "vars": {"service_name": "agentgateway", "agw_upstream_requires_key": requires_key},
        "tasks": [{"ansible.builtin.set_fact": {"_resolved": resolved}}, gate],
    }]
    path = tmp_path / "gate.yml"
    path.write_text(yaml.safe_dump(harness))
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    return subprocess.run(["ansible-playbook", "-i", "localhost,", str(path)], cwd=REPO, env=env,
                          text=True, capture_output=True).returncode


def test_the_upstream_key_gate_passes_a_stored_key(tmp_path):
    # Semaphore task 1177: the gate read `secrets`, undefined at play level, and refused every
    # production deploy whose upstream takes a key.
    assert _upstream_key_gate(tmp_path, {"vllm_api_key": "k"}, True) == 0


def test_the_upstream_key_gate_refuses_an_empty_key_unless_keyless(tmp_path):
    assert _upstream_key_gate(tmp_path, {"vllm_api_key": ""}, True) != 0
    assert _upstream_key_gate(tmp_path, {"vllm_api_key": ""}, False) == 0


@pytest.mark.parametrize("refused,allow,passes", [
    (False, False, True), (False, True, True), (True, False, False), (True, True, True),
])
def test_an_unproven_round_trip_fails_unless_accepted(tmp_path, refused, allow, passes):
    # PR 221 review: a gateway refusal skipped the completion assert, so a deploy passed without
    # proving the upstream. It now fails unless agw_verify_allow_unproven accepts it.
    play = next(p for p in yaml.safe_load(PLAYBOOK.read_text()) if p.get("name", "").startswith("Phase 3"))
    gate = next(t for t in play["tasks"] if t.get("name", "").startswith("Refuse to report success"))
    harness = [{
        "hosts": "localhost", "connection": "local", "gather_facts": False,
        "vars": {"_verify_client": "c", "_verify_refused": refused, "agw_verify_allow_unproven": allow,
                 "agw_upstream_base_url": "http://upstream.invalid/v1"},
        "tasks": [gate],
    }]
    path = tmp_path / "unproven.yml"
    path.write_text(yaml.safe_dump(harness))
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    done = subprocess.run(["ansible-playbook", "-i", "localhost,", str(path)], cwd=REPO, env=env,
                          text=True, capture_output=True)
    assert (done.returncode == 0) is passes, done.stdout[-1500:]
