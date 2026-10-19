"""Safety checks for the inventory-pinned Proxmox import-content workflow."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import forgeries
import pytest
import yaml
from ansible_python import ansible_python, needs_ansible
from jinja2.nativetypes import NativeEnvironment

ROOT = Path(__file__).resolve().parents[2]
PLAYBOOK = ROOT / "platform/playbooks/enable-o11y-pbs-import-content.yml"
TEMPLATES = ROOT / "platform/semaphore/templates.yml"
BAO_TRANSPORT = ROOT / "platform/playbooks/tasks/assert-bao-transport.yml"


def test_dev_template_and_write_guards_are_declared() -> None:
    plays = yaml.safe_load(PLAYBOOK.read_text())
    catalog = yaml.safe_load(TEMPLATES.read_text())["templates"]
    assert plays[0]["tags"] == ["always"]
    assert plays[1]["tags"] == ["always"]
    rows = [row for row in catalog if row.get("name") == "Enable PBS Import Storage Content (Dev)"]
    assert len(rows) == 1
    assert rows[0]["playbook"] == "platform/playbooks/enable-o11y-pbs-import-content.yml"
    assert rows[0]["repository"] == "agent-cloud dev"
    assert rows[0]["survey_vars"] == [
        {
            "name": "expected_repository_sha",
            "title": "Reviewed commit SHA",
            "description": "Exact reviewed Dev revision for the inventory-pinned Proxmox import-content update",
            "type": "string",
            "required": True,
        }
    ]
    assert plays[0]["ansible.builtin.import_playbook"] == "refuse-internal-extra-vars.yml"
    assert plays[1]["ansible.builtin.import_playbook"] == "require-reviewed-checkout.yml"
    assert (
        "platform/playbooks/enable-o11y-pbs-import-content.yml" in plays[1]["vars"]["_reviewed_checkout_payload_paths"]
    )

    play = plays[2]
    assert (
        play["vars"]["_storage_id"]
        == "{{ hostvars[_o11y_host].proxmox_pbs_import_storage_id | default('') if _o11y_host else '' }}"
    )
    tasks = play["tasks"]
    names = [task["name"] for task in tasks]
    first_request = next(i for i, task in enumerate(tasks) if "ansible.builtin.uri" in task)
    assert names.index("Require the exact Dev run and one private declaration") < first_request
    assert names.index("Require exact reviewed revision") < first_request
    assert names.index("Require clean reviewed checkout") < first_request
    assert names.index("Require secure Proxmox API credentials") < first_request
    assert names.index("Validate active file storage and the digest-protected content list") < names.index(
        "Add import while preserving content with the observed digest"
    )
    validation = next(
        task["ansible.builtin.assert"]["that"]
        for task in tasks
        if task["name"] == "Validate active file storage and the digest-protected content list"
    )
    assert any("'none' not in _current_content" in item for item in validation)
    assert any("'' not in _current_content" in item for item in validation)
    assert any("_node_storage_status.json.data.type == _storage_config.json.data.type" in item for item in validation)
    assert any("_storage_config.json.data.type | default('') == 'dir'" in item for item in validation)
    assert all("_node_storage_status.json.data.storage" not in item for item in validation)
    status_request = next(task for task in tasks if task["name"] == "Read the exact node storage status")
    assert status_request["ansible.builtin.uri"]["url"] == (
        "{{ _pve_host }}/api2/json/nodes/{{ _target_node | urlencode }}"
        "/storage/{{ _storage_id | urlencode }}/status"
    )
    assert all(
        "no_log" not in task
        for task in tasks
        if task["name"] in {
            "Preserve the current content list",
            "Add import to the requested content list only when absent",
        }
    )
    assert names.index("Verify import content readback") > names.index(
        "Add import while preserving content with the observed digest"
    )

    write = next(
        task for task in tasks if task["name"] == "Add import while preserving content with the observed digest"
    )
    assert "verify" not in write.get("tags", [])
    request = write["ansible.builtin.uri"]
    assert request["method"] == "PUT"
    assert request["body"] == {
        "content": "{{ _desired_content | join(',') }}",
        "digest": "{{ _storage_config.json.data.digest }}",
    }
    assert "not ansible_check_mode" in write["when"]
    assert "check_mode" not in write
    assert write["no_log"] is True
    assert all(
        task.get("no_log") is True
        for task in tasks
        if task.get("ansible.builtin.uri", {}).get("headers", {}).get("Authorization")
    )
    assert all(
        "verify" in task.get("tags", [])
        for task in tasks
        if task["name"] != "Add import while preserving content with the observed digest"
    )
    assert all(
        task["ansible.builtin.uri"].get("method", "GET") == "GET"
        for task in tasks
        if "verify" in task.get("tags", []) and "ansible.builtin.uri" in task
    )


def test_inventory_openbao_url_uses_shared_transport_policy() -> None:
    play = yaml.safe_load(PLAYBOOK.read_text())[2]
    tasks = play["tasks"]
    names = [task["name"] for task in tasks]
    initial_guard = next(
        task["ansible.builtin.assert"]["that"]
        for task in tasks
        if task["name"] == "Require the exact Dev run and one private declaration"
    )
    assert "_openbao_inventory_url | string | length > 0" in initial_guard
    assert "openbao_addr | default('') == _openbao_inventory_url" in initial_guard
    assert "not (local_mode | default(false) | bool)" in initial_guard
    assert "service_url" in play["vars"]["_forgery_names"]
    transport_index = names.index("Refuse cleartext OpenBao transport")
    lookup_index = names.index("Read Proxmox API credentials from OpenBao")
    assert transport_index < lookup_index
    assert tasks[transport_index]["ansible.builtin.include_tasks"] == "tasks/assert-bao-transport.yml"
    assert tasks[transport_index]["vars"]["_assert_bao_url"] == "{{ _bao_url }}"

    source = BAO_TRANSPORT.read_text()
    strict_match = re.search(r"_pat_strict: '(.*?)'", source)
    https_match = re.search(r"_pat_https: '(.*?)'", source)
    assert strict_match and https_match
    strict = strict_match.group(1).replace(r"\\", "\\")
    https = https_match.group(1).replace(r"\\", "\\")
    assert "_assert_bao_url is match(_pat_strict)" in source
    assert "or ((local_mode | default(false) | bool)" in source

    def shared_guard_accepts(url: str) -> bool:
        return bool(re.match(strict, url) and (not url.startswith("https://") or re.match(https, url)))

    internal_host = ".".join(str(octet) for octet in (10, 1, 2, 3))
    assert shared_guard_accepts(f"http://{internal_host}:8200")
    assert not shared_guard_accepts("http://openbao.example.invalid:8200")


def test_content_union_preserves_existing_values_and_is_idempotent() -> None:
    tasks = yaml.safe_load(PLAYBOOK.read_text())[2]["tasks"]
    union = next(
        task["ansible.builtin.set_fact"]
        for task in tasks
        if task["name"] == "Add import to the requested content list only when absent"
    )
    env = NativeEnvironment()

    desired = env.from_string(union["_desired_content"]).render(_current_content=["images", "iso", "backup"])
    add = env.from_string(union["_add_import"]).render(_current_content=["images", "iso", "backup"])
    already = env.from_string(union["_desired_content"]).render(_current_content=["images", "import", "iso"])
    already_add = env.from_string(union["_add_import"]).render(_current_content=["images", "import", "iso"])

    assert desired == ["images", "iso", "backup", "import"]
    assert add is True
    assert already == ["images", "import", "iso"]
    assert already_add is False


@needs_ansible
@pytest.mark.parametrize(
    "forge",
    forgeries.templated_forgeries(
        "proxmox_pbs_import_storage_id",
        "private-import-store",
        "attacker-store",
    ),
)
def test_templated_extra_var_forgery_is_refused(tmp_path: Path, forge) -> None:
    inventory = tmp_path / "inventory.yml"
    inventory.write_text(
        yaml.safe_dump(
            {
                "all": {
                    "hosts": {"localhost": {"ansible_connection": "local"}},
                    "children": {
                        "openbao_svc": {"hosts": {"bao": {"service_url": "https://bao.example.invalid:8200"}}},
                        "o11y_svc": {
                            "hosts": {
                                "o11y": {
                                    "proxmox_pbs_storage_node": "node-a",
                                    "proxmox_pbs_import_storage_id": "private-import-store",
                                }
                            }
                        },
                    },
                },
            }
        )
    )
    payload = {
        "expected_repository_sha": "a" * 40,
        "openbao_addr": "https://bao.example.invalid:8200",
        "extra_var": json.loads(forge(tmp_path)),
    }
    harness = r"""
