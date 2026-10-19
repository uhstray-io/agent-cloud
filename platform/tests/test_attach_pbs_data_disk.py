"""Focused guards for the PBS virtual data-disk attachment workflow."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
PLAYBOOK = ROOT / "platform/playbooks/attach-pbs-data-disk.yml"


def _tasks(play: dict) -> list[dict]:
    result: list[dict] = []
    for task in play.get("tasks", []):
        result.append(task)
        result.extend(_tasks({"tasks": task.get("block", [])}))
    return result


def test_workflow_reruns_pinned_preflight_before_vm_config_read_and_write() -> None:
    plays = yaml.safe_load(PLAYBOOK.read_text())
    assert plays[0]["ansible.builtin.import_playbook"] == "refuse-internal-extra-vars.yml"
    assert plays[1]["ansible.builtin.import_playbook"] == "require-reviewed-checkout.yml"
    assert plays[1]["vars"]["_reviewed_checkout_payload_paths"] == [
        "platform/playbooks/attach-pbs-data-disk.yml",
        "platform/playbooks/refuse-internal-extra-vars.yml",
        "platform/playbooks/preflight-o11y-pbs-thick-lvm-allocation.yml",
        "platform/playbooks/survey-o11y-pbs-physical-storage.yml",
        "platform/playbooks/tasks/assert-bao-transport.yml",
        "platform/playbooks/files/inspect_o11y_pbs_physical_storage.py",
        "platform/semaphore/templates.yml",
    ]
    assert plays[2]["ansible.builtin.import_playbook"] == "preflight-o11y-pbs-thick-lvm-allocation.yml"
    assert plays[2]["vars"]["_extra_var_guard_nested"] is True
    play = plays[3]
    tasks = _tasks(play)
    names = [task.get("name", "") for task in tasks]
    assert play["hosts"] == "localhost"
    assert names.index("Refuse VM identity, layout, bus, or storage drift before any write") < names.index(
        "Allocate exactly one virtual data disk through the Proxmox API"
    )
    assert names.index("Read the exact declared PBS VM configuration") < names.index(
        "Allocate exactly one virtual data disk through the Proxmox API"
    )


def test_only_guarded_api_allocation_writes_exact_scsi1_with_config_digest() -> None:
    tasks = _tasks(yaml.safe_load(PLAYBOOK.read_text())[3])
    writes = [
        task["ansible.builtin.uri"]
        for task in tasks
        if task.get("ansible.builtin.uri", {}).get("method") in {"POST", "PUT", "PATCH", "DELETE"}
    ]
    assert len(writes) == 1
    assert "/nodes/{{ _node | urlencode }}/qemu/{{ _vmid }}/config" in writes[0]["url"]
    assert writes[0]["body"] == {"scsi1": "{{ _disk_request }}", "digest": "{{ _vm_config_digest }}"}
    assert "{{ _storage }}:512" in str(yaml.safe_load(PLAYBOOK.read_text())[3]["vars"])
    source = PLAYBOOK.read_text()
    assert "vm-{{ _vmid }}-disk-[0-9]+,size=512G" in source
    assert "host-device" in source
    assert "disk_keys | difference(['scsi0', 'scsi1', 'ide2']) | length == 0" in source


def test_conflicting_or_mismatched_existing_disks_refuse_but_exact_scsi1_is_idempotent() -> None:
    tasks = _tasks(yaml.safe_load(PLAYBOOK.read_text())[3])
    validate = next(task for task in tasks if task.get("name", "").startswith("Refuse VM identity"))
    checks = " ".join(validate["ansible.builtin.assert"]["that"])
    assert "_disk_keys | difference(['scsi0', 'scsi1', 'ide2']) | length == 0" in checks
    assert "_scsi1 | length == 0 or _scsi1 is search(_expected_disk_pattern)" in checks
    allocate = next(task for task in tasks if task.get("name", "").startswith("Allocate exactly one"))
    assert allocate["when"] == ["not ansible_check_mode", "_scsi1 | length == 0"]
    assert "digest" in checks and "_vm_config_digest" in checks
    final_readback = next(task for task in tasks if task.get("name", "").startswith("Require exact scsi1 readback"))
    assert "_final_config.json.data.scsi1" in str(final_readback["ansible.builtin.assert"])


def test_pbs_data_disk_inventory_must_match_pinned_store_and_exact_512g_declaration() -> None:
    tasks = _tasks(yaml.safe_load(PLAYBOOK.read_text())[3])
    declaration = next(
        task for task in tasks if task.get("name", "").startswith("Require one declared PBS VM")
    )
    checks = declaration["ansible.builtin.assert"]["that"]
    assert "_decl.pbs_data_disk_storage | default('') == _storage" in checks
    assert "_decl.pbs_data_disk_size | default('') == '512G'" in checks
    override_guard = next(
        task for task in tasks if task.get("name", "").startswith("Refuse launch-time PBS")
    )
    assert "pbs_data_disk_storage" in str(override_guard)
    assert "pbs_data_disk_size" in str(override_guard)
    assert "_disk_request: \"{{ _storage }}:512\"" in PLAYBOOK.read_text()


def test_check_mode_reads_and_reports_without_any_mutation_or_host_device_operation() -> None:
    plays = yaml.safe_load(PLAYBOOK.read_text())
    tasks = _tasks(plays[3])
    post = next(task for task in tasks if task.get("name", "").startswith("Allocate exactly one"))
    assert "not ansible_check_mode" in post["when"]
    assert all("ansible.builtin.command" not in task for task in tasks)
    assert not any("/dev/" in str(task) or "ansible.builtin.shell" in task for task in tasks)
    config_reads = [
        task["ansible.builtin.uri"]
        for task in tasks
        if task.get("ansible.builtin.uri", {}).get("method") == "GET"
        and "/qemu/{{ _vmid }}/config" in task.get("ansible.builtin.uri", {}).get("url", "")
    ]
    assert len(config_reads) == 2
    assert all(task.get("check_mode") is False for task in tasks if task.get("ansible.builtin.uri", {}).get("method") == "GET")


def test_semaphore_template_is_dev_bound_and_only_accepts_reviewed_sha() -> None:
    templates = yaml.safe_load((ROOT / "platform/semaphore/templates.yml").read_text())["templates"]
    template = next(item for item in templates if item["name"] == "Attach PBS Data Disk (Dev)")
    assert template["playbook"] == "platform/playbooks/attach-pbs-data-disk.yml"
    assert template["repository"] == "agent-cloud dev"
    assert template["survey_vars"] == [
        {
            "name": "expected_repository_sha",
            "title": "Reviewed commit SHA",
            "description": "Exact reviewed Dev revision for the guarded PBS virtual-disk attachment",
            "type": "string",
            "required": True,
        }
    ]
