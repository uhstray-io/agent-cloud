"""Guards for the private GET-only PBS inventory candidate survey."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import yaml

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


def test_cli_never_echoes_private_candidate_data_on_refusal(monkeypatch, capsys) -> None:
    data = sample()
    data["nextid_checks"] = []
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(json.dumps(data)))
    monkeypatch.setattr(sys, "argv", [str(HELPER_PATH)])
    assert MODULE.main() == 2
    output = capsys.readouterr().out
    assert output == '{"refusal":"Proxmox candidate data failed strict validation."}\n'
    assert "node-a" not in output and "vm-images" not in output
