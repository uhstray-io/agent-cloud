"""Focused guards for the PBS-specific path through the shared VM lifecycle."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
PLAYBOOK_DIR = ROOT / "platform" / "playbooks"


def _tasks(play: dict) -> list[dict]:
    """Return nested task blocks in execution order for structural gate checks."""
    result: list[dict] = []
    for task in play.get("tasks", []):
        result.append(task)
        result.extend(_tasks({"tasks": task.get("block", [])}))
    return result


def test_pbs_wrapper_is_sha_pinned_and_has_no_launch_identity_survey() -> None:
    wrapper = yaml.safe_load((PLAYBOOK_DIR / "provision-pbs-vm.yml").read_text())
    assert wrapper[0]["ansible.builtin.import_playbook"] == "refuse-internal-extra-vars.yml"
    assert wrapper[1]["ansible.builtin.import_playbook"] == "require-reviewed-checkout.yml"
    assert wrapper[1]["vars"]["_reviewed_checkout_payload_paths"] == [
        "platform/playbooks/provision-pbs-vm.yml",
        "platform/playbooks/provision-vm.yml",
        "platform/semaphore/templates.yml",
    ]
    assert wrapper[-1]["vars"]["target_service"] == "pbs"
    assert wrapper[-1]["vars"]["_extra_var_guard_nested"] is True

    templates = yaml.safe_load((ROOT / "platform/semaphore/templates.yml").read_text())["templates"]
    pbs_template = next(item for item in templates if item["name"] == "Provision PBS VM (Dev)")
    assert pbs_template["repository"] == "agent-cloud dev"
    assert pbs_template["survey_vars"] == [
        {
            "name": "expected_repository_sha",
            "title": "Reviewed commit SHA",
            "description": "Exact Dev commit required on the Semaphore controller",
            "type": "string",
            "required": True,
        }
    ]


def test_pbs_netbox_and_exact_pve_guards_precede_every_mutation() -> None:
    plays = yaml.safe_load((PLAYBOOK_DIR / "provision-vm.yml").read_text())
    play = next(item for item in plays if item.get("name") == "Provision VM from template")
    tasks = _tasks(play)
    names = [task.get("name", "") for task in tasks]
    required_preflights = [
        "Refuse launch-time PBS allocation overrides",
        "Require the PBS template and VM declaration to agree",
        "Read the PBS address from the NetBox authority",
        "Require one reserved PBS record or its exact assigned VM record",
        "Require the exact declared Debian template configuration",
        "Read the exact PBS target VM config and exact VMID availability",
        "Check exact PBS target VMID availability",
        "Refuse any PBS target VMID, node, disk, or address mismatch before writes",
        "Refuse a census/config/nextid contradiction for PBS",
    ]
    positions = [names.index(name) for name in required_preflights]
    first_write = next(
        index
        for index, task in enumerate(tasks)
        if task.get("ansible.builtin.uri", {}).get("method") in {"POST", "PUT", "PATCH", "DELETE"}
    )
    assert positions == sorted(positions)
    assert max(positions) < first_write

    uri_tasks = [task["ansible.builtin.uri"] for task in tasks if "ansible.builtin.uri" in task]
    assert any("cluster/nextid?vmid={{ _tmpl_vmid }}" in task.get("url", "") for task in uri_tasks)
    assert any("cluster/nextid?vmid={{ _vmid }}" in task.get("url", "") for task in uri_tasks)
    assert any(
        "nodes/{{ _node | urlencode }}/qemu/{{ _vmid }}/config" in task.get("url", "")
        and task.get("status_code") == [200, 404, 500]
        for task in uri_tasks
    )

    source = (PLAYBOOK_DIR / "provision-vm.yml").read_text()
    assert "description | default('') == 'Reserved for o11y PBS VM'" in source
    assert "assigned_object_type | default('') == 'virtualization.vminterface'" in source
    assert "virtual_machine | default({})).name | default('') == _name" in source
    assert "_pbs_target_config.status in [404, 500]" in source
    assert "_pbs_target_nextid.status == 200" in source
    assert "_pbs_target_nextid.json.data | int == _vmid | int" in source
    assert "_pbs_resumable_managed" in source
    assert "_pbs_extra_disk_keys | length) == 0" in source
    assert "_pbs_data_disk_valid" in source
    assert "proxmox_pbs_vm_storage_id" in source
    assert "vm-' ~ (_vmid | string) ~ '-disk-[0-9]+,size=512G" in source


def test_template_and_shared_lifecycle_use_identical_pinned_provenance() -> None:
    template = yaml.safe_load(
        (PLAYBOOK_DIR / "provision-pbs-debian-template.yml").read_text()
    )
    lifecycle = yaml.safe_load((PLAYBOOK_DIR / "provision-vm.yml").read_text())
    expected = (
        "Debian 13 generic cloud image; "
        "SHA-512=97675b27e69153002c4e13644e36200c8f9067f661dca00918c54f1cacbdb88d4bff8c0fbf5cf5d63a0397bdf0cc472d7a6372bae5281bf7ced756249c10f8a2"
        "; "
        "source=https://cloud.debian.org/cdimage/cloud/trixie/20260601-2496/"
        "debian-13-generic-amd64-20260601-2496.qcow2; detached signature unavailable"
    )
    source = template[2]["vars"]["_image_url"]
    publisher = next(
        play for play in template
        if play.get("name") == "Publish the pinned Debian cloud-init template"
    )
    assert publisher["vars"]["_image_provenance"] == expected.replace(
        "SHA-512=" + expected.split("SHA-512=")[1].split(";", 1)[0],
        "SHA-512={{ _image_sha512 }}",
    ).replace(
        "source=" + source,
        "source={{ _image_url }}",
    )
    provision = next(play for play in lifecycle if play.get("name") == "Provision VM from template")
    assert provision["vars"]["_pbs_template_description"] == expected


def test_pbs_adopts_only_complete_vm_or_stopped_template_clone() -> None:
    source = (PLAYBOOK_DIR / "provision-vm.yml").read_text()
    assert "_pbs_target_managed" in source
    assert "_pbs_partial_clone" in source
    assert "_pbs_target_status.json.data.status | default('')) == 'stopped'" in source
    assert "_pbs_existing_disk_size == _pbs_template_disk_size" in source
    assert "_pbs_target_config.json.data.ipconfig0 | default('')) == ('ip=' ~ _ip" in source
    assert "_pbs_vm_description" in source
    assert "_ci_user: \"{{ 'debian' if _pbs_mode else (ci_user | default(ansible_user)) }}\"" in source
    assert 'ciuser: "{{ _ci_user }}"' in source
    assert 'ansible_user: "{{ _ci_user }}"' in source
    assert "src: /etc/os-release" in source
    assert "ID=\"?debian\"?" in source
    assert "VERSION_ID=\"?13\"?" in source
    assert "default('pbs;debian13' if _pbs_mode else 'ubuntu;24.04')" in source

    # PBS relies on NetBox's authoritative reservation. It does not use ICMP as proof.
    probe = source.split('name: "Probe the declared address from the controller (ICMP)"', 1)[1]
    assert "- not (_pbs_mode | bool)" in probe.split("- name:", 1)[0]
    assert "private host and address details are omitted from this run output" in source


def test_configured_under_sized_vm_can_resume_only_with_exact_network_and_no_extra_disks() -> None:
    play = next(
        item for item in yaml.safe_load((PLAYBOOK_DIR / "provision-vm.yml").read_text())
        if item.get("name") == "Provision VM from template"
    )
    tasks = _tasks(play)
    classify = next(
        task for task in tasks
        if task.get("name") == "Classify a completed PBS VM or a safe interrupted clone"
    )
    managed = classify["ansible.builtin.set_fact"]["_pbs_target_managed"]
    resumable = classify["ansible.builtin.set_fact"]["_pbs_resumable_managed"]
    assert "_pbs_data_disk_valid" in managed and "_pbs_extra_disk_keys" in managed
    assert "_pbs_vm_description" in resumable
    assert "_pbs_template_disk_size" in resumable and "_disk | regex_replace('G$', '')" in resumable
    assert "ipconfig0" in resumable and "_pbs_extra_disk_keys" in resumable
    assert "['stopped', 'running']" in resumable
    assert "_pbs_scsi1 | length) == 0" in resumable
    resize = next(task for task in tasks if task.get("name", "").startswith("Resize disk to size"))
    assert "not (_pbs_target_managed | default(false) | bool)" in str(resize["when"])
    assert resize["ansible.builtin.uri"]["body"]["disk"] == "scsi0"
