"""Structural safety checks for the reviewed Debian cloud-template publisher."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
PLAYBOOK_PATH = ROOT / "platform/playbooks/provision-pbs-debian-template.yml"
IMAGE_URL = (
    "https://cloud.debian.org/cdimage/cloud/trixie/20260601-2496/"
    "debian-13-generic-amd64-20260601-2496.qcow2"
)
IMAGE_SHA512 = (
    "97675b27e69153002c4e13644e36200c8f9067f661dca00918c54f1cacbdb88"
    "d4bff8c0fbf5cf5d63a0397bdf0cc472d7a6372bae5281bf7ced756249c10f8a2"
)


def test_dev_template_is_bound_to_exact_reviewed_revision():
    plays = yaml.safe_load(PLAYBOOK_PATH.read_text())
    templates = yaml.safe_load((ROOT / "platform/semaphore/templates.yml").read_text())["templates"]
    declaration = [row for row in templates if row.get("name") == "Provision PBS Debian Cloud Template (Dev)"]

    assert len(declaration) == 1
    assert declaration[0]["playbook"] == "platform/playbooks/provision-pbs-debian-template.yml"
    assert declaration[0]["repository"] == "agent-cloud dev"
    assert declaration[0]["survey_vars"] == [{
        "name": "expected_repository_sha",
        "title": "Reviewed commit SHA",
        "description": "Exact reviewed Dev revision for pinned Debian cloud template publication",
        "type": "string",
        "required": True,
    }]
    assert plays[0]["ansible.builtin.import_playbook"] == "refuse-internal-extra-vars.yml"
    assert plays[1]["ansible.builtin.import_playbook"] == "require-reviewed-checkout.yml"
    assert plays[1]["vars"]["_reviewed_checkout_payload_paths"] == [
        "platform/playbooks/provision-pbs-debian-template.yml",
        "platform/semaphore/templates.yml",
    ]
    play = next(
        item for item in plays
        if item.get("name") == "Publish the pinned Debian cloud-init template"
    )
    override_guard = next(
        task for task in play["tasks"]
        if task.get("name") == "Refuse runtime overrides of private PBS template allocation"
    )
    assert {"openbao_addr", "bao_role_id", "bao_secret_id", "proxmox_validate_certs"} <= set(
        override_guard["vars"]["_pbs_template_allocation_names"]
    )


def test_publisher_refuses_check_mode_and_requires_private_inventory_declaration():
    plays = yaml.safe_load(PLAYBOOK_PATH.read_text())
    tasks = plays[2]["tasks"]
    first_uri = next(i for i, task in enumerate(tasks) if "ansible.builtin.uri" in task)

    assert tasks[0]["name"] == "Require a reviewed production-mode write run"
    gate = tasks[0]["ansible.builtin.assert"]["that"]
    assert any("expected_repository_sha" in str(item) for item in gate)
    assert any("ansible_check_mode" in str(item) for item in gate)
    assert first_uri > next(i for i, task in enumerate(tasks) if "pbs_svc" in str(task))

    declaration_gate = next(
        task for task in tasks
        if task["name"] == "Require one PBS inventory declaration and all required fields"
    )
    declaration_checks = declaration_gate["ansible.builtin.assert"]["that"]
    for field in (
        "pbs_debian_template_vmid", "pbs_debian_template_name", "pbs_debian_template_node",
        "pbs_debian_template_import_storage", "vm_disk_storage",
    ):
        assert field in str(declaration_checks) or field in str(plays[2]["vars"])
    assert "_decl_hosts | length == 1" in str(declaration_checks)


def test_fixed_debian_provenance_sha512_and_proxmox_readbacks_are_enforced():
    text = PLAYBOOK_PATH.read_text()
    plays = yaml.safe_load(text)
    tasks = plays[2]["tasks"]
    task_names = [task["name"] for task in tasks]
    uri_tasks = [task for task in tasks if "ansible.builtin.uri" in task]

    assert IMAGE_URL in text
    assert IMAGE_SHA512 in text
    assert "checksum-algorithm: sha512" in text
    assert "verify-certificates: \"1\"" in text
    assert "detached signature unavailable" in text
    assert "Debian does not provide a detached" in text
    download_task = tasks[task_names.index("Download the pinned image into private Proxmox import storage")]
    assert "/download-url" in download_task["ansible.builtin.uri"]["url"]
    assert download_task["ansible.builtin.uri"]["body"]["checksum"] == "{{ _image_sha512 }}"
    assert "Require the exact staged file and import type" in task_names
    assert "file-based" in str(
        tasks[task_names.index("Require live import and VM image storage capabilities")]["ansible.builtin.assert"]
    )
    assert "Require a matching cloud-init template" in task_names
    assert "status_code: [200, 404, 500]" in text
    assert "cluster/resources?type=vm" in text
    assert "cluster/nextid?vmid={{ _vmid }}" in text
    assert "status_code: 200" in text
    assert all(task.get("no_log") is True for task in uri_tasks)
    assert "curl" not in text
    assert "api_token" not in " ".join(str(task.get("ansible.builtin.debug", {})) for task in tasks)
    assert not any("status/start" in str(task) for task in tasks)


def test_config_http_500_is_absence_only_when_exact_vmid_check_proves_free():
    plays = yaml.safe_load(PLAYBOOK_PATH.read_text())
    tasks = plays[2]["tasks"]
    names = [task["name"] for task in tasks]
    config_index = names.index("Read the exact declared VM configuration if present")
    nextid_index = names.index("Ask Proxmox whether the exact VMID is free")
    collision_index = names.index("Refuse VMID collisions and mismatched partial templates")
    config_statuses = tasks[config_index]["ansible.builtin.uri"]["status_code"]
    gate = tasks[collision_index]["ansible.builtin.assert"]["that"]

    assert config_statuses == [200, 404, 500]
    assert config_index < nextid_index < collision_index
    assert any("_existing_config.status in [404, 500]" in item for item in gate)
    assert any("_exact_vmid_check.status == 200" in item for item in gate)
    assert any(
        "_existing_config.status == 200 and _exact_vmid_check.status == 400" in item
        for item in gate
    )
    assert any("_existing_config.status == 200 or _vmid_rows | length == 0" in item for item in gate)
    create = tasks[names.index("Create the VM from the verified image when its VMID is free")]
    assert create["when"] == "_existing_config.status in [404, 500]"


def test_every_pve_write_follows_review_mode_inventory_and_collision_gates():
    plays = yaml.safe_load(PLAYBOOK_PATH.read_text())
    tasks = plays[2]["tasks"]
    names = [task["name"] for task in tasks]
    first_write = min(
        i for i, task in enumerate(tasks)
        if "ansible.builtin.uri" in task
        and task["ansible.builtin.uri"].get("method", "GET") in {"POST", "PUT", "DELETE"}
    )

    for gate in (
        "Require a reviewed production-mode write run",
        "Refuse runtime overrides of private PBS template allocation",
        "Require one PBS inventory declaration and all required fields",
        "Require HTTPS and complete Proxmox credentials",
        "Require live import and VM image storage capabilities",
        "Refuse VMID collisions and mismatched partial templates",
    ):
        assert names.index(gate) < first_write
    assert plays[1]["ansible.builtin.import_playbook"] == "require-reviewed-checkout.yml"

    assert "Read back the staged import volume" in names
    assert "Read back the final template configuration" in names
