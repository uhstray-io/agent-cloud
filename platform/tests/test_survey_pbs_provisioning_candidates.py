"""Guards for the private GET-only PBS inventory candidate survey."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from ansible_python import ansible_python, needs_ansible

ROOT = Path(__file__).resolve().parents[2]
PLAYBOOK_DIR = ROOT / "platform" / "playbooks"
HELPER_PATH = PLAYBOOK_DIR / "files" / "select_pbs_provisioning_candidates.py"
SPEC = importlib.util.spec_from_file_location("pbs_candidates", HELPER_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def sample() -> dict:
    seed = 400
    checks = [
        {"candidate": vmid, "status": 400 if vmid == seed else 200, "data": vmid}
        for vmid in range(seed, seed + 64)
    ]
    return {
        "target_node": "node-a",
        "declared_vm_image_storage": "vm-images",
        "seed_status": 200,
        "seed_data": seed,
        "nextid_checks": checks,
        "node_storage_status": 200,
        "node_storage": [
            {"storage": "stage-dir", "type": "dir", "content": "import,iso", "active": 1},
            {"storage": "vm-images", "type": "lvm", "content": "images", "active": True},
            {"storage": "inactive-dir", "type": "dir", "content": "import", "active": 0},
        ],
        "cluster_storage_status": 200,
        "cluster_storage": [
            {"storage": "stage-dir", "type": "dir", "content": "import,iso", "nodes": "node-a", "disable": 0},
            {"storage": "vm-images", "type": "lvm", "content": "images", "nodes": "node-a", "disable": False},
            {"storage": "inactive-dir", "type": "dir", "content": "import", "nodes": "node-a", "disable": 0},
        ],
    }


def test_selector_returns_two_exact_free_ids_and_only_active_capabilities() -> None:
    result = MODULE.inspect(sample())
    assert result == {
        "target_node": "node-a",
        "template_vmid_candidate": 401,
        "pbs_vmid_candidate": 402,
        "import_store_status": "candidates_present",
        "active_file_import_stores": [{"storage": "stage-dir", "type": "dir", "content": ["import", "iso"]}],
        "active_image_stores": [
            {"storage": "vm-images", "type": "lvm", "content": ["images"]},
        ],
        "declared_vm_image_storage_available": True,
    }


def test_no_import_capable_file_storage_is_a_fixed_absence() -> None:
    data = sample()
    data["node_storage"] = [row for row in data["node_storage"] if row["storage"] != "stage-dir"]
    data["cluster_storage"] = [row for row in data["cluster_storage"] if row["storage"] != "stage-dir"]
    result = MODULE.inspect(data)
    assert result["import_store_status"] == "none"
    assert result["active_file_import_stores"] == []


def test_selector_does_not_recommend_glusterfs_import_storage() -> None:
    data = sample()
    data["node_storage"].append(
        {"storage": "unsupported-import", "type": "glusterfs", "content": "import", "active": 1}
    )
    data["cluster_storage"].append(
        {
            "storage": "unsupported-import",
            "type": "glusterfs",
            "content": "import",
            "nodes": "node-a",
            "disable": 0,
        }
    )

    result = MODULE.inspect(data)

    assert "unsupported-import" not in {
        item["storage"] for item in result["active_file_import_stores"]
    }


def test_inactive_storage_is_returned_by_proxmox_but_not_recommended() -> None:
    data = sample()

    result = MODULE.inspect(data)

    assert "inactive-dir" not in {
        item["storage"] for item in result["active_file_import_stores"]
    }


def test_selector_refuses_mismatched_exact_nextid_echo() -> None:
    data = sample()
    data["nextid_checks"][1]["data"] = 402
    try:
        MODULE.inspect(data)
    except MODULE.Refusal as error:
        assert "did not echo" in str(error)
    else:
        raise AssertionError("mismatched exact nextid response was accepted")


def test_selector_refuses_duplicate_and_malformed_storage_rows() -> None:
    data = sample()
    data["node_storage"].append(dict(data["node_storage"][0]))
    try:
        MODULE.inspect(data)
    except MODULE.Refusal as error:
        assert "duplicate" in str(error)
    else:
        raise AssertionError("duplicate storage row was accepted")

    data = sample()
    data["cluster_storage"][0]["content"] = "import, import"
    try:
        MODULE.inspect(data)
    except MODULE.Refusal as error:
        assert "malformed" in str(error)
    else:
        raise AssertionError("malformed storage capability was accepted")


def test_survey_is_dev_bound_get_only_and_outputs_only_sanitized_receipt() -> None:
    plays = yaml.safe_load((PLAYBOOK_DIR / "survey-pbs-provisioning-candidates.yml").read_text())
    assert plays[1]["ansible.builtin.import_playbook"] == "require-reviewed-checkout.yml"
    play = next(item for item in plays if item.get("name") == "Survey private PBS provisioning candidates")
    assert "groups['o11y_svc']" in str(play["vars"])
    assert "proxmox_pbs_storage_node" in str(play["vars"])
    assert "proxmox_pbs_vm_storage_id" in str(play["vars"])
    uri_tasks = [task for task in play["tasks"] if "ansible.builtin.uri" in task]
    assert uri_tasks
    assert all(task["ansible.builtin.uri"]["method"] == "GET" for task in uri_tasks)
    assert all(task.get("no_log") is True and task.get("check_mode") is False for task in uri_tasks)
    task_by_name = {task.get("name"): task for task in play["tasks"]}
    assert task_by_name["Require a bounded VMID search window"].get("no_log") is not True
    assert task_by_name["Initialize exact VMID receipts"].get("no_log") is not True
    assert any("cluster/nextid?vmid={{ item }}" in task["ansible.builtin.uri"]["url"] for task in uri_tasks)
    assert any(
        task["ansible.builtin.uri"]["url"].endswith(
            "/api2/json/nodes/{{ _validated_node | urlencode }}/storage"
        )
        for task in uri_tasks
    )
    assert any(task["ansible.builtin.uri"]["url"].endswith("/api2/json/storage") for task in uri_tasks)
    assert not any(task["ansible.builtin.uri"].get("method") != "GET" for task in uri_tasks)
    assert "_candidate_receipt" in str(play["tasks"][-1])

    templates = yaml.safe_load((ROOT / "platform" / "semaphore" / "templates.yml").read_text())["templates"]
    template = next(item for item in templates if item["name"] == "Survey PBS Provisioning Candidates (Dev)")
    assert template["repository"] == "agent-cloud dev"
    assert [var["name"] for var in template["survey_vars"]] == ["expected_repository_sha"]


@needs_ansible
@pytest.mark.parametrize(
    "bao_hosts,environment_url,expected_checks",
    [
        (["bao"], "https://bao.example.invalid:8200", [True, True, True]),
        (["bao"], "https://other.example.invalid:8200", [True, True, False]),
        ([], "https://bao.example.invalid:8200", [False, False, False]),
        (["bao", "bao-duplicate"], "https://bao.example.invalid:8200", [False, True, True]),
    ],
    ids=["matching-semaphore-environment", "url-mismatch", "missing-inventory", "ambiguous-inventory"],
)
def test_openbao_environment_must_match_the_sole_inventory_host(
    tmp_path: Path, bao_hosts: list[str], environment_url: str, expected_checks: list[bool]
) -> None:
    fixture_hosts = {
        host: {"service_url": "https://bao.example.invalid:8200"}
        for host in bao_hosts
    }
    harness = r'''
import json, sys, yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template

play = next(item for item in yaml.safe_load(open(sys.argv[1]))
            if item.get("name") == "Survey private PBS provisioning candidates")
payload = json.load(sys.stdin)
variables = {
    "groups": {"openbao_svc": payload["bao_hosts"]},
    "hostvars": payload["hostvars"],
    "openbao_addr": payload["environment_url"],
}
for name in ("_openbao_hosts", "_openbao_host", "_openbao_inventory_url", "_bao_url"):
    variables[name] = Templar(loader=DataLoader(), variables=variables).template(
        trust_as_template(play["vars"][name])
    )
guard = next(
    task for task in play["tasks"]
    if task.get("name") == "Require the exact Dev run and one private declaration"
)
conditions = guard["ansible.builtin.assert"]["that"]
templar = Templar(loader=DataLoader(), variables=variables)
checked = [
    templar.evaluate_conditional(trust_as_template(condition))
    for condition in conditions
    if "_openbao_hosts | length == 1" in condition
    or "_openbao_inventory_url | string | length > 0" in condition
    or "openbao_addr | default('') == _openbao_inventory_url" in condition
]
print(json.dumps({"url": variables["_bao_url"], "checked": checked}))
'''
    payload = {
        "bao_hosts": bao_hosts,
        "environment_url": environment_url,
        "hostvars": fixture_hosts,
    }
    result = subprocess.run(
        [ansible_python(), "-c", harness, str(PLAYBOOK_DIR / "survey-pbs-provisioning-candidates.yml")],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "ANSIBLE_LOCAL_TEMP": str(tmp_path)},
    )
    assert result.returncode == 0, result.stderr
    resolved = json.loads(result.stdout)
    assert resolved["checked"] == expected_checks
    if bao_hosts:
        assert resolved["url"] == "https://bao.example.invalid:8200"
    else:
        assert resolved["url"] == ""


def test_semaphore_openbao_injection_is_allowed_but_url_must_be_inventory_bound() -> None:
    play = next(
        item for item in yaml.safe_load((PLAYBOOK_DIR / "survey-pbs-provisioning-candidates.yml").read_text())
        if item.get("name") == "Survey private PBS provisioning candidates"
    )
    override_names = set(play["vars"]["_survey_override_names"])
    assert {"openbao_addr", "bao_role_id", "bao_secret_id"}.isdisjoint(override_names)
    assert {"target_service", "target_host", "target_node", "proxmox_validate_certs"} <= override_names
    assert "bao_role_id | default(lookup('env', 'BAO_ROLE_ID'))" in play["vars"]["_bao_role_id"]
    assert "bao_secret_id | default(lookup('env', 'BAO_SECRET_ID'))" in play["vars"]["_bao_secret_id"]
    assert play["vars"]["_bao_url"] == "{{ _openbao_inventory_url }}"

    tasks = play["tasks"]
    guard_index = next(
        i for i, task in enumerate(tasks)
        if task.get("name") == "Require the exact Dev run and one private declaration"
    )
    bao_guard_index = next(
        i for i, task in enumerate(tasks)
        if task.get("name") == "Refuse cleartext OpenBao transport"
    )
    auth_index = next(
        i for i, task in enumerate(tasks)
        if task.get("name") == "Read Proxmox API credentials from OpenBao"
    )
    first_uri_index = next(
        i for i, task in enumerate(tasks) if "ansible.builtin.uri" in task
    )
    assert guard_index < bao_guard_index < auth_index < first_uri_index


@needs_ansible
def test_actual_ansible_extra_vars_cannot_redirect_the_inventory_openbao_url(tmp_path: Path) -> None:
    inventory = tmp_path / "inventory.yml"
    inventory.write_text(yaml.safe_dump({
        "all": {
            "hosts": {"localhost": {"ansible_connection": "local"}},
            "children": {
                "openbao_svc": {"hosts": {"bao": {"service_url": "https://inventory.example.invalid:8200"}}},
                "o11y_svc": {"hosts": {"o11y": {
                    "proxmox_pbs_storage_node": "pbs-node",
                    "proxmox_pbs_vm_storage_id": "vm-images",
                }}},
            },
        },
    }))
    playbook = PLAYBOOK_DIR / "survey-pbs-provisioning-candidates.yml"
    harness = r'''
import importlib.util, json, sys, yaml
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
manager._extra_vars = payload["extra_vars"]
hostvars = HostVars(inventory, manager, loader)
plays = yaml.safe_load(open(sys.argv[1]))
play_doc = next(p for p in plays if p.get("name") == "Survey private PBS provisioning candidates")
play = Play.load({
    "name": play_doc["name"],
    "hosts": play_doc["hosts"],
    "connection": play_doc["connection"],
    "gather_facts": play_doc["gather_facts"],
    "vars": play_doc["vars"],
}, variable_manager=manager, loader=loader)
variables = manager.get_vars(play=play, host=inventory.get_host("localhost"))
play_vars = play.get_vars()
for name in ("_openbao_hosts", "_openbao_host", "_openbao_inventory_url", "_bao_url"):
    variables[name] = Templar(loader=loader, variables=variables).template(
        trust_as_template(play_vars[name])
    )

# Use the real source-name filter against the extra-var mapping VariableManager applies.
ansible_vars.load_extra_vars = lambda loader: manager.extra_vars
spec = importlib.util.spec_from_file_location("extra_var_names", payload["filter_path"])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
extra_names = module.FilterModule().filters()["extra_var_names"]("")
templar = Templar(loader=loader, variables=variables)
override_names = templar.template(play_vars["_survey_override_names"])
variables["_survey_forged_names"] = sorted(set(extra_names) & set(override_names))
templar = Templar(loader=loader, variables=variables)
guard = next(t for t in play_doc["tasks"]
             if t.get("name") == "Require the exact Dev run and one private declaration")
conditions = guard["ansible.builtin.assert"]["that"]
probes = [
    expression for expression in conditions
    if any(token in expression for token in (
        "_survey_forged_names | length == 0",
        "_openbao_hosts | length == 1",
        "_openbao_inventory_url | string | length > 0",
        "openbao_addr | default('') == _openbao_inventory_url",
    ))
]
assert len(probes) == 4, probes
checks = [templar.evaluate_conditional(trust_as_template(expr)) for expr in probes]
print(json.dumps({
    "groups": variables["groups"]["openbao_svc"],
    "hostvars_type": type(variables["hostvars"]).__name__,
    "url": variables["_bao_url"],
    "environment_url": variables["openbao_addr"],
    "extra_names": extra_names,
    "forged_names": variables["_survey_forged_names"],
    "checks": checks,
}))
'''
    extra_vars = {
        "service_url": "https://attacker.example.invalid:8200",
        "openbao_addr": "https://attacker.example.invalid:8200",
        "groups": {"openbao_svc": ["attacker"]},
        "hostvars": {"bao": {"service_url": "https://attacker.example.invalid:8200"}},
    }
    result = subprocess.run(
        [ansible_python(), "-c", harness, str(playbook), str(inventory)],
        input=json.dumps({
            "extra_vars": extra_vars,
            "filter_path": str(PLAYBOOK_DIR / "filter_plugins" / "extra_var_names.py"),
        }),
        text=True,
        capture_output=True,
        check=False,
        cwd=ROOT,
        env={**os.environ, "ANSIBLE_LOCAL_TEMP": str(tmp_path)},
    )
    assert result.returncode == 0, result.stderr
    resolved = json.loads(result.stdout)
    assert resolved["groups"] == ["bao"]
    assert resolved["hostvars_type"] == "HostVars"
    assert resolved["url"] == "https://attacker.example.invalid:8200"
    assert resolved["environment_url"] == resolved["url"]
    assert set(resolved["extra_names"]) == {"service_url", "openbao_addr", "groups", "hostvars"}
    assert resolved["forged_names"] == ["service_url"]
    assert resolved["checks"] == [False, True, True, True]


def test_cli_never_echoes_private_candidate_data_on_refusal(monkeypatch, capsys) -> None:
    data = sample()
    data["nextid_checks"] = []
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(json.dumps(data)))
    monkeypatch.setattr(sys, "argv", [str(HELPER_PATH)])
    assert MODULE.main() == 2
    output = capsys.readouterr().out
    assert output == '{"refusal":"Proxmox candidate data failed strict validation."}\n'
    assert "node-a" not in output and "vm-images" not in output
