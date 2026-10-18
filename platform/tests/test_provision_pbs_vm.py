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
        "Require one reserved or active NetBox address record",
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
    assert "_pbs_target_config.status in [404, 500]" in source
    assert "_pbs_target_nextid.status == 200" in source
    assert "_pbs_target_nextid.json.data | int == _vmid | int" in source


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
