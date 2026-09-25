"""The service deployment workflow's NetBox calls keep their tokens out of task output.

PR 195 Codex review: the collector copied whole registered uri results (which can carry the
request headers) into a visible fact, and validate-address's token-bearing lookup could print
its headers on failure. Also covers two local-dev defects from the same review.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
PLAYBOOKS = REPO / "platform/playbooks"


def _tasks(path: Path) -> list[dict]:
    out = []

    def walk(tasks):
        for t in tasks or []:
            if isinstance(t, dict):
                out.append(t)
                for k in ("block", "rescue", "always"):
                    walk(t.get(k))

    doc = yaml.safe_load(path.read_text())
    for play in doc if isinstance(doc, list) else []:
        walk(play.get("tasks") if isinstance(play, dict) and "hosts" in play else [play])
    return out


def _named(path: Path, name: str) -> dict:
    return next(t for t in _tasks(path) if t.get("name") == name)


@pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")
def test_the_collector_keeps_only_service_and_vm_id(tmp_path):
    sort = _named(PLAYBOOKS / "collect-service-conformance.yml", "NetBox: sort the lookups")
    results = [
        {"item": "svc-a", "status": 200, "json": {"count": 1, "results": [{"id": 7}]},
         "invocation": {"module_args": {"headers": {"Authorization": "Bearer nbt_secret.value"}}}},
        {"item": "svc-b", "status": 200, "json": {"count": 0, "results": []}},
    ]
    harness = [{
        "hosts": "localhost", "connection": "local", "gather_facts": False,
        "vars": {"_vms": {"results": results}},
        "tasks": [sort, {"ansible.builtin.debug": {"msg": "FOUND {{ _vm_found | to_json }}"}}],
    }]
    path = tmp_path / "sort.yml"
    path.write_text(yaml.safe_dump(harness))
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env["ANSIBLE_NOCOLOR"] = "1"
    out = subprocess.run(["ansible-playbook", "-i", "localhost,", str(path)], cwd=REPO, env=env,
                         text=True, capture_output=True, check=True).stdout
    line = next(ln for ln in out.splitlines() if "FOUND " in ln)
    found = json.loads(json.loads(line.split('"msg": ', 1)[1]).split("FOUND ", 1)[1])
    assert found == [{"item": "svc-a", "id": 7}]
    assert "nbt_secret" not in out


def test_validate_address_hides_its_token_bearing_requests():
    path = PLAYBOOKS / "validate-address-free.yml"
    for name in ("NetBox: find the service's VM record and the Proxmox clusters",
                 "NetBox: create the VM record (planned, in the Proxmox cluster)"):
        task = _named(path, name)
        assert task.get("no_log") is True and task.get("failed_when") is False, name
    assert _named(path, "Require both NetBox reads to answer").get("no_log") is not True


def test_token_minting_uses_the_configured_engine():
    play = yaml.safe_load((PLAYBOOKS / "provision-netbox-automation-token.yml").read_text())[0]
    manage = play["vars"]["_manage"]
    assert "container_engine" in manage and "netbox_app_container" in manage, manage


def test_local_discovery_requires_the_subnet_its_scans_render():
    names = [t.get("name") for t in yaml.safe_load((PLAYBOOKS / "tasks/assert-local-discovery-scope.yml").read_text())]
    assert "Local discovery: extra targets need the subnet the scans cover" in names
