"""deploy-agentgateway.yml's secret declaration evaluates to a list on current ansible-core.

`_secret_definitions` is `[...] + _client_defs`. When `_client_defs` rendered as text, ansible-core
2.16 converted it and 2.19+ did not, so the deploy failed on the production controller image
(semaphore v2.19.11, ansible-core 2.20.8) with "can only concatenate list". CI installs the latest
ansible-core, so this evaluates the play's real variables there.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
PLAYBOOK = REPO / "platform/playbooks/deploy-agentgateway.yml"

pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")


def _names(tmp_path, clients):
    play = next(p for p in yaml.safe_load(PLAYBOOK.read_text()) if "_secret_definitions" in (p.get("vars") or {}))
    pv = {k: play["vars"][k] for k in ("_client_defs", "_secret_definitions")}
    harness = [{
        "hosts": "localhost", "connection": "local", "gather_facts": False,
        "vars": {**pv, "agw_clients": clients},
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