import json, sys, yaml
from ansible.inventory.manager import InventoryManager
from ansible.parsing.dataloader import DataLoader
from ansible.playbook.play import Play
from ansible.template import Templar, trust_as_template
from ansible.vars.hostvars import HostVars
from ansible.vars.manager import VariableManager
from ansible.utils import vars as ansible_vars

payload = json.load(sys.stdin)
loader = DataLoader()
inventory = InventoryManager(loader=loader, sources=sys.argv[2])
manager = VariableManager(loader=loader, inventory=inventory)
manager._extra_vars = {**payload["extra_var"], "expected_repository_sha": payload["expected_repository_sha"],
                       "openbao_addr": payload["openbao_addr"]}
plays = yaml.safe_load(open(sys.argv[1]))
doc = next(p for p in plays if p.get("name") == "Enable PBS import content on the declared file store")
play = Play.load({"name": doc["name"], "hosts": doc["hosts"], "connection": doc["connection"],
                  "gather_facts": doc["gather_facts"], "vars": doc["vars"]},
                 variable_manager=manager, loader=loader)
variables = manager.get_vars(play=play, host=inventory.get_host("localhost"))
variables["hostvars"] = HostVars(inventory, manager, loader)
play_vars = play.get_vars()
for name in ("_o11y_hosts", "_o11y_host", "_storage_id"):
    variables[name] = Templar(loader=loader, variables=variables).template(trust_as_template(play_vars[name]))
variables["_forgery_names"] = play_vars["_forgery_names"]
ansible_vars.load_extra_vars = lambda loader: manager.extra_vars
spec = __import__("importlib.util", fromlist=["spec_from_file_location"]).spec_from_file_location(
    "extra_var_names", sys.argv[3])
module = __import__("importlib.util", fromlist=["module_from_spec"]).module_from_spec(spec)
spec.loader.exec_module(module)
extra_names = module.FilterModule().filters()["extra_var_names"]("")
variables["_forged_names"] = sorted(set(extra_names) & set(variables["_forgery_names"]))
templar = Templar(loader=loader, variables=variables)
guard = next(t for t in doc["tasks"] if t.get("name") == "Require the exact Dev run and one private declaration")
condition = next(c for c in guard["ansible.builtin.assert"]["that"] if "_forged_names | length == 0" in c)
print(json.dumps({"refused": not templar.evaluate_conditional(trust_as_template(condition))}))
"""
    result = subprocess.run(
        [
            ansible_python(),
            "-c",
            harness,
            str(PLAYBOOK),
            str(inventory),
            str(ROOT / "platform/playbooks/filter_plugins/extra_var_names.py"),
        ],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "ANSIBLE_LOCAL_TEMP": str(tmp_path)},
    )
    assert result.returncode == 0, result.stderr
    resolved = json.loads(result.stdout)
    assert resolved["refused"] is True
